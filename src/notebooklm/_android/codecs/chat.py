"""Projection of the admitted Android chat protobuf fields."""

from __future__ import annotations

from typing import Any

from ..._types.documents import DocumentAnnotation, StructuredDocument, utf16_len
from ...types import ChatReference, ConversationTurnKey
from .documents import (
    decode_blocks,
    decode_document,
    structural_elements_plain_text,
    tailwind_doc_plain_text,
)


def _fragment_projection(citation: Any) -> tuple[str | None, int | None, int | None]:
    if not citation.HasField("fragment"):
        return None, None, None
    blocks = decode_blocks(citation.fragment.elements)
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
    text = "".join(parts)
    if not text:
        text = structural_elements_plain_text(citation.fragment.elements)
    return text or None, start, end


def _declared_fragment_range(citation: Any) -> tuple[int | None, int | None]:
    """Return the strict union of the server-declared source ranges."""

    if not citation.ranges:
        return None, None
    pairs = [(int(item.start_index), int(item.end_index)) for item in citation.ranges]
    if any(start < 0 or end < start for start, end in pairs):
        return None, None
    return min(start for start, _end in pairs), max(end for _start, end in pairs)


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
        fragment_start, fragment_end = _declared_fragment_range(citation)
        anchor = anchors.get(chunk_id or "")
        references.append(
            ChatReference(
                source_id=source_id,
                citation_number=ordinal,
                cited_text=cited_text,
                start_char=start,
                end_char=end,
                chunk_id=chunk_id or None,
                fragment_start_char=fragment_start,
                fragment_end_char=fragment_end,
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
    """Pair newest-first Android question/answer event rows, oldest first."""

    bounded_limit = max(0, limit)
    if bounded_limit == 0:
        return []

    pairs: list[tuple[str, str]] = []
    pending_answer = ""
    answer_seen = False
    for turn in response.chat_turns:
        role = turn.observed_event_type
        if role == 2:
            answer = ""
            if turn.HasField("act_on_sources_response") and turn.act_on_sources_response.HasField(
                "response"
            ):
                answer_response = turn.act_on_sources_response.response
                answer = answer_response.response
                if not answer and answer_response.HasField("response_doc"):
                    answer = tailwind_doc_plain_text(answer_response.response_doc)

            # Captured rows carry generated response (2) immediately before
            # their user query (1) in the newest-first feed. Retain the newest
            # response until its question arrives. A combined legacy-shaped
            # row remains decodable without inventing a second question.
            if turn.user_query_text:
                pairs.append((turn.user_query_text, answer))
                pending_answer = ""
                answer_seen = False
            elif not answer_seen:
                pending_answer = answer
                answer_seen = True
        elif role == 1 and turn.user_query_text:
            pairs.append((turn.user_query_text, pending_answer if answer_seen else ""))
            pending_answer = ""
            answer_seen = False

        if len(pairs) >= bounded_limit:
            break

    pairs.reverse()
    return pairs


__all__ = [
    "decode_document",
    "decode_history",
    "decode_references",
    "decode_turn_key",
]
