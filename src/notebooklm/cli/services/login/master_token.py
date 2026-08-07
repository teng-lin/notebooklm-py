"""Master-token oauth_token capture (Click-free, per ADR-0015).

Interactive-browser capture of the single-use ``oauth_token`` cookie Directive
B needs. #2103 PR-2 structural follow-up relocated the rest of the master-token
transaction (bootstrap / re-mint / ownership guard) into
:mod:`notebooklm._auth.master_token`. The CLI driver
(``cli/master_token_login.py``) invokes those whole transactions through the
public ``notebooklm.auth`` facade (``master_token_bootstrap`` /
``master_token_remint`` / ``assert_account_writable``) and never assembles
minting primitives itself; this module (not the driver) keeps ONLY the piece
that must stay CLI-side: launching a real, visible browser is inherently
interactive.
"""

from __future__ import annotations

# The single ``_auth`` module the CLI-boundary guardrail sanctions;
# ``classify_launch_failure`` lives in its ``browser_launch_errors`` leaf and
# is re-exported there.
from ...._auth.browser_capture import (  # noqa: TID252
    classify_launch_failure,
    sync_playwright_context,
)
from ....auth import MasterTokenError  # noqa: TID252 (package-relative; public boundary)

_EMBEDDED_SETUP_URL = "https://accounts.google.com/EmbeddedSetup"


def capture_oauth_token(
    *, browser: str = "chromium", cdp_url: str | None = None, timeout_s: float = 300.0
) -> str:
    """Directive B: open a *visible* browser at Google's EmbeddedSetup, let the
    user sign in, and scrape the single-use ``oauth_token`` cookie. No unattended
    headless Google login (anti-bot) — the user completes auth interactively.

    Requires the ``[browser]`` extra. Attaches to a running Chrome via ``cdp_url``
    when given, else launches a headed Playwright browser."""
    try:
        import playwright.sync_api  # noqa: F401, PLC0415
    except ImportError as exc:  # pragma: no cover - import guard
        raise MasterTokenError(
            "Browser-assisted oauth_token capture needs the [browser] extra "
            "(pip install 'notebooklm-py[browser]'), or pass --oauth-token manually."
        ) from exc

    with sync_playwright_context() as p:
        # Track what WE created so teardown never closes the user's own browser/
        # context (CDP adopts the user's live session) and always runs on error.
        owns_browser = owns_context = False
        if cdp_url:
            browser_obj = p.chromium.connect_over_cdp(cdp_url)
            if browser_obj.contexts:
                context = browser_obj.contexts[0]
            else:
                context = browser_obj.new_context()
                owns_context = True
        else:
            # Respect --browser: "chromium" is the bundled build; "chrome"/"msedge"
            # are system Chromium channels (the documented macOS-15-crash workaround).
            # channel=None selects the bundled Chromium.
            channel = browser if browser and browser != "chromium" else None
            # Google refuses sign-in in browsers that advertise automation ("This
            # browser or app may not be secure"). Drop the --enable-automation
            # banner and the AutomationControlled blink feature so
            # navigator.webdriver is false. This is the minimal de-automation, not
            # a stealth library (rejected — see auth-cookie-lifecycle.md §7); if
            # Google still blocks, use --cdp-url (your own Chrome) or --oauth-token.
            try:
                browser_obj = p.chromium.launch(
                    headless=False,
                    channel=channel,
                    args=["--disable-blink-features=AutomationControlled"],
                    ignore_default_args=["--enable-automation"],
                )
            except Exception as exc:
                # This bootstrap is NOT browser-free — it spawns a headed
                # browser exactly like ``notebooklm login`` — so it hits the
                # same launch vetoes (#2004). Reuse the shared classifier and
                # surface a MasterTokenError (red message, exit 1) instead of
                # letting a raw Playwright error reach the "This may be a bug"
                # handler. Unclassified failures re-raise unchanged.
                launch_help = classify_launch_failure(browser, str(exc))
                if launch_help is None:
                    raise
                raise MasterTokenError(launch_help) from exc
            owns_browser = True
            context = browser_obj.new_context()
            owns_context = True
        page = context.new_page()
        try:
            page.goto(_EMBEDDED_SETUP_URL)
            # Poll the context's cookie jar for oauth_token until present/timeout.
            deadline = page.evaluate("Date.now()") + timeout_s * 1000
            token = ""
            while page.evaluate("Date.now()") < deadline:
                for c in context.cookies():
                    if c.get("name") == "oauth_token" and c.get("value"):
                        token = c["value"]
                        break
                if token:
                    break
                page.wait_for_timeout(1000)
        finally:
            page.close()  # always close the page WE created
            if owns_context:
                context.close()
            if owns_browser:
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
