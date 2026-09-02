"""Characterization tests for private streamed-chat protocol helpers."""

from __future__ import annotations

import ast
import builtins
import copy
import importlib
import importlib.util
import inspect
import json
import logging
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import pytest

from notebooklm import ConversationTurnKey, MagicArtifactType
from notebooklm._web.params.chat_stream import build_streaming_chat_request
from notebooklm._web.rows.chat_stream import (
    StreamingChatParseResult,
    extract_answer_and_refs_from_chunk,
    extract_fragment_range,
    extract_score,
    extract_text_passages,
    extract_uuid_from_nested,
    parse_citations,
    parse_single_citation,
    parse_streaming_chat_response,
    raise_if_rate_limited,
)
from notebooklm.exceptions import ChatError, RateLimitError, UnknownRPCMethodError
from notebooklm.rpc.types import get_query_url

SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "notebooklm"
PROJECT_ROOT = SRC_ROOT.parents[1]


def _run_import_probe(source: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", source],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


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
    response_doc: bool = True,
    conversation_id: str | None = None,
    citations: list[Any] | None = None,
    is_final: bool = True,
    turn_key: list[Any] | None = None,
    suggestions: list[list[Any]] | None = None,
) -> str:
    """Build one ``wrb.fr`` frame around a ``GenerateFreeFormStreamedResponse``.

    ``is_final`` fills ``inner_data[4]`` (``isFinalResponse``). It defaults to
    ``True`` because a single-chunk fixture stands in for a *complete* stream,
    and every live stream marks its last chunk — a helper defaulting to
    ``False`` would build a shape the backend never sends and would put every
    fixture on the parser's truncated-stream fallback path.
    """
    marker = 1 if marked else 0
    type_info = [[], None, None, citations or [], marker] if response_doc else None
    if turn_key is None and conversation_id is not None:
        turn_key = [conversation_id, 123]
    answer_row: list[Any] = [text, None, turn_key, None, type_info]
    inner_data: list[Any] = [answer_row, None, None, None, is_final]
    if suggestions is not None:
        inner_data.append([suggestions])
    inner_json = json.dumps(inner_data)
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
    fragment_start: int | None = None,
    fragment_end: int | None = None,
) -> list[Any]:
    """Build one ``DocumentObject`` citation entry in the shape the wire sends.

    The nesting is the live one, level for level (see
    ``tests/unit/fixtures/chat_answer_row_with_citations.json`` for the capture
    this mirrors): ``Citation.fragment`` is a *message* whose ``elements`` list
    sits one level below it, each element is a ``StructuralElement``
    ``[startIndex, endIndex, paragraph]``, and the leaf ``TextRun`` wraps its
    content in a list rather than being a bare string.

    That last detail is not pedantry: this helper used to place a bare string
    where the wire places ``[content]``, and the resulting fixture agreed with
    a decoder that also read it wrongly (#2120).
    """
    text_run = [text]
    paragraph_element = [start, end, text_run]
    paragraph = [[paragraph_element]]
    structural_element = [start, end, paragraph]
    fragment = [[structural_element]]
    return [
        [chunk_id],
        [
            None,
            None,
            score,
            [[None, fragment_start, fragment_end]] if fragment_start is not None else [[None]],
            fragment,
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
        "extract_fragment_range": inspect.signature(extract_fragment_range),
        "extract_score": inspect.signature(extract_score),
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
    # ``document`` is optional and defaults to ``None`` (#2120): the stream
    # parser passes the answer document it has already built, and a direct
    # single-argument call still works exactly as before.
    assert list(signatures["parse_citations"].parameters) == ["first", "document"]
    assert signatures["parse_citations"].parameters["document"].default is None
    assert list(signatures["parse_single_citation"].parameters) == ["cite"]
    assert list(signatures["extract_text_passages"].parameters) == ["cite_inner"]
    assert list(signatures["extract_fragment_range"].parameters) == ["cite_inner"]
    assert list(signatures["extract_score"].parameters) == ["cite_inner"]
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
    # Both chunks are marked final, so the winner here is the LAST final chunk
    # rather than the longest — the two coincide, and the point of this test is
    # the XSSI/length-prefix/raw-JSON mix, not the selection policy (#2122).
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
    import notebooklm._web.rows.chat_stream as chat_wire

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


def test_next_step_suggestions_decode_and_preserve_unknown_codes() -> None:
    result = parse_streaming_chat_response(
        _length_prefixed(
            _chunk(
                "Answer.",
                suggestions=[
                    ["What happens next?", 9],
                    ["A future suggestion", 99],
                    ["missing code"],
                ],
            )
        )
    )

    assert [(step.question, step.type_code) for step in result.next_steps] == [
        ("What happens next?", 9),
        ("A future suggestion", 99),
    ]
    assert result.next_steps[0].kind is MagicArtifactType.CONVERSATIONAL_TEXT_CHIP
    assert result.next_steps[1].kind is None


def test_next_steps_are_collected_from_a_textless_chunk() -> None:
    body = _length_prefixed(
        _chunk(
            "",
            response_doc=False,
            is_final=False,
            suggestions=[["Try this follow-up", 9]],
        ),
        _chunk("Final answer.", suggestions=None),
    )

    result = parse_streaming_chat_response(body)

    assert result.answer == "Final answer."
    assert [step.question for step in result.next_steps] == ["Try this follow-up"]


def test_unmarked_fallback_logs_under_chat_logger(caplog) -> None:
    response = _length_prefixed(_chunk("Only unmarked answer.", marked=False))

    with caplog.at_level(logging.WARNING, logger="notebooklm._chat"):
        result = parse_streaming_chat_response(response)

    assert result.answer == "Only unmarked answer."
    assert any(
        record.name == "notebooklm._chat" and "No marked answer found" in record.message
        for record in caplog.records
    )


def test_no_answer_without_response_doc_does_not_log_drift_warning(caplog) -> None:
    response = _length_prefixed(
        _chunk("The sources do not contain enough information.", response_doc=False)
    )

    with caplog.at_level(logging.WARNING, logger="notebooklm._chat"):
        result = parse_streaming_chat_response(response)

    assert result.answer == "The sources do not contain enough information."
    assert not any("No marked answer found" in record.message for record in caplog.records)


def test_row_truncated_before_the_reason_slot_still_warns(caplog) -> None:
    """A row too short to carry ``emptyAnswerReason`` is drift, not a clean no-answer.

    "No ``responseDoc``" only means "deliberate empty answer" for a row that COULD
    have said so. A single-element row cannot, so silencing it here would spend the
    one drift signal on a genuinely malformed payload.
    """
    inner_json = json.dumps([["No supported answer."]])
    response = _length_prefixed(json.dumps([["wrb.fr", None, inner_json]]))

    with caplog.at_level(logging.WARNING, logger="notebooklm._chat"):
        result = parse_streaming_chat_response(response)

    assert result.answer == "No supported answer."
    assert any("No marked answer found" in record.message for record in caplog.records)


def test_present_unmarked_doc_warns_even_when_absent_doc_text_is_longer(caplog) -> None:
    response = _length_prefixed(
        _chunk("Drift.", marked=False),
        _chunk("The sources do not contain enough information.", response_doc=False),
    )

    with caplog.at_level(logging.WARNING, logger="notebooklm._chat"):
        result = parse_streaming_chat_response(response)

    assert result.answer == "The sources do not contain enough information."
    assert any("No marked answer found" in record.message for record in caplog.records)


def test_malformed_present_response_doc_still_logs_drift_warning(caplog) -> None:
    inner_json = json.dumps([["Fallback text.", None, None, None, {}]])
    response = _length_prefixed(json.dumps([["wrb.fr", None, inner_json]]))

    with caplog.at_level(logging.WARNING, logger="notebooklm._chat"):
        result = parse_streaming_chat_response(response)

    assert result.answer == "Fallback text."
    assert any("No marked answer found" in record.message for record in caplog.records)


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
            fragment_start=100,
            fragment_end=200,
        ),
        _citation(
            source_id="11111111-2222-3333-4444-555555555555",
            chunk_id="chunk-2",
            text="second citation",
            start=12,
            end=27,
            score=0.7,
            fragment_start=200,
            fragment_end=350,
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
    assert [(ref.fragment_start_char, ref.fragment_end_char) for ref in result.references] == [
        (100, 200),
        (200, 350),
    ]
    # The deprecated aliases keep reporting the same values (#2120).
    assert [(ref.answer_start_char, ref.answer_end_char) for ref in result.references] == [
        (100, 200),
        (200, 350),
    ]
    assert [ref.score for ref in result.references] == [0.85, 0.7]


def test_extract_fragment_range_handles_well_formed_and_malformed_shapes() -> None:
    # Well-formed: [[None, start, end]]
    assert extract_fragment_range([None, None, None, [[None, 10, 20]]]) == (10, 20)
    # Zero-length but valid: end == start
    assert extract_fragment_range([None, None, None, [[None, 5, 5]]]) == (5, 5)
    # Missing outer: too short
    assert extract_fragment_range([None, None, None]) == (None, None)
    # Inner [None] only (server omitted positions)
    assert extract_fragment_range([None, None, None, [[None]]]) == (None, None)
    # Non-int positions
    assert extract_fragment_range([None, None, None, [[None, "10", "20"]]]) == (None, None)
    # Empty outer
    assert extract_fragment_range([None, None, None, []]) == (None, None)
    # Outer[0] not a list
    assert extract_fragment_range([None, None, None, ["bad"]]) == (None, None)
    # bool positions rejected (bool is int subclass in Python)
    assert extract_fragment_range([None, None, None, [[None, True, False]]]) == (None, None)
    # Partial range: end is None — paired check returns (None, None) not (10, None)
    assert extract_fragment_range([None, None, None, [[None, 10, None]]]) == (None, None)
    assert extract_fragment_range([None, None, None, [[None, None, 20]]]) == (None, None)
    # Negative start rejected
    assert extract_fragment_range([None, None, None, [[None, -1, 10]]]) == (None, None)
    # end < start rejected
    assert extract_fragment_range([None, None, None, [[None, 20, 10]]]) == (None, None)


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
    from notebooklm._chat import _resolve_reference

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
    # A fragment whose elements carry no usable range degrades the same way.
    assert extract_text_passages([None, None, None, None, [["malformed"]]]) == (
        None,
        None,
        None,
    )


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


def test_user_displayable_error_payload_surfaces_server_message_when_present() -> None:
    """``google.rpc.Status.message`` is surfaced ALONGSIDE the client guidance (#2188).

    No captured chat rejection has ever populated index 1 — the test above pins
    the wording users actually see. This one pins that a server-authored reason
    is appended rather than discarded, and that appending it does not cost the
    client's remedy.
    """
    payload = [
        8,
        "You have reached your daily chat limit",
        [["type.googleapis.com/google.rpc.UserDisplayableError", "details"]],
    ]

    with pytest.raises(ChatError) as exc_info:
        raise_if_rate_limited(payload)

    message = str(exc_info.value)
    assert "You have reached your daily chat limit" in message
    # APPENDED, not substituted: nobody has ever seen this slot populated, so
    # there is no evidence a server message would be as actionable as the
    # remedy it would displace (#2188).
    assert "Wait a few seconds and try again" in message


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
    ``architecture-gap-review`` flagged for the chat stream parser (ADR-0011:38).
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


def test_bare_rejection_surfaces_a_server_message_when_present() -> None:
    """A request-level rejection echoes the server's own reason (#2188).

    The captured #1472 rejection is a bare ``[3]`` with no message, so the
    generic guidance above is what users see; this pins the alternative.
    """
    wire = _length_prefixed(
        json.dumps([["wrb.fr", None, None, None, None, [3, "Question exceeds 100000 characters"]]])
    )

    with pytest.raises(ChatError) as exc_info:
        parse_streaming_chat_response(wire)

    message = str(exc_info.value)
    assert "Question exceeds 100000 characters" in message
    assert "status 3" in message
    # The client's guidance is kept alongside the server's words, not replaced.
    assert "shorten it and" in message


def test_message_without_a_status_is_still_surfaced() -> None:
    """``[None, "text"]`` — a reason with no code. The reason still reaches the user."""
    wire = _length_prefixed(
        json.dumps([["wrb.fr", None, None, None, None, [None, "Try a shorter question"]]])
    )

    with pytest.raises(ChatError) as exc_info:
        parse_streaming_chat_response(wire)

    message = str(exc_info.value)
    assert "Try a shorter question" in message
    # No code, so no "(status …)" detail is fabricated.
    assert "status" not in message.split("The server said:")[0].lower()


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


def test_error_only_e_terminator_surfaces_rate_limit() -> None:
    """A payload-free terminal frame is the Web chat-quota response.

    Android reports the same live account state as RESOURCE_EXHAUSTED. The
    sequence value remains diagnostic bookkeeping, not a status code: the
    quota response used 3, while successful streams carry values such as 15.
    """
    body = _length_prefixed(
        json.dumps([["di", 651], ["af.httprm", 651, "redacted-request", 15]]),
        json.dumps([["e", 3, None, None, 91]]),
    )

    with pytest.raises(RateLimitError) as raised:
        parse_streaming_chat_response(body)

    message = str(raised.value)
    assert "before the server returned an RPC payload" in message
    assert "terminal stream sequence 3" in message
    assert "rate limit" in message.lower()
    assert raised.value.rpc_code == "STREAM_TERMINATED_WITHOUT_RPC_PAYLOAD"
    assert "redacted-request" not in message


def test_chat_wire_static_import_guard() -> None:
    forbidden = {
        "notebooklm",
        "notebooklm.client",
        "notebooklm._chat",
        "notebooklm._core",
        "notebooklm._web.wire.overrides",
    }
    tree = ast.parse((SRC_ROOT / "_web" / "rows" / "chat_stream.py").read_text(encoding="utf-8"))

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
        "notebooklm._web.wire.overrides",
    }
    sys.modules.pop("notebooklm._web.rows.chat_stream", None)
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

    module = importlib.import_module("notebooklm._web.rows.chat_stream")
    assert module.__name__ == "notebooklm._web.rows.chat_stream"


def test_chat_wire_and_chat_smoke_import_order() -> None:
    for first, second in (
        ("notebooklm._web.rows.chat_stream", "notebooklm._chat"),
        ("notebooklm._chat", "notebooklm._web.rows.chat_stream"),
    ):
        _run_import_probe(
            f"""
import importlib
first = importlib.import_module({first!r})
second = importlib.import_module({second!r})
assert first.__name__ == {first!r}
assert second.__name__ == {second!r}
"""
        )


def test_chat_module_turn_helper_is_a_lazy_identity_shim() -> None:
    """The neutral module loads no Web parser until the legacy name is read."""
    _run_import_probe(
        """
import importlib
import sys
import notebooklm
module_name = "notebooklm._web.rows.chat_stream"
sys.modules.pop("notebooklm._chat", None)
sys.modules.pop(module_name, None)
chat = importlib.import_module("notebooklm._chat")
assert module_name not in sys.modules
helper = chat._extract_next_turn_content
parser = importlib.import_module(module_name)
assert helper is parser._extract_next_turn_content
"""
    )


def test_chat_module_keeps_only_delegating_stream_parser_wrappers() -> None:
    tree = ast.parse((SRC_ROOT / "_web" / "chat.py").read_text(encoding="utf-8"))
    wrapper_names = {
        "_parse_ask_response_with_references",
        "_extract_answer_and_refs_from_chunk",
        "_raise_if_rate_limited",
        "_parse_citations",
        "_parse_single_citation",
        "_extract_text_passages",
        "_extract_uuid_from_nested",
    }
    expected_delegate = {
        "_parse_ask_response_with_references": "parse_streaming_chat_response",
        "_extract_answer_and_refs_from_chunk": "extract_answer_and_refs_from_chunk",
        "_raise_if_rate_limited": "raise_if_rate_limited",
        "_parse_citations": "parse_citations",
        "_parse_single_citation": "parse_single_citation",
        "_extract_text_passages": "extract_text_passages",
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


# ---------------------------------------------------------------------------
# isFinalResponse-driven answer selection + ConversationTurnKey (#2122)
# ---------------------------------------------------------------------------

#: Live five-chunk ``GenerateFreeFormStreamedResponse`` capture, re-serialized
#: into the length-prefixed stream body the parser is fed. Built from the same
#: fixture ``test_chat_row_adapter.py`` pins the adapters against, so the
#: end-to-end assertions below are made against a real backend stream rather
#: than the ``_chunk`` builder's idea of one.
_CAPTURED_CHUNKS: list[Any] = json.loads(
    (Path(__file__).parent / "fixtures" / "chat_stream_final_response.json").read_text(
        encoding="utf-8"
    )
)["chunks"]


def _captured_stream_body() -> str:
    return _length_prefixed(
        *(json.dumps([["wrb.fr", None, json.dumps(chunk)]]) for chunk in _CAPTURED_CHUNKS)
    )


def test_captured_stream_selects_the_final_chunk_and_carries_its_turn_key(caplog) -> None:
    """End-to-end against the live capture: the answer parses, the turn key is
    decoded, and no fallback diagnostic fires."""
    with caplog.at_level(logging.DEBUG, logger="notebooklm._chat"):
        result = parse_streaming_chat_response(_captured_stream_body())

    assert result.answer == "A task wraps a **coroutine** [1]."
    assert result.turn_key == ConversationTurnKey(
        session_id="3afea005-7d13-41d0-9257-6a9e28597818",
        turn_id="b38d4003-5be1-487d-a121-5c5958709021",
        turn_code=2187103311,
    )
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_final_marker_beats_a_longer_earlier_chunk() -> None:
    """The failure #2122 describes: a corrected/truncated final chunk that is
    SHORTER than an earlier one. Longest-wins returns the stale mid-stream text;
    the server's own marker returns the answer it ended on."""
    body = _length_prefixed(
        _chunk("A long provisional answer that the model later cut down.", is_final=False),
        _chunk("Short corrected answer.", is_final=True),
    )
    assert parse_streaming_chat_response(body).answer == "Short corrected answer."


def test_final_marker_selection_is_logged_when_it_changes_the_outcome(caplog) -> None:
    with caplog.at_level(logging.DEBUG, logger="notebooklm._chat"):
        parse_streaming_chat_response(
            _length_prefixed(
                _chunk("A long provisional answer that was later cut down.", is_final=False),
                _chunk("Short corrected answer.", is_final=True),
            )
        )
    assert any("differs from the longest marked chunk" in r.message for r in caplog.records)


def test_final_marker_agreeing_with_longest_logs_nothing(caplog) -> None:
    """The ordinary stream — final chunk IS the longest — stays silent."""
    with caplog.at_level(logging.DEBUG, logger="notebooklm._chat"):
        parse_streaming_chat_response(
            _length_prefixed(
                _chunk("Partial", is_final=False),
                _chunk("Partial answer, complete.", is_final=True),
            )
        )
    assert [r for r in caplog.records if r.name.startswith("notebooklm")] == []


def test_no_final_marker_falls_back_to_longest_and_warns(caplog) -> None:
    """A stream that never marks a final chunk (truncated, or the flag moved)
    still answers — from the heuristic — but says so at WARNING."""
    with caplog.at_level(logging.DEBUG, logger="notebooklm._chat"):
        result = parse_streaming_chat_response(
            _length_prefixed(
                _chunk("Short", is_final=False),
                _chunk("The longest chunk in this stream.", is_final=False),
            )
        )
    assert result.answer == "The longest chunk in this stream."
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "isFinalResponse" in warnings[0].message


def test_final_chunk_without_text_falls_back_to_longest_and_warns(caplog) -> None:
    """ "The final chunk carried no answer" must not return an empty answer.

    And it must be diagnosed as ITSELF, not as the truncated-stream case: a
    stream that completed and said nothing in its last chunk is a different
    operator problem from one whose final marker never arrived. This is the
    only consumer of ``is_final_response`` on a text-less chunk, so without it
    that propagation would be dead code the docstrings claim is live.
    """
    with caplog.at_level(logging.DEBUG, logger="notebooklm._chat"):
        result = parse_streaming_chat_response(
            _length_prefixed(
                _chunk("The only chunk with text.", is_final=False),
                _chunk("", is_final=True),
            )
        )
    assert result.answer == "The only chunk with text."
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "carried no answer text" in warnings[0].message


def test_the_two_fallback_causes_are_diagnosed_differently(caplog) -> None:
    """A truncated stream and an empty final chunk must not share a message."""
    with caplog.at_level(logging.WARNING, logger="notebooklm._chat"):
        parse_streaming_chat_response(
            _length_prefixed(
                _chunk("Short", is_final=False),
                _chunk("The longest chunk in this stream.", is_final=False),
            )
        )
    truncated = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert len(truncated) == 1
    assert "No chunk carried isFinalResponse" in truncated[0]
    assert "carried no answer text" not in truncated[0]


def test_final_unmarked_chunk_does_not_beat_a_marked_answer() -> None:
    """``isFinalResponse`` says "last chunk", not "this is an answer" — the
    answer marker still decides what counts as an answer at all."""
    body = _length_prefixed(
        _chunk("The marked answer.", marked=True, is_final=False),
        _chunk("Unmarked trailing text.", marked=False, is_final=True),
    )
    assert parse_streaming_chat_response(body).answer == "The marked answer."


def test_last_final_marked_chunk_wins_when_two_claim_finality() -> None:
    """Not an observed shape; the tie has to break somewhere, and "final" is a
    position claim, so the later chunk is the later position."""
    body = _length_prefixed(
        _chunk("First claim of finality.", is_final=True),
        _chunk("Second claim of finality.", is_final=True),
    )
    assert parse_streaming_chat_response(body).answer == "Second claim of finality."


def test_stream_without_a_turn_key_reports_none() -> None:
    """Most hand-built and legacy shapes carry no key; that is absence, not
    an error."""
    result = parse_streaming_chat_response(_length_prefixed(_chunk("Answer.")))
    assert result.turn_key is None
    assert result.answer == "Answer."


def test_turn_key_survives_a_losing_chunk() -> None:
    """The key is collected independently of which chunk wins the answer.

    Only the LOSING chunk carries a key here, so this fails if collection is
    ever gated on the winning chunk (or on a chunk bearing text). That is the
    live shape: chunk 1 of every observed stream carries the full key and no
    answer text.
    """
    body = _length_prefixed(
        _chunk("Partial", turn_key=["conv-uuid", "turn-uuid", 42], is_final=False),
        _chunk("Partial answer, complete.", turn_key=None),
    )
    assert parse_streaming_chat_response(body).turn_key == ConversationTurnKey(
        "conv-uuid", "turn-uuid", 42
    )


def test_last_chunk_to_carry_a_turn_key_wins() -> None:
    """Two DIFFERENT keys pins the collector's last-wins rule.

    Not an observed shape — the key was identical on every chunk of every
    observed turn — but the rule has to be pinned or a future first-wins edit
    is invisible.
    """
    body = _length_prefixed(
        _chunk("Partial", turn_key=["conv-uuid", "turn-EARLY", 1], is_final=False),
        _chunk("Partial answer, complete.", turn_key=["conv-uuid", "turn-LATE", 2]),
    )
    assert parse_streaming_chat_response(body).turn_key == ConversationTurnKey(
        "conv-uuid", "turn-LATE", 2
    )


def test_empty_answer_stream_still_reports_no_answer(caplog) -> None:
    """A genuinely empty answer stays an empty answer (not a parse failure) —
    the final-marker path must not change that contract."""
    with caplog.at_level(logging.DEBUG, logger="notebooklm._chat"):
        result = parse_streaming_chat_response(_length_prefixed(_chunk("", is_final=True)))
    assert result.answer == ""
    assert any("No answer extracted" in r.message for r in caplog.records)


def test_answer_document_follows_the_same_chunk_as_the_answer() -> None:
    """The answer and its document must come from ONE chunk (#2120 + #2122).

    Built from the live capture: the final chunk is kept verbatim (33 chars,
    one decodable block) while an earlier, longer chunk is stripped of its
    document. If the selection ever took the answer from one chunk and the
    document from another, the annotation offsets would index a string the
    caller never receives.
    """
    final_chunk = copy.deepcopy(_CAPTURED_CHUNKS[-1])
    longer_chunk = copy.deepcopy(final_chunk)
    longer_chunk[4] = False  # isFinalResponse
    longer_chunk[0][0] = "A much longer provisional answer the model later cut down."
    longer_chunk[0][4][0] = None  # drop the responseDoc body

    result = parse_streaming_chat_response(
        _length_prefixed(
            *(
                json.dumps([["wrb.fr", None, json.dumps(chunk)]])
                for chunk in (longer_chunk, final_chunk)
            )
        )
    )

    assert result.answer == "A task wraps a **coroutine** [1]."
    assert result.answer_document.text == "A task wraps a coroutine."


def _multi_item_chunk(*items: tuple[str, bool]) -> str:
    """One stream line carrying SEVERAL ``wrb.fr`` frames.

    ``items`` is ``(text, is_final)`` per frame; ``text=""`` builds the real
    text-less shape the backend sends (a populated answer row whose text slot
    is the empty string — chunk 1 of every observed stream), NOT an empty row.
    Real streams put one frame per LINE, so the multi-frame line itself is
    unobserved — which is why the parser's behaviour on it needs pinning
    rather than assuming.
    """
    frames = []
    for text, is_final in items:
        row = [text, None, ["conv-uuid", "turn-uuid", 7], None, [[], None, None, [], 1]]
        inner = [row, None, None, None, is_final]
        frames.append(["wrb.fr", None, json.dumps(inner)])
    return json.dumps(frames)


def test_final_flag_is_read_from_the_envelope_that_carried_the_answer() -> None:
    """A later frame's ``isFinalResponse`` must not be attributed to an earlier
    frame's answer.

    The parser returns at the first frame that yields text, so a `true` on a
    LATER frame is never seen — and a `true` on an EARLIER, text-less frame
    must not be borrowed by this one either. Both directions land on the
    fallback here, which is the conservative outcome.
    """
    body = _length_prefixed(_multi_item_chunk(("The answer.", False), ("", True)))
    result = parse_streaming_chat_response(body)
    assert result.answer == "The answer."


def test_turn_key_is_kept_from_a_chunk_that_carried_no_answer_text() -> None:
    """An empty answer still belongs to a turn (#2122).

    The key is read above the text gate, so a stream whose only chunks are
    text-less still yields it — otherwise the turn a caller most wants to give
    feedback on would be the one turn they could not address.
    """
    result = parse_streaming_chat_response(_length_prefixed(_multi_item_chunk(("", True))))
    assert result.answer == ""
    assert result.turn_key == ConversationTurnKey("conv-uuid", "turn-uuid", 7)


def test_empty_answer_stream_from_the_capture_still_reports_its_turn_key() -> None:
    """Same property against the live capture: chunk 1 has no text but does
    carry the key, so truncating the stream to it must not lose the key."""
    first_only = copy.deepcopy(_CAPTURED_CHUNKS[0])
    result = parse_streaming_chat_response(
        _length_prefixed(json.dumps([["wrb.fr", None, json.dumps(first_only)]]))
    )
    assert result.answer == ""
    assert result.turn_key is not None
    assert result.turn_key.session_id == "3afea005-7d13-41d0-9257-6a9e28597818"
