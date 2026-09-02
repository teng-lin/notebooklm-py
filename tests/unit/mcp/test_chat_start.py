"""Unit tests for the detached chat ask pair (``chat_start`` / ``chat_status``)
and the :class:`~notebooklm.mcp._chattasks.ChatTaskRegistry` backing them.

The registry is exercised directly (bounded caps, idempotent claim states, TTL
expiry, detachment from the caller, shutdown cancellation) — same rationale as
``test_await_upload``: the semantics live in the helper, not the transport. The
tools are then driven through one in-memory FastMCP ``Client`` session against
the mocked ``NotebookLMClient`` (a single session, because the registry is
lifespan state — ``chat_status`` must see what ``chat_start`` claimed).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

import pytest

# Skip cleanly when the `mcp` extra (fastmcp) is absent; see conftest.py.
pytest.importorskip("fastmcp")

from fastmcp import Client  # noqa: E402 - after importorskip guard
from fastmcp.exceptions import ToolError  # noqa: E402 - after importorskip guard

from notebooklm import ChatSessionStatus  # noqa: E402 - after importorskip guard
from notebooklm.exceptions import ChatError  # noqa: E402 - after importorskip guard
from notebooklm.mcp._chattasks import (  # noqa: E402 - after importorskip guard
    ChatTaskCapacityError,
    ChatTaskRegistry,
    compute_chat_task_key,
)

from .conftest import AsyncMock  # noqa: E402 - after importorskip guard

NB_ID = "11111111-1111-1111-1111-111111111111"
CONV_ID = "22222222-2222-2222-2222-222222222222"


@dataclass
class FakeAskResult:
    answer: str
    conversation_id: str
    turn_number: int = 1
    is_follow_up: bool = False
    references: list[Any] = field(default_factory=list)
    raw_response: str = ""


# ---------------------------------------------------------------------------
# ChatTaskRegistry (direct)
# ---------------------------------------------------------------------------


async def test_registry_finished_entry_never_short_circuits_a_new_start() -> None:
    """A finished ask is never replayed to a new start (asking twice = two turns,
    like chat_ask) — but its payload stays pollable by task_id for the TTL."""
    registry = ChatTaskRegistry()

    async def _work() -> dict[str, Any]:
        return {"answer": "42"}

    entry, how = registry.start("k1", _work)
    assert how == "created"
    assert entry.done_at is None
    assert entry.task is not None
    await entry.task
    assert entry.result == {"answer": "42"}
    assert entry.done_at is not None and entry.done_wall is not None
    fresh, how2 = registry.start("k1", _work)
    assert how2 == "created"  # re-asks — no cached replay
    assert fresh is not entry
    assert registry.status(entry.task_id) is entry  # old payload still pollable
    assert fresh.task is not None
    await fresh.task


async def test_registry_attaches_to_running_duplicate() -> None:
    registry = ChatTaskRegistry()
    gate = asyncio.Event()

    async def _work() -> dict[str, Any]:
        await gate.wait()
        return {"answer": "slow"}

    first, how1 = registry.start("k1", _work)
    second, how2 = registry.start("k1", _work)
    assert (how1, how2) == ("created", "running")
    assert second is first  # attached — no double generation
    other, how3 = registry.start("k2", _work)
    assert how3 == "created"
    assert other is not first
    gate.set()
    assert first.task is not None and other.task is not None
    await asyncio.gather(first.task, other.task)
    assert first.result == {"answer": "slow"}


async def test_registry_failed_entry_is_replaced_not_cached() -> None:
    registry = ChatTaskRegistry()

    async def _boom() -> dict[str, Any]:
        raise ChatError("upstream fell over")

    entry, _ = registry.start("k1", _boom)
    assert entry.task is not None
    await entry.task
    assert isinstance(entry.error, ChatError)
    assert entry.result is None

    async def _ok() -> dict[str, Any]:
        return {"answer": "retry"}

    # A retry after a failure must re-ask ("failures are not cached").
    fresh, how = registry.start("k1", _ok)
    assert how == "created"
    assert fresh is not entry
    assert fresh.task is not None
    await fresh.task
    assert fresh.result == {"answer": "retry"}


async def test_registry_task_survives_cancelled_caller() -> None:
    """The detachment guarantee: cancelling the request that called ``start``
    must NOT cancel the spawned ask (that is the entire point of the pair)."""
    registry = ChatTaskRegistry()
    gate = asyncio.Event()

    async def _work() -> dict[str, Any]:
        await gate.wait()
        return {"answer": "survived"}

    async def _request() -> None:
        registry.start("k1", _work)
        await asyncio.sleep(3600)  # simulate the transport holding the call open

    request = asyncio.create_task(_request())
    await asyncio.sleep(0)  # let the request run start()
    request.cancel()  # the ~60s watchdog killing the start call
    with pytest.raises(asyncio.CancelledError):
        await request
    entry, how = registry.start("k1", _work)
    assert how == "running"  # the detached ask is still alive; we attached to it
    gate.set()
    assert entry.task is not None
    await entry.task
    assert entry.result == {"answer": "survived"}


async def test_registry_queues_past_concurrency_and_autostarts() -> None:
    """Past the concurrency ceiling new asks QUEUE (started_at None) and
    auto-start as slots free — the server owns the pacing, no caller errors."""
    registry = ChatTaskRegistry(concurrency=1)
    gate = asyncio.Event()

    async def _work() -> dict[str, Any]:
        await gate.wait()
        return {"answer": "ok"}

    first, how1 = registry.start("k1", _work)
    second, how2 = registry.start("k2", _work)
    assert (how1, how2) == ("created", "created")  # both ACCEPTED
    await asyncio.sleep(0)  # let the first guard acquire the slot
    assert first.started_at is not None  # generating
    assert second.started_at is None  # queued behind the slot
    assert registry.counts()["generating"] == 1
    assert registry.counts()["queued"] == 1
    gate.set()
    assert first.task is not None and second.task is not None
    await asyncio.gather(first.task, second.task)
    assert first.result == {"answer": "ok"}
    assert second.result == {"answer": "ok"}  # auto-started after k1 freed the slot
    assert second.started_at is not None and second.started_at >= first.created_at


async def test_registry_capacity_fuse_when_all_slots_unfinished() -> None:
    registry = ChatTaskRegistry(max_tasks=1, concurrency=1)
    gate = asyncio.Event()

    async def _work() -> dict[str, Any]:
        await gate.wait()
        return {}

    entry, _ = registry.start("k1", _work)
    with pytest.raises(ChatTaskCapacityError):
        registry.start("k2", _work)  # the single retained slot is unfinished
    gate.set()
    assert entry.task is not None
    await entry.task
    # A finished entry is evictable, so capacity frees up.
    _, how = registry.start("k2", _work)
    assert how == "created"


async def test_registry_ttl_expires_completed_entries() -> None:
    registry = ChatTaskRegistry(result_ttl_s=60.0)

    async def _work() -> dict[str, Any]:
        return {"answer": "ephemeral"}

    entry, _ = registry.start("k1", _work)
    assert entry.task is not None
    await entry.task
    assert registry.status(entry.task_id) is entry
    # Age the completion past the TTL deterministically (no clock patching).
    # Expiry runs on the WALL clock (done_wall), not monotonic — see the field's
    # docstring (gVisor monotonic freeze observed in production).
    entry.done_wall = time.time() - 61
    assert registry.counts()["cached_results"] == 0  # gauges sweep expired cache
    assert registry.status(entry.task_id) is None  # expired == never existed


async def test_registry_start_sweeps_expired_entries() -> None:
    """The sweep on ``start()`` (not ``status()``'s own drop path) evicts an
    expired entry for a DIFFERENT key."""
    registry = ChatTaskRegistry(result_ttl_s=60.0)

    async def _work() -> dict[str, Any]:
        return {}

    old, _ = registry.start("k1", _work)
    assert old.task is not None
    await old.task
    old.done_wall = time.time() - 61
    registry.start("k2", _work)  # sweeps before claiming
    assert old.task_id not in registry._tasks  # dropped by _sweep, not by status()


async def test_registry_evicts_oldest_done_at_cap() -> None:
    registry = ChatTaskRegistry(max_tasks=2)

    async def _work() -> dict[str, Any]:
        return {}

    first, _ = registry.start("k1", _work)
    second, _ = registry.start("k2", _work)
    assert first.task is not None and second.task is not None
    await asyncio.gather(first.task, second.task)
    first.done_wall = time.time() - 60  # make k1 unambiguously the oldest
    registry.start("k3", _work)
    assert registry.status(first.task_id) is None  # evicted
    assert registry.status(second.task_id) is second  # kept


async def test_registry_aclose_cancels_running() -> None:
    """The real path: the ask is mid-flight, so ``_guard``'s CancelledError
    branch records the outcome."""
    registry = ChatTaskRegistry()

    async def _work() -> dict[str, Any]:
        await asyncio.sleep(3600)
        return {}

    entry, _ = registry.start("k1", _work)
    await asyncio.sleep(0)  # let _guard acquire the slot and await the ask
    assert entry.started_at is not None
    await registry.aclose()
    assert entry.done_at is not None and entry.done_wall is not None
    assert isinstance(entry.error, asyncio.CancelledError)


async def test_registry_cancel_stops_one_task_and_preserves_metadata() -> None:
    registry = ChatTaskRegistry()

    async def _work() -> dict[str, Any]:
        await asyncio.sleep(3600)
        return {}

    entry, _ = registry.start(
        "k1",
        _work,
        notebook_id=NB_ID,
        conversation_id=CONV_ID,
    )
    await asyncio.sleep(0)

    assert await registry.cancel(entry.task_id) is True
    assert entry.notebook_id == NB_ID
    assert entry.conversation_id == CONV_ID
    assert entry.done_at is not None
    assert isinstance(entry.error, asyncio.CancelledError)
    assert await registry.cancel(entry.task_id) is False


async def test_registry_aclose_stamps_task_cancelled_before_first_step() -> None:
    """Cancelled before its first scheduler step, a task never enters ``_guard``;
    ``aclose`` stamps both clocks itself so the entry can still expire."""
    registry = ChatTaskRegistry(result_ttl_s=60.0)

    async def _work() -> dict[str, Any]:
        await asyncio.sleep(3600)
        return {}

    entry, _ = registry.start("k1", _work)
    await registry.aclose()  # no sleep(0): cancelled pre-start
    assert entry.started_at is None
    assert entry.done_at is not None and entry.done_wall is not None
    assert isinstance(entry.error, asyncio.CancelledError)
    entry.done_wall = time.time() - 61
    assert registry.status(entry.task_id) is None  # expirable


def test_compute_chat_task_key_semantics() -> None:
    base = compute_chat_task_key(NB_ID, "q", ["s1", "s2"], CONV_ID, "lite")
    # Source-id ORDER is not semantic — the key sorts.
    assert base == compute_chat_task_key(NB_ID, "q", ["s2", "s1"], CONV_ID, "lite")
    # Every other field is: any change is a different ask.
    assert base != compute_chat_task_key(NB_ID, "q2", ["s1", "s2"], CONV_ID, "lite")
    assert base != compute_chat_task_key(NB_ID, "q", ["s1"], CONV_ID, "lite")
    assert base != compute_chat_task_key(NB_ID, "q", ["s1", "s2"], None, "lite")
    assert base != compute_chat_task_key(NB_ID, "q", ["s1", "s2"], CONV_ID, "full")
    # Field boundaries can't collide via concatenation.
    assert compute_chat_task_key(NB_ID, "ab", None, None, "lite") != compute_chat_task_key(
        NB_ID, "a", None, "b", "lite"
    )


# ---------------------------------------------------------------------------
# chat_start / chat_status (through one in-memory FastMCP session)
# ---------------------------------------------------------------------------


async def test_chat_start_then_status_full_cycle(server_factory, mock_client) -> None:
    gate = asyncio.Event()

    async def _slow_ask(*args: Any, **kwargs: Any) -> FakeAskResult:
        await gate.wait()
        return FakeAskResult(answer="42", conversation_id=CONV_ID)

    mock_client.chat.ask = AsyncMock(side_effect=_slow_ask)
    async with Client(server_factory()) as session:
        started = (
            await session.call_tool("chat_start", {"notebook": NB_ID, "question": "what?"})
        ).structured_content
        assert started["status"] == "started"
        assert started["notebook_id"] == NB_ID
        assert "chat_status" in started["hint"]
        task_id = started["task_id"]

        pending = (await session.call_tool("chat_status", {"task_id": task_id})).structured_content
        assert pending["status"] == "pending"

        gate.set()
        # Let the detached task land; the poll then returns the full payload.
        for _ in range(50):
            done = (await session.call_tool("chat_status", {"task_id": task_id})).structured_content
            if done["status"] != "pending":
                break
            await asyncio.sleep(0.01)
        assert done["status"] == "completed"
        assert done["answer"] == "42"
        assert done["notebook_id"] == NB_ID
        assert done["conversation_id"] == CONV_ID
    mock_client.chat.ask.assert_awaited_once_with(
        NB_ID, "what?", source_ids=None, conversation_id=None
    )


async def test_chat_start_dedupes_in_flight_but_reasks_after_completion(
    server_factory, mock_client
) -> None:
    gate = asyncio.Event()
    answers = iter(["FIRST answer", "SECOND answer"])

    async def _slow_ask(*args: Any, **kwargs: Any) -> FakeAskResult:
        await gate.wait()
        return FakeAskResult(answer=next(answers), conversation_id=CONV_ID)

    mock_client.chat.ask = AsyncMock(side_effect=_slow_ask)
    async with Client(server_factory()) as session:
        first = (
            await session.call_tool("chat_start", {"notebook": NB_ID, "question": "same q"})
        ).structured_content
        dup = (
            await session.call_tool("chat_start", {"notebook": NB_ID, "question": "same q"})
        ).structured_content
        assert dup["status"] == "already_running"  # in-flight dedupe
        assert dup["task_id"] == first["task_id"]

        gate.set()
        for _ in range(50):
            done = (
                await session.call_tool("chat_status", {"task_id": first["task_id"]})
            ).structured_content
            if done["status"] != "pending":
                break
            await asyncio.sleep(0.01)
        assert done["status"] == "completed"
        assert done["answer"] == "FIRST answer"

        # Asking the SAME question again after completion re-asks: a NEW task,
        # a fresh generation, the second answer — never the cached first one.
        again = (
            await session.call_tool("chat_start", {"notebook": NB_ID, "question": "same q"})
        ).structured_content
        assert again["status"] == "started"
        assert again["task_id"] != first["task_id"]
        for _ in range(50):
            done2 = (
                await session.call_tool("chat_status", {"task_id": again["task_id"]})
            ).structured_content
            if done2["status"] != "pending":
                break
            await asyncio.sleep(0.01)
        assert done2["answer"] == "SECOND answer"
        # The first payload is still pollable by its own task_id.
        old = (
            await session.call_tool("chat_status", {"task_id": first["task_id"]})
        ).structured_content
        assert old["answer"] == "FIRST answer"
    assert mock_client.chat.ask.await_count == 2


async def test_chat_start_with_source_ids_scopes_and_echoes(server_factory, mock_client) -> None:
    src_a = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    src_b = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    mock_client.chat.ask = AsyncMock(
        return_value=FakeAskResult(answer="scoped", conversation_id=CONV_ID)
    )
    async with Client(server_factory()) as session:
        started = (
            await session.call_tool(
                "chat_start",
                {"notebook": NB_ID, "question": "scoped q", "source_ids": [src_a, src_b]},
            )
        ).structured_content
        for _ in range(50):
            done = (
                await session.call_tool("chat_status", {"task_id": started["task_id"]})
            ).structured_content
            if done["status"] != "pending":
                break
            await asyncio.sleep(0.01)
        assert done["status"] == "completed"
        assert done["source_ids"] == [src_a, src_b]  # resolved scope echoed (#1808)
    assert mock_client.chat.ask.await_args.kwargs["source_ids"] == [src_a, src_b]


async def test_chat_start_capacity_projects_as_retriable(mcp_call, monkeypatch) -> None:
    """The registry fuse must surface as a RETRIABLE category (the call succeeds
    unchanged once a slot frees), never as VALIDATION, which agents stop on."""
    from notebooklm.mcp import _chattasks

    def _full(self, key, coro_factory, **kwargs):  # noqa: ANN001 - test double
        raise ChatTaskCapacityError("all 256 task slots hold unfinished asks")

    monkeypatch.setattr(_chattasks.ChatTaskRegistry, "start", _full)
    with pytest.raises(ToolError) as excinfo:
        await mcp_call("chat_start", {"notebook": NB_ID, "question": "q"})
    text = str(excinfo.value)
    assert "RATE_LIMITED" in text
    assert "VALIDATION" not in text


async def test_chat_status_rejects_oversized_batch(mcp_call) -> None:
    with pytest.raises(ToolError, match="at most 64"):
        await mcp_call("chat_status", {"task_id": [f"id{i}" for i in range(65)]})


async def test_chat_start_failure_surfaces_via_status(server_factory, mock_client) -> None:
    mock_client.chat.ask = AsyncMock(side_effect=ChatError("generation exploded"))
    async with Client(server_factory()) as session:
        started = (
            await session.call_tool("chat_start", {"notebook": NB_ID, "question": "doomed"})
        ).structured_content
        task_id = started["task_id"]
        for _ in range(50):
            status = (
                await session.call_tool("chat_status", {"task_id": task_id})
            ).structured_content
            if status["status"] != "pending":
                break
            await asyncio.sleep(0.01)
        assert status["status"] == "failed"
        # The stored exception is projected through the shared MCP error
        # vocabulary: {code, message, retriable} — agents branch on these.
        assert set(status["error"]) >= {"code", "message", "retriable"}
        assert "retry" in status["hint"]


async def test_chat_status_batch_shape_and_timings(server_factory, mock_client) -> None:
    """A LIST of task_ids polls the whole batch in one call: ``{"tasks": [...]}``
    in input order, unknown ids reported per-task, completed entries carrying
    the ``queued_s`` / ``generation_s`` timings."""
    mock_client.chat.ask = AsyncMock(
        return_value=FakeAskResult(answer="batched", conversation_id=CONV_ID)
    )
    async with Client(server_factory()) as session:
        started = (
            await session.call_tool("chat_start", {"notebook": NB_ID, "question": "batch q"})
        ).structured_content
        task_id = started["task_id"]
        for _ in range(50):
            single = (
                await session.call_tool("chat_status", {"task_id": task_id})
            ).structured_content
            if single["status"] != "pending":
                break
            await asyncio.sleep(0.01)
        assert single["status"] == "completed"
        assert single["queued_s"] >= 0.0 and single["generation_s"] >= 0.0

        batch = (
            await session.call_tool("chat_status", {"task_id": [task_id, "bogus"]})
        ).structured_content
        assert [t["task_id"] for t in batch["tasks"]] == [task_id, "bogus"]
        assert batch["tasks"][0]["status"] == "completed"
        assert batch["tasks"][0]["answer"] == "batched"
        assert batch["tasks"][1]["status"] == "unknown"


async def test_chat_status_pending_reports_queue_state(server_factory, mock_client) -> None:
    gate = asyncio.Event()

    async def _slow_ask(*args: Any, **kwargs: Any) -> FakeAskResult:
        await gate.wait()
        return FakeAskResult(answer="x", conversation_id=CONV_ID)

    mock_client.chat.ask = AsyncMock(side_effect=_slow_ask)
    async with Client(server_factory()) as session:
        started = (
            await session.call_tool("chat_start", {"notebook": NB_ID, "question": "state q"})
        ).structured_content
        pending = (
            await session.call_tool("chat_status", {"task_id": started["task_id"]})
        ).structured_content
        assert pending["status"] == "pending"
        assert pending["state"] in ("queued", "generating")
        assert pending["waited_s"] >= 0.0
        gate.set()


async def test_chat_start_rejects_blank_question(mcp_call) -> None:
    with pytest.raises(ToolError, match="non-empty question"):
        await mcp_call("chat_start", {"notebook": NB_ID, "question": "   "})


async def test_chat_status_unknown_task(mcp_call) -> None:
    result = (await mcp_call("chat_status", {"task_id": "nope"})).structured_content
    assert result["status"] == "unknown"
    assert "chat_start" in result["hint"]


async def test_chat_status_live_session_mode(mcp_call, mock_client) -> None:
    mock_client.chat.get_conversation_id = AsyncMock(return_value=CONV_ID)
    mock_client.chat.session_status = AsyncMock(
        return_value=ChatSessionStatus(generating=True, token="generation-token")
    )

    result = (await mcp_call("chat_status", {"notebook": NB_ID})).structured_content

    assert result == {
        "status": "generating",
        "notebook_id": NB_ID,
        "conversation_id": CONV_ID,
        "generation_token": "generation-token",
    }
    mock_client.chat.session_status.assert_awaited_once_with(NB_ID, CONV_ID)


async def test_chat_cancel_stops_backend_and_detached_stream(server_factory, mock_client) -> None:
    gate = asyncio.Event()

    async def _slow_ask(*args: Any, **kwargs: Any) -> FakeAskResult:
        await gate.wait()
        return FakeAskResult(answer="too late", conversation_id=CONV_ID)

    mock_client.chat.ask = AsyncMock(side_effect=_slow_ask)
    mock_client.chat.cancel = AsyncMock(return_value=None)
    async with Client(server_factory()) as session:
        started = (
            await session.call_tool(
                "chat_start",
                {
                    "notebook": NB_ID,
                    "question": "stop this",
                    "conversation_id": CONV_ID,
                },
            )
        ).structured_content
        cancelled = (
            await session.call_tool(
                "chat_cancel",
                {"notebook": NB_ID, "task_id": started["task_id"]},
            )
        ).structured_content
        status = (
            await session.call_tool("chat_status", {"task_id": started["task_id"]})
        ).structured_content

    assert cancelled == {
        "status": "cancelled",
        "cancelled": True,
        "local_task_cancelled": True,
        "notebook_id": NB_ID,
        "conversation_id": CONV_ID,
    }
    assert status["status"] == "cancelled"
    mock_client.chat.cancel.assert_awaited_once_with(NB_ID, CONV_ID)


async def test_chat_start_resolves_conversation_before_detaching(
    server_factory, mock_client
) -> None:
    gate = asyncio.Event()

    async def _slow_ask(*args: Any, **kwargs: Any) -> FakeAskResult:
        await gate.wait()
        return FakeAskResult(answer="resolved", conversation_id=CONV_ID)

    mock_client.chat.get_conversation_id = AsyncMock(return_value=CONV_ID)
    mock_client.chat.ask = AsyncMock(side_effect=_slow_ask)
    mock_client.chat.cancel = AsyncMock(return_value=None)
    async with Client(server_factory()) as session:
        started = (
            await session.call_tool("chat_start", {"notebook": NB_ID, "question": "continue"})
        ).structured_content
        cancelled = (
            await session.call_tool(
                "chat_cancel",
                {"notebook": NB_ID, "task_id": started["task_id"]},
            )
        ).structured_content

    assert cancelled["conversation_id"] == CONV_ID
    mock_client.chat.ask.assert_awaited_once_with(
        NB_ID, "continue", source_ids=None, conversation_id=CONV_ID
    )
    mock_client.chat.cancel.assert_awaited_once_with(NB_ID, CONV_ID)


async def test_chat_cancel_cancels_queued_task_without_remote_session(
    server_factory, mock_client, monkeypatch
) -> None:
    monkeypatch.setenv("NOTEBOOKLM_MCP_CHAT_CONCURRENCY", "1")
    gate = asyncio.Event()

    async def _slow_ask(*args: Any, **kwargs: Any) -> FakeAskResult:
        await gate.wait()
        return FakeAskResult(answer="first", conversation_id=CONV_ID)

    mock_client.chat.ask = AsyncMock(side_effect=_slow_ask)
    mock_client.chat.get_conversation_id = AsyncMock(return_value=None)
    mock_client.chat.cancel = AsyncMock(return_value=None)
    async with Client(server_factory()) as session:
        first = (
            await session.call_tool("chat_start", {"notebook": NB_ID, "question": "first"})
        ).structured_content
        second = (
            await session.call_tool("chat_start", {"notebook": NB_ID, "question": "queued"})
        ).structured_content
        pending = (
            await session.call_tool("chat_status", {"task_id": second["task_id"]})
        ).structured_content
        assert pending["state"] == "queued"

        cancelled = (
            await session.call_tool(
                "chat_cancel",
                {"notebook": NB_ID, "task_id": second["task_id"]},
            )
        ).structured_content
        first_status = (
            await session.call_tool("chat_status", {"task_id": first["task_id"]})
        ).structured_content
        gate.set()

    assert cancelled == {
        "status": "cancelled",
        "cancelled": True,
        "local_task_cancelled": True,
        "notebook_id": NB_ID,
        "conversation_id": None,
    }
    assert first_status["status"] == "pending"
    mock_client.chat.cancel.assert_not_awaited()


async def test_chat_cancel_terminal_task_does_not_cancel_newer_session(
    server_factory, mock_client
) -> None:
    mock_client.chat.ask = AsyncMock(
        return_value=FakeAskResult(answer="done", conversation_id=CONV_ID)
    )
    mock_client.chat.cancel = AsyncMock(return_value=None)
    async with Client(server_factory()) as session:
        started = (
            await session.call_tool(
                "chat_start",
                {"notebook": NB_ID, "question": "done", "conversation_id": CONV_ID},
            )
        ).structured_content
        for _ in range(50):
            status = (
                await session.call_tool("chat_status", {"task_id": started["task_id"]})
            ).structured_content
            if status["status"] != "pending":
                break
            await asyncio.sleep(0.01)
        assert status["status"] == "completed"

        cancelled = (
            await session.call_tool(
                "chat_cancel",
                {"notebook": NB_ID, "task_id": started["task_id"]},
            )
        ).structured_content

    assert cancelled == {
        "status": "already_finished",
        "cancelled": False,
        "local_task_cancelled": False,
        "notebook_id": NB_ID,
        "conversation_id": CONV_ID,
    }
    mock_client.chat.cancel.assert_not_awaited()


async def test_chat_cancel_rejects_task_conversation_mismatch(server_factory, mock_client) -> None:
    gate = asyncio.Event()

    async def _slow_ask(*args: Any, **kwargs: Any) -> FakeAskResult:
        await gate.wait()
        return FakeAskResult(answer="still scoped", conversation_id=CONV_ID)

    mock_client.chat.ask = AsyncMock(side_effect=_slow_ask)
    async with Client(server_factory()) as session:
        started = (
            await session.call_tool(
                "chat_start",
                {
                    "notebook": NB_ID,
                    "question": "keep scoped",
                    "conversation_id": CONV_ID,
                },
            )
        ).structured_content
        with pytest.raises(ToolError, match="different conversation"):
            await session.call_tool(
                "chat_cancel",
                {
                    "notebook": NB_ID,
                    "conversation_id": "33333333-3333-3333-3333-333333333333",
                    "task_id": started["task_id"],
                },
            )
