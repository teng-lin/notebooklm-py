"""Unit tests for the loop-affinity guard (P0-2).

The free helper :func:`notebooklm._loop_affinity.assert_bound_loop` is the
new shared chokepoint that every async entry point on the seam helpers
(``_transport_drain.TransportDrainTracker.drain``,
``_reqid_counter.ReqidCounter.next_reqid``,
``_session_auth.AuthRefreshCoordinator.await_refresh``,
``_artifact_polling.ArtifactPollingService.wait_for_completion``,
``_chat.ChatAPI.ask``) now consults so a cross-loop call surfaces an
actionable ``RuntimeError`` at the call site rather than hanging on a
lock bound to a dead loop.

The guard in ``Session._perform_authed_post`` already covers the
transport-POST path. The shared guard extends the same contract to the
four async entry points that don't pass through that POST path (drain,
reqid, auth refresh, artifact polling) and to the chat-ask lock that
``_perform_authed_post`` only catches *after* the per-conversation lock
acquire — too late.

Acceptance:
- ``bound_loop=None`` is a silent no-op (lazy / unopened helpers).
- ``bound_loop=<current loop>`` is a silent no-op (steady state).
- ``bound_loop=<a different loop>`` raises ``RuntimeError`` with the same
  diagnostic the transport guard uses.
- Each of the 5 guarded entry points calls :func:`assert_bound_loop` with
  its own bound-loop reference before any awaits that touch loop-bound
  primitives (so cross-loop misuse never hits the lock-wait path).
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest

from notebooklm._artifact_polling import ArtifactPollingService
from notebooklm._loop_affinity import assert_bound_loop
from notebooklm._reqid_counter import ReqidCounter
from notebooklm._session_auth import AuthRefreshCoordinator
from notebooklm._transport_drain import TransportDrainTracker

# ---------------------------------------------------------------------------
# Free helper — the building block.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assert_bound_loop_none_is_noop() -> None:
    """``bound_loop=None`` must never raise.

    Standalone fixtures and lazy-init paths construct the seam helpers
    without ever observing an ``open()``. The guard's job is to catch
    cross-loop misuse, not to enforce that a binding has happened.
    """
    # Should not raise.
    assert_bound_loop(None)


@pytest.mark.asyncio
async def test_assert_bound_loop_matching_loop_is_noop() -> None:
    """Steady-state: same loop as the captured binding → no raise."""
    current = asyncio.get_running_loop()
    # Should not raise.
    assert_bound_loop(current)


def test_assert_bound_loop_mismatch_raises_runtime_error() -> None:
    """Cross-loop call → ``RuntimeError`` with the canonical message.

    Runs the guard under a fresh ``asyncio.run`` while passing in the
    *other* loop reference; the mismatch must be caught and surfaced as
    ``RuntimeError`` containing the canonical "bound to a different event
    loop" phrase used by the transport guard for diagnostic consistency.
    """
    other_loop = asyncio.new_event_loop()
    try:

        async def inner() -> None:
            # ``other_loop`` is NOT the loop currently running ``inner()``;
            # ``asyncio.run`` below builds its own loop.
            assert_bound_loop(other_loop)

        with pytest.raises(RuntimeError, match="different event loop"):
            asyncio.run(inner())
    finally:
        other_loop.close()


# ---------------------------------------------------------------------------
# Per-seam wiring — each guarded entry point consults its own bound-loop.
# ---------------------------------------------------------------------------


def test_drain_guards_against_cross_loop_call() -> None:
    """``TransportDrainTracker.drain`` must raise on cross-loop misuse.

    Bind the tracker to loop A, then drive ``drain()`` from a fresh loop B
    via ``asyncio.run``. The cross-loop guard at the top of ``drain``
    must catch the mismatch before the condition acquire would otherwise
    hang on a lock bound to loop A.
    """
    tracker = TransportDrainTracker()
    other_loop = asyncio.new_event_loop()
    try:
        tracker.set_bound_loop(other_loop)

        async def inner() -> None:
            await tracker.drain()

        with pytest.raises(RuntimeError, match="different event loop"):
            asyncio.run(inner())
    finally:
        other_loop.close()


def test_next_reqid_guards_against_cross_loop_call() -> None:
    """``ReqidCounter.next_reqid`` must raise on cross-loop misuse."""
    counter = ReqidCounter()
    other_loop = asyncio.new_event_loop()
    try:
        counter.set_bound_loop(other_loop)

        async def inner() -> int:
            return await counter.next_reqid()

        with pytest.raises(RuntimeError, match="different event loop"):
            asyncio.run(inner())
    finally:
        other_loop.close()


def test_await_refresh_guards_against_cross_loop_call() -> None:
    """``AuthRefreshCoordinator.await_refresh`` must raise on cross-loop misuse."""

    async def _refresh_cb() -> Any:
        raise AssertionError("refresh callback should not run on cross-loop call")

    coord = AuthRefreshCoordinator(refresh_callback=_refresh_cb)
    other_loop = asyncio.new_event_loop()
    try:
        coord.set_bound_loop(other_loop)

        # Wave 3b of session-decoupling (Task 1.0): ``await_refresh`` no
        # longer takes a host parameter — the lock-wait metric is recorded
        # via the coordinator's own ``metrics`` field (None here, which is
        # a safe fallback). The cross-loop guard short-circuits before any
        # metric is recorded either way.
        async def inner() -> None:
            await coord.await_refresh()

        with pytest.raises(RuntimeError, match="different event loop"):
            asyncio.run(inner())
    finally:
        other_loop.close()


def test_wait_for_completion_guards_against_cross_loop_call() -> None:
    """``ArtifactPollingService.wait_for_completion`` must raise on cross-loop misuse.

    The service routes the guard through its capability adapter's
    ``assert_bound_loop`` method.
    """
    capabilities = MagicMock()
    other_loop = asyncio.new_event_loop()
    try:
        capabilities.assert_bound_loop = MagicMock(
            side_effect=RuntimeError("NotebookLM client used from a different event loop")
        )

        service = ArtifactPollingService(capabilities)

        async def _unused_poll(_nb: str, _task: str) -> Any:
            raise AssertionError("poll_status should not run on cross-loop call")

        async def inner() -> None:
            await service.wait_for_completion(
                "nb-id",
                "task-id",
                poll_status=_unused_poll,
            )

        with pytest.raises(RuntimeError, match="different event loop"):
            asyncio.run(inner())
    finally:
        other_loop.close()


def test_chat_ask_guards_against_cross_loop_call() -> None:
    """``ChatAPI.ask`` must raise on cross-loop misuse.

    The chat entry calls its core capability's ``assert_bound_loop`` *before*
    acquiring the per-conversation lock so a cross-loop follow-up doesn't
    hang on a lock bound to a dead loop.
    """
    from notebooklm._chat import ChatAPI

    capabilities = MagicMock()
    other_loop = asyncio.new_event_loop()
    try:
        capabilities.assert_bound_loop = MagicMock(
            side_effect=RuntimeError("NotebookLM client used from a different event loop")
        )

        chat = ChatAPI(capabilities)

        async def inner() -> None:
            await chat.ask("nb-id", "question", source_ids=["src-1"])

        with pytest.raises(RuntimeError, match="different event loop"):
            asyncio.run(inner())
    finally:
        other_loop.close()


def test_add_file_guards_against_cross_loop_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """``SourceUploadPipeline.add_file`` must raise on cross-loop misuse.

    Regression guard for audit finding C1: previously the upload pipeline
    entered ``operation_scope`` and acquired the lazy upload
    ``asyncio.Semaphore`` *before* any loop check, so a cross-loop
    ``client.sources.add_file(...)`` could attach the semaphore to the
    wrong loop before the documented ``RuntimeError`` guard fired.

    The new contract: ``add_file`` calls ``runtime.assert_bound_loop()`` as
    its first statement (mirroring ``ArtifactPollingService.wait_for_completion``
    and ``ChatAPI.ask``) so cross-loop misuse surfaces a clean
    ``RuntimeError`` before any loop-bound primitive is touched. The
    ``UploadRuntime`` Protocol must declare ``LoopGuard`` so the
    structural type check covers the new attribute.
    """
    from notebooklm._source_upload import SourceUploadPipeline

    runtime = MagicMock()
    runtime.assert_bound_loop = MagicMock(
        side_effect=RuntimeError("NotebookLM client used from a different event loop")
    )
    kernel = MagicMock()
    auth = MagicMock()

    # Construct the pipeline outside any running loop — its ``__init__`` is
    # event-loop-agnostic; the cross-loop guard fires inside ``add_file``.
    pipeline = SourceUploadPipeline(runtime, kernel, auth)
    register_file_source = MagicMock(side_effect=AssertionError("register should not run"))
    start_resumable_upload = MagicMock(side_effect=AssertionError("start should not run"))
    upload_file_streaming = MagicMock(side_effect=AssertionError("stream should not run"))
    monkeypatch.setattr(pipeline, "register_file_source", register_file_source)
    monkeypatch.setattr(pipeline, "start_resumable_upload", start_resumable_upload)
    monkeypatch.setattr(pipeline, "upload_file_streaming", upload_file_streaming)

    async def inner() -> None:
        await pipeline.add_file(
            "nb-id",
            "/nonexistent/path/should-never-be-touched.pdf",
        )

    with pytest.raises(RuntimeError, match="different event loop"):
        asyncio.run(inner())

    # Confirm the cross-loop guard fired *before* any collaborator was
    # touched. Three independent witnesses to the contract: the guard
    # was called once, ``operation_scope`` (the loop-bound async-context
    # manager the audit specifically calls out) was never entered, upload
    # collaborators were never touched, and the lazy upload semaphore was
    # never allocated.
    runtime.assert_bound_loop.assert_called_once()
    runtime.operation_scope.assert_not_called()
    register_file_source.assert_not_called()
    start_resumable_upload.assert_not_called()
    upload_file_streaming.assert_not_called()
    assert pipeline._upload_semaphore is None
