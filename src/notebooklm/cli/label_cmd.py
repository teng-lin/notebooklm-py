"""Source-label management CLI commands — thin Click-handler layer (ADR-0008).

Each command resolves CLI inputs, delegates mutation/read workflows to the
transport-neutral :mod:`notebooklm._app.labels` core, uses
``cli/services/label_listing.py`` for the members-to-titles ``label list``
render pipeline and resolver re-export, and renders results. The typed
:class:`LabelResolutionError` is mapped through ``output_error`` so the
``--json`` envelope contract (ADR-0015) holds throughout.

Commands:
    list      List labels (with member ids + titles)
    sources   Expand a label to its source objects
    generate  AI-group sources into labels (Reorganize)
    create    Create an empty named label
    rename    Rename a label
    emoji     Set a label's emoji
    add       Add source(s) to a label
    remove    Un-assign source(s) from a label
    delete    Delete one or more labels
"""

from __future__ import annotations

from typing import Any, NoReturn

import click

from .._app.labels import (
    execute_label_add_sources,
    execute_label_create,
    execute_label_delete,
    execute_label_generate,
    execute_label_remove_sources,
    execute_label_rename,
    execute_label_set_emoji,
    execute_label_sources,
)
from ..types import Label
from .auth_runtime import resolve_client_factory, with_client
from .error_handler import output_error
from .options import json_option, notebook_option
from .rendering import cli_print, json_output_response, render_list
from .resolve import require_notebook, resolve_notebook_id, resolve_source_ids
from .services.confirming_mutation import MutationPlan, run_confirmed_mutation
from .services.label_listing import (
    LabelListPlan,
    LabelResolutionError,
    execute_label_list,
    resolve_label_id,
)


def _handle_label_resolution_error(exc: LabelResolutionError, *, json_output: bool) -> NoReturn:
    """Render a typed label-resolution error through the CLI error contract."""
    output_error(
        exc.message,
        code=exc.code,
        json_output=json_output,
        exit_code=1,
        extra=dict(exc.extra) if exc.extra else None,
    )
    raise AssertionError("unreachable")  # pragma: no cover


def _label_payload(label: Label) -> dict[str, Any]:
    """Stable public JSON shape for a single label mutation result."""
    return {
        "id": label.id,
        "name": label.name,
        "emoji": label.emoji,
        "source_ids": list(label.source_ids),
    }


@click.group()
def label():
    """Source-label management commands.

    \b
    Commands:
      list      List labels (with member ids + titles)
      sources   Expand a label to its source objects
      generate  AI-group sources into labels (the UI's "Reorganize")
      create    Create an empty named label
      rename    Rename a label
      emoji     Set a label's emoji
      add       Add source(s) to a label
      remove    Un-assign source(s) from a label (the sources survive)
      delete    Delete one or more labels (the label only, not its sources)

    \b
    Name-or-ID Support:
      <id|name> arguments accept a label id (or partial prefix) OR an exact
      label name. An ambiguous name lists the matching ids — specify the id.
    """


@label.command("list")
@notebook_option
@json_option
@with_client
def label_list(ctx, notebook_id, json_output, client_auth):
    """List all labels in a notebook (with member ids + resolved titles)."""
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with resolve_client_factory(ctx)(client_auth) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)
            plan = LabelListPlan(
                notebook_id=nb_id_resolved,
                json_output=json_output,
                limit=None,
                no_truncate=False,
            )
            render_list(await execute_label_list(client, plan))

    return _run()


@label.command("sources")
@click.argument("label_ref")
@notebook_option
@json_option
@with_client
def label_sources(ctx, label_ref, notebook_id, json_output, client_auth):
    """Expand a label to its source objects (group -> sources).

    LABEL_REF can be a label id (or partial prefix) or an exact label name.
    """
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with resolve_client_factory(ctx)(client_auth) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)
            try:
                label_id = await resolve_label_id(
                    client, nb_id_resolved, label_ref, json_output=json_output
                )
            except LabelResolutionError as exc:
                _handle_label_resolution_error(exc, json_output=json_output)
            sources = await execute_label_sources(client, nb_id_resolved, label_id)

            if json_output:
                json_output_response(
                    {
                        "notebook_id": nb_id_resolved,
                        "label_id": label_id,
                        "sources": [{"id": s.id, "title": s.title, "url": s.url} for s in sources],
                        "count": len(sources),
                    }
                )
                return

            if not sources:
                cli_print("[yellow]No sources in this label[/yellow]", ctx=ctx)
                return
            for source in sources:
                cli_print(f"{source.id}  {source.title or '-'}", ctx=ctx)

    return _run()


@label.command("generate")
@notebook_option
@click.option(
    "--scope",
    type=click.Choice(["all", "unlabeled"], case_sensitive=False),
    default="unlabeled",
    help="'unlabeled' (safe, default) labels only unlabeled sources; "
    "'all' WIPES and regenerates every label (destructive).",
)
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation for --scope all")
@json_option
@with_client
def label_generate(ctx, notebook_id, scope, yes, json_output, client_auth):
    """AI-group sources into topic labels (the UI's "Reorganize").

    \b
    --scope unlabeled  (default, safe) label only currently-unlabeled sources.
    --scope all        WIPE and regenerate EVERY label with new ids (destructive).
    """
    nb_id = require_notebook(notebook_id)
    scope = scope.lower()
    destructive = scope == "all"

    async def _run():
        async with resolve_client_factory(ctx)(client_auth) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)

            if destructive and not yes:
                if json_output:
                    output_error(
                        "Pass --yes to confirm --scope all (regenerates every label)",
                        code="CONFIRM_REQUIRED",
                        json_output=True,
                        exit_code=1,
                        extra={"notebook_id": nb_id_resolved, "scope": scope},
                    )
                    return
                if not click.confirm(
                    "--scope all wipes and regenerates EVERY label with new ids. Continue?"
                ):
                    cli_print("[yellow]Cancelled[/yellow]", ctx=ctx)
                    return

            labels = (await execute_label_generate(client, nb_id_resolved, scope)).labels

            if json_output:
                json_output_response(
                    {
                        "notebook_id": nb_id_resolved,
                        "scope": scope,
                        "labels": [_label_payload(label_) for label_ in labels],
                        "count": len(labels),
                    }
                )
                return

            cli_print(f"[green]Generated {len(labels)} label(s)[/green]", ctx=ctx)
            for label_ in labels:
                emoji = f"{label_.emoji} " if label_.emoji else ""
                cli_print(f"  {label_.id}  {emoji}{label_.name}", ctx=ctx)

    return _run()


@label.command("create")
@click.argument("name")
@notebook_option
@click.option("--emoji", default="", help="Optional emoji for the label")
@json_option
@with_client
def label_create(ctx, name, notebook_id, emoji, json_output, client_auth):
    """Create an empty, manually-named label."""
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with resolve_client_factory(ctx)(client_auth) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)
            label_ = await execute_label_create(client, nb_id_resolved, name, emoji)

            if json_output:
                json_output_response({"notebook_id": nb_id_resolved, **_label_payload(label_)})
                return

            cli_print(f"[green]Created label:[/green] {label_.id} ({label_.name})", ctx=ctx)

    return _run()


@label.command("rename")
@click.argument("label_ref")
@click.argument("new_name")
@notebook_option
@json_option
@with_client
def label_rename(ctx, label_ref, new_name, notebook_id, json_output, client_auth):
    """Rename a label (preserves its emoji).

    LABEL_REF can be a label id (or partial prefix) or an exact label name.
    """
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with resolve_client_factory(ctx)(client_auth) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)
            try:
                label_id = await resolve_label_id(
                    client, nb_id_resolved, label_ref, json_output=json_output
                )
            except LabelResolutionError as exc:
                _handle_label_resolution_error(exc, json_output=json_output)
            label_ = await execute_label_rename(client, nb_id_resolved, label_id, new_name)

            if json_output:
                json_output_response({"notebook_id": nb_id_resolved, **_label_payload(label_)})
                return

            cli_print(f"[green]Renamed label:[/green] {label_id} -> {new_name}", ctx=ctx)

    return _run()


@label.command("emoji")
@click.argument("label_ref")
@click.argument("emoji_value")
@notebook_option
@json_option
@with_client
def label_emoji(ctx, label_ref, emoji_value, notebook_id, json_output, client_auth):
    """Set a label's emoji.

    LABEL_REF can be a label id (or partial prefix) or an exact label name.
    """
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with resolve_client_factory(ctx)(client_auth) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)
            try:
                label_id = await resolve_label_id(
                    client, nb_id_resolved, label_ref, json_output=json_output
                )
            except LabelResolutionError as exc:
                _handle_label_resolution_error(exc, json_output=json_output)
            label_ = await execute_label_set_emoji(client, nb_id_resolved, label_id, emoji_value)

            if json_output:
                json_output_response({"notebook_id": nb_id_resolved, **_label_payload(label_)})
                return

            cli_print(f"[green]Set emoji on:[/green] {label_id} ({emoji_value})", ctx=ctx)

    return _run()


@label.command("add")
@click.argument("label_ref")
@click.argument("source_ids", nargs=-1, required=True)
@notebook_option
@json_option
@with_client
def label_add(ctx, label_ref, source_ids, notebook_id, json_output, client_auth):
    """Add source(s) to a label (append; existing members preserved).

    LABEL_REF can be a label id (or partial prefix) or an exact label name.
    SOURCE_IDS accept partial-prefix matching like every other source-id command.
    """
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with resolve_client_factory(ctx)(client_auth) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)
            try:
                label_id = await resolve_label_id(
                    client, nb_id_resolved, label_ref, json_output=json_output
                )
            except LabelResolutionError as exc:
                _handle_label_resolution_error(exc, json_output=json_output)
            resolved_source_ids = await resolve_source_ids(
                client, nb_id_resolved, source_ids, json_output=json_output
            )
            membership = await execute_label_add_sources(
                client, nb_id_resolved, label_id, resolved_source_ids or []
            )
            label_ = membership.label

            if json_output:
                json_output_response(
                    {
                        "notebook_id": nb_id_resolved,
                        "added_source_ids": membership.source_ids,
                        **_label_payload(label_),
                    }
                )
                return

            cli_print(
                f"[green]Added {len(membership.source_ids)} source(s) to:[/green] {label_id}",
                ctx=ctx,
            )

    return _run()


@label.command("remove")
@click.argument("label_ref")
@click.argument("source_ids", nargs=-1, required=True)
@notebook_option
@json_option
@with_client
def label_remove(ctx, label_ref, source_ids, notebook_id, json_output, client_auth):
    """Un-assign source(s) from a label (the inverse of ``add``).

    Removal un-assigns the source from this label only; it does NOT delete the
    source from the notebook (that is what ``source delete`` does) and leaves the
    source in any other label it belongs to. Because un-assigning is
    non-destructive, there is no ``--yes`` gate (unlike ``label delete``).

    LABEL_REF can be a label id (or partial prefix) or an exact label name.
    SOURCE_IDS accept partial-prefix matching like every other source-id command.
    """
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with resolve_client_factory(ctx)(client_auth) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)
            try:
                label_id = await resolve_label_id(
                    client, nb_id_resolved, label_ref, json_output=json_output
                )
            except LabelResolutionError as exc:
                _handle_label_resolution_error(exc, json_output=json_output)
            resolved_source_ids = await resolve_source_ids(
                client, nb_id_resolved, source_ids, json_output=json_output
            )
            membership = await execute_label_remove_sources(
                client, nb_id_resolved, label_id, resolved_source_ids or []
            )
            label_ = membership.label

            if json_output:
                json_output_response(
                    {
                        "notebook_id": nb_id_resolved,
                        "removed_source_ids": membership.source_ids,
                        **_label_payload(label_),
                    }
                )
                return

            cli_print(
                f"[green]Removed {len(membership.source_ids)} source(s) from:[/green] {label_id}",
                ctx=ctx,
            )

    return _run()


@label.command("delete")
@click.argument("label_refs", nargs=-1, required=True)
@notebook_option
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
@json_option
@with_client
def label_delete(ctx, label_refs, notebook_id, yes, json_output, client_auth):
    """Delete one or more labels (the label only, not its sources).

    LABEL_REFS accept label ids (or partial prefixes) or exact label names.
    """
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with resolve_client_factory(ctx)(client_auth) as client:

            async def resolve_delete(client):
                nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)
                try:
                    label_ids = [
                        await resolve_label_id(client, nb_id_resolved, ref, json_output=json_output)
                        for ref in label_refs
                    ]
                except LabelResolutionError as exc:
                    _handle_label_resolution_error(exc, json_output=json_output)

                if json_output and not yes:
                    output_error(
                        "Pass --yes to confirm deletion in --json mode",
                        code="CONFIRM_REQUIRED",
                        json_output=True,
                        exit_code=1,
                        extra={"label_ids": label_ids, "notebook_id": nb_id_resolved},
                    )
                    raise AssertionError("unreachable")  # pragma: no cover

                return {"notebook_id": nb_id_resolved, "label_ids": label_ids}

            async def execute_delete(client, resolved):
                await execute_label_delete(client, resolved["notebook_id"], resolved["label_ids"])

            plan = MutationPlan(
                entity_label="label",
                resolve=resolve_delete,
                confirm_message="Delete {resolved[label_ids]}?",
                execute=execute_delete,
                serialize_success=lambda resolved: {
                    "notebook_id": resolved["notebook_id"],
                    "label_ids": resolved["label_ids"],
                    "deleted": True,
                },
                serialize_cancel=lambda resolved: {
                    "notebook_id": resolved["notebook_id"],
                    "label_ids": resolved["label_ids"],
                    "deleted": False,
                    "status": "cancelled",
                },
            )
            result = await run_confirmed_mutation(
                plan,
                client,
                yes=yes,
                json_output=json_output,
                confirmer=click.confirm,
            )
            if json_output:
                json_output_response(result.payload)
                return
            if result.status == "cancelled":
                return

            cli_print(
                f"[green]Deleted {len(result.resolved['label_ids'])} label(s)[/green]",
                ctx=ctx,
            )

    return _run()
