"""Artifact management CLI commands.

Commands:
    list        List all artifacts
    get         Get artifact details
    get-prompt  Show the generation prompt behind an artifact
    rename      Rename an artifact
    delete      Delete an artifact
    export      Export to Google Docs/Sheets
    poll        Poll generation status (single check)
    wait        Wait for generation to complete (blocking)
    retry       Retry a failed artifact in place
    suggestions Get AI-suggested report topics
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import click
from rich.table import Table

from .._app.artifacts import (
    delete_artifact,
    export_artifact,
    get_artifact,
    get_artifact_prompt,
    poll_artifact,
    rename_artifact,
    retry_artifact,
    wait_for_artifact,
)
from ..exceptions import ArtifactNotFoundError
from .auth_runtime import resolve_client_factory, with_client
from .context import get_current_notebook
from .error_handler import _output_error, exit_with_code
from .options import (
    _complete_artifacts,
    json_option,
    list_options,
    notebook_option,
    wait_polling_options,
)
from .polling_ui import status_with_elapsed
from .rendering import (
    cli_name_to_artifact_type,
    cli_print,
    console,
    get_artifact_type_display,
    json_output_response,
    render_list,
)
from .resolve import (
    require_notebook,
    resolve_artifact_id,
    resolve_notebook_id,
)
from .services.confirming_mutation import MutationPlan, run_confirmed_mutation
from .services.listing import ListSpec, prepare_list

if TYPE_CHECKING:
    from ..client import NotebookLMClient


@click.group()
def artifact():
    """Artifact management commands.

    \b
    Commands:
      list         List all artifacts (or by type)
      get          Get artifact details
      get-prompt   Show the generation prompt behind an artifact
      rename       Rename an artifact
      delete       Delete an artifact
      export       Export to Google Docs/Sheets
      poll         Poll generation status (single check)
      wait         Wait for generation to complete (blocking)
      retry        Retry a failed artifact in place
      suggestions  Get AI-suggested report topics
      copy         Copy artifacts into another notebook
      choices      Show the Studio "Customize" option tables

    \b
    Partial ID Support:
      ARTIFACT_ID arguments support partial matching. Instead of typing the full
      UUID, you can use a prefix (e.g., 'abc' matches 'abc123def456...').
    """
    pass


@artifact.command("list")
@notebook_option
@click.option(
    "--type",
    "artifact_type",
    type=click.Choice(
        [
            "all",
            "audio",
            "video",
            "slide-deck",
            "quiz",
            "flashcard",
            "infographic",
            "data-table",
            "mind-map",
            "report",
            "fantasy-map",
            "file",
        ]
    ),
    default="all",
    help="Filter by type",
)
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
@list_options
@with_client
def artifact_list(ctx, notebook_id, artifact_type, json_output, limit, no_truncate, client_auth):
    """List artifacts in a notebook.

    \b
    Pagination & display:
      --limit N         Show at most N artifacts (default: unlimited).
      --no-truncate     Do not truncate the Title column in the table view.
    """
    nb_id = require_notebook(notebook_id)
    type_filter = cli_name_to_artifact_type(artifact_type)

    async def _run():
        async with resolve_client_factory(ctx)(client_auth) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)

            async def envelope_extras(
                client: NotebookLMClient, notebook_id: str
            ) -> dict[str, str | None]:
                nb = await client.notebooks.get(notebook_id)
                return {"notebook_id": notebook_id, "notebook_title": nb.title if nb else None}

            spec = ListSpec(
                title="Artifacts in {notebook_id}",
                items_key="artifacts",
                # artifacts.list() already includes mind maps from notes system
                fetch=lambda client, notebook_id: client.artifacts.list(
                    notebook_id,
                    artifact_type=type_filter,
                ),
                serialize=lambda art: {
                    "id": art.id,
                    "title": art.title,
                    "type": get_artifact_type_display(art).split(" ", 1)[-1],
                    "type_id": art.kind.value,
                    "status": art.status_str,
                    "status_id": art.status,
                    "created_at": art.created_at.isoformat() if art.created_at else None,
                },
                columns=["ID", "Title", "Type", "Created", "Status"],
                row=lambda art: [
                    art.id,
                    art.title,
                    get_artifact_type_display(art),
                    art.created_at.strftime("%Y-%m-%d %H:%M") if art.created_at else "-",
                    art.status_str,
                ],
                envelope_extras=envelope_extras,
                empty_message=f"[yellow]No {artifact_type} artifacts found[/yellow]",
            )
            render_list(
                await prepare_list(
                    spec,
                    client,
                    notebook_id=nb_id_resolved,
                    limit=limit,
                    json_output=json_output,
                    no_truncate=no_truncate,
                )
            )

    return _run()


@artifact.command("get")
@click.argument("artifact_id")
@notebook_option
@json_option
@with_client
def artifact_get(ctx, artifact_id, notebook_id, json_output, client_auth):
    """Get artifact details.

    ARTIFACT_ID can be a full UUID or a partial prefix (e.g., 'abc' matches 'abc123...').
    """
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with resolve_client_factory(ctx)(client_auth) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)
            resolved_id = await resolve_artifact_id(
                client, nb_id_resolved, artifact_id, json_output=json_output
            )

            # The neutral ``get_artifact`` raises ``ArtifactNotFoundError`` when
            # the backend reports the artifact gone (deleted between the
            # partial-id resolve and the get, or a canonical UUID pointing at a
            # since-deleted artifact). Render the historical ``NOT_FOUND``
            # envelope here so the CLI ``--json`` body + exit-1 contract stay
            # byte-stable (BREAKING vs the old exit-0 ``found: false``; see the
            # matching ``cli/source_cmd.py::source_get`` change and the BREAKING
            # entry in ``CHANGELOG.md``).
            try:
                art = await get_artifact(client, nb_id_resolved, resolved_id)
            except ArtifactNotFoundError:
                _output_error(
                    "Artifact not found",
                    code="NOT_FOUND",
                    json_output=json_output,
                    exit_code=1,
                    extra={"id": resolved_id, "notebook_id": nb_id_resolved},
                )
                raise AssertionError("unreachable") from None  # pragma: no cover

            if json_output:
                data = {
                    "notebook_id": nb_id_resolved,
                    "id": art.id,
                    "title": art.title,
                    "type": get_artifact_type_display(art).split(" ", 1)[-1],
                    "type_id": art.kind.value,
                    "status": art.status_str,
                    "status_id": art.status,
                    "created_at": art.created_at.isoformat() if art.created_at else None,
                    "found": True,
                }
                json_output_response(data)
                return

            console.print(f"[bold cyan]Artifact:[/bold cyan] {art.id}")
            console.print(f"[bold]Title:[/bold] {art.title}")
            console.print(f"[bold]Type:[/bold] {get_artifact_type_display(art)}")
            console.print(f"[bold]Status:[/bold] {art.status_str}")
            if art.created_at:
                console.print(f"[bold]Created:[/bold] {art.created_at.strftime('%Y-%m-%d %H:%M')}")

    return _run()


@artifact.command("get-prompt")
@click.argument("artifact_id")
@notebook_option
@json_option
@with_client
def artifact_get_prompt(ctx, artifact_id, notebook_id, json_output, client_auth):
    """Show the generation prompt that produced an artifact.

    Prints the free-text prompt the artifact was generated from. ARTIFACT_ID
    can be a full UUID or a partial prefix (e.g., 'abc' matches 'abc123...').
    A missing artifact exits 1 with a typed NOT_FOUND error.
    """
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with resolve_client_factory(ctx)(client_auth) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)
            resolved_id = await resolve_artifact_id(
                client, nb_id_resolved, artifact_id, json_output=json_output
            )

            # The neutral ``get_artifact_prompt`` raises ``ArtifactNotFoundError``
            # for an id absent from the studio listing; render the same
            # ``NOT_FOUND`` envelope + exit 1 contract as ``artifact get``.
            try:
                prompt = await get_artifact_prompt(client, nb_id_resolved, resolved_id)
            except ArtifactNotFoundError:
                _output_error(
                    "Artifact not found",
                    code="NOT_FOUND",
                    json_output=json_output,
                    exit_code=1,
                    extra={"id": resolved_id, "notebook_id": nb_id_resolved},
                )
                raise AssertionError("unreachable") from None  # pragma: no cover

            if json_output:
                json_output_response(
                    {"notebook_id": nb_id_resolved, "id": resolved_id, "prompt": prompt}
                )
                return

            if prompt is None:
                console.print("[yellow]This artifact has no stored prompt.[/yellow]")
                return
            # Print the prompt verbatim with no Rich markup interpretation so a
            # prompt containing literal brackets is shown exactly as authored.
            console.print(prompt, markup=False)

    return _run()


@artifact.command("rename")
@click.argument("artifact_id")
@click.argument("new_title")
@notebook_option
@json_option
@with_client
def artifact_rename(ctx, artifact_id, new_title, notebook_id, json_output, client_auth):
    """Rename an artifact.

    ARTIFACT_ID can be a full UUID or a partial prefix (e.g., 'abc' matches 'abc123...').
    """
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with resolve_client_factory(ctx)(client_auth) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)
            resolved_id = await resolve_artifact_id(
                client, nb_id_resolved, artifact_id, json_output=json_output
            )

            # Kind-aware mind-map dispatch + the rename RPC live in the neutral
            # core. The rename API raises on a missing target; if no exception
            # was raised, the operation succeeded. We display the requested
            # new_title as confirmation.
            result = await rename_artifact(client, nb_id_resolved, resolved_id, new_title)
            if json_output:
                json_output_response(
                    {"id": result.artifact_id, "renamed": True, "new_title": result.new_title}
                )
            else:
                cli_print(f"[green]Renamed artifact:[/green] {result.artifact_id}", ctx=ctx)
                cli_print(f"[bold]New title:[/bold] {result.new_title}", ctx=ctx)

    return _run()


@artifact.command("delete")
@click.argument("artifact_id")
@notebook_option
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
@json_option
@with_client
def artifact_delete(ctx, artifact_id, notebook_id, yes, json_output, client_auth):
    """Delete an artifact.

    ARTIFACT_ID can be a full UUID or a partial prefix (e.g., 'abc' matches 'abc123...').
    """
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with resolve_client_factory(ctx)(client_auth) as client:

            async def resolve_delete(client):
                nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)
                resolved_id = await resolve_artifact_id(
                    client, nb_id_resolved, artifact_id, json_output=json_output
                )

                # In JSON mode, refuse to prompt: ``click.confirm`` writes to
                # stdout, which would corrupt the parseable JSON contract callers
                # rely on. Require --yes and emit a structured error otherwise.
                if json_output and not yes:
                    _output_error(
                        "Pass --yes to confirm deletion in --json mode",
                        code="VALIDATION_ERROR",
                        json_output=json_output,
                        exit_code=1,
                        extra={
                            "id": resolved_id,
                            "notebook_id": nb_id_resolved,
                            "deleted": False,
                        },
                    )
                    raise AssertionError("unreachable")  # pragma: no cover

                return {
                    "notebook_id": nb_id_resolved,
                    "artifact_id": resolved_id,
                    "kind": "artifact",
                }

            async def execute_delete(client, resolved):
                # Neutral core clears note-backed mind maps via notes.delete and
                # deletes regular artifacts via artifacts.delete; it reports back
                # which path ran so the serializer can flag the mind-map carve-out.
                is_mind_map = await delete_artifact(
                    client, resolved["notebook_id"], resolved["artifact_id"]
                )
                if is_mind_map:
                    resolved["kind"] = "mind_map"

            def serialize_success(resolved):
                if resolved["kind"] == "mind_map":
                    return {
                        "id": resolved["artifact_id"],
                        "deleted": True,
                        "kind": "mind_map",
                        "note": (
                            "Mind maps are cleared, not removed. "
                            "Google may garbage collect them later."
                        ),
                    }
                return {"id": resolved["artifact_id"], "deleted": True}

            plan = MutationPlan(
                entity_label="artifact",
                resolve=resolve_delete,
                confirm_message="Delete artifact {resolved[artifact_id]}?",
                execute=execute_delete,
                serialize_success=serialize_success,
                serialize_cancel=lambda resolved: {
                    "id": resolved["artifact_id"],
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
            if result.status == "cancelled":
                return

            if json_output:
                json_output_response(result.payload)
                return

            resolved_id = result.resolved["artifact_id"]
            if result.resolved["kind"] == "mind_map":
                cli_print(f"[yellow]Cleared mind map:[/yellow] {resolved_id}", ctx=ctx)
                cli_print(
                    "[dim]Note: Mind maps are cleared, not removed. Google may garbage collect them later.[/dim]",
                    ctx=ctx,
                )
            else:
                cli_print(f"[green]Deleted artifact:[/green] {resolved_id}", ctx=ctx)

    return _run()


@artifact.command("export")
@click.argument("artifact_id")
@notebook_option
@click.option("--title", required=True, help="Title for exported document")
@click.option("--type", "export_type", type=click.Choice(["docs", "sheets"]), default="docs")
@json_option
@with_client
def artifact_export(ctx, artifact_id, notebook_id, title, export_type, json_output, client_auth):
    """Export artifact to Google Docs/Sheets.

    ARTIFACT_ID can be a full UUID or a partial prefix (e.g., 'abc' matches 'abc123...').
    """
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with resolve_client_factory(ctx)(client_auth) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)
            resolved_id = await resolve_artifact_id(
                client, nb_id_resolved, artifact_id, json_output=json_output
            )
            result = await export_artifact(client, nb_id_resolved, resolved_id, title, export_type)

            if json_output:
                json_output_response(
                    {
                        "id": result.artifact_id,
                        "exported": result.exported,
                        "export_type": result.export_type,
                        "title": result.title,
                        "result": result.result,
                    }
                )
                return

            if result.exported:
                console.print(f"[green]Exported to Google {export_type.title()}[/green]")
                console.print(result.result)
            else:
                console.print("[yellow]Export may have failed[/yellow]")

    return _run()


@artifact.command("poll")
@click.argument("task_id")
@notebook_option
@json_option
@with_client
def artifact_poll(ctx, task_id, notebook_id, json_output, client_auth):
    """Single non-blocking generation status check.

    \b
    TASK_ID is the identifier returned by `notebooklm generate <type>` (it
    appears in the `task_id` field of the JSON payload, or after `Started:`
    in the human-readable output). Pass it through unchanged — `poll` does
    NOT prefix-match against `artifact list`, so a freshly-issued task_id
    works even before the artifact appears in the list.

    \b
    Note: this is the same identifier `wait` accepts. The API uses one ID
    that serves as both the generation task_id (during creation) and the
    artifact_id (once listed); the difference is operational, not semantic:
      - `poll`: one-shot check, accepts the raw task_id from `generate`.
      - `wait`: blocks until terminal, prefix-matches against `artifact list`.

    \b
    Examples:
      # Right after `generate audio` returns task_id "abc123def...":
      notebooklm artifact poll abc123def
      # JSON output for scripting:
      notebooklm artifact poll abc123def --json
    """
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with resolve_client_factory(ctx)(client_auth) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)
            status = await poll_artifact(client, nb_id_resolved, task_id)

            if json_output:
                # Mirror the GenerationStatus dataclass fields so automation can
                # introspect status / url / error without parsing prose.
                json_output_response(
                    {
                        "task_id": status.task_id,
                        "status": status.status,
                        "url": status.url,
                        "error": status.error,
                        "error_code": status.error_code,
                        "metadata": status.metadata,
                    }
                )
                return

            console.print("[bold cyan]Task Status:[/bold cyan]")
            console.print(status)

    return _run()


@artifact.command("wait")
@click.argument("artifact_id")
@notebook_option
@wait_polling_options(default_timeout=300, default_interval=2)
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
@with_client
def artifact_wait(ctx, artifact_id, notebook_id, timeout, interval, json_output, client_auth):
    """Block until artifact generation finishes (or times out).

    \b
    ARTIFACT_ID is an identifier from `notebooklm artifact list` — it can be
    a full UUID or a unique prefix (e.g., `abc` matches `abc123def...`).
    Wait blocks until status is `completed`, `failed`, or `--timeout`
    elapses; useful for scripts and LLM agents that need a synchronous gate.

    \b
    Note: this is the same identifier `poll` accepts. The API uses one ID
    that serves as both the generation task_id (during creation) and the
    artifact_id (once listed); the difference is operational, not semantic:
      - `poll`: one-shot check, accepts the raw task_id from `generate`.
      - `wait`: blocks until terminal, prefix-matches against `artifact list`.

    \b
    Examples:
      # After `artifact list` shows id "abc123def...":
      notebooklm artifact wait abc123 -n nb_456
      # Long-running generation with longer ceiling, JSON for scripting:
      notebooklm artifact wait abc123 --timeout 600 --json
    """
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with resolve_client_factory(ctx)(client_auth) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)
            resolved_id = await resolve_artifact_id(
                client, nb_id_resolved, artifact_id, json_output=json_output
            )

            try:
                # Wrap the blocking poll in a transient spinner so interactive
                # users see progress feedback during the wait.
                # The status line includes the artifact ID and a live
                # elapsed-seconds counter. No-op under --json so stdout stays
                # pure JSON.
                #
                # ``resume_hint`` plumbs the canonical M2 cancellation message
                # (``Cancelled. Resume with: notebooklm artifact poll <id>``)
                # so Ctrl-C during the wait surfaces a resume command instead
                # of a Python KeyboardInterrupt traceback. Same hint shape as
                # ``generate <kind> --wait`` because both polling loops resume
                # via ``artifact poll``.
                async with status_with_elapsed(
                    f"Waiting for artifact {resolved_id} to complete...",
                    json_output=json_output,
                    resume_hint=f"notebooklm artifact poll {resolved_id}",
                ):
                    status = await wait_for_artifact(
                        client,
                        nb_id_resolved,
                        resolved_id,
                        initial_interval=float(interval),
                        timeout=float(timeout),
                    )

                if json_output:
                    data = {
                        "artifact_id": resolved_id,
                        "status": status.status,
                        "url": status.url,
                        "error": status.error,
                    }
                    json_output_response(data)
                    # Any non-completed status is an error for automation;
                    # intentionally stricter than the non-JSON path (which
                    # exits 0 for unknown/pending statuses). Without this,
                    # automation sees a JSON payload with an "error" message
                    # but the command still exits 0.
                    if not status.is_complete:
                        exit_with_code(1)
                else:
                    if status.is_complete:
                        console.print(f"[green]✓ Artifact completed:[/green] {resolved_id}")
                        if status.url:
                            console.print(f"[dim]URL:[/dim] {status.url}")
                    elif status.error:
                        console.print(f"[red]✗ Generation failed:[/red] {status.error}")
                        exit_with_code(1)
                    else:
                        console.print(f"[yellow]Status:[/yellow] {status.status}")

            except TimeoutError:
                if json_output:
                    json_output_response(
                        {
                            "artifact_id": resolved_id,
                            "status": "timeout",
                            "error": f"Timed out after {timeout} seconds",
                        }
                    )
                else:
                    console.print(f"[red]✗ Timeout after {timeout}s[/red]")
                exit_with_code(1)

    return _run()


@artifact.command("retry")
@click.argument("artifact_id")
@notebook_option
@click.option("--wait", is_flag=True, help="Block until the retried generation finishes")
@wait_polling_options(default_timeout=300, default_interval=2)
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
@with_client
def artifact_retry(
    ctx, artifact_id, notebook_id, wait, timeout, interval, json_output, client_auth
):
    """Retry a failed Studio artifact in place (the UI "Retry" action).

    \b
    Re-runs generation for an already-failed artifact WITHOUT deleting it. The
    same ARTIFACT_ID is preserved, so `poll`/`wait` keep working against it.
    ARTIFACT_ID can be a full UUID or a unique prefix (e.g. `abc` matches
    `abc123def...`); unlike `poll`, it is resolved against `artifact list`, so
    the artifact must already appear there (a failed artifact always does).

    \b
    A synchronous refusal (rate limit / quota / not-retryable) exits non-zero
    with a typed error rather than reporting a started task. Pass `--wait` to
    block until the retried generation reaches a terminal state.

    \b
    Examples:
      # Kick off an in-place retry and return immediately:
      notebooklm artifact retry abc123 -n nb_456
      # Retry and block until it completes (or fails again):
      notebooklm artifact retry abc123 -n nb_456 --wait
    """
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with resolve_client_factory(ctx)(client_auth) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)
            resolved_id = await resolve_artifact_id(
                client, nb_id_resolved, artifact_id, json_output=json_output
            )

            status = await retry_artifact(client, nb_id_resolved, resolved_id)

            if not wait:
                if json_output:
                    json_output_response(
                        {
                            "task_id": status.task_id,
                            "status": status.status,
                            "url": status.url,
                            "error": status.error,
                            "error_code": status.error_code,
                        }
                    )
                else:
                    console.print(f"[green]Retry started:[/green] {status.task_id}")
                    console.print(f"[bold]Status:[/bold] {status.status}")
                return

            try:
                # Same transient-spinner UX as ``artifact wait``: the resume
                # hint points back at ``artifact poll`` so Ctrl-C surfaces a
                # resume command instead of a traceback.
                async with status_with_elapsed(
                    f"Waiting for retried artifact {status.task_id} to complete...",
                    json_output=json_output,
                    resume_hint=f"notebooklm artifact poll {status.task_id}",
                ):
                    final = await wait_for_artifact(
                        client,
                        nb_id_resolved,
                        status.task_id,
                        initial_interval=float(interval),
                        timeout=float(timeout),
                    )

                if json_output:
                    # Once we are blocking on completion the id is an
                    # ``artifact_id``, so the ``--wait`` payload mirrors
                    # ``artifact wait``'s shape/keys exactly (the non-wait
                    # kickoff above stays ``task_id``, matching ``artifact
                    # poll`` / ``generate``).
                    json_output_response(
                        {
                            "artifact_id": final.task_id,
                            "status": final.status,
                            "url": final.url,
                            "error": final.error,
                        }
                    )
                    if not final.is_complete:
                        exit_with_code(1)
                else:
                    if final.is_complete:
                        console.print(f"[green]✓ Artifact completed:[/green] {final.task_id}")
                        if final.url:
                            console.print(f"[dim]URL:[/dim] {final.url}")
                    elif final.error:
                        console.print(f"[red]✗ Generation failed:[/red] {final.error}")
                        exit_with_code(1)
                    else:
                        # Any terminal non-completed status (e.g. ``failed``
                        # with no extractable error, or ``removed``) is a
                        # non-success for automation — exit non-zero so a
                        # provider-side retry failure is not reported as a
                        # successful command. Matches the JSON branch above and
                        # ADR-0019's "report failures as failures" posture.
                        console.print(f"[yellow]Status:[/yellow] {final.status}")
                        exit_with_code(1)

            except TimeoutError:
                if json_output:
                    # Matches ``artifact wait``'s timeout payload key.
                    json_output_response(
                        {
                            "artifact_id": status.task_id,
                            "status": "timeout",
                            "error": f"Timed out after {timeout} seconds",
                        }
                    )
                else:
                    console.print(f"[red]✗ Timeout after {timeout}s[/red]")
                exit_with_code(1)

    return _run()


@artifact.command("suggestions")
@notebook_option
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
@with_client
def artifact_suggestions(ctx, notebook_id, json_output, client_auth):
    """Get AI-suggested report topics based on notebook content."""
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with resolve_client_factory(ctx)(client_auth) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)
            suggestions = await client.artifacts.suggest_reports(nb_id_resolved)

            if json_output:
                data = [
                    {"title": s.title, "description": s.description, "prompt": s.prompt}
                    for s in suggestions
                ]
                json_output_response(data)
                return

            if not suggestions:
                console.print("[yellow]No suggestions available[/yellow]")
                return

            table = Table(title="Suggested Reports")
            table.add_column("#", style="dim")
            table.add_column("Title", style="green")
            table.add_column("Description")

            for i, suggestion in enumerate(suggestions, 1):
                table.add_row(str(i), suggestion.title, suggestion.description)

            console.print(table)
            console.print('\n[dim]Use the prompt with: notebooklm generate report "<prompt>"[/dim]')

    return _run()


# ---------------------------------------------------------------------------
# #2283: CopyArtifactsAsync / GetArtifactCustomizationChoices
# ---------------------------------------------------------------------------


@artifact.command("copy")
@click.argument("artifact_ids", nargs=-1, required=True, shell_complete=_complete_artifacts)
@click.option("--to", "target", required=True, help="Target notebook id (or unique prefix)")
@notebook_option
@json_option
@with_client
def artifact_copy(ctx, artifact_ids, target, notebook_id, json_output, client_auth):
    """Copy Studio artifacts into another notebook (``CopyArtifactsAsync``).

    Prints each original id alongside the new artifact created in the target
    notebook. ARTIFACT_IDS may be full ids or unique prefixes within the
    current notebook.

    \b
    Example:
      notebooklm artifact copy art1 art2 --to 1a2b3c4d
      notebooklm artifact copy art1 --to 1a2b3c4d --json
    """
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with resolve_client_factory(ctx)(client_auth) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)
            target_resolved = await resolve_notebook_id(client, target, json_output=json_output)
            resolved_ids = [
                await resolve_artifact_id(client, nb_id_resolved, aid, json_output=json_output)
                for aid in artifact_ids
            ]
            copied = await client.artifacts.copy(nb_id_resolved, resolved_ids, target_resolved)
            not_copied = [aid for aid in resolved_ids if aid not in {c.original_id for c in copied}]
            if json_output:
                json_output_response(
                    {
                        "notebook_id": nb_id_resolved,
                        "target_notebook_id": target_resolved,
                        "copied": [
                            {
                                "original_id": item.original_id,
                                "artifact": {
                                    "id": item.artifact.id,
                                    "title": item.artifact.title,
                                    "type": item.artifact.kind.value,
                                    "status": item.artifact.status_str,
                                },
                            }
                            for item in copied
                        ],
                        "not_copied": not_copied,
                        "count": len(copied),
                        "requested": len(resolved_ids),
                    }
                )
            else:
                cli_print(
                    f"[green]Copied {len(copied)} of {len(resolved_ids)} artifact(s) to[/green] "
                    f"{target_resolved}",
                    ctx=ctx,
                )
                for item in copied:
                    cli_print(
                        f"  {item.original_id} -> {item.artifact.id}  {item.artifact.title}",
                        ctx=ctx,
                    )
            if not_copied:
                # A partial copy is a partial failure: the JSON payload already
                # names the ids that stayed behind (``not_copied``); text mode says
                # so explicitly. Either way exit non-zero so scripts cannot mistake
                # a partial copy for success.
                if not json_output:
                    cli_print(
                        f"[yellow]Not copied ({len(not_copied)}):[/yellow] {', '.join(not_copied)}",
                        ctx=ctx,
                    )
                exit_with_code(1)

    return _run()


@artifact.command("choices")
@notebook_option
@json_option
@with_client
def artifact_choices(ctx, notebook_id, json_output, client_auth):
    """Show the Studio "Customize" option tables served to this account.

    Lists the audio / video / slide-deck formats (with the wire codes behind
    the ``generate --format`` names) and the report presets with their full
    generation directives (``GetArtifactCustomizationChoices``). The table is
    account-level, so ``-n`` is optional and only fills the request's project_id slot.

    \b
    Example:
      notebooklm artifact choices
      notebooklm artifact choices --json
    """
    nb_id = notebook_id or get_current_notebook()

    async def _run():
        async with resolve_client_factory(ctx)(client_auth) as client:
            nb_id_resolved = (
                await resolve_notebook_id(client, nb_id, json_output=json_output) if nb_id else None
            )
            choices = await client.artifacts.get_customization_choices(nb_id_resolved)
            if json_output:
                json_output_response(
                    {
                        "audio": [
                            {"code": c.code, "title": c.title, "description": c.description}
                            for c in choices.audio
                        ],
                        "video": [
                            {"code": c.code, "title": c.title, "description": c.description}
                            for c in choices.video
                        ],
                        "slide_deck": [
                            {"code": c.code, "title": c.title, "description": c.description}
                            for c in choices.slide_deck
                        ],
                        "reports": [
                            {
                                "report_type": r.report_type,
                                "description": r.description,
                                "directive": r.directive,
                            }
                            for r in choices.reports
                        ],
                    }
                )
                return
            for label, rows in (
                ("Audio formats", choices.audio),
                ("Video formats", choices.video),
                ("Slide-deck formats", choices.slide_deck),
            ):
                table = Table(title=label)
                table.add_column("Code", style="dim")
                table.add_column("Title", style="green")
                table.add_column("Description")
                for row in rows:
                    table.add_row(str(row.code), row.title, row.description)
                console.print(table)
            table = Table(title="Report presets")
            table.add_column("Preset", style="green")
            table.add_column("Description")
            for preset in choices.reports:
                table.add_row(preset.report_type, preset.description)
            console.print(table)
            console.print(
                "\n[dim]Use --json to see each report preset's full generation directive.[/dim]"
            )

    return _run()
