"""Compatibility helpers for the migrated saved-from-chat note codec.

The live ``CREATE_NOTE:saved_from_chat`` binding is owned by the
``_web/bindings/chat.py:CHAT_SAVE_NOTE`` codec row. These helpers retain
historical private encoder imports without accepting an RPC dispatcher.
"""

from __future__ import annotations

from .._projectors import chat_reference_record
from .._web.codec.chat_saved_note import (
    _CITATION_MARKER_RE,
    _strip_citation_markers,
    build_save_note_params,
)
from .._web.codec.chat_saved_note import _resolve_reference as _resolve_record
from ..types import ChatReference


def _resolve_reference(
    references: list[ChatReference],
    citation_number: int,
) -> ChatReference | None:
    """Compatibility projection retaining the original public object."""
    records = tuple(chat_reference_record(reference) for reference in references)
    resolved = _resolve_record(records, citation_number)
    if resolved is None:
        return None
    for reference, record in zip(references, records, strict=True):
        if record is resolved:
            return reference
    raise AssertionError("resolved reference must belong to the projected record tuple")


def build_save_chat_as_note_params(
    notebook_id: str,
    answer_text: str,
    references: list[ChatReference],
    title: str,
) -> list[object]:
    """Delegate the historical encoder name to the web-owned neutral codec."""
    return build_save_note_params(
        notebook_id,
        answer_text,
        tuple(chat_reference_record(reference) for reference in references),
        title,
    )


__all__ = [
    "_CITATION_MARKER_RE",
    "_strip_citation_markers",
    "build_save_chat_as_note_params",
]
