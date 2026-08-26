"""Free-function helpers for research source import + verification.

Extracted under the ADR-0008 module-size ratchet so the ``import_sources`` /
``import_sources_with_verification`` machinery — URL normalization for import
verification, the provenance check, the imported-entry / merge helpers, the
#1961 idempotency pre-filter, and the #2187 batch-scaled read timeout — lives
in one cohesive place.

Every one of these is backend-neutral policy, so they are consumed by
``_research_service.py`` rather than by the ``_research.py`` facade, and this
module is their only import home. (P6.2 retired the pass-through re-exports the
facade used to carry; reference them here.)

The retry-time ``FAILED_PRECONDITION`` predicate that used to live here reads a
neutral :class:`~notebooklm._backend.BackendStatus` rather than a wire status
code, so it moved to ``_research_service.py`` beside the reason sets it is
branched on with (P10 R6.4). R6.4 also retyped every helper here onto the
neutral record vocabulary — a batch is a tuple of
:class:`~notebooklm._semantic.records.ResearchImportCandidate`, the probe reads
``SourceRecord`` rows, and the results are ``ResearchImportedSourceRecord`` /
``ResearchPresentSourceRecord``. The public-shape concerns those replaced (the
``ResearchSource | Mapping`` union, the report predicate that inspects it, and
the ``list[dict[str, str]]`` side-channel carrier) belong to ``_research.py``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

from ._runtime.config import (
    AUTO_READ_TIMEOUT,
    DEFAULT_IMPORT_RESEARCH_BASE_TIMEOUT,
    DEFAULT_IMPORT_RESEARCH_MAX_TIMEOUT,
    DEFAULT_IMPORT_RESEARCH_PER_SOURCE_TIMEOUT,
    DEFAULT_TIMEOUT,
    compose_builtin_read_timeout,
)
from ._semantic.records import (
    ResearchImportCandidate,
    ResearchImportedSourceRecord,
    ResearchPresentSourceRecord,
    ResearchSourceRecord,
    SourceRecord,
)
from .exceptions import ResearchTaskMismatchError, ValidationError


def _validate_research_task_provenance(
    candidates: Sequence[ResearchImportCandidate], task_id: str
) -> str:
    """Validate per-source research-task provenance; return the effective task id.

    Each source's ``research_task_id`` (when present) must match ``task_id`` — a
    mismatch is the wire-crossing bug (importing under the wrong task
    mis-attributes provenance), so it raises :class:`ResearchTaskMismatchError`.
    A batch spanning more than one task id is refused with a
    :class:`ValidationError`. Returns the id to import under: ``task_id`` unless
    every pinned source agrees on one shared id.

    Runs BEFORE the #1961 idempotency pre-filter (see
    :func:`_partition_requested_sources`) so a mismatched-provenance source is
    rejected even when its URL is already present in the notebook and would
    otherwise be dropped without ever reaching :meth:`ResearchAPI.import_sources`.
    """
    for candidate in candidates:
        source_task_id = candidate.source.research_task_id
        if source_task_id and source_task_id != task_id:
            raise ResearchTaskMismatchError(
                task_id=task_id,
                source_research_task_id=source_task_id,
            )
    research_task_ids = {
        candidate.source.research_task_id
        for candidate in candidates
        if candidate.source.research_task_id
    }
    if len(research_task_ids) > 1:
        raise ValidationError("Cannot import sources from multiple research tasks in one batch.")
    return next(iter(research_task_ids), task_id)


def _normalize_import_verification_url(url: str) -> str:
    """Lowercase scheme + host and strip a trailing slash for comparison.

    Distinct from ``notebooklm.research.normalize_citation_url`` (used for
    matching URLs cited inside report markdown): this variant drops the URL
    fragment because the server stores fragments stripped, and skips the
    trailing-punctuation strip because these URLs come from a structured
    ``sources.list`` payload rather than free-form markdown.
    """
    parsed = urlsplit(url)
    return urlunsplit(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path.rstrip("/"),
            parsed.query,
            "",
        )
    )


def _source_import_verification_url(source: ResearchSourceRecord) -> str | None:
    url = source.url
    if not url:
        return None
    return _normalize_import_verification_url(url)


def _candidate_import_verification_url(candidate: ResearchImportCandidate) -> str | None:
    return _source_import_verification_url(candidate.source)


def _requested_import_verification_urls(
    candidates: Sequence[ResearchImportCandidate],
) -> set[str]:
    return {url for c in candidates if (url := _candidate_import_verification_url(c))}


def _no_import_verification_url_entry_count(
    candidates: Sequence[ResearchImportCandidate],
) -> int:
    return sum(1 for c in candidates if _candidate_import_verification_url(c) is None)


def _imported_source_entry(source: SourceRecord) -> ResearchImportedSourceRecord:
    return ResearchImportedSourceRecord(id=source.id or "", title=source.title or source.url or "")


def _merge_imported_sources(
    imported: Sequence[ResearchImportedSourceRecord],
    verified_imported: Sequence[ResearchImportedSourceRecord],
    verified_imported_ids: set[str],
) -> tuple[ResearchImportedSourceRecord, ...]:
    if not verified_imported:
        return tuple(imported)
    return (
        *verified_imported,
        *(entry for entry in imported if entry.id not in verified_imported_ids),
    )


@dataclass
class _ImportProbeOutcome:
    """Result of reconciling ``sources.list`` against a failed IMPORT_RESEARCH call.

    ``fully_verified_entries`` is ``None`` unless every outstanding requested
    source (URL or no-URL) is confirmed present — a non-``None`` value
    (including an empty list) means the caller should return success. The
    remaining fields describe the retry-continuation state: whether anything
    was filtered, and the (possibly unchanged) inputs to retry with.

    When the pre-import baseline snapshot failed (``baseline_ids is None``),
    "confirmed present" widens from "present among *new* sources" to "present
    among *all current* sources" for the filtered-to-empty path — i.e. every
    remaining requested URL merely already existing in the notebook (not
    necessarily committed by this call) counts as verified. This mirrors the
    pre-existing RPCTimeoutError behavior for a failed snapshot; it is not new
    to the FAILED_PRECONDITION case.
    """

    fully_verified_entries: list[ResearchImportedSourceRecord] | None
    newly_verified: list[ResearchImportedSourceRecord] = field(default_factory=list)
    filtered: bool = False
    removed_count: int = 0
    candidates: list[ResearchImportCandidate] = field(default_factory=list)
    requested_urls_norm: set[str] = field(default_factory=set)
    requested_no_url_count: int = 0


def _reconcile_import_probe(
    *,
    current: Sequence[SourceRecord],
    baseline_ids: set[str] | None,
    requested_urls_norm: set[str],
    requested_no_url_count: int,
    candidates: list[ResearchImportCandidate],
    already_verified_ids: set[str],
    allow_duplicate: bool,
) -> _ImportProbeOutcome:
    """Reconcile a post-failure ``sources.list`` snapshot against what was requested.

    Pure computation extracted from ``import_sources_with_verification``'s
    retry loop (ADR-0008 module-size ratchet) — no I/O, no logging, no
    exception-type awareness. The caller (which knows whether the triggering
    exception was ``RPCTimeoutError`` or the #2187 FAILED_PRECONDITION case)
    decides what to log and whether a non-fully-verified outcome is safe to
    retry from.
    """
    new_sources = (
        [src for src in current if src.id not in baseline_ids] if baseline_ids is not None else []
    )
    new_urls_norm = {_normalize_import_verification_url(src.url) for src in new_sources if src.url}
    current_urls_norm = {_normalize_import_verification_url(src.url) for src in current if src.url}
    committed_urls_norm = requested_urls_norm & new_urls_norm

    # Every URL landing is treated as the whole batch verified — including any
    # no-URL (report) entries — on the assumption reports commit first
    # server-side (see ``drop_no_url_entries`` below), so a URL landing implies
    # the report did too. Pre-existing assumption, unchanged by #2187; noted
    # here since this path now also resolves FAILED_PRECONDITION, not just
    # RPCTimeoutError (claude review).
    if baseline_ids is not None and requested_urls_norm.issubset(new_urls_norm):
        entries: list[ResearchImportedSourceRecord] = []
        remaining_no_url = requested_no_url_count
        for src in new_sources:
            if src.url and _normalize_import_verification_url(src.url) in requested_urls_norm:
                entries.append(_imported_source_entry(src))
            elif not src.url and remaining_no_url > 0:
                entries.append(_imported_source_entry(src))
                remaining_no_url -= 1
        return _ImportProbeOutcome(fully_verified_entries=entries)

    candidate_norms = [
        (candidate, _candidate_import_verification_url(candidate)) for candidate in candidates
    ]
    # Filter for retry: drop already-present URLs. Also, when *any* URL
    # committed, drop no-URL entries (deep-research reports are appended
    # FIRST in the IMPORT_RESEARCH payload, so a newly-observed URL implies
    # the report committed too — without this guard each retry duplicates it
    # server-side). Pre-existing URLs only de-dupe URL entries. When no URL
    # committed, keep no-URL entries (report fate unknown; the caller's
    # report-only attempt cap bounds the worst case).
    drop_no_url_entries = bool(committed_urls_norm)
    # Drop-for-retry anchor: normally a URL already present in the notebook
    # (baseline OR committed by this call) is dropped to avoid duplicate
    # inflation. But under ``allow_duplicate`` the caller explicitly opted to
    # re-add baseline URLs, so anchor on the post-baseline ``new_urls_norm`` —
    # only URLs THIS attempt committed are dropped; a pre-existing baseline
    # URL is still retried and re-added, not silently treated as "already
    # done" (#1961 codex review). #1934 safety holds in both modes: a URL
    # committed by this attempt is never retried.
    retry_present_urls = new_urls_norm if allow_duplicate else current_urls_norm
    filtered_candidates = [
        candidate
        for candidate, url in candidate_norms
        if url not in retry_present_urls and not (drop_no_url_entries and url is None)
    ]

    if len(filtered_candidates) == len(candidates):
        return _ImportProbeOutcome(
            fully_verified_entries=None,
            candidates=candidates,
            requested_urls_norm=requested_urls_norm,
            requested_no_url_count=requested_no_url_count,
        )

    newly_verified = [
        _imported_source_entry(src)
        for src in new_sources
        if src.url
        and _normalize_import_verification_url(src.url) in committed_urls_norm
        and src.id not in already_verified_ids
    ]
    return _ImportProbeOutcome(
        fully_verified_entries=[] if not filtered_candidates else None,
        newly_verified=newly_verified,
        filtered=True,
        removed_count=len(candidates) - len(filtered_candidates),
        candidates=filtered_candidates,
        requested_urls_norm=_requested_import_verification_urls(filtered_candidates),
        requested_no_url_count=_no_import_verification_url_entry_count(filtered_candidates),
    )


def _partition_requested_sources(
    candidates: list[ResearchImportCandidate],
    existing_by_norm_url: dict[str, SourceRecord],
) -> tuple[list[ResearchImportCandidate], list[ResearchPresentSourceRecord]]:
    """Split requested candidates into (new, already-present) by normalized URL.

    Report candidates (``candidate.report``, the facade's verdict on the public
    report predicate) and any source without a dedupable URL are always kept as
    *new* — reports/pasted text cannot be URL-deduped, so they follow existing
    behavior. Only a non-report source whose normalized URL already exists in
    the notebook is treated as already-present.

    Returns ``(new_candidates, already_present)``, where ``already_present``
    describes the EXISTING notebook source that matched.
    """
    new_candidates: list[ResearchImportCandidate] = []
    already_present: list[ResearchPresentSourceRecord] = []
    already_present_ids: set[str] = set()
    for candidate in candidates:
        norm = None if candidate.report else _candidate_import_verification_url(candidate)
        existing = existing_by_norm_url.get(norm) if norm is not None else None
        if existing is not None:
            # Skip every matching input, but report each existing source once —
            # a request that repeats the same URL must not inflate the count.
            existing_id = existing.id or ""
            if existing_id not in already_present_ids:
                already_present_ids.add(existing_id)
                already_present.append(
                    ResearchPresentSourceRecord(
                        id=existing_id,
                        title=existing.title or existing.url or "",
                        url=existing.url or "",
                    )
                )
            continue
        new_candidates.append(candidate)
    return new_candidates, already_present


def _import_research_read_timeout(
    source_count: int,
    *,
    base_timeout: float | None = DEFAULT_TIMEOUT,
    override: float | None = AUTO_READ_TIMEOUT,
    remaining_budget: float | None = None,
) -> float | None:
    """Resolve IMPORT_RESEARCH's per-attempt read timeout.

    Batch scaling (#2187): the server ingests every entry (fetch/parse/embed)
    before responding to one RPC, so a large deep-research batch needs
    materially more time than a 3-source fast-research one. The base term is a
    floor so a tiny import still fails fast on a genuinely broken call; the max
    is a ceiling so a pathologically large batch is still bounded rather than
    open-ended.

    Composition (#2205): the scaled window is a *default*, so it is floored at
    the client's configured ``base_timeout`` — a caller who bought
    ``timeout=600`` keeps 600 s here instead of being silently capped at 240 s.

    ``override`` is the ``import_research_timeout`` constructor kwarg and reads
    exactly like ``chat_timeout`` does, so the two knobs are one rule:

    * :data:`AUTO_READ_TIMEOUT` (unset) — batch-scaled, floored at ``base_timeout``;
    * a number — the caller's word, replacing both the scaling and the floor;
    * ``None`` — inherit ``base_timeout`` verbatim (no per-RPC override).

    Retry-budget clamp (#2205): ``remaining_budget`` is what is left of
    ``import_sources_with_verification``'s ``max_elapsed`` when this attempt
    starts, so a late retry cannot be *granted* a window larger than the budget
    it has left — without it, a retry starting 50 s from the deadline still got
    the full batch-scaled (or configured) window. It is applied last, so it
    also bounds an ``override`` and an inherited (``None``) window. That caller
    only passes a budget it has already found viable — see
    ``MIN_IMPORT_RESEARCH_ATTEMPT_TIMEOUT``, which is what keeps this from
    producing a uselessly small window.

    What this is *not*: a wall-clock deadline for the attempt. Every timeout in
    this client is an ``httpx`` slot, and ``read`` is an inactivity limit
    between socket reads — connect/pool waits sit outside it, and a server that
    keeps dribbling bytes just inside the window keeps the request alive. So
    the clamp bounds what an attempt is *given*, not how long it can take.
    Enforcing the latter would mean cancelling an in-flight
    ``NON_IDEMPOTENT_NO_RETRY`` POST, trading a bounded overshoot for an
    unbounded duplicate-source risk (#808).
    """
    window: float | None
    if override is AUTO_READ_TIMEOUT:
        window = compose_builtin_read_timeout(
            min(
                DEFAULT_IMPORT_RESEARCH_MAX_TIMEOUT,
                DEFAULT_IMPORT_RESEARCH_BASE_TIMEOUT
                + DEFAULT_IMPORT_RESEARCH_PER_SOURCE_TIMEOUT * source_count,
            ),
            base_timeout,
        )
    else:
        window = override
    if remaining_budget is None:
        return window
    return remaining_budget if window is None else min(window, remaining_budget)
