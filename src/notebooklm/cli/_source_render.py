"""Source CLI render/validation helpers (extracted from ``source_cmd.py``).

This module holds client-free render and validation helpers used by the
``source`` command group. Selected names are re-exported from ``source_cmd``
to preserve the historical ``source_cmd.<helper>`` import/patch surface, and a
few wrappers remain explicit test patch points.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, NoReturn

import click
from rich.markup import render as render_markup
from rich.table import Table

from .._app import source_add as source_add_service
from .._app import source_clean as source_clean_service
from .._app.source_add import SourceAddResult
from .._app.source_content import (
    SourceFulltextResult,
    SourceGetResult,
    SourceGuideResult,
    SourceStaleResult,
)
from .._app.source_wait import (
    SourceWaitNotFound,
    SourceWaitOutcome,
    SourceWaitProcessingError,
    SourceWaitReady,
    SourceWaitTimeout,
)
from ..types import Source, source_status_to_str
from .error_handler import _output_error, current_json_output, exit_with_code
from .rendering import (
    cli_print,
    console,
    display_report,
    display_research_sources,
    emit_status,
    get_source_type_display,
    json_output_response,
)
from .services.source_mutations import (
    SourceAddDriveResult,
    SourceDeleteByTitleResult,
    SourceDeleteResult,
    SourceMutationError,
    SourceRefreshResult,
    SourceRenameResult,
)
from .services.source_research import SourceAddResearchResult
from .services.source_serializers import (
    source_fulltext_payload,
    source_kind_value,
    source_summary_payload,
)

# Compatibility wrappers — tests patch these names on this module. Each
# one is a one-liner forwarder to the canonical service-layer home.


def _looks_like_path(content: str) -> bool:
    """Compatibility wrapper for tests patching source-add path detection."""
    return source_add_service.looks_like_path(content)


def _validate_upload_path(content: str, follow_symlinks: bool) -> Path:
    """Compatibility wrapper for tests patching source-add upload validation."""
    try:
        return source_add_service.validate_upload_path(content, follow_symlinks)
    except source_add_service.SourceAddValidationError as exc:
        _output_error(f"Error: {exc}", "VALIDATION_ERROR", current_json_output(), 1)
        raise AssertionError("unreachable") from None  # pragma: no cover


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


def _render_source_get_result(result: SourceGetResult, *, json_output: bool) -> None:
    """Render ``source get`` output and not-found exit policy."""
    src = result.source
    if src is None:
        _output_error(
            "Source not found",
            code="NOT_FOUND",
            json_output=json_output,
            exit_code=1,
            extra={"source_id": result.source_id, "notebook_id": result.notebook_id},
        )
        raise AssertionError("unreachable")  # pragma: no cover

    if json_output:
        json_output_response(
            {
                "source": {
                    **source_summary_payload(src),
                    "status": source_status_to_str(src.status),
                    "status_id": src.status,
                    "created_at": (src.created_at.isoformat() if src.created_at else None),
                },
                "found": True,
            }
        )
        return

    console.print(f"[bold cyan]Source:[/bold cyan] {src.id}")
    console.print(f"[bold]Title:[/bold] {src.title}")
    console.print(f"[bold]Type:[/bold] {get_source_type_display(src.kind)}")
    if src.url:
        console.print(f"[bold]URL:[/bold] {src.url}")
    if src.created_at:
        console.print(f"[bold]Created:[/bold] {src.created_at.strftime('%Y-%m-%d %H:%M')}")


def _available_output_path(path: Path) -> Path:
    """Return an available sibling path using the download command's suffix style."""
    counter = 2
    base_name = path.stem
    parent = path.parent
    ext = path.suffix
    while path.exists():
        path = parent / f"{base_name} ({counter}){ext}"
        counter += 1
    return path


def _emit_source_fulltext_flag_conflict(message: str, *, json_output: bool) -> NoReturn:
    """Surface a ``source fulltext`` flag conflict via the active CLI error contract."""
    if json_output:
        _output_error(message, "VALIDATION_ERROR", json_output, 1)
        raise AssertionError("unreachable")  # pragma: no cover
    raise click.UsageError(  # cli-input-validation: source fulltext flag conflict
        message
    )


def _resolve_source_fulltext_output_path(
    output: str,
    *,
    force: bool,
    no_clobber: bool,
    json_output: bool,
) -> Path:
    """Resolve ``source fulltext -o`` conflicts without silently overwriting."""
    path = Path(output)
    if path.exists() and path.is_dir():
        _emit_source_fulltext_flag_conflict(
            f"Output path is a directory: {path}",
            json_output=json_output,
        )
    if force and no_clobber:
        _emit_source_fulltext_flag_conflict(
            "Cannot specify both --force and --no-clobber",
            json_output=json_output,
        )
    if not path.exists() or force:
        return path
    if no_clobber:
        suggestion = "Use --force to overwrite or choose a different path"
        _output_error(
            f"File exists: {path}",
            "FILE_EXISTS",
            json_output,
            1,
            extra={"path": str(path), "suggestion": suggestion},
            hint=suggestion,
        )
        raise AssertionError("unreachable")  # pragma: no cover
    return _available_output_path(path)


def _render_source_fulltext_result(
    result: SourceFulltextResult,
    *,
    json_output: bool,
    output: Path | None,
) -> None:
    """Render ``source fulltext`` output, including optional file output."""
    fulltext = result.fulltext
    if json_output:
        if output:
            content_bytes = fulltext.content.encode("utf-8")
            output.write_bytes(content_bytes)
            json_output_response(
                {
                    "path": str(output),
                    "bytes": len(content_bytes),
                    "source_id": fulltext.source_id,
                    "title": fulltext.title,
                    "kind": source_kind_value(fulltext.kind),
                }
            )
            return

        json_output_response(source_fulltext_payload(fulltext))
        return

    if output:
        output.write_text(fulltext.content, encoding="utf-8")
        console.print(
            f"Saved {fulltext.char_count} chars to {output}",
            style="green",
            markup=False,
            soft_wrap=True,
        )
        return

    console.print(f"[bold cyan]Source:[/bold cyan] {fulltext.source_id}")
    console.print(f"[bold]Title:[/bold] {fulltext.title}")
    console.print(f"[bold]Characters:[/bold] {fulltext.char_count:,}")
    if fulltext.url:
        console.print(f"[bold]URL:[/bold] {fulltext.url}")
    console.print()
    console.print("[bold cyan]Content:[/bold cyan]")
    if len(fulltext.content) > 2000:
        console.print(fulltext.content[:2000], markup=False, highlight=False)
        console.print(
            f"\n[dim]... ({fulltext.char_count - 2000:,} more chars, "
            "use -o to save full content)[/dim]"
        )
    else:
        console.print(fulltext.content, markup=False, highlight=False)


def _render_source_guide_result(result: SourceGuideResult, *, json_output: bool) -> None:
    """Render ``source guide`` output."""
    if json_output:
        json_output_response(
            {
                "source_id": result.source_id,
                "summary": result.summary,
                "keywords": result.keywords,
            }
        )
        return

    summary = result.summary.strip()
    if not summary and not result.keywords:
        console.print("[yellow]No guide available for this source[/yellow]")
        return

    if summary:
        console.print("[bold cyan]Summary:[/bold cyan]")
        console.print(summary)
        console.print()

    if result.keywords:
        console.print("[bold cyan]Keywords:[/bold cyan]")
        console.print(", ".join(result.keywords))


def _render_source_wait_outcome(outcome: SourceWaitOutcome, *, json_output: bool) -> None:
    """Render the ``source wait`` outcome and exit with the documented code.

    Exit codes (preserved from the service-side contract):
        * 0 — :class:`SourceWaitReady`.
        * 1 — :class:`SourceWaitNotFound` or :class:`SourceWaitProcessingError`.
        * 2 — :class:`SourceWaitTimeout`.
    """
    if isinstance(outcome, SourceWaitReady):
        source = outcome.source
        if json_output:
            json_output_response(
                {
                    "source_id": source.id,
                    "title": source.title,
                    "status": "ready",
                    "status_code": source.status,
                }
            )
            return
        console.print(f"[green]✓ Source ready:[/green] {source.id}")
        if source.title:
            console.print(f"[bold]Title:[/bold] {source.title}")
        return

    elif isinstance(outcome, SourceWaitNotFound):
        not_found_error = outcome.error
        if json_output:
            json_output_response(
                {
                    "source_id": not_found_error.source_id,
                    "status": "not_found",
                    "error": str(not_found_error),
                }
            )
        else:
            console.print(f"[red]✗ Source not found:[/red] {not_found_error.source_id}")
        exit_with_code(1)
        raise AssertionError("unreachable")  # pragma: no cover

    elif isinstance(outcome, SourceWaitProcessingError):
        processing_error = outcome.error
        if json_output:
            json_output_response(
                {
                    "source_id": processing_error.source_id,
                    "status": "error",
                    "status_code": processing_error.status,
                    "error": str(processing_error),
                }
            )
        else:
            console.print(f"[red]✗ Source processing failed:[/red] {processing_error.source_id}")
        exit_with_code(1)
        raise AssertionError("unreachable")  # pragma: no cover

    elif isinstance(outcome, SourceWaitTimeout):
        timeout_error = outcome.error
        if json_output:
            json_output_response(
                {
                    "source_id": timeout_error.source_id,
                    "status": "timeout",
                    "last_status_code": timeout_error.last_status,
                    "timeout_seconds": int(timeout_error.timeout),
                    "error": str(timeout_error),
                }
            )
        else:
            console.print(
                f"[yellow]⚠ Timeout waiting for source:[/yellow] {timeout_error.source_id}"
            )
            console.print(f"[dim]Last status: {timeout_error.last_status}[/dim]")
        exit_with_code(2)
        raise AssertionError("unreachable")  # pragma: no cover

    raise AssertionError(f"unreachable: {type(outcome)}")


def _render_source_stale_result(
    result: SourceStaleResult, *, json_output: bool, exit_on_stale: bool = False
) -> None:
    """Render ``source stale`` output and pick the exit-code policy.

    Default policy is the standard CLI convention: exit ``0`` if the
    freshness check succeeded (regardless of whether the source is fresh
    or stale), exit ``1`` only if an error occurred (raised earlier via
    ``handle_errors``). Callers branch on the JSON ``stale``/``fresh``
    fields (or the rendered text) to decide what to do.

    Passing ``exit_on_stale=True`` (CLI: ``--exit-on-stale``) opts into
    the back-compat inverted-predicate semantics — exit ``0`` if stale,
    ``1`` if fresh — so the shell idiom
    ``if notebooklm source stale --exit-on-stale ID; then refresh; fi``
    keeps working for scripts written against the prior default.

    See ``docs/cli-exit-codes.md`` for the canonical exit-code table and
    the ``source stale`` section for the inverted-predicate opt-in.
    """
    if json_output:
        json_output_response(
            {
                "source_id": result.source_id,
                "notebook_id": result.notebook_id,
                "stale": result.stale,
                "fresh": result.is_fresh,
            }
        )
        if exit_on_stale:
            exit_with_code(0 if result.stale else 1)
        return

    if result.is_fresh:
        console.print("[green]✓ Source is fresh[/green]")
        if exit_on_stale:
            exit_with_code(1)
        return

    console.print("[yellow]⚠ Source is stale[/yellow]")
    console.print("[dim]Run 'source refresh' to update[/dim]")
    if exit_on_stale:
        exit_with_code(0)


def _handle_source_mutation_error(exc: SourceMutationError, *, json_output: bool) -> NoReturn:
    """Render a typed source-mutation error through the CLI error contract."""
    extra = dict(exc.extra) if exc.extra else None
    hint = None
    if exc.status_message:
        plain_status = render_markup(exc.status_message).plain
        if json_output:
            extra = extra or {}
            extra["status_message"] = plain_status
        else:
            hint = plain_status
    _output_error(
        exc.message,
        code=exc.code,
        json_output=json_output,
        exit_code=1,
        extra=extra,
        hint=hint,
    )
    raise AssertionError("unreachable")  # pragma: no cover


def _delete_status_value(status: str, success: bool) -> str:
    """Map a delete outcome status onto the historical ``--json`` status string."""
    if status == "cancelled":
        return "cancelled"
    return "deleted" if success else "unknown"


def _source_delete_payload(result: SourceDeleteResult) -> dict[str, Any]:
    """Build the ``source delete`` ``--json`` envelope from the typed result (§11)."""
    return {
        "action": "delete",
        "source_id": result.source_id,
        "notebook_id": result.notebook_id,
        "success": result.success,
        "status": _delete_status_value(result.status, result.success),
    }


def _source_delete_by_title_payload(result: SourceDeleteByTitleResult) -> dict[str, Any]:
    """Build the ``source delete-by-title`` ``--json`` envelope (§11)."""
    return {
        "action": "delete-by-title",
        "source_id": result.source_id,
        "title": result.title,
        "notebook_id": result.notebook_id,
        "success": result.success,
        "status": _delete_status_value(result.status, result.success),
    }


def _source_rename_payload(result: SourceRenameResult) -> dict[str, Any]:
    """Build the ``source rename`` ``--json`` envelope from the typed result (§11)."""
    return {
        "action": "rename",
        "source_id": result.source.id,
        "notebook_id": result.notebook_id,
        "title": result.source.title,
        "status": "renamed",
    }


def _source_refresh_payload(result: SourceRefreshResult) -> dict[str, Any]:
    """Build the ``source refresh`` ``--json`` envelope from the typed result (§11)."""
    refreshed = result.result
    if isinstance(refreshed, Source):
        return {
            "action": "refresh",
            "source_id": refreshed.id,
            "notebook_id": result.notebook_id,
            "title": refreshed.title,
            "status": "refreshed",
        }
    # ``sources.refresh`` returns ``None`` on success (#1290); any failure
    # raises before reaching here, so ``None`` is the refreshed-OK case.
    return {
        "action": "refresh",
        "source_id": result.source_id,
        "notebook_id": result.notebook_id,
        "status": "refreshed",
    }


def _render_source_delete_result(
    result: SourceDeleteResult | SourceDeleteByTitleResult,
    *,
    json_output: bool,
    ctx: click.Context,
) -> None:
    if result.status_message:
        emit_status(result.status_message, json_output=json_output)

    if json_output:
        payload = (
            _source_delete_by_title_payload(result)
            if isinstance(result, SourceDeleteByTitleResult)
            else _source_delete_payload(result)
        )
        json_output_response(payload)
        return

    if result.status == "cancelled":
        return
    if result.success:
        cli_print(f"[green]Deleted source:[/green] {result.source_id}", ctx=ctx)
    else:
        cli_print("[yellow]Delete may have failed[/yellow]", ctx=ctx)


def _render_source_rename_result(
    result: SourceRenameResult,
    *,
    json_output: bool,
    ctx: click.Context,
) -> None:
    if json_output:
        json_output_response(_source_rename_payload(result))
        return

    cli_print(f"[green]Renamed source:[/green] {result.source.id}", ctx=ctx)
    cli_print(f"[bold]New title:[/bold] {result.source.title}", ctx=ctx)


def _render_source_refresh_result(
    result: SourceRefreshResult,
    *,
    json_output: bool,
    ctx: click.Context,
) -> None:
    if json_output:
        json_output_response(_source_refresh_payload(result))
        return

    refreshed = result.result
    if isinstance(refreshed, Source):
        cli_print(f"[green]Source refreshed:[/green] {refreshed.id}", ctx=ctx)
        cli_print(f"[bold]Title:[/bold] {refreshed.title}", ctx=ctx)
    else:
        # ``sources.refresh`` returns ``None`` on success (#1290); failures
        # raise before reaching here, so ``None`` is the refreshed-OK case.
        cli_print(f"[green]Source refreshed:[/green] {result.source_id}", ctx=ctx)


def source_add_payload(result: SourceAddResult) -> dict[str, Any]:
    """Build the ``source add`` ``--json`` envelope from the typed result.

    The envelope is ``{"source": {...summary...}}`` where the inner summary is
    the neutral ``source_summary_payload`` shape. Built in the CLI render layer
    (§11) so the ``_app`` result dataclass stays typed-fields-only.
    """
    return {"source": source_summary_payload(result.source)}


def _render_source_add_drive_result(
    result: SourceAddDriveResult,
    *,
    json_output: bool,
    ctx: click.Context,
) -> None:
    if json_output:
        # The add-drive envelope embeds the ``source_summary_payload`` serializer
        # (presentation), so it is built here rather than on the neutral result.
        json_output_response(
            {
                "action": "add-drive",
                "source": {
                    **source_summary_payload(result.source),
                    "drive_file_id": result.file_id,
                    "mime_type": result.mime_type,
                },
                "notebook_id": result.notebook_id,
            }
        )
        return

    cli_print(f"[green]Added Drive source:[/green] {result.source.id}", ctx=ctx)
    cli_print(f"[bold]Title:[/bold] {result.source.title}", ctx=ctx)


def _emit_add_research_flag_conflict(message: str, *, json_output: bool) -> NoReturn:
    """Surface a ``source add-research`` flag conflict via the active CLI error contract.

    Per ADR-0015, post-parse flag-combination failures route through the typed
    JSON envelope under ``--json`` (exit ``1`` with
    ``{"error": true, "code": "VALIDATION_ERROR", ...}`` on stdout) and via
    Click's parser-style ``UsageError`` otherwise (exit ``2`` with usage text
    on stderr). Never returns — both branches raise.
    """
    if json_output:
        _output_error(message, "VALIDATION_ERROR", json_output, 1)
        raise AssertionError("unreachable")  # pragma: no cover
    raise click.UsageError(  # cli-input-validation: source add-research flag conflict
        message
    )


def _print_add_research_task_ids(result: SourceAddResearchResult) -> None:
    if result.start_task_id:
        console.print(f"[dim]Task ID: {result.start_task_id}[/dim]")
    if result.poll_task_id and result.poll_task_id != result.start_task_id:
        console.print(f"[dim]Poll ID: {result.poll_task_id}[/dim]")


def _exit_with_add_research_status(status: str, message: str, **extra: Any) -> NoReturn:
    payload: dict[str, Any] = {"status": status, "error": message}
    payload.update(extra)
    json_output_response(payload)
    exit_with_code(1)


def _render_add_research_result(result: SourceAddResearchResult, *, json_output: bool) -> None:
    """Render :class:`SourceAddResearchResult` and exit on non-success outcomes.

    The handler owns all CLI I/O — text vs JSON, exit codes, the
    ``Starting ... research`` info line, and the ``Imported N sources``
    summary — so the service layer can stay pure (ADR-0008) and exit-policy
    free.
    """
    if result.outcome == "start_failed":
        if json_output:
            _output_error("Research failed to start", "VALIDATION_ERROR", json_output, 1)
        else:
            console.print("[red]Research failed to start[/red]")
            exit_with_code(1)
        return  # pragma: no cover — both branches above terminate

    if not json_output:
        _print_add_research_task_ids(result)

    if result.outcome == "started_no_wait":
        if json_output:
            payload: dict[str, Any] = {
                "status": "started",
                "task_id": result.start_task_id,
            }
            if result.poll_task_id and result.poll_task_id != result.start_task_id:
                payload["poll_task_id"] = result.poll_task_id
            json_output_response(payload)
            return
        console.print(
            "[green]Research started.[/green] "
            "Run 'notebooklm research wait --import-all' to commit "
            "sources once it completes, otherwise the NotebookLM web "
            "UI will keep an 'Add sources?' modal open."
        )
        return

    if result.outcome == "no_research":
        if json_output:
            _exit_with_add_research_status("no_research", "Research failed to start")
        else:
            console.print("[red]Research failed to start[/red]")
            exit_with_code(1)
        return  # pragma: no cover

    if result.outcome in ("failed", "timeout"):
        message = "Research timed out" if result.outcome == "timeout" else "Research failed"
        if json_output:
            _exit_with_add_research_status(result.outcome, message)
        else:
            console.print(f"[red]{message}[/red]")
            exit_with_code(1)
        return  # pragma: no cover

    if result.outcome == "unknown_status":
        status_val = result.status or "unknown"
        if json_output:
            _exit_with_add_research_status(
                "unknown_status",
                f"Unexpected research status: {status_val}",
                raw_status=status_val,
            )
        else:
            console.print(f"[yellow]Status: {status_val}[/yellow]")
            exit_with_code(1)
        return  # pragma: no cover

    # outcome == "completed"
    if json_output:
        completed_payload: dict[str, Any] = {
            "status": "completed",
            "task_id": result.poll_task_id,
            "sources_found": len(result.sources),
            "sources": result.sources,
            "report": result.report,
        }
        import_result = result.import_result
        if import_result is not None:
            if import_result.cited_selection is not None:
                completed_payload["cited_only"] = True
                completed_payload["cited_sources_selected"] = len(import_result.sources)
                completed_payload["cited_only_fallback"] = (
                    import_result.cited_selection.used_fallback
                )
            completed_payload["imported"] = len(import_result.imported)
            completed_payload["imported_sources"] = import_result.imported
        json_output_response(completed_payload)
        return

    # Text mode
    console.print()
    display_research_sources(result.sources)
    display_report(result.report, json_hint=False)
    import_result = result.import_result
    if import_result is not None:
        console.print(f"[green]Imported {len(import_result.imported)} sources[/green]")
