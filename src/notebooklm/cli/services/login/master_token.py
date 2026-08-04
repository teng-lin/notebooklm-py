"""Master-token login service (Click-free, per ADR-0015).

Bootstrap and refresh of headless master-token auth: obtain/persist the durable
``aas_et/`` master token, mint web cookies from it, and write them to the
profile's ``storage_state.json`` so the existing client runs unchanged.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

# Both via ``browser_capture``, the single ``_auth`` module the CLI-boundary
# guardrail sanctions; ``classify_launch_failure`` lives in its
# ``browser_launch_errors`` leaf and is re-exported there.
from ...._auth.browser_capture import (  # noqa: TID252
    classify_launch_failure,
    sync_playwright_context,
)
from ....auth import (  # noqa: TID252 (package-relative; public boundary, not _auth.*)
    MasterTokenError,
    exchange_master_token,
    get_account_email_for_storage,
    mint_cookies,
    persist_minted_jar,
    read_master_token,
    write_master_token,
)

_EMBEDDED_SETUP_URL = "https://accounts.google.com/EmbeddedSetup"


async def _verify(storage_path: Path) -> int:
    """Smoke-test the minted session: list notebooks. Returns the count."""
    from ....client import NotebookLMClient  # noqa: PLC0415 (avoid import cycle)

    async with NotebookLMClient.from_storage(path=str(storage_path)) as client:
        return len(await client.notebooks.list())


def assert_account_writable(
    *, email: str, storage_path: Path, master_token_path: Path, force: bool = False
) -> None:
    """Refuse to overwrite a profile that already belongs to a *different* Google
    account, unless ``force``. ``--account`` selects the account to mint; the
    profile selects where it lands — minting account B into account A's profile
    silently clobbers A's cookies *and* durable master token. Checks BOTH the
    stored session and the existing ``master_token.json`` owner, since either can
    be present without the other (e.g. a stale storage file beside a token for a
    different account). Called early (before capture) so a wrong profile fails
    fast."""
    if force:
        return
    try:
        token_rec = read_master_token(master_token_path)
    except MasterTokenError:
        token_rec = None  # malformed token will be re-bootstrapped; not an owner signal
    owners = {
        owner.strip()
        for owner in (get_account_email_for_storage(storage_path), (token_rec or {}).get("email"))
        if isinstance(owner, str) and owner.strip()
    }
    conflict = next((o for o in owners if o.casefold() != email.casefold()), None)
    if conflict:
        raise MasterTokenError(
            f"This profile already belongs to {conflict}, but --account is {email}. "
            f"Minting here would overwrite {conflict}'s session and master token. "
            "Use a dedicated profile (e.g. `notebooklm -p <name> login --master-token "
            f"--account {email}`), or pass --force to overwrite this one."
        )


async def bootstrap(
    *,
    email: str,
    oauth_token: str,
    android_id: str,
    storage_path: Path,
    master_token_path: Path,
    verify: bool = True,
    force: bool = False,
) -> int:
    """One-time: exchange the single-use ``oauth_token`` for a durable master
    token, persist it (0600), mint cookies, write ``storage_state.json``, and
    (optionally) verify by listing notebooks. Returns the notebook count (or -1
    when verify is False). Raises :class:`MasterTokenError` on rejection.

    Refuses to overwrite a profile that already belongs to a *different* account
    (``--account`` mismatch) unless ``force`` — minting writes a full session +
    durable token into the profile, so a wrong profile silently clobbers it."""
    await asyncio.to_thread(
        assert_account_writable,
        email=email,
        storage_path=storage_path,
        master_token_path=master_token_path,
        force=force,
    )
    # exchange/write/persist are sync (network + locked file I/O) — off-thread so
    # they don't block the event loop the CLI runs them on.
    token = await asyncio.to_thread(exchange_master_token, email, oauth_token, android_id)
    await asyncio.to_thread(
        write_master_token,
        master_token_path,
        email=email,
        master_token=token,
        android_id=android_id,
    )
    jar = await mint_cookies(email, token, android_id)
    await asyncio.to_thread(persist_minted_jar, storage_path, jar, email=email)
    return await _verify(storage_path) if verify else -1


async def refresh(*, storage_path: Path, master_token_path: Path) -> None:
    """No-prompt re-mint from the stored master token (recovery / hand-run).
    Overwrites ``storage_state.json`` with a fresh session."""
    rec = await asyncio.to_thread(read_master_token, master_token_path)
    if rec is None:
        raise MasterTokenError(
            f"No master token at {master_token_path}. Run `notebooklm login --master-token` first."
        )
    jar = await mint_cookies(rec["email"], rec["master_token"], rec["android_id"])
    await asyncio.to_thread(persist_minted_jar, storage_path, jar, email=rec.get("email"))


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
