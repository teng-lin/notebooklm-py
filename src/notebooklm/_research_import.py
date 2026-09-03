"""Backend-neutral helpers for research source import + verification.

Extracted from the research APIs so the ``import_sources`` /
``import_sources_with_verification`` machinery
— URL normalization for import verification, the report-source predicate, the
imported-entry / merge helpers, the #1961 idempotency pre-filter + its
``already_present`` side-channel carrier, and the #2187 batch-scaled read
timeout + retry-time FAILED_PRECONDITION predicate — lives in one cohesive
place without making the neutral base depend on either transport package.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlsplit, urlunsplit

from ._runtime.config import resolve_import_research_read_timeout
from ._types.enums import GrpcStatusCode, normalize_grpc_status
from ._types.research import ResearchSource, ResearchSourceInput
from .exceptions import ResearchTaskMismatchError, RPCError, ValidationError

if TYPE_CHECKING:
    from .types import Source


@dataclass(frozen=True)
class _ResearchImportPolicy:
    """Backend compatibility policy for neutral import classification."""

    validate_canonical_task_id: bool
    require_explicit_report_fields: bool
    reports_first: bool
    log_classification: bool


@dataclass(frozen=True)
class _ResearchImportItem:
    """One transport-neutral source selected for an import mutation."""

    kind: Literal["report", "web"]
    source_input: ResearchSourceInput
    source: ResearchSource


@dataclass(frozen=True)
class _ResearchImportBatch:
    """Validated import entries in the backend's historical wire order."""

    task_id: str
    items: tuple[_ResearchImportItem, ...]
    requested_count: int
    skipped_count: int


_WEB_RESEARCH_IMPORT_POLICY = _ResearchImportPolicy(
    validate_canonical_task_id=False,
    require_explicit_report_fields=True,
    reports_first=True,
    log_classification=True,
)

_ANDROID_RESEARCH_IMPORT_POLICY = _ResearchImportPolicy(
    validate_canonical_task_id=True,
    require_explicit_report_fields=False,
    reports_first=False,
    log_classification=False,
)


def _coerce_research_source(source: ResearchSourceInput) -> ResearchSource:
    """Return the typed research-source model for one public input."""
    if isinstance(source, ResearchSource):
        return source
    return ResearchSource.from_public_dict(source)


def _coerce_research_sources(
    sources: Sequence[ResearchSourceInput],
) -> list[ResearchSource]:
    """Return typed research-source models while preserving input order."""
    return [_coerce_research_source(source) for source in sources]


def _validate_research_task_provenance(
    source_models: Sequence[ResearchSource], task_id: str
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
    otherwise be dropped without ever reaching the backend ``import_sources`` mutation.
    """
    for source in source_models:
        source_task_id = source.research_task_id
        if source_task_id and source_task_id != task_id:
            raise ResearchTaskMismatchError(
                task_id=task_id,
                source_research_task_id=source_task_id,
            )
    research_task_ids = {
        source.research_task_id for source in source_models if source.research_task_id
    }
    if len(research_task_ids) > 1:
        raise ValidationError("Cannot import sources from multiple research tasks in one batch.")
    return next(iter(research_task_ids), task_id)


def _validate_import_task_id(task_id: str, policy: _ResearchImportPolicy) -> str:
    """Apply the backend's pre-classification task-id contract."""
    if not policy.validate_canonical_task_id:
        return task_id
    try:
        parsed = uuid.UUID(task_id)
    except (AttributeError, ValueError):
        raise ValidationError("run_id must be a canonical UUID") from None
    canonical = str(parsed)
    if task_id != canonical:
        raise ValidationError("run_id must be a canonical UUID")
    return canonical


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


def _source_import_verification_url(source: ResearchSource) -> str | None:
    url = source.url
    if not url:
        return None
    return _normalize_import_verification_url(url)


def _requested_import_verification_urls(sources: Sequence[ResearchSource]) -> set[str]:
    return {url for source in sources if (url := _source_import_verification_url(source))}


def _no_import_verification_url_entry_count(sources: Sequence[ResearchSource]) -> int:
    return sum(1 for source in sources if _source_import_verification_url(source) is None)


def _is_importable_report_source(
    source_input: ResearchSourceInput,
    source: ResearchSource,
) -> bool:
    """Preserve the public-dict report predicate from the legacy importer."""
    if not source.is_report or not source.report_markdown:
        return False
    if isinstance(source_input, ResearchSource):
        return isinstance(source.title, str)
    return isinstance(source_input.get("title"), str) and isinstance(
        source_input.get("report_markdown"), str
    )


def _classify_research_import(
    source_inputs: Sequence[ResearchSourceInput],
    source_models: Sequence[ResearchSource],
    *,
    task_id: str,
    policy: _ResearchImportPolicy,
) -> _ResearchImportBatch:
    """Classify usable report/URL entries under one explicit backend policy."""
    items: list[_ResearchImportItem] = []
    for source_input, source in zip(source_inputs, source_models, strict=True):
        is_report = source.is_report and bool(source.report_markdown)
        if policy.require_explicit_report_fields:
            is_report = is_report and _is_importable_report_source(source_input, source)
        if is_report:
            items.append(_ResearchImportItem("report", source_input, source))
        elif source.url:
            items.append(_ResearchImportItem("web", source_input, source))

    if policy.reports_first:
        items.sort(key=lambda item: item.kind != "report")
    return _ResearchImportBatch(
        task_id=task_id,
        items=tuple(items),
        requested_count=len(source_models),
        skipped_count=len(source_models) - len(items),
    )


def _imported_source_entry(source: Source) -> dict[str, str]:
    return {"id": source.id or "", "title": source.title or source.url or ""}


def _already_present_source_entry(source: Source) -> dict[str, str]:
    """Return the historical id/title/URL side-channel row."""
    return {**_imported_source_entry(source), "url": source.url or ""}


def _merge_imported_sources(
    imported: list[dict[str, str]],
    verified_imported: list[dict[str, str]],
    verified_imported_ids: set[str],
) -> list[dict[str, str]]:
    if not verified_imported:
        return imported
    return [
        *verified_imported,
        *(entry for entry in imported if entry.get("id") not in verified_imported_ids),
    ]


class _ImportedResearchSources(list):
    """Newly-imported source entries carrying the already-present ones (#1961).

    ``client.research.import_sources_with_verification`` pre-filters requested
    sources whose (normalized) URL already exists in the notebook so a repeat
    import does not duplicate them. This ``list`` subclass keeps every list
    behavior existing callers rely on (iteration, ``len``, indexing, JSON
    serialization) — the wrapped items ARE the newly-imported entries — while
    exposing the deduped ``already_present`` entries as a side channel for
    callers (the ``_app`` import wrapper) that want an idempotency report.

    The public method's return annotation stays ``list[dict[str, str]]`` on
    purpose: the annotation is what the public-API compat gate inspects, so this
    runtime-only subclass adds the side channel without a return-type break.
    """

    already_present: list[dict[str, str]]

    def __init__(
        self,
        iterable: Sequence[dict[str, str]] = (),
        already_present: Sequence[dict[str, str]] | None = None,
    ) -> None:
        super().__init__(iterable)
        self.already_present = list(already_present or [])


def _imported_result(
    imported: list[dict[str, str]],
    already_present: list[dict[str, str]],
) -> list[dict[str, str]]:
    """Wrap newly-imported entries in the side-channel carrier (#1961).

    Returns a :class:`_ImportedResearchSources` typed as the historical
    ``list[dict[str, str]]`` so callers see no annotation change.
    """
    return _ImportedResearchSources(imported, already_present)


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

    fully_verified_entries: list[dict[str, str]] | None
    newly_verified: list[dict[str, str]] = field(default_factory=list)
    filtered: bool = False
    removed_count: int = 0
    source_inputs: list[ResearchSourceInput] = field(default_factory=list)
    source_models: list[ResearchSource] = field(default_factory=list)
    requested_urls_norm: set[str] = field(default_factory=set)
    requested_no_url_count: int = 0


def _reconcile_import_probe(
    *,
    current: Sequence[Source],
    baseline_ids: set[str] | None,
    requested_urls_norm: set[str],
    requested_no_url_count: int,
    source_inputs: list[ResearchSourceInput],
    source_models: list[ResearchSource],
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
        entries: list[dict[str, str]] = []
        remaining_no_url = requested_no_url_count
        for src in new_sources:
            if src.url and _normalize_import_verification_url(src.url) in requested_urls_norm:
                entries.append(_imported_source_entry(src))
            elif not src.url and remaining_no_url > 0:
                entries.append(_imported_source_entry(src))
                remaining_no_url -= 1
        return _ImportProbeOutcome(fully_verified_entries=entries)

    source_norms = [
        (source_input, source, _source_import_verification_url(source))
        for source_input, source in zip(source_inputs, source_models, strict=True)
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
    filtered_source_pairs = [
        (source_input, source)
        for source_input, source, url in source_norms
        if url not in retry_present_urls and not (drop_no_url_entries and url is None)
    ]

    if len(filtered_source_pairs) == len(source_models):
        return _ImportProbeOutcome(
            fully_verified_entries=None,
            source_inputs=source_inputs,
            source_models=source_models,
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
    filtered_source_inputs = [source_input for source_input, _ in filtered_source_pairs]
    filtered_source_models = [source for _, source in filtered_source_pairs]
    return _ImportProbeOutcome(
        fully_verified_entries=[] if not filtered_source_models else None,
        newly_verified=newly_verified,
        filtered=True,
        removed_count=len(source_models) - len(filtered_source_pairs),
        source_inputs=filtered_source_inputs,
        source_models=filtered_source_models,
        requested_urls_norm=_requested_import_verification_urls(filtered_source_models),
        requested_no_url_count=_no_import_verification_url_entry_count(filtered_source_models),
    )


def _partition_requested_sources(
    source_inputs: list[ResearchSourceInput],
    source_models: list[ResearchSource],
    existing_by_norm_url: dict[str, Source],
) -> tuple[list[ResearchSourceInput], list[ResearchSource], list[dict[str, str]]]:
    """Split requested sources into (new, already-present) by normalized URL.

    Report entries (:func:`_is_importable_report_source`) and any source without
    a dedupable URL are always kept as *new* — reports/pasted text cannot be
    URL-deduped, so they follow existing behavior. Only a non-report source
    whose normalized URL already exists in the notebook is treated as
    already-present.

    Returns ``(new_inputs, new_models, already_present)`` where the parallel
    ``new_*`` lists stay index-aligned and ``already_present`` holds an
    ``{id, title, url}`` entry for the EXISTING notebook source that matched.
    """
    new_inputs: list[ResearchSourceInput] = []
    new_models: list[ResearchSource] = []
    already_present: list[dict[str, str]] = []
    already_present_ids: set[str] = set()
    for source_input, source in zip(source_inputs, source_models, strict=True):
        norm = (
            None
            if _is_importable_report_source(source_input, source)
            else _source_import_verification_url(source)
        )
        existing = existing_by_norm_url.get(norm) if norm is not None else None
        if existing is not None:
            # Skip every matching input, but report each existing source once —
            # a request that repeats the same URL must not inflate the count.
            existing_id = existing.id or ""
            if existing_id not in already_present_ids:
                already_present_ids.add(existing_id)
                already_present.append(_already_present_source_entry(existing))
            continue
        new_inputs.append(source_input)
        new_models.append(source)
    return new_inputs, new_models, already_present


_import_research_read_timeout = resolve_import_research_read_timeout


def _is_import_research_failed_precondition(exc: RPCError) -> bool:
    """True when ``exc`` is IMPORT_RESEARCH's documented retry-time FAILED_PRECONDITION.

    The server rejects an ``IMPORT_RESEARCH`` call against a ``task_id`` whose
    state an earlier attempt against that same id already partially mutated —
    commonly this method's own prior (client-timed-out) call within the same
    retry loop, but not necessarily; documented backend behavior, not a novel
    failure (issue #1926, item F2b). ``import_sources_with_verification``
    shares its post-error ``sources.list`` probe with :class:`RPCTimeoutError`
    for this one specific, well-understood code, but — unlike a timeout —
    only a fully-verified success is accepted; a partial/no match re-raises
    rather than retrying the rejected task_id. Every other ``RPCError``
    propagates immediately without probing.

    This is a pure ``rpc_code`` check with no awareness of which RPC produced
    it — correct today because ``import_sources`` issues exactly one RPC
    inside the guarded ``try``. A future ``import_sources`` change that adds a
    second RPC call there would need this predicate revisited.

    Note: the verification probe (like the pre-existing RPCTimeoutError one it
    shares) confirms a matching source *exists*, not that *this* IMPORT_RESEARCH
    call is what created it — an unrelated concurrent addition of the same URL
    could coincidentally satisfy it. That race is inherent to ID/URL-based
    verification and pre-dates this predicate; it is not made more likely by
    extending verification to cover FAILED_PRECONDITION alongside timeouts.
    """
    return normalize_grpc_status(exc.rpc_code) is GrpcStatusCode.FAILED_PRECONDITION
