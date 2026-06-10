"""Unit tests for the CDP-attach arm of browser capture (``run_cdp_capture``).

An alternative credential source for layer-3 re-auth: instead of launching the
dedicated persistent profile, attach to an operator-pointed already-running
Chrome over the Chrome DevTools Protocol. The motivation is freshness — the
operator's daily Chrome is continuously Google-refreshed where our dedicated
profile can go stale in the long-idle case.

Covers:

* authenticated landing (lands on the NotebookLM host) → capture / filter /
  atomically persist ``storage_state.json`` (the SAME path the other arms use);
* redirected off-host → raise :class:`HeadlessLoginRequiredError` loudly (the
  attached browser's session cannot reach NotebookLM); NEVER persists;
* lifecycle: teardown only DISCONNECTS (``browser.close()``) and never closes
  the operator's context;
* the same cookie-domain allowlist applies, so the on-disk state is equivalent
  regardless of the credential source.

The Playwright client is faked via ``patch("playwright.sync_api.sync_playwright")``
so no real browser / network is required and ``playwright`` stays lazily
imported.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from notebooklm._auth.browser_capture import BrowserCapturePlan, run_cdp_capture
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
        raise AssertionError("run_async not used in the CDP arm")


class _FakeSyncPlaywright:
    def __init__(self, playwright: Any) -> None:
        self._playwright = playwright

    def __enter__(self) -> Any:
        return self._playwright

    def __exit__(self, *exc: Any) -> bool:
        return False


def _fake_cdp_browser(
    url: str,
    *,
    cookies: list[dict] | None = None,
    has_context: bool = True,
) -> tuple[Any, Any, Any, Any]:
    """Build a fake playwright whose CDP-attached browser lands on ``url``.

    Returns ``(playwright, browser, context, page)`` where ``page`` is the
    TEMPORARY page the capture creates via ``context.new_page()`` — so tests can
    assert the temporary-page lifecycle and the disconnect-only teardown. The
    operator's own pre-existing pages are deliberately NOT modeled as the
    captured page: the capture must never navigate them.
    """
    page = MagicMock()
    page.url = url
    page.goto.return_value = None
    page.content.return_value = "<html></html>"
    context = MagicMock()
    context.new_page.return_value = page
    context.storage_state.return_value = {
        "cookies": cookies if cookies is not None else [],
        "origins": [],
    }
    browser = MagicMock()
    browser.contexts = [context] if has_context else []
    playwright = MagicMock()
    playwright.chromium.connect_over_cdp.return_value = browser
    return playwright, browser, context, page


def _run_cdp(plan: BrowserCapturePlan, io: Any, playwright: Any, cdp_url: str) -> Any:
    with patch(
        "playwright.sync_api.sync_playwright",
        side_effect=lambda: _FakeSyncPlaywright(playwright),
    ):
        return run_cdp_capture(plan, io, cdp_url=cdp_url)


def _plan(tmp_path: Path) -> BrowserCapturePlan:
    return BrowserCapturePlan(
        browser="chromium",
        browser_profile=tmp_path,  # ignored on the CDP arm
        storage_path=tmp_path / "storage_state.json",
    )


# ---------------------------------------------------------------------------
# Authenticated landing → capture + persist (same allowlist as other arms)
# ---------------------------------------------------------------------------


@pytest.mark.requires_playwright
def test_cdp_authenticated_landing_persists_and_filters(tmp_path: Path) -> None:
    cookies = [
        {"name": "SID", "value": "v", "domain": ".google.com", "path": "/"},
        # A sibling-product cookie the domain filter must DROP.
        {"name": "X", "value": "y", "domain": "mail.google.com", "path": "/"},
    ]
    playwright, browser, _context, page = _fake_cdp_browser(
        "https://notebooklm.google.com/", cookies=cookies
    )
    io = _RaisingCaptureIO()

    result = _run_cdp(_plan(tmp_path), io, playwright, "http://127.0.0.1:9222")

    # Attached to the operator-pointed endpoint.
    playwright.chromium.connect_over_cdp.assert_called_once_with("http://127.0.0.1:9222")
    # We navigated our temporary page to the NotebookLM base URL.
    page.goto.assert_called_once()
    # Persisted, with the same domain allowlist (mail.google.com dropped).
    storage = tmp_path / "storage_state.json"
    assert storage.exists()
    persisted = json.loads(storage.read_text(encoding="utf-8"))
    names = {c["name"] for c in persisted["cookies"]}
    assert "SID" in names
    assert "X" not in names
    assert result is not None
    # Teardown DISCONNECTS the client (never kills the operator's Chrome).
    browser.close.assert_called_once()


@pytest.mark.requires_playwright
def test_cdp_uses_temporary_page_in_existing_context(tmp_path: Path) -> None:
    """Reuse the operator's EXISTING context but navigate/close our OWN page."""
    playwright, browser, context, page = _fake_cdp_browser("https://notebooklm.google.com/")
    io = _RaisingCaptureIO()

    _run_cdp(_plan(tmp_path), io, playwright, "http://127.0.0.1:9222")

    # Reused the existing context (never created a fresh, logged-out one).
    browser.new_context.assert_not_called()
    # Created a TEMPORARY page we own, and closed ONLY it (never the operator's).
    context.new_page.assert_called_once()
    page.close.assert_called_once()
    context.close.assert_not_called()


# ---------------------------------------------------------------------------
# Redirected off-host → loud failure, nothing persisted
# ---------------------------------------------------------------------------


@pytest.mark.requires_playwright
def test_cdp_off_host_landing_raises_loudly_and_persists_nothing(tmp_path: Path) -> None:
    playwright, browser, _context, page = _fake_cdp_browser(
        "https://accounts.google.com/signin/v2/identifier"
    )
    io = _RaisingCaptureIO()

    with pytest.raises(HeadlessLoginRequiredError, match="cannot reach NotebookLM"):
        _run_cdp(_plan(tmp_path), io, playwright, "http://127.0.0.1:9222")

    # Same security boundary as the headless arm: nothing persisted on a dead
    # session; the temporary page is closed and the client disconnected.
    assert not (tmp_path / "storage_state.json").exists()
    page.close.assert_called_once()
    browser.close.assert_called_once()


# ---------------------------------------------------------------------------
# No context to harvest → fail loudly (never fabricate a logged-out context)
# ---------------------------------------------------------------------------


@pytest.mark.requires_playwright
def test_cdp_no_context_raises_and_persists_nothing(tmp_path: Path) -> None:
    """An attached browser with no context cannot supply a session → raise."""
    playwright, browser, _context, _page = _fake_cdp_browser(
        "https://notebooklm.google.com/", has_context=False
    )
    io = _RaisingCaptureIO()

    with pytest.raises(HeadlessLoginRequiredError, match="no browser"):
        _run_cdp(_plan(tmp_path), io, playwright, "http://127.0.0.1:9222")

    assert not (tmp_path / "storage_state.json").exists()
    # We still disconnected, and never fabricated a context.
    browser.new_context.assert_not_called()
    browser.close.assert_called_once()


# ---------------------------------------------------------------------------
# Cookie-value redaction: malformed live-browser cookies must not leak values
# ---------------------------------------------------------------------------


@pytest.mark.requires_playwright
def test_cdp_malformed_cookie_value_never_logged(tmp_path: Path, caplog) -> None:
    """Malformed cookies from the live browser must not leak their value to logs.

    The CDP arm feeds ``context.storage_state()`` from the operator's running
    Chrome through the shared domain filter, whose malformed-row warnings must
    log only a value-free shape — never the cookie ``value`` (a live
    credential).
    """
    import logging

    sentinel = "SUPER_SECRET_COOKIE_VALUE_4f2a"
    cookies = [
        # Malformed: non-str domain — triggers the "non-str domain" warning,
        # which must NOT echo the value.
        {"name": "bad", "value": sentinel, "domain": 12345, "path": "/"},
        # A valid allowed cookie so the capture still persists something.
        {"name": "SID", "value": "ok", "domain": ".google.com", "path": "/"},
    ]
    playwright, _browser, _context, _page = _fake_cdp_browser(
        "https://notebooklm.google.com/", cookies=cookies
    )
    io = _RaisingCaptureIO()

    with caplog.at_level(logging.WARNING):
        _run_cdp(_plan(tmp_path), io, playwright, "http://127.0.0.1:9222")

    # The malformed row WAS flagged...
    assert any("non-str domain" in r.message for r in caplog.records)
    # ...but its value never appears in any log record.
    assert sentinel not in caplog.text


def test_safe_cookie_shape_is_value_free() -> None:
    """``_safe_cookie_shape`` summarizes structure with NO values."""
    from notebooklm._auth.browser_capture import _safe_cookie_shape

    shape = _safe_cookie_shape({"name": "SID", "value": "SECRET", "domain": 5})
    assert "SECRET" not in shape
    # Keys and per-field types are present.
    assert "name" in shape and "value" in shape and "domain" in shape
    assert "int" in shape  # domain's type


def test_safe_cookie_shape_tolerates_non_str_keys() -> None:
    """A malformed cookie with a non-str key must not raise KeyError.

    This helper exists to *describe* malformed rows, so it must never itself
    choke on one (regression for a ``cookie[str(k)]`` re-subscript bug).
    """
    from notebooklm._auth.browser_capture import _safe_cookie_shape

    shape = _safe_cookie_shape({3: "x", "value": "SECRET"})
    assert "SECRET" not in shape
    assert "3" in shape


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
