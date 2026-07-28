"""Free-function helpers for research source import + verification.

Extracted from ``_research.py`` (ADR-0008 module-size ratchet) so the
``ResearchAPI.import_sources`` / ``import_sources_with_verification`` machinery
— URL normalization for import verification, the report-source predicate, the
imported-entry / merge helpers, and the #1961 idempotency pre-filter + its
``already_present`` side-channel carrier — lives in one cohesive place. These
are re-imported by ``_research.py`` and remain reachable as
``notebooklm._research.<name>`` for callers/tests that reference them there.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING
from urllib.parse import urlsplit, urlunsplit

from ._types.research import ResearchSource, ResearchSourceInput
from .exceptions import ResearchTaskMismatchError, ValidationError

if TYPE_CHECKING:
    from .types import Source


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
    otherwise be dropped without ever reaching :meth:`ResearchAPI.import_sources`.
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


def _imported_source_entry(source: Source) -> dict[str, str]:
    return {"id": source.id or "", "title": source.title or source.url or ""}


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

    :meth:`ResearchAPI.import_sources_with_verification` pre-filters requested
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
                already_present.append(
                    {
                        "id": existing_id,
                        "title": existing.title or existing.url or "",
                        "url": existing.url or "",
                    }
                )
            continue
        new_inputs.append(source_input)
        new_models.append(source)
    return new_inputs, new_models, already_present
