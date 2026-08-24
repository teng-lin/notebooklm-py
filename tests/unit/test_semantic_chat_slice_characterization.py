"""Migration sentinels for the P6.1 chat slice (session/history/ask/clear).

P6.1 is the first P6 domain and carries contracts no other domain has.  The
plan states them in prose and the P0 catalog row for ``chat.ask`` records them
declaratively; these tests make each one fail *executably* if a codec /
projector / stream-protocol split changes it.

Nothing here asserts a desired post-migration shape.  Every assertion is
current production behavior that a migration PR must reproduce, and each test
names the contract it pins:

1. citation anchors index ``answer_document.text``, never the markdown answer;
2. ``AskResult.raw_response`` is the first 1000 chars, verbatim;
3. ``ask`` is unary — no public stream/iterator/context-manager surface;
4. ``chat_response_max_bytes`` aborts mid-stream, **pre-decode**, with
   ``bytes_read > limit_bytes`` and ``RPCResponseTooLargeError``;
5. ``ask`` is two-phase and all-or-nothing — a phase-2 miss raises
   ``ChatError`` *after* an ERROR-level audit log carrying the answer;
6. the loop-affinity guard fires before any lock or I/O, and a cancel between
   the two phases is accepted behavior that strands a server-recorded turn;
7. every chat public call issues its exact declared ``GET_NOTEBOOK`` count.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from notebooklm._backend import BackendDeadlineExceededError
from notebooklm._chat import ChatAPI
from notebooklm._deadline import RuntimeDeadline
from notebooklm._notebook_payloads import build_get_notebook_params
from notebooklm._records import ChatAskInput
from notebooklm._streaming_post import stream_post_with_size_cap
from notebooklm._transport_errors import TransportAuthExpired, TransportServerError
from notebooklm._web.codec import chat as chat_codec
from notebooklm.exceptions import ChatError, NetworkError, RPCResponseTooLargeError
from notebooklm.rpc import ChatGoal, RPCMethod
from notebooklm.types import AskResult, ChatMode
from scripts import audit_operation_catalog as catalog
from tests._fixtures.web_backend import build_web_backend

FIXTURES_DIR = Path(__file__).parent / "fixtures"

#: Live capture reused from ``test_citation_alignment.py``: a 536-char markdown
#: answer whose document is 476 chars, with a three-entry annotation map.
ANSWER_ROW: list[Any] = json.loads(
    (FIXTURES_DIR / "chat_answer_row_with_citations.json").read_text()
)

_DEFAULT_RPC_RESULTS: dict[RPCMethod, Any] = {
    # ``notebook_info[1]`` is the sources slot; empty keeps ``get_source_ids``
    # on its happy path without inventing source rows this slice does not test.
    RPCMethod.GET_NOTEBOOK: [[["nb-1"], [], None, None, None, None, None, None]],
    RPCMethod.GET_CONVERSATION_TURNS: [[]],
    RPCMethod.GET_LAST_CONVERSATION_ID: [[["conv-1"]]],
    RPCMethod.DELETE_CONVERSATION: [],
    RPCMethod.RENAME_NOTEBOOK: None,
}


class _RpcRecorder:
    """Records every ``RPCMethod`` dispatched, so recency counts are exact."""

    def __init__(self, **overrides: Any) -> None:
        self.calls: list[RPCMethod] = []
        self._results = dict(_DEFAULT_RPC_RESULTS)
        for name, value in overrides.items():
            self._results[RPCMethod[name]] = value

    async def rpc_call(self, method: RPCMethod, params: Any, **kwargs: Any) -> Any:
        self.calls.append(method)
        result = self._results[method]
        if isinstance(result, BaseException):
            raise result
        return result

    def count(self, method: RPCMethod) -> int:
        return self.calls.count(method)


class _NotebookSources:
    def __init__(self, rpc: _RpcRecorder) -> None:
        self._rpc = rpc

    async def get_source_ids(self, notebook_id: str) -> list[str]:
        await self._rpc.rpc_call(
            RPCMethod.GET_NOTEBOOK,
            build_get_notebook_params(notebook_id),
            source_path=f"/notebook/{notebook_id}",
        )
        return []


def _stream_body(answer_row: list[Any]) -> bytes:
    """A minimal ``GenerateFreeFormStreamed`` body carrying one answer row."""
    return (
        ")]}'\n\n"
        + json.dumps([["wrb.fr", None, json.dumps([answer_row]), None, None, None, "generic"]])
    ).encode()


def _response(body: bytes) -> httpx.Response:
    return httpx.Response(
        200,
        request=httpx.Request("POST", "https://example.test/chat"),
        content=body,
    )


def _chat(
    *,
    rpc: _RpcRecorder | None = None,
    transport: Any = None,
    body: bytes | None = None,
    loop_guard: Any = None,
    chat_response_max_bytes: int | None = None,
) -> ChatAPI:
    if transport is None:
        transport = SimpleNamespace(
            perform_authed_post=AsyncMock(return_value=_response(body or _stream_body(ANSWER_ROW)))
        )
    rpc = rpc if rpc is not None else _RpcRecorder()
    reqid = SimpleNamespace(next_reqid=AsyncMock(return_value=100000))
    backend = build_web_backend(
        rpc,
        chat_transport=transport,
        chat_reqid=reqid,
        chat_timeout=45.0,
        chat_response_max_bytes=chat_response_max_bytes,
    )
    return ChatAPI(
        backend=backend,
        loop_guard=loop_guard or SimpleNamespace(assert_bound_loop=lambda: None),
        notebooks=_NotebookSources(rpc),
    )


# ---------------------------------------------------------------------------
# 1-3. Public shape: unary ask, document-indexed anchors, truncated preview
# ---------------------------------------------------------------------------


def test_ask_is_unary_and_exposes_no_public_streaming_surface() -> None:
    """Principle 2's stream protocol must not surface a public generator.

    ``ChatAPI.ask`` is a coroutine returning one ``AskResult``.  Making it an
    async generator, or adding a public ``stream``/``__aiter__`` sibling, is
    what would make a partial ``AskResult`` representable — see the phase-2
    test below for why that matters.
    """
    assert list(inspect.signature(ChatAPI.ask).parameters) == [
        "self",
        "notebook_id",
        "question",
        "source_ids",
        "conversation_id",
    ]
    assert inspect.signature(ChatAPI.ask).return_annotation == "AskResult"
    assert inspect.iscoroutinefunction(ChatAPI.ask)
    assert not inspect.isasyncgenfunction(ChatAPI.ask)
    public = {name for name in dir(ChatAPI) if not name.startswith("_")}
    assert not {name for name in public if "stream" in name or "iter" in name}
    assert not hasattr(ChatAPI, "__aiter__")


@pytest.mark.asyncio
async def test_ask_anchors_index_the_answer_document_not_the_markdown_answer() -> None:
    """``tests/unit/test_citation_alignment.py``'s invariant, at the facade.

    The parse-level gate proves the decoder stamps the anchors; this proves
    they still resolve against ``AskResult.answer_document`` after the result
    is assembled.  A projector that rebuilt ``AskResult`` from a neutral record
    while re-deriving offsets from ``answer`` would pass every parse test and
    fail here.
    """
    result = await _chat().ask("nb-1", "Q?", conversation_id="conv-1")

    assert isinstance(result, AskResult)
    assert len(result.answer) == 536
    assert len(result.answer_document.text) == 476
    anchors = [(r.answer_anchor_start, r.answer_anchor_end) for r in result.references]
    assert anchors == [(85, 85), (219, 235), (328, 328)]
    # The one spanning anchor resolves to real answer-document text...
    assert result.answer_document.slice(219, 235) == "into electricity"
    # ...and to something else entirely when read against ``answer``.
    assert result.answer[219:235] != "into electricity"


@pytest.mark.asyncio
async def test_ask_raw_response_is_the_verbatim_first_1000_chars() -> None:
    """The second field (with ``RPCError.raw_response``) exempt from the
    "no response bodies" rule, because it is already truncated.

    Verbatim matters: the preview is a prefix of the *undecoded* body, so a
    record-based rebuild that re-serialized the decoded payload would keep the
    length and change every character.
    """
    body = _stream_body(ANSWER_ROW)
    assert len(body) > 1000  # the capture is large enough to be truncated
    result = await _chat(body=body).ask("nb-1", "Q?", conversation_id="conv-1")

    assert len(result.raw_response) == 1000
    assert result.raw_response == body.decode()[:1000]


@pytest.mark.asyncio
async def test_chat_backend_forwards_one_deadline_and_clamps_stream_attempt() -> None:
    transport = SimpleNamespace(
        perform_authed_post=AsyncMock(return_value=_response(_stream_body(ANSWER_ROW)))
    )
    chat = _chat(transport=transport)
    deadline = RuntimeDeadline(timeout=5.0, started_at=10.0, monotonic=lambda: 12.0)

    result = await chat._service.ask(
        ChatAskInput(
            notebook_id="nb-1",
            question="Q?",
            source_ids=("source-1",),
            resolved_conversation_id="conv-1",
        ),
        deadline=deadline,
    )

    assert result.conversation_id == "conv-1"
    kwargs = transport.perform_authed_post.await_args.kwargs
    assert kwargs["retry_deadline"] is deadline
    assert kwargs["read_timeout"] == 3.0


@pytest.mark.asyncio
async def test_chat_backend_marks_expiry_after_stream_commit_as_outcome_unknown() -> None:
    transport = SimpleNamespace(
        perform_authed_post=AsyncMock(return_value=_response(_stream_body(ANSWER_ROW)))
    )
    chat = _chat(transport=transport)
    times = iter((10.0, 10.0, 16.0, 16.0))
    deadline = RuntimeDeadline(timeout=5.0, started_at=10.0, monotonic=lambda: next(times))

    with pytest.raises(BackendDeadlineExceededError) as caught:
        await chat._service.ask(
            ChatAskInput(
                notebook_id="nb-1",
                question="Q?",
                source_ids=("source-1",),
                resolved_conversation_id="conv-1",
            ),
            deadline=deadline,
        )

    assert caught.value.outcome_unknown is True
    assert caught.value.diagnostics == {
        "timeout": 5.0,
        "remaining": 0.0,
        "timeout_seconds": 5.0,
    }


@pytest.mark.asyncio
async def test_chat_deadline_clamped_read_timeout_maps_to_semantic_expiry() -> None:
    request = httpx.Request("POST", "https://notebooklm.google.com/chat")
    original = httpx.ReadTimeout("stream stalled", request=request)
    transport = SimpleNamespace(
        perform_authed_post=AsyncMock(
            side_effect=TransportServerError("transport failed", original=original)
        )
    )
    chat = _chat(transport=transport)
    times = iter((10.0, 10.0, 16.0, 16.0))
    deadline = RuntimeDeadline(timeout=5.0, started_at=10.0, monotonic=lambda: next(times))

    with pytest.raises(BackendDeadlineExceededError) as caught:
        await chat._service.ask(
            ChatAskInput(
                notebook_id="nb-1",
                question="Q?",
                source_ids=("source-1",),
                resolved_conversation_id="conv-1",
            ),
            deadline=deadline,
        )

    assert caught.value.outcome_unknown is True
    assert caught.value.diagnostics == {
        "timeout": 5.0,
        "remaining": 0.0,
        "timeout_seconds": 5.0,
    }


@pytest.mark.asyncio
async def test_chat_phase_two_predispatch_expiry_is_outcome_unknown() -> None:
    transport = SimpleNamespace(
        perform_authed_post=AsyncMock(return_value=_response(_stream_body(ANSWER_ROW)))
    )
    rpc = _RpcRecorder()
    chat = _chat(rpc=rpc, transport=transport)
    times = iter((10.0, 10.0, 10.0, 16.0))
    deadline = RuntimeDeadline(timeout=5.0, started_at=10.0, monotonic=lambda: next(times))

    with pytest.raises(BackendDeadlineExceededError) as caught:
        await chat._service.ask(
            ChatAskInput(
                notebook_id="nb-1",
                question="Q?",
                source_ids=("source-1",),
            ),
            deadline=deadline,
        )

    assert caught.value.outcome_unknown is True
    assert rpc.count(RPCMethod.GET_LAST_CONVERSATION_ID) == 0


@pytest.mark.asyncio
async def test_chat_facade_preserves_bounded_network_error_graph_and_scrubs_url() -> None:
    request = httpx.Request(
        "POST",
        "https://notebooklm.google.com/chat?f.sid=secret-session&authuser=owner@example.com",
    )
    original = httpx.ReadTimeout("stream stalled", request=request)
    transport_error = TransportServerError("transport failed", original=original)
    transport = SimpleNamespace(perform_authed_post=AsyncMock(side_effect=transport_error))
    chat = _chat(transport=transport)

    with pytest.raises(NetworkError) as caught:
        await chat.ask("nb-1", "Q?", source_ids=["source-1"], conversation_id="conv-1")

    projected = caught.value
    assert isinstance(projected.original_error, httpx.ReadTimeout)
    assert isinstance(projected.__cause__, TransportServerError)
    assert projected.__cause__.original is projected.original_error
    assert projected.__context__ is projected.__cause__
    assert projected.__suppress_context__ is True
    assert projected.original_error.request.url.query == b""
    rendered = repr(projected.original_error.request.url)
    assert "secret-session" not in rendered
    assert "owner@example.com" not in rendered


@pytest.mark.asyncio
async def test_chat_facade_preserves_chat_error_transport_cause() -> None:
    request = httpx.Request("POST", "https://notebooklm.google.com/chat?at=secret-csrf")
    response = httpx.Response(401, request=request)
    original = httpx.HTTPStatusError("unauthorized", request=request, response=response)
    transport_error = TransportAuthExpired("refresh failed", original=original)
    transport = SimpleNamespace(perform_authed_post=AsyncMock(side_effect=transport_error))
    chat = _chat(transport=transport)

    with pytest.raises(ChatError) as caught:
        await chat.ask("nb-1", "Q?", source_ids=["source-1"], conversation_id="conv-1")

    assert isinstance(caught.value.__cause__, TransportAuthExpired)
    projected_original = caught.value.__cause__.original
    assert isinstance(projected_original, httpx.HTTPStatusError)
    assert projected_original.response.status_code == 401
    assert projected_original.request.url.query == b""
    assert "secret-csrf" not in repr(projected_original.request.url)


# ---------------------------------------------------------------------------
# 4. The byte cap fires mid-stream, pre-decode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_byte_cap_aborts_pre_decode_with_bytes_read_over_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All three observables move if chat routes through a decoded-record protocol.

    ``chat_response_max_bytes`` is validated in ``_client_composition`` but
    enforced in ``_streaming_post`` on the raw buffered byte total: the read
    loop aborts early, ``bytes_read`` is documented as strictly greater than
    ``limit_bytes``, and the failure is ``RPCResponseTooLargeError`` — not a
    decode-stage error.  This wires ``ask`` to the real enforcement point so
    the cap cannot silently move behind the decoder.
    """
    cap = 1024
    chunks_yielded = 0
    decoded = False

    class _Streamed:
        status_code = 200
        headers: dict[str, str] = {}
        request = httpx.Request("POST", "https://example.test/chat")

        def raise_for_status(self) -> None:
            return None

        async def aiter_bytes(self) -> Any:
            nonlocal chunks_yielded
            for _ in range(8):
                chunks_yielded += 1
                yield b"y" * (cap // 2)

    @asynccontextmanager
    async def fake_stream(method: str, url: str, **kwargs: Any) -> Any:
        yield _Streamed()

    def _decoder_tripwire(text: str) -> Any:  # pragma: no cover - must never run
        nonlocal decoded
        decoded = True
        raise AssertionError("the byte cap must abort before any decode")

    monkeypatch.setattr(chat_codec, "parse_streaming_chat_response", _decoder_tripwire)

    http_client = httpx.AsyncClient()
    monkeypatch.setattr(http_client, "stream", fake_stream)
    seen: dict[str, Any] = {}

    async def perform_authed_post(*, max_response_bytes: int | None = None, **kwargs: Any) -> Any:
        seen["max_response_bytes"] = max_response_bytes
        return await stream_post_with_size_cap(
            http_client,
            "https://example.test/chat",
            body=b"",
            headers=None,
            max_bytes=max_response_bytes,
        )

    chat = _chat(
        transport=SimpleNamespace(perform_authed_post=perform_authed_post),
        chat_response_max_bytes=cap,
    )
    try:
        with pytest.raises(RPCResponseTooLargeError) as excinfo:
            await chat.ask("nb-1", "Q?", conversation_id="conv-1")
    finally:
        await http_client.aclose()

    assert seen["max_response_bytes"] == cap
    assert excinfo.value.limit_bytes == cap
    assert excinfo.value.bytes_read is not None
    assert excinfo.value.bytes_read > cap  # documented: strictly greater
    assert chunks_yielded < 8  # aborted the live connection, did not buffer on
    assert decoded is False


# ---------------------------------------------------------------------------
# 5. Two-phase, all-or-nothing, with an ERROR audit log
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ask_phase_two_miss_raises_after_logging_the_answer_at_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No partial ``AskResult`` is reachable, and the answer survives the raise.

    The streamed POST's per-stream conversation id is deliberately discarded
    (live testing proved it is not a real id), so a phase-2 miss has no id to
    fall back on.  The ERROR log is a documented side effect — it is the only
    place the paid-for answer survives — and the raise is what keeps an empty
    ``conversation_id`` from being representable.

    What survives is a **500-character preview plus the exact character count**,
    not the whole answer: the plan and ``ask``'s docstring both say "the full
    answer text is logged", and the code logs ``answer_text[:500]``.  Pinned as
    it behaves, so a migration reproduces the real audit trail.
    """
    rpc = _RpcRecorder(GET_LAST_CONVERSATION_ID=[])  # hPTbtc knows no conversation
    chat = _chat(rpc=rpc)
    answer = ANSWER_ROW[0]
    assert len(answer) == 536

    with (
        caplog.at_level(logging.ERROR, logger="notebooklm._chat.api"),
        pytest.raises(ChatError, match="did not register a conversation"),
    ):
        await chat.ask("nb-1", "Q?")

    (record,) = [r for r in caplog.records if r.levelno == logging.ERROR]
    message = record.getMessage()
    assert "536 chars" in message
    assert repr(answer[:500]) in message
    assert answer[500:] not in message  # a preview, not the whole answer
    # All-or-nothing: nothing partial was cached, and the discarded per-stream
    # id never became a conversation id.
    assert chat.cache_size() == 0
    # Phase 1 (POST) happened; phase 2 ran twice — the pre-POST resolve and the
    # post-POST recovery — and neither produced an id.
    assert rpc.count(RPCMethod.GET_LAST_CONVERSATION_ID) == 2


# ---------------------------------------------------------------------------
# 6. Loop affinity before the lock; cancellation between phases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_affinity_guard_fires_before_any_lock_or_io() -> None:
    """Moving this guard into a backend reintroduces the hang.

    The POST-path guard catches misuse only once the conversation lock is
    already held — too late for a lock bound to a dead loop.  So the guard must
    stay ahead of lock acquisition *and* ahead of source-id resolution.
    """
    rpc = _RpcRecorder(GET_LAST_CONVERSATION_ID=[])
    post = AsyncMock(return_value=_response(_stream_body(ANSWER_ROW)))
    chat = _chat(
        rpc=rpc,
        transport=SimpleNamespace(perform_authed_post=post),
        loop_guard=SimpleNamespace(
            assert_bound_loop=lambda: (_ for _ in ()).throw(
                RuntimeError("NotebookLMClient is bound to a different event loop.")
            )
        ),
    )
    locks_taken: list[str] = []
    chat._get_conversation_lock = lambda cid: locks_taken.append(cid)  # type: ignore[assignment]
    chat._get_new_conversation_lock = lambda nb: locks_taken.append(nb)  # type: ignore[assignment]

    with pytest.raises(RuntimeError, match="different event loop"):
        await chat.ask("nb-1", "Q?", conversation_id="conv-1")

    assert locks_taken == []
    assert rpc.calls == []
    post.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancellation_between_the_two_phases_strands_a_recorded_turn() -> None:
    """Current accepted behavior, which a stream protocol would make common.

    A cancel landing after the POST and before the id resolution leaves a turn
    the server recorded and the caller can never address: no ``AskResult``, no
    cached turn, no conversation id.  Pinned so a migration that changes it
    changes it deliberately.
    """
    post = AsyncMock(return_value=_response(_stream_body(ANSWER_ROW)))
    rpc = _RpcRecorder(GET_LAST_CONVERSATION_ID=[])
    chat = _chat(rpc=rpc, transport=SimpleNamespace(perform_authed_post=post))
    original_rpc_call = rpc.rpc_call
    resolved: list[str] = []

    async def cancel_after_the_post(method: RPCMethod, params: Any, **kwargs: Any) -> Any:
        if method is RPCMethod.GET_LAST_CONVERSATION_ID:
            resolved.append(params[2])
            if len(resolved) == 2:
                raise asyncio.CancelledError
        return await original_rpc_call(method, params, **kwargs)

    rpc.rpc_call = cancel_after_the_post  # type: ignore[method-assign]

    with pytest.raises(asyncio.CancelledError):
        await chat.ask("nb-1", "Q?")

    post.assert_awaited_once()  # the server recorded a turn...
    assert chat.cache_size() == 0  # ...that the caller cannot address
    assert chat.get_cached_turns("conv-1") == []


# ---------------------------------------------------------------------------
# 7. Exact GET_NOTEBOOK recency counts
# ---------------------------------------------------------------------------


async def _ask_without_source_ids(chat: ChatAPI) -> None:
    await chat.ask("nb-1", "Q?", conversation_id="conv-1")


async def _ask_with_source_ids(chat: ChatAPI) -> None:
    await chat.ask("nb-1", "Q?", source_ids=["s1"], conversation_id="conv-1")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("label", "call", "expected"),
    [
        ("chat.ask (source_ids omitted)", _ask_without_source_ids, 1),
        ("chat.ask (source_ids pinned)", _ask_with_source_ids, 0),
        ("chat.get_settings", lambda chat: chat.get_settings("nb-1"), 1),
        ("chat.get_conversation_id", lambda chat: chat.get_conversation_id("nb-1"), 0),
        ("chat.get_history", lambda chat: chat.get_history("nb-1"), 0),
        (
            "chat.get_conversation_turns",
            lambda chat: chat.get_conversation_turns("nb-1", "conv-1"),
            0,
        ),
        (
            "chat.delete_conversation",
            lambda chat: chat.delete_conversation("nb-1", "conv-1"),
            0,
        ),
        ("chat.configure", lambda chat: chat.configure("nb-1", ChatGoal.DEFAULT), 0),
        ("chat.set_mode", lambda chat: chat.set_mode("nb-1", ChatMode.CONCISE), 0),
    ],
    ids=lambda value: value if isinstance(value, str) else "",
)
async def test_chat_public_calls_pin_their_exact_get_notebook_recency_counts(
    label: str, call: Any, expected: int
) -> None:
    """``GET_NOTEBOOK`` writes ``lastViewedTime`` — the "Recent" list sort key.

    A projector-based service that caches or de-duplicates a notebook payload
    changes user-visible ordering, so dropping a "redundant" read here is a
    behavior change, not an optimization.
    """
    rpc = _RpcRecorder()
    await call(_chat(rpc=rpc))

    assert rpc.count(RPCMethod.GET_NOTEBOOK) == expected, label


def test_runtime_recency_counts_match_the_p0_catalog_rows() -> None:
    """The executable counts above and the declared catalog rows are one contract."""
    rows = {row["key"]: row for row in catalog.build_operation_catalog()["operations"]}

    (ask_rule,) = rows["chat.ask"]["recency_contract"]
    assert (ask_rule["minimum_calls"], ask_rule["maximum_calls"]) == (0, 1)
    assert ask_rule["public_methods"] == ["chat.ask"]
    assert {
        tuple(rule["public_methods"]): (rule["minimum_calls"], rule["maximum_calls"])
        for rule in rows["chat.configure"]["recency_contract"]
    } == {("chat.get_settings",): (1, 1), ("chat.configure", "chat.set_mode"): (0, 0)}
    for key in ("chat.get_conversation", "chat.get_history", "chat.delete_history"):
        assert rows[key]["recency_contract"] == []
        assert rows[key]["recency_effect"] == "none"


def test_chat_ask_stays_a_stream_policy_operation_with_its_two_phases_declared() -> None:
    """The catalog row is P6.1's specification; it must keep naming both phases."""
    row = next(
        row for row in catalog.build_operation_catalog()["operations"] if row["key"] == "chat.ask"
    )

    assert row["policy"] == "stream"
    kinds = {authority["transport_kind"] for authority in row["execution_authorities"]}
    assert kinds == {"stream", "rpc"}
    bindings = {authority["binding"] for authority in row["execution_authorities"]}
    assert "streamed_query" in bindings  # phase 1
    assert "GET_LAST_CONVERSATION_ID:<default>" in bindings  # phase 2
