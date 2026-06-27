"""Characterization tests for private streamed-chat protocol helpers."""

from __future__ import annotations

import ast
import builtins
import importlib
import importlib.util
import inspect
import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import pytest

from notebooklm._chat.wire import (
    StreamingChatParseResult,
    build_streaming_chat_request,
    collect_texts_from_nested,
    extract_answer_and_refs_from_chunk,
    extract_answer_range,
    extract_score,
    extract_text_passages,
    extract_uuid_from_nested,
    parse_citations,
    parse_single_citation,
    parse_streaming_chat_response,
    raise_if_rate_limited,
)
from notebooklm.exceptions import ChatError, UnknownRPCMethodError
from notebooklm.rpc.types import get_query_url

SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "notebooklm"


def _snapshot(
    *,
    csrf_token: str = "csrf",
    session_id: str = "sid",
    authuser: int = 0,
    account_email: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        csrf_token=csrf_token,
        session_id=session_id,
        authuser=authuser,
        account_email=account_email,
    )


def _decode_body(body: str) -> tuple[list[Any], dict[str, list[str]]]:
    body_qs = parse_qs(body, keep_blank_values=True)
    f_req = json.loads(unquote(body_qs["f.req"][0]))
    params = json.loads(f_req[1])
    return params, body_qs


def _chunk(
    text: str,
    *,
    marked: bool = True,
    conversation_id: str | None = None,
    citations: list[Any] | None = None,
) -> str:
    marker = 1 if marked else 0
    type_info = [[], None, None, citations or [], marker]
    conv = [conversation_id, 123] if conversation_id is not None else None
    inner_json = json.dumps([[text, None, conv, None, type_info]])
    return json.dumps([["wrb.fr", None, inner_json]])


def _length_prefixed(*chunks: str, xssi: bool = True) -> str:
    parts = [")]}'"] if xssi else []
    for chunk in chunks:
        parts.append(f"\n{len(chunk)}\n{chunk}")
    parts.append("\n")
    return "".join(parts)


def _citation(
    *,
    source_id: str,
    chunk_id: str = "chunk-1",
    text: str = "cited passage",
    start: int = 10,
    end: int = 20,
    score: float | None = 0.9,
    answer_start: int | None = None,
    answer_end: int | None = None,
) -> list[Any]:
    return [
        [chunk_id],
        [
            None,
            None,
            score,
            [[None, answer_start, answer_end]] if answer_start is not None else [[None]],
            [[[start, end, [[[start, end, text]]]]]],
            [[[source_id]]],
            [chunk_id],
        ],
    ]


def test_module_signatures_are_stable() -> None:
    signatures = {
        "build_streaming_chat_request": inspect.signature(build_streaming_chat_request),
        "parse_streaming_chat_response": inspect.signature(parse_streaming_chat_response),
        "extract_answer_and_refs_from_chunk": inspect.signature(extract_answer_and_refs_from_chunk),
        "raise_if_rate_limited": inspect.signature(raise_if_rate_limited),
        "parse_citations": inspect.signature(parse_citations),
        "parse_single_citation": inspect.signature(parse_single_citation),
        "extract_text_passages": inspect.signature(extract_text_passages),
        "extract_answer_range": inspect.signature(extract_answer_range),
        "extract_score": inspect.signature(extract_score),
        "collect_texts_from_nested": inspect.signature(collect_texts_from_nested),
        "extract_uuid_from_nested": inspect.signature(extract_uuid_from_nested),
    }

    assert list(signatures["build_streaming_chat_request"].parameters) == [
        "snapshot",
        "notebook_id",
        "question",
        "source_ids",
        "conversation_history",
        "conversation_id",
        "reqid",
    ]
    assert signatures["build_streaming_chat_request"].parameters["snapshot"].kind is (
        inspect.Parameter.KEYWORD_ONLY
    )
    assert list(signatures["parse_streaming_chat_response"].parameters) == ["response_text"]
    assert list(signatures["extract_answer_and_refs_from_chunk"].parameters) == ["json_str"]
    assert list(signatures["raise_if_rate_limited"].parameters) == ["error_payload"]
    assert list(signatures["parse_citations"].parameters) == ["first"]
    assert list(signatures["parse_single_citation"].parameters) == ["cite"]
    assert list(signatures["extract_text_passages"].parameters) == ["cite_inner"]
    assert list(signatures["extract_answer_range"].parameters) == ["cite_inner"]
    assert list(signatures["extract_score"].parameters) == ["cite_inner"]
    assert list(signatures["collect_texts_from_nested"].parameters) == ["nested", "texts"]
    assert list(signatures["extract_uuid_from_nested"].parameters) == ["data", "max_depth"]
    assert signatures["extract_uuid_from_nested"].parameters["max_depth"].default == 10
    assert StreamingChatParseResult("a", [], None).answer == "a"


def test_build_request_preserves_url_body_and_param_invariants(monkeypatch) -> None:
    monkeypatch.setenv("NOTEBOOKLM_BL", "boq_labs-custom_99999999.00_p0")
    monkeypatch.setenv("NOTEBOOKLM_HL", "ja")

    url, body, extra_headers = build_streaming_chat_request(
        snapshot=_snapshot(account_email="me@example.com", authuser=5),
        notebook_id="nb-123",
        question="Q?",
        source_ids=["s1", "s2"],
        conversation_history=[["previous answer", None, 2], ["previous question", None, 1]],
        conversation_id="conv-1",
        reqid=234567,
    )

    query = parse_qs(urlparse(url).query, keep_blank_values=True)
    params, body_qs = _decode_body(body)

    assert url.startswith(f"{get_query_url()}?")
    assert query["bl"] == ["boq_labs-custom_99999999.00_p0"]
    assert query["hl"] == ["ja"]
    assert query["_reqid"] == ["234567"]
    assert query["rt"] == ["c"]
    assert query["f.sid"] == ["sid"]
    assert query["authuser"] == ["me@example.com"]
    assert body_qs["at"] == ["csrf"]
    assert body.endswith("&")
    assert extra_headers == {}
    assert params[0] == [[["s1"]], [["s2"]]]
    assert len(params) == 9
    assert params[7] == "nb-123"


def test_build_request_omits_default_authuser_and_blank_csrf() -> None:
    url, body, _ = build_streaming_chat_request(
        snapshot=_snapshot(csrf_token="", authuser=0, account_email=None),
        notebook_id="nb-123",
        question="Q?",
        source_ids=["s1"],
        conversation_history=None,
        conversation_id="conv-1",
        reqid=1,
    )

    query = parse_qs(urlparse(url).query, keep_blank_values=True)
    _, body_qs = _decode_body(body)

    assert "authuser" not in query
    assert "at" not in body_qs


def test_build_request_uses_authuser_index_when_email_absent() -> None:
    url, _, _ = build_streaming_chat_request(
        snapshot=_snapshot(authuser=3, account_email=None),
        notebook_id="nb-123",
        question="Q?",
        source_ids=["s1"],
        conversation_history=None,
        conversation_id="conv-1",
        reqid=1,
    )

    assert parse_qs(urlparse(url).query)["authuser"] == ["3"]


def test_build_request_sends_null_conversation_id_for_new_conversations() -> None:
    """Regression for issue #659.

    New-conversation asks must send JSON ``null`` in ``params[4]`` so the
    server assigns a conversation_id that is visible in the web UI's
    conversation list. The previous behavior generated ``uuid.uuid4()``
    client-side and orphaned the conversation from the UI.
    """
    _, body, _ = build_streaming_chat_request(
        snapshot=_snapshot(),
        notebook_id="nb-123",
        question="Q?",
        source_ids=["s1"],
        conversation_history=None,
        conversation_id=None,
        reqid=1,
    )

    params, _ = _decode_body(body)
    assert params[4] is None, (
        "params[4] must be null for new conversations so the server assigns "
        f"the conversation_id; got {params[4]!r}"
    )
    # Notebook id still pinned to slot 7 — the fix only touches slot 4.
    assert params[7] == "nb-123"


def test_build_request_passes_through_caller_conversation_id_for_follow_ups() -> None:
    """Follow-ups must forward the caller-supplied conversation_id verbatim."""
    _, body, _ = build_streaming_chat_request(
        snapshot=_snapshot(),
        notebook_id="nb-123",
        question="Q?",
        source_ids=["s1"],
        conversation_history=[["prior answer", None, 2], ["prior question", None, 1]],
        conversation_id="caller-supplied-conv",
        reqid=1,
    )

    params, _ = _decode_body(body)
    assert params[4] == "caller-supplied-conv"


def test_parse_response_handles_xssi_length_prefix_raw_json_and_server_conversation_id() -> None:
    first = _chunk("First answer.", conversation_id="server-conv")
    second = _chunk("Raw JSON answer.", conversation_id="server-conv-2")
    response = _length_prefixed(first) + second

    result = parse_streaming_chat_response(response)

    assert result.answer == "Raw JSON answer."
    assert result.references == []
    assert result.conversation_id == "server-conv-2"


def test_xssi_prefix_strip_matches_shared_helper_on_real_wire_format() -> None:
    """The chat parser delegates anti-XSSI stripping to ``strip_anti_xssi``.

    On the real chat wire format the ``)]}'`` prefix is always followed by a
    newline, so routing through the shared stripper yields the same parsed
    answer whether or not the prefix is present (regression guard for the
    duplicate-stripper consolidation in issue #1205).
    """
    chunk = _chunk("Prefixed answer.", conversation_id="conv")

    with_prefix = parse_streaming_chat_response(_length_prefixed(chunk, xssi=True))
    without_prefix = parse_streaming_chat_response(_length_prefixed(chunk, xssi=False))

    assert with_prefix.answer == without_prefix.answer == "Prefixed answer."
    assert with_prefix.conversation_id == without_prefix.conversation_id == "conv"


def test_chat_parser_uses_shared_strip_anti_xssi(monkeypatch) -> None:
    """``parse_streaming_chat_response`` calls the shared ``strip_anti_xssi``."""
    import notebooklm._chat.wire as chat_wire

    seen: list[str] = []
    real_strip = chat_wire.strip_anti_xssi

    def _spy(response: str) -> str:
        seen.append(response)
        return real_strip(response)

    monkeypatch.setattr(chat_wire, "strip_anti_xssi", _spy)

    response = _length_prefixed(_chunk("Answer.", conversation_id="conv"))
    result = parse_streaming_chat_response(response)

    assert result.answer == "Answer."
    assert seen == [response]


def test_marked_answer_beats_longer_unmarked_text() -> None:
    marked = _chunk("Marked.", marked=True)
    unmarked = _chunk("This unmarked text is longer than the answer marker.", marked=False)

    result = parse_streaming_chat_response(_length_prefixed(unmarked, marked))

    assert result.answer == "Marked."


def test_unmarked_fallback_logs_under_chat_logger(caplog) -> None:
    response = _length_prefixed(_chunk("Only unmarked answer.", marked=False))

    with caplog.at_level(logging.WARNING, logger="notebooklm._chat"):
        result = parse_streaming_chat_response(response)

    assert result.answer == "Only unmarked answer."
    assert any(
        record.name == "notebooklm._chat" and "No marked answer found" in record.message
        for record in caplog.records
    )


def test_empty_response_raises_chat_response_parse_error() -> None:
    """Empty response body → zero parseable ``wrb.fr`` envelopes → raise.

    This pins the contract: an empty body is wire-protocol drift / a
    failed RPC, NOT a legitimate empty answer. The legitimate empty-answer
    path (parseable chunk with empty text) is covered in
    ``tests/unit/test_chat.py``.
    """
    from notebooklm.exceptions import ChatResponseParseError

    with pytest.raises(ChatResponseParseError) as raised:
        parse_streaming_chat_response("")

    assert "No parseable chunks" in str(raised.value)


def test_parse_citations_extracts_multiple_references_and_assigns_numbers() -> None:
    citations = [
        _citation(
            source_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            chunk_id="chunk-1",
            text="first citation",
            start=1,
            end=11,
            score=0.85,
            answer_start=100,
            answer_end=200,
        ),
        _citation(
            source_id="11111111-2222-3333-4444-555555555555",
            chunk_id="chunk-2",
            text="second citation",
            start=12,
            end=27,
            score=0.7,
            answer_start=200,
            answer_end=350,
        ),
    ]

    result = parse_streaming_chat_response(_length_prefixed(_chunk("Answer.", citations=citations)))

    assert [ref.citation_number for ref in result.references] == [1, 2]
    assert [ref.source_id for ref in result.references] == [
        "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "11111111-2222-3333-4444-555555555555",
    ]
    assert [ref.chunk_id for ref in result.references] == ["chunk-1", "chunk-2"]
    assert [ref.cited_text for ref in result.references] == ["first citation", "second citation"]
    assert [(ref.start_char, ref.end_char) for ref in result.references] == [(1, 11), (12, 27)]
    assert [(ref.answer_start_char, ref.answer_end_char) for ref in result.references] == [
        (100, 200),
        (200, 350),
    ]
    assert [ref.score for ref in result.references] == [0.85, 0.7]


def test_extract_answer_range_handles_well_formed_and_malformed_shapes() -> None:
    # Well-formed: [[None, start, end]]
    assert extract_answer_range([None, None, None, [[None, 10, 20]]]) == (10, 20)
    # Zero-length but valid: end == start
    assert extract_answer_range([None, None, None, [[None, 5, 5]]]) == (5, 5)
    # Missing outer: too short
    assert extract_answer_range([None, None, None]) == (None, None)
    # Inner [None] only (server omitted positions)
    assert extract_answer_range([None, None, None, [[None]]]) == (None, None)
    # Non-int positions
    assert extract_answer_range([None, None, None, [[None, "10", "20"]]]) == (None, None)
    # Empty outer
    assert extract_answer_range([None, None, None, []]) == (None, None)
    # Outer[0] not a list
    assert extract_answer_range([None, None, None, ["bad"]]) == (None, None)
    # bool positions rejected (bool is int subclass in Python)
    assert extract_answer_range([None, None, None, [[None, True, False]]]) == (None, None)
    # Partial range: end is None — paired check returns (None, None) not (10, None)
    assert extract_answer_range([None, None, None, [[None, 10, None]]]) == (None, None)
    assert extract_answer_range([None, None, None, [[None, None, 20]]]) == (None, None)
    # Negative start rejected
    assert extract_answer_range([None, None, None, [[None, -1, 10]]]) == (None, None)
    # end < start rejected
    assert extract_answer_range([None, None, None, [[None, 20, 10]]]) == (None, None)


def test_extract_score_accepts_float_and_int_rejects_bool_and_out_of_range() -> None:
    assert extract_score([None, None, 0.6998]) == pytest.approx(0.6998)
    assert extract_score([None, None, 0.0]) == 0.0  # boundary
    assert extract_score([None, None, 1.0]) == 1.0  # boundary
    assert extract_score([None, None, 1]) == 1.0  # int coerces
    assert extract_score([None, None, None]) is None
    assert extract_score([None, None, True]) is None  # bool rejected
    assert extract_score([None, None, "0.5"]) is None  # str rejected
    assert extract_score([None, None]) is None  # missing index
    # Out-of-range or non-finite floats
    assert extract_score([None, None, 1.5]) is None
    assert extract_score([None, None, -0.1]) is None
    assert extract_score([None, None, float("nan")]) is None
    assert extract_score([None, None, float("inf")]) is None
    assert extract_score([None, None, float("-inf")]) is None


def test_citation_absence_shapes_stay_silent(caplog) -> None:
    """Genuine absence (answer without citations) emits ZERO log records.

    Pins the soft half of the #1505 absence-vs-malformed policy for the
    citation path: no type block, a short type block, a ``None`` citation
    slot (the routine real-traffic shape), and an empty citation list all
    parse to ``[]`` with no logging at any level.
    """
    absent_shapes = [
        ["Answer only"],  # short row: no type block at all
        ["Answer", None, None, None],  # no first[4]
        ["Answer", None, None, None, [1, 2, 3]],  # type block too short for [3]
        ["Answer", None, None, None, [[], None, None, None, 1]],  # slot is None
        ["Answer", None, None, None, [[], None, None, [], 1]],  # empty citation list
    ]
    with caplog.at_level(logging.DEBUG, logger="notebooklm._chat"):
        for first in absent_shapes:
            assert parse_citations(first) == []
    assert [r for r in caplog.records if r.name.startswith("notebooklm")] == []


def test_citationless_answer_stream_parses_silently(caplog) -> None:
    """End-to-end absence pin: a citation-less answer stream stays soft/silent."""
    with caplog.at_level(logging.DEBUG, logger="notebooklm._chat"):
        result = parse_streaming_chat_response(
            _length_prefixed(_chunk("Answer without citations."))
        )
    assert result.answer == "Answer without citations."
    assert result.references == []
    assert [r for r in caplog.records if r.name.startswith("notebooklm")] == []


def test_citation_container_truthy_non_list_raises() -> None:
    """PRESENT-BUT-MALFORMED container: a truthy non-list at ``first[4][3]`` raises.

    Documented choice (raise, not warn): the surrounding parser already treats
    equivalent container drift as a raise — ``_extract_chunk_with_parseable``
    raises ``UnknownRPCMethodError`` for a non-list ``inner_data[0]``, and the
    #1505 ``unwrap_conversation_turns`` raises for a truthy non-list turn
    container — so the citation container follows the same precedent.
    """
    first = ["Answer", None, None, None, [[], None, None, {"reshaped": True}, 1]]
    with pytest.raises(UnknownRPCMethodError) as raised:
        parse_citations(first)
    assert raised.value.source == "ChatAnswerRow.citations"
    assert raised.value.path == (4, 3)


def test_non_list_answer_row_raises_in_parse_citations() -> None:
    """A non-list answer row mirrors the stream parser's structural raise."""
    with pytest.raises(UnknownRPCMethodError) as raised:
        parse_citations("not-a-row")  # type: ignore[arg-type]
    assert raised.value.source == "_chat_wire.parse_citations"


def test_stream_with_reshaped_citation_container_raises() -> None:
    """Container drift inside a real stream surfaces loudly (no silent no-citations answer)."""
    inner = json.dumps([["Answer.", None, None, None, [[], None, None, "drifted", 1]]])
    chunk = json.dumps([["wrb.fr", None, inner]])
    with pytest.raises(UnknownRPCMethodError):
        parse_streaming_chat_response(_length_prefixed(chunk))


def test_malformed_citation_rows_warn_and_keep_survivors(caplog) -> None:
    """Per-row malformation warns (at least once per row) and keeps survivors.

    The rows crafted here each emit exactly one warning. A deep malformed
    source-id tree could additionally trip the UUID max-recursion warning,
    so the production contract is "at least one bounded warning per
    malformed row" rather than exactly one.
    """
    good_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

    class _ExplodingLen(list):
        """A list whose ``len()`` raises — exercises the defensive per-row except."""

        def __len__(self) -> int:
            raise TypeError("boom")

    bad_rows: list = [
        ["present-but-too-short"],  # no detail slot -> unusable
        [["chunk-x"], "detail-not-a-list"],  # wrong type at the detail slot
        _citation(source_id="not-a-uuid"),  # no extractable source id
        _ExplodingLen(),  # unexpected error while decoding the row
    ]
    first = [
        "Answer",
        None,
        None,
        None,
        [[], None, None, [*bad_rows, _citation(source_id=good_id)], 1],
    ]

    with caplog.at_level(logging.WARNING, logger="notebooklm._chat"):
        refs = parse_citations(first)

    assert [ref.source_id for ref in refs] == [good_id]
    # The survivor keeps its RAW wire ordinal (position 5, after 4 skipped
    # rows) — not a dense re-count — so the answer's [5] marker still
    # resolves to it and [1]-[4] resolve to nothing.
    assert [ref.citation_number for ref in refs] == [5]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == len(bad_rows)  # these rows: exactly one each
    assert all("citation" in r.message for r in warnings)


def test_skipped_citation_row_leaves_numbering_hole_for_markers(caplog) -> None:
    """Regression: raw rows [good#1, bad#2, good#3] yield citation numbers {1, 3}.

    Dense renumbering after a skip would re-anchor the answer's literal
    ``[2]`` marker onto raw citation #3 (save-as-note would anchor the WRONG
    chunk via the positional fallback). Survivors must keep raw ordinals so
    the skipped row leaves a hole: marker ``[2]`` resolves to ``None`` and
    its anchor is dropped — never mis-anchored.
    """
    from notebooklm._chat.notes import _resolve_reference

    good_1 = _citation(source_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", chunk_id="chunk-1")
    bad_2 = ["present-but-unusable"]
    good_3 = _citation(source_id="11111111-2222-3333-4444-555555555555", chunk_id="chunk-3")
    first = ["Answer [1][2][3].", None, None, None, [[], None, None, [good_1, bad_2, good_3], 1]]

    with caplog.at_level(logging.WARNING, logger="notebooklm._chat"):
        refs = parse_citations(first)
    assert [ref.citation_number for ref in refs] == [1, 3]

    # End-to-end: the stream parser's final dense fill only touches None
    # numbers, so the hole survives parse_streaming_chat_response.
    inner = json.dumps([first])
    chunk = json.dumps([["wrb.fr", None, inner]])
    result = parse_streaming_chat_response(_length_prefixed(chunk))
    assert [ref.citation_number for ref in result.references] == [1, 3]

    # Downstream marker resolution: the hole yields None (anchor skipped),
    # the surviving markers resolve to their own chunks.
    resolved_1 = _resolve_reference(result.references, 1)
    assert resolved_1 is not None and resolved_1.chunk_id == "chunk-1"
    assert _resolve_reference(result.references, 2) is None
    resolved_3 = _resolve_reference(result.references, 3)
    assert resolved_3 is not None and resolved_3.chunk_id == "chunk-3"


def test_row_level_citation_helpers_keep_soft_contracts() -> None:
    """Row-level helper contracts are unchanged: unusable rows yield None/defaults."""
    assert parse_single_citation(_citation(source_id="not-a-uuid")) is None
    assert extract_text_passages([None, None, None, None, ["bad-passage"]]) == (
        None,
        None,
        None,
    )

    texts: list[str] = []
    collect_texts_from_nested([["malformed"]], texts)
    assert texts == []


def test_parse_single_citation_chunk_id_absent_keeps_citation_with_none_chunk() -> None:
    """An absent/empty/non-list chunk-id block legitimately yields ``chunk_id=None``.

    Regression guard for the #1389 ``chunk_block = cite[0]`` migration: the
    chunk-id slot is optional, so a missing or malformed leading block must NOT
    drop the citation — it keeps the reference and leaves ``chunk_id`` ``None``.
    """
    source_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    base = _citation(source_id=source_id)
    cite_inner = base[1]

    # Empty chunk-id block: present-but-empty list -> chunk_id stays None.
    empty_block = parse_single_citation([[], cite_inner])
    assert empty_block is not None
    assert empty_block.source_id == source_id
    assert empty_block.chunk_id is None

    # Non-list chunk-id block: still a valid citation, chunk_id None.
    non_list_block = parse_single_citation([None, cite_inner])
    assert non_list_block is not None
    assert non_list_block.chunk_id is None

    # Non-string leading element inside the block -> chunk_id None.
    non_str_leaf = parse_single_citation([[123], cite_inner])
    assert non_str_leaf is not None
    assert non_str_leaf.chunk_id is None

    # Sanity: the well-formed block still populates chunk_id (migration intact).
    populated = parse_single_citation(base)
    assert populated is not None
    assert populated.chunk_id == "chunk-1"


def test_uuid_max_recursion_logs_under_chat_logger(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="notebooklm._chat"):
        result = extract_uuid_from_nested([["aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"]], max_depth=0)

    assert result is None
    assert any(
        record.name == "notebooklm._chat" and "Max recursion depth" in record.message
        for record in caplog.records
    )


def test_non_json_inner_chunk_debug_log_is_guarded_and_uses_chat_logger(caplog) -> None:
    bad_chunk = json.dumps([["wrb.fr", "method_id", "{not valid json}"]])

    with caplog.at_level(logging.DEBUG, logger="notebooklm._chat"):
        text, is_answer, refs, conv_id = extract_answer_and_refs_from_chunk(bad_chunk)

    assert (text, is_answer, refs, conv_id) == (None, False, [], None)
    assert any(
        record.name == "notebooklm._chat" and "Stream parser" in record.message
        for record in caplog.records
    )


def test_user_displayable_error_payload_raises_same_chat_error_message() -> None:
    payload = [
        8,
        None,
        [["type.googleapis.com/google.rpc.UserDisplayableError", "details"]],
    ]

    with pytest.raises(
        ChatError,
        match=(
            "Chat request was rate limited or rejected by the API. "
            "Wait a few seconds and try again."
        ),
    ):
        raise_if_rate_limited(payload)


def _wrb_envelope(inner: Any) -> str:
    """Length-prefixed single-``wrb.fr``-frame body wrapping ``inner``."""
    return _length_prefixed(json.dumps([["wrb.fr", None, json.dumps(inner)]]))


@pytest.mark.parametrize(
    "drifted_inner",
    [
        ["scalar-answer-row"],  # answer row is a str (indexable but wrong type)
        [{"answer": "x"}],  # answer row became a dict
        [42],  # answer row became an int
        [None],  # answer row became null
    ],
)
def test_drifted_answer_row_raises(drifted_inner: Any) -> None:
    """A populated ``wrb.fr`` record whose answer row is not a list is drift.

    Strict decoding is the only mode (the ``NOTEBOOKLM_STRICT_DECODE=0``
    soft-mode opt-out was retired in v0.7.0), so this raises
    :class:`UnknownRPCMethodError` instead of silently collapsing to an empty
    answer — that silent collapse was the gap the
    ``architecture-gap-review`` flagged for ``_chat.wire`` (ADR-0011:38).
    """
    from notebooklm.exceptions import UnknownRPCMethodError

    with pytest.raises(UnknownRPCMethodError):
        parse_streaming_chat_response(_wrb_envelope(drifted_inner))


def test_empty_answer_row_is_tolerated_as_heartbeat() -> None:
    """A populated outer wrapping an empty answer row (``[[]]``) is a
    degenerate/heartbeat record, NOT drift — it returns an empty answer."""
    result = parse_streaming_chat_response(_wrb_envelope([[]]))
    assert result.answer == ""


def test_error_frame_surfaces_chat_error_with_code() -> None:
    """An ``"er"`` error frame must surface the embedded server error.

    The previous parser only inspected ``"wrb.fr"`` frames and silently
    skipped ``"er"`` frames, so a server-side chat error collapsed into the
    generic empty/parse-failure path. The parser now raises a
    :class:`ChatError` that echoes the embedded error code.
    """
    error_frame = json.dumps([["er", "GenerateFreeFormStreamed", 13, "internal boom"]])

    with pytest.raises(ChatError, match=r"server returned an error frame.*code 13"):
        parse_streaming_chat_response(_length_prefixed(error_frame))


def test_error_frame_without_code_still_surfaces_chat_error() -> None:
    """A short ``"er"`` frame (no code slot) still raises a ``ChatError``
    rather than being silently skipped."""
    error_frame = json.dumps([["er", "GenerateFreeFormStreamed"]])

    with pytest.raises(ChatError, match="server returned an error frame"):
        parse_streaming_chat_response(_length_prefixed(error_frame))


def test_oversized_request_rejection_surfaces_status_not_parse_error() -> None:
    """A null-inner ``wrb.fr`` with an item[5] status payload is a rejection.

    The server rejects an over-long question with a ``wrb.fr`` frame carrying no
    answer JSON (null item[2]) and a bare grpc-style status at item[5]
    (``[3]`` == ``INVALID_ARGUMENT``), followed by ``di`` / ``af.httprm`` / ``e``
    stream-bookkeeping frames. The parser used to skip the status frame and
    raise the generic "No parseable chunks" error, masking the real cause
    (discussion #1472). It now raises a :class:`ChatError` echoing the status.

    The body below is byte-for-byte the real captured rejection from #1472 — the
    ``["e", 4, None, None, 132]`` trailer is deliberately included to pin that it
    is NOT treated as the error (its trailing ``132`` is a response byte count,
    not an error code).
    """
    wire = _length_prefixed(
        json.dumps([["wrb.fr", None, None, None, None, [3]]]),
        json.dumps([["di", 198], ["af.httprm", 197, "-1460015816955467607", 6]]),
        json.dumps([["e", 4, None, None, 132]]),
    )

    with pytest.raises(ChatError, match=r"rejected by the server \(status 3\)"):
        parse_streaming_chat_response(wire)


def test_null_inner_frame_with_empty_payload_still_raises_without_status() -> None:
    """A null-inner ``wrb.fr`` with an empty ``[]`` payload is not swallowed.

    Guards the ``is not None`` (not truthy) check: a present-but-empty item[5]
    has no status to report, but the frame still carries no answer, so it must
    raise a rejection rather than collapse into "No parseable chunks".
    """
    wire = _length_prefixed(json.dumps([["wrb.fr", None, None, None, None, []]]))

    with pytest.raises(ChatError, match=r"rejected by the server\."):
        parse_streaming_chat_response(wire)


def test_e_terminator_frame_does_not_break_successful_answer() -> None:
    """A trailing ``"e"`` stream terminator must not turn a good answer into an error.

    Successful streamed responses end with an ``["e", n, None, None, bytes]``
    bookkeeping frame (the trailing number is a running byte count). Regression
    guard for discussion #1472: the ``"e"`` tag must stay inert so a valid
    answer parses normally instead of raising.
    """
    body = _length_prefixed(_chunk("Real answer."), json.dumps([["e", 15, None, None, 41687]]))

    result = parse_streaming_chat_response(body)

    assert result.answer == "Real answer."


def test_chat_wire_static_import_guard() -> None:
    forbidden = {
        "notebooklm",
        "notebooklm.client",
        "notebooklm._chat",
        "notebooklm._core",
        "notebooklm.rpc.overrides",
    }
    tree = ast.parse((SRC_ROOT / "_chat" / "wire.py").read_text(encoding="utf-8"))

    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level == 1 or node.level == 2:
                module = f"notebooklm.{module}" if module else "notebooklm"
            imports.add(module)
            for alias in node.names:
                imports.add(f"{module}.{alias.name}" if module else alias.name)

    violations = forbidden & imports
    assert not violations, f"_chat_wire.py imported forbidden modules: {violations}"


def test_chat_wire_runtime_import_does_not_request_forbidden_modules(monkeypatch) -> None:
    import notebooklm  # noqa: F401

    forbidden = {
        "notebooklm.client",
        "notebooklm._chat",
        "notebooklm._core",
        "notebooklm.rpc.overrides",
    }
    sys.modules.pop("notebooklm._chat.wire", None)
    real_import = builtins.__import__

    def guarded_import(
        name: str,
        globals_: dict[str, Any] | None = None,
        locals_: dict[str, Any] | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> Any:
        resolved = name
        if level:
            package = globals_.get("__package__") if globals_ else None
            if package:
                resolved = importlib.util.resolve_name(f"{'.' * level}{name}", package)
        candidates = {resolved}
        if fromlist:
            candidates.update(f"{resolved}.{item}" for item in fromlist)
        violations = forbidden & candidates
        if violations:
            raise AssertionError(f"_chat_wire imported forbidden modules {violations}")
        return real_import(name, globals_, locals_, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", guarded_import)

    module = importlib.import_module("notebooklm._chat.wire")
    assert module.__name__ == "notebooklm._chat.wire"


def test_chat_wire_and_chat_smoke_import_order() -> None:
    for name in ("notebooklm._chat", "notebooklm._chat.wire"):
        sys.modules.pop(name, None)
    protocol = importlib.import_module("notebooklm._chat.wire")
    chat = importlib.import_module("notebooklm._chat")
    assert protocol.__name__ == "notebooklm._chat.wire"
    assert chat.__name__ == "notebooklm._chat"

    for name in ("notebooklm._chat", "notebooklm._chat.wire"):
        sys.modules.pop(name, None)
    chat = importlib.import_module("notebooklm._chat")
    protocol = importlib.import_module("notebooklm._chat.wire")
    assert chat.__name__ == "notebooklm._chat"
    assert protocol.__name__ == "notebooklm._chat.wire"


def test_chat_module_keeps_only_delegating_stream_parser_wrappers() -> None:
    tree = ast.parse((SRC_ROOT / "_chat" / "api.py").read_text(encoding="utf-8"))
    wrapper_names = {
        "_parse_ask_response_with_references",
        "_extract_answer_and_refs_from_chunk",
        "_raise_if_rate_limited",
        "_parse_citations",
        "_parse_single_citation",
        "_extract_text_passages",
        "_collect_texts_from_nested",
        "_extract_uuid_from_nested",
    }
    expected_delegate = {
        "_parse_ask_response_with_references": "parse_streaming_chat_response",
        "_extract_answer_and_refs_from_chunk": "extract_answer_and_refs_from_chunk",
        "_raise_if_rate_limited": "raise_if_rate_limited",
        "_parse_citations": "parse_citations",
        "_parse_single_citation": "parse_single_citation",
        "_extract_text_passages": "extract_text_passages",
        "_collect_texts_from_nested": "collect_texts_from_nested",
        "_extract_uuid_from_nested": "extract_uuid_from_nested",
    }

    wrappers = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name in wrapper_names
    }
    assert set(wrappers) == wrapper_names

    for name, node in wrappers.items():
        constants = {child.value for child in ast.walk(node) if isinstance(child, ast.Constant)}
        assert "wrb.fr" not in constants, f"{name} owns streamed wrb.fr parsing"
        called_helpers = {
            child.func.id
            for child in ast.walk(node)
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Name)
        }
        called_helpers.update(
            child.func.attr
            for child in ast.walk(node)
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
        )
        assert expected_delegate[name] in called_helpers, f"{name} does not delegate"
        for child in ast.walk(node):
            assert not (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Attribute)
                and child.func.attr == "loads"
                and isinstance(child.func.value, ast.Name)
                and child.func.value.id == "json"
            ), f"{name} owns JSON streamed chunk parsing"
            assert not (
                name == "_extract_uuid_from_nested"
                and isinstance(child, ast.Call)
                and isinstance(child.func, ast.Attribute)
                and isinstance(child.func.value, ast.Name)
                and child.func.value.id == "self"
                and child.func.attr == "_extract_uuid_from_nested"
            ), "_chat.py owns local UUID recursion"
