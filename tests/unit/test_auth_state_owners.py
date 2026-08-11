"""Direct state-owner, race, retention, and compatibility behavior oracles."""

from __future__ import annotations

import asyncio
import gc
import http.cookiejar
import pickle
import threading
import time
import weakref
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

import notebooklm.auth as auth_facade
from notebooklm._auth import keepalive, master_token, master_token_types, recovery, single_flight
from notebooklm._auth.cookie_types import CookieJar
from notebooklm._auth.cookies import _clone_cookie_jar, _LoadedCookiePair
from notebooklm._auth.extraction import _LoginRedirectError
from notebooklm._auth.storage import MasterTokenError as StorageMasterTokenError


def _settle_contended_lock_in_fresh_loop(
    get_lock: Callable[[], asyncio.Lock],
) -> tuple[weakref.ReferenceType[asyncio.AbstractEventLoop], weakref.ReferenceType[asyncio.Lock]]:
    refs: list[
        tuple[weakref.ReferenceType[asyncio.AbstractEventLoop], weakref.ReferenceType[asyncio.Lock]]
    ] = []
    errors: list[BaseException] = []

    def run() -> None:
        async def exercise() -> None:
            loop = asyncio.get_running_loop()
            lock = get_lock()
            await lock.acquire()
            waiter_started = asyncio.Event()

            async def wait_then_release() -> None:
                waiter_started.set()
                await lock.acquire()
                lock.release()

            waiter = asyncio.create_task(wait_then_release())
            await waiter_started.wait()
            await asyncio.sleep(0)
            assert lock._loop is loop
            refs.append((weakref.ref(loop), weakref.ref(lock)))
            lock.release()
            await waiter

        try:
            asyncio.run(exercise())
        except BaseException as exc:  # pragma: no cover - surfaced in the parent thread
            errors.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert not errors
    assert len(refs) == 1
    return refs[0]


@pytest.mark.asyncio
async def test_single_flight_identity_epoch_stale_skip_and_quiescent_reset() -> None:
    owner = single_flight.SingleFlight()
    release = asyncio.Event()
    calls = 0

    async def work() -> object:
        nonlocal calls
        calls += 1
        await release.wait()
        return marker

    marker = object()
    leader, flight = owner.claim(("path", "policy"), work)
    follower, same = owner.claim(("path", "policy"), work)
    assert leader is True and follower is False and same is flight
    assert flight.task in owner._leader_tasks
    with pytest.raises(RuntimeError, match="cannot reset SingleFlight with live work"):
        owner._reset_for_tests()

    release.set()
    assert await owner.await_flight(flight) is marker
    await asyncio.sleep(0)
    assert calls == 1
    assert owner._flights == {} and owner._leader_tasks == set()

    owner.note_success("path")
    created = False

    def stale_factory() -> Any:
        nonlocal created
        created = True
        return work()

    assert (
        owner.claim_if_epoch_current(
            ("path", "policy"),
            stale_factory,
            path_key="path",
            epoch_before=0,
        )
        is None
    )
    assert created is False and owner.read_success_epoch("path") == 1
    owner._reset_for_tests()
    assert owner.read_success_epoch("path") == 0


def test_single_flight_isolated_owner_coalesces_across_event_loops() -> None:
    owner = single_flight.SingleFlight()
    started = threading.Event()
    follower_claimed = threading.Event()
    release = threading.Event()
    calls = 0
    outputs: list[object] = []
    errors: list[BaseException] = []

    async def drive() -> None:
        nonlocal calls

        async def work() -> object:
            nonlocal calls
            calls += 1
            started.set()
            await asyncio.to_thread(release.wait)
            return owner

        is_leader, flight = owner.claim(("path", "cross-loop"), work)
        if not is_leader:
            follower_claimed.set()
        outputs.append(await owner.await_flight(flight))

    def run() -> None:
        try:
            asyncio.run(drive())
        except BaseException as exc:  # pragma: no cover - diagnostic collection
            errors.append(exc)

    first = threading.Thread(target=run)
    first.start()
    assert started.wait(timeout=2)
    second = threading.Thread(target=run)
    second.start()
    assert follower_claimed.wait(timeout=2)
    release.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert not first.is_alive() and not second.is_alive()
    assert not errors
    assert calls == 1 and outputs == [owner, owner]
    assert owner._flights == {} and owner._leader_tasks == set()


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["success", "error", "cancel"])
async def test_single_flight_mirrors_exact_terminal_and_retains_no_settled_slot(
    terminal: str,
) -> None:
    owner = single_flight.SingleFlight()
    error = LookupError("worker failed")

    async def work() -> str:
        if terminal == "error":
            raise error
        if terminal == "cancel":
            raise asyncio.CancelledError
        return "ok"

    _, flight = owner.claim(("path", terminal), work)
    if terminal == "success":
        assert await owner.await_flight(flight) == "ok"
    elif terminal == "error":
        with pytest.raises(LookupError) as raised:
            await owner.await_flight(flight)
        assert raised.value is error
    else:
        with pytest.raises(asyncio.CancelledError):
            await owner.await_flight(flight)
    await asyncio.sleep(0)
    assert owner._flights == {} and owner._leader_tasks == set()


@pytest.mark.asyncio
async def test_waiter_repeated_cancellation_settles_worker_without_cancelling_it() -> None:
    owner = single_flight.SingleFlight()
    started = asyncio.Event()
    release = asyncio.Event()
    settled = False

    async def work() -> str:
        nonlocal settled
        started.set()
        await release.wait()
        settled = True
        return "done"

    _, flight = owner.claim(("path", "cancel-waiter"), work)
    waiter = asyncio.create_task(owner.await_flight(flight))
    await started.wait()
    waiter.cancel()
    waiter.cancel()
    await asyncio.sleep(0)
    assert flight.task is not None and not flight.task.cancelled()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    await asyncio.sleep(0)
    assert settled is True
    assert owner._flights == {} and owner._leader_tasks == set()


@pytest.mark.asyncio
async def test_terminal_callback_order_and_replacement_slot_preservation() -> None:
    events: list[str] = []

    class Observed(single_flight.SingleFlight):
        def _mirror(self, task: asyncio.Task[Any], flight: single_flight.Flight[Any]) -> None:
            assert task not in self._leader_tasks
            assert self._flights[("path", "barrier")] is flight
            events.append("mirror")
            super()._mirror(task, flight)

        def _pop(
            self,
            flight_key: single_flight.FlightKey,
            flight: single_flight.Flight[Any],
        ) -> None:
            assert flight.bridge.done()
            events.append("pop")
            super()._pop(flight_key, flight)

    owner = Observed()

    async def work() -> str:
        return "ok"

    _, flight = owner.claim(("path", "barrier"), work)
    assert await owner.await_flight(flight) == "ok"
    await asyncio.sleep(0)
    assert events == ["mirror", "pop"]

    replacement: single_flight.Flight[str] = single_flight.Flight()
    replacement.bridge.set_result("replacement")
    with owner._lock:
        owner._flights[("path", "replacement")] = replacement
    owner._pop(("path", "replacement"), flight)
    assert owner._flights[("path", "replacement")] is replacement


def test_process_single_flight_adapters_are_late_identity_views() -> None:
    owner = single_flight.SingleFlight.process_default()
    assert owner is single_flight.SingleFlight.process_default()
    owner._reset_for_tests()
    single_flight.note_success("adapter-path")
    assert owner.read_success_epoch("adapter-path") == 1
    single_flight._reset_for_tests()


def test_cold_state_exact_loop_path_identity_weak_cleanup_and_locked_reset() -> None:
    state = recovery.ColdRecoveryState()
    path = Path("relative.json")
    loop = asyncio.new_event_loop()

    async def exercise() -> None:
        lock = state.path_lock(path)
        assert lock is state.path_lock(path)
        assert state.path_lock(Path("nested/../relative.json")) is not lock
        assert state.success_generation(path) == 0
        state.note_success(path)
        assert state.success_generation(path) == 1
        await lock.acquire()
        with pytest.raises(RuntimeError, match="locked paths"):
            state._reset_for_tests()
        lock.release()

    loop.run_until_complete(exercise())
    assert state._locks_by_loop and state._success_generations
    loop_ref = weakref.ref(loop)
    loop.close()
    del loop
    gc.collect()
    assert loop_ref() is None
    assert not state._locks_by_loop and not state._success_generations
    state._reset_for_tests()


def test_cold_state_contended_lock_does_not_retain_closed_loop() -> None:
    state = recovery.ColdRecoveryState()
    path = Path("/tmp/contended-cold-profile.json")

    def get_lock() -> asyncio.Lock:
        state.note_success(path)
        return state.path_lock(path)

    loop_ref, lock_ref = _settle_contended_lock_in_fresh_loop(get_lock)
    for _ in range(5):
        gc.collect()

    assert loop_ref() is None
    assert lock_ref() is None
    assert not state._locks_by_loop
    assert not state._success_generations


def test_cold_state_synchronizes_distinct_thread_loops() -> None:
    state = recovery.ColdRecoveryState()
    path = Path("/tmp/profile.json")
    loops: list[asyncio.AbstractEventLoop] = []
    locks: list[asyncio.Lock] = []
    barrier = threading.Barrier(2)

    def run() -> None:
        loop = asyncio.new_event_loop()
        loops.append(loop)

        async def exercise() -> None:
            barrier.wait()
            locks.append(state.path_lock(path))
            state.note_success(path)
            assert state.success_generation(path) == 1

        loop.run_until_complete(exercise())

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)
    assert len(locks) == 2 and locks[0] is not locks[1]
    assert len(state._locks_by_loop) == 2 and len(state._success_generations) == 2
    for loop in loops:
        loop.close()


@pytest.mark.asyncio
async def test_cold_driver_spells_l3_then_l4_and_tracks_winning_sample() -> None:
    state = recovery.ColdRecoveryState()
    path = Path("/tmp/profile.json")
    events: list[tuple[Any, ...]] = []
    initial = _LoadedCookiePair(httpx.Cookies({"SID": "initial"}), CookieJar())
    winner = _LoadedCookiePair(httpx.Cookies({"SID": "master"}), CookieJar())

    async def headless(got: Path | None, allowed: bool) -> None:
        events.append(("headless", got, allowed))
        return None

    async def master(got: Path | None) -> _LoadedCookiePair:
        events.append(("master", got))
        return winner

    async def validate(jar: httpx.Cookies) -> None:
        events.append(("validate", jar.get("SID")))

    result = await recovery.ColdRecoveryCoordinator._drive_cold(
        state=state,
        storage_path=path,
        allow_headless=True,
        load_cookie_pair=lambda got: initial,
        run_headless_attempt=headless,
        run_master_token_attempt=master,
        validate_recovered=validate,
        snapshot_cookie_jar=lambda jar: {"cookies": [{"value": jar.get("SID")}]},
        initial_error=_LoginRedirectError("expired"),
    )

    assert events == [("headless", path, True), ("master", path), ("validate", "master")]
    assert result.cookie_jar is winner.live
    assert result.baseline == winner.baseline
    assert result.snapshot == {"cookies": [{"value": "master"}]}
    assert state.success_generation(path) == 1


@pytest.mark.asyncio
async def test_coalesced_cold_returns_caller_copies_and_transports_expected_exhaustion() -> None:
    state = recovery.ColdRecoveryState()
    flights = single_flight.SingleFlight()
    path = Path("/tmp/profile.json")
    pair = _LoadedCookiePair(httpx.Cookies({"SID": "fresh"}), CookieJar())
    calls = 0

    async def headless(_path: Path | None, _allowed: bool) -> _LoadedCookiePair:
        nonlocal calls
        calls += 1
        return pair

    async def master(_path: Path | None) -> None:
        return None

    async def validate(_jar: httpx.Cookies) -> None:
        return None

    kwargs = {
        "state": state,
        "single_flight": flights,
        "storage_path": path,
        "allow_headless": True,
        "load_cookie_pair": lambda got: pair,
        "run_headless_attempt": headless,
        "run_master_token_attempt": master,
        "validate_recovered": validate,
        "snapshot_cookie_jar": lambda jar: {"cookies": []},
        "clone_cookie_jar": _clone_cookie_jar,
        "raise_on_exhaustion": False,
        "initial_error": _LoginRedirectError("expired"),
    }
    first, second = await asyncio.gather(
        recovery.ColdRecoveryCoordinator._coalesce_cold(**kwargs),
        recovery.ColdRecoveryCoordinator._coalesce_cold(**kwargs),
    )
    assert first is not None and second is not None
    assert calls == 1
    assert first.cookie_jar is not second.cookie_jar
    assert first.snapshot is not second.snapshot

    exhausted = _LoginRedirectError("exhausted")

    async def fail_headless(_path: Path | None, _allowed: bool) -> None:
        raise exhausted

    kwargs["run_headless_attempt"] = fail_headless
    kwargs["run_master_token_attempt"] = master
    kwargs["initial_error"] = exhausted
    kwargs["state"] = recovery.ColdRecoveryState()
    kwargs["single_flight"] = single_flight.SingleFlight()
    assert await recovery.ColdRecoveryCoordinator._coalesce_cold(**kwargs) is None
    kwargs["raise_on_exhaustion"] = True
    with pytest.raises(_LoginRedirectError) as raised:
        await recovery.ColdRecoveryCoordinator._coalesce_cold(**kwargs)
    assert raised.value is exhausted


def test_rotation_state_boundaries_aliases_isolation_and_atomic_threads() -> None:
    owner = keepalive.RotationState()
    path = Path("/tmp/profile.json")
    canonical_path = keepalive.canonical_storage_key(path)
    assert canonical_path is not None
    now = time.monotonic()
    owner._last_attempt_monotonic[canonical_path] = now - 59.0
    assert owner.try_claim(path) is False
    owner._last_attempt_monotonic[canonical_path] = time.monotonic() - 60.0
    assert owner.try_claim(path) is True
    assert owner.try_claim(None) is True
    assert owner.try_claim(None) is False

    process = keepalive.RotationState.process_default()
    assert keepalive._POKE_STATE_LOCK is process._lock
    assert keepalive._POKE_LOCKS_BY_LOOP is process._locks_by_loop
    assert keepalive._LAST_POKE_ATTEMPT_MONOTONIC is process._last_attempt_monotonic
    assert owner._last_attempt_monotonic is not process._last_attempt_monotonic

    contender = keepalive.RotationState()
    barrier = threading.Barrier(8)
    results: list[bool] = []

    def claim() -> None:
        barrier.wait()
        results.append(contender.try_claim(path))

    threads = [threading.Thread(target=claim) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)
    assert results.count(True) == 1 and results.count(False) == 7


@pytest.mark.asyncio
async def test_rotation_state_loop_lock_and_safe_reset() -> None:
    state = keepalive.RotationState()
    path = Path("/tmp/profile.json")
    lock = state.poke_lock(path)
    assert lock is state.poke_lock(path)
    await lock.acquire()
    with pytest.raises(RuntimeError, match="while a poke lock is held"):
        state._reset_for_tests()
    lock.release()
    state._reset_for_tests()
    assert not state._locks_by_loop and not state._last_attempt_monotonic


def test_rotation_state_contended_lock_does_not_retain_closed_loop() -> None:
    state = keepalive.RotationState()
    path = Path("/tmp/contended-rotation-profile.json")
    loop_ref, lock_ref = _settle_contended_lock_in_fresh_loop(lambda: state.poke_lock(path))
    for _ in range(5):
        gc.collect()

    assert loop_ref() is None
    assert lock_ref() is None
    assert not state._locks_by_loop


def test_master_token_error_identity_pickle_global_and_cause_are_historical() -> None:
    cls = master_token_types.MasterTokenError
    assert cls is master_token.MasterTokenError is StorageMasterTokenError
    assert cls is auth_facade.MasterTokenError
    assert cls.__module__ == "notebooklm._auth.master_token"
    value = cls("legacy message")
    assert type(pickle.loads(pickle.dumps(value))) is cls
    legacy_global = b"cnotebooklm._auth.master_token\nMasterTokenError\n(Vlegacy message\ntR."
    restored = pickle.loads(legacy_global)
    assert type(restored) is cls and str(restored) == "legacy message"
    cause = ValueError("cause")
    try:
        raise value from cause
    except cls as raised:
        assert raised is value
        assert raised.__cause__ is cause
        assert raised.__traceback__ is not None


def test_cookie_jar_merge_projection_include_none_is_explicit() -> None:
    live = httpx.Cookies()
    live.jar.set_cookie(
        http.cookiejar.Cookie(
            0,
            "SID",
            None,
            None,
            False,
            ".google.com",
            True,
            True,
            "/",
            True,
            False,
            None,
            True,
            None,
            None,
            {},
        )
    )
    assert len(CookieJar.from_live_httpx_for_merge(live, include_none=False)) == 0
    included = CookieJar.from_live_httpx_for_merge(live, include_none=True)
    assert len(included) == 1 and next(iter(included)).value is None
