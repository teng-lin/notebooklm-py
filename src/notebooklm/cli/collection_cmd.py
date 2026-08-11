"""Collection management CLI commands — thin Click-handler layer (ADR-0008).

Collections group whole notebooks (account-level, playlist-style), the sibling
of the notebook-scoped ``label`` group. Each command resolves CLI inputs,
delegates read/mutation workflows to the transport-neutral
:mod:`notebooklm._app.collections` core, and renders results. The typed
:class:`CollectionResolutionError` is mapped through ``output_error`` so the
``--json`` envelope contract (ADR-0015) holds throughout.

Commands:
    list       List collections (with notebook counts)
    notebooks  Expand a collection to its member notebooks
    create     Create an empty named collection
    rename     Rename a collection
    add        Add notebook(s) to a collection
    remove     Un-assign notebook(s) from a collection
    delete     Delete one or more collections
"""

from __future__ import annotations

from typing import Any, NoReturn

import click

from .._app.collections import (
    CollectionResolutionError,
    execute_collection_add_notebooks,
    execute_collection_create,
    execute_collection_delete,
    execute_collection_list,
    execute_collection_notebooks,
    execute_collection_remove_notebooks,
    execute_collection_rename,
    resolve_collection_id,
)
from .._app.errors import did_you_mean_hint
from ..types import Collection
from .auth_runtime import resolve_client_factory, with_client
from .error_handler import output_error
from .options import json_option
from .rendering import cli_print, json_output_response, render_list
from .resolve import resolve_partial_id_in_items
from .services.confirming_mutation import MutationPlan, run_confirmed_mutation
from .services.listing import ListSpec, prepare_list


def _handle_collection_resolution_error(
    exc: CollectionResolutionError, *, json_output: bool
) -> NoReturn:
    """Render a typed collection-resolution error through the CLI error contract."""
    candidates = list(exc.candidates)
    output_error(
        exc.message,
        code=exc.code,
        json_output=json_output,
        exit_code=1,
        extra=dict(exc.extra) if exc.extra else None,
        hint=did_you_mean_hint(candidates) if candidates else None,
    )
    raise AssertionError("unreachable")  # pragma: no cover


def _collection_payload(collection: Collection) -> dict[str, Any]:
    """Stable public JSON shape for a single collection mutation result."""
    return {
        "id": collection.id,
        "name": collection.name,
        "emoji": collection.emoji,
        "notebook_ids": list(collection.notebook_ids),
    }


async def _resolve_notebook_ids(client, refs, *, json_output: bool) -> list[str]:
    """Resolve each (possibly partial) notebook ref to a full notebook id.

    Fetches ``notebooks.list()`` **once** and resolves every ref against that
    shared snapshot (via the same ``resolve_partial_id_in_items`` core the single
    ``resolve_notebook_id`` uses), so ``collection add/remove C nb1 nb2 nb3`` does
    one account-level LIST rather than one per ref.
    """
    notebooks = await client.notebooks.list()
    return [
        resolve_partial_id_in_items(
            ref,
            notebooks,
            entity_name="notebook",
            list_command="list",
            json_output=json_output,
        )
        for ref in refs
    ]


@click.group()
def collection():
    """Collection management commands (account-level notebook groups).

    \b
    Commands:
      list       List collections (with notebook counts)
      notebooks  Expand a collection to its member notebooks
      create     Create an empty named collection
      rename     Rename a collection
      add        Add notebook(s) to a collection
      remove     Un-assign notebook(s) from a collection (the notebooks survive)
      delete     Delete one or more collections (the collection only, not its notebooks)

    \b
    Name-or-ID Support:
      <id|name> arguments accept a collection id (or partial prefix) OR an exact
      collection name. An ambiguous name lists the matching ids — specify the id.
    """


@collection.command("list")
@json_option
@with_client
def collection_list(ctx, json_output, client_auth):
    """List all collections in the account (with notebook counts)."""

    async def _run():
        async with resolve_client_factory(ctx)(client_auth) as client:
            spec = ListSpec(
                title="Collections",
                items_key="collections",
                fetch=lambda client, _: execute_collection_list(client),
                serialize=lambda c: {
                    "id": c.id,
                    "name": c.name,
                    "emoji": c.emoji,
                    "notebook_count": len(c.notebook_ids),
                },
                columns=["ID", "Emoji", "Name", "Notebooks"],
                row=lambda c: [c.id, c.emoji or "-", c.name, str(len(c.notebook_ids))],
                empty_message="[yellow]No collections found[/yellow]",
            )
            render_list(
                await prepare_list(
                    spec, client, notebook_id="", limit=None, json_output=json_output
                )
            )

    return _run()


@collection.command("notebooks")
@click.argument("collection_ref")
@json_option
@with_client
def collection_notebooks(ctx, collection_ref, json_output, client_auth):
    """Expand a collection to its member notebooks (group -> notebooks).

    COLLECTION_REF can be a collection id (or partial prefix) or an exact name.
    """

    async def _run():
        async with resolve_client_factory(ctx)(client_auth) as client:
            try:
                collection_id = await resolve_collection_id(
                    client, collection_ref, json_output=json_output
                )
            except CollectionResolutionError as exc:
                _handle_collection_resolution_error(exc, json_output=json_output)
            notebooks = await execute_collection_notebooks(client, collection_id)

            if json_output:
                json_output_response(
                    {
                        "collection_id": collection_id,
                        "notebooks": [{"id": nb.id, "title": nb.title} for nb in notebooks],
                        "count": len(notebooks),
                    }
                )
                return

            if not notebooks:
                cli_print("[yellow]No notebooks in this collection[/yellow]", ctx=ctx)
                return
            for nb in notebooks:
                cli_print(f"{nb.id}  {nb.title or '-'}", ctx=ctx)

    return _run()


@collection.command("create")
@click.argument("name")
@json_option
@with_client
def collection_create(ctx, name, json_output, client_auth):
    """Create an empty, named collection."""

    async def _run():
        async with resolve_client_factory(ctx)(client_auth) as client:
            collection_ = await execute_collection_create(client, name)

            if json_output:
                json_output_response(_collection_payload(collection_))
                return

            cli_print(
                f"[green]Created collection:[/green] {collection_.id} ({collection_.name})", ctx=ctx
            )

    return _run()


@collection.command("rename")
@click.argument("collection_ref")
@click.argument("new_name")
@json_option
@with_client
def collection_rename(ctx, collection_ref, new_name, json_output, client_auth):
    """Rename a collection (preserves its emoji).

    COLLECTION_REF can be a collection id (or partial prefix) or an exact name.
    """

    async def _run():
        async with resolve_client_factory(ctx)(client_auth) as client:
            try:
                collection_id = await resolve_collection_id(
                    client, collection_ref, json_output=json_output
                )
            except CollectionResolutionError as exc:
                _handle_collection_resolution_error(exc, json_output=json_output)
            collection_ = await execute_collection_rename(client, collection_id, new_name)

            if json_output:
                json_output_response(_collection_payload(collection_))
                return

            cli_print(f"[green]Renamed collection:[/green] {collection_id} -> {new_name}", ctx=ctx)

    return _run()


@collection.command("add")
@click.argument("collection_ref")
@click.argument("notebook_refs", nargs=-1, required=True)
@json_option
@with_client
def collection_add(ctx, collection_ref, notebook_refs, json_output, client_auth):
    """Add notebook(s) to a collection (append; existing members preserved).

    COLLECTION_REF can be a collection id (or partial prefix) or an exact name.
    NOTEBOOK_REFS accept partial-prefix matching like every other notebook-id arg.
    """

    async def _run():
        async with resolve_client_factory(ctx)(client_auth) as client:
            try:
                collection_id = await resolve_collection_id(
                    client, collection_ref, json_output=json_output
                )
            except CollectionResolutionError as exc:
                _handle_collection_resolution_error(exc, json_output=json_output)
            notebook_ids = await _resolve_notebook_ids(
                client, notebook_refs, json_output=json_output
            )
            membership = await execute_collection_add_notebooks(client, collection_id, notebook_ids)

            if json_output:
                json_output_response(
                    {
                        "added_notebook_ids": membership.notebook_ids,
                        **_collection_payload(membership.collection),
                    }
                )
                return

            cli_print(
                f"[green]Added {len(membership.notebook_ids)} notebook(s) to:[/green] "
                f"{collection_id}",
                ctx=ctx,
            )

    return _run()


@collection.command("remove")
@click.argument("collection_ref")
@click.argument("notebook_refs", nargs=-1, required=True)
@json_option
@with_client
def collection_remove(ctx, collection_ref, notebook_refs, json_output, client_auth):
    """Un-assign notebook(s) from a collection (the inverse of ``add``).

    Removal un-assigns the notebook from this collection only; it does NOT delete
    the notebook and leaves it in any other collection it belongs to. Because
    un-assigning is non-destructive, there is no ``--yes`` gate.

    COLLECTION_REF can be a collection id (or partial prefix) or an exact name.
    NOTEBOOK_REFS accept partial-prefix matching like every other notebook-id arg.
    """

    async def _run():
        async with resolve_client_factory(ctx)(client_auth) as client:
            try:
                collection_id = await resolve_collection_id(
                    client, collection_ref, json_output=json_output
                )
            except CollectionResolutionError as exc:
                _handle_collection_resolution_error(exc, json_output=json_output)
            notebook_ids = await _resolve_notebook_ids(
                client, notebook_refs, json_output=json_output
            )
            membership = await execute_collection_remove_notebooks(
                client, collection_id, notebook_ids
            )

            if json_output:
                json_output_response(
                    {
                        "removed_notebook_ids": membership.notebook_ids,
                        **_collection_payload(membership.collection),
                    }
                )
                return

            cli_print(
                f"[green]Removed {len(membership.notebook_ids)} notebook(s) from:[/green] "
                f"{collection_id}",
                ctx=ctx,
            )

    return _run()


@collection.command("delete")
@click.argument("collection_refs", nargs=-1, required=True)
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
@json_option
@with_client
def collection_delete(ctx, collection_refs, yes, json_output, client_auth):
    """Delete one or more collections (the collection only, not its notebooks).

    COLLECTION_REFS accept collection ids (or partial prefixes) or exact names.
    """

    async def _run():
        async with resolve_client_factory(ctx)(client_auth) as client:

            async def resolve_delete(client):
                try:
                    collections = await client.collections.list()
                    collection_ids = [
                        await resolve_collection_id(
                            client, ref, json_output=json_output, collections=collections
                        )
                        for ref in collection_refs
                    ]
                except CollectionResolutionError as exc:
                    _handle_collection_resolution_error(exc, json_output=json_output)

                if json_output and not yes:
                    output_error(
                        "Pass --yes to confirm deletion in --json mode",
                        code="CONFIRM_REQUIRED",
                        json_output=True,
                        exit_code=1,
                        extra={"collection_ids": collection_ids},
                    )
                    raise AssertionError("unreachable")  # pragma: no cover

                return {"collection_ids": collection_ids}

            async def execute_delete(client, resolved):
                await execute_collection_delete(client, resolved["collection_ids"])

            plan = MutationPlan(
                entity_label="collection",
                resolve=resolve_delete,
                confirm_message="Delete {resolved[collection_ids]}?",
                execute=execute_delete,
                serialize_success=lambda resolved: {
                    "collection_ids": resolved["collection_ids"],
                    "deleted": True,
                },
                serialize_cancel=lambda resolved: {
                    "collection_ids": resolved["collection_ids"],
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
                f"[green]Deleted {len(result.resolved['collection_ids'])} collection(s)[/green]",
                ctx=ctx,
            )

    return _run()
