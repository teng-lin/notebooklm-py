"""refresh-cmd success-epoch race + cancel safety (single-flight core).

c-PR2 relocated the refresh-cmd coalescing into
``notebooklm._auth.single_flight``. This suite is the case-for-case migration of
the deleted white-box suite: every pinned GUARANTEE is re-expressed against the
new surface — the ``_REFRESH_GENERATIONS`` counter becomes the process-global
per-path ``success_epoch`` (``single_flight.read_success_epoch``), and the
cancellation / failure-propagation scenarios are preserved:

1. **Failed refresh must not skip a concurrent waiter** — a subprocess failure
   never bumps the success epoch, so a waiter still sees a real refresh attempt
   (``failure leaves waiters retrying``).
2. **A subsequent caller after a failure sees a fresh attempt** — the follower
   whose leader failed re-attempts as a new leader.
3. **Caller cancellation must not abort the in-flight subprocess** — the shared
   subprocess is settled before cancellation propagates; a duplicate never
   spawns; exactly-once coalescing holds regardless of cancellation timing.
4. **Cancel/settle race must not bump on failure** — a caller cancelled as the
   subprocess settles with failure leaves the epoch untouched.
5. **Cancel-before-work must not phantom-bump** — only the leader body bumps,
   and only after real subprocess success (issue #816; warm-registry variant).
"""

from __future__ import annotations

import asyncio
import threading

import httpx
import pytest

from notebooklm import auth as auth_mod
from notebooklm._auth import refresh as _auth_refresh
from notebooklm._auth import single_flight as _single_flight

# Mock-only tests (no real HTTP, no cassette) — opt out of the
# integration-tree enforcement hook in ``tests/integration/conftest.py``.
pytestmark = pytest.mark.allow_no_vcr


@pytest.fixture(autouse=True)
def _clear_single_flight_state():
    """Reset the single-flight core so each test starts at epoch 0."""
    _single_flight._reset_for_tests()
    yield
    _single_flight._reset_for_tests()


def _epoch(storage) -> int:
    return _single_flight.read_success_epoch(str(storage.expanduser().resolve()))


@pytest.mark.asyncio
async def test_failed_refresh_does_not_skip_concurrent_waiter(monkeypatch, tmp_path):
    """Concurrent callers — the refresh subprocess fails; neither skips silently.

    The old bug bumped the generation BEFORE the subprocess ran, so a concurrent
    waiter observed the phantom bump and short-circuited onto stale storage. In
    the single-flight core the success epoch is bumped ONLY after the subprocess
    exits zero; a failure leaves it at 0, so a follower whose leader failed
    re-attempts (rather than skipping) and every caller surfaces the failure.
    """
    storage = tmp_path / "storage_state.json"
    storage.write_text('{"cookies": [], "origins": []}')
    monkeypatch.setenv(auth_mod.NOTEBOOKLM_REFRESH_CMD_ENV, "dummy")

    refresh_calls = 0
    refresh_call_lock = threading.Lock()

    async def fake_run_refresh_cmd(storage_path, profile):
        nonlocal refresh_calls
        with refresh_call_lock:
            refresh_calls += 1
            call_n = refresh_calls
        # All attempts fail in this scenario.
        raise RuntimeError(f"NOTEBOOKLM_REFRESH_CMD exited 2: synthetic failure {call_n}")

    async def fake_fetch_tokens_with_jar(cookie_jar, storage_path, **kwargs):
        if not getattr(cookie_jar, "_first_fetch_done", False):
            cookie_jar._first_fetch_done = True
            raise ValueError("Authentication expired. Run 'notebooklm login'.")
        return "csrf-token", "session-id"

    def fake_build(_p):
        return httpx.Cookies()

    def fake_snapshot(_j):
        return None

    # The subprocess runner is INJECTED via ``RefreshCmdDeps`` rather than
    # monkeypatched onto the module (plan §7 deps record).
    deps = _auth_refresh.RefreshCmdDeps(run_refresh_cmd=fake_run_refresh_cmd)
    monkeypatch.setattr(_auth_refresh, "_fetch_tokens_with_jar", fake_fetch_tokens_with_jar)
    monkeypatch.setattr(_auth_refresh, "build_httpx_cookies_from_storage", fake_build)
    monkeypatch.setattr(_auth_refresh, "snapshot_cookie_jar", fake_snapshot)

    async def caller(jar):
        return await auth_mod._fetch_tokens_with_refresh(jar, storage_path=storage, deps=deps)

    jar_a = httpx.Cookies()
    jar_b = httpx.Cookies()
    results = await asyncio.gather(caller(jar_a), caller(jar_b), return_exceptions=True)

    # Both must surface the subprocess failure — neither may silently succeed via
    # reloaded-stale-cookies on a phantom epoch bump.
    for idx, r in enumerate(results):
        assert isinstance(r, RuntimeError), (
            f"Caller {idx} expected RuntimeError, got {r!r}. A silent success "
            "means it short-circuited on a phantom success-epoch bump."
        )

    # The subprocess must have been attempted; the load-bearing assertion is that
    # NEITHER caller silently skipped (checked above).
    assert refresh_calls >= 1
    # Regression assertion: the epoch must NOT advance after a failed refresh.
    assert _epoch(storage) == 0, (
        f"Success epoch must not advance when refresh-cmd fails; saw {_epoch(storage)}."
    )


@pytest.mark.asyncio
async def test_sibling_success_between_epoch_read_and_claim_skips_subprocess(monkeypatch, tmp_path):
    """Compare-under-exclusion: no redundant subprocess #2 on the late-waiter race.

    Models the interleaving the REVISION targets: a cross-loop sibling finishes
    its subprocess → ``note_success`` (epoch++) → prompt-pops its flight BETWEEN
    this caller's ``epoch_before`` capture and its claim. Because
    ``claim_if_epoch_current`` compares the epoch and claims under a SINGLE lock
    hold, this caller observes the advanced epoch and skips — it does NOT become a
    new leader and run a redundant second subprocess. Combined with the sibling's
    one run, exactly ONE subprocess executes for the storage path.

    Determinism: patch ``read_success_epoch`` (used only for the ``epoch_before``
    capture) to return the STALE pre-sibling value while the real
    ``_SUCCESS_EPOCHS`` is already bumped — exactly the state the race produces at
    the claim point.
    """
    storage = tmp_path / "storage_state.json"
    storage.write_text('{"cookies": [], "origins": []}')
    monkeypatch.setenv(auth_mod.NOTEBOOKLM_REFRESH_CMD_ENV, "dummy")
    path_key = str(storage.expanduser().resolve())

    this_caller_subprocess_calls = 0

    async def fake_run_refresh_cmd(storage_path, profile):
        nonlocal this_caller_subprocess_calls
        this_caller_subprocess_calls += 1

    # The subprocess runner is INJECTED via ``RefreshCmdDeps`` rather than
    # monkeypatched onto the module (plan §7 deps record).
    deps = _auth_refresh.RefreshCmdDeps(run_refresh_cmd=fake_run_refresh_cmd)

    # The sibling already ran its subprocess and bumped the real epoch to 1.
    _single_flight.note_success(path_key)  # models the sibling's one subprocess

    # This caller captured its epoch_before BEFORE the sibling bumped: force the
    # stale read so ``epoch_before == 0`` at the compare point.
    monkeypatch.setattr(_single_flight, "read_success_epoch", lambda _pk: 0)

    await _auth_refresh._coalesced_run_refresh_cmd(
        path_key, storage.expanduser().resolve(), None, deps=deps
    )

    # This caller must have SKIPPED under exclusion — no redundant subprocess #2.
    assert this_caller_subprocess_calls == 0, (
        "compare-under-exclusion failed: this caller ran a redundant subprocess "
        "even though a sibling had already succeeded and bumped the epoch."
    )


@pytest.mark.asyncio
async def test_concurrent_refresh_failure_followup_sees_attempt(monkeypatch, tmp_path):
    """First refresh fails; a caller waiting behind it MUST run its own attempt.

    Caller A leads, its subprocess fails. Caller B, which arrived while A's
    subprocess was in flight and coalesced onto A's flight, observes the failure
    and — because the epoch never bumped — re-attempts as a fresh leader. B's own
    subprocess then succeeds. B must NOT skip on a phantom bump.
    """
    storage = tmp_path / "storage_state.json"
    storage.write_text('{"cookies": [], "origins": []}')
    monkeypatch.setenv(auth_mod.NOTEBOOKLM_REFRESH_CMD_ENV, "dummy")

    refresh_calls = 0
    refresh_call_lock = threading.Lock()
    enter_subprocess_event = asyncio.Event()
    leader_can_proceed_event = asyncio.Event()

    async def fake_run_refresh_cmd(storage_path, profile):
        nonlocal refresh_calls
        with refresh_call_lock:
            refresh_calls += 1
            call_n = refresh_calls
        if call_n == 1:
            enter_subprocess_event.set()
            await leader_can_proceed_event.wait()
            raise RuntimeError("NOTEBOOKLM_REFRESH_CMD exited 2: synthetic failure")
        # Subsequent calls succeed (storage refreshed).

    async def fake_fetch_tokens_with_jar(cookie_jar, storage_path, **kwargs):
        if not getattr(cookie_jar, "_first_fetch_done", False):
            cookie_jar._first_fetch_done = True
            raise ValueError("Authentication expired. Run 'notebooklm login'.")
        return "csrf-token", "session-id"

    def fake_build(_p):
        return httpx.Cookies()

    def fake_snapshot(_j):
        return None

    # The subprocess runner is INJECTED via ``RefreshCmdDeps`` rather than
    # monkeypatched onto the module (plan §7 deps record).
    deps = _auth_refresh.RefreshCmdDeps(run_refresh_cmd=fake_run_refresh_cmd)
    monkeypatch.setattr(_auth_refresh, "_fetch_tokens_with_jar", fake_fetch_tokens_with_jar)
    monkeypatch.setattr(_auth_refresh, "build_httpx_cookies_from_storage", fake_build)
    monkeypatch.setattr(_auth_refresh, "snapshot_cookie_jar", fake_snapshot)

    async def caller(jar):
        return await auth_mod._fetch_tokens_with_refresh(jar, storage_path=storage, deps=deps)

    jar_a = httpx.Cookies()
    jar_b = httpx.Cookies()

    task_a = asyncio.create_task(caller(jar_a))
    await enter_subprocess_event.wait()
    task_b = asyncio.create_task(caller(jar_b))
    # Yield so B enters the refresh path and coalesces onto A's in-flight flight.
    await asyncio.sleep(0.05)

    # Release the leader; A's subprocess fails.
    leader_can_proceed_event.set()

    results = await asyncio.gather(task_a, task_b, return_exceptions=True)
    assert isinstance(results[0], RuntimeError), f"A expected RuntimeError, got {results[0]!r}"
    assert "synthetic failure" in str(results[0])

    # B must have SEEN A REFRESH ATTEMPT: A's failure left the epoch at 0, so B
    # (which followed A's failing flight) re-ran its own subprocess (call #2),
    # which succeeded.
    assert refresh_calls == 2, (
        f"Expected 2 subprocess calls (A failed, B retried), saw {refresh_calls}. "
        "Caller B short-circuited on the failed leader's phantom epoch bump."
    )
    assert not isinstance(results[1], BaseException), f"B raised: {results[1]!r}"
    _csrf, _sid, refreshed, _snap = results[1]
    assert refreshed is True
    # B's subprocess succeeded → epoch bumped exactly once.
    assert _epoch(storage) == 1


@pytest.mark.asyncio
async def test_waiter_cancellation_does_not_kill_inflight_subprocess(monkeypatch, tmp_path):
    """Cancellation of the leader-caller must not abort the shared subprocess.

    Caller A leads and starts the subprocess. Caller B coalesces onto the shared
    flight. A is cancelled; the subprocess MUST keep running (settle before
    propagate) for B's benefit. After it completes, B observes a successful
    refresh, the subprocess ran exactly ONCE, and no duplicate ever overlapped.
    """
    storage = tmp_path / "storage_state.json"
    storage.write_text('{"cookies": [], "origins": []}')
    monkeypatch.setenv(auth_mod.NOTEBOOKLM_REFRESH_CMD_ENV, "dummy")

    subprocess_invocations = 0
    subprocess_completions = 0
    concurrent_invocations_observed = 0
    in_flight_count = 0
    max_in_flight = 0
    invocation_lock = threading.Lock()
    leader_entered = asyncio.Event()
    leader_can_proceed = asyncio.Event()

    async def fake_run_refresh_cmd(storage_path, profile):
        nonlocal subprocess_invocations, subprocess_completions
        nonlocal in_flight_count, max_in_flight, concurrent_invocations_observed
        with invocation_lock:
            subprocess_invocations += 1
            in_flight_count += 1
            if in_flight_count > max_in_flight:
                max_in_flight = in_flight_count
            if in_flight_count > 1:
                concurrent_invocations_observed += 1
            is_first = subprocess_invocations == 1
        try:
            if is_first:
                leader_entered.set()
                await leader_can_proceed.wait()
        finally:
            with invocation_lock:
                in_flight_count -= 1
        with invocation_lock:
            subprocess_completions += 1

    async def fake_fetch_tokens_with_jar(cookie_jar, storage_path, **kwargs):
        if not getattr(cookie_jar, "_first_fetch_done", False):
            cookie_jar._first_fetch_done = True
            raise ValueError("Authentication expired. Run 'notebooklm login'.")
        return "csrf-token", "session-id"

    def fake_build(_p):
        return httpx.Cookies()

    def fake_snapshot(_j):
        return None

    # The subprocess runner is INJECTED via ``RefreshCmdDeps`` rather than
    # monkeypatched onto the module (plan §7 deps record).
    deps = _auth_refresh.RefreshCmdDeps(run_refresh_cmd=fake_run_refresh_cmd)
    monkeypatch.setattr(_auth_refresh, "_fetch_tokens_with_jar", fake_fetch_tokens_with_jar)
    monkeypatch.setattr(_auth_refresh, "build_httpx_cookies_from_storage", fake_build)
    monkeypatch.setattr(_auth_refresh, "snapshot_cookie_jar", fake_snapshot)

    async def caller(jar):
        return await auth_mod._fetch_tokens_with_refresh(jar, storage_path=storage, deps=deps)

    jar_a = httpx.Cookies()
    jar_b = httpx.Cookies()

    task_a = asyncio.create_task(caller(jar_a))
    await leader_entered.wait()
    task_b = asyncio.create_task(caller(jar_b))
    # Let B enter the refresh path and coalesce onto the in-flight flight.
    await asyncio.sleep(0.05)

    # Cancel A — the shared subprocess MUST keep running for B (settle loop).
    task_a.cancel()
    await asyncio.sleep(0.05)

    assert subprocess_completions == 0, (
        f"After A's cancellation, subprocess_completions={subprocess_completions}; "
        "expected the in-flight subprocess to still be running (settle-before-propagate)."
    )

    leader_can_proceed.set()

    a_result = await asyncio.gather(task_a, return_exceptions=True)
    assert isinstance(a_result[0], asyncio.CancelledError)

    # B observes a SUCCESSFUL refresh — not stale-after-skip, not a propagated
    # cancellation.
    csrf, sid, refreshed, _snap = await task_b
    assert refreshed is True
    assert csrf == "csrf-token"
    assert sid == "session-id"

    assert max_in_flight == 1, (
        f"Expected at most 1 concurrent subprocess invocation, saw {max_in_flight}."
    )
    assert concurrent_invocations_observed == 0
    # Exact-once coalescing across both callers, regardless of cancel timing.
    assert subprocess_invocations == 1, (
        f"Expected exactly 1 subprocess invocation (A+B coalesced), saw {subprocess_invocations}."
    )
    assert subprocess_completions == 1
    # The subprocess completed successfully — epoch bumped exactly once.
    assert _epoch(storage) == 1, "Epoch must bump exactly once on a coalesced success."


@pytest.mark.asyncio
async def test_cancel_settle_race_does_not_bump_on_failure(monkeypatch, tmp_path):
    """Cancel/settle race: caller cancelled AS the subprocess settles with failure.

    The caller is cancelled in the same window the subprocess fails.
    ``await_flight`` settles the shared work before propagating the caller's
    cancellation, and the leader body raises before it can bump — so the epoch
    stays 0 and no phantom bump leaks to sibling waiters.
    """
    storage = tmp_path / "storage_state.json"
    storage.write_text('{"cookies": [], "origins": []}')
    monkeypatch.setenv(auth_mod.NOTEBOOKLM_REFRESH_CMD_ENV, "dummy")

    cancel_now = asyncio.Event()
    settle_after_cancel = asyncio.Event()

    async def fake_run_refresh_cmd(storage_path, profile):
        cancel_now.set()
        await settle_after_cancel.wait()
        raise RuntimeError("NOTEBOOKLM_REFRESH_CMD exited 2: synthetic failure")

    async def fake_fetch_tokens_with_jar(cookie_jar, storage_path, **kwargs):
        if not getattr(cookie_jar, "_first_fetch_done", False):
            cookie_jar._first_fetch_done = True
            raise ValueError("Authentication expired. Run 'notebooklm login'.")
        return "csrf-token", "session-id"

    def fake_build(_p):
        return httpx.Cookies()

    def fake_snapshot(_j):
        return None

    # The subprocess runner is INJECTED via ``RefreshCmdDeps`` rather than
    # monkeypatched onto the module (plan §7 deps record).
    deps = _auth_refresh.RefreshCmdDeps(run_refresh_cmd=fake_run_refresh_cmd)
    monkeypatch.setattr(_auth_refresh, "_fetch_tokens_with_jar", fake_fetch_tokens_with_jar)
    monkeypatch.setattr(_auth_refresh, "build_httpx_cookies_from_storage", fake_build)
    monkeypatch.setattr(_auth_refresh, "snapshot_cookie_jar", fake_snapshot)

    jar = httpx.Cookies()
    task = asyncio.create_task(
        auth_mod._fetch_tokens_with_refresh(jar, storage_path=storage, deps=deps)
    )
    await cancel_now.wait()
    # Cancel the caller and release the subprocess in adjacent ticks so settle
    # and cancellation interleave.
    task.cancel()
    settle_after_cancel.set()

    result = await asyncio.gather(task, return_exceptions=True)
    assert isinstance(result[0], asyncio.CancelledError), (
        f"Expected CancelledError, got {result[0]!r}"
    )

    # Load-bearing: the success epoch must NOT have advanced (the subprocess
    # failed; a phantom bump would silently skip sibling waiters).
    assert _epoch(storage) == 0, (
        f"Epoch must not advance when the subprocess failed, even under a "
        f"simultaneous caller cancellation; saw {_epoch(storage)}."
    )


@pytest.mark.asyncio
async def test_cancel_before_subprocess_runs_no_phantom_bump(monkeypatch, tmp_path):
    """Cancel-before-work must not phantom-bump the epoch (issue #816).

    Modelled by patching ``_coalesced_run_refresh_cmd`` to raise
    ``CancelledError`` before any subprocess runs (equivalent to caller
    cancellation landing in the sync prefix). Only the leader body bumps the
    epoch, and only after real subprocess success, so a caller cancelled before
    any work leaves the epoch at 0.
    """
    storage = tmp_path / "storage_state.json"
    storage.write_text('{"cookies": [], "origins": []}')
    monkeypatch.setenv(auth_mod.NOTEBOOKLM_REFRESH_CMD_ENV, "dummy")

    coalesced_calls = 0

    async def fake_coalesced_run_refresh_cmd(refresh_key, storage_path, profile, *, deps=None):
        nonlocal coalesced_calls
        coalesced_calls += 1
        raise asyncio.CancelledError()

    async def fake_fetch_tokens_with_jar(cookie_jar, storage_path, **kwargs):
        if not getattr(cookie_jar, "_first_fetch_done", False):
            cookie_jar._first_fetch_done = True
            raise ValueError("Authentication expired. Run 'notebooklm login'.")
        return "csrf-token", "session-id"

    def fake_build(_p):
        return httpx.Cookies()

    def fake_snapshot(_j):
        return None

    monkeypatch.setattr(_auth_refresh, "_coalesced_run_refresh_cmd", fake_coalesced_run_refresh_cmd)
    monkeypatch.setattr(_auth_refresh, "_fetch_tokens_with_jar", fake_fetch_tokens_with_jar)
    monkeypatch.setattr(_auth_refresh, "build_httpx_cookies_from_storage", fake_build)
    monkeypatch.setattr(_auth_refresh, "snapshot_cookie_jar", fake_snapshot)

    jar = httpx.Cookies()
    result = await asyncio.gather(
        auth_mod._fetch_tokens_with_refresh(jar, storage_path=storage),
        return_exceptions=True,
    )

    assert isinstance(result[0], asyncio.CancelledError), (
        f"Expected CancelledError to propagate, got {result[0]!r}"
    )
    assert coalesced_calls == 1, "Test scaffolding: the modelled race must fire once."
    assert _epoch(storage) == 0, (
        f"Epoch must not advance when cancellation arrived before any subprocess "
        f"ran; saw {_epoch(storage)} — issue #816 regression."
    )


@pytest.mark.asyncio
async def test_cancel_before_work_with_warm_epoch_no_phantom_bump(monkeypatch, tmp_path):
    """Warm-epoch variant of issue #816.

    A prior successful refresh left the success epoch at 1. A new caller is
    cancelled before any subprocess runs (modelled as above). The epoch must
    stay at 1 — a stale prior success must never be re-attributed to this
    caller's no-op attempt.
    """
    storage = tmp_path / "storage_state.json"
    storage.write_text('{"cookies": [], "origins": []}')
    monkeypatch.setenv(auth_mod.NOTEBOOKLM_REFRESH_CMD_ENV, "dummy")

    # Seed the warm state: a prior successful refresh bumped the epoch to 1.
    _single_flight.note_success(str(storage.expanduser().resolve()))
    assert _epoch(storage) == 1

    async def fake_coalesced_run_refresh_cmd(refresh_key, storage_path, profile, *, deps=None):
        raise asyncio.CancelledError()

    async def fake_fetch_tokens_with_jar(cookie_jar, storage_path, **kwargs):
        if not getattr(cookie_jar, "_first_fetch_done", False):
            cookie_jar._first_fetch_done = True
            raise ValueError("Authentication expired. Run 'notebooklm login'.")
        return "csrf-token", "session-id"

    def fake_build(_p):
        return httpx.Cookies()

    def fake_snapshot(_j):
        return None

    monkeypatch.setattr(_auth_refresh, "_coalesced_run_refresh_cmd", fake_coalesced_run_refresh_cmd)
    monkeypatch.setattr(_auth_refresh, "_fetch_tokens_with_jar", fake_fetch_tokens_with_jar)
    monkeypatch.setattr(_auth_refresh, "build_httpx_cookies_from_storage", fake_build)
    monkeypatch.setattr(_auth_refresh, "snapshot_cookie_jar", fake_snapshot)

    jar = httpx.Cookies()
    result = await asyncio.gather(
        auth_mod._fetch_tokens_with_refresh(jar, storage_path=storage),
        return_exceptions=True,
    )

    assert isinstance(result[0], asyncio.CancelledError), (
        f"Expected CancelledError to propagate, got {result[0]!r}"
    )
    assert _epoch(storage) == 1, (
        f"Epoch must not advance against a prior cycle's success when cancellation "
        f"arrived before any subprocess ran; saw {_epoch(storage)} — warm #816 regression."
    )
