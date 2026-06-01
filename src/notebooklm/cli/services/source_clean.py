"""Service helpers for the ``source clean`` command.

This module owns the pure orchestration of source cleanup (classifying
junk sources, batched deletion, returning a typed
:class:`SourceCleanResult`). Presentation (Rich text vs. JSON envelope),
confirmation prompting, and exit-code policy live in the Click command
layer (:mod:`notebooklm.cli.source_cmd`) — the service never imports
``click``, ``..rendering``, or ``..error_handler``.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlparse, urlunparse

from ...types import Source, source_status_to_str

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
