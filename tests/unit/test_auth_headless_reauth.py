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
from typing import Any

import pytest

from notebooklm._browser import headless_reauth as hr
from notebooklm._browser.browser_capture import _CaptureAbortKind, _HeadlessCaptureAbort
from notebooklm._browser.headless_reauth import (
    HeadlessReauthResult,
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


def test_optin_off_is_unavailable_and_never_launches(tmp_path: Path, monkeypatch) -> None:
    """No opt-in + no env → UNAVAILABLE; the capture core is never reached.

    This pins the locked design decision: L3 NEVER fires by default.
    """
    profile = _make_profile(tmp_path)

    def _boom(*_a, **_k):  # pragma: no cover - must not be called
        raise AssertionError("run_browser_capture must not be called when opt-in is off")

    monkeypatch.setattr(hr, "run_browser_capture", _boom)

    result = attempt_headless_reauth(
        storage_path=tmp_path / "storage_state.json",
        allow_headless=False,
        browser_profile=profile,
        env={},
    )
    assert result.status is HeadlessReauthStatus.UNAVAILABLE
    assert result.succeeded is False
    assert "not enabled" in result.reason


def test_optin_off_even_with_profile_is_unavailable(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(hr, "run_browser_capture", lambda *a, **k: None)
    result = attempt_headless_reauth(
        storage_path=tmp_path / "storage_state.json",
        allow_headless=False,
        browser_profile=_make_profile(tmp_path),
        env={"NOTEBOOKLM_HEADLESS_REAUTH": "0"},
    )
    assert result.status is HeadlessReauthStatus.UNAVAILABLE


# ---------------------------------------------------------------------------
# Decision matrix: opt-in ON but no reusable profile → UNAVAILABLE
# ---------------------------------------------------------------------------


def test_optin_on_no_profile_dir_is_unavailable(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(hr, "run_browser_capture", lambda *a, **k: None)
    result = attempt_headless_reauth(
        storage_path=tmp_path / "storage_state.json",
        allow_headless=True,
        browser_profile=tmp_path / "does_not_exist",
        env={},
    )
    assert result.status is HeadlessReauthStatus.UNAVAILABLE
    assert "no reusable browser profile" in result.reason


def test_optin_on_empty_profile_dir_is_unavailable(tmp_path: Path, monkeypatch) -> None:
    """A freshly-mkdir'd but empty profile holds no Google session → decline."""
    monkeypatch.setattr(hr, "run_browser_capture", lambda *a, **k: None)
    empty = tmp_path / "browser_profile"
    empty.mkdir()
    result = attempt_headless_reauth(
        storage_path=tmp_path / "storage_state.json",
        allow_headless=True,
        browser_profile=empty,
        env={},
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


def test_success_when_capture_succeeds(tmp_path: Path, monkeypatch) -> None:
    """Capture returns normally → SUCCESS, storage_path carried out."""
    storage = tmp_path / "storage_state.json"
    captured: dict[str, object] = {}

    def _fake_capture(plan, io, *, headless, interactive):
        captured["headless"] = headless
        captured["interactive"] = interactive
        captured["profile"] = plan.browser_profile
        return None

    profile = _make_profile(tmp_path)
    monkeypatch.setattr(hr, "run_browser_capture", _fake_capture)
    # Ensure the playwright-import probe passes by faking it present.
    monkeypatch.setitem(__import__("sys").modules, "playwright", _DummyModule())
    monkeypatch.setitem(__import__("sys").modules, "playwright.sync_api", _DummyModule())

    result = attempt_headless_reauth(
        storage_path=storage,
        allow_headless=True,
        browser_profile=profile,
        env={},
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


def test_failed_when_profile_session_also_dead(tmp_path: Path, monkeypatch) -> None:
    """Headless landed on the Google login page → FAILED, NEVER success."""

    def _redirected(plan, io, *, headless, interactive):
        raise HeadlessLoginRequiredError("redirected to login")

    monkeypatch.setattr(hr, "run_browser_capture", _redirected)
    monkeypatch.setitem(__import__("sys").modules, "playwright", _DummyModule())
    monkeypatch.setitem(__import__("sys").modules, "playwright.sync_api", _DummyModule())

    result = attempt_headless_reauth(
        storage_path=tmp_path / "storage_state.json",
        allow_headless=True,
        browser_profile=_make_profile(tmp_path),
        env={},
    )
    assert result.status is HeadlessReauthStatus.FAILED
    assert result.succeeded is False
    assert result.storage_path is None
    assert "expired" in result.reason


def test_failed_on_unexpected_capture_error(tmp_path: Path, monkeypatch) -> None:
    """An unexpected capture exception → FAILED (best-effort recovery)."""

    def _boom(plan, io, *, headless, interactive):
        raise RuntimeError("launch blew up")

    monkeypatch.setattr(hr, "run_browser_capture", _boom)
    monkeypatch.setitem(__import__("sys").modules, "playwright", _DummyModule())
    monkeypatch.setitem(__import__("sys").modules, "playwright.sync_api", _DummyModule())

    result = attempt_headless_reauth(
        storage_path=tmp_path / "storage_state.json",
        allow_headless=True,
        browser_profile=_make_profile(tmp_path),
        env={},
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
    tmp_path: Path, monkeypatch, kind: _CaptureAbortKind, expected: str
) -> None:
    """Typed capture aborts stay FAILED without masquerading as expired sessions."""

    def _abort(*_args, **_kwargs):
        raise _HeadlessCaptureAbort(kind)

    monkeypatch.setattr(hr, "run_browser_capture", _abort)
    monkeypatch.setitem(__import__("sys").modules, "playwright", _DummyModule())
    monkeypatch.setitem(__import__("sys").modules, "playwright.sync_api", _DummyModule())

    result = attempt_headless_reauth(
        storage_path=tmp_path / "storage_state.json",
        allow_headless=True,
        browser_profile=_make_profile(tmp_path),
        env={},
    )

    assert result.status is HeadlessReauthStatus.FAILED
    assert expected in result.reason
    assert "expired" not in result.reason
    assert "http" not in result.reason
    assert "cookie" not in result.reason


def test_typed_capture_abort_from_cdp_uses_same_safe_mapping(tmp_path: Path, monkeypatch) -> None:
    """The CDP arm shares the profile arm's infrastructure classification."""

    def _abort(*_args, **_kwargs):
        raise _HeadlessCaptureAbort(_CaptureAbortKind.BROWSER_CLOSED)

    monkeypatch.setattr(hr, "run_cdp_capture", _abort)
    monkeypatch.setattr(hr, "_playwright_installed", lambda: True)

    result = attempt_headless_reauth(
        storage_path=tmp_path / "storage_state.json",
        allow_headless=True,
        cdp_url="http://127.0.0.1:9222",
        env={},
    )

    assert result.status is HeadlessReauthStatus.FAILED
    assert result.reason == (
        "headless capture failed: browser was closed during capture; "
        "retry headless re-authentication"
    )


def test_env_optin_drives_browser_without_explicit_flag(tmp_path: Path, monkeypatch) -> None:
    """``NOTEBOOKLM_HEADLESS_REAUTH=1`` enables L3 even with allow_headless=False."""
    monkeypatch.setattr(hr, "run_browser_capture", lambda *a, **k: None)
    monkeypatch.setitem(__import__("sys").modules, "playwright", _DummyModule())
    monkeypatch.setitem(__import__("sys").modules, "playwright.sync_api", _DummyModule())

    result = attempt_headless_reauth(
        storage_path=tmp_path / "storage_state.json",
        allow_headless=False,
        browser_profile=_make_profile(tmp_path),
        env={"NOTEBOOKLM_HEADLESS_REAUTH": "1"},
    )
    assert result.status is HeadlessReauthStatus.SUCCESS


def test_unavailable_when_playwright_missing(tmp_path: Path, monkeypatch) -> None:
    """Opt-in + profile, but the ``browser`` extra is absent → UNAVAILABLE.

    Distinct from FAILED: there is nothing to drive, not a dead session.
    """
    import builtins

    real_import = builtins.__import__

    def _no_playwright(name, *args, **kwargs):
        if name == "playwright.sync_api" or name == "playwright":
            raise ImportError("No module named 'playwright'")
        return real_import(name, *args, **kwargs)

    def _must_not_run(*_a, **_k):  # pragma: no cover
        raise AssertionError("capture must not run when playwright is missing")

    monkeypatch.setattr(hr, "run_browser_capture", _must_not_run)
    monkeypatch.setattr(builtins, "__import__", _no_playwright)

    result = attempt_headless_reauth(
        storage_path=tmp_path / "storage_state.json",
        allow_headless=True,
        browser_profile=_make_profile(tmp_path),
        env={},
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


def test_concurrent_explicit_attempts_coalesce_to_one_browser(tmp_path: Path, monkeypatch) -> None:
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

    monkeypatch.setattr(hr, "run_browser_capture", _slow_capture)
    monkeypatch.setitem(__import__("sys").modules, "playwright", _DummyModule())
    monkeypatch.setitem(__import__("sys").modules, "playwright.sync_api", _DummyModule())

    results: list[HeadlessReauthResult] = []
    results_lock = threading.Lock()

    def _worker() -> None:
        barrier.wait()
        res = attempt_headless_reauth(
            storage_path=storage, allow_headless=True, browser_profile=profile, env={}
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


def test_follower_does_not_report_false_success_when_leader_failed(
    tmp_path: Path, monkeypatch
) -> None:
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
    record = hr._DriveRecord(
        drive_lock=_ContentionSignalLock(),  # type: ignore[arg-type]
        _state_lock=threading.Lock(),
    )
    monkeypatch.setattr(hr, "_get_drive_record", lambda _p, *, source: record)
    monkeypatch.setitem(__import__("sys").modules, "playwright", _DummyModule())
    monkeypatch.setitem(__import__("sys").modules, "playwright.sync_api", _DummyModule())

    leader_in_capture = threading.Event()
    leader_may_finish = threading.Event()

    def _leader_capture(plan, io, *, headless, interactive):
        leader_in_capture.set()
        assert leader_may_finish.wait(timeout=5)
        # The leader's re-mint FAILS: the profile's Google session is dead.
        raise HeadlessLoginRequiredError("profile session is dead")

    monkeypatch.setattr(hr, "run_browser_capture", _leader_capture)

    results: dict[str, HeadlessReauthResult] = {}

    def _run(tag: str) -> None:
        results[tag] = attempt_headless_reauth(
            storage_path=storage, allow_headless=True, browser_profile=profile, env={}
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


def test_stale_outcome_from_previous_cycle_is_not_coalesced(tmp_path: Path, monkeypatch) -> None:
    """A solo follower after a past drive treats the old outcome as 'no outcome'.

    A caller that arrives when NO drive is active during its wait must not
    coalesce onto the outcome of a *previous* drive cycle (whose cookies may
    have re-died since) — it drives its own browser. This pins the
    strictly-newer-sequence discipline (``completed > pre``, not ``>=``).
    """
    storage = tmp_path / "storage_state.json"
    profile = _make_profile(tmp_path)
    monkeypatch.setitem(__import__("sys").modules, "playwright", _DummyModule())
    monkeypatch.setitem(__import__("sys").modules, "playwright.sync_api", _DummyModule())

    drives = {"count": 0}

    def _capture(plan, io, *, headless, interactive):
        drives["count"] += 1
        plan.storage_path.write_text('{"cookies": [], "origins": []}', encoding="utf-8")

    monkeypatch.setattr(hr, "run_browser_capture", _capture)

    # First (completed) drive cycle publishes a SUCCESS outcome at sequence 1.
    first = attempt_headless_reauth(
        storage_path=storage, allow_headless=True, browser_profile=profile, env={}
    )
    assert first.status is HeadlessReauthStatus.SUCCESS
    assert drives["count"] == 1

    # A later, solo caller must NOT coalesce onto that stale outcome; it drives.
    second = attempt_headless_reauth(
        storage_path=storage, allow_headless=True, browser_profile=profile, env={}
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
                sf.claim(("path", "profile"), _never_runs)
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

    profile_record = hr._get_drive_record(storage, source="profile")
    cdp_record = hr._get_drive_record(storage, source="cdp")

    # Same storage file, different credential source → different records, so a
    # dead profile's FAILED can never be handed to a live CDP attach.
    assert profile_record is not cdp_record
    assert hr._get_drive_record(storage, source="profile") is profile_record

    # The source is only knowable after ``resolve_cdp_url`` runs, and that call
    # is the loopback gate: a remote endpoint resolves to ``None`` (→ the
    # "profile" source), so the key and the security boundary are one decision.
    assert hr.resolve_cdp_url("http://127.0.0.1:9222", {}) == "http://127.0.0.1:9222"
    assert hr.resolve_cdp_url("http://remote-host:9222", {}) is None


class _DummyModule:
    """Stand-in for the ``playwright`` / ``playwright.sync_api`` modules.

    Only used to satisfy the function-local ``import playwright.sync_api``
    availability probe in :func:`attempt_headless_reauth` without installing
    the real extra; the actual capture is faked via ``run_browser_capture``.
    """


# ---------------------------------------------------------------------------
# Readiness probe (doctor diagnostics): credential-free, launches nothing
# ---------------------------------------------------------------------------


def test_readiness_ready_when_profile_present_and_playwright(tmp_path: Path, monkeypatch) -> None:
    """Profile present + playwright importable → available, ready detail."""
    profile = _make_profile(tmp_path)
    monkeypatch.setattr(hr, "_playwright_installed", lambda: True)

    readiness = hr.headless_reauth_readiness(browser_profile=profile)

    assert readiness.profile_present is True
    assert readiness.playwright_installed is True
    assert readiness.available is True
    assert "ready" in readiness.detail
    assert "NOTEBOOKLM_HEADLESS_REAUTH" in readiness.detail


def test_readiness_unavailable_without_profile(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(hr, "_playwright_installed", lambda: True)

    readiness = hr.headless_reauth_readiness(browser_profile=tmp_path / "nope")

    assert readiness.profile_present is False
    assert readiness.available is False
    assert "no reusable browser profile" in readiness.detail


def test_readiness_unavailable_without_playwright(tmp_path: Path, monkeypatch) -> None:
    profile = _make_profile(tmp_path)
    monkeypatch.setattr(hr, "_playwright_installed", lambda: False)

    readiness = hr.headless_reauth_readiness(browser_profile=profile)

    assert readiness.profile_present is True
    assert readiness.playwright_installed is False
    assert readiness.available is False
    assert "playwright not installed" in readiness.detail


def test_readiness_reports_both_missing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(hr, "_playwright_installed", lambda: False)

    readiness = hr.headless_reauth_readiness(browser_profile=tmp_path / "nope")

    assert readiness.available is False
    assert "no reusable browser profile" in readiness.detail
    assert "playwright not installed" in readiness.detail


def test_readiness_never_drives_a_browser(tmp_path: Path, monkeypatch) -> None:
    """The readiness probe must never launch the capture core."""

    def _boom(*_a, **_k):  # pragma: no cover - must not be called
        raise AssertionError("headless_reauth_readiness must not drive a browser")

    monkeypatch.setattr(hr, "run_browser_capture", _boom)
    monkeypatch.setattr(hr, "_playwright_installed", lambda: True)

    readiness = hr.headless_reauth_readiness(browser_profile=_make_profile(tmp_path))
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


def test_cdp_path_skips_profile_gate_and_drives_cdp(tmp_path: Path, monkeypatch) -> None:
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

    def _no_launch(*_a, **_k):  # pragma: no cover - must not be called
        raise AssertionError("the CDP arm must not launch the dedicated profile")

    monkeypatch.setattr(hr, "run_cdp_capture", _fake_cdp)
    monkeypatch.setattr(hr, "run_browser_capture", _no_launch)
    monkeypatch.setattr(hr, "_playwright_installed", lambda: True)

    result = attempt_headless_reauth(
        storage_path=storage,
        allow_headless=True,
        cdp_url="http://127.0.0.1:9222",
        env={},
    )

    assert result.status is HeadlessReauthStatus.SUCCESS
    assert calls["cdp_url"] == "http://127.0.0.1:9222"


def test_cdp_url_from_env_routes_to_cdp(tmp_path: Path, monkeypatch) -> None:
    storage = tmp_path / "storage_state.json"
    used: dict[str, Any] = {}

    def _fake_cdp(plan, io, *, cdp_url):
        used["cdp_url"] = cdp_url
        storage.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(hr, "run_cdp_capture", _fake_cdp)
    monkeypatch.setattr(hr, "_playwright_installed", lambda: True)

    result = attempt_headless_reauth(
        storage_path=storage,
        allow_headless=True,
        env={"NOTEBOOKLM_HEADLESS_REAUTH_CDP_URL": "http://127.0.0.1:9333"},
    )

    assert result.status is HeadlessReauthStatus.SUCCESS
    assert used["cdp_url"] == "http://127.0.0.1:9333"


def test_cdp_off_host_maps_to_failed(tmp_path: Path, monkeypatch) -> None:
    """A HeadlessLoginRequiredError from the CDP arm → honest FAILED."""
    storage = tmp_path / "storage_state.json"

    def _fake_cdp(plan, io, *, cdp_url):
        raise HeadlessLoginRequiredError("attached browser cannot reach NotebookLM")

    monkeypatch.setattr(hr, "run_cdp_capture", _fake_cdp)
    monkeypatch.setattr(hr, "_playwright_installed", lambda: True)

    result = attempt_headless_reauth(
        storage_path=storage,
        allow_headless=True,
        cdp_url="http://127.0.0.1:9222",
        env={},
    )

    assert result.status is HeadlessReauthStatus.FAILED
    assert "attached browser" in result.reason
    assert not storage.exists()


def test_cdp_opt_in_still_required(tmp_path: Path, monkeypatch) -> None:
    """Even with a CDP URL, opt-in is required — never fires by default."""

    def _boom(*_a, **_k):  # pragma: no cover - must not be called
        raise AssertionError("CDP arm must not run without opt-in")

    monkeypatch.setattr(hr, "run_cdp_capture", _boom)
    monkeypatch.setattr(hr, "_playwright_installed", lambda: True)

    result = attempt_headless_reauth(
        storage_path=tmp_path / "storage_state.json",
        allow_headless=False,
        cdp_url="http://127.0.0.1:9222",
        env={},
    )

    assert result.status is HeadlessReauthStatus.UNAVAILABLE
    assert "not enabled" in result.reason


def test_remote_cdp_url_does_not_route_to_cdp(tmp_path: Path, monkeypatch) -> None:
    """A remote CDP endpoint must NOT drive the CDP arm (local-only boundary).

    With a remote URL and no reusable profile, the attempt declines as
    UNAVAILABLE (the remote URL was rejected by ``resolve_cdp_url``, then the
    profile arm found no profile) — and ``run_cdp_capture`` is never called.
    """

    def _boom(*_a, **_k):  # pragma: no cover - must not be called
        raise AssertionError("a remote CDP endpoint must never reach run_cdp_capture")

    monkeypatch.setattr(hr, "run_cdp_capture", _boom)
    monkeypatch.setattr(hr, "run_browser_capture", lambda *a, **k: None)
    monkeypatch.setattr(hr, "_playwright_installed", lambda: True)

    result = attempt_headless_reauth(
        storage_path=tmp_path / "storage_state.json",
        allow_headless=True,
        browser_profile=tmp_path / "no_profile",
        env={"NOTEBOOKLM_HEADLESS_REAUTH_CDP_URL": "http://remote-host:9222"},
    )

    # Fell through to the profile arm, which declined (no profile) → UNAVAILABLE.
    assert result.status is HeadlessReauthStatus.UNAVAILABLE
    assert "no reusable browser profile" in result.reason


def test_cdp_playwright_missing_is_unavailable(tmp_path: Path, monkeypatch) -> None:
    def _boom(*_a, **_k):  # pragma: no cover - must not be called
        raise AssertionError("CDP arm must not run without playwright")

    monkeypatch.setattr(hr, "run_cdp_capture", _boom)
    monkeypatch.setattr(hr, "_playwright_installed", lambda: False)

    result = attempt_headless_reauth(
        storage_path=tmp_path / "storage_state.json",
        allow_headless=True,
        cdp_url="http://127.0.0.1:9222",
        env={},
    )

    assert result.status is HeadlessReauthStatus.UNAVAILABLE
    assert "playwright is not installed" in result.reason


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
