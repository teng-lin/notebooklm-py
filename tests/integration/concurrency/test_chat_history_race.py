"""Regression test for the per-``conversation_id`` lock for serial follow-ups.

``ChatAPI.ask`` rebuilds the conversation history from
``_core._conversation_cache`` at the top of the request, then ``await``s
the streamed POST, then writes the new turn back to the cache. Two
concurrent ``ask`` calls on the *same* ``conversation_id`` interleave at
the ``await`` — both read the SAME pre-update history, both POST the
exact same prior-turn context, and the second cache-write overwrites
no-op (it appends, but the server-side turn lineage is already corrupted
because Google saw two follow-ups both claiming to be turn N+1).

Post-fix: ``ChatAPI`` holds a per-``conversation_id`` ``asyncio.Lock``
from history-build through cache-append. Two concurrent follow-ups on
the same conversation_id serialize; the second sees the first's cached
turn in its outgoing history payload.

Acceptance invariant:
  seed conversation ``cid`` with one Q/A turn; fire
  ``gather(ask("q2", conversation_id=cid), ask("q3", conversation_id=cid))``
  against a transport that delays each response so both requests overlap;
  assert that ONE of the two outgoing requests carries the OTHER turn's
  Q/A pair in its ``conversation_history``. Pre-fix both would carry
  only the seed turn (length 2 = 1 Q + 1 A); post-fix the second to run
  carries seed + first-follow-up (length 4 = 2 Q + 2 A).
"""

from __future__ import annotations

import asyncio
import json
from urllib.parse import parse_qs, unquote

import httpx
import pytest

from notebooklm import NotebookLMClient

# Mock-only tests (no real HTTP, no cassette) — opt out of the
# integration-tree enforcement hook in ``tests/integration/conftest.py``.
pytestmark = pytest.mark.allow_no_vcr


def _build_chat_response_body(answer_text: str, conversation_id: str) -> str:
    """Build a minimal streamed-chat response containing ``answer_text``.

    Mirrors the shape ``_chat._parse_ask_response_with_references`` expects:
    a ``)]}'`` prelude, a length-prefixed JSON chunk, where the chunk is a
    ``wrb.fr`` envelope whose ``item[2]`` is the inner JSON string. The inner
    payload's ``first[0]`` is the answer, ``first[2][0]`` is the
    conversation id, and ``first[4][-1] == 1`` marks the row as the final
    answer (vs. an intermediate streaming chunk).
    """
    inner = [
        [
            answer_text,
            None,
            [conversation_id, 12345],
            None,
            [[], None, None, [], 1],
        ]
    ]
    inner_json = json.dumps(inner)
    chunk = json.dumps([["wrb.fr", None, inner_json]])
    return f")]}}'\n{len(chunk)}\n{chunk}\n"


def _parse_chat_params(request: httpx.Request) -> list:
    """Decode the params list out of a chat POST body.

    The body is ``f.req=<url-encoded JSON>&at=<csrf>&``. The JSON is
    ``[null, "<params-json>"]``; the inner params list matches the order
    in ``_chat._build_chat_request`` (sources, question, history, ...,
    conversation_id, ..., notebook_id). One parse per request keeps the
    transport hot-path cheap and avoids triple-decoding for the question /
    history / conversation-id reads each call needs.
    """
    body_text = request.content.decode("utf-8")
    parsed = parse_qs(body_text, keep_blank_values=True)
    f_req_values = parsed.get("f.req", [])
    assert f_req_values, f"chat POST body missing f.req param: {body_text!r}"
    f_req = json.loads(unquote(f_req_values[0]))
    return json.loads(f_req[1])


def _extract_conversation_history(request: httpx.Request) -> list | None:
    """Return ``params[2]`` — the ``conversation_history`` slot.

    ``None`` for new-conversation asks (no prior history); a list of
    ``[answer, None, 2]`` / ``[query, None, 1]`` entries for follow-ups.
    """
    return _parse_chat_params(request)[2]


def _extract_question(request: httpx.Request) -> str:
    """Return ``params[1]`` — the user question string."""
    return _parse_chat_params(request)[1]


class _SerializingChatTransport(httpx.AsyncBaseTransport):
    """Mock transport that delays each chat response and records request bodies.

    The ``response_delay`` is wide enough (relative to gather scheduling) that
    two ``gather``ed asks both enter ``handle_async_request`` before either
    returns — so without a per-conversation lock, the two ``conversation_history``
    snapshots seen by the transport are identical (both read the same seed).

    With the lock the second ask blocks BEFORE entering history-build,
    so it observes the first ask's cached turn and the two histories differ
    in length.
    """

    def __init__(self, *, response_delay: float = 0.1) -> None:
        self._delay = response_delay
        self._captured: list[httpx.Request] = []
        self._answer_for_question: dict[str, str] = {}

    def set_answer(self, question: str, answer: str) -> None:
        self._answer_for_question[question] = answer

    def captured(self) -> list[httpx.Request]:
        return list(self._captured)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        # Record the request BEFORE the await so both fan-out requests
        # appear in ``captured()`` with the history they had at entry.
        self._captured.append(request)
        params = _parse_chat_params(request)
        question = params[1]
        # ``params[4]`` is the conversation_id slot — echo it back as the
        # server-assigned id so the cache stays pinned to the caller's
        # seeded cid instead of being remapped to a fresh server uuid.
        conversation_id = params[4]
        answer = self._answer_for_question.get(question, f"answer-for:{question}")
        # The delay is the overlap window. Without the per-conversation
        # lock, both gather'd asks reach this await holding the same
        # pre-update history. With the lock, the second ask has not even
        # built its request yet — its request is appended to ``_captured``
        # only after the first's response is parsed and cached.
        await asyncio.sleep(self._delay)
        return httpx.Response(
            200,
            text=_build_chat_response_body(answer, conversation_id),
        )


def _make_client(transport: httpx.AsyncBaseTransport, auth_tokens) -> NotebookLMClient:
    """Build a ``NotebookLMClient`` wired to ``transport``.

    Mirrors ``test_idempotency_create._make_client_with_transport``: stub
    ``_core._http_client`` with a pre-built ``AsyncClient`` so the chat
    POSTs route through the mock instead of opening a real socket.
    """
    client = NotebookLMClient(auth_tokens)
    client._core._http_client = httpx.AsyncClient(
        transport=transport,
        headers={
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        },
    )
    return client


@pytest.mark.asyncio
async def test_concurrent_follow_ups_serialize_on_conversation_id(auth_tokens) -> None:
    """Two gather'd follow-ups on the same conversation_id must serialize.

    Pre-fix: ``ChatAPI.ask`` builds history before the await and writes
    cache after — two concurrent calls read identical pre-update history
    (length 2: seed Q + seed A) and the assertion below fails.

    Post-fix: the per-``conversation_id`` lock holds across history build,
    network round-trip, and cache append. The second ask cannot enter
    history-build until the first appends its turn — so one of the two
    outgoing requests has history length 4 (seed Q/A + first-follow-up Q/A)
    while the other has length 2 (seed only).
    """
    cid = "conv_t7f1"
    notebook_id = "nb_t7f1"

    transport = _SerializingChatTransport(response_delay=0.1)
    transport.set_answer("q2", "answer-2")
    transport.set_answer("q3", "answer-3")

    client = _make_client(transport, auth_tokens)
    try:
        # Seed the conversation cache so both follow-ups have at least one
        # prior turn to read. ``ask`` would normally populate this on a
        # first call but we want a known, fixed seed to assert against.
        client._core.cache_conversation_turn(cid, "q1", "answer-1", turn_number=1)

        results = await asyncio.gather(
            client.chat.ask(
                notebook_id,
                "q2",
                source_ids=["src_001"],
                conversation_id=cid,
            ),
            client.chat.ask(
                notebook_id,
                "q3",
                source_ids=["src_001"],
                conversation_id=cid,
            ),
            return_exceptions=False,
        )
    finally:
        await client._core._http_client.aclose()

    # Sanity: both calls returned their respective answers.
    answers = sorted(r.answer for r in results)
    assert answers == ["answer-2", "answer-3"], (
        f"expected both follow-ups to receive their dedicated answers; got {answers!r}"
    )

    captured = transport.captured()
    assert len(captured) == 2, f"expected two chat POSTs, got {len(captured)}"

    # Pair each captured request with its outgoing history length.
    # ``conversation_history`` is a list of alternating [answer, None, 2]
    # and [query, None, 1] entries; length 2 == seed only, length 4 ==
    # seed + first follow-up.
    histories = {_extract_question(req): _extract_conversation_history(req) for req in captured}
    q2_hist = histories.get("q2")
    q3_hist = histories.get("q3")
    assert q2_hist is not None, "q2 request carried no conversation_history"
    assert q3_hist is not None, "q3 request carried no conversation_history"

    lengths = {len(q2_hist), len(q3_hist)}
    # Pre-fix both lengths == 2 (both read seed-only). Post-fix one is 2
    # (the first to run) and the other is 4 (saw the first's cached turn).
    assert lengths == {2, 4}, (
        "expected one follow-up to see the other's cached turn in its history "
        f"(serialized by the per-conversation_id lock); got history lengths {lengths}. "
        f"q2 history: {q2_hist!r}; q3 history: {q3_hist!r}"
    )

    # Confirm the longer history includes the OTHER follow-up's question
    # text — proves serialization, not just length parity.
    longer_question, longer_history = max(histories.items(), key=lambda kv: len(kv[1]))
    other_question = "q3" if longer_question == "q2" else "q2"
    questions_in_history = [
        entry[0] for entry in longer_history if isinstance(entry, list) and entry[-1] == 1
    ]
    assert other_question in questions_in_history, (
        f"{longer_question}'s history did not include {other_question}'s turn; "
        f"questions seen: {questions_in_history!r}"
    )


@pytest.mark.asyncio
async def test_different_conversation_ids_run_in_parallel(auth_tokens) -> None:
    """The lock must be PER-conversation: different cids do NOT serialize.

    The fix's value depends on lock granularity: a global ``ChatAPI`` lock
    would also pass the serialization test above, but would also serialize
    every unrelated chat in the process. This test wires two follow-ups
    against DIFFERENT conversation_ids and asserts they overlap at the
    transport boundary (peak in-flight == 2). A regression to a coarse
    lock would cap peak in-flight at 1 and fail.

    The transport's 100ms response delay means serialized execution would
    take ~200ms, parallel execution ~100ms. We use peak-inflight rather
    than wall-clock to avoid CI-jitter flakiness.
    """
    cid_a = "conv_t7f1_a"
    cid_b = "conv_t7f1_b"
    notebook_id = "nb_t7f1"

    # Track peak in-flight requests at the transport boundary.
    inflight = 0
    peak_inflight = 0
    transport = _SerializingChatTransport(response_delay=0.1)
    transport.set_answer("qA", "answer-A")
    transport.set_answer("qB", "answer-B")

    original_handler = transport.handle_async_request

    async def tracking_handler(request: httpx.Request) -> httpx.Response:
        nonlocal inflight, peak_inflight
        inflight += 1
        peak_inflight = max(peak_inflight, inflight)
        try:
            return await original_handler(request)
        finally:
            inflight -= 1

    transport.handle_async_request = tracking_handler  # type: ignore[method-assign]

    client = _make_client(transport, auth_tokens)
    try:
        # Seed BOTH conversations so the asks take the follow-up path
        # (the path the lock protects). New-conversation asks would
        # also fan out in parallel but for a different reason — fresh
        # UUIDs — so this test wants the follow-up path specifically.
        client._core.cache_conversation_turn(cid_a, "q0", "a0", turn_number=1)
        client._core.cache_conversation_turn(cid_b, "q0", "a0", turn_number=1)

        await asyncio.gather(
            client.chat.ask(notebook_id, "qA", source_ids=["src_001"], conversation_id=cid_a),
            client.chat.ask(notebook_id, "qB", source_ids=["src_001"], conversation_id=cid_b),
        )
    finally:
        await client._core._http_client.aclose()

    assert peak_inflight == 2, (
        f"different-conversation follow-ups must run in parallel, "
        f"got peak_inflight={peak_inflight}. A coarse global lock would cap this at 1."
    )
