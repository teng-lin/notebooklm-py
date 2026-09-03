"""Visible-browser capture of Google's single-use ``oauth_token`` cookie.

Interactive-browser capture of the single-use ``oauth_token`` cookie Directive
B needs. This implementation lives with the other optional browser acquisition
code; callers receive only the captured token string or a canonical
``MasterTokenError``.
"""

from __future__ import annotations

from ipaddress import ip_address
from urllib.parse import SplitResult, urlsplit

from .._auth.master_token_types import MasterTokenError

# ADR-0034 Phase 11D: ``MasterTokenBootstrapper`` now owns that coordination;
# ``_auth.master_token`` retains the exact v0.x transaction adapters.
# ``classify_launch_failure`` remains owned by the sibling
# ``browser_launch_errors`` leaf and is re-exported through ``browser_capture``
# for private import continuity. CLI callers reach this module only through the
# public auth capability facade.
from .browser_capture import classify_launch_failure, sync_playwright_context

_EMBEDDED_SETUP_URL = "https://accounts.google.com/EmbeddedSetup"


def _is_loopback_host(host: str | None) -> bool:
    """Return whether a parsed CDP host stays on the operator's machine."""
    if host is None:
        return False
    if host.casefold() == "localhost":
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


def capture_oauth_token(
    *, browser: str = "chromium", cdp_url: str | None = None, timeout_s: float = 300.0
) -> str:
    """Directive B: open a *visible* browser at Google's EmbeddedSetup, let the
    user sign in, and scrape the single-use ``oauth_token`` cookie. No unattended
    headless Google login (anti-bot) — the user completes auth interactively.

    Requires the ``[browser]`` extra. Attaches to a running Chrome via ``cdp_url``
    when given, else launches a headed Playwright browser."""
    parsed_cdp: SplitResult | None = None
    rejected_cdp = False
    playwright_driver = None
    browser_obj = None
    context = None
    page = None
    cookie = None
    token = ""
    deadline = 0.0
    channel = None
    launch_help = None
    owns_browser = False
    owns_context = False
    try:
        try:
            import playwright.sync_api  # noqa: F401, PLC0415
        except ImportError as exc:  # pragma: no cover - import guard
            raise MasterTokenError(
                "Browser-assisted oauth_token capture needs the [browser] extra "
                "(pip install 'notebooklm-py[browser]'), or pass --oauth-token manually."
            ) from exc

        if cdp_url:
            try:
                parsed_cdp = urlsplit(cdp_url)
            except ValueError:
                rejected_cdp = True
            else:
                rejected_cdp = (
                    parsed_cdp.username is not None
                    or parsed_cdp.password is not None
                    or bool(parsed_cdp.query)
                    or bool(parsed_cdp.fragment)
                    or not _is_loopback_host(parsed_cdp.hostname)
                )
            if rejected_cdp:
                raise MasterTokenError(
                    "CDP URL must be a credential-free loopback scheme/host/path endpoint "
                    "without userinfo, query, or fragment."
                )

        with sync_playwright_context() as playwright_driver:
            # Track what WE created so teardown never closes the user's own
            # browser/context while still settling every C-created resource.
            try:
                if cdp_url:
                    browser_obj = playwright_driver.chromium.connect_over_cdp(cdp_url)
                    if browser_obj.contexts:
                        context = browser_obj.contexts[0]
                    else:
                        context = browser_obj.new_context()
                        owns_context = True
                else:
                    channel = browser if browser and browser != "chromium" else None
                    try:
                        browser_obj = playwright_driver.chromium.launch(
                            headless=False,
                            channel=channel,
                            args=["--disable-blink-features=AutomationControlled"],
                            ignore_default_args=["--enable-automation"],
                        )
                    except Exception as exc:
                        launch_help = classify_launch_failure(browser, str(exc))
                        if launch_help is None:
                            raise
                        raise MasterTokenError(launch_help) from exc
                    owns_browser = True
                    context = browser_obj.new_context()
                    owns_context = True

                page = context.new_page()
                page.goto(_EMBEDDED_SETUP_URL)
                deadline = page.evaluate("Date.now()") + timeout_s * 1000
                while page.evaluate("Date.now()") < deadline:
                    for cookie in context.cookies():
                        if cookie.get("name") == "oauth_token" and cookie.get("value"):
                            token = cookie["value"]
                            break
                    if token:
                        break
                    page.wait_for_timeout(1000)
            finally:
                if page is not None:
                    page.close()
                if owns_context and context is not None:
                    context.close()
                if owns_browser and browser_obj is not None:
                    browser_obj.close()

        if not token:
            raise MasterTokenError(
                "Did not observe an oauth_token cookie. If Google showed 'This browser "
                "or app may not be secure', it blocked the automated browser — attach "
                "to your own Chrome with --cdp-url (launch it with "
                "--remote-debugging-port=9222), or sign in manually and pass the "
                "oauth_token cookie via --oauth-token. Otherwise complete sign-in at "
                "accounts.google.com/EmbeddedSetup, then retry."
            )
        return token
    finally:
        del parsed_cdp
        del rejected_cdp
        del playwright_driver
        del browser_obj
        del context
        del page
        del cookie
        del token
        del deadline
        del channel
        del launch_help
        del owns_browser
        del owns_context
        del browser
        del cdp_url
        del timeout_s
