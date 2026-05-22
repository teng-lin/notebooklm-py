"""Shared cookie-jar enumeration helper.

Contains :func:`_enumerate_one_jar` — probes one rookiepy cookie set
against ``?authuser=N`` to return tagged :class:`Account` records. Both
the legacy single-jar path (``_read_browser_cookies``) and the Chromium
multi-profile fan-out path call this helper.

Also owns :data:`_ROOKIEPY_BROWSER_ALIASES` — the user-facing browser
name → rookiepy function-name map (referenced by
:mod:`.browser_accounts._read_browser_cookies` for the named-browser
dispatch path).

Imports from :mod:`.rookiepy_errors` only (used implicitly via the
shared error printing in caller modules; this module itself relies on
the auth-side helpers for cookie shape conversion).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import httpx

from ....auth import (
    convert_rookiepy_cookies_to_storage_state,
    extract_cookies_from_storage,
)
from ...error_handler import exit_with_code
from ...rendering import console
from ...runtime import run_async

if TYPE_CHECKING:
    from ....auth import Account

logger = logging.getLogger(__name__)


# Maps user-facing browser names to rookiepy function names.
_ROOKIEPY_BROWSER_ALIASES: dict[str, str] = {
    "arc": "arc",
    "brave": "brave",
    "chrome": "chrome",
    "chromium": "chromium",
    "edge": "edge",
    "firefox": "firefox",
    "ie": "ie",
    "librewolf": "librewolf",
    "octo": "octo",
    "opera": "opera",
    "opera-gx": "opera_gx",
    "opera_gx": "opera_gx",
    "safari": "safari",
    "vivaldi": "vivaldi",
    "zen": "zen",
}


def _enumerate_one_jar(
    raw_cookies: list[dict[str, Any]],
    browser_name: str,
    browser_profile: str | None,
    *,
    quiet: bool = False,
) -> list[Account]:
    """Probe ``?authuser=N`` against one cookie set and return tagged Accounts.

    Shared by both the legacy single-jar path and the chromium multi-profile
    fan-out path. ``browser_profile`` annotates the resulting Accounts so the
    fan-out caller can route writes back to the right source.

    Args:
        raw_cookies: rookiepy cookie dicts for one source.
        browser_name: The browser the cookies came from (for error messages).
        browser_profile: Tag attached to each Account (``"Default"``,
            ``"Profile 1"``, ...) or ``None`` for the legacy single-jar path.
        quiet: Suppress the loud multi-line user-facing error panels
            (``"No valid Google authentication cookies"``, ``"Account
            discovery failed: …stale"``) for "this profile is signed out"
            cases and just raise ``SystemExit``. Used by the fan-out caller,
            which prints its own per-profile soft note for signed-out /
            stale-cookie profiles and would otherwise bleed those panels into
            the table output. Network errors (``httpx.RequestError``) are
            NOT downgraded — they propagate as-is so the caller can
            distinguish transport failures from per-profile "signed out".

    Raises:
        SystemExit: On missing required cookies or stale-cookie rejection
            by Google (Google redirected to the account chooser, etc.).
            These are per-profile "signed out" conditions in fan-out mode
            and are caught and skipped by the fan-out caller.
        httpx.RequestError: On network transport failure. Re-raised
            unchanged so the fan-out aborts (vs. silently downgrading every
            offline profile to a soft skip).
    """
    from ....auth import (
        Account,
        build_cookie_jar,
        enumerate_accounts,
        extract_cookies_with_domains,
    )

    storage_state = convert_rookiepy_cookies_to_storage_state(raw_cookies)
    try:
        extract_cookies_from_storage(storage_state)
    except ValueError as e:
        if not quiet:
            console.print(
                "[red]No valid Google authentication cookies found.[/red]\n"
                f"{e}\n\n"
                "Make sure you are logged into Google in your browser."
            )
        exit_with_code(1)

    cookie_map = extract_cookies_with_domains(storage_state)
    jar = build_cookie_jar(cookies=cookie_map)
    try:
        accounts = run_async(enumerate_accounts(jar))
    except ValueError:
        # Cookies are present but Google rejected them (passive sign-in
        # redirected to the account chooser, or RotateCookies returned 401).
        if not quiet:
            console.print(
                f"[red]Account discovery failed: {browser_name}'s saved cookies are "
                f"too stale for Google to re-authenticate.[/red]\n\n"
                "Refresh them by opening the browser and visiting a Google site "
                "(e.g. https://notebooklm.google.com), then re-run this command.\n\n"
                "If the browser is signed out, sign back in there first.\n"
                "If you'd rather skip the browser entirely, use "
                "[cyan]notebooklm login[/cyan] (Playwright flow)."
            )
        exit_with_code(1)
    except httpx.RequestError as e:
        # Distinct from "signed out / stale" SystemExit branches above:
        # a network failure means EVERY profile probe will fail the same
        # way, so we must surface the transport error rather than let the
        # fan-out caller collapse it into a soft per-profile skip.
        if not quiet:
            console.print(
                f"[red]Account discovery failed (network error):[/red] {e}\n"
                "Check your internet connection and try again."
            )
            exit_with_code(1)
        raise

    if browser_profile is None:
        return list(accounts)
    return [
        Account(
            authuser=a.authuser,
            email=a.email,
            is_default=a.is_default,
            browser_profile=browser_profile,
        )
        for a in accounts
    ]
