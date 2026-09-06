"""Transport-neutral ``source clean`` business logic.

This is the Click-free core behind ``source clean`` (imported directly by the
``cli/source_cmd.py`` / ``cli/_source_render.py`` command layer): it owns the
pure orchestration of source cleanup (classifying junk sources, batched
deletion, returning a typed :class:`SourceCleanResult`). Presentation (Rich
text vs. JSON envelope), confirmation prompting, and exit-code policy live in
the Click command layer (:mod:`notebooklm.cli.source_cmd`).

This module is transport-neutral — no ``click`` / ``rich`` / ``cli`` /
``fastmcp`` imports (enforced by ``tests/_guardrails/test_app_boundary.py``).
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlparse, urlunparse

from ..types import Source, source_status_to_str

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


@dataclass(frozen=True)
class SourceCleanPreview:
    """Immutable cleanup target set prepared before adapter authorization."""

    notebook_id: str
    dry_run: bool
    candidates: tuple[CleanCandidate, ...]


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


async def prepare_source_clean(
    *,
    notebook_id: str,
    dry_run: bool,
    list_sources: Callable[[str], Awaitable[list[Source]]],
    classify_sources: Callable[[list[Source]], list[CleanCandidate]] = classify_junk_sources,
) -> SourceCleanPreview:
    """Classify junk sources without deleting or consulting presentation policy."""
    sources = await list_sources(notebook_id)
    candidates = classify_sources(sources)
    return SourceCleanPreview(notebook_id, dry_run, tuple(candidates))


def skip_source_clean(preview: SourceCleanPreview, *, cancelled: bool = False) -> SourceCleanResult:
    """Project a prepared no-op, dry run, or adapter-declined cleanup."""
    if not preview.candidates:
        return SourceCleanResult(
            notebook_id=preview.notebook_id,
            status="already_clean",
            candidates=(),
        )
    if preview.dry_run:
        return SourceCleanResult(
            notebook_id=preview.notebook_id,
            status="dry_run",
            candidates=preview.candidates,
        )
    if cancelled:
        return SourceCleanResult(
            notebook_id=preview.notebook_id,
            status="cancelled",
            candidates=preview.candidates,
        )
    raise ValueError("non-empty cleanup preview requires execution or cancellation")


async def execute_source_clean(
    preview: SourceCleanPreview,
    *,
    delete_source: Callable[[str, str], Awaitable[object]],
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> SourceCleanResult:
    """Delete the exact immutable target set an adapter already authorized."""
    if preview.dry_run or not preview.candidates:
        return skip_source_clean(preview)

    delete_list = [candidate[0] for candidate in preview.candidates]
    chunk_size = 10
    deleted = 0
    failures: list[CleanFailure] = []
    for i in range(0, len(delete_list), chunk_size):
        chunk = delete_list[i : i + chunk_size]
        delete_tasks = [delete_source(preview.notebook_id, sid) for sid in chunk]
        results = await asyncio.gather(*delete_tasks, return_exceptions=True)
        for sid, result in zip(chunk, results, strict=True):
            # ``return_exceptions=True`` also captures non-``Exception``
            # ``BaseException``s (``CancelledError`` / ``KeyboardInterrupt`` /
            # ``SystemExit``). Never count those as a successful delete — re-raise
            # so cancellation/interrupts propagate instead of being swallowed.
            if isinstance(result, BaseException) and not isinstance(result, Exception):
                raise result
            if isinstance(result, Exception):
                failures.append((sid, str(result)))
            else:
                deleted += 1
        if i + chunk_size < len(delete_list):
            await sleep(0.5)

    return SourceCleanResult(
        notebook_id=preview.notebook_id,
        status="completed",
        candidates=preview.candidates,
        deleted_count=deleted,
        failures=tuple(failures),
    )


__all__ = [
    "CleanCandidate",
    "CleanFailure",
    "CleanStatus",
    "SourceCleanResult",
    "SourceCleanPreview",
    "candidates_payload",
    "classify_junk_sources",
    "normalize_url_for_dedup",
    "execute_source_clean",
    "prepare_source_clean",
    "skip_source_clean",
]
