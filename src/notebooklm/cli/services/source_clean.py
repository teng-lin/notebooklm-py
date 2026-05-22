"""Service helpers for the ``source clean`` command."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlparse, urlunparse

from ...types import Source, source_status_to_str

if TYPE_CHECKING:
    import click

    from ...client import NotebookLMClient

CleanCandidate = tuple[str, str, str, str]
CleanFailure = tuple[str, str]
CleanStatus = Literal["already_clean", "dry_run", "cancelled", "completed"]


@dataclass(frozen=True)
class SourceCleanResult:
    """Result of source-clean orchestration."""

    notebook_id: str
    status: CleanStatus
    candidates: tuple[CleanCandidate, ...]
    deleted_count: int = 0
    failures: tuple[CleanFailure, ...] = ()

    @property
    def failure_count(self) -> int:
        return len(self.failures)

    @property
    def candidate_count(self) -> int:
        return len(self.candidates)


_GATEWAY_TITLE_PATTERN = re.compile(
    r"^\s*(access denied|403|404|forbidden|not found|502"
    r"|just a moment|attention required|security check|captcha)",
    re.IGNORECASE,
)
_JUNK_STATUSES = frozenset({"error"})
_UNDATED_SORT_KEY = float("inf")


def normalize_url_for_dedup(url: str) -> str:
    """Return a URL with only the fragment stripped, for dedup comparison."""
    parsed = urlparse(url)
    return urlunparse(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path,
            parsed.params,
            parsed.query,
            "",
        )
    )


def classify_junk_sources(sources: list[Source]) -> list[CleanCandidate]:
    """Identify duplicate, error, and access-blocked sources for cleanup."""
    sorted_sources = sorted(
        sources,
        key=lambda s: s.created_at.timestamp() if s.created_at else _UNDATED_SORT_KEY,
    )

    candidates: list[CleanCandidate] = []
    seen_urls: dict[str, str] = {}

    for source in sorted_sources:
        title = (source.title or "").strip()
        status = source_status_to_str(source.status) if source.status else "unknown"

        if status in _JUNK_STATUSES:
            candidates.append((source.id, title, status, "error_status"))
            continue

        if _GATEWAY_TITLE_PATTERN.match(title):
            candidates.append((source.id, title, status, "gateway_title"))
            continue

        url = source.url or ""
        if url:
            normalized = normalize_url_for_dedup(url)
            kept = seen_urls.get(normalized)
            if kept is not None:
                candidates.append((source.id, title, status, f"duplicate_of:{kept[:8]}"))
                continue
            seen_urls[normalized] = source.id

    return candidates


def candidates_payload(candidates: Sequence[CleanCandidate]) -> list[dict[str, str]]:
    """Convert clean candidates to the JSON payload shape."""
    return [
        {"id": sid, "title": title, "status": status, "reason": reason}
        for sid, title, status, reason in candidates
    ]


async def run_source_clean(
    *,
    notebook_id: str,
    dry_run: bool,
    yes: bool,
    list_sources: Callable[[str], Awaitable[list[Source]]],
    delete_source: Callable[[str, str], Awaitable[object]],
    confirm_delete: Callable[[int], bool],
    on_candidates: Callable[[list[CleanCandidate]], None] | None = None,
    on_delete_start: Callable[[int], None] | None = None,
    classify_sources: Callable[[list[Source]], list[CleanCandidate]] = classify_junk_sources,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> SourceCleanResult:
    """Classify and optionally delete junk sources."""
    sources = await list_sources(notebook_id)
    candidates = classify_sources(sources)

    if not candidates:
        return SourceCleanResult(
            notebook_id=notebook_id,
            status="already_clean",
            candidates=(),
        )

    if on_candidates is not None:
        on_candidates(candidates)

    if dry_run:
        return SourceCleanResult(
            notebook_id=notebook_id,
            status="dry_run",
            candidates=tuple(candidates),
        )

    if not yes and not confirm_delete(len(candidates)):
        return SourceCleanResult(
            notebook_id=notebook_id,
            status="cancelled",
            candidates=tuple(candidates),
        )

    if on_delete_start is not None:
        on_delete_start(len(candidates))

    delete_list = [candidate[0] for candidate in candidates]
    chunk_size = 10
    deleted = 0
    failures: list[CleanFailure] = []
    for i in range(0, len(delete_list), chunk_size):
        chunk = delete_list[i : i + chunk_size]
        delete_tasks = [delete_source(notebook_id, sid) for sid in chunk]
        results = await asyncio.gather(*delete_tasks, return_exceptions=True)
        for sid, result in zip(chunk, results, strict=True):
            if isinstance(result, Exception):
                failures.append((sid, str(result)))
            else:
                deleted += 1
        if i + chunk_size < len(delete_list):
            await sleep(0.5)

    return SourceCleanResult(
        notebook_id=notebook_id,
        status="completed",
        candidates=tuple(candidates),
        deleted_count=deleted,
        failures=tuple(failures),
    )


@dataclass(frozen=True)
class SourceCleanPlan:
    """Click-shaped inputs for ``execute_source_clean``."""

    notebook_id: str
    dry_run: bool
    yes: bool
    json_output: bool
    quiet_mode: bool


async def execute_source_clean(
    client: NotebookLMClient,
    plan: SourceCleanPlan,
    *,
    ctx: click.Context | None = None,
    classify_sources: Callable[[list[Source]], list[CleanCandidate]] = classify_junk_sources,
    on_candidates: Callable[[list[CleanCandidate]], None] | None = None,
) -> None:
    """Run the ``source clean`` workflow with progress + JSON / text rendering.

    Caller may pass ``classify_sources`` and ``on_candidates`` callbacks so
    tests that patch the source-cmd compatibility wrappers (e.g.
    ``_classify_junk_sources``, ``_print_clean_candidates``) continue to see
    their patched implementations exercised end-to-end. Defaults are the
    canonical service-layer implementations.
    """
    # Local imports keep this service module light at import time and avoid
    # pulling click / rendering into non-CLI callers of ``run_source_clean``.
    import click as _click

    from ..error_handler import exit_with_code
    from ..rendering import cli_print, cli_status, json_output_response
    from .source_mutations import require_yes_in_json

    async def _list_sources(notebook_id: str) -> list[Source]:
        if plan.json_output:
            return await client.sources.list(notebook_id)
        with cli_status("Fetching sources for cleanup...", ctx=ctx):
            return await client.sources.list(notebook_id)

    # P1.T2 bug 3: in --json mode, never prompt — automation cannot answer
    # the question. Pass a non-interactive ``confirm_delete`` that always
    # declines; once the service returns ``cancelled`` we synthesize a
    # structured ``CONFIRM_REQUIRED`` error below.
    confirm_delete: Callable[[int], bool] = (
        (lambda count: False)
        if plan.json_output
        else (lambda count: _click.confirm(f"Delete {count} source(s)?"))
    )

    suppress_status = plan.json_output or plan.quiet_mode
    cb_candidates = None if suppress_status else on_candidates
    cb_delete_start: Callable[[int], None] | None = (
        None
        if suppress_status
        else lambda count: cli_print(
            f"[dim]Cleaning {count} source(s) (in chunks of 10)...[/dim]",
            ctx=ctx,
        )
    )

    result = await run_source_clean(
        notebook_id=plan.notebook_id,
        dry_run=plan.dry_run,
        yes=plan.yes,
        list_sources=_list_sources,
        delete_source=client.sources.delete,
        confirm_delete=confirm_delete,
        on_candidates=cb_candidates,
        on_delete_start=cb_delete_start,
        classify_sources=classify_sources,
    )

    candidate_payload = candidates_payload(result.candidates)

    if plan.json_output:
        # P1.T2 bug 3: synthesize structured error when --json + no --yes
        # left candidates uncleaned.
        if result.status == "cancelled" and not plan.yes:
            require_yes_in_json(
                action="clean",
                extra={
                    "notebook_id": result.notebook_id,
                    "candidate_count": result.candidate_count,
                    "candidates": candidate_payload,
                },
            )

        payload: dict[str, Any] = {
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
            payload["failures"] = [{"id": sid, "error": err} for sid, err in result.failures]
        json_output_response(payload)
        # P1.T2 bug 8: partial-failure exits non-zero so shell automation
        # (set -e, CI) sees the failure.
        if result.failures:
            exit_with_code(1)
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
        exit_with_code(1)

    cli_print(
        f"[green]Successfully cleaned {result.deleted_count} source(s).[/green]",
        ctx=ctx,
    )
