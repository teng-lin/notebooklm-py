"""Source management CLI commands.

Commands:
    list             List sources in a notebook
    add              Add a source (url, text, file, youtube)
    add-drive        Add a Google Drive document
    add-research     Search web/drive and add sources from results
    get              Get source details
    fulltext         Get full indexed text content of a source
    guide            Get AI-generated source summary and keywords
    stale            Check if a URL/Drive source needs refresh
    wait             Wait for a source to finish processing
    clean            Remove duplicate, error, and access-blocked sources
    delete           Delete a source
    delete-by-title  Delete a source by exact title
    rename           Rename a source
    refresh          Refresh a URL/Drive source
"""

import asyncio
import contextlib
import os
import re
from pathlib import Path
from typing import Any, Literal

import click
from rich.table import Table

from ..client import NotebookLMClient
from ..types import Source, source_status_to_str
from .auth_runtime import with_client
from .error_handler import _output_error
from .input import read_stdin_text, resolve_prompt
from .options import (
    json_option,
    list_options,
    notebook_option,
    prompt_file_option,
    wait_polling_options,
)
from .rendering import (
    cli_print,
    cli_status,
    console,
    display_report,
    display_research_sources,
    emit_status,
    get_source_type_display,
    json_output_response,
)
from .research_import import import_research_sources
from .resolve import require_notebook, resolve_notebook_id, resolve_source_id, validate_id
from .runtime import is_quiet
from .services import source_add as source_add_service
from .services import source_clean as source_clean_service
from .services.polling import status_with_elapsed


def _looks_like_path(content: str) -> bool:
    """Compatibility wrapper for tests patching source-add path detection."""
    return source_add_service.looks_like_path(content)


def _validate_upload_path(content: str, follow_symlinks: bool) -> Path:
    """Compatibility wrapper for tests patching source-add upload validation."""
    try:
        return source_add_service.validate_upload_path(content, follow_symlinks)
    except source_add_service.SourceAddValidationError as exc:
        raise click.ClickException(str(exc)) from exc


def _classify_junk_sources(sources: list[Source]) -> list[tuple[str, str, str, str]]:
    """Compatibility wrapper for tests patching source-clean classification."""
    return source_clean_service.classify_junk_sources(sources)


def _print_clean_candidates(candidates: list[tuple[str, str, str, str]]) -> None:
    """Print a Rich table summarizing sources that will (or would) be deleted."""
    table = Table(title=f"{len(candidates)} source(s) flagged for cleanup")
    table.add_column("ID", style="dim", overflow="fold")
    table.add_column("Title", overflow="fold")
    table.add_column("Status")
    table.add_column("Reason")
    for sid, title, status, reason in candidates:
        display_title = title if title else "[dim](no title)[/dim]"
        table.add_row(sid[:8], display_title, status, reason)
    console.print(table)


def _require_yes_in_json(*, action: str, extra: dict[str, Any] | None = None) -> None:
    """Emit a structured ``CONFIRM_REQUIRED`` error and exit non-zero.

    Centralises the JSON-mode confirmation gate used by destructive commands
    (``source delete``, ``source delete-by-title``, ``source clean``). Calling
    this helper always raises ``SystemExit(1)`` via :func:`_output_error` — it
    never returns normally.

    Args:
        action: Short verb identifying the command (``"delete"``,
            ``"delete-by-title"``, ``"clean"``) so callers can match the
            envelope to the originating command.
        extra: Additional command-specific fields (e.g. ``source_id``,
            ``notebook_id``, ``candidates``) merged into the JSON envelope so
            automation has full context about what was refused.
    """
    payload: dict[str, Any] = {"action": action}
    if extra:
        payload.update(extra)
    _output_error(
        "Pass --yes to confirm destructive operation in --json mode",
        code="CONFIRM_REQUIRED",
        json_output=True,
        exit_code=1,
        extra=payload,
    )
    raise AssertionError("unreachable")  # pragma: no cover


@click.group()
def source():
    """Source management commands.

    \b
    Commands:
      list             List sources in a notebook
      add              Add a source (url, text, file, youtube)
      add-drive        Add a Google Drive document
      add-research     Search web/drive and add sources from results
      get              Get source details
      fulltext         Get full indexed text content
      guide            Get AI-generated source summary and keywords
      stale            Check if source needs refresh
      wait             Wait for a source to finish processing
      clean            Remove duplicate, error, and access-blocked sources
      delete           Delete a source
      delete-by-title  Delete a source by exact title
      rename           Rename a source
      refresh          Refresh a URL/Drive source

    \b
    Partial ID Support:
      SOURCE_ID arguments support partial matching. Instead of typing the full
      UUID, you can use a prefix (e.g., 'abc' matches 'abc123def456...').
    """
    pass


def _build_id_ambiguity_error(source_id: str, matches) -> click.ClickException:
    """Build a consistent ambiguity error for source ID prefix matches."""
    lines = [f"Ambiguous ID '{source_id}' matches {len(matches)} sources:"]
    for item in matches[:5]:
        title = item.title or "(untitled)"
        lines.append(f"  {item.id[:12]}... {title}")
    if len(matches) > 5:
        lines.append(f"  ... and {len(matches) - 5} more")
    lines.append("Specify more characters to narrow down.")
    return click.ClickException("\n".join(lines))


def _looks_like_full_source_id(source_id: str) -> bool:
    """Return True for UUID-shaped source IDs that can skip list-based resolution."""
    return bool(
        re.fullmatch(
            r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
            source_id,
        )
    )


async def _resolve_source_for_delete(
    client, notebook_id: str, source_id: str, *, json_output: bool = False
) -> str:
    """Resolve a source ID for delete, returning the full source ID string.

    Canonical UUIDs take a fast path and skip the live source list lookup.
    Partial IDs are resolved against the live list.

    When ``json_output`` is True, the "Matched..." diagnostic for a successful
    partial match is routed to stderr so stdout stays parseable JSON.
    """
    source_id = validate_id(source_id, "source")
    if _looks_like_full_source_id(source_id):
        return source_id

    sources = await client.sources.list(notebook_id)
    matches = [item for item in sources if item.id.lower().startswith(source_id.lower())]

    if len(matches) == 1:
        if matches[0].id != source_id:
            title = matches[0].title or "(untitled)"
            emit_status(
                f"[dim]Matched: {matches[0].id[:12]}... ({title})[/dim]",
                json_output=json_output,
            )
        return matches[0].id

    if len(matches) > 1:
        raise _build_id_ambiguity_error(source_id, matches)

    title_matches = [item for item in sources if item.title == source_id]
    if title_matches:
        lines = [
            f"'{source_id}' matches {len(title_matches)} source title(s), not source IDs.",
            f"Use 'notebooklm source delete-by-title \"{source_id}\"' or delete by ID:",
        ]
        for item in title_matches[:5]:
            lines.append(f"  {item.id[:12]}... {item.title}")
        if len(title_matches) > 5:
            lines.append(f"  ... and {len(title_matches) - 5} more")
        raise click.ClickException("\n".join(lines))

    raise click.ClickException(
        f"No source found starting with '{source_id}'. "
        "Run 'notebooklm source list' to see available sources."
    )


async def _resolve_source_by_exact_title(client, notebook_id: str, title: str):
    """Resolve a source by exact title for the explicit delete-by-title flow."""
    title = validate_id(title, "source title")
    sources = await client.sources.list(notebook_id)
    matches = [item for item in sources if item.title == title]

    if len(matches) == 1:
        return matches[0]

    if len(matches) > 1:
        lines = [f"Title '{title}' matches {len(matches)} sources. Delete by ID instead:"]
        for item in matches[:5]:
            lines.append(f"  {item.id[:12]}... {item.title}")
        if len(matches) > 5:
            lines.append(f"  ... and {len(matches) - 5} more")
        raise click.ClickException("\n".join(lines))

    raise click.ClickException(
        f"No source found with title '{title}'. "
        "Run 'notebooklm source list' to see available sources."
    )


@source.command("list")
@notebook_option
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
@list_options
@with_client
def source_list(ctx, notebook_id, json_output, limit, no_truncate, client_auth):
    """List all sources in a notebook.

    \b
    Pagination & display:
      --limit N         Show at most N sources (default: unlimited).
      --no-truncate     Do not truncate the Title column in the table view.
    """
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)
            sources = await client.sources.list(nb_id_resolved)
            # Client-side offset slicing.
            if limit is not None and limit >= 0:
                sources = sources[:limit]
            nb = None
            if json_output:
                nb = await client.notebooks.get(nb_id_resolved)

            if json_output:
                data = {
                    "notebook_id": nb_id_resolved,
                    "notebook_title": nb.title if nb else None,
                    "sources": [
                        {
                            "index": i,
                            "id": src.id,
                            "title": src.title,
                            "type": str(src.kind),
                            "url": src.url,
                            "status": source_status_to_str(src.status),
                            "status_id": src.status,
                            "created_at": src.created_at.isoformat() if src.created_at else None,
                        }
                        for i, src in enumerate(sources, 1)
                    ],
                    "count": len(sources),
                }
                json_output_response(data)
                return

            table = Table(title=f"Sources in {nb_id_resolved}")
            table.add_column("ID", style="cyan")
            title_overflow: Literal["fold", "ellipsis"] = "fold" if no_truncate else "ellipsis"
            table.add_column("Title", style="green", overflow=title_overflow)
            table.add_column("Type")
            table.add_column("Created", style="dim")
            table.add_column("Status", style="yellow")

            for src in sources:
                type_display = get_source_type_display(src.kind)
                created = src.created_at.strftime("%Y-%m-%d %H:%M") if src.created_at else "-"
                status = source_status_to_str(src.status)
                table.add_row(src.id, src.title or "-", type_display, created, status)

            console.print(table)

    return _run()


@source.command("add")
@click.argument("content")
@notebook_option
@click.option(
    "--type",
    "source_type",
    type=click.Choice(["url", "text", "file", "youtube"]),
    default=None,
    help="Source type (auto-detected if not specified)",
)
@click.option("--title", help="Custom title for text and uploaded-file sources")
# DEPRECATION-REMOVAL: v0.6.0 — ``--mime-type`` on the file-source path is a
# no-op (the upload pipeline ignores it; the server derives the MIME type from
# the filename extension). A deprecation note is echoed to stderr when the flag
# is used with a file source. The separate Drive-source ``--mime-type`` on the
# ``add-drive`` command remains live and IS NOT affected by this deprecation.
@click.option(
    "--mime-type",
    help=(
        "[Deprecated] MIME type for file sources — unused; the server "
        "derives MIME from the filename extension. Drive sources retain "
        "this option (see ``source add-drive``)."
    ),
)
@click.option(
    "--timeout",
    default=None,
    type=float,
    help=(
        "HTTP request timeout in seconds (default: 30, from the library). "
        "Increase when adding slow URLs or large files that exceed the default."
    ),
)
@click.option(
    "--follow-symlinks",
    is_flag=True,
    default=False,
    help=(
        "Follow symbolic links when uploading a file. By default, symlinks "
        "are rejected so a workspace symlink cannot silently exfiltrate the "
        "file it points at (e.g. ~/Downloads/foo.pdf -> /etc/passwd)."
    ),
)
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
@with_client
def source_add(
    ctx,
    content,
    notebook_id,
    source_type,
    title,
    mime_type,
    timeout,
    follow_symlinks,
    json_output,
    client_auth,
):
    """Add a source to a notebook.

    \b
    Source type is auto-detected:
      - URLs (http/https) -> url or youtube
      - Existing files (.pdf, .md, .txt, etc.) -> file (uploaded)
      - Other content -> text (inline)
      - Use --type to override

    \b
    Examples:
      notebooklm source add https://example.com             # URL
      notebooklm source add ./doc.pdf                       # Existing file uploaded
      notebooklm source add https://youtube.com/...         # YouTube video
      notebooklm source add "My notes here"                 # Inline text
      notebooklm source add "My notes" --title "Research"   # Text with custom title

    \b
    Note: a path-shaped argument (contains '/' or ends in a known document
    extension) that does not exist on disk is still ingested as inline text,
    but a stderr warning is emitted so a typo (e.g. ``./missin.md``) cannot
    silently masquerade as a successful upload. Pass ``--type text`` to suppress
    the warning when the input is genuinely text content that happens to look
    path-shaped.
    """
    # Unix ``-`` convention: ``source add -`` reads inline text
    # from stdin and forces the text-source path. Intercepted here BEFORE
    # the URL / file / path-shaped auto-detection branches so a single dash
    # never falls into the path-shaped warning ("'-' looks like a path...")
    # and so an explicit ``--type file -`` does not try to open a file
    # literally named ``-``. We always route through the text branch — URL
    # / file / YouTube would be nonsensical for piped text and the
    # ``--type`` override is silently coerced for the same reason.
    if content == "-":
        content = read_stdin_text(source_label="source content")
        source_type = "text"

    nb_id = require_notebook(notebook_id)
    plan = source_add_service.build_source_add_plan(
        content=content,
        source_type=source_type,
        title=title,
        mime_type=mime_type,
        follow_symlinks=follow_symlinks,
        suppress_file_mime_deprecation=os.environ.get("NOTEBOOKLM_QUIET_DEPRECATIONS") == "1",
        validate_path=_validate_upload_path,
        looks_path_shaped=_looks_like_path,
    )

    for warning in plan.warnings:
        click.echo(warning, err=True)

    client_kwargs: dict = {}
    if timeout is not None:
        client_kwargs["timeout"] = timeout

    async def _run():
        async with NotebookLMClient(client_auth, **client_kwargs) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)
            # P1.T2 bug 5: ``rich.console.Console.status`` is a SYNCHRONOUS
            # context manager. The old shape ``with console.status(...): return
            # _run()`` exited the spinner as soon as ``_run()`` returned the
            # coroutine — BEFORE ``with_client`` awaited it — so the spinner
            # was effectively invisible during the actual upload. Moving the
            # ``with`` block inside the awaited coroutine makes the spinner
            # span the real I/O. JSON mode still suppresses the spinner so
            # stdout stays pure JSON.
            spinner = (
                contextlib.nullcontext()
                if json_output
                else console.status(f"Adding {plan.detected_type} source...")
            )
            with spinner:
                src = await source_add_service.add_source(
                    client.sources,
                    notebook_id=nb_id_resolved,
                    plan=plan,
                )

            if json_output:
                data = {
                    "source": {
                        "id": src.id,
                        "title": src.title,
                        "type": str(src.kind),
                        "url": src.url,
                    }
                }
                json_output_response(data)
                return

            cli_print(f"[green]Added source:[/green] {src.id}", ctx=ctx)

    return _run()


@source.command("get")
@click.argument("source_id")
@notebook_option
@json_option
@with_client
def source_get(ctx, source_id, notebook_id, json_output, client_auth):
    """Get source details.

    SOURCE_ID can be a full UUID or a partial prefix (e.g., 'abc' matches 'abc123...').
    """
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)
            # Resolve partial ID to full ID
            resolved_id = await resolve_source_id(
                client, nb_id_resolved, source_id, json_output=json_output
            )
            src = await client.sources.get(nb_id_resolved, resolved_id)

            # BREAKING: not-found exits 1 with a typed error instead of
            # the previous exit-0 ``found: false`` placeholder. The
            # ``_output_error`` helper writes the message to stderr (text mode)
            # or emits ``{error, code, message, source_id}`` to stdout (json
            # mode) and raises ``SystemExit(1)``. See ``docs/cli-exit-codes.md``
            # and the BREAKING entry in ``CHANGELOG.md`` (Unreleased → Changed).
            #
            # The trailing ``raise AssertionError`` is unreachable at runtime
            # (``_output_error`` always raises) — it exists solely to narrow
            # ``src`` from ``Source | None`` to ``Source`` for mypy without
            # forcing a ``NoReturn`` annotation onto
            # ``error_handler._output_error`` (which would change the shared
            # error helper's typing contract).
            if src is None:
                _output_error(
                    "Source not found",
                    code="NOT_FOUND",
                    json_output=json_output,
                    exit_code=1,
                    extra={"source_id": resolved_id, "notebook_id": nb_id_resolved},
                )
                raise AssertionError("unreachable")  # pragma: no cover

            if json_output:
                data = {
                    "source": {
                        "id": src.id,
                        "title": src.title,
                        "type": str(src.kind),
                        "url": src.url,
                        "status": source_status_to_str(src.status),
                        "status_id": src.status,
                        "created_at": (src.created_at.isoformat() if src.created_at else None),
                    },
                    "found": True,
                }
                json_output_response(data)
                return

            console.print(f"[bold cyan]Source:[/bold cyan] {src.id}")
            console.print(f"[bold]Title:[/bold] {src.title}")
            console.print(f"[bold]Type:[/bold] {get_source_type_display(src.kind)}")
            if src.url:
                console.print(f"[bold]URL:[/bold] {src.url}")
            if src.created_at:
                console.print(f"[bold]Created:[/bold] {src.created_at.strftime('%Y-%m-%d %H:%M')}")

    return _run()


@source.command("delete")
@click.argument("source_id")
@notebook_option
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
@json_option
@with_client
def source_delete(ctx, source_id, notebook_id, yes, json_output, client_auth):
    """Delete a source.

    SOURCE_ID can be a full UUID or a partial prefix (e.g., 'abc' matches 'abc123...').
    """
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)
            resolved_id = await _resolve_source_for_delete(
                client, nb_id_resolved, source_id, json_output=json_output
            )

            # P1.T2 bug 1: In --json mode, never prompt — automation cannot
            # answer an interactive confirmation and a hanging prompt is
            # indistinguishable from a stuck command. Require --yes and emit a
            # structured JSON error otherwise.
            if json_output and not yes:
                _require_yes_in_json(
                    action="delete",
                    extra={
                        "source_id": resolved_id,
                        "notebook_id": nb_id_resolved,
                    },
                )

            if not yes and not click.confirm(f"Delete source {resolved_id}?"):
                return

            success = await client.sources.delete(nb_id_resolved, resolved_id)

            if json_output:
                json_output_response(
                    {
                        "action": "delete",
                        "source_id": resolved_id,
                        "notebook_id": nb_id_resolved,
                        "success": bool(success),
                        "status": "deleted" if success else "unknown",
                    }
                )
                return

            if success:
                cli_print(f"[green]Deleted source:[/green] {resolved_id}", ctx=ctx)
            else:
                cli_print("[yellow]Delete may have failed[/yellow]", ctx=ctx)

    return _run()


@source.command("delete-by-title")
@click.argument("title")
@notebook_option
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
@json_option
@with_client
def source_delete_by_title(ctx, title, notebook_id, yes, json_output, client_auth):
    """Delete a source by exact title."""
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)
            source = await _resolve_source_by_exact_title(client, nb_id_resolved, title)

            # P1.T2 bug 2: same JSON-mode confirmation contract as
            # ``source delete``. Never prompt under --json; require --yes.
            if json_output and not yes:
                _require_yes_in_json(
                    action="delete-by-title",
                    extra={
                        "source_id": source.id,
                        "title": source.title,
                        "notebook_id": nb_id_resolved,
                    },
                )

            if not yes and not click.confirm(f"Delete source '{source.title}' ({source.id})?"):
                return

            success = await client.sources.delete(nb_id_resolved, source.id)

            if json_output:
                json_output_response(
                    {
                        "action": "delete-by-title",
                        "source_id": source.id,
                        "title": source.title,
                        "notebook_id": nb_id_resolved,
                        "success": bool(success),
                        "status": "deleted" if success else "unknown",
                    }
                )
                return

            if success:
                cli_print(f"[green]Deleted source:[/green] {source.id}", ctx=ctx)
            else:
                cli_print("[yellow]Delete may have failed[/yellow]", ctx=ctx)

    return _run()


@source.command("rename")
@click.argument("source_id")
@click.argument("new_title")
@notebook_option
@json_option
@with_client
def source_rename(ctx, source_id, new_title, notebook_id, json_output, client_auth):
    """Rename a source.

    SOURCE_ID can be a full UUID or a partial prefix (e.g., 'abc' matches 'abc123...').
    """
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)
            # Resolve partial ID to full ID
            resolved_id = await resolve_source_id(
                client, nb_id_resolved, source_id, json_output=json_output
            )
            src = await client.sources.rename(nb_id_resolved, resolved_id, new_title)

            if json_output:
                json_output_response(
                    {
                        "action": "rename",
                        "source_id": src.id,
                        "notebook_id": nb_id_resolved,
                        "title": src.title,
                        "status": "renamed",
                    }
                )
                return

            cli_print(f"[green]Renamed source:[/green] {src.id}", ctx=ctx)
            cli_print(f"[bold]New title:[/bold] {src.title}", ctx=ctx)

    return _run()


@source.command("refresh")
@click.argument("source_id")
@notebook_option
@json_option
@with_client
def source_refresh(ctx, source_id, notebook_id, json_output, client_auth):
    """Refresh a URL/Drive source.

    SOURCE_ID can be a full UUID or a partial prefix (e.g., 'abc' matches 'abc123...').
    """
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)
            # Resolve partial ID to full ID
            resolved_id = await resolve_source_id(
                client, nb_id_resolved, source_id, json_output=json_output
            )

            if json_output:
                src = await client.sources.refresh(nb_id_resolved, resolved_id)
            else:
                with cli_status("Refreshing source...", ctx=ctx):
                    src = await client.sources.refresh(nb_id_resolved, resolved_id)

            if json_output:
                # ``refresh`` may return a Source dataclass, ``True``, or
                # falsy/None. Surface the same three states in JSON so
                # automation can branch on ``status`` without scraping text.
                if src and src is not True:
                    data = {
                        "action": "refresh",
                        "source_id": src.id,
                        "notebook_id": nb_id_resolved,
                        "title": src.title,
                        "status": "refreshed",
                    }
                elif src is True:
                    data = {
                        "action": "refresh",
                        "source_id": resolved_id,
                        "notebook_id": nb_id_resolved,
                        "status": "refreshed",
                    }
                else:
                    data = {
                        "action": "refresh",
                        "source_id": resolved_id,
                        "notebook_id": nb_id_resolved,
                        "status": "no_result",
                    }
                json_output_response(data)
                return

            if src and src is not True:
                cli_print(f"[green]Source refreshed:[/green] {src.id}", ctx=ctx)
                cli_print(f"[bold]Title:[/bold] {src.title}", ctx=ctx)
            elif src is True:
                cli_print(f"[green]Source refreshed:[/green] {resolved_id}", ctx=ctx)
            else:
                cli_print("[yellow]Refresh returned no result[/yellow]", ctx=ctx)

    return _run()


@source.command("add-drive")
@click.argument("file_id")
@click.argument("title")
@notebook_option
@click.option(
    "--mime-type",
    type=click.Choice(["google-doc", "google-slides", "google-sheets", "pdf"]),
    default="google-doc",
    help="Document type (default: google-doc)",
)
@json_option
@with_client
def source_add_drive(ctx, file_id, title, notebook_id, mime_type, json_output, client_auth):
    """Add a Google Drive document as a source."""
    from ..types import DriveMimeType

    nb_id = require_notebook(notebook_id)
    mime_map = {
        "google-doc": DriveMimeType.GOOGLE_DOC.value,
        "google-slides": DriveMimeType.GOOGLE_SLIDES.value,
        "google-sheets": DriveMimeType.GOOGLE_SHEETS.value,
        "pdf": DriveMimeType.PDF.value,
    }
    mime = mime_map[mime_type]

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)

            if json_output:
                src = await client.sources.add_drive(nb_id_resolved, file_id, title, mime)
            else:
                with console.status("Adding Drive source..."):
                    src = await client.sources.add_drive(nb_id_resolved, file_id, title, mime)

            if json_output:
                json_output_response(
                    {
                        "action": "add-drive",
                        "source": {
                            "id": src.id,
                            "title": src.title,
                            "type": str(src.kind),
                            "url": src.url,
                            "drive_file_id": file_id,
                            "mime_type": mime_type,
                        },
                        "notebook_id": nb_id_resolved,
                    }
                )
                return

            cli_print(f"[green]Added Drive source:[/green] {src.id}", ctx=ctx)
            cli_print(f"[bold]Title:[/bold] {src.title}", ctx=ctx)

    return _run()


@source.command("add-research")
@click.argument("query", default="", required=False)
@prompt_file_option
@notebook_option
@click.option(
    "--from",
    "search_source",
    type=click.Choice(["web", "drive"]),
    default="web",
    help="Search source (default: web)",
)
@click.option(
    "--mode",
    type=click.Choice(["fast", "deep"]),
    default="fast",
    help="Search mode (default: fast)",
)
@click.option("--import-all", is_flag=True, help="Import all found sources")
@click.option("--cited-only", is_flag=True, help="With --import-all, import only cited sources")
@click.option(
    "--no-wait",
    is_flag=True,
    help="Start research and return immediately (use 'research status/wait' to monitor)",
)
@click.option(
    "--timeout",
    default=1800,
    type=int,
    help=(
        "Per-phase seconds budget for (a) the research-completion poll loop "
        "and (b) the --import-all retry loop (default: 1800). Each phase "
        "gets the full budget independently, so worst-case total wall time "
        "is up to 2× this value. Matches 'research wait --timeout' "
        "semantics. Bumping this is required for deep research that runs "
        "longer than the legacy 5-minute cap — otherwise the CLI gives up "
        "before IMPORT_RESEARCH fires and the NotebookLM web UI is left "
        "showing an 'Add sources?' modal."
    ),
)
@with_client
def source_add_research(
    ctx,
    query,
    prompt_file,
    notebook_id,
    search_source,
    mode,
    import_all,
    cited_only,
    no_wait,
    timeout,
    client_auth,
):
    """Search web or drive and add sources from results.

    \b
    Examples:
      notebooklm source add-research "machine learning"              # Search web
      notebooklm source add-research "project docs" --from drive     # Search Google Drive
      notebooklm source add-research "AI papers" --mode deep         # Deep search
      notebooklm source add-research "tutorials" --import-all        # Auto-import all results
      notebooklm source add-research "topic" --import-all --cited-only
      notebooklm source add-research "topic" --mode deep --no-wait   # Non-blocking deep search
      notebooklm source add-research --prompt-file query.txt --mode deep   # Read query from file
    """
    query = resolve_prompt(query, prompt_file, "query", required=True)
    if cited_only and not import_all:
        raise click.UsageError("--cited-only requires --import-all")
    # P1.T2 bug 7: --no-wait returns immediately without polling, so
    # --import-all would have no completed results to import. Silently
    # ignoring --import-all is the worst failure mode (user assumes import
    # happened); refuse the combination instead. Callers that want
    # deferred imports must explicitly run ``research wait --import-all``
    # after start (the message in --no-wait already hints at this).
    if no_wait and import_all:
        raise click.UsageError(
            "--import-all requires --wait (the default) or a separate "
            "'research wait --import-all' after --no-wait."
        )

    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id)
            console.print(f"[yellow]Starting {mode} research on {search_source}...[/yellow]")
            result = await client.research.start(nb_id_resolved, query, search_source, mode)
            if not result:
                console.print("[red]Research failed to start[/red]")
                raise SystemExit(1)

            task_id = result["task_id"]
            console.print(f"[dim]Task ID: {task_id}[/dim]")

            # Non-blocking mode: return immediately. Research will keep
            # running server-side; until something fires IMPORT_RESEARCH the
            # NotebookLM web UI will show an "Add sources?" modal (#315).
            if no_wait:
                console.print(
                    "[green]Research started.[/green] "
                    "Run 'notebooklm research wait --import-all' to commit "
                    "sources once it completes, otherwise the NotebookLM web "
                    "UI will keep an 'Add sources?' modal open."
                )
                return

            # Poll budget mirrors `research wait --timeout`: total seconds
            # divided by the 5 s interval. The legacy hardcoded 60-iteration
            # cap stranded deep research (#315) because the import branch
            # below is gated on `status == "completed"`.
            #
            # P1.T2 bug 6: pin every poll to the ``task_id`` we received from
            # ``research.start`` so a second research task started mid-wait
            # (e.g. by a concurrent caller, web UI, or retry) cannot
            # cross-wire its sources / report into this task's import branch.
            # Matches the pattern in ``cli/research.py:155-189``.
            _POLL_INTERVAL_S = 5
            status = None
            for _ in range(max(1, timeout // _POLL_INTERVAL_S)):
                status = await client.research.poll(nb_id_resolved, task_id=task_id)
                if status.get("status") == "completed":
                    break
                elif status.get("status") == "no_research":
                    console.print("[red]Research failed to start[/red]")
                    raise SystemExit(1)
                await asyncio.sleep(_POLL_INTERVAL_S)
            else:
                status = {"status": "timeout"}

            if status.get("status") == "completed":
                sources = status.get("sources", [])
                console.print()
                display_research_sources(sources)

                display_report(status.get("report", ""), json_hint=False)

                if import_all and sources and task_id:
                    import_result = await import_research_sources(
                        client,
                        nb_id_resolved,
                        task_id,
                        sources,
                        report=status.get("report", ""),
                        cited_only=cited_only,
                        max_elapsed=timeout,
                    )
                    console.print(f"[green]Imported {len(import_result.imported)} sources[/green]")
            else:
                console.print(f"[yellow]Status: {status.get('status', 'unknown')}[/yellow]")

    return _run()


@source.command("fulltext")
@click.argument("source_id")
@notebook_option
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
@click.option("--output", "-o", type=click.Path(), help="Write content to file")
@click.option(
    "--format",
    "-f",
    "output_format",
    type=click.Choice(["text", "markdown"]),
    default="text",
    help="Content format: text (default) or markdown",
)
@with_client
def source_fulltext(ctx, source_id, notebook_id, json_output, output, output_format, client_auth):
    """Get full content of a source.

    Retrieves the complete content from NotebookLM. Use --format markdown to get
    a rich version with headings, tables, links, and emphasis preserved.

    SOURCE_ID can be a full UUID or a partial prefix (e.g., 'abc' matches 'abc123...').

    \b
    Output shapes:
      Text mode (default):
        - No ``-o``: full content rendered to the terminal (truncated at 2000 chars).
        - With ``-o``: full content written to FILE; stderr/stdout shows a brief saved-N-chars line.
      JSON mode (``--json``):
        - No ``-o``: full ``asdict(SourceFulltext)`` payload on stdout
          (``{source_id, title, content, char_count, url, ...}``).
        - With ``-o``: full content written to FILE; a *metadata envelope*
          on stdout — ``{path, bytes, source_id, title}``. This avoids
          duplicating multi-MB fulltext to both stdout and disk while still
          giving automation a parseable stdout payload that names the file.

    \b
    Examples:
      notebooklm source fulltext abc123                        # Show plaintext in terminal
      notebooklm source fulltext abc123 -f markdown -o out.md  # Save markdown to file
      notebooklm source fulltext abc123 --json                 # Full JSON payload to stdout
      notebooklm source fulltext abc123 --json -o out.txt      # File + metadata envelope on stdout
    """
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)
            resolved_id = await resolve_source_id(
                client, nb_id_resolved, source_id, json_output=json_output
            )

            async def _fetch():
                return await client.sources.get_fulltext(
                    nb_id_resolved, resolved_id, output_format=output_format
                )

            if json_output:
                fulltext = await _fetch()
            else:
                with console.status("Fetching fulltext content..."):
                    fulltext = await _fetch()

            if json_output:
                # P1.T2 bug 4: when both --json and -o are given, write the
                # (potentially multi-MB) content to disk and emit a small
                # metadata envelope on stdout — not the full content twice.
                if output:
                    content_bytes = fulltext.content.encode("utf-8")
                    Path(output).write_bytes(content_bytes)
                    json_output_response(
                        {
                            "path": str(output),
                            "bytes": len(content_bytes),
                            "source_id": fulltext.source_id,
                            "title": fulltext.title,
                        }
                    )
                    return

                from dataclasses import asdict

                json_output_response(asdict(fulltext))
                return

            if output:
                Path(output).write_text(fulltext.content, encoding="utf-8")
                console.print(f"[green]Saved {fulltext.char_count} chars to {output}[/green]")
                return

            console.print(f"[bold cyan]Source:[/bold cyan] {fulltext.source_id}")
            console.print(f"[bold]Title:[/bold] {fulltext.title}")
            console.print(f"[bold]Characters:[/bold] {fulltext.char_count:,}")
            if fulltext.url:
                console.print(f"[bold]URL:[/bold] {fulltext.url}")
            console.print()
            console.print("[bold cyan]Content:[/bold cyan]")
            # markup=False so markdown links like `[text](url)` are not eaten by Rich's tag parser
            if len(fulltext.content) > 2000:
                console.print(fulltext.content[:2000], markup=False, highlight=False)
                console.print(
                    f"\n[dim]... ({fulltext.char_count - 2000:,} more chars, use -o to save full content)[/dim]"
                )
            else:
                console.print(fulltext.content, markup=False, highlight=False)

    return _run()


@source.command("guide")
@click.argument("source_id")
@notebook_option
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
@with_client
def source_guide(ctx, source_id, notebook_id, json_output, client_auth):
    """Get AI-generated source summary and keywords.

    Shows the "Source Guide" - an AI-generated overview of what a source contains,
    including a summary with highlighted keywords and topic tags.

    SOURCE_ID can be a full UUID or a partial prefix (e.g., 'abc' matches 'abc123...').

    \b
    Examples:
      notebooklm source guide abc123                    # Get guide for source
      notebooklm source guide abc123 --json             # Output as JSON
    """
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)
            resolved_id = await resolve_source_id(
                client, nb_id_resolved, source_id, json_output=json_output
            )

            async def _fetch_guide():
                return await client.sources.get_guide(nb_id_resolved, resolved_id)

            if json_output:
                guide = await _fetch_guide()
            else:
                with console.status("Generating source guide..."):
                    guide = await _fetch_guide()

            if json_output:
                data = {
                    "source_id": resolved_id,
                    "summary": guide.get("summary", ""),
                    "keywords": guide.get("keywords", []),
                }
                json_output_response(data)
                return

            summary = guide.get("summary", "").strip()
            keywords = guide.get("keywords", [])

            if not summary and not keywords:
                console.print("[yellow]No guide available for this source[/yellow]")
                return

            if summary:
                console.print("[bold cyan]Summary:[/bold cyan]")
                console.print(summary)
                console.print()

            if keywords:
                console.print("[bold cyan]Keywords:[/bold cyan]")
                console.print(", ".join(keywords))

    return _run()


@source.command("stale")
@click.argument("source_id")
@notebook_option
@json_option
@with_client
def source_stale(ctx, source_id, notebook_id, json_output, client_auth):
    """Check if a URL/Drive source needs refresh.

    Returns exit code 0 if stale (needs refresh), 1 if fresh.
    This enables shell scripting: if notebooklm source stale ID; then refresh; fi

    The inverted exit-code semantics are intentional and apply to ``--json``
    too — see docs/cli-exit-codes.md. Branch on the JSON ``stale`` field
    when the predicate-style exit code is awkward.

    SOURCE_ID can be a full UUID or a partial prefix (e.g., 'abc' matches 'abc123...').

    \b
    Examples:
      notebooklm source stale abc123              # Check if stale
      notebooklm source stale abc123 --json       # Same exit codes; JSON body
    """
    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)
            resolved_id = await resolve_source_id(
                client, nb_id_resolved, source_id, json_output=json_output
            )
            is_fresh = await client.sources.check_freshness(nb_id_resolved, resolved_id)
            stale = not is_fresh

            if json_output:
                # PRESERVE INVERTED EXIT-CODE SEMANTICS: ``source stale`` is the
                # only command that exits 0 on a "true predicate" and 1 on a
                # "false predicate". The JSON body carries the boolean
                # explicitly so callers who would prefer to branch on a field
                # rather than the exit code can do so.
                json_output_response(
                    {
                        "source_id": resolved_id,
                        "notebook_id": nb_id_resolved,
                        "stale": stale,
                        "fresh": is_fresh,
                    }
                )
                # Exit codes remain inverted by design — see docs/cli-exit-codes.md.
                raise SystemExit(0 if stale else 1)

            if is_fresh:
                console.print("[green]✓ Source is fresh[/green]")
                raise SystemExit(1)  # Not stale
            else:
                console.print("[yellow]⚠ Source is stale[/yellow]")
                console.print("[dim]Run 'source refresh' to update[/dim]")
                raise SystemExit(0)  # Is stale

    return _run()


@source.command("wait")
@click.argument("source_id")
@notebook_option
@wait_polling_options(default_timeout=120, default_interval=1)
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
@with_client
def source_wait(ctx, source_id, notebook_id, timeout, interval, json_output, client_auth):
    """Wait for a source to finish processing.

    After adding a source, it needs to be processed before it can be used
    for chat or artifact generation. This command polls until the source
    is ready or fails.

    SOURCE_ID can be a full UUID or a partial prefix (e.g., 'abc' matches 'abc123...').

    \b
    Exit codes:
      0 - Source is ready
      1 - Source not found or processing failed
      2 - Timeout reached

    \b
    Examples:
      notebooklm source wait abc123                          # Wait for source to be ready
      notebooklm source wait abc123 --timeout 300            # Wait up to 5 minutes
      notebooklm source wait abc123 --interval 5             # Poll every 5 seconds
      notebooklm source wait abc123 --json                   # Output status as JSON

    \b
    Subagent pattern for long-running operations:
      # In main conversation, add source then spawn subagent to wait:
      notebooklm source add https://example.com
      # Subagent runs: notebooklm source wait <source_id>
    """
    from ..types import SourceNotFoundError, SourceProcessingError, SourceTimeoutError

    nb_id = require_notebook(notebook_id)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)
            resolved_id = await resolve_source_id(
                client, nb_id_resolved, source_id, json_output=json_output
            )

            try:
                # Wrap the blocking poll in a transient spinner so interactive
                # Users see progress feedback during the wait.
                # Replaces the prior static "[dim]Waiting for source ...[/dim]"
                # print — the spinner conveys the same information AND a live
                # elapsed-seconds counter, then disappears so the final
                # ready / failure / timeout line stands alone. No-op under
                # --json so stdout stays pure JSON.
                async with status_with_elapsed(
                    f"Waiting for source {resolved_id} to finish processing...",
                    json_output=json_output,
                    # Parallel hint for ``source wait``: there is
                    # no separate ``source poll`` command, so the resume IS
                    # re-running the same wait. Keeps the ``Cancelled. Resume
                    # with: ...`` phrasing consistent across the three
                    # long-running paths.
                    resume_hint=f"notebooklm source wait {resolved_id}",
                ):
                    source = await client.sources.wait_until_ready(
                        nb_id_resolved,
                        resolved_id,
                        timeout=float(timeout),
                        initial_interval=float(interval),
                    )

                if json_output:
                    data = {
                        "source_id": source.id,
                        "title": source.title,
                        "status": "ready",
                        "status_code": source.status,
                    }
                    json_output_response(data)
                else:
                    console.print(f"[green]✓ Source ready:[/green] {source.id}")
                    if source.title:
                        console.print(f"[bold]Title:[/bold] {source.title}")

            except SourceNotFoundError as e:
                if json_output:
                    data = {
                        "source_id": e.source_id,
                        "status": "not_found",
                        "error": str(e),
                    }
                    json_output_response(data)
                else:
                    console.print(f"[red]✗ Source not found:[/red] {e.source_id}")
                raise SystemExit(1) from None

            except SourceProcessingError as e:
                if json_output:
                    data = {
                        "source_id": e.source_id,
                        "status": "error",
                        "status_code": e.status,
                        "error": str(e),
                    }
                    json_output_response(data)
                else:
                    console.print(f"[red]✗ Source processing failed:[/red] {e.source_id}")
                raise SystemExit(1) from None

            except SourceTimeoutError as e:
                if json_output:
                    data = {
                        "source_id": e.source_id,
                        "status": "timeout",
                        "last_status_code": e.last_status,
                        "timeout_seconds": int(e.timeout),
                        "error": str(e),
                    }
                    json_output_response(data)
                else:
                    console.print(f"[yellow]⚠ Timeout waiting for source:[/yellow] {e.source_id}")
                    console.print(f"[dim]Last status: {e.last_status}[/dim]")
                raise SystemExit(2) from None

    return _run()


@source.command("clean")
@notebook_option
@click.option(
    "--dry-run", is_flag=True, help="Show what would be deleted without actually deleting"
)
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
@json_option
@with_client
def source_clean(ctx, notebook_id, dry_run, yes, json_output, client_auth):
    """Automatically remove duplicate, error, and access-blocked sources."""
    nb_id = require_notebook(notebook_id)

    quiet_mode = is_quiet(ctx)

    async def _run():
        async with NotebookLMClient(client_auth) as client:
            nb_id_resolved = await resolve_notebook_id(client, nb_id, json_output=json_output)

            async def _list_sources(notebook_id: str) -> list[Source]:
                if json_output:
                    return await client.sources.list(notebook_id)
                with cli_status("Fetching sources for cleanup...", ctx=ctx):
                    return await client.sources.list(notebook_id)

            # P1.T2 bug 3: in --json mode, never prompt — automation cannot
            # answer the question. Pass a non-interactive ``confirm_delete``
            # that always declines; once the service returns ``cancelled`` we
            # synthesize a structured ``CONFIRM_REQUIRED`` error below
            # (only when there were candidates — empty notebooks short-circuit
            # to ``already_clean`` before ``confirm_delete`` is ever called).
            confirm_delete = (
                (lambda count: False)
                if json_output
                else (lambda count: click.confirm(f"Delete {count} source(s)?"))
            )

            # Candidate tables and progress lines are status prose; JSON and
            # quiet modes both suppress them.
            on_candidates = None if (json_output or quiet_mode) else _print_clean_candidates
            on_delete_start = (
                None
                if (json_output or quiet_mode)
                else lambda count: cli_print(
                    f"[dim]Cleaning {count} source(s) (in chunks of 10)...[/dim]",
                    ctx=ctx,
                )
            )

            result = await source_clean_service.run_source_clean(
                notebook_id=nb_id_resolved,
                dry_run=dry_run,
                yes=yes,
                list_sources=_list_sources,
                delete_source=client.sources.delete,
                confirm_delete=confirm_delete,
                on_candidates=on_candidates,
                on_delete_start=on_delete_start,
                classify_sources=_classify_junk_sources,
            )

            candidate_payload = source_clean_service.candidates_payload(result.candidates)

            if json_output:
                # P1.T2 bug 3: synthesize structured error when --json + no
                # --yes left candidates uncleaned (the silent
                # ``status=cancelled`` form would be invisible to automation).
                if result.status == "cancelled" and not yes:
                    _require_yes_in_json(
                        action="clean",
                        extra={
                            "notebook_id": result.notebook_id,
                            "candidate_count": result.candidate_count,
                            "candidates": candidate_payload,
                        },
                    )

                payload = {
                    "action": "clean",
                    "notebook_id": result.notebook_id,
                    "status": result.status,
                    "candidates": candidate_payload,
                    "deleted_count": result.deleted_count,
                    "failure_count": result.failure_count,
                }
                if result.status != "already_clean":
                    payload["candidate_count"] = result.candidate_count
                if result.status == "completed":
                    payload["failures"] = [
                        {"id": sid, "error": err} for sid, err in result.failures
                    ]
                json_output_response(payload)
                # P1.T2 bug 8: partial-failure must exit non-zero so shell
                # automation (set -e, CI) sees the failure. The full report
                # is still on stdout above so callers can introspect which
                # IDs failed.
                if result.failures:
                    raise SystemExit(1)
                return

            if result.status == "already_clean":
                cli_print(
                    "[green]Notebook is already clean. No junk sources found.[/green]",
                    ctx=ctx,
                )
                return

            if result.status == "dry_run":
                cli_print(
                    f"[yellow]Dry run: would delete {result.candidate_count} source(s).[/yellow]",
                    ctx=ctx,
                )
                return

            if result.status == "cancelled":
                return

            if result.failures:
                cli_print(
                    f"[yellow]Cleaned {result.deleted_count} source(s). "
                    f"{len(result.failures)} deletion(s) failed.[/yellow]",
                    ctx=ctx,
                )
                for sid, err in result.failures[:5]:
                    cli_print(f"  [red]{sid}:[/red] {err}", ctx=ctx)
                if len(result.failures) > 5:
                    cli_print(
                        f"  [dim]...and {len(result.failures) - 5} more[/dim]",
                        ctx=ctx,
                    )
                # P1.T2 bug 8: text-mode parity with JSON-mode exit code.
                raise SystemExit(1)

            cli_print(
                f"[green]Successfully cleaned {result.deleted_count} source(s).[/green]",
                ctx=ctx,
            )

    return _run()
