"""Unit tests for the layer-3 headless re-auth decision layer.

Covers :mod:`notebooklm._auth.headless_reauth`:

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

from pathlib import Path

import pytest

from notebooklm._auth import headless_reauth as hr
from notebooklm._auth.headless_reauth import (
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
    freshness skip in ``attempt_headless_reauth`` is what prevents redundant
    browsers. The leader writes the storage file (advancing its mtime); waiting
    followers observe the fresh file and coalesce.
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


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
