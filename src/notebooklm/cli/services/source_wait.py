"""Service for ``source wait`` — long-running source-readiness poll.

Owns the dataclass + executor pair so ``cli/source_cmd.py`` stays a thin
Click handler. The executor wraps the underlying
``client.sources.wait_until_ready`` call in a transient
``status_with_elapsed`` spinner (suppressed under JSON) and translates the
three ``SourceWaitError`` subclasses into the documented exit-code +
envelope contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ...types import (
    Source,
    SourceNotFoundError,
    SourceProcessingError,
    SourceTimeoutError,
)
from ..error_handler import exit_with_code
from ..rendering import console, json_output_response
from .polling import status_with_elapsed

if TYPE_CHECKING:
    from ...client import NotebookLMClient


@dataclass(frozen=True)
class SourceWaitPlan:
    """Prepared inputs for ``execute_source_wait``."""

    notebook_id: str
    source_id: str
    timeout: float
    interval: float
    json_output: bool


def _emit_ready_payload(source: Source, *, json_output: bool) -> None:
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


def _emit_not_found(exc: SourceNotFoundError, *, json_output: bool) -> None:
    if json_output:
        json_output_response(
            {
                "source_id": exc.source_id,
                "status": "not_found",
                "error": str(exc),
            }
        )
    else:
        console.print(f"[red]✗ Source not found:[/red] {exc.source_id}")


def _emit_processing_error(exc: SourceProcessingError, *, json_output: bool) -> None:
    if json_output:
        json_output_response(
            {
                "source_id": exc.source_id,
                "status": "error",
                "status_code": exc.status,
                "error": str(exc),
            }
        )
    else:
        console.print(f"[red]✗ Source processing failed:[/red] {exc.source_id}")


def _emit_timeout(exc: SourceTimeoutError, *, json_output: bool) -> None:
    if json_output:
        json_output_response(
            {
                "source_id": exc.source_id,
                "status": "timeout",
                "last_status_code": exc.last_status,
                "timeout_seconds": int(exc.timeout),
                "error": str(exc),
            }
        )
    else:
        console.print(f"[yellow]⚠ Timeout waiting for source:[/yellow] {exc.source_id}")
        console.print(f"[dim]Last status: {exc.last_status}[/dim]")


async def execute_source_wait(client: NotebookLMClient, plan: SourceWaitPlan) -> None:
    """Run the ``source wait`` workflow with status spinner + error mapping.

    Exit-code contract (matches ``source_cmd.source_wait`` pre-extraction):
        * 0 — source reached READY before timeout.
        * 1 — :class:`SourceNotFoundError` or :class:`SourceProcessingError`.
        * 2 — :class:`SourceTimeoutError`.

    Caller is responsible for resolving ``plan.source_id`` to a full UUID
    BEFORE calling this executor (so the spinner message and JSON envelope
    carry the resolved id consistently).
    """
    try:
        async with status_with_elapsed(
            f"Waiting for source {plan.source_id} to finish processing...",
            json_output=plan.json_output,
            # Parallel hint: ``source wait`` has no separate ``source
            # poll`` command, so the resume IS re-running the same wait.
            resume_hint=f"notebooklm source wait {plan.source_id}",
        ):
            source = await client.sources.wait_until_ready(
                plan.notebook_id,
                plan.source_id,
                timeout=plan.timeout,
                initial_interval=plan.interval,
            )
    except SourceNotFoundError as exc:
        _emit_not_found(exc, json_output=plan.json_output)
        exit_with_code(1)
    except SourceProcessingError as exc:
        _emit_processing_error(exc, json_output=plan.json_output)
        exit_with_code(1)
    except SourceTimeoutError as exc:
        _emit_timeout(exc, json_output=plan.json_output)
        exit_with_code(2)
    else:
        _emit_ready_payload(source, json_output=plan.json_output)


__all__ = ["SourceWaitPlan", "execute_source_wait"]
