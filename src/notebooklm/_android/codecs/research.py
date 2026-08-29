"""Decode evidence-qualified Android Research protobufs."""

from __future__ import annotations

from datetime import timezone
from typing import Any

from ..._types.enums import DiscoveryMode
from ..._types.research import (
    RESEARCH_RESULT_TYPE_DRIVE,
    RESEARCH_RESULT_TYPE_REPORT,
    RESEARCH_RESULT_TYPE_WEB,
    ResearchSource,
    ResearchTask,
    status_from_termination_reason,
    termination_reason_from_code,
)
from ...exceptions import DecodingError

_REPORT_CONTENT_KIND = 3


def _timestamp(message: Any, field: str) -> Any:
    if not message.HasField(field):
        return None
    return getattr(message, field).ToDatetime(tzinfo=timezone.utc)


def _mode(value: int) -> DiscoveryMode | None:
    if value == 0:
        return None
    try:
        return DiscoveryMode(value)
    except ValueError:
        return DiscoveryMode.UNKNOWN


def decode_discovered_source(
    row: Any,
    *,
    task_id: str,
    source_type: int,
) -> ResearchSource | None:
    """Decode only URL candidates and the captured Markdown report shape."""
    content_kind = row.content.kind if row.HasField("content") else 0
    content = row.content.text if row.HasField("content") else ""
    if not row.source_url:
        if content_kind != _REPORT_CONTENT_KIND or not content:
            return None
        return ResearchSource(
            url="",
            title=row.title,
            result_type=RESEARCH_RESULT_TYPE_REPORT,
            research_task_id=task_id,
            report_markdown=content,
            source_ordinal=row.source_ordinal or None,
            hint=row.hint,
        )
    result_type = (
        RESEARCH_RESULT_TYPE_DRIVE
        if source_type == RESEARCH_RESULT_TYPE_DRIVE
        else RESEARCH_RESULT_TYPE_WEB
    )
    return ResearchSource(
        url=row.source_url,
        title=row.title,
        result_type=result_type,
        research_task_id=task_id,
        source_ordinal=row.source_ordinal or None,
        hint=row.hint,
    )


def decode_research_job(row: Any, *, method_id: str) -> ResearchTask:
    """Project one historical job without guessing unresolved result rows."""
    task_id = row.source_discovery_job_id
    if not task_id:
        raise DecodingError("Android Research job omitted its run id", method_id=method_id)
    info = row.info
    source_type = info.query.source_type if info.HasField("query") else 0
    query = info.query.query if info.HasField("query") else ""
    status_code = info.status
    reason = termination_reason_from_code(status_code)
    sources: list[ResearchSource] = []
    if info.HasField("results"):
        for source in info.results.sources:
            decoded = decode_discovered_source(
                source,
                task_id=task_id,
                source_type=source_type,
            )
            if decoded is not None:
                sources.append(decoded)
    report = next((source.report_markdown for source in sources if source.is_report), "")
    return ResearchTask(
        task_id=task_id,
        status=status_from_termination_reason(reason),
        query=query,
        sources=tuple(sources),
        summary=info.results.summary if info.HasField("results") else "",
        report=report,
        status_code=status_code,
        source_type=source_type or None,
        discovery_mode=_mode(info.discovery_mode),
        created_at=_timestamp(row, "create_time"),
        updated_at=_timestamp(row, "update_time"),
    )


def decode_research_jobs(response: Any, *, method_id: str) -> list[ResearchTask]:
    """Decode valid historical jobs, isolating only rows with no identity."""
    decoded: list[ResearchTask] = []
    for row in response.jobs:
        try:
            decoded.append(decode_research_job(row, method_id=method_id))
        except DecodingError:
            continue
    return decoded


__all__ = ["decode_discovered_source", "decode_research_job", "decode_research_jobs"]
