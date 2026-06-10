"""Unit tests for the headless arm of ``run_browser_capture`` (layer-3 re-auth).

Covers the landing-URL classifier and the no-human contract:

* authenticated landing (lands on the NotebookLM host) → capture / filter /
  atomically persist ``storage_state.json`` (reusing the P1 path); NEVER waits
  for a human.
* redirected to the Google login page → raise
  :class:`HeadlessLoginRequiredError` loudly (the profile's session is also
  dead); NEVER hangs on ``wait_for_url``.
* the ``(headless, interactive)`` mode guard: only the two sanctioned arms are
  accepted.

The Playwright context is faked via ``patch("playwright.sync_api.sync_playwright")``
(the same shape the existing login-coverage suite uses), so no real browser /
network is required and ``playwright`` stays lazily imported.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from notebooklm._auth.browser_capture import (
    BrowserCapturePlan,
    _reject_unsupported_mode,
    run_browser_capture,
)
from notebooklm.exceptions import HeadlessLoginRequiredError


class _RaisingCaptureIO:
    """``BrowserCaptureIO`` whose ``fail`` raises (mirrors the headless sink)."""

    def __init__(self) -> None:
        self.emitted: list[Any] = []

    def emit(self, *args: Any, **kwargs: Any) -> None:
        self.emitted.append(args)

    def fail(self, code: int) -> Any:
        raise HeadlessLoginRequiredError(f"io.fail({code})")

    def run_async(self, coro: Any) -> Any:  # pragma: no cover - not reached
        raise AssertionError("run_async not used in the headless arm")


class _FakeSyncPlaywright:
    def __init__(self, playwright: Any) -> None:
        self._playwright = playwright

    def __enter__(self) -> Any:
        return self._playwright

    def __exit__(self, *exc: Any) -> bool:
        return False


def _fake_playwright_landing(url: str, *, cookies: list[dict] | None = None) -> Any:
    """Build a fake playwright whose page lands on ``url``."""
    page = MagicMock()
    page.url = url
    page.goto.return_value = None
    page.content.return_value = "<html></html>"
    context = MagicMock()
    context.pages = [page]
    context.storage_state.return_value = {
        "cookies": cookies if cookies is not None else [],
        "origins": [],
    }
    playwright = MagicMock()
    playwright.chromium.launch_persistent_context.return_value = context
    return playwright, context, page


def _run_headless(plan: BrowserCapturePlan, io: Any, playwright: Any) -> Any:
    with patch(
        "playwright.sync_api.sync_playwright",
        side_effect=lambda: _FakeSyncPlaywright(playwright),
    ):
        return run_browser_capture(plan, io, headless=True, interactive=False)


# ---------------------------------------------------------------------------
# Mode guard
# ---------------------------------------------------------------------------


def test_mode_guard_accepts_both_sanctioned_arms() -> None:
    io = _RaisingCaptureIO()
    # interactive login arm
    _reject_unsupported_mode(headless=False, interactive=True, io=io)
    # headless re-auth arm
    _reject_unsupported_mode(headless=True, interactive=False, io=io)


@pytest.mark.parametrize(
    ("headless", "interactive"),
    [(True, True), (False, False)],
)
def test_mode_guard_rejects_contradictory_combos(headless: bool, interactive: bool) -> None:
    with pytest.raises(NotImplementedError, match="Unsupported browser-capture mode"):
        _reject_unsupported_mode(headless=headless, interactive=interactive, io=_RaisingCaptureIO())


# ---------------------------------------------------------------------------
# Authenticated landing → capture + persist
# ---------------------------------------------------------------------------


@pytest.mark.requires_playwright
def test_headless_authenticated_landing_persists_storage(tmp_path: Path) -> None:
    storage = tmp_path / "storage_state.json"
    profile = tmp_path / "browser_profile"
    profile.mkdir()

    cookies = [
        {"name": "SID", "value": "v", "domain": ".google.com", "path": "/"},
        # A sibling-product cookie that the domain filter must DROP.
        {"name": "X", "value": "y", "domain": "mail.google.com", "path": "/"},
    ]
    playwright, _context, _page = _fake_playwright_landing(
        "https://notebooklm.google.com/", cookies=cookies
    )
    io = _RaisingCaptureIO()

    result = _run_headless(
        BrowserCapturePlan(browser="chromium", browser_profile=profile, storage_path=storage),
        io,
        playwright,
    )

    # Persisted, and the domain allowlist filtered out the mail.google.com row.
    assert storage.exists()
    persisted = json.loads(storage.read_text(encoding="utf-8"))
    names = {c["name"] for c in persisted["cookies"]}
    assert "SID" in names
    assert "X" not in names
    assert result is not None


# ---------------------------------------------------------------------------
# Redirected to login → loud failure, no hang
# ---------------------------------------------------------------------------


@pytest.mark.requires_playwright
def test_headless_redirected_to_login_raises_loudly(tmp_path: Path) -> None:
    storage = tmp_path / "storage_state.json"
    profile = tmp_path / "browser_profile"
    profile.mkdir()

    playwright, _context, page = _fake_playwright_landing(
        "https://accounts.google.com/signin/v2/identifier"
    )
    io = _RaisingCaptureIO()

    with pytest.raises(HeadlessLoginRequiredError, match="session is"):
        _run_headless(
            BrowserCapturePlan(browser="chromium", browser_profile=profile, storage_path=storage),
            io,
            playwright,
        )

    # The headless arm must NOT wait for a human.
    page.wait_for_url.assert_not_called()
    # Nothing was persisted on the dead-session path.
    assert not storage.exists()
