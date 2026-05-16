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

from notebooklm._chat_protocol import (
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
from notebooklm.exceptions import ChatError
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


def test_empty_response_returns_empty_result_and_logs_under_chat_logger(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="notebooklm._chat"):
        result = parse_streaming_chat_response("")

    assert result == StreamingChatParseResult("", [], None)
    assert any(
        record.name == "notebooklm._chat" and "No answer extracted" in record.message
        for record in caplog.records
    )


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


def test_missing_and_malformed_citation_shapes_degrade_without_raising() -> None:
    assert parse_citations(["Answer", None, None, None]) == []
    assert parse_citations(["Answer", None, None, None, [[], None, None, None, 1]]) == []
    assert parse_single_citation(_citation(source_id="not-a-uuid")) is None
    assert extract_text_passages([None, None, None, None, ["bad-passage"]]) == (
        None,
        None,
        None,
    )

    texts: list[str] = []
    collect_texts_from_nested([["malformed"]], texts)
    assert texts == []


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


def test_chat_protocol_static_import_guard() -> None:
    forbidden = {
        "notebooklm",
        "notebooklm.client",
        "notebooklm._chat",
        "notebooklm._core",
        "notebooklm.rpc.overrides",
    }
    tree = ast.parse((SRC_ROOT / "_chat_protocol.py").read_text(encoding="utf-8"))

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
    assert not violations, f"_chat_protocol.py imported forbidden modules: {violations}"


def test_chat_protocol_runtime_import_does_not_request_forbidden_modules(monkeypatch) -> None:
    import notebooklm  # noqa: F401

    forbidden = {
        "notebooklm.client",
        "notebooklm._chat",
        "notebooklm._core",
        "notebooklm.rpc.overrides",
    }
    sys.modules.pop("notebooklm._chat_protocol", None)
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
            raise AssertionError(f"_chat_protocol imported forbidden modules {violations}")
        return real_import(name, globals_, locals_, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", guarded_import)

    module = importlib.import_module("notebooklm._chat_protocol")
    assert module.__name__ == "notebooklm._chat_protocol"


def test_chat_protocol_and_chat_smoke_import_order() -> None:
    for name in ("notebooklm._chat", "notebooklm._chat_protocol"):
        sys.modules.pop(name, None)
    protocol = importlib.import_module("notebooklm._chat_protocol")
    chat = importlib.import_module("notebooklm._chat")
    assert protocol.__name__ == "notebooklm._chat_protocol"
    assert chat.__name__ == "notebooklm._chat"

    for name in ("notebooklm._chat", "notebooklm._chat_protocol"):
        sys.modules.pop(name, None)
    chat = importlib.import_module("notebooklm._chat")
    protocol = importlib.import_module("notebooklm._chat_protocol")
    assert chat.__name__ == "notebooklm._chat"
    assert protocol.__name__ == "notebooklm._chat_protocol"


def test_chat_module_keeps_only_delegating_stream_parser_wrappers() -> None:
    tree = ast.parse((SRC_ROOT / "_chat.py").read_text(encoding="utf-8"))
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
