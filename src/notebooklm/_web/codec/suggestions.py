"""Web codecs for notebook prompt and report-format suggestions."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from ..._binding import CodecPayload
from ..._records import (
    ArtifactSuggestReportsInput,
    ArtifactSuggestReportsResult,
    NotebookSuggestPromptsResult,
    PromptSuggestionRecord,
    ReportSuggestionRecord,
)
from ..._row_adapters.artifacts import (
    ReportSuggestionRow,
    unwrap_artifact_rows,
)
from ..._row_adapters.notebooks import PromptSuggestionRow, unwrap_prompt_suggestions
from ..._row_adapters.sources import SourceRow
from ...rpc import RPCMethod, nest_source_ids, safe_index

logger = logging.getLogger("notebooklm._notebooks")


def _prompt_suggestions_client_context() -> list[Any]:
    return [2, None, None, [1, None, None, None, None, None, None, None, None, None, [1]]]


def encode_prompt_suggestions(
    notebook_id: str,
    source_ids: Sequence[str],
    *,
    mode: int = 4,
    query: str | None = None,
) -> list[Any]:
    """Build the live ``GeneratePromptSuggestions`` positional request."""
    if not 1 <= mode <= 10:
        raise ValueError(f"mode must be in the inclusive range 1..10, got {mode!r}")
    resolved_query = query if query and query.strip() else None
    return [
        _prompt_suggestions_client_context(),
        notebook_id,
        nest_source_ids(list(source_ids), 1),
        mode,
        None,
        resolved_query,
    ]


def encode_report_suggestions(notebook_id: str) -> list[Any]:
    """Build the live ``GenerateReportSuggestions`` positional request."""
    return [[2], notebook_id]


def encode_artifact_suggest_reports(value: ArtifactSuggestReportsInput) -> CodecPayload:
    """Payload for the ``artifact.suggest_reports`` codec row (P9.3).

    The notebook route and ``allow_null`` — an empty suggestion list decodes to
    no suggestions — travel with the params so the row never names a method.
    """
    return CodecPayload(
        params=encode_report_suggestions(value.notebook_id),
        source_path=f"/notebook/{value.notebook_id}",
        allow_null=True,
    )


def decode_prompt_source_ids(data: Any, *, notebook_id: str) -> tuple[str, ...]:
    """Decode the embedded source ids with the legacy tolerant diagnostics."""
    source_ids: list[str] = []
    if not data or not isinstance(data, list):
        return ()

    method_id = RPCMethod.GET_NOTEBOOK.value
    try:
        notebook_info = safe_index(
            data,
            0,
            method_id=method_id,
            source="NotebooksAPI.get_source_ids",
        )
        if not isinstance(notebook_info, list):
            logger.warning(
                "get_source_ids: notebook_data[0] shape unexpected for %s "
                "(schema drift?). top-type=%s",
                notebook_id,
                type(notebook_info).__name__,
            )
            return ()
        if len(notebook_info) <= 1:
            logger.warning(
                "get_source_ids: notebook_info has no sources slot for %s (schema drift?). len=%d",
                notebook_id,
                len(notebook_info),
            )
            return ()
        sources = safe_index(
            notebook_info,
            1,
            method_id=method_id,
            source="NotebooksAPI.get_source_ids",
        )
        if sources is None:
            return ()
        if not isinstance(sources, list):
            logger.warning(
                "get_source_ids: notebook_info[1] not list for %s (schema drift?). len=%d",
                notebook_id,
                len(notebook_info),
            )
            return ()
        for source in sources:
            if not (isinstance(source, list) and source):
                continue
            source_id = SourceRow.from_entry(source, method_id=method_id).id
            if source_id:
                source_ids.append(source_id)
    except (IndexError, TypeError) as exc:
        logger.warning(
            "get_source_ids: unexpected exception despite guards for %s: %s",
            notebook_id,
            exc,
            exc_info=True,
        )
    return tuple(source_ids)


def decode_prompt_suggestions(data: Any) -> NotebookSuggestPromptsResult:
    """Decode prompt rows with the existing best-effort tolerance."""
    rows = unwrap_prompt_suggestions(
        data,
        method_id=RPCMethod.SUGGEST_PROMPTS.value,
        source="suggest_prompts",
    )
    return NotebookSuggestPromptsResult(
        tuple(
            PromptSuggestionRecord(row.title, row.prompt)
            for row in map(PromptSuggestionRow, rows)
            if row.is_well_formed
        )
    )


def decode_report_suggestions(data: Any) -> ArtifactSuggestReportsResult:
    """Decode report rows with the existing wrapped/flat tolerance."""
    if not (data and isinstance(data, list)):
        return ArtifactSuggestReportsResult(())
    rows = unwrap_artifact_rows(
        data,
        method_id=RPCMethod.GET_SUGGESTED_REPORTS.value,
        source="suggest_reports",
    )
    return ArtifactSuggestReportsResult(
        tuple(
            ReportSuggestionRecord(
                title=row.title,
                description=row.description,
                prompt=row.prompt,
                # A present unknown value is intentionally preserved verbatim.
                audience_level=row.audience_level,
            )
            for row in map(ReportSuggestionRow, rows)
            if row.is_well_formed
        )
    )


def decode_artifact_suggest_reports(
    value: ArtifactSuggestReportsInput, data: Any
) -> ArtifactSuggestReportsResult:
    """Row decoder for ``artifact.suggest_reports``."""
    del value
    return decode_report_suggestions(data)


__all__ = [
    "decode_artifact_suggest_reports",
    "decode_prompt_source_ids",
    "decode_prompt_suggestions",
    "decode_report_suggestions",
    "encode_artifact_suggest_reports",
    "encode_prompt_suggestions",
    "encode_report_suggestions",
]
