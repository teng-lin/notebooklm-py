"""Regression test for the shield UPDATE_NOTE finalize, cleanup on cancel.

Audit item §28: ``_mind_map.create_note`` issued CREATE_NOTE then UPDATE_NOTE
back-to-back. A cancellation arriving after CREATE_NOTE returned but before
UPDATE_NOTE completed left an empty/orphan note on the server (CREATE_NOTE
persists; UPDATE_NOTE applies the title/content payload that callers expect).

Post-fix:
- UPDATE_NOTE call is wrapped in ``asyncio.shield`` so an outer cancel cannot
  abort an in-flight finalize.
- On ``CancelledError`` raised during child publication or the shielded await,
  a best-effort DELETE_NOTE child is supervised without blocking the re-raise.

Acceptance invariant:
  cancel mid-flight after CREATE_NOTE returns but before UPDATE_NOTE
  completes; assert either
    (a) both succeed (shield wins — UPDATE_NOTE finished within the shielded
        await before the outer cancel arrived), OR
    (b) both effectively undone (UPDATE_NOTE was cancelled and the best-effort
        DELETE_NOTE cleanup ran on the mock transport).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from notebooklm import NotebookLMClient
from notebooklm.rpc import RPCMethod
from tests._helpers.client_factory import build_client_shell_for_tests

# mock-transport cancel-during-create tests; no HTTP, no cassette.
# Opt out of the tier-enforcement hook in tests/integration/conftest.py.
pytestmark = pytest.mark.allow_no_vcr


def _wrb_response(rpc_id: str, payload) -> str:
    """Single-RPC batchexecute response body in ``)]}}\\n<len>\\n<chunk>\\n`` form."""
    inner = json.dumps(payload)
    chunk = json.dumps([["wrb.fr", rpc_id, inner, None, None]])
    return f")]}}'\n{len(chunk)}\n{chunk}\n"


def _rpc_id_in_request(request: httpx.Request) -> str | None:
    """Extract the ``rpcids=`` query param from a batchexecute request URL."""
    for key, value in request.url.params.multi_items():
        if key == "rpcids":
            return value
    return None


async def _open_client_with_transport(
    transport: httpx.AsyncBaseTransport, auth_tokens
) -> NotebookLMClient:
    """Open through the root lifecycle with a synthetic HTTP transport."""

    def _client_factory(**kwargs: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport, **kwargs)

    client = build_client_shell_for_tests(
        auth_tokens,
        async_client_factory=_client_factory,
    )
    await client.__aenter__()
    generation = client._collaborators.call_supervisor._current
    assert generation is not None
    epoch = client._collaborators.lifecycle._epoch
    assert generation.epoch == epoch
    assert client._collaborators.web_transport._active_epoch == epoch
    assert client._collaborators.kernel._active_epoch == epoch
    assert client._collaborators.auth_coord._active_epoch == epoch
    return client


class _NoteCancelTransport(httpx.AsyncBaseTransport):
    """Mock transport that lets the caller pace UPDATE_NOTE precisely.

    ``update_started`` fires on UPDATE_NOTE entry so the test can cancel the
    outer task at exactly that point. ``release_update`` (an asyncio.Event)
    is awaited inside UPDATE_NOTE so the test controls when the shielded
    finalize actually completes.
    """

    def __init__(self) -> None:
        self.captured: list[tuple[str | None, httpx.Request]] = []
        self.update_started = asyncio.Event()
        self.release_update = asyncio.Event()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        rpc_id = _rpc_id_in_request(request)
        self.captured.append((rpc_id, request))

        if rpc_id == RPCMethod.CREATE_NOTE.value:
            # CREATE_NOTE returns the new note id; mirrors the web mind-map note service.
            # parsing path that pulls ``result[0][0]`` when result is nested.
            return httpx.Response(
                200,
                text=_wrb_response(RPCMethod.CREATE_NOTE.value, [["note_new_001"]]),
            )

        if rpc_id == RPCMethod.UPDATE_NOTE.value:
            # Signal the test that UPDATE_NOTE has begun, then suspend until
            # the test releases us. This is the precise window the
            # cancellation shield must protect.
            self.update_started.set()
            await self.release_update.wait()
            return httpx.Response(
                200,
                text=_wrb_response(RPCMethod.UPDATE_NOTE.value, None),
            )

        if rpc_id == RPCMethod.DELETE_NOTE.value:
            return httpx.Response(
                200,
                text=_wrb_response(RPCMethod.DELETE_NOTE.value, None),
            )

        # Any other RPC: empty payload (decodes to None / []).
        return httpx.Response(200, text=_wrb_response(rpc_id or "unknown", None))

    def rpc_ids(self) -> list[str | None]:
        return [rpc_id for rpc_id, _ in self.captured]


@pytest.mark.asyncio
async def test_cancel_during_update_note_shields_or_cleans_up(auth_tokens) -> None:
    """Cancel after CREATE_NOTE / before UPDATE_NOTE completes.

    The acceptance invariant from the plan: assert EITHER both writes
    succeed (shield wins) OR a DELETE_NOTE cleanup task fired on the
    transport. Either branch proves the orphan-note bug is gone.
    """
    transport = _NoteCancelTransport()
    client = await _open_client_with_transport(transport, auth_tokens)

    try:
        create_task = asyncio.create_task(
            client.notes.create("nb_test", title="Hello", content="World")
        )

        # Let CREATE_NOTE run to completion AND let UPDATE_NOTE enter the
        # transport. The child reaches this seam before NoteTaskRegistry.spawn
        # necessarily publishes it back to the parent, pinning both the
        # publication-reservation race and the later shielded-await window.
        await asyncio.wait_for(transport.update_started.wait(), timeout=2.0)
        registry = client.notes._notes._task_registry
        active_tasks = registry.active_tasks()
        assert len(active_tasks) == 1
        active_task = active_tasks[0]
        assert active_task is create_task or active_task.get_name() == (
            "note-update-nb_test-note_new_001"
        )

        # Cancel mid-UPDATE_NOTE. If the child has been published, the shield
        # keeps it running; if publication is still reserved, spawn_child
        # cancels and settles it. Both paths must schedule DELETE_NOTE.
        create_task.cancel()

        # Yield so the cancellation can be delivered and any best-effort
        # cleanup task can be scheduled.
        await asyncio.sleep(0)

        # Release UPDATE_NOTE so the published/shielded branch can finish. In
        # the reserved-publication branch the supervisor has already cancelled
        # and settled it, so setting the event is harmless.
        transport.release_update.set()

        # Drain the outer task. May raise CancelledError (cancel propagated
        # because the await released after release_update fired) or return
        # normally if the shielded UPDATE_NOTE happened to complete before
        # the cancel landed. Both are acceptance-valid.
        cancelled = False
        try:
            await create_task
        except asyncio.CancelledError:
            cancelled = True

        # Yield enough times for the best-effort DELETE_NOTE create_task to
        # be scheduled AND issue its request on the mock transport.
        for _ in range(50):  # up to ~0.5s
            if RPCMethod.DELETE_NOTE.value in transport.rpc_ids():
                break
            await asyncio.sleep(0.01)

        rpc_ids = transport.rpc_ids()

        # CREATE_NOTE is always observed — that's the prerequisite for the
        # cancellation window we're testing.
        assert RPCMethod.CREATE_NOTE.value in rpc_ids, (
            f"CREATE_NOTE never reached the transport: rpc_ids={rpc_ids!r}"
        )

        # UPDATE_NOTE is always observed: even when shield-protected, the
        # inner task keeps running through the await point.
        assert RPCMethod.UPDATE_NOTE.value in rpc_ids, (
            f"UPDATE_NOTE never reached the transport: rpc_ids={rpc_ids!r}"
        )

        # Acceptance OR:
        #   (a) shield won — outer task returned normally, both writes
        #       succeeded, no cleanup required.
        #   (b) cancel landed — DELETE_NOTE cleanup task ran.
        delete_ran = RPCMethod.DELETE_NOTE.value in rpc_ids
        if cancelled:
            assert delete_ran, (
                "outer task was cancelled but no best-effort DELETE_NOTE "
                f"cleanup reached the transport: rpc_ids={rpc_ids!r}"
            )
        else:
            # Shield won: UPDATE_NOTE completed before cancel propagated.
            # No cleanup expected.
            assert not delete_ran, (
                "shield won (no CancelledError) yet DELETE_NOTE still fired — "
                f"cleanup should only run on cancel: rpc_ids={rpc_ids!r}"
            )
    finally:
        # Never leave the mock UPDATE barrier closed when an assertion fails.
        # Once ``update_started`` fires, scheduling may leave the registry in
        # either its reservation state or its published-child state; both are
        # valid, but a failure before the release above must not turn into a
        # misleading global-timeout failure inside graceful client shutdown.
        transport.release_update.set()
        await client.close()


@pytest.mark.asyncio
async def test_no_cancel_no_cleanup(auth_tokens) -> None:
    """Sanity: when no cancel arrives, CREATE+UPDATE succeed and DELETE never fires.

    Guards against an over-eager fix that fires DELETE_NOTE unconditionally
    (which would silently nuke every newly-created note).
    """
    transport = _NoteCancelTransport()
    # Release UPDATE_NOTE immediately — no cancel will arrive.
    transport.release_update.set()
    client = await _open_client_with_transport(transport, auth_tokens)

    try:
        note = await client.notes.create("nb_test", title="Hello", content="World")
        assert note.id == "note_new_001"

        rpc_ids = transport.rpc_ids()
        assert RPCMethod.CREATE_NOTE.value in rpc_ids
        assert RPCMethod.UPDATE_NOTE.value in rpc_ids
        assert RPCMethod.DELETE_NOTE.value not in rpc_ids, (
            "DELETE_NOTE fired on a non-cancelled create — cleanup is "
            f"over-eager: rpc_ids={rpc_ids!r}"
        )
    finally:
        await client.close()
