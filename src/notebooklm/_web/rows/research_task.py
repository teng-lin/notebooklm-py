"""POLL_RESEARCH wire-row parsing helpers.

The public typed models (:class:`ResearchSource`, :class:`ResearchTask`,
:class:`ResearchStatus`) live in ``_types/research.py`` (issue #1209); they are
re-exported here as this module succeeds the historical
``_research_task_parser`` implementation and stays the home of the wire-row
parsing logic.
"""

from __future__ import annotations

import logging
from typing import Any

from ..._types.enums import DiscoveryMode
from ..._types.research import (
    RESEARCH_RESULT_TYPE_REPORT,
    RESEARCH_RESULT_TYPE_WEB,
    RESEARCH_SOURCE_TYPE_WEB,
    RESEARCH_STATUS_CODE_COMPLETED,
    RESEARCH_STATUS_CODE_NO_RESULTS,
    ResearchResultType,
    ResearchSource,
    ResearchStatus,
    ResearchTask,
    parse_result_type,
    status_from_termination_reason,
    termination_reason_from_code,
)
from ...exceptions import DecodingError
from ...rpc import RPCMethod, safe_index
from .research import (
    ResearchResultRow,
    ResearchTaskInfoRow,
    ResearchTaskRow,
    unwrap_poll_tasks,
)

__all__ = [
    "RESEARCH_RESULT_TYPE_REPORT",
    "RESEARCH_RESULT_TYPE_WEB",
    "ResearchResultType",
    "ResearchSource",
    "ResearchStatus",
    "ResearchTask",
    "parse_research_task_models",
    "parse_research_tasks",
    "parse_result_type",
]

logger = logging.getLogger("notebooklm._research_task_parser")

_POLL_SOURCE = "_research.poll"
_POLL_METHOD_ID = RPCMethod.POLL_RESEARCH.value


def extract_report_markdown(src: list[Any]) -> str:
    """Return markdown from the kind-3 content block stored in ``src[6]``."""
    return ResearchResultRow(src).report_markdown


def _extract_task_id(task_data: Any) -> str | None:
    """Return ``task_data[0]`` as a string when present, else ``None``."""
    value = ResearchTaskRow(task_data).task_id_raw
    if isinstance(value, str):
        return value
    if value is not None:
        logger.warning(
            "task_data[0] is not a string (method_id=%r, source=%r): %r",
            _POLL_METHOD_ID,
            _POLL_SOURCE,
            type(value).__name__,
        )
    return None


def _extract_task_info(task_data: Any) -> list[Any] | None:
    """Return ``task_data[1]`` as a list when present, else ``None``."""
    value = ResearchTaskRow(task_data).task_info_raw
    if isinstance(value, list):
        return value
    if value is not None:
        logger.warning(
            "task_data[1] is not a list (method_id=%r, source=%r): %r",
            _POLL_METHOD_ID,
            _POLL_SOURCE,
            type(value).__name__,
        )
    return None


def _extract_query_text(task_info: Any) -> str | None:
    """Return ``task_info[1][0]`` as the original query text, else ``None``."""
    query_info = safe_index(task_info, 1, method_id=_POLL_METHOD_ID, source=_POLL_SOURCE)
    if not isinstance(query_info, list):
        if query_info is not None:
            logger.warning(
                "task_info[1] is not a list (method_id=%r, source=%r): %r",
                _POLL_METHOD_ID,
                _POLL_SOURCE,
                type(query_info).__name__,
            )
        return None

    value = ResearchTaskInfoRow.query_text(query_info)
    if isinstance(value, str):
        return value
    if value is not None:
        logger.warning(
            "task_info[1][0] is not a string (method_id=%r, source=%r): %r",
            _POLL_METHOD_ID,
            _POLL_SOURCE,
            type(value).__name__,
        )
    return None


def _extract_source_type(task_info: Any) -> int | None:
    """Return the search-source tag at ``task_info[1][1]`` (1=web, 2=drive), else ``None``.

    Read for issue #1964 so a terminal run can carry source-specific
    remediation guidance. The TAG is purely advisory: absent, non-int, or
    drifted, it degrades to ``None`` and the hint falls back to its
    source-agnostic wording.

    The enclosing ``task_info[1]`` block is not advisory, though — it is the
    same guaranteed descent :func:`_extract_query_text` makes, so an absent
    slot raises ``UnknownRPCMethodError`` from :func:`safe_index` exactly as it
    does there. In the parse loop that is unreachable in practice, since
    ``_extract_query_text`` runs first on the same ``task_info`` and raises for
    the identical input.
    """
    query_info = safe_index(task_info, 1, method_id=_POLL_METHOD_ID, source=_POLL_SOURCE)
    if not isinstance(query_info, list):
        return None
    value = ResearchTaskInfoRow.query_source_type(query_info)
    # ``bool`` is an ``int`` subclass; reject it so a drifted flag slot cannot
    # masquerade as the web (1) / drive (2) tag.
    if isinstance(value, bool) or not isinstance(value, int):
        if value is not None:
            logger.warning(
                "task_info[1][1] is not an int source tag (method_id=%r, source=%r): %r",
                _POLL_METHOD_ID,
                _POLL_SOURCE,
                type(value).__name__,
            )
        return None
    return value


def _extract_status_code(task_info: Any) -> int | None:
    """Return ``task_info[4]`` as an int status code, else ``None``."""
    value = safe_index(task_info, 4, method_id=_POLL_METHOD_ID, source=_POLL_SOURCE)
    if isinstance(value, bool):
        # bool is a subclass of int; reject explicitly so callers don't get
        # surprising truthy comparisons against status codes 1/2/6.
        logger.warning(
            "task_info[4] is bool, not int (method_id=%r, source=%r)",
            _POLL_METHOD_ID,
            _POLL_SOURCE,
        )
        return None
    if isinstance(value, int):
        return value
    if value is not None:
        logger.warning(
            "task_info[4] is not an int (method_id=%r, source=%r): %r",
            _POLL_METHOD_ID,
            _POLL_SOURCE,
            type(value).__name__,
        )
    return None


def _extract_sources_and_summary(task_info: Any) -> tuple[list[Any], str | None]:
    """Return ``(sources_data, summary)`` from ``task_info[3]``."""
    bundle = safe_index(task_info, 3, method_id=_POLL_METHOD_ID, source=_POLL_SOURCE)
    if not isinstance(bundle, list) or not bundle:
        if bundle is not None and not isinstance(bundle, list):
            logger.warning(
                "task_info[3] is not a list (method_id=%r, source=%r): %r",
                _POLL_METHOD_ID,
                _POLL_SOURCE,
                type(bundle).__name__,
            )
        return [], None

    raw_sources = ResearchTaskInfoRow.bundle_sources(bundle)
    sources_data = raw_sources if isinstance(raw_sources, list) else []
    if raw_sources is not None and not isinstance(raw_sources, list):
        logger.warning(
            "task_info[3][0] is not a list (method_id=%r, source=%r): %r",
            _POLL_METHOD_ID,
            _POLL_SOURCE,
            type(raw_sources).__name__,
        )

    raw_summary = ResearchTaskInfoRow.bundle_summary(bundle)
    summary: str | None = raw_summary if isinstance(raw_summary, str) else None

    return sources_data, summary


def _status_from_code(status_code: int | None) -> ResearchStatus:
    """Coarsen a raw ``task_info[4]`` code into the lifecycle status.

    Derived from the SAME code table that produces the termination reason
    (``_types/research.py``) rather than restating it in literals: a second
    hand-written copy could drift and emit a task whose ``status`` and
    ``termination_reason`` disagree (e.g. ``completed`` alongside a "retry the
    run" hint). Behavior is unchanged — 1 → in_progress, 2/6 → completed,
    every other non-null code → failed, ``None`` → in_progress.
    """
    return status_from_termination_reason(termination_reason_from_code(status_code))


def _parse_source_row(
    src: Any, *, task_id: str, report_found: bool = False
) -> tuple[ResearchSource | None, str]:
    row = ResearchResultRow(src)
    if not row.is_well_formed:
        return None, ""

    title = ""
    url = ""
    source_report = ""

    # Fast research: [url, title, desc, type, ...]
    # Deep research (captured): [None, title, None, type, ..., content_block]
    # Deep research (compat): [None, [title, report_markdown], None, type, ...]
    # src[3] is the authoritative result_type when present.
    result_type = (
        parse_result_type(row.result_type_slot) if row.has_result_type else RESEARCH_RESULT_TYPE_WEB
    )
    if row.url_slot is None and row.length > 1:
        # A compatibility shape packs ``[title, report_markdown]`` at ``src[1]``;
        # ``ResearchResultRow.deep_payload`` unpacks that exact shape (a 2+-length
        # list of two strings) and returns ``None`` for the legitimate
        # alternatives (bare-string title, or neither), which fall through to the
        # elif / outer branches below.
        payload = row.title_slot
        deep = ResearchResultRow.deep_payload(payload)
        if deep is not None:
            title, source_report = deep
            url = ""
            if result_type == RESEARCH_RESULT_TYPE_WEB:
                result_type = RESEARCH_RESULT_TYPE_REPORT
        elif isinstance(payload, str):
            title = payload
            url = ""
            if result_type == RESEARCH_RESULT_TYPE_WEB:
                result_type = RESEARCH_RESULT_TYPE_REPORT
    elif isinstance(row.url_slot, str) or row.length >= 3:
        url = row.url_slot if isinstance(row.url_slot, str) else ""
        title = row.title_slot if row.length > 1 and isinstance(row.title_slot, str) else ""

    parsed_source = None
    if title or url:
        parsed_source = ResearchSource(
            url=url,
            title=title,
            result_type=result_type,
            research_task_id=task_id,
            source_ordinal=row.source_ordinal,
            hint=row.hint,
        )

    report = source_report
    if not report and not report_found:
        report = extract_report_markdown(src)
    if report and parsed_source is not None:
        parsed_source = parsed_source.with_report_markdown(report)

    return parsed_source, report


def _unwrap_poll_result(result: Any) -> list[Any]:
    # POLL_RESEARCH returns either a wrapped envelope (``[[task1, ...]]``) or an
    # already-flat list of tasks; ``unwrap_poll_tasks`` centralises that envelope
    # probe (the former ``result[0]`` / ``first[0]`` reads) behind the research
    # row adapter.
    return unwrap_poll_tasks(result)


def parse_research_task_models(result: Any) -> list[ResearchTask]:
    """Parse a raw ``POLL_RESEARCH`` result into typed task models."""
    parsed_tasks: list[ResearchTask] = []
    for task_data in _unwrap_poll_result(result):
        if not isinstance(task_data, list):
            continue

        task_id = _extract_task_id(task_data)
        task_info = _extract_task_info(task_data)
        if task_id is None or task_info is None:
            continue

        query_text = _extract_query_text(task_info) or ""
        sources_data, summary_opt = _extract_sources_and_summary(task_info)
        status_code = _extract_status_code(task_info)
        source_type = _extract_source_type(task_info)
        discovery_mode = ResearchTaskInfoRow.discovery_mode(task_info)
        task_row = ResearchTaskRow(task_data)

        parsed_sources: list[ResearchSource] = []
        report = ""
        for src in sources_data:
            parsed_source, source_report = _parse_source_row(
                src, task_id=task_id, report_found=bool(report)
            )
            if parsed_source is not None:
                parsed_sources.append(parsed_source)
            if not report and source_report:
                report = source_report

        parsed_tasks.append(
            ResearchTask(
                task_id=task_id,
                status=_status_from_code(status_code),
                query=query_text,
                sources=tuple(parsed_sources),
                summary=summary_opt or "",
                report=report,
                # Preserve the raw ``task_info[4]`` integer alongside the coarsened
                # ``status`` enum (issue #1922, F10) so a caller can distinguish
                # failure sub-codes the enum flattens into ``FAILED``.
                status_code=status_code,
                # Search source (1=web, 2=drive) backing the source-specific
                # remediation hint on a terminal run (issue #1964).
                source_type=source_type,
                # Always-populated task metadata recovered by #2122: the mode
                # the run is executing under, its create/update times, and the
                # account it belongs to. Every one of these is a soft read —
                # a poll that omits them still yields a usable task.
                discovery_mode=discovery_mode,
                created_at=task_row.created_at,
                updated_at=task_row.updated_at,
                account_id=task_row.account_id,
            )
        )

    return parsed_tasks


def parse_research_tasks(result: Any) -> list[dict[str, Any]]:
    """Parse a raw ``POLL_RESEARCH`` result into compatibility dictionaries.

    Each dict has the historical per-task shape (``task_id`` / ``status`` /
    ``query`` / ``sources`` / ``summary`` / ``report``); the top-level
    ``tasks`` sibling key belongs to :meth:`ResearchAPI.poll`'s result, not to
    these individual task dicts.
    """
    return [task._to_task_dict() for task in parse_research_task_models(result)]


_DISCOVER_METHOD_ID = RPCMethod.DISCOVER_SOURCES.value
_DISCOVER_SOURCE = "DiscoverSourcesResponse"


def parse_discover_task(
    result: Any,
    *,
    query: str = "",
    discovery_mode: DiscoveryMode | None = None,
) -> ResearchTask:
    """Build the completed :class:`ResearchTask` a ``DISCOVER_SOURCES`` call returns.

    The synchronous response is ``[sources, overview, [job_id]]`` — the same
    ``[sources, summary]`` bundle a ``POLL_RESEARCH`` task row carries at
    ``task_info[3]``, plus the ``DiscoverSourcesFeedbackKey`` whose first slot
    is the id of the completed job the call also recorded (live-verified on
    150 rows, #2283). Source rows reuse the poll row parser so both routes
    decode identically. The job id is required: without it the result could
    not be imported or cancelled, so its absence raises
    :class:`~notebooklm.exceptions.DecodingError`.
    """
    if not isinstance(result, list):
        raise DecodingError(
            f"DISCOVER_SOURCES returned a non-list payload: {type(result).__name__}",
            method_id=_DISCOVER_METHOD_ID,
        )
    # Every slot is optional on the wire (proto3 omits empties), so read them
    # by length rather than ``safe_index``: a short payload must surface as the
    # domain error below ("no job id"), not as a generic index failure.
    feedback_key = result[2] if len(result) > 2 else None
    task_id = feedback_key[0] if isinstance(feedback_key, list) and feedback_key else None
    if not isinstance(task_id, str) or not task_id:
        raise DecodingError(
            "DISCOVER_SOURCES response omitted its job id (feedback key [2][0])",
            method_id=_DISCOVER_METHOD_ID,
        )
    raw_sources = result[0] if result else None
    if raw_sources is not None and not isinstance(raw_sources, list):
        logger.warning(
            "DISCOVER_SOURCES [0] is not a list (method_id=%r, source=%r): %r",
            _DISCOVER_METHOD_ID,
            _DISCOVER_SOURCE,
            type(raw_sources).__name__,
        )
    sources: list[ResearchSource] = []
    for src in raw_sources if isinstance(raw_sources, list) else []:
        parsed_source, _report = _parse_source_row(src, task_id=task_id, report_found=True)
        if parsed_source is not None:
            sources.append(parsed_source)
    raw_overview = result[1] if len(result) > 1 else None
    # Slot 4 is the web-only in-band error enum (1 "trouble fetching",
    # 2 unsupported file type, 3 few results, 4 notebook already holds every
    # result, 5 query too long). Live: the Drive corpus answers
    # ``[null, null, null, null, 1]`` and the recorded job lands at status 3,
    # so a non-zero code is the poll path's NO_RESULTS failure, not a success.
    error_code = result[4] if len(result) > 4 else None
    # ``type(...) is int`` so a stray boolean is not read as a code.
    failed = type(error_code) is int and error_code != 0
    return ResearchTask(
        task_id=task_id,
        status=ResearchStatus.FAILED if failed else ResearchStatus.COMPLETED,
        query=query,
        sources=tuple(sources),
        summary=raw_overview if isinstance(raw_overview, str) else "",
        status_code=RESEARCH_STATUS_CODE_NO_RESULTS if failed else RESEARCH_STATUS_CODE_COMPLETED,
        source_type=RESEARCH_SOURCE_TYPE_WEB,
        discovery_mode=discovery_mode,
    )
