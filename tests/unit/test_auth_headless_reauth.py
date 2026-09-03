"""Unit tests for the layer-3 headless re-auth decision layer.

Covers :mod:`notebooklm._browser.headless_reauth`:

* the opt-in × profile-present × failure-class decision matrix,
* the three typed honest outcomes (UNAVAILABLE / FAILED / SUCCESS) and that
  SUCCESS is never reported on a dead/redirected session,
* the env-var opt-in gate (``NOTEBOOKLM_HEADLESS_REAUTH=1``),
* the default-unchanged behavior (no opt-in + no profile → UNAVAILABLE, the
  browser is never launched).

The browser drive is faked end-to-end via ``run_browser_capture`` so no real
Playwright / network is needed; ``playwright`` stays lazily imported.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, NoReturn

import pytest

from notebooklm._browser import headless_reauth as hr
from notebooklm._browser.browser_capture import _CaptureAbortKind, _HeadlessCaptureAbort
from notebooklm._browser.headless_reauth import (
    HeadlessReauthResult,
    HeadlessReauthState,
    HeadlessReauthStatus,
    attempt_headless_reauth,
    headless_reauth_env_enabled,
)
from notebooklm.exceptions import HeadlessLoginRequiredError


def _make_profile(tmp_path: Path) -> Path:
    """Create (idempotently) a non-empty browser-profile dir on disk."""
    profile = tmp_path / "browser_profile"
    profile.mkdir(exist_ok=True)
    (profile / "Default").mkdir(exist_ok=True)  # a populated profile dir
    return profile


def _unexpected_profile_capture(*_args: object, **_kwargs: object) -> NoReturn:
    raise AssertionError("the dedicated-profile capture gateway must not run")


def _unexpected_cdp_capture(*_args: object, **_kwargs: object) -> NoReturn:
    raise AssertionError("the CDP capture gateway must not run")


def _attempt(
    *,
    storage_path: Path,
    allow_headless: bool,
    browser_profile: Path | None,
    env: dict[str, str],
    playwright_installed: bool,
    profile_capture,
    cdp_capture,
    state: HeadlessReauthState,
    cdp_url: str | None = None,
) -> HeadlessReauthResult:
    """Drive the operation through explicit gateways and fresh owner state."""
    return hr._attempt_headless_reauth(
        storage_path=storage_path,
        allow_headless=allow_headless,
        browser_profile=browser_profile,
        profile=None,
        browser="chromium",
        include_domains=None,
        cdp_url=cdp_url,
        env=env,
        deps=hr._HeadlessReauthDeps(
            playwright_installed=lambda: playwright_installed,
            resolve_profile=hr._resolve_reusable_profile,
            resolve_cdp=hr.resolve_cdp_url,
            run_profile_capture=profile_capture,
            run_cdp_capture=cdp_capture,
            state=state,
        ),
    )


def _readiness(
    *,
    browser_profile: Path,
    playwright_installed: bool,
) -> hr.HeadlessReauthReadiness:
    return hr._headless_reauth_readiness(
        browser_profile=browser_profile,
        profile=None,
        resolve_profile=hr._resolve_reusable_profile,
        playwright_installed=lambda: playwright_installed,
    )


# ---------------------------------------------------------------------------
# env opt-in gate
# ---------------------------------------------------------------------------


def test_env_enabled_only_for_exact_one() -> None:
    assert headless_reauth_env_enabled({"NOTEBOOKLM_HEADLESS_REAUTH": "1"}) is True
    assert headless_reauth_env_enabled({"NOTEBOOKLM_HEADLESS_REAUTH": "0"}) is False
    assert headless_reauth_env_enabled({"NOTEBOOKLM_HEADLESS_REAUTH": "true"}) is False
    assert headless_reauth_env_enabled({}) is False


# ---------------------------------------------------------------------------
# Decision matrix: opt-in OFF → UNAVAILABLE, never launches a browser
# ---------------------------------------------------------------------------


def test_optin_off_is_unavailable_and_never_launches(tmp_path: Path) -> None:
    """No opt-in + no env → UNAVAILABLE; the capture core is never reached.

    This pins the locked design decision: L3 NEVER fires by default.
    """
    profile = _make_profile(tmp_path)

    result = _attempt(
        storage_path=tmp_path / "storage_state.json",
        allow_headless=False,
        browser_profile=profile,
        env={},
        playwright_installed=True,
        profile_capture=_unexpected_profile_capture,
        cdp_capture=_unexpected_cdp_capture,
        state=HeadlessReauthState(),
    )
    assert result.status is HeadlessReauthStatus.UNAVAILABLE
    assert result.succeeded is False
    assert "not enabled" in result.reason


def test_optin_off_even_with_profile_is_unavailable(tmp_path: Path) -> None:
    result = _attempt(
        storage_path=tmp_path / "storage_state.json",
        allow_headless=False,
        browser_profile=_make_profile(tmp_path),
        env={"NOTEBOOKLM_HEADLESS_REAUTH": "0"},
        playwright_installed=True,
        profile_capture=_unexpected_profile_capture,
        cdp_capture=_unexpected_cdp_capture,
        state=HeadlessReauthState(),
    )
    assert result.status is HeadlessReauthStatus.UNAVAILABLE


# ---------------------------------------------------------------------------
# Decision matrix: opt-in ON but no reusable profile → UNAVAILABLE
# ---------------------------------------------------------------------------


def test_optin_on_no_profile_dir_is_unavailable(tmp_path: Path) -> None:
    result = _attempt(
        storage_path=tmp_path / "storage_state.json",
        allow_headless=True,
        browser_profile=tmp_path / "does_not_exist",
        env={},
        playwright_installed=True,
        profile_capture=_unexpected_profile_capture,
        cdp_capture=_unexpected_cdp_capture,
        state=HeadlessReauthState(),
    )
    assert result.status is HeadlessReauthStatus.UNAVAILABLE
    assert "no reusable browser profile" in result.reason


def test_optin_on_empty_profile_dir_is_unavailable(tmp_path: Path) -> None:
    """A freshly-mkdir'd but empty profile holds no Google session → decline."""
    empty = tmp_path / "browser_profile"
    empty.mkdir()
    result = _attempt(
        storage_path=tmp_path / "storage_state.json",
        allow_headless=True,
        browser_profile=empty,
        env={},
        playwright_installed=True,
        profile_capture=_unexpected_profile_capture,
        cdp_capture=_unexpected_cdp_capture,
        state=HeadlessReauthState(),
    )
    assert result.status is HeadlessReauthStatus.UNAVAILABLE


def test_ownership_marker_only_profile_is_not_reusable(tmp_path: Path) -> None:
    """A freshly prepared marker-only directory holds no reusable Google session."""
    profile = tmp_path / "A.json.browser_profile"
    profile.mkdir()
    (profile / ".notebooklm-owned").touch()

    readiness = hr.headless_reauth_readiness(browser_profile=profile)

    assert readiness.profile_present is False


# ---------------------------------------------------------------------------
# Decision matrix: opt-in ON + profile present → drives the browser
# ---------------------------------------------------------------------------


def test_success_when_capture_succeeds(tmp_path: Path) -> None:
    """Capture returns normally → SUCCESS, storage_path carried out."""
    storage = tmp_path / "storage_state.json"
    captured: dict[str, object] = {}

    def _fake_capture(plan, io, *, headless, interactive):
        captured["headless"] = headless
        captured["interactive"] = interactive
        captured["profile"] = plan.browser_profile
        return None

    profile = _make_profile(tmp_path)
    result = _attempt(
        storage_path=storage,
        allow_headless=True,
        browser_profile=profile,
        env={},
        playwright_installed=True,
        profile_capture=_fake_capture,
        cdp_capture=_unexpected_cdp_capture,
        state=HeadlessReauthState(),
    )
    assert result.status is HeadlessReauthStatus.SUCCESS
    assert result.succeeded is True
    assert result.storage_path == storage
    # The headless arm must be driven non-interactively, headless.
    assert captured == {
        "headless": True,
        "interactive": False,
        "profile": profile,
    }


def test_failed_when_profile_session_also_dead(tmp_path: Path) -> None:
    """Headless landed on the Google login page → FAILED, NEVER success."""

    def _redirected(plan, io, *, headless, interactive):
        raise HeadlessLoginRequiredError("redirected to login")

    result = _attempt(
        storage_path=tmp_path / "storage_state.json",
        allow_headless=True,
        browser_profile=_make_profile(tmp_path),
        env={},
        playwright_installed=True,
        profile_capture=_redirected,
        cdp_capture=_unexpected_cdp_capture,
        state=HeadlessReauthState(),
    )
    assert result.status is HeadlessReauthStatus.FAILED
    assert result.succeeded is False
    assert result.storage_path is None
    assert "expired" in result.reason


def test_failed_on_unexpected_capture_error(tmp_path: Path) -> None:
    """An unexpected capture exception → FAILED (best-effort recovery)."""

    def _boom(plan, io, *, headless, interactive):
        raise RuntimeError("launch blew up")

    result = _attempt(
        storage_path=tmp_path / "storage_state.json",
        allow_headless=True,
        browser_profile=_make_profile(tmp_path),
        env={},
        playwright_installed=True,
        profile_capture=_boom,
        cdp_capture=_unexpected_cdp_capture,
        state=HeadlessReauthState(),
    )
    assert result.status is HeadlessReauthStatus.FAILED
    # Error TYPE only — never a cookie value.
    assert "RuntimeError" in result.reason


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        (_CaptureAbortKind.BROWSER_CLOSED, "browser was closed"),
        (_CaptureAbortKind.CONNECTION_EXHAUSTED, "connection retries were exhausted"),
    ],
)
def test_typed_capture_abort_maps_to_safe_infrastructure_reason(
    tmp_path: Path, kind: _CaptureAbortKind, expected: str
) -> None:
    """Typed capture aborts stay FAILED without masquerading as expired sessions."""

    def _abort(*_args, **_kwargs):
        raise _HeadlessCaptureAbort(kind)

    result = _attempt(
        storage_path=tmp_path / "storage_state.json",
        allow_headless=True,
        browser_profile=_make_profile(tmp_path),
        env={},
        playwright_installed=True,
        profile_capture=_abort,
        cdp_capture=_unexpected_cdp_capture,
        state=HeadlessReauthState(),
    )

    assert result.status is HeadlessReauthStatus.FAILED
    assert expected in result.reason
    assert "expired" not in result.reason
    assert "http" not in result.reason
    assert "cookie" not in result.reason


def test_typed_capture_abort_from_cdp_uses_same_safe_mapping(tmp_path: Path) -> None:
    """The CDP arm shares the profile arm's infrastructure classification."""

    def _abort(*_args, **_kwargs):
        raise _HeadlessCaptureAbort(_CaptureAbortKind.BROWSER_CLOSED)

    result = _attempt(
        storage_path=tmp_path / "storage_state.json",
        allow_headless=True,
        browser_profile=None,
        cdp_url="http://127.0.0.1:9222",
        env={},
        playwright_installed=True,
        profile_capture=_unexpected_profile_capture,
        cdp_capture=_abort,
        state=HeadlessReauthState(),
    )

    assert result.status is HeadlessReauthStatus.FAILED
    assert result.reason == (
        "headless capture failed: browser was closed during capture; "
        "retry headless re-authentication"
    )


def test_env_optin_drives_browser_without_explicit_flag(tmp_path: Path) -> None:
    """``NOTEBOOKLM_HEADLESS_REAUTH=1`` enables L3 even with allow_headless=False."""
    result = _attempt(
        storage_path=tmp_path / "storage_state.json",
        allow_headless=False,
        browser_profile=_make_profile(tmp_path),
        env={"NOTEBOOKLM_HEADLESS_REAUTH": "1"},
        playwright_installed=True,
        profile_capture=lambda *_args, **_kwargs: None,
        cdp_capture=_unexpected_cdp_capture,
        state=HeadlessReauthState(),
    )
    assert result.status is HeadlessReauthStatus.SUCCESS


def test_unavailable_when_playwright_missing(tmp_path: Path) -> None:
    """Opt-in + profile, but the ``browser`` extra is absent → UNAVAILABLE.

    Distinct from FAILED: there is nothing to drive, not a dead session.
    """
    result = _attempt(
        storage_path=tmp_path / "storage_state.json",
        allow_headless=True,
        browser_profile=_make_profile(tmp_path),
        env={},
        playwright_installed=False,
        profile_capture=_unexpected_profile_capture,
        cdp_capture=_unexpected_cdp_capture,
        state=HeadlessReauthState(),
    )
    assert result.status is HeadlessReauthStatus.UNAVAILABLE
    assert "playwright" in result.reason


# ---------------------------------------------------------------------------
# HeadlessReauthResult convenience
# ---------------------------------------------------------------------------


def test_result_succeeded_property() -> None:
    assert HeadlessReauthResult(HeadlessReauthStatus.SUCCESS, "ok").succeeded is True
    assert HeadlessReauthResult(HeadlessReauthStatus.FAILED, "no").succeeded is False
    assert HeadlessReauthResult(HeadlessReauthStatus.UNAVAILABLE, "no").succeeded is False


# ---------------------------------------------------------------------------
# Explicit-path coalescing: concurrent attempts → ONE browser per profile
# ---------------------------------------------------------------------------


def test_concurrent_explicit_attempts_coalesce_to_one_browser(tmp_path: Path) -> None:
    """N concurrent ``attempt_headless_reauth`` calls drive ONE browser.

    The explicit ``refresh_auth(allow_headless=True)`` entry bypasses the
    mid-RPC coordinator's single-flight, so the per-storage-path drive lock +
    outcome coalescing in ``attempt_headless_reauth`` is what prevents redundant
    browsers. The leader drives one real capture and publishes its typed
    SUCCESS; the followers, which snapshotted the drive-sequence before blocking
    and see it advance during their wait, coalesce onto that SUCCESS outcome
    (not on the storage file's mtime).
    """
    import threading
    import time

    storage = tmp_path / "storage_state.json"
    profile = _make_profile(tmp_path)
    drives = {"count": 0}
    barrier = threading.Barrier(6)

    def _slow_capture(plan, io, *, headless, interactive):
        drives["count"] += 1
        time.sleep(0.05)  # hold the lock so followers pile up behind it
        # Simulate the real capture writing fresh storage (advances mtime).
        plan.storage_path.write_text('{"cookies": [], "origins": []}', encoding="utf-8")

    state = HeadlessReauthState()

    results: list[HeadlessReauthResult] = []
    results_lock = threading.Lock()

    def _worker() -> None:
        barrier.wait()
        res = _attempt(
            storage_path=storage,
            allow_headless=True,
            browser_profile=profile,
            env={},
            playwright_installed=True,
            profile_capture=_slow_capture,
            cdp_capture=_unexpected_cdp_capture,
            state=state,
        )
        with results_lock:
            results.append(res)

    threads = [threading.Thread(target=_worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Exactly one real browser drive; all six callers report SUCCESS (the
    # leader drove, the followers coalesced onto the fresh storage).
    assert drives["count"] == 1
    assert len(results) == 6
    assert all(r.status is HeadlessReauthStatus.SUCCESS for r in results)


class _ContentionSignalLock:
    """A drive-lock stand-in that fires an event the moment it must block.

    Wraps a real ``threading.Lock`` and exposes a ``contended`` event set on the
    first ``acquire`` that cannot take the lock immediately — i.e. the moment a
    follower is about to block behind the leader. That lets a test gate the
    "an unrelated writer advances the file WHILE the follower waits" step
    deterministically, without sleeps, and guarantees the follower has already
    taken its pre-wait drive-sequence snapshot (the snapshot happens right
    before ``with record.drive_lock`` in ``_drive_capture_coalesced``).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.contended = threading.Event()

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        if self._lock.acquire(blocking=False):
            return True
        # Held by someone else — this acquirer is about to block.
        self.contended.set()
        if timeout is None or timeout < 0:
            return self._lock.acquire(blocking)
        return self._lock.acquire(blocking, timeout)

    def release(self) -> None:
        self._lock.release()

    def __enter__(self) -> _ContentionSignalLock:
        self.acquire()
        return self

    def __exit__(self, *_exc: object) -> bool:
        self.release()
        return False


def test_follower_does_not_report_false_success_when_leader_failed(tmp_path: Path) -> None:
    """Leader re-mint FAILS + an unrelated write advances the file → follower FAILED.

    This is the [capture-3] regression. The old coalescing keyed on the storage
    file's mtime: a follower that observed the mtime advance while it waited
    reported SUCCESS — even if the leader's re-mint actually FAILED and the
    advance came from an *unrelated* writer (keepalive / PSIDTS rotation). The
    new coalescing keys on the leader's TYPED outcome stamped with a
    drive-sequence, so the unrelated mtime bump is ignored and the follower
    surfaces the leader's real FAILED result.

    Deterministic ordering (no sleeps): the leader parks inside its capture
    holding the drive lock; the follower blocks on that lock (firing
    ``contended`` right after it takes its pre-wait sequence snapshot); the test
    then advances the file mtime and only then lets the leader's drive fail.
    On the pre-c-PR3 mtime code the follower would read SUCCESS here; on the
    outcome-based code it reads FAILED.
    """
    import os

    storage = tmp_path / "storage_state.json"
    storage.write_text("{}", encoding="utf-8")  # pre-existing file (mtime t0)
    profile = _make_profile(tmp_path)

    # Inject a shared drive record whose lock signals contention, so both the
    # leader and the follower coalesce through the same sequence + outcome.
    signal_lock = _ContentionSignalLock()
    state = HeadlessReauthState(
        _record_factory=lambda: hr._DriveRecord(
            drive_lock=signal_lock,  # type: ignore[arg-type]
            _state_lock=threading.Lock(),
        )
    )
    record = state.drive_record(storage, source="profile")
    assert record.drive_lock is signal_lock

    leader_in_capture = threading.Event()
    leader_may_finish = threading.Event()

    def _leader_capture(plan, io, *, headless, interactive):
        leader_in_capture.set()
        assert leader_may_finish.wait(timeout=5)
        # The leader's re-mint FAILS: the profile's Google session is dead.
        raise HeadlessLoginRequiredError("profile session is dead")

    results: dict[str, HeadlessReauthResult] = {}

    def _run(tag: str) -> None:
        results[tag] = _attempt(
            storage_path=storage,
            allow_headless=True,
            browser_profile=profile,
            env={},
            playwright_installed=True,
            profile_capture=_leader_capture,
            cdp_capture=_unexpected_cdp_capture,
            state=state,
        )

    leader = threading.Thread(target=_run, args=("leader",))
    leader.start()
    assert leader_in_capture.wait(timeout=5)  # leader holds the drive lock

    follower = threading.Thread(target=_run, args=("follower",))
    follower.start()
    # Follower has snapshotted its sequence (pre=0) and is now blocked on the lock.
    assert record.drive_lock.contended.wait(timeout=5)  # type: ignore[attr-defined]

    # An UNRELATED writer advances the storage file's mtime while the follower
    # waits — the exact trigger that false-positived the old mtime heuristic.
    st = storage.stat()
    storage.write_text('{"cookies": [{"name": "x"}], "origins": []}', encoding="utf-8")
    os.utime(storage, (st.st_atime + 10, st.st_mtime + 10))

    leader_may_finish.set()  # leader's drive now fails → publishes FAILED
    leader.join(timeout=5)
    follower.join(timeout=5)

    assert results["leader"].status is HeadlessReauthStatus.FAILED
    # The crux: the follower must NOT infer SUCCESS from the advanced mtime.
    assert results["follower"].status is HeadlessReauthStatus.FAILED
    assert results["follower"].succeeded is False


def test_stale_outcome_from_previous_cycle_is_not_coalesced(tmp_path: Path) -> None:
    """A solo follower after a past drive treats the old outcome as 'no outcome'.

    A caller that arrives when NO drive is active during its wait must not
    coalesce onto the outcome of a *previous* drive cycle (whose cookies may
    have re-died since) — it drives its own browser. This pins the
    strictly-newer-sequence discipline (``completed > pre``, not ``>=``).
    """
    storage = tmp_path / "storage_state.json"
    profile = _make_profile(tmp_path)
    drives = {"count": 0}
    state = HeadlessReauthState()

    def _capture(plan, io, *, headless, interactive):
        drives["count"] += 1
        plan.storage_path.write_text('{"cookies": [], "origins": []}', encoding="utf-8")

    # First (completed) drive cycle publishes a SUCCESS outcome at sequence 1.
    first = _attempt(
        storage_path=storage,
        allow_headless=True,
        browser_profile=profile,
        env={},
        playwright_installed=True,
        profile_capture=_capture,
        cdp_capture=_unexpected_cdp_capture,
        state=state,
    )
    assert first.status is HeadlessReauthStatus.SUCCESS
    assert drives["count"] == 1

    # A later, solo caller must NOT coalesce onto that stale outcome; it drives.
    second = _attempt(
        storage_path=storage,
        allow_headless=True,
        browser_profile=profile,
        env={},
        playwright_installed=True,
        profile_capture=_capture,
        cdp_capture=_unexpected_cdp_capture,
        state=state,
    )
    assert second.status is HeadlessReauthStatus.SUCCESS
    assert drives["count"] == 2  # drove its own browser, did not coalesce


# ---------------------------------------------------------------------------
# Why the drive coalescer is NOT ``_auth.single_flight`` (ADR-0030 honesty pass)
# ---------------------------------------------------------------------------


def test_single_flight_is_unreachable_from_the_sync_drive_entry() -> None:
    """``single_flight.claim`` needs a running loop; this drive never has one.

    Pins the reason ``_DriveRecord``'s docstring now states, so the refusal
    cannot rot back into the retired "a later shared single-flight core will
    reuse this wholesale" claim.

    ``single_flight.claim`` creates a leader ``asyncio.Task`` and therefore
    calls ``asyncio.get_running_loop()``. The headless drive's coalescing point
    (``_drive_capture_coalesced``, reached only from the SYNC public
    ``attempt_headless_reauth``) has no running loop, and its one production
    caller runs it *off* the loop via ``asyncio.to_thread`` precisely because
    the browser drive blocks — so the loop is absent by construction, not by
    accident.
    """
    import asyncio
    import inspect

    from notebooklm._auth import recovery
    from notebooklm._auth import single_flight as sf

    single_flight = sf.SingleFlight()

    # The entry point and its coalescer are synchronous; the async caller that
    # reaches them hands them to a worker thread.
    assert not inspect.iscoroutinefunction(attempt_headless_reauth)
    assert not inspect.iscoroutinefunction(hr._drive_capture_coalesced)
    assert inspect.iscoroutinefunction(recovery.try_headless_reauth)

    async def _never_runs() -> None:  # pragma: no cover - claim raises first
        return None

    # In the production shape (inside ``asyncio.to_thread``) there is no loop,
    # so a claim cannot be made at all.
    async def _drive_in_worker_thread() -> str:
        def _claim_from_worker() -> str:
            try:
                single_flight.claim(("path", "profile"), _never_runs)
            except RuntimeError as exc:
                return str(exc)
            return "claimed"  # pragma: no cover - would mean a loop was present

        return await asyncio.to_thread(_claim_from_worker)

    assert asyncio.run(_drive_in_worker_thread()) == "no running event loop"


def test_drive_record_keying_is_resolved_inside_the_sync_entry(tmp_path: Path) -> None:
    """The ``(path, source)`` key depends on CDP resolution the async caller lacks.

    The second reason ``_DriveRecord`` documents for not hoisting the claim up
    into ``recovery.try_headless_reauth``: the ``source`` half of the key is
    decided by ``resolve_cdp_url`` — which also enforces the loopback boundary —
    inside ``attempt_headless_reauth``. A hoisted claim would have to duplicate
    that security decision or collapse the two sources onto one key, which is
    the cross-source false-FAILED bug the split key exists to prevent.
    """
    storage = tmp_path / "storage_state.json"

    state = HeadlessReauthState()
    profile_record = state.drive_record(storage, source="profile")
    cdp_record = state.drive_record(storage, source="cdp")

    # Same storage file, different credential source → different records, so a
    # dead profile's FAILED can never be handed to a live CDP attach.
    assert profile_record is not cdp_record
    assert state.drive_record(storage, source="profile") is profile_record

    # The source is only knowable after ``resolve_cdp_url`` runs, and that call
    # is the loopback gate: a remote endpoint resolves to ``None`` (→ the
    # "profile" source), so the key and the security boundary are one decision.
    assert hr.resolve_cdp_url("http://127.0.0.1:9222", {}) == "http://127.0.0.1:9222"
    assert hr.resolve_cdp_url("http://remote-host:9222", {}) is None


def test_headless_reauth_state_isolated_and_quiescent_reset(tmp_path: Path) -> None:
    """Fresh owners do not share records and only clear settled state."""
    storage = tmp_path / "storage_state.json"
    first = HeadlessReauthState()
    second = HeadlessReauthState()

    first_record = first.drive_record(storage, source="profile")
    assert second.drive_record(storage, source="profile") is not first_record

    first_record.drive_lock.acquire()
    try:
        with pytest.raises(RuntimeError, match="while a drive is active"):
            first.reset_if_quiescent()
        assert first.drive_record(storage, source="profile") is first_record
    finally:
        first_record.drive_lock.release()

    first.reset_if_quiescent()
    assert first.drive_record(storage, source="profile") is not first_record


def test_headless_reauth_state_rejects_reset_during_pre_lock_reservation(
    tmp_path: Path,
) -> None:
    """Reset cannot clear a record between lookup and drive-lock acquisition."""
    state = HeadlessReauthState()
    storage = tmp_path / "storage_state.json"
    reserved = threading.Event()
    release = threading.Event()

    def reserve_without_locking() -> None:
        with state._reserve_drive_record(storage, source="profile") as record:
            assert not record.drive_lock.locked()
            reserved.set()
            assert release.wait(timeout=5)

    worker = threading.Thread(target=reserve_without_locking)
    worker.start()
    assert reserved.wait(timeout=5)
    try:
        with pytest.raises(RuntimeError, match="while a drive is active"):
            state.reset_if_quiescent()
    finally:
        release.set()
        worker.join(timeout=5)

    assert not worker.is_alive()
    state.reset_if_quiescent()


def test_operation_dependencies_carry_no_credential_values() -> None:
    """The operation seam contains gateways and state, never captured secrets."""
    deps = hr._HeadlessReauthDeps(
        playwright_installed=lambda: True,
        resolve_profile=hr._resolve_reusable_profile,
        resolve_cdp=hr.resolve_cdp_url,
        run_profile_capture=_unexpected_profile_capture,
        run_cdp_capture=_unexpected_cdp_capture,
        state=HeadlessReauthState(),
    )

    assert set(vars(deps)) == {
        "playwright_installed",
        "resolve_profile",
        "resolve_cdp",
        "run_profile_capture",
        "run_cdp_capture",
        "state",
    }


def test_production_composition_reuses_the_process_default_state() -> None:
    """Every public attempt composes the same process-lifetime coalescer."""
    process_default = HeadlessReauthState.process_default()

    assert hr._production_deps().state is process_default
    assert hr._production_deps().state is process_default


# ---------------------------------------------------------------------------
# Readiness probe (doctor diagnostics): credential-free, launches nothing
# ---------------------------------------------------------------------------


def test_readiness_ready_when_profile_present_and_playwright(tmp_path: Path) -> None:
    """Profile present + playwright importable → available, ready detail."""
    profile = _make_profile(tmp_path)
    readiness = _readiness(browser_profile=profile, playwright_installed=True)

    assert readiness.profile_present is True
    assert readiness.playwright_installed is True
    assert readiness.available is True
    assert "ready" in readiness.detail
    assert "NOTEBOOKLM_HEADLESS_REAUTH" in readiness.detail


def test_readiness_unavailable_without_profile(tmp_path: Path) -> None:
    readiness = _readiness(
        browser_profile=tmp_path / "nope",
        playwright_installed=True,
    )

    assert readiness.profile_present is False
    assert readiness.available is False
    assert "no reusable browser profile" in readiness.detail


def test_readiness_unavailable_without_playwright(tmp_path: Path) -> None:
    profile = _make_profile(tmp_path)
    readiness = _readiness(browser_profile=profile, playwright_installed=False)

    assert readiness.profile_present is True
    assert readiness.playwright_installed is False
    assert readiness.available is False
    assert "playwright not installed" in readiness.detail


def test_readiness_reports_both_missing(tmp_path: Path) -> None:
    readiness = _readiness(
        browser_profile=tmp_path / "nope",
        playwright_installed=False,
    )

    assert readiness.available is False
    assert "no reusable browser profile" in readiness.detail
    assert "playwright not installed" in readiness.detail


def test_readiness_never_drives_a_browser(tmp_path: Path) -> None:
    """The readiness probe must never launch the capture core."""

    readiness = _readiness(
        browser_profile=_make_profile(tmp_path),
        playwright_installed=True,
    )
    assert readiness.available is True


def test_playwright_installed_true_with_extra() -> None:
    """The browser extra IS installed in the test env, so the probe is True."""
    assert hr._playwright_installed() is True


# ---------------------------------------------------------------------------
# CDP attach arm (alternative credential source): resolution + routing
# ---------------------------------------------------------------------------


def test_resolve_cdp_url_explicit_arg_wins() -> None:
    assert (
        hr.resolve_cdp_url(
            "http://127.0.0.1:9222", {"NOTEBOOKLM_HEADLESS_REAUTH_CDP_URL": "http://x"}
        )
        == "http://127.0.0.1:9222"
    )


def test_resolve_cdp_url_falls_back_to_env() -> None:
    assert (
        hr.resolve_cdp_url(None, {"NOTEBOOKLM_HEADLESS_REAUTH_CDP_URL": "http://127.0.0.1:9222"})
        == "http://127.0.0.1:9222"
    )


def test_resolve_cdp_url_blank_is_unset() -> None:
    assert hr.resolve_cdp_url(None, {"NOTEBOOKLM_HEADLESS_REAUTH_CDP_URL": "   "}) is None
    assert hr.resolve_cdp_url("", {}) is None
    assert hr.resolve_cdp_url(None, {}) is None


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:9222",
        "http://localhost:9222",
        "http://[::1]:9222",
        "http://127.5.6.7:9222",  # anywhere in 127.0.0.0/8
        "ws://127.0.0.1:9222/devtools/browser/abc",
        "127.0.0.1:9222",  # bare host:port (no scheme)
    ],
)
def test_resolve_cdp_url_allows_loopback(url: str) -> None:
    """Loopback endpoints are the only sanctioned LOCAL-ONLY targets."""
    assert hr.resolve_cdp_url(url, {}) == url


@pytest.mark.parametrize(
    "url",
    [
        "http://remote-host:9222",
        "http://10.0.0.5:9222",  # private LAN, NOT loopback
        "http://192.168.1.10:9222",
        "http://0.0.0.0:9222",  # wildcard bind is not loopback
        "http://example.com:9222",
        "http://[malformed-secret",
        "not a url",
    ],
)
def test_resolve_cdp_url_rejects_non_loopback(url: str, caplog) -> None:
    """Non-loopback (remote / LAN / wildcard / junk) endpoints are rejected.

    This is the LOCAL-UNATTENDED-ONLY boundary: a remote CDP endpoint must
    never reach ``connect_over_cdp``. The rejection must NOT log the endpoint.
    """
    import logging

    with caplog.at_level(logging.WARNING, logger="notebooklm.auth"):
        assert hr.resolve_cdp_url(url, {}) is None
    # The endpoint value must not appear in any log record.
    assert url not in caplog.text


def test_cdp_path_skips_profile_gate_and_drives_cdp(tmp_path: Path) -> None:
    """A resolved CDP URL routes to run_cdp_capture WITHOUT requiring a profile.

    The dedicated profile is intentionally NOT created here: the CDP arm must
    not decline for a missing profile (the live browser is the source).
    """
    storage = tmp_path / "storage_state.json"
    calls: dict[str, Any] = {}

    def _fake_cdp(plan, io, *, cdp_url):
        calls["cdp_url"] = cdp_url
        storage.write_text("{}", encoding="utf-8")
        return None

    result = _attempt(
        storage_path=storage,
        allow_headless=True,
        browser_profile=None,
        cdp_url="http://127.0.0.1:9222",
        env={},
        playwright_installed=True,
        profile_capture=_unexpected_profile_capture,
        cdp_capture=_fake_cdp,
        state=HeadlessReauthState(),
    )

    assert result.status is HeadlessReauthStatus.SUCCESS
    assert calls["cdp_url"] == "http://127.0.0.1:9222"


def test_cdp_url_from_env_routes_to_cdp(tmp_path: Path) -> None:
    storage = tmp_path / "storage_state.json"
    used: dict[str, Any] = {}

    def _fake_cdp(plan, io, *, cdp_url):
        used["cdp_url"] = cdp_url
        storage.write_text("{}", encoding="utf-8")

    result = _attempt(
        storage_path=storage,
        allow_headless=True,
        browser_profile=None,
        env={"NOTEBOOKLM_HEADLESS_REAUTH_CDP_URL": "http://127.0.0.1:9333"},
        playwright_installed=True,
        profile_capture=_unexpected_profile_capture,
        cdp_capture=_fake_cdp,
        state=HeadlessReauthState(),
    )

    assert result.status is HeadlessReauthStatus.SUCCESS
    assert used["cdp_url"] == "http://127.0.0.1:9333"


def test_cdp_off_host_maps_to_failed(tmp_path: Path) -> None:
    """A HeadlessLoginRequiredError from the CDP arm → honest FAILED."""
    storage = tmp_path / "storage_state.json"

    def _fake_cdp(plan, io, *, cdp_url):
        raise HeadlessLoginRequiredError("attached browser cannot reach NotebookLM")

    result = _attempt(
        storage_path=storage,
        allow_headless=True,
        browser_profile=None,
        cdp_url="http://127.0.0.1:9222",
        env={},
        playwright_installed=True,
        profile_capture=_unexpected_profile_capture,
        cdp_capture=_fake_cdp,
        state=HeadlessReauthState(),
    )

    assert result.status is HeadlessReauthStatus.FAILED
    assert "attached browser" in result.reason
    assert not storage.exists()


def test_cdp_opt_in_still_required(tmp_path: Path) -> None:
    """Even with a CDP URL, opt-in is required — never fires by default."""

    result = _attempt(
        storage_path=tmp_path / "storage_state.json",
        allow_headless=False,
        browser_profile=None,
        cdp_url="http://127.0.0.1:9222",
        env={},
        playwright_installed=True,
        profile_capture=_unexpected_profile_capture,
        cdp_capture=_unexpected_cdp_capture,
        state=HeadlessReauthState(),
    )

    assert result.status is HeadlessReauthStatus.UNAVAILABLE
    assert "not enabled" in result.reason


def test_remote_cdp_url_does_not_route_to_cdp(tmp_path: Path) -> None:
    """A remote CDP endpoint must NOT drive the CDP arm (local-only boundary).

    With a remote URL and no reusable profile, the attempt declines as
    UNAVAILABLE (the remote URL was rejected by ``resolve_cdp_url``, then the
    profile arm found no profile) — and ``run_cdp_capture`` is never called.
    """

    result = _attempt(
        storage_path=tmp_path / "storage_state.json",
        allow_headless=True,
        browser_profile=tmp_path / "no_profile",
        env={"NOTEBOOKLM_HEADLESS_REAUTH_CDP_URL": "http://remote-host:9222"},
        playwright_installed=True,
        profile_capture=_unexpected_profile_capture,
        cdp_capture=_unexpected_cdp_capture,
        state=HeadlessReauthState(),
    )

    # Fell through to the profile arm, which declined (no profile) → UNAVAILABLE.
    assert result.status is HeadlessReauthStatus.UNAVAILABLE
    assert "no reusable browser profile" in result.reason


def test_cdp_playwright_missing_is_unavailable(tmp_path: Path) -> None:
    result = _attempt(
        storage_path=tmp_path / "storage_state.json",
        allow_headless=True,
        browser_profile=None,
        cdp_url="http://127.0.0.1:9222",
        env={},
        playwright_installed=False,
        profile_capture=_unexpected_profile_capture,
        cdp_capture=_unexpected_cdp_capture,
        state=HeadlessReauthState(),
    )

    assert result.status is HeadlessReauthStatus.UNAVAILABLE
    assert "playwright is not installed" in result.reason


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
