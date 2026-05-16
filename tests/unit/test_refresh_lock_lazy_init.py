"""regression tests for ``ClientCore._refresh_lock`` lazy-init.

Pins two behaviors:

1. ``ClientCore`` can be constructed outside a running event loop even when
   a ``refresh_callback`` is wired. Before the fix, the constructor created
   ``asyncio.Lock()`` eagerly, which fails under some Python versions when
   no loop is running.

2. The lock is allocated on the first ``_await_refresh`` and that refresh
   succeeds — i.e. lazy-init does not regress the single-flight dedupe
   contract pinned by ``test_refresh_state_machine.py``.
"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import pytest

from notebooklm._core import ClientCore
from notebooklm.auth import AuthTokens
from notebooklm.rpc import AuthError, RPCMethod

_UNIT_CONFTEST_SPEC = importlib.util.spec_from_file_location(
    "unit_conftest_make_core",
    Path(__file__).resolve().parent / "conftest.py",
)
assert _UNIT_CONFTEST_SPEC is not None and _UNIT_CONFTEST_SPEC.loader is not None
_unit_conftest = importlib.util.module_from_spec(_UNIT_CONFTEST_SPEC)
_UNIT_CONFTEST_SPEC.loader.exec_module(_unit_conftest)
make_core = _unit_conftest.make_core

EVENT_TIMEOUT_S = 5.0


def _auth_tokens() -> AuthTokens:
    return AuthTokens(
        cookies={"SID": "test_sid"},
        csrf_token="test_csrf",
        session_id="test_session",
    )


async def _noop_refresh() -> AuthTokens:
    """Throwaway callback — never invoked by the construction test."""
    return _auth_tokens()


# --------------------------------------------------------------------------- #
# (1) Construction outside an event loop
# --------------------------------------------------------------------------- #


def test_construct_outside_event_loop_with_callback() -> None:
    """``ClientCore(refresh_callback=...)`` must succeed with no running loop.

    Previously, the eager ``asyncio.Lock()`` in ``__init__`` could raise
    ``RuntimeError: no running event loop`` on some interpreters / asyncio
    versions when the client was constructed from sync code. Lazy-init
    moves that allocation to the first ``_await_refresh`` call.

    Sanity check: ``asyncio.get_running_loop()`` must currently raise. If a
    plugin (e.g. ``pytest-asyncio``) sneaks a loop in for sync tests, this
    test's premise is invalid and we want to know — fail loudly rather
    than silently pass.
    """
    with pytest.raises(RuntimeError, match="no running event loop"):
        asyncio.get_running_loop()

    # Eager construction would have blown up under the prior code path.
    core_with_cb = ClientCore(auth=_auth_tokens(), refresh_callback=_noop_refresh)
    assert core_with_cb._refresh_lock is None, (
        "Lazy-init contract: lock must remain None until first refresh."
    )
    assert core_with_cb._refresh_callback is _noop_refresh

    # And the no-callback path stays the same (also lazy / also None).
    core_without_cb = ClientCore(auth=_auth_tokens())
    assert core_without_cb._refresh_lock is None
    assert core_without_cb._refresh_callback is None


# --------------------------------------------------------------------------- #
# (2) Refresh works on first await
# --------------------------------------------------------------------------- #


async def _trigger_refresh(core: ClientCore) -> object:
    """Drive ``_try_refresh_and_retry`` with throwaway args (matches the
    helper in ``test_refresh_state_machine.py`` so this test pins the same
    code path)."""
    return await core._try_refresh_and_retry(
        RPCMethod.LIST_NOTEBOOKS,
        [],
        "/",
        False,
        AuthError("simulated"),
    )


@pytest.mark.asyncio
async def test_refresh_lock_allocated_on_first_await() -> None:
    """First ``_await_refresh`` allocates the lock and completes successfully.

    Proves the lazy-init wiring: lock starts ``None`` after construction
    (verified inside the running loop here too), is non-``None`` after the
    first refresh, and refresh-task succeeded (single-flight contract
    unchanged — see ``test_refresh_state_machine.py`` for the deeper
    dedupe pinning).
    """
    call_count = 0
    core_box: list[ClientCore] = []

    async def cb() -> AuthTokens:
        nonlocal call_count
        call_count += 1
        tokens = AuthTokens(
            csrf_token="CSRF_REFRESHED",
            session_id="SID_REFRESHED",
            cookies={"SID": "post_refresh"},
        )
        # Mirror real-world callback behavior: update core.auth in place so
        # ``_try_refresh_and_retry``'s subsequent ``rpc_call`` retry sees
        # the new tokens.
        core_box[0].auth.csrf_token = tokens.csrf_token
        core_box[0].auth.session_id = tokens.session_id
        return tokens

    async with make_core(refresh_callback=cb) as core:
        core_box.append(core)

        # Stub out the retry so the test stays focused on the refresh-lock
        # allocation path — matches the pattern in
        # ``test_refresh_state_machine.py::test_concurrent_callers_share_single_refresh``.
        async def fake_retry(*args: object, **kwargs: object) -> str:
            return "ok"

        core.rpc_call = fake_retry  # type: ignore[method-assign]

        # Pre-refresh invariant: lock is unallocated even after ``open()``.
        assert core._refresh_lock is None, (
            "Lock must remain unallocated until the first refresh attempt."
        )

        result = await asyncio.wait_for(_trigger_refresh(core), EVENT_TIMEOUT_S)

        assert result == "ok"
        assert call_count == 1, f"Refresh callback must fire exactly once, got {call_count}"
        # Post-refresh invariant: lock is now allocated and is a real asyncio.Lock.
        assert core._refresh_lock is not None, (
            "Lock must be allocated by the first ``_await_refresh`` call."
        )
        assert isinstance(core._refresh_lock, asyncio.Lock)
        # And the refresh task ran to completion, matching the single-flight
        # state-machine pinning in ``test_refresh_state_machine.py``.
        assert core._refresh_task is not None
        assert core._refresh_task.done()
        assert core.auth.csrf_token == "CSRF_REFRESHED"


@pytest.mark.asyncio
async def test_refresh_lock_instance_stable_across_calls() -> None:
    """Repeated refreshes resolve to the SAME lock instance.

    Single-flight depends on every caller acquiring the same lock; this
    test pins that ``_get_refresh_lock`` is idempotent — important because
    a buggy lazy-init that re-creates the lock on each call would silently
    break dedupe (each caller would enter its own critical section in
    parallel).
    """

    async def cb() -> AuthTokens:
        return AuthTokens(
            csrf_token="R",
            session_id="S",
            cookies={"SID": "sid"},
        )

    async with make_core(refresh_callback=cb) as core:

        async def fake_retry(*args: object, **kwargs: object) -> str:
            return "ok"

        core.rpc_call = fake_retry  # type: ignore[method-assign]

        await _trigger_refresh(core)
        first_lock = core._refresh_lock
        assert first_lock is not None

        await _trigger_refresh(core)
        second_lock = core._refresh_lock

        assert second_lock is first_lock, (
            "Lazy-init must be idempotent — same lock instance across refreshes "
            "to preserve single-flight dedupe."
        )
