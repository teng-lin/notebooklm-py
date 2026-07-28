"""Regression test for the per-``conversation_id`` lock for serial follow-ups.

``ChatAPI.ask`` rebuilds the conversation history from
``ChatAPI._cache`` at the top of the request, then ``await``s
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
from urllib.parse import parse_qs, unquote, urlparse

import httpx
import pytest

from notebooklm import NotebookLMClient
from notebooklm.rpc import RPCMethod
from tests._fixtures.kernel_test_helpers import install_http_client_for_test

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


def _build_get_conversation_id_response_body(conversation_id: str) -> str:
    """Build a minimal ``hPTbtc`` response for post-ask id recovery."""
    inner = json.dumps([[[conversation_id]]])
    chunk = json.dumps(["wrb.fr", RPCMethod.GET_LAST_CONVERSATION_ID.value, inner, None, None])
    return f")]}}'\n{len(chunk)}\n{chunk}\n"


def _build_get_conversation_turns_response_body() -> str:
    """Build a minimal ``khqZz`` response containing one existing turn."""
    inner = json.dumps([[[None, None, 1, "Existing question?"]]])
    chunk = json.dumps(["wrb.fr", RPCMethod.GET_CONVERSATION_TURNS.value, inner, None, None])
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


def _extract_source_path_notebook_id(request: httpx.Request) -> str:
    """Return the notebook id from a batchexecute ``source-path`` query param."""
    query = parse_qs(urlparse(str(request.url)).query, keep_blank_values=True)
    source_path = query.get("source-path", [""])[0]
    return source_path.rsplit("/", 1)[-1] if source_path.startswith("/notebook/") else ""


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

    def __init__(
        self,
        *,
        response_delay: float = 0.1,
        response_delays_by_question: dict[str, float] | None = None,
        conversation_ids_by_notebook: dict[str, str] | None = None,
    ) -> None:
        self._delay = response_delay
        self._response_delays_by_question = response_delays_by_question or {}
        self._captured: list[httpx.Request] = []
        self._answer_for_question: dict[str, str] = {}
        self._conversation_ids_by_notebook = conversation_ids_by_notebook or {}
        self._chat_inflight = 0
        self._peak_chat_inflight = 0
        self._events: list[tuple[str, str, str | None]] = []

    def set_answer(self, question: str, answer: str) -> None:
        self._answer_for_question[question] = answer

    def captured(self) -> list[httpx.Request]:
        return list(self._captured)

    def peak_chat_inflight(self) -> int:
        return self._peak_chat_inflight

    def events(self) -> list[tuple[str, str, str | None]]:
        return list(self._events)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if "batchexecute" in str(request.url):
            notebook_id = _extract_source_path_notebook_id(request)
            if "rpcids=khqZz" in str(request.url):
                self._events.append(("khqzz", notebook_id, None))
                return httpx.Response(
                    200,
                    text=_build_get_conversation_turns_response_body(),
                    request=request,
                )
            self._events.append(("hptbtc", notebook_id, None))
            conversation_id = self._conversation_ids_by_notebook.get(
                notebook_id,
                f"conv-for-{notebook_id}",
            )
            return httpx.Response(
                200,
                text=_build_get_conversation_id_response_body(conversation_id),
                request=request,
            )

        # Record the request BEFORE the await so both fan-out requests
        # appear in ``captured()`` with the history they had at entry.
        self._captured.append(request)
        params = _parse_chat_params(request)
        question = params[1]
        notebook_id = params[7]
        self._events.append(("chat-start", notebook_id, question))
        # ``params[4]`` is the conversation_id slot — echo it back as the
        # server-assigned id so the cache stays pinned to the caller's
        # seeded cid instead of being remapped to a fresh server uuid.
        conversation_id = params[4] or f"stream-for-{notebook_id}-{question}"
        answer = self._answer_for_question.get(question, f"answer-for:{question}")
        # The delay is the overlap window. Without the per-conversation
        # lock, both gather'd asks reach this await holding the same
        # pre-update history. With the lock, the second ask has not even
        # built its request yet — its request is appended to ``_captured``
        # only after the first's response is parsed and cached.
        delay = self._response_delays_by_question.get(question, self._delay)
        self._chat_inflight += 1
        self._peak_chat_inflight = max(self._peak_chat_inflight, self._chat_inflight)
        try:
            await asyncio.sleep(delay)
            return httpx.Response(
                200,
                text=_build_chat_response_body(answer, conversation_id),
                request=request,
            )
        finally:
            self._chat_inflight -= 1
            self._events.append(("chat-end", notebook_id, question))


def _make_client(transport: httpx.AsyncBaseTransport, auth_tokens) -> NotebookLMClient:
    """Build a ``NotebookLMClient`` wired to ``transport``.

    Mirrors ``test_idempotency_create._make_client_with_transport``: stub
    ``client._collaborators.kernel.http_client`` with a pre-built ``AsyncClient`` so the chat
    POSTs route through the mock instead of opening a real socket.
    """
    client = NotebookLMClient(auth_tokens)
    install_http_client_for_test(
        client._collaborators.kernel,
        httpx.AsyncClient(
            transport=transport,
            headers={
                "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
            },
        ),
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
        client.chat._cache.cache_conversation_turn(cid, "q1", "answer-1", turn_number=1)

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
        await client._collaborators.kernel.get_http_client().aclose()

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

    transport = _SerializingChatTransport(response_delay=0.1)
    transport.set_answer("qA", "answer-A")
    transport.set_answer("qB", "answer-B")

    client = _make_client(transport, auth_tokens)
    try:
        # Seed BOTH conversations so the asks take the follow-up path
        # (the path the lock protects). New-conversation asks would
        # also fan out in parallel but for a different reason — fresh
        # UUIDs — so this test wants the follow-up path specifically.
        client.chat._cache.cache_conversation_turn(cid_a, "q0", "a0", turn_number=1)
        client.chat._cache.cache_conversation_turn(cid_b, "q0", "a0", turn_number=1)

        await asyncio.gather(
            client.chat.ask(notebook_id, "qA", source_ids=["src_001"], conversation_id=cid_a),
            client.chat.ask(notebook_id, "qB", source_ids=["src_001"], conversation_id=cid_b),
        )
    finally:
        await client._collaborators.kernel.get_http_client().aclose()

    assert transport.peak_chat_inflight() == 2, (
        f"different-conversation follow-ups must run in parallel, "
        f"got peak_inflight={transport.peak_chat_inflight()}. "
        "A coarse global lock would cap this at 1."
    )


@pytest.mark.asyncio
async def test_same_notebook_new_conversation_asks_serialize_until_id_exists(
    auth_tokens,
) -> None:
    """Two null-conversation asks on one notebook must not overlap pre-id work.

    ``conversation_id=None`` means the only stable local key before the post-ask
    ``hPTbtc`` lookup is ``notebook_id``. The second chat POST must not start
    until the first ask has recovered the real conversation id, otherwise both
    calls are in the same notebook's anonymous/current-conversation path at
    once.
    """
    notebook_id = "nb_new_shared"
    conversation_id = "conv_new_shared"

    transport = _SerializingChatTransport(
        response_delay=0.1,
        conversation_ids_by_notebook={notebook_id: conversation_id},
    )
    transport.set_answer("q-new-1", "answer-new-1")
    transport.set_answer("q-new-2", "answer-new-2")

    client = _make_client(transport, auth_tokens)
    try:
        results = await asyncio.gather(
            client.chat.ask(notebook_id, "q-new-1", source_ids=["src_001"]),
            client.chat.ask(notebook_id, "q-new-2", source_ids=["src_001"]),
        )
    finally:
        await client._collaborators.kernel.get_http_client().aclose()

    assert {result.conversation_id for result in results} == {conversation_id}
    assert transport.peak_chat_inflight() == 1, (
        "same-notebook null-conversation asks must serialize until hPTbtc "
        f"returns the real id; got peak_chat_inflight={transport.peak_chat_inflight()}"
    )

    events = transport.events()
    first_hptbtc_index = next(
        index for index, event in enumerate(events) if event == ("hptbtc", notebook_id, None)
    )
    chat_start_indices = [
        index
        for index, event in enumerate(events)
        if event[0] == "chat-start" and event[1] == notebook_id
    ]
    assert len(chat_start_indices) == 2
    assert first_hptbtc_index < chat_start_indices[1], (
        "second null-conversation chat POST started before the first ask "
        f"resolved hPTbtc; events={events!r}"
    )

    cached_turns = client.chat.get_cached_turns(conversation_id)
    assert [turn.turn_number for turn in cached_turns] == [1, 2]
    assert {turn.query for turn in cached_turns} == {"q-new-1", "q-new-2"}


@pytest.mark.asyncio
async def test_different_notebook_new_conversation_asks_run_in_parallel(auth_tokens) -> None:
    """Notebook-scoped null-conversation locks must not become a global lock."""
    notebook_a = "nb_new_a"
    notebook_b = "nb_new_b"
    conversation_a = "conv_new_a"
    conversation_b = "conv_new_b"

    transport = _SerializingChatTransport(
        response_delay=0.1,
        conversation_ids_by_notebook={
            notebook_a: conversation_a,
            notebook_b: conversation_b,
        },
    )
    transport.set_answer("q-new-a", "answer-new-a")
    transport.set_answer("q-new-b", "answer-new-b")

    client = _make_client(transport, auth_tokens)
    try:
        results = await asyncio.gather(
            client.chat.ask(notebook_a, "q-new-a", source_ids=["src_001"]),
            client.chat.ask(notebook_b, "q-new-b", source_ids=["src_001"]),
        )
    finally:
        await client._collaborators.kernel.get_http_client().aclose()

    assert {result.conversation_id for result in results} == {conversation_a, conversation_b}
    assert transport.peak_chat_inflight() == 2, (
        "different notebooks must not share the null-conversation lock; "
        f"got peak_chat_inflight={transport.peak_chat_inflight()}"
    )


@pytest.mark.asyncio
async def test_new_conversation_cache_update_waits_for_resolved_conversation_lock(
    auth_tokens,
) -> None:
    """A null ask resolving to a conversation shares that conversation's lock.

    The explicit follow-up contends ``_conversation_locks[conversation_id]``.
    Under the #1875 two-phase design the null ask resolves that same id under
    the notebook lock and then holds the conversation lock across BOTH its POST
    and its cache write — so the two serialize on one lock (peak in-flight 1)
    and produce contiguous, non-corrupted turns. Which of the two wins the lock
    is a scheduling detail; the invariant is that neither clobbers the other's
    turn. Pre-fix the null ask POSTed under the notebook lock and only took the
    conversation lock for the cache write, so the two POSTs overlapped.
    """
    notebook_id = "nb_new_followup"
    conversation_id = "conv_new_followup"

    transport = _SerializingChatTransport(
        response_delay=0.1,
        response_delays_by_question={
            "q-new": 0.05,
            "q-follow": 0.2,
        },
        conversation_ids_by_notebook={notebook_id: conversation_id},
    )
    transport.set_answer("q-new", "answer-new")
    transport.set_answer("q-follow", "answer-follow")

    client = _make_client(transport, auth_tokens)
    try:
        client.chat._cache.cache_conversation_turn(
            conversation_id,
            "q0",
            "answer-0",
            turn_number=1,
        )

        await asyncio.gather(
            client.chat.ask(notebook_id, "q-new", source_ids=["src_001"]),
            client.chat.ask(
                notebook_id,
                "q-follow",
                source_ids=["src_001"],
                conversation_id=conversation_id,
            ),
        )
    finally:
        await client._collaborators.kernel.get_http_client().aclose()

    # Both asks serialize on the resolved conversation's lock: peak in-flight 1.
    assert transport.peak_chat_inflight() == 1, (
        "a null ask resolving to the follow-up's conversation must serialize "
        f"on its lock; got peak_chat_inflight={transport.peak_chat_inflight()}"
    )
    # Serialized under one lock → contiguous turns, no lost update, both present
    # (exact order depends on which task wins the lock and is not asserted).
    cached_turns = client.chat.get_cached_turns(conversation_id)
    assert [turn.turn_number for turn in cached_turns] == [1, 2, 3]
    assert {turn.query for turn in cached_turns} == {"q0", "q-new", "q-follow"}


@pytest.mark.asyncio
async def test_null_ask_serializes_with_explicit_current_conversation(auth_tokens) -> None:
    """A null ask and an explicit follow-up on the *current* conversation serialize.

    Regression for #1875: the server appends a ``conversation_id=None`` ask to
    the notebook's current conversation (``params[4]=null``). When that current
    conversation is the same one an explicit follow-up targets, the two must
    hold the SAME per-conversation lock so their streamed POSTs serialize.
    Pre-fix the null ask POSTed under the notebook lock while the follow-up
    POSTed under the conversation lock — disjoint locks, peak in-flight 2.
    Post-fix the null ask resolves the current id under the notebook lock and
    takes that conversation's lock around POST + cache — peak in-flight 1.
    """
    notebook_id = "nb_1875_same"
    conversation_id = "conv_1875_same"

    transport = _SerializingChatTransport(
        response_delay=0.1,
        conversation_ids_by_notebook={notebook_id: conversation_id},
    )
    transport.set_answer("q-null", "answer-null")
    transport.set_answer("q-follow", "answer-follow")

    client = _make_client(transport, auth_tokens)
    try:
        client.chat._cache.cache_conversation_turn(conversation_id, "q0", "answer-0", turn_number=1)
        await asyncio.gather(
            client.chat.ask(notebook_id, "q-null", source_ids=["src_001"]),
            client.chat.ask(
                notebook_id,
                "q-follow",
                source_ids=["src_001"],
                conversation_id=conversation_id,
            ),
        )
    finally:
        await client._collaborators.kernel.get_http_client().aclose()

    assert transport.peak_chat_inflight() == 1, (
        "a null ask and an explicit follow-up on the notebook's current "
        "conversation must serialize on the same per-conversation lock; "
        f"got peak_chat_inflight={transport.peak_chat_inflight()}"
    )

    # Cache coherent: seed turn + the two serialized asks, contiguous 1/2/3.
    cached_turns = client.chat.get_cached_turns(conversation_id)
    assert [turn.turn_number for turn in cached_turns] == [1, 2, 3]
    assert {turn.query for turn in cached_turns} == {"q0", "q-null", "q-follow"}


@pytest.mark.asyncio
async def test_null_ask_parallel_with_followup_on_other_conversation(auth_tokens) -> None:
    """Granularity guard: a null ask resolving to convX runs parallel to convY.

    The #1875 fix must not become a coarse notebook/global POST lock: a null
    ask whose current conversation is convX shares no lock with an explicit
    follow-up on an unrelated convY, so the two overlap at the transport
    (peak in-flight 2). A regression to a notebook-wide POST lock caps this at 1.
    """
    notebook_id = "nb_1875_other"
    conversation_x = "conv_1875_x"
    conversation_y = "conv_1875_y"

    transport = _SerializingChatTransport(
        response_delay=0.1,
        conversation_ids_by_notebook={notebook_id: conversation_x},
    )
    transport.set_answer("q-null", "answer-null")
    transport.set_answer("q-other", "answer-other")

    client = _make_client(transport, auth_tokens)
    try:
        client.chat._cache.cache_conversation_turn(conversation_y, "q0", "answer-0", turn_number=1)
        await asyncio.gather(
            client.chat.ask(notebook_id, "q-null", source_ids=["src_001"]),
            client.chat.ask(
                notebook_id,
                "q-other",
                source_ids=["src_001"],
                conversation_id=conversation_y,
            ),
        )
    finally:
        await client._collaborators.kernel.get_http_client().aclose()

    assert transport.peak_chat_inflight() == 2, (
        "a null ask resolving to convX must not serialize with a follow-up on "
        f"a different convY; got peak_chat_inflight={transport.peak_chat_inflight()}"
    )


def _build_delete_ok_body() -> str:
    """Minimal success envelope for a DELETE_CONVERSATION (``J7Gthc``) RPC."""
    inner = json.dumps([])
    chunk = json.dumps(["wrb.fr", RPCMethod.DELETE_CONVERSATION.value, inner, None, None])
    return f")]}}'\n{len(chunk)}\n{chunk}\n"


def _request_rpcid(request: httpx.Request) -> str:
    """Return the ``rpcids`` query param of a batchexecute request."""
    return parse_qs(urlparse(str(request.url)).query).get("rpcids", [""])[0]


class _DeleteRaceTransport(httpx.AsyncBaseTransport):
    """Transport that lets a ``delete_conversation`` hold the conversation lock
    while a concurrent null ask resolves the same id and blocks on that lock.

    The DELETE RPC sleeps for ``delete_delay`` (holding the per-conversation
    lock the whole time), so a null ask gathered *after* it resolves the current
    id (first ``hPTbtc`` -> ``resolved_id``) and then parks on the same lock.
    When the delete finishes it marks the id deleted; the null ask wakes, drops
    its ``resolved_id_override``, POSTs (server starts a fresh conversation) and
    recovers the real id via the second ``hPTbtc`` -> ``recovered_id``.
    """

    def __init__(self, *, resolved_id: str, recovered_id: str, delete_delay: float = 0.1) -> None:
        self._resolved_id = resolved_id
        self._recovered_id = recovered_id
        self._delete_delay = delete_delay
        self._hptbtc_calls = 0
        self._events: list[str] = []

    def events(self) -> list[str]:
        return list(self._events)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if "batchexecute" in str(request.url):
            rpcid = _request_rpcid(request)
            if rpcid == RPCMethod.GET_LAST_CONVERSATION_ID.value:
                self._hptbtc_calls += 1
                cid = self._resolved_id if self._hptbtc_calls == 1 else self._recovered_id
                self._events.append(f"hptbtc:{cid}")
                return httpx.Response(
                    200,
                    text=_build_get_conversation_id_response_body(cid),
                    request=request,
                )
            # DELETE_CONVERSATION: hold the conversation lock for the delay.
            self._events.append("delete-start")
            await asyncio.sleep(self._delete_delay)
            self._events.append("delete-end")
            return httpx.Response(200, text=_build_delete_ok_body(), request=request)
        # Null chat POST: params[4] is null; the answer's stream id is discarded
        # and the real id comes from the post-POST hPTbtc (recovered_id).
        self._events.append("chat-post")
        return httpx.Response(
            200,
            text=_build_chat_response_body("answer-after-delete", "stream-id-discarded"),
            request=request,
        )


@pytest.mark.asyncio
async def test_null_ask_recovers_when_current_conversation_deleted_mid_flight(
    auth_tokens,
) -> None:
    """A delete that lands between resolve and POST must not pin the turn to the
    deleted id.

    Regression for the #1875 review (Codex P2): the null ask resolves
    ``current_id`` = X, then blocks on ``_get_conversation_lock(X)`` held by a
    concurrent ``delete_conversation(notebook, X)``. When the delete completes,
    the server starts a FRESH conversation for the null POST. Without the
    deleted-id re-check the null ask would suppress the post-POST ``hPTbtc``
    recovery (``resolved_id_override=X``) and cache/report the new turn under the
    DELETED id X. Post-fix it drops the override and recovers the real id Y.
    """
    notebook_id = "nb_1875_delrace"
    deleted_id = "conv_1875_deleted"
    fresh_id = "conv_1875_fresh"

    transport = _DeleteRaceTransport(
        resolved_id=deleted_id, recovered_id=fresh_id, delete_delay=0.1
    )
    client = _make_client(transport, auth_tokens)
    try:
        # Seed a turn under the soon-to-be-deleted id so we can prove the cache
        # entry is gone (delete clears it) and the new turn lands under Y, not X.
        client.chat._cache.cache_conversation_turn(deleted_id, "q0", "a0", turn_number=1)
        # Delete is gathered first so it acquires the conversation lock before the
        # null ask, which then resolves X and parks on that same lock.
        _, result = await asyncio.gather(
            client.chat.delete_conversation(notebook_id, deleted_id),
            client.chat.ask(notebook_id, "q-after-delete", source_ids=["src_001"]),
        )
    finally:
        await client._collaborators.kernel.get_http_client().aclose()

    # The turn is reported and cached under the FRESH id, never the deleted one.
    assert result.conversation_id == fresh_id, (
        "null ask must recover the fresh server conversation after its resolved "
        f"current conversation was deleted mid-flight; got {result.conversation_id!r}"
    )
    assert client.chat.get_cached_turns(fresh_id), "new turn must be cached under the fresh id"
    assert not client.chat.get_cached_turns(deleted_id), (
        "the deleted conversation's cache (and the recovered turn) must not live "
        "under the deleted id"
    )
    # Ordering proof: the delete completed before the chat POST started.
    events = transport.events()
    assert "delete-end" in events and "chat-post" in events
    assert events.index("delete-end") < events.index("chat-post"), (
        f"delete must complete before the null POST for the race to be exercised; events={events!r}"
    )
