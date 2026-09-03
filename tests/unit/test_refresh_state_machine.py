"""Refresh state-machine regression tests.

Pins three behaviors of ``RpcExecutor.try_refresh_and_retry`` (the
canonical implementation; ``Session._try_refresh_and_retry`` was
inlined in PR #4b and callers now reach the executor through
``core._web_runtime.executor``):

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

from notebooklm._web.transport.auth_refresh_retry import RefreshBudget
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
    """Drive ``RpcExecutor.try_refresh_and_retry`` with throwaway args."""
    resource_epoch = _assert_open_generation(core)
    return await core._web_runtime.executor.try_refresh_and_retry(
        RPCMethod.LIST_NOTEBOOKS,
        [],
        "/",
        False,
        AuthError("simulated"),
        _refresh_budget=RefreshBudget(),
        _resource_epoch=resource_epoch,
    )


def _assert_open_generation(core) -> int:
    """Return the lifecycle-created epoch after proving every owner agrees."""
    collaborators = core._collaborators
    web = core._web_runtime
    lifecycle = collaborators.lifecycle
    generation = collaborators.call_supervisor._current

    assert lifecycle.is_open()
    assert generation is not None
    epoch = lifecycle._epoch
    assert epoch > 0
    assert generation.epoch == epoch
    assert web.web_transport._active_epoch == epoch
    assert web.kernel._active_epoch == epoch
    assert web.auth_coord._active_epoch == epoch
    return epoch


async def _wait_for_inflight_refresh_task(core, ticks: int = 20) -> bool:
    """Yield up to ``ticks`` times for the shared refresh task to appear."""
    for _ in range(ticks):
        await asyncio.sleep(0)
        if (
            core._web_runtime.auth_coord._refresh_task is not None
            and not core._web_runtime.auth_coord._refresh_task.done()
        ):
            return True
    return False


@pytest.mark.asyncio
async def test_concurrent_callers_share_single_refresh():
    callback_entered = asyncio.Event()
    release_refresh = asyncio.Event()
    call_count = 0
    core_box: list = []

    callback_epochs: list[int] = []

    async def cb(expected_epoch: int):
        nonlocal call_count
        callback_epochs.append(expected_epoch)
        assert expected_epoch == _assert_open_generation(core_box[0])
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

        core._web_runtime.executor.rpc_call = fake_retry  # type: ignore[method-assign]

        tasks = [asyncio.create_task(_trigger_refresh(core)) for _ in range(3)]

        await asyncio.wait_for(callback_entered.wait(), EVENT_TIMEOUT_S)
        assert call_count == 1, f"FIRST entry should have call_count=1, got {call_count}"

        # Give tasks 2 and 3 a chance to reach `await refresh_task`. The real
        # single-flight invariant is proven by the post-release assertion below;
        # this loop just lets the scheduler tick.
        if not await _wait_for_inflight_refresh_task(core):
            pytest.fail("Refresh task did not appear in 20 ticks")
        refresh_task = core._web_runtime.auth_coord._refresh_task
        assert refresh_task is not None and not refresh_task.done()

        assert call_count == 1, f"Multiple refreshes fired before release: {call_count}"

        release_refresh.set()
        results = await asyncio.gather(*tasks)

        assert all(r == "ok" for r in results)
        assert call_count == 1, f"Post-release call_count drifted to {call_count}"
        assert callback_epochs == [_assert_open_generation(core)]
        assert refresh_task.done()
        refresh_result = refresh_task.result()
        assert refresh_result.error is None
        assert refresh_result.value is not None
        assert refresh_result.value.csrf_token == "CSRF_REFRESHED"
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
    callback_epochs: list[int] = []
    core_box: list = []

    async def cb(expected_epoch: int):
        nonlocal call_count
        callback_epochs.append(expected_epoch)
        assert expected_epoch == _assert_open_generation(core_box[0])
        call_count += 1
        enter.set()
        await release.wait()
        raise boom

    async with make_core(refresh_callback=cb) as core:
        core_box.append(core)
        tasks = [asyncio.create_task(_trigger_refresh(core)) for _ in range(3)]

        await asyncio.wait_for(enter.wait(), EVENT_TIMEOUT_S)
        if not await _wait_for_inflight_refresh_task(core):
            pytest.fail("Refresh task did not appear in 20 ticks")
        refresh_task = core._web_runtime.auth_coord._refresh_task
        assert refresh_task is not None and not refresh_task.done()

        assert call_count == 1, (
            f"Failure propagation test invalid: {call_count} callbacks fired "
            "before release. Single-flight broken — each waiter spun its own."
        )

        release.set()
        results = await asyncio.gather(*tasks, return_exceptions=True)

        assert call_count == 1, f"Refresh re-fired after failure: {call_count}"
        assert callback_epochs == [_assert_open_generation(core)]
        assert refresh_task.done()
        assert refresh_task.result().error is boom
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
    callback_epochs: list[int] = []
    expected_epoch: int | None = None

    async def cb(resource_epoch: int):
        nonlocal call_count
        callback_epochs.append(resource_epoch)
        assert resource_epoch == expected_epoch
        call_count += 1
        return AuthTokens(
            csrf_token=f"R{call_count}",
            session_id="S",
            cookies={"SID": f"sid{call_count}"},
        )

    async with make_core(refresh_callback=cb) as core:
        expected_epoch = _assert_open_generation(core)

        async def fake_retry(*args, **kwargs):
            return "ok"

        core._web_runtime.executor.rpc_call = fake_retry  # type: ignore[method-assign]

        await _trigger_refresh(core)
        first_task = core._web_runtime.auth_coord._refresh_task
        assert first_task is not None and first_task.done()
        assert first_task.result().error is None
        assert first_task.result().value is not None

        await _trigger_refresh(core)
        second_task = core._web_runtime.auth_coord._refresh_task
        assert second_task is not None and second_task.done()
        assert second_task.result().error is None
        assert second_task.result().value is not None

        assert first_task is not second_task, "Second wave reused completed task"
        assert call_count == 2
        assert callback_epochs == [expected_epoch, expected_epoch]
