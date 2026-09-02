"""Unit coverage for the Android chat projection's admission branches.

``tests/unit/android/test_chat.py`` covers the fully-populated decode through
the adapter. These cases call the projection directly to reach the citation
rejections, the overlapping-fragment reconstruction, and the history pairing
rules for rows that arrive without their partner.
"""

from __future__ import annotations

import pytest

from notebooklm._android.codecs.chat import (
    decode_history,
    decode_references,
    decode_turn_key,
)
from notebooklm._android.codecs.documents import decode_document
from notebooklm._android.proto.google.internal.labs.tailwind.orchestration.v1 import (
    chat_pb2,
    read_pb2,
)
from notebooklm._types.documents import StructuredDocument


def _fragment_element(text: str, *, start: int, end: int) -> chat_pb2.StructuralElement:
    element = chat_pb2.StructuralElement(start_index=start, end_index=end)
    run = element.paragraph.elements.add(start_index=start, end_index=end)
    run.text_run.content = text
    return element


def _citation_object(
    *,
    object_id: str | None = "c1",
    source_id: str | None = "src-1",
    fragment: list[chat_pb2.StructuralElement] | None = None,
    ranges: list[tuple[int, int]] | None = None,
    attribution: bool = True,
) -> chat_pb2.DocumentObject:
    item = chat_pb2.DocumentObject()
    if object_id is not None:
        item.object_id.id = object_id
    citation = item.citation
    if attribution:
        citation.source_attribution.ingested_source.source.CopyFrom(
            read_pb2.SourceId(id=source_id or "")
        )
    if fragment is not None:
        citation.fragment.elements.extend(fragment)
    for start, end in ranges or []:
        citation.ranges.add(start_index=start, end_index=end)
    return item


def _document(*objects: chat_pb2.DocumentObject) -> chat_pb2.TailwindDoc:
    document = chat_pb2.TailwindDoc()
    document.objects.extend(objects)
    return document


# ---------------------------------------------------------------------------
# decode_references
# ---------------------------------------------------------------------------


def test_an_empty_response_document_yields_no_references() -> None:
    assert decode_references(chat_pb2.TailwindDoc(), StructuredDocument()) == []


def test_objects_without_a_citation_are_skipped() -> None:
    document = chat_pb2.TailwindDoc()
    document.objects.add().object_id.id = "c1"

    assert decode_references(document, StructuredDocument()) == []


@pytest.mark.parametrize(
    "item",
    [
        pytest.param(_citation_object(attribution=False), id="no-source-attribution"),
        pytest.param(_citation_object(source_id=""), id="empty-source-id"),
    ],
)
def test_citations_without_a_usable_source_are_skipped(item: chat_pb2.DocumentObject) -> None:
    assert decode_references(_document(item), StructuredDocument()) == []


def test_a_partial_attribution_chain_is_skipped() -> None:
    item = chat_pb2.DocumentObject()
    item.object_id.id = "c1"
    # ``source_attribution`` present, but without its ``ingested_source`` arm.
    item.citation.source_attribution.SetInParent()

    assert decode_references(_document(item), StructuredDocument()) == []


def test_a_citation_without_a_fragment_carries_no_text_or_offsets() -> None:
    [reference] = decode_references(_document(_citation_object()), StructuredDocument())

    assert (reference.cited_text, reference.start_char, reference.end_char) == (None, None, None)
    assert (reference.source_id, reference.citation_number, reference.chunk_id) == (
        "src-1",
        1,
        "c1",
    )


def test_a_fragment_whose_blocks_all_fail_admission_carries_no_text() -> None:
    """Inverted block ranges decode to nothing, so the projection stays empty."""
    item = _citation_object(fragment=[_fragment_element("x", start=9, end=2)])

    [reference] = decode_references(_document(item), StructuredDocument())

    assert reference.cited_text is None


def test_fragment_blocks_are_concatenated_in_offset_order() -> None:
    item = _citation_object(
        fragment=[
            _fragment_element("world", start=5, end=10),
            _fragment_element("hello", start=0, end=5),
        ]
    )

    [reference] = decode_references(_document(item), StructuredDocument())

    assert reference.cited_text == "helloworld"
    assert (reference.start_char, reference.end_char) == (0, 10)


def test_an_overlapping_fragment_block_is_trimmed_to_the_new_text_only() -> None:
    """The overlap is measured in UTF-16 units, matching the declared offsets."""
    item = _citation_object(
        fragment=[
            _fragment_element("abcde", start=0, end=5),
            _fragment_element("cdefg", start=2, end=7),
        ]
    )

    [reference] = decode_references(_document(item), StructuredDocument())

    assert reference.cited_text == "abcdefg"


def test_a_fully_contained_fragment_block_contributes_nothing() -> None:
    item = _citation_object(
        fragment=[
            _fragment_element("abcde", start=0, end=5),
            _fragment_element("bc", start=1, end=3),
            _fragment_element("fg", start=5, end=7),
        ]
    )

    [reference] = decode_references(_document(item), StructuredDocument())

    assert reference.cited_text == "abcdefg"


def test_a_text_less_fragment_falls_back_to_the_plain_rendering() -> None:
    element = chat_pb2.StructuralElement(start_index=0, end_index=4)
    element.code_block.content = "code"
    item = _citation_object(fragment=[element])

    [reference] = decode_references(_document(item), StructuredDocument())

    assert reference.cited_text == "code"


@pytest.mark.parametrize(
    ("ranges", "expected"),
    [
        pytest.param([], (None, None), id="no-declared-ranges"),
        pytest.param([(3, 9)], (3, 9), id="single-range"),
        pytest.param([(10, 12), (3, 9)], (3, 12), id="union-of-ranges"),
        pytest.param([(9, 3)], (None, None), id="inverted-range-rejected"),
    ],
)
def test_declared_fragment_range_is_the_strict_union(
    ranges: list[tuple[int, int]], expected: tuple[int | None, int | None]
) -> None:
    item = _citation_object(ranges=ranges)

    [reference] = decode_references(_document(item), StructuredDocument())

    assert (reference.fragment_start_char, reference.fragment_end_char) == expected


def test_answer_anchors_ignore_annotations_past_the_answer_extent() -> None:
    answer = chat_pb2.TailwindDoc()
    answer.body.content.add(start_index=0, end_index=5).paragraph.elements.add(
        start_index=0, end_index=5
    ).text_run.content = "hello"
    in_range = answer.body.inline_object_locations.add()
    in_range.object_id.id = "c1"
    in_range.content_range.start_index = 1
    in_range.content_range.end_index = 4
    out_of_range = answer.body.inline_object_locations.add()
    out_of_range.object_id.id = "c2"
    out_of_range.content_range.start_index = 90
    out_of_range.content_range.end_index = 99
    answer_document = decode_document(answer)

    references = decode_references(
        _document(_citation_object(object_id="c1"), _citation_object(object_id="c2")),
        answer_document,
    )

    anchored, unanchored = references
    assert (anchored.answer_anchor_start, anchored.answer_anchor_end) == (1, 4)
    assert (unanchored.answer_anchor_start, unanchored.answer_anchor_end) == (None, None)


# ---------------------------------------------------------------------------
# decode_turn_key
# ---------------------------------------------------------------------------


def test_turn_key_is_absent_without_the_field() -> None:
    assert decode_turn_key(chat_pb2.AnswerResponse()) is None


def test_turn_key_without_a_session_id_is_not_usable() -> None:
    answer = chat_pb2.AnswerResponse()
    answer.conversation_turn_key.conversation_id = "turn-1"

    assert decode_turn_key(answer) is None


def test_turn_key_projects_the_three_captured_fields() -> None:
    answer = chat_pb2.AnswerResponse()
    answer.conversation_turn_key.CopyFrom(
        chat_pb2.ConversationTurnKey(
            session_id="sess-1", conversation_id="turn-1", observed_field_3=4
        )
    )

    key = decode_turn_key(answer)

    assert key is not None
    assert (key.session_id, key.turn_id, key.turn_code) == ("sess-1", "turn-1", 4)


def test_turn_key_reports_a_missing_conversation_id_as_none() -> None:
    answer = chat_pb2.AnswerResponse()
    answer.conversation_turn_key.session_id = "sess-1"

    key = decode_turn_key(answer)

    assert key is not None
    assert key.turn_id is None


# ---------------------------------------------------------------------------
# decode_history
# ---------------------------------------------------------------------------


def _turns(*turns: chat_pb2.ChatHistoryMessage) -> chat_pb2.ListChatTurnsResponse:
    return chat_pb2.ListChatTurnsResponse(chat_turns=list(turns))


def _answer_turn(
    text: str = "",
    *,
    doc_text: str | None = None,
    query: str = "",
) -> chat_pb2.ChatHistoryMessage:
    turn = chat_pb2.ChatHistoryMessage(observed_event_type=2, user_query_text=query)
    response = turn.act_on_sources_response.response
    response.response = text
    if doc_text is not None:
        response.response_doc.body.content.add().paragraph.elements.add().text_run.content = (
            doc_text
        )
    return turn


def _query_turn(text: str) -> chat_pb2.ChatHistoryMessage:
    return chat_pb2.ChatHistoryMessage(observed_event_type=1, user_query_text=text)


@pytest.mark.parametrize("limit", [0, -1])
def test_a_non_positive_limit_returns_nothing(limit: int) -> None:
    assert decode_history(_turns(_query_turn("q")), limit=limit) == []


def test_newest_first_rows_are_paired_and_returned_oldest_first() -> None:
    response = _turns(
        _answer_turn("a2"),
        _query_turn("q2"),
        _answer_turn("a1"),
        _query_turn("q1"),
    )

    assert decode_history(response, limit=10) == [("q1", "a1"), ("q2", "a2")]


def test_a_combined_row_is_paired_without_inventing_a_second_question() -> None:
    """A legacy row carrying both halves stays one pair."""
    response = _turns(_answer_turn("a1", query="q1"))

    assert decode_history(response, limit=10) == [("q1", "a1")]


def test_an_answer_falls_back_to_its_response_document() -> None:
    response = _turns(_answer_turn("", doc_text="from doc"), _query_turn("q1"))

    assert decode_history(response, limit=10) == [("q1", "from doc")]


def test_only_the_newest_of_several_orphan_answers_is_retained() -> None:
    response = _turns(_answer_turn("newest"), _answer_turn("older"), _query_turn("q1"))

    assert decode_history(response, limit=10) == [("q1", "newest")]


def test_a_question_without_a_preceding_answer_pairs_with_an_empty_string() -> None:
    response = _turns(_query_turn("q1"))

    assert decode_history(response, limit=10) == [("q1", "")]


def test_a_turn_without_a_response_arm_yields_an_empty_answer() -> None:
    bare = chat_pb2.ChatHistoryMessage(observed_event_type=2)

    assert decode_history(_turns(bare, _query_turn("q1")), limit=10) == [("q1", "")]


def test_rows_of_an_unrecovered_event_type_are_ignored() -> None:
    unknown = chat_pb2.ChatHistoryMessage(observed_event_type=7, user_query_text="ignored")

    assert decode_history(_turns(unknown, _query_turn("q1")), limit=10) == [("q1", "")]


def test_pairing_stops_once_the_limit_is_reached() -> None:
    response = _turns(
        _answer_turn("a3"),
        _query_turn("q3"),
        _answer_turn("a2"),
        _query_turn("q2"),
        _answer_turn("a1"),
        _query_turn("q1"),
    )

    assert decode_history(response, limit=2) == [("q2", "a2"), ("q3", "a3")]
