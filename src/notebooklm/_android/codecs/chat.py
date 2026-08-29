"""Projection of the admitted Android chat protobuf fields."""

from __future__ import annotations

from typing import Any

from ..._types.documents import (
    BlockKind,
    DocumentAnnotation,
    DocumentBlock,
    StructuredDocument,
    TextSpan,
    utf16_len,
)
from ...types import ChatReference, ConversationTurnKey


def _decode_blocks(elements: Any) -> tuple[DocumentBlock, ...]:
    blocks: list[DocumentBlock] = []
    for element in elements:
        start = element.start_index
        end = element.end_index
        if start < 0 or end < start:
            continue

        spans: list[TextSpan] = []
        kind = BlockKind.UNKNOWN
        if element.HasField("paragraph"):
            kind = BlockKind.PARAGRAPH
            for paragraph_element in element.paragraph.elements:
                span_start = paragraph_element.start_index
                span_end = paragraph_element.end_index
                if span_start < 0 or span_end < span_start:
                    continue
                if not paragraph_element.HasField("text_run"):
                    continue
                spans.append(
                    TextSpan(
                        start_index=span_start,
                        end_index=span_end,
                        text=paragraph_element.text_run.content,
                    )
                )

        blocks.append(
            DocumentBlock(
                start_index=start,
                end_index=end,
                spans=tuple(spans),
                kind=kind,
            )
        )
    return tuple(sorted(blocks, key=lambda block: (block.start_index, block.end_index)))


def decode_document(document: Any) -> StructuredDocument:
    """Decode only chat-proven paragraph text and answer annotation fields."""
    if not document.HasField("body"):
        return StructuredDocument()

    annotations: list[DocumentAnnotation] = []
    for entry in document.body.inline_object_locations:
        if not entry.HasField("object_id") or not entry.object_id.id:
            continue
        if not entry.HasField("content_range"):
            continue
        start = entry.content_range.start_index
        end = entry.content_range.end_index
        if start < 0 or end < start:
            continue
        annotations.append(
            DocumentAnnotation(
                object_id=entry.object_id.id,
                start_index=start,
                end_index=end,
            )
        )
    return StructuredDocument(
        blocks=_decode_blocks(document.body.content),
        annotations=tuple(annotations),
    )


def _fragment_projection(citation: Any) -> tuple[str | None, int | None, int | None]:
    if not citation.HasField("fragment"):
        return None, None, None
    blocks = _decode_blocks(citation.fragment.elements)
    if not blocks:
        return None, None, None

    start = min(block.start_index for block in blocks)
    end = max(block.end_index for block in blocks)
    parts: list[str] = []
    cursor = start
    for block in blocks:
        if block.end_index <= cursor:
            continue
        text = block.text
        if block.start_index < cursor:
            overlap = cursor - block.start_index
            encoded = text.encode("utf-16-le", errors="surrogatepass")
            text = encoded[overlap * 2 :].decode("utf-16-le", errors="replace")
        parts.append(text)
        cursor = block.end_index
    return "".join(parts) or None, start, end


def decode_references(document: Any, answer_document: StructuredDocument) -> list[ChatReference]:
    """Decode citations only through responseDoc's proven object graph."""
    if not document.HasField("body") and not document.objects:
        return []

    extent = utf16_len(answer_document.text)
    anchors: dict[str, DocumentAnnotation] = {}
    for annotation in answer_document.annotations:
        if annotation.end_index <= extent:
            anchors.setdefault(annotation.object_id, annotation)

    references: list[ChatReference] = []
    for ordinal, document_object in enumerate(document.objects, start=1):
        if not document_object.HasField("citation"):
            continue
        citation = document_object.citation
        if (
            not citation.HasField("source_attribution")
            or not citation.source_attribution.HasField("ingested_source")
            or not citation.source_attribution.ingested_source.HasField("source")
        ):
            continue
        source_id = citation.source_attribution.ingested_source.source.id
        if not source_id:
            continue

        chunk_id = document_object.object_id.id if document_object.HasField("object_id") else None
        cited_text, start, end = _fragment_projection(citation)
        anchor = anchors.get(chunk_id or "")
        references.append(
            ChatReference(
                source_id=source_id,
                citation_number=ordinal,
                cited_text=cited_text,
                start_char=start,
                end_char=end,
                chunk_id=chunk_id or None,
                answer_anchor_start=None if anchor is None else anchor.start_index,
                answer_anchor_end=None if anchor is None else anchor.end_index,
            )
        )
    return references


def decode_turn_key(answer: Any) -> ConversationTurnKey | None:
    """Decode the captured three-field answer turn key without reinterpreting it."""
    if not answer.HasField("conversation_turn_key"):
        return None
    key = answer.conversation_turn_key
    if not key.session_id:
        return None
    return ConversationTurnKey(
        session_id=key.session_id,
        turn_id=key.conversation_id or None,
        turn_code=key.observed_field_3,
    )


def decode_history(response: Any, *, limit: int) -> list[tuple[str, str]]:
    """Decode newest-first Android history into oldest-first Q&A pairs."""
    pairs: list[tuple[str, str]] = []
    for turn in response.chat_turns[: max(0, limit)]:
        if not turn.user_query_text:
            continue
        answer = ""
        if turn.HasField("act_on_sources_response") and turn.act_on_sources_response.HasField(
            "response"
        ):
            answer = turn.act_on_sources_response.response.response
        pairs.append((turn.user_query_text, answer))
    pairs.reverse()
    return pairs


__all__ = [
    "decode_document",
    "decode_history",
    "decode_references",
    "decode_turn_key",
]
