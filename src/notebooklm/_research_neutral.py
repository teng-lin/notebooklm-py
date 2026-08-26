"""Compatibility projection from retained Research models to neutral records."""

from __future__ import annotations

from typing import Any

from ._research_task_parser import parse_research_task_models
from ._semantic.records import ResearchSourceRecord, ResearchTaskRecord
from ._types.research import (
    RESEARCH_SOURCE_TYPE_DRIVE,
    RESEARCH_SOURCE_TYPE_WEB,
    ResearchSource,
    ResearchTask,
)
from .rpc.types import discovery_mode_to_str


def _source_record(source: ResearchSource) -> ResearchSourceRecord:
    return ResearchSourceRecord(
        url=source.url,
        title=source.title,
        result_type=source.result_type,
        research_task_id=source.research_task_id,
        report_markdown=source.report_markdown,
        source_ordinal=source.source_ordinal,
        hint=source.hint,
    )


def _task_record(task: ResearchTask) -> ResearchTaskRecord:
    return ResearchTaskRecord(
        task_id=task.task_id,
        status=task.status.value,
        query=task.query,
        sources=tuple(_source_record(source) for source in task.sources),
        summary=task.summary,
        report=task.report,
        status_code=task.status_code,
        source_type=task.source_type,
        discovery_mode=(
            None if task.discovery_mode is None else discovery_mode_to_str(task.discovery_mode)
        ),
        created_at=task.created_at,
        updated_at=task.updated_at,
        account_id=task.account_id,
    )


def decode_research_task_records(result: Any) -> tuple[ResearchTaskRecord, ...]:
    """Decode a poll payload through the retained parser into neutral records."""
    return tuple(_task_record(task) for task in parse_research_task_models(result))


__all__ = [
    "RESEARCH_SOURCE_TYPE_DRIVE",
    "RESEARCH_SOURCE_TYPE_WEB",
    "decode_research_task_records",
]
