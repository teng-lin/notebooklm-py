"""MCP projection for public source-batch outcomes."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..outcomes import SourceBatchItemOutcome, redact_operation_text
from ..types import Source, source_status_to_str


def project_source_batch_item(
    item: SourceBatchItemOutcome,
    *,
    error_payload: Callable[[BaseException], dict[str, Any]],
) -> tuple[dict[str, Any], Source | None]:
    """Project one settled member without deriving continuation policy."""

    assert item.outcome is not None
    if item.error is not None:
        return (
            {
                "input": item.outcome.input,
                "status": "error",
                "commit_state": item.outcome.commit_state.value,
                "error": error_payload(item.error),
            },
            None,
        )
    source = item.source
    if source is None:  # pragma: no cover - public outcome invariant
        raise AssertionError("confirmed source batch outcome has no source")
    projected: dict[str, Any] = {
        "input": item.outcome.input,
        "status": "added",
        "commit_state": "confirmed",
        "source_id": source.id,
        "title": None if source.title is None else redact_operation_text(source.title),
        "status_label": source_status_to_str(source.status),
    }
    if source.is_error:
        projected["warning"] = (
            "Import failed: the source row was created but processing errored "
            "(status_label='error'). Delete it with source_delete, or list "
            "failures via source_list(status='error')."
        )
    return projected, source if source.is_ready else None


__all__ = ["project_source_batch_item"]
