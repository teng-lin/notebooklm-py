"""Web codecs for notebook prompt and report-format suggestions."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from ..._binding import CodecPayload
from ..._records import (
    ArtifactSuggestReportsInput,
    ArtifactSuggestReportsResult,
    NotebookSuggestPromptsInput,
    NotebookSuggestPromptsResult,
    PromptSuggestionRecord,
    ReportSuggestionRecord,
)
from ..._row_adapters.artifacts import (
    ReportSuggestionRow,
    unwrap_artifact_rows,
)
from ..._row_adapters.notebooks import PromptSuggestionRow, unwrap_prompt_suggestions
from ...rpc import RPCMethod, nest_source_ids
from .source_ids import SourceIdDiagnostics, decode_notebook_source_ids


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
    """Decode the embedded source ids with the legacy tolerant (guarded) diagnostics."""
    return decode_notebook_source_ids(
        data, notebook_id=notebook_id, diagnostics=SourceIdDiagnostics.GUARDED
    )


def encode_notebook_suggest_prompts(
    value: NotebookSuggestPromptsInput, *, source_ids: Sequence[str]
) -> CodecPayload:
    """Payload for the ``notebook.suggest_prompts`` kickoff (P9.4b custom row).

    ``allow_null`` travels with the params — an empty suggestion response decodes
    to no suggestions — so the row never names a method.
    """
    return CodecPayload(
        params=encode_prompt_suggestions(
            value.notebook_id,
            source_ids,
            mode=value.mode,
            query=value.query,
        ),
        source_path=f"/notebook/{value.notebook_id}",
        allow_null=True,
    )


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
    "encode_notebook_suggest_prompts",
    "encode_prompt_suggestions",
    "encode_report_suggestions",
]
