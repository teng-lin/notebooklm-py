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

from notebooklm._browser.browser_capture import (
    TARGET_CLOSED_ERROR,
    BrowserCapturePlan,
    _CaptureAbortKind,
    _HeadlessCaptureAbort,
    run_cdp_capture,
)
from notebooklm._env import get_base_url
from notebooklm.exceptions import HeadlessLoginRequiredError


def _landed_on_app() -> str:
    """The URL a healthy session lands on, tracking the configured host.

    Production navigates the captured page to ``get_base_url()``, so the
    simulated landing has to follow it. Pinned to the legacy alias instead,
    every generic-landing test below reached its "authenticated" branch through
    the alias-accept in ``accepted_login_hosts`` — dropping the *configured*
    host from that accept set would not have failed a single one of them. The
    deliberate cross-host case
    (:func:`test_cdp_cross_personal_host_landing_is_authenticated`) still names
    both hosts literally.
    """
    return f"{get_base_url()}/"


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


def _authenticated_cookies() -> list[dict[str, str]]:
    return [
        {"name": "SID", "value": "v", "domain": ".google.com", "path": "/"},
        {"name": "APISID", "value": "a", "domain": ".google.com", "path": "/"},
        {"name": "SAPISID", "value": "s", "domain": ".google.com", "path": "/"},
        {
            "name": "__Secure-1PSIDTS",
            "value": "ts",
            "domain": ".google.com",
            "path": "/",
        },
    ]


# ---------------------------------------------------------------------------
# Authenticated landing → capture + persist (same allowlist as other arms)
# ---------------------------------------------------------------------------


@pytest.mark.requires_playwright
def test_cdp_authenticated_landing_persists_and_filters(tmp_path: Path) -> None:
    cookies = [
        *_authenticated_cookies(),
        # A distinct optional root the domain filter must DROP.
        {"name": "X", "value": "y", "domain": ".youtube.com", "path": "/"},
    ]
    playwright, browser, _context, page = _fake_cdp_browser(_landed_on_app(), cookies=cookies)
    io = _RaisingCaptureIO()

    result = _run_cdp(_plan(tmp_path), io, playwright, "http://127.0.0.1:9222")

    # Attached to the operator-pointed endpoint.
    playwright.chromium.connect_over_cdp.assert_called_once_with("http://127.0.0.1:9222")
    # We navigated our temporary page to the NotebookLM base URL, using an early
    # lifecycle state -- the streaming SPA never fires "load", so the default
    # wait_until would waste 30s then TimeoutError before classification (#1697).
    page.goto.assert_called_once()
    assert page.goto.call_args.kwargs.get("wait_until") in {"commit", "domcontentloaded"}
    # Persisted, with the same domain allowlist (unrequested YouTube dropped).
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
    playwright, browser, context, page = _fake_cdp_browser(
        _landed_on_app(), cookies=_authenticated_cookies()
    )
    io = _RaisingCaptureIO()

    _run_cdp(_plan(tmp_path), io, playwright, "http://127.0.0.1:9222")

    # Reused the existing context (never created a fresh, logged-out one).
    browser.new_context.assert_not_called()
    # Created a TEMPORARY page we own, and closed ONLY it (never the operator's).
    context.new_page.assert_called_once()
    page.close.assert_called_once()
    context.close.assert_not_called()


@pytest.mark.requires_playwright
@pytest.mark.parametrize(
    ("selected", "landed"),
    [
        ("https://notebooklm.google.com", "https://notebook.google.com/"),
        ("https://notebook.google.com", "https://notebooklm.google.com/"),
    ],
)
def test_cdp_cross_personal_host_landing_is_authenticated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, selected: str, landed: str
) -> None:
    """Landing on the *other* personal host is a success, not an off-host miss.

    The CDP arm classifies its landing through ``url_matches_base_host``, so it
    inherits ``accepted_login_hosts``. Google may redirect between the legacy
    host and the post-rebrand alias in either direction; treating that as
    off-host would raise on a perfectly good session.
    """
    monkeypatch.setenv("NOTEBOOKLM_BASE_URL", selected)
    # A complete cookie set: capture validates the captured rows before
    # persisting them (#2061), so a SID-only jar would fail here on the
    # cookie set rather than on the host classification under test.
    playwright, browser, _context, page = _fake_cdp_browser(
        landed, cookies=_authenticated_cookies()
    )
    io = _RaisingCaptureIO()

    result = _run_cdp(_plan(tmp_path), io, playwright, "http://127.0.0.1:9222")

    assert result is not None
    storage = tmp_path / "storage_state.json"
    assert storage.exists()
    assert {c["name"] for c in json.loads(storage.read_text(encoding="utf-8"))["cookies"]} == {
        "SID",
        "APISID",
        "SAPISID",
        "__Secure-1PSIDTS",
    }
    page.close.assert_called_once()
    browser.close.assert_called_once()


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
    playwright, browser, _context, _page = _fake_cdp_browser(_landed_on_app(), has_context=False)
    io = _RaisingCaptureIO()

    with pytest.raises(HeadlessLoginRequiredError, match="no browser"):
        _run_cdp(_plan(tmp_path), io, playwright, "http://127.0.0.1:9222")

    assert not (tmp_path / "storage_state.json").exists()
    # We still disconnected, and never fabricated a context.
    browser.new_context.assert_not_called()
    browser.close.assert_called_once()


@pytest.mark.requires_playwright
def test_cdp_target_closed_is_typed_instead_of_session_expired(tmp_path: Path) -> None:
    """A closed attached browser is infrastructure failure, not a dead session."""
    playwright, browser, _context, page = _fake_cdp_browser(_landed_on_app())
    from playwright.sync_api import Error as PlaywrightError

    page.goto.side_effect = PlaywrightError(TARGET_CLOSED_ERROR)

    with pytest.raises(_HeadlessCaptureAbort) as excinfo:
        _run_cdp(_plan(tmp_path), _RaisingCaptureIO(), playwright, "http://127.0.0.1:9222")

    assert excinfo.value.kind is _CaptureAbortKind.BROWSER_CLOSED
    assert not (tmp_path / "storage_state.json").exists()
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
        {"name": "APISID", "value": "a", "domain": ".google.com", "path": "/"},
        {"name": "SAPISID", "value": "s", "domain": ".google.com", "path": "/"},
        {
            "name": "__Secure-1PSIDTS",
            "value": "ts",
            "domain": ".google.com",
            "path": "/",
        },
    ]
    playwright, _browser, _context, _page = _fake_cdp_browser(_landed_on_app(), cookies=cookies)
    io = _RaisingCaptureIO()

    with caplog.at_level(logging.WARNING):
        _run_cdp(_plan(tmp_path), io, playwright, "http://127.0.0.1:9222")

    # The malformed row WAS flagged...
    assert any("non-str domain" in r.message for r in caplog.records)
    # ...but its value never appears in any log record.
    assert sentinel not in caplog.text
    # ...and it was flagged on the documented operator-facing logger. ADR-0030
    # c-PR5 requires these warnings to reach ``notebooklm.auth`` rather than a
    # private per-module child, and the relocation of this filter into the
    # persistence module made that a live risk: a logger bound to ``__name__``
    # follows its defining module, so a future move would silently re-home these
    # records. Nothing asserted the name before, so such a move would have gone
    # green — this test captured at the root and only ever matched on message
    # text.
    assert all(
        record.name == "notebooklm.auth"
        for record in caplog.records
        if "storage_state cookie" in record.message
    ), "filter warnings must stay on the documented notebooklm.auth logger (ADR-0030)"


def test_safe_cookie_shape_is_value_free() -> None:
    """``_safe_cookie_shape`` summarizes structure with NO values."""
    from notebooklm._browser.browser_capture import _safe_cookie_shape

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
    from notebooklm._browser.browser_capture import _safe_cookie_shape

    shape = _safe_cookie_shape({3: "x", "value": "SECRET"})
    assert "SECRET" not in shape
    assert "3" in shape


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))


# ---------------------------------------------------------------------------
# A failed in-memory heal must never discard the completed sign-in (#2082)
# ---------------------------------------------------------------------------


@pytest.mark.requires_playwright
def test_capture_persists_even_when_the_psidts_heal_declines(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Google withholding ``__Secure-1PSIDTS`` must not cost the user their login.

    The login flow's passive ``goto()`` navigations do not always draw a
    ``Set-Cookie: __Secure-1PSIDTS`` (issue #865), and the in-memory heal cannot
    run at all without a rotatable secondary binding. Before this guard the
    capture raised before ``atomic_write_json``, throwing away a completed SSO
    round-trip and surfacing as a generic "please report a bug". The cookies are
    still the best material available, and the disk-based cold-start recovery
    retries from them on the next command — so persist, and say so.
    """
    playwright, browser, _context, page = _fake_cdp_browser(
        _landed_on_app(),
        cookies=[{"name": "SID", "value": "v", "domain": ".google.com", "path": "/"}],
    )
    io = _RaisingCaptureIO()

    with caplog.at_level("WARNING", logger="notebooklm._browser.browser_capture"):
        result = _run_cdp(_plan(tmp_path), io, playwright, "http://127.0.0.1:9222")

    assert result is not None
    storage = tmp_path / "storage_state.json"
    assert storage.exists(), "a completed sign-in must survive a failed heal"
    assert {c["name"] for c in json.loads(storage.read_text(encoding="utf-8"))["cookies"]} == {
        "SID"
    }
    assert any("__Secure-1PSIDTS" in record.getMessage() for record in caplog.records), (
        "the incomplete state must be reported, not silently persisted"
    )
    page.close.assert_called_once()
    browser.close.assert_called_once()
