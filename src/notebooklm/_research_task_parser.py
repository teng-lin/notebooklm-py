"""POLL_RESEARCH wire-row parsing helpers.

The public typed models (:class:`ResearchSource`, :class:`ResearchTask`,
:class:`ResearchStatus`) live in ``_types/research.py`` (issue #1209); they are
re-exported here so the historical import path
``from ._research_task_parser import ResearchSource, ResearchTask`` keeps
working and this module stays the home of the wire-row parsing logic.
"""

from __future__ import annotations

import logging
from typing import Any

from ._types.research import (
    RESEARCH_RESULT_TYPE_REPORT,
    RESEARCH_RESULT_TYPE_WEB,
    ResearchResultType,
    ResearchSource,
    ResearchStatus,
    ResearchTask,
    parse_result_type,
)
from .rpc import RPCMethod, safe_index

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

logger = logging.getLogger(__name__)

_POLL_SOURCE = "_research.poll"
_POLL_METHOD_ID = RPCMethod.POLL_RESEARCH.value


def extract_legacy_report_chunks(src: list[Any]) -> str:
    """Join legacy deep-research report chunks stored in ``src[6]``."""
    if len(src) <= 6 or not isinstance(src[6], list):
        return ""
    chunks = [chunk for chunk in src[6] if isinstance(chunk, str) and chunk]
    return "\n\n".join(chunks)


def _extract_task_id(task_data: Any) -> str | None:
    """Return ``task_data[0]`` as a string when present, else ``None``."""
    value = safe_index(task_data, 0, method_id=_POLL_METHOD_ID, source=_POLL_SOURCE)
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
    value = safe_index(task_data, 1, method_id=_POLL_METHOD_ID, source=_POLL_SOURCE)
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

    value = query_info[0] if query_info else None
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

    sources_data = bundle[0] if isinstance(bundle[0], list) else []
    if bundle[0] is not None and not isinstance(bundle[0], list):
        logger.warning(
            "task_info[3][0] is not a list (method_id=%r, source=%r): %r",
            _POLL_METHOD_ID,
            _POLL_SOURCE,
            type(bundle[0]).__name__,
        )

    summary: str | None = None
    if len(bundle) >= 2 and isinstance(bundle[1], str):
        summary = bundle[1]

    return sources_data, summary


def _status_from_code(status_code: int | None) -> ResearchStatus:
    # Research: 1=in_progress, 2=completed, 6=completed (deep research).
    # Unknown non-null codes are terminal failures so wait loops do not spin
    # until timeout after the backend rejects a task.
    if status_code in (2, 6):
        return ResearchStatus.COMPLETED
    if status_code == 1 or status_code is None:
        return ResearchStatus.IN_PROGRESS
    return ResearchStatus.FAILED


def _parse_source_row(
    src: Any, *, task_id: str, report_found: bool = False
) -> tuple[ResearchSource | None, str]:
    if not isinstance(src, list) or len(src) < 2:
        return None, ""

    title = ""
    url = ""
    source_report = ""

    # Fast research: [url, title, desc, type, ...]
    # Deep research (legacy): [None, title, None, type, ..., [report_markdown]]
    # Deep research (current): [None, [title, report_markdown], None, type, ...]
    # src[3] is the authoritative result_type when present.
    result_type = parse_result_type(src[3]) if len(src) > 3 else RESEARCH_RESULT_TYPE_WEB
    if src[0] is None and len(src) > 1:
        if (
            isinstance(src[1], list)
            and len(src[1]) >= 2
            and isinstance(src[1][0], str)
            and isinstance(src[1][1], str)
        ):
            title = src[1][0]
            source_report = src[1][1]
            url = ""
            if result_type == RESEARCH_RESULT_TYPE_WEB:
                result_type = RESEARCH_RESULT_TYPE_REPORT
        elif isinstance(src[1], str):
            title = src[1]
            url = ""
            if result_type == RESEARCH_RESULT_TYPE_WEB:
                result_type = RESEARCH_RESULT_TYPE_REPORT
    elif isinstance(src[0], str) or len(src) >= 3:
        url = src[0] if isinstance(src[0], str) else ""
        title = src[1] if len(src) > 1 and isinstance(src[1], str) else ""

    parsed_source = None
    if title or url:
        parsed_source = ResearchSource(
            url=url,
            title=title,
            result_type=result_type,
            research_task_id=task_id,
        )

    report = source_report
    if not report and not report_found:
        report = extract_legacy_report_chunks(src)
    if report and parsed_source is not None:
        parsed_source = parsed_source.with_report_markdown(report)

    return parsed_source, report


def _unwrap_poll_result(result: Any) -> list[Any]:
    if not result or not isinstance(result, list):
        return []
    if isinstance(result[0], list) and len(result[0]) > 0 and isinstance(result[0][0], list):
        return result[0]
    return result


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
