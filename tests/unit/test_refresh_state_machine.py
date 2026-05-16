"""Phase 1.5 P1.5.4 — refresh state-machine regression tests.

Pins three behaviors of ``ClientCore._try_refresh_and_retry``:

1. Concurrent callers share the same in-flight refresh task (single-flight).
2. Refresh failures propagate to all waiters with chained ``__cause__``.
3. A second wave after the first task completes creates a *new* task
   (the slot is not silently reused).
"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import pytest

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

# Tight enough to fail fast if a regression hangs the suite, generous enough
# not to flake on a slow CI runner. Each event-wait should resolve in <100ms;
# 5s is two orders of magnitude of headroom.
EVENT_TIMEOUT_S = 5.0


async def _trigger_refresh(core):
    """Drive ``_try_refresh_and_retry`` with throwaway args."""
    return await core._try_refresh_and_retry(
        RPCMethod.LIST_NOTEBOOKS,
        [],
        "/",
        False,
        AuthError("simulated"),
    )


async def _wait_for_inflight_refresh_task(core, ticks: int = 20) -> bool:
    """Yield up to ``ticks`` times for the shared refresh task to appear."""
    for _ in range(ticks):
        await asyncio.sleep(0)
        if core._refresh_task is not None and not core._refresh_task.done():
            return True
    return False


@pytest.mark.asyncio
async def test_concurrent_callers_share_single_refresh():
    callback_entered = asyncio.Event()
    release_refresh = asyncio.Event()
    call_count = 0
    core_box: list = []

    async def cb():
        nonlocal call_count
        call_count += 1
        callback_entered.set()
        await release_refresh.wait()
        tokens = AuthTokens(
            csrf_token="CSRF_REFRESHED",
            session_id="SID_REFRESHED",
            cookies={"SID": "post_refresh"},
        )
        # Mirror real-world callback behavior: update core.auth in place.
        core_box[0].auth.csrf_token = tokens.csrf_token
        core_box[0].auth.session_id = tokens.session_id
        return tokens

    async with make_core(refresh_callback=cb) as core:
        core_box.append(core)

        async def fake_retry(*args, **kwargs):
            return "ok"

        core.rpc_call = fake_retry  # instance shadow over the bound method

        tasks = [asyncio.create_task(_trigger_refresh(core)) for _ in range(3)]

        await asyncio.wait_for(callback_entered.wait(), EVENT_TIMEOUT_S)
        assert call_count == 1, f"FIRST entry should have call_count=1, got {call_count}"

        # Give tasks 2 and 3 a chance to reach `await refresh_task`. The real
        # single-flight invariant is proven by the post-release assertion below;
        # this loop just lets the scheduler tick.
        if not await _wait_for_inflight_refresh_task(core):
            pytest.fail("Refresh task did not appear in 20 ticks")

        assert call_count == 1, f"Multiple refreshes fired before release: {call_count}"

        release_refresh.set()
        results = await asyncio.gather(*tasks)

        assert all(r == "ok" for r in results)
        assert call_count == 1, f"Post-release call_count drifted to {call_count}"
        assert core.auth.csrf_token == "CSRF_REFRESHED"


@pytest.mark.asyncio
async def test_refresh_failure_propagates_to_all_waiters():
    """All waiters on the shared refresh task observe the same failure.

    Uses a gated failing callback so all three triggers must join the in-flight
    task before it raises — without the gate, the first task could complete
    immediately and let the others spin up their own failed tasks, which would
    pass the per-task assertions but not prove shared-task propagation.
    """
    boom = RuntimeError("refresh boom")
    enter = asyncio.Event()
    release = asyncio.Event()
    call_count = 0

    async def cb():
        nonlocal call_count
        call_count += 1
        enter.set()
        await release.wait()
        raise boom

    async with make_core(refresh_callback=cb) as core:
        tasks = [asyncio.create_task(_trigger_refresh(core)) for _ in range(3)]

        await asyncio.wait_for(enter.wait(), EVENT_TIMEOUT_S)
        if not await _wait_for_inflight_refresh_task(core):
            pytest.fail("Refresh task did not appear in 20 ticks")

        assert call_count == 1, (
            f"Failure propagation test invalid: {call_count} callbacks fired "
            "before release. Single-flight broken — each waiter spun its own."
        )

        release.set()
        results = await asyncio.gather(*tasks, return_exceptions=True)

        assert call_count == 1, f"Refresh re-fired after failure: {call_count}"
        # Identity check: every waiter must observe the SAME RuntimeError as
        # __cause__. This proves shared-task propagation — a per-waiter retry
        # would produce distinct RuntimeError instances even with the same msg.
        for r in results:
            assert isinstance(r, AuthError)
            assert r.__cause__ is boom, (
                f"Expected shared-task propagation (cause is boom), got "
                f"{r.__cause__!r} (id={id(r.__cause__)}, boom id={id(boom)})"
            )


@pytest.mark.asyncio
async def test_second_wave_creates_distinct_refresh_task():
    call_count = 0

    async def cb():
        nonlocal call_count
        call_count += 1
        return AuthTokens(
            csrf_token=f"R{call_count}",
            session_id="S",
            cookies={"SID": f"sid{call_count}"},
        )

    async with make_core(refresh_callback=cb) as core:

        async def fake_retry(*args, **kwargs):
            return "ok"

        core.rpc_call = fake_retry

        await _trigger_refresh(core)
        first_task = core._refresh_task
        assert first_task is not None and first_task.done()

        await _trigger_refresh(core)
        second_task = core._refresh_task
        assert second_task is not None and second_task.done()

        assert first_task is not second_task, "Second wave reused completed task"
        assert call_count == 2
