"""Stage B1 PR 2 — executor persists across ``close()`` → ``open()``.

Replacement regression test for the deleted
``test_session_lifecycle.test_close_nulls_rpc_executor``. Before
Stage B1 PR 2 of the post-refactoring plan, :meth:`ClientLifecycle.close`
nulled out ``host._rpc_executor`` so a follow-up :meth:`open` would
trigger the lazy ``Session._get_rpc_executor`` factory to rebuild the
executor against the new ``httpx.AsyncClient``.

PR 2 deleted both that null line and the lazy factory itself — the
executor is bound exactly once by the composition root
(:func:`notebooklm._session.compose_session_internals`) via
:meth:`Session._bind_executor`, and the same instance survives any
``close()`` → ``open()`` cycle. This is safe because the executor's
transport collaborator (:class:`Kernel`) rebuilds its
``httpx.AsyncClient`` lazily on each :meth:`Kernel.open`, so a stale
executor reference continues to drive RPCs against a fresh transport.

This module pins three load-bearing invariants:

1. The :class:`RpcExecutor` instance is identity-equal before and after
   a full ``close()`` → ``open()`` cycle.
2. The reused executor can still execute an RPC after the cycle (it is
   not bound to a stale transport reference).
3. The same regression vector that the deleted
   ``test_close_nulls_rpc_executor`` blocked — a follow-up
   :meth:`open` quietly missing the executor — surfaces as a clean
   ``RuntimeError`` if the composition contract is ever broken (the
   :meth:`_require_constructed` guard fires in :meth:`Session.rpc_call`
   when ``_rpc_executor`` is ``None``).
"""

from __future__ import annotations

from typing import Any

import pytest

from _helpers.session_factory import build_session_for_tests
from notebooklm.auth import AuthTokens
from notebooklm.rpc import RPCMethod


def _make_auth() -> AuthTokens:
    return AuthTokens(
        cookies={"SID": "x", "__Secure-1PSIDTS": "y"},
        csrf_token="csrf",
        session_id="sid",
    )


@pytest.mark.asyncio
async def test_executor_identity_survives_close_then_open() -> None:
    """``open()`` → ``close()`` → ``open()`` preserves the executor instance.

    Pins the Stage B1 PR 2 contract: the composition root binds the
    executor exactly once and :meth:`ClientLifecycle.close` no longer
    nulls ``host._rpc_executor``. The same :class:`RpcExecutor`
    reference drives RPCs across the lifecycle cycle — feature
    adapters that captured the executor at construction time
    (``ChatAPI`` / ``SourcesAPI`` / etc.) do not need to re-grab it.
    """
    core = build_session_for_tests(_make_auth())
    initial_executor = core._rpc_executor
    assert initial_executor is not None, "composition root must bind the executor"

    await core.open()
    try:
        assert core._rpc_executor is initial_executor, (
            "open() must not rebind the executor — it persists from composition"
        )
    finally:
        await core.close()

    # Stage B1 PR 2 dropped the close-time null on _rpc_executor; the
    # binding survives close().
    assert core._rpc_executor is initial_executor, (
        "close() must not null the executor — Stage B1 PR 2 dropped that step"
    )

    await core.open()
    try:
        assert core._rpc_executor is initial_executor, (
            "second open() also leaves the executor alone — same instance "
            "throughout the close()→open() cycle"
        )
    finally:
        await core.close()


@pytest.mark.asyncio
async def test_rpc_call_succeeds_after_close_then_open_with_same_executor() -> None:
    """A reused executor still executes RPCs after a full lifecycle cycle.

    The fail-fast guard in :meth:`Session.rpc_call` requires
    ``_rpc_executor`` to be set; if Stage B1 PR 2 had accidentally
    re-nulled the slot inside :meth:`ClientLifecycle.close`, the second
    ``rpc_call`` after the cycle would raise
    ``RuntimeError("Session not fully constructed: _rpc_executor is None")``.
    This test exercises the call path end-to-end through a mocked
    executor to confirm the binding survives.
    """
    core = build_session_for_tests(_make_auth())
    executor = core._rpc_executor
    assert executor is not None

    # Stub ``rpc_call`` on the executor with a plain async function
    # rather than ``unittest.mock.AsyncMock`` — ADR-007 forbids
    # ``Mock`` / ``AsyncMock`` attribute assignment as a test seam, so
    # we use a captured-state ``async def`` to record the dispatch.
    # This is the same pattern as ``_fixtures/fake_core.py``: an
    # ordinary callable substituted for a method, no mock library
    # involved.
    sentinel: dict[str, Any] = {"call_count": 0}

    async def fake_rpc_call(*_args: Any, **_kwargs: Any) -> str:
        sentinel["call_count"] += 1
        return "ok"

    executor.rpc_call = fake_rpc_call  # type: ignore[method-assign,assignment]

    # Drive a full lifecycle cycle.
    await core.open()
    result1 = await core.rpc_call(RPCMethod.LIST_NOTEBOOKS, [])
    await core.close()

    # Critical re-open + rpc_call — the deleted close-time null would
    # have left ``_rpc_executor`` at ``None`` here, raising from the
    # fail-fast guard.
    await core.open()
    try:
        result2 = await core.rpc_call(RPCMethod.LIST_NOTEBOOKS, [])
    finally:
        await core.close()

    assert result1 == "ok"
    assert result2 == "ok"
    assert sentinel["call_count"] == 2
    # The executor reference never moved — both calls dispatched
    # through the same fake.
    assert core._rpc_executor is executor


def test_rpc_call_raises_actionably_when_executor_is_unbound() -> None:
    """The :meth:`_require_constructed` guard fires if ``_rpc_executor`` is ``None``.

    Tests the contract that would have caught a regression where the
    close-time null was reintroduced and the next :meth:`open` did not
    rebind. Bypasses the composition root via ``Session.__new__`` to
    simulate that broken state.
    """
    import asyncio

    from notebooklm._session import Session

    session = Session.__new__(Session)
    # ``_rpc_executor`` is unset on a __new__ instance — the fail-fast
    # guard inside :meth:`Session.rpc_call` should fire with a clear
    # message before reaching the (also-unset) executor.
    with pytest.raises(RuntimeError, match="Session not fully constructed: _rpc_executor is None"):
        asyncio.run(session.rpc_call(RPCMethod.LIST_NOTEBOOKS, []))
