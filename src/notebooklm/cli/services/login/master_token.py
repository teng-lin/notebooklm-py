"""Thin CLI adapter for visible-browser OAuth token capture."""

from __future__ import annotations

from ....auth import capture_browser_oauth_token


def capture_oauth_token(
    *,
    browser: str = "chromium",
    cdp_url: str | None = None,
    timeout_s: float = 300.0,
) -> str:
    """Delegate browser-specific capture to the optional implementation package."""
    try:
        return capture_browser_oauth_token(
            browser=browser,
            cdp_url=cdp_url,
            timeout_s=timeout_s,
        )
    finally:
        del browser, cdp_url, timeout_s
