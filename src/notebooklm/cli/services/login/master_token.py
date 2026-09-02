"""Thin CLI adapter for visible-browser OAuth token capture."""

from __future__ import annotations

from ...._browser.oauth_token import capture_oauth_token as _capture_oauth_token  # noqa: TID252


def capture_oauth_token(
    *,
    browser: str = "chromium",
    cdp_url: str | None = None,
    timeout_s: float = 300.0,
) -> str:
    """Delegate browser-specific capture to the optional implementation package."""
    return _capture_oauth_token(browser=browser, cdp_url=cdp_url, timeout_s=timeout_s)
