"""T7.F4 — refresh-cmd generation race + cancel safety.

Regression test for the two failure modes at ``src/notebooklm/auth.py``:

1. **Failed refresh marked successful (audit §27)**:
   ``_fetch_tokens_with_refresh()`` previously bumped ``_REFRESH_GENERATIONS``
   *before* awaiting ``_run_refresh_cmd()``. If the subprocess raised, the
   bump remained — concurrent waiters then observed the bumped generation
   and skipped their own refresh attempt, proceeding with stale storage.

2. **Cancellation can spawn duplicate subprocesses**:
   If the leader was cancelled while inside ``asyncio.to_thread(subprocess.run)``,
   the lock released while the subprocess kept running. A second caller could
   then acquire the lock and start a *second* subprocess against the same
   storage file.

The fix shares a per-resolved-storage-path ``asyncio.Future`` for the
in-flight refresh, shields the awaited future against caller cancellation,
and bumps the generation ONLY after the subprocess succeeds (no eager
claim). Cross-loop concurrent refreshes may both run their subprocess in
the rare race — the relaxed invariant is captured in
``tests/unit/test_refresh_lock_registry.py``
``test_two_loops_at_most_two_refreshes``.

Acceptance (per
``.sisyphus/plans/tier-7-thread-safety-concurrency/phase-2-wave-4.md#T7.F4``):

    Two callers triggering refresh concurrently while ``_run_refresh_cmd``
    fails on the first; assert the second still sees a refresh attempt
    (NOT "skip because generation bumped").

This test is the red gate — it must fail on origin/main (the second caller
skips refresh because the failed first caller already bumped the
generation) and pass once the fix is applied.
"""

from __future__ import annotations

import asyncio
import threading

import httpx
import pytest

from notebooklm import auth as auth_mod

# Mock-only tests (no real HTTP, no cassette) — opt out of the T8.D11
# tier-enforcement hook in ``tests/integration/conftest.py``. Marker
# was missed when this file landed (PR #621 T7.F4 merged the same day
# as PR #622 T8.D11 tier-enforcement).
pytestmark = pytest.mark.allow_no_vcr


@pytest.fixture(autouse=True)
def _clear_refresh_state():
    """Reset module state between tests so each starts with generation=0."""
    auth_mod._REFRESH_GENERATIONS.clear()
    yield
    auth_mod._REFRESH_GENERATIONS.clear()


@pytest.mark.asyncio
async def test_failed_refresh_does_not_skip_concurrent_waiter(monkeypatch, tmp_path):
    """Concurrent same-loop callers — first refresh fails, second must retry.

    The bug: caller A bumps ``_REFRESH_GENERATIONS`` BEFORE running the
    subprocess. Caller B (concurrent) had already captured the
    PRE-bump generation. When B reaches the inner check-and-claim mutex,
    it observes ``current_generation != refresh_generation`` and
    short-circuits — proceeding without calling ``_run_refresh_cmd``.
    Since A's subprocess actually FAILED (storage was never refreshed),
    B then reloads stale cookies and the retry fetch fails.

    Pre-fix observed failure: caller B succeeds silently with stale
    cookies — its retry fetch happens to succeed against the
    unchanged fake jar, masking the missed refresh.

    Post-fix: BOTH callers surface ``RuntimeError`` (the subprocess
    failure is propagated; neither silently skips). The generation
    counter stays at 0 because the subprocess never succeeded —
    subsequent callers will re-attempt the refresh.

    To force the race deterministically in a unit test, we wrap
    ``_get_refresh_lock`` with a barrier so both callers capture
    ``refresh_generation = 0`` BEFORE either enters the inner
    check-and-claim mutex. Without the barrier, asyncio scheduling
    would let A bump generation to 1 before B's pre-lock capture, so
    B would naturally observe gen=1, set its own ``refresh_generation
    = 1``, and the bug would never trigger.
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
        # All calls fail in this scenario — we want to verify B retries
        # (or, equivalently, that the subprocess is called for both
        # waiters even though A "owns" the in-flight slot).
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

    # Barrier aligned at "both callers have entered the refresh branch and
    # captured pre-lock generation, but neither has entered the inner
    # mutex yet". This is exactly the window the bug exploits.
    pre_lock_alignment = asyncio.Event()
    callers_reached_pre_lock = 0
    pre_lock_lock = threading.Lock()

    original_get_refresh_lock = auth_mod._get_refresh_lock

    def aligned_get_refresh_lock(p):
        # Called AFTER pre-lock generation capture, BEFORE the inner
        # mutex check-and-claim.
        nonlocal callers_reached_pre_lock
        with pre_lock_lock:
            callers_reached_pre_lock += 1
            if callers_reached_pre_lock >= 2:
                pre_lock_alignment.set()
        return _AlignedLock(original_get_refresh_lock(p), pre_lock_alignment)

    class _AlignedLock:
        def __init__(self, inner, gate):
            self._inner = inner
            self._gate = gate

        async def __aenter__(self):
            await self._inner.acquire()
            # Wait for BOTH callers to reach the pre-lock state.
            await self._gate.wait()
            return self

        async def __aexit__(self, exc_type, exc, tb):
            self._inner.release()
            return False

    monkeypatch.setattr(auth_mod, "_run_refresh_cmd", fake_run_refresh_cmd)
    monkeypatch.setattr(auth_mod, "_fetch_tokens_with_jar", fake_fetch_tokens_with_jar)
    monkeypatch.setattr(auth_mod, "build_httpx_cookies_from_storage", fake_build)
    monkeypatch.setattr(auth_mod, "snapshot_cookie_jar", fake_snapshot)
    monkeypatch.setattr(auth_mod, "_get_refresh_lock", aligned_get_refresh_lock)

    async def caller(jar):
        return await auth_mod._fetch_tokens_with_refresh(jar, storage_path=storage)

    jar_a = httpx.Cookies()
    jar_b = httpx.Cookies()
    results = await asyncio.gather(caller(jar_a), caller(jar_b), return_exceptions=True)

    # Both must surface the subprocess failure — neither may silently
    # succeed via reloaded-stale-cookies.
    for idx, r in enumerate(results):
        assert isinstance(r, RuntimeError), (
            f"Caller {idx} expected RuntimeError, got {r!r}. "
            "If a caller succeeded silently, it short-circuited on the "
            "leader's phantom generation bump and reloaded stale cookies."
        )

    # Subprocess must have been called at least once. The critical
    # assertion is that NEITHER caller silently skipped (caught above);
    # whether the second caller coalesced on the first's future or ran
    # its own subprocess is an implementation choice. Both designs are
    # acceptable as long as no caller short-circuits on a phantom bump.
    assert refresh_calls >= 1
    # And — the regression assertion — generation must NOT have advanced
    # after a failed refresh. A subsequent caller must still observe
    # ``should_run_refresh == True``.
    refresh_key = str(storage.expanduser().resolve())
    assert auth_mod._REFRESH_GENERATIONS.get(refresh_key, 0) == 0, (
        "Generation must not advance when refresh-cmd fails. Saw "
        f"_REFRESH_GENERATIONS[{refresh_key!r}] = "
        f"{auth_mod._REFRESH_GENERATIONS.get(refresh_key, 0)}"
    )


@pytest.mark.asyncio
async def test_concurrent_refresh_failure_followup_sees_attempt(monkeypatch, tmp_path):
    """First refresh fails; a subsequent caller MUST observe a refresh attempt.

    This is the spec's red-test wording: "two callers triggering refresh
    concurrently while ``_run_refresh_cmd`` fails on the first; assert the
    second still sees a refresh attempt (NOT 'skip because generation
    bumped')."

    Race model: caller A enters refresh, claims the in-flight slot,
    subprocess fails. With the fix, the generation is rolled back on
    failure. A subsequent caller B (whether concurrent-and-blocked or
    sequential-arriving) must STILL invoke the subprocess; it must NOT
    short-circuit on a phantom generation bump from A.

    Pre-fix: A bumps generation pre-subprocess (line 2634). Subprocess
    fails. Generation remains bumped. B's pre-lock generation capture
    sees the bumped value; inside the lock, the values match;
    ``should_run_refresh = True`` BUT B is reading the post-bump value
    that A left behind. In a TRULY concurrent scenario (modelled by the
    barrier in ``test_failed_refresh_does_not_skip_concurrent_waiter``),
    B captures gen=0 pre-lock, then inside the lock sees gen=1,
    short-circuits → reloads stale cookies, retry "succeeds" silently
    despite no actual refresh. This test exercises the simpler
    sequential-after-failure path: pre-fix, the retry-from-clean-slate
    code happens to also work, so this test alone is not a full red
    gate — it complements the concurrent-barrier test above.
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

    monkeypatch.setattr(auth_mod, "_run_refresh_cmd", fake_run_refresh_cmd)
    monkeypatch.setattr(auth_mod, "_fetch_tokens_with_jar", fake_fetch_tokens_with_jar)
    monkeypatch.setattr(auth_mod, "build_httpx_cookies_from_storage", fake_build)
    monkeypatch.setattr(auth_mod, "snapshot_cookie_jar", fake_snapshot)

    async def caller(jar):
        return await auth_mod._fetch_tokens_with_refresh(jar, storage_path=storage)

    jar_a = httpx.Cookies()
    jar_b = httpx.Cookies()

    task_a = asyncio.create_task(caller(jar_a))
    # Wait until A is inside the subprocess so B is forced to wait on the
    # asyncio lock.
    await enter_subprocess_event.wait()
    task_b = asyncio.create_task(caller(jar_b))
    # Yield so B enters the refresh path and blocks on the lock.
    await asyncio.sleep(0.05)

    # Release the leader; A's subprocess fails.
    leader_can_proceed_event.set()

    results = await asyncio.gather(task_a, task_b, return_exceptions=True)
    # A must surface the subprocess failure.
    assert isinstance(results[0], RuntimeError), f"A expected RuntimeError, got {results[0]!r}"
    assert "synthetic failure" in str(results[0])

    # B must have SEEN A REFRESH ATTEMPT — i.e. ``_run_refresh_cmd`` was
    # called for B's account. With the fix, A's failure rolled back the
    # generation, so B (waiting on the lock) acquires it, claims afresh,
    # and runs its own subprocess (call #2). That subprocess succeeded,
    # so B returns ``(csrf, sid, refreshed=True, snapshot)``.
    #
    # Pre-fix: A's failure left the generation bumped to 1; B's pre-lock
    # capture (taken BEFORE A bumped, because A only yields at the
    # ``await _run_refresh_cmd``) was 0; inside the lock B saw 1, so
    # ``should_run_refresh = False`` — B skipped the subprocess. The fix
    # ensures B sees a real refresh attempt (subprocess called for B).
    assert refresh_calls == 2, (
        f"Expected 2 subprocess calls (A failed, B retried), saw {refresh_calls}. "
        "Caller B short-circuited on the failed leader's phantom generation bump."
    )
    # B's result should be a successful refresh (its own subprocess call
    # succeeded). If B short-circuited it would still "succeed" because
    # the fake ``_fetch_tokens_with_jar`` succeeds on the second call —
    # which is exactly why the assertion above on ``refresh_calls`` is
    # the load-bearing one.
    assert not isinstance(results[1], BaseException), f"B raised: {results[1]!r}"
    csrf, sid, refreshed, _snap = results[1]
    assert refreshed is True


@pytest.mark.asyncio
async def test_waiter_cancellation_does_not_kill_inflight_subprocess(monkeypatch, tmp_path):
    """Cancellation of the leader must not abort the in-flight subprocess.

    Maps to the spec's cancel-safety requirement: "On caller cancellation,
    keep awaiting the in-flight subprocess instead of releasing the lock."

    Audit §27 (failure #2): "If the leader is cancelled while inside
    ``asyncio.to_thread(subprocess.run, ...)``, the subprocess keeps
    running but the lock releases — a later caller can start a *second*
    concurrent refresh against the same storage file."

    Scenario: caller A enters the refresh path, claims the in-flight
    slot, starts the subprocess. While the subprocess is gated, caller B
    arrives and should coalesce on the same in-flight future. Caller A
    is cancelled. The subprocess MUST keep running for B's benefit
    (shielded). After release, B observes a successful refresh, AND the
    subprocess ran exactly ONCE — not twice.

    Pre-fix: A's cancellation kills the awaited subprocess (no shield),
    B then either (a) starts its own subprocess (2 runs), or (b) skips
    on a phantom bump and reloads stale cookies.

    Post-fix: shielded shared future. A's cancellation unwinds A; the
    subprocess survives; B awaits the same future; subprocess runs
    exactly once.
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

    monkeypatch.setattr(auth_mod, "_run_refresh_cmd", fake_run_refresh_cmd)
    monkeypatch.setattr(auth_mod, "_fetch_tokens_with_jar", fake_fetch_tokens_with_jar)
    monkeypatch.setattr(auth_mod, "build_httpx_cookies_from_storage", fake_build)
    monkeypatch.setattr(auth_mod, "snapshot_cookie_jar", fake_snapshot)

    async def caller(jar):
        return await auth_mod._fetch_tokens_with_refresh(jar, storage_path=storage)

    jar_a = httpx.Cookies()
    jar_b = httpx.Cookies()

    task_a = asyncio.create_task(caller(jar_a))
    await leader_entered.wait()
    task_b = asyncio.create_task(caller(jar_b))
    # Let B enter the refresh path and coalesce on the in-flight future.
    await asyncio.sleep(0.05)

    # Cancel A — the in-flight subprocess MUST keep running for B's benefit
    # (cancel-safety: the leader's task is shielded).
    task_a.cancel()
    # Yield so cancellation propagates.
    await asyncio.sleep(0.05)

    # Critical assertion 1: cancellation of A must NOT kill the subprocess.
    assert subprocess_completions == 0, (
        f"After A's cancellation, subprocess_completions={subprocess_completions}; "
        "expected the in-flight subprocess to still be running (shielded). "
        "If it completed, A's cancellation cancelled the shared subprocess."
    )

    # Release the subprocess; both callers' state should settle.
    leader_can_proceed.set()

    a_result = await asyncio.gather(task_a, return_exceptions=True)
    assert isinstance(a_result[0], asyncio.CancelledError)

    # Critical assertion 2: B observes a SUCCESSFUL refresh — not stale
    # cookies-after-skip, not propagated CancelledError.
    csrf, sid, refreshed, _snap = await task_b
    assert refreshed is True
    assert csrf == "csrf-token"
    assert sid == "session-id"

    # Critical assertion 3 — audit §27 failure #2: no second subprocess
    # may run CONCURRENTLY with the in-flight one. If the leader's
    # cancellation released the lock mid-subprocess, a second caller
    # would acquire it and start a duplicate subprocess overlapping
    # the first. The fix keeps the lock held until the in-flight
    # subprocess settles, so ``max_in_flight`` must never exceed 1.
    assert max_in_flight == 1, (
        f"Expected at most 1 concurrent subprocess invocation (cancel-safety: "
        f"lock held until subprocess settles), saw max_in_flight={max_in_flight}. "
        "The leader's lock released mid-subprocess, allowing a duplicate."
    )
    assert concurrent_invocations_observed == 0, (
        f"Observed {concurrent_invocations_observed} concurrent subprocess "
        "invocations — bug §27 #2 regression."
    )
    # Critical assertion 4 — exact-once coalescing (CodeRabbit PR #621
    # follow-up): a regression where A's cancellation aborts the leader
    # subprocess and B starts a SECOND non-overlapping subprocess later
    # would still pass the non-overlap checks above. Pin the same-loop
    # coalescing contract directly: subprocess runs EXACTLY once across
    # both callers, regardless of cancellation timing.
    assert subprocess_invocations == 1, (
        f"Expected exactly 1 subprocess invocation (A+B coalesced on shared "
        f"future), saw {subprocess_invocations}. A sequential retry after "
        "leader cancellation still violates the shared in-flight refresh contract."
    )
    assert subprocess_completions == 1, (
        f"Expected exactly 1 completed subprocess invocation, saw {subprocess_completions}."
    )
    # The first subprocess completed successfully — generation must be
    # bumped exactly once.
    refresh_key = str(storage.expanduser().resolve())
    assert auth_mod._REFRESH_GENERATIONS.get(refresh_key, 0) == 1, (
        "Generation must be bumped exactly once on successful coalesced refresh."
    )


@pytest.mark.asyncio
async def test_cancel_settle_race_does_not_bump_on_failure(monkeypatch, tmp_path):
    """Cancel/settle race: caller cancelled AS subprocess settles with failure.

    Regression for the CodeRabbit finding on PR #621: when the leader is
    cancelled in the same loop tick that the subprocess settles with an
    exception, the ``_settle`` callback used to clear the in-flight
    registry entry, leaving the caller's CancelledError handler unable
    to inspect ``inflight.exception()``. The handler then fell into the
    success path — bumped generation — and other waiters observed the
    phantom bump, skipped their own refresh, and proceeded with stale
    cookies.

    Fix: ``_settle`` keeps the done future in the registry; the
    CancelledError handler observes ``inflight.exception()`` and routes
    the caller down the failure branch (no generation bump).
    """
    storage = tmp_path / "storage_state.json"
    storage.write_text('{"cookies": [], "origins": []}')
    monkeypatch.setenv(auth_mod.NOTEBOOKLM_REFRESH_CMD_ENV, "dummy")

    cancel_now = asyncio.Event()
    settle_after_cancel = asyncio.Event()

    async def fake_run_refresh_cmd(storage_path, profile):
        # Signal the test to cancel the caller, then wait briefly so the
        # cancellation is scheduled before the subprocess settles.
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

    monkeypatch.setattr(auth_mod, "_run_refresh_cmd", fake_run_refresh_cmd)
    monkeypatch.setattr(auth_mod, "_fetch_tokens_with_jar", fake_fetch_tokens_with_jar)
    monkeypatch.setattr(auth_mod, "build_httpx_cookies_from_storage", fake_build)
    monkeypatch.setattr(auth_mod, "snapshot_cookie_jar", fake_snapshot)

    jar = httpx.Cookies()
    task = asyncio.create_task(auth_mod._fetch_tokens_with_refresh(jar, storage_path=storage))
    await cancel_now.wait()
    # Cancel the caller and release the subprocess in adjacent loop ticks
    # so settle and cancellation interleave.
    task.cancel()
    settle_after_cancel.set()

    result = await asyncio.gather(task, return_exceptions=True)
    # Caller cancellation wins. The subprocess failure must NOT have
    # been swallowed silently.
    assert isinstance(result[0], asyncio.CancelledError), (
        f"Expected CancelledError, got {result[0]!r}"
    )

    # The load-bearing assertion: generation must NOT have advanced.
    # If the cancel/settle race silently swallowed the subprocess
    # failure, the caller would have fallen into the success branch and
    # bumped ``_REFRESH_GENERATIONS`` — leaving a phantom bump for
    # other waiters.
    refresh_key = str(storage.expanduser().resolve())
    assert auth_mod._REFRESH_GENERATIONS.get(refresh_key, 0) == 0, (
        "Generation must not advance when the subprocess failed, even when "
        "the caller was cancelled at the same time. "
        f"_REFRESH_GENERATIONS[{refresh_key!r}] = "
        f"{auth_mod._REFRESH_GENERATIONS.get(refresh_key, 0)} — phantom bump regression."
    )
