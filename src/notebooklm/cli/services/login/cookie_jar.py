"""Shared cookie-jar enumeration helper.

Contains :func:`_enumerate_one_jar` — probes one rookiepy cookie set
against ``?authuser=N`` to return tagged :class:`Account` records. Both
the legacy single-jar path (``_read_browser_cookies``) and the Chromium
multi-profile fan-out path call this helper.

Also owns :data:`_ROOKIEPY_BROWSER_ALIASES` — the user-facing browser
name → rookiepy function-name map (referenced by
:mod:`.browser_accounts._read_browser_cookies` for the named-browser
dispatch path).

Failure shape: :func:`_enumerate_one_jar` returns either a list of
:class:`Account` records (success) OR a
:class:`.outcomes.BrowserCookieOutcome` subclass for cookie-policy /
stale-cookie failures. Network failures (``httpx.RequestError``) are
returned as :class:`.outcomes.NetworkFailure` in normal mode but
propagate unchanged in ``quiet=True`` fan-out mode — that caller must
distinguish transport failures from per-profile "signed out" so it can
abort cleanly. The boundary test
(``tests/unit/cli/test_services_boundary.py``) keeps this module in
:data:`GUARDED_PATHS`; there is no presentation or exit policy in this
file.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import httpx

from ....auth import (
    cookie_names_from_storage,
    missing_cookies_hint,
    validate_with_recovery,
)
from .io_seam import resolve_login_io
from .outcomes import (
    BrowserCookieOutcome,
    CookieValidationFailure,
    NetworkFailure,
    StaleCookies,
)

if TYPE_CHECKING:
    from ....auth import Account
    from .io_seam import LoginIO

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
    io: LoginIO | None = None,
) -> list[Account] | BrowserCookieOutcome:
    """Probe ``?authuser=N`` against one cookie set and return tagged Accounts.

    Shared by both the legacy single-jar path and the chromium multi-profile
    fan-out path. ``browser_profile`` annotates the resulting Accounts so the
    fan-out caller can route writes back to the right source.

    Args:
        raw_cookies: rookiepy cookie dicts for one source.
        browser_name: The browser the cookies came from (for error messages).
        browser_profile: Tag attached to each Account (``"Default"``,
            ``"Profile 1"``, ...) or ``None`` for the legacy single-jar path.
        io: Optional caller-injected :class:`.io_seam.LoginIO` sink (resolved
            to the command-layer default when ``None``) whose ``run_async``
            drives the synchronous account-enumeration probe. This module owns
            no presentation/exit — only the async bridge.
        quiet: Suppress the loud multi-line user-facing message body in the
            returned outcome (the fan-out caller prints its own per-profile
            soft note for signed-out / stale-cookie profiles and would
            otherwise bleed those panels into the table output). The
            returned outcome class is unchanged; only the ``message``
            payload is collapsed when ``quiet=True``. Network errors
            (``httpx.RequestError``) are NOT downgraded — they propagate
            as-is so the caller can distinguish transport failures from
            per-profile "signed out".

    Returns:
        list[Account]: signed-in Google accounts on the success path.

        :class:`.outcomes.BrowserCookieOutcome`:
        * :class:`.outcomes.CookieValidationFailure` — missing required
          cookies / malformed policy.
        * :class:`.outcomes.StaleCookies` — Google rejected the cookie
          set (account chooser redirect, RotateCookies 401).
        * :class:`.outcomes.NetworkFailure` — account enumeration hit a
          transport error. In ``quiet=True`` mode this propagates as
          ``httpx.RequestError`` instead so Chromium fan-out aborts the
          whole discovery rather than treating every profile as signed out.

    Raises:
        httpx.RequestError: On network transport failure when ``quiet=True``.
            Re-raised unchanged so fan-out aborts (vs. silently downgrading
            every offline profile to a soft skip).
    """
    from ....auth import (
        Account,
        build_cookie_jar,
        enumerate_accounts,
        extract_cookies_with_domains,
    )

    io = resolve_login_io(io)
    storage_state, validation_error = validate_with_recovery(raw_cookies)
    if validation_error is not None:
        if quiet:
            return CookieValidationFailure(
                code="COOKIE_VALIDATION_FAILED",
                message=f"No valid Google authentication cookies found in {browser_name}.",
            )
        cookie_names = cookie_names_from_storage(storage_state)
        hint = missing_cookies_hint(cookie_names, browser_label=browser_name)
        return CookieValidationFailure(
            code="COOKIE_VALIDATION_FAILED",
            message=(
                "[red]No valid Google authentication cookies found.[/red]\n"
                f"{validation_error}\n\n"
                f"{hint}"
            ),
        )

    cookie_map = extract_cookies_with_domains(storage_state)
    jar = build_cookie_jar(cookies=cookie_map)
    try:
        accounts = io.run_async(enumerate_accounts(jar))
    except ValueError:
        # Cookies are present but Google rejected them (passive sign-in
        # redirected to the account chooser, or RotateCookies returned 401).
        if quiet:
            return StaleCookies(
                code="STALE_COOKIES",
                message=(
                    f"Saved cookies for {browser_name} are too stale for Google to re-authenticate."
                ),
            )
        return StaleCookies(
            code="STALE_COOKIES",
            message=(
                f"[red]Account discovery failed: {browser_name}'s saved cookies are "
                f"too stale for Google to re-authenticate.[/red]\n\n"
                "Refresh them by opening the browser and visiting a Google site "
                "(e.g. https://notebooklm.google.com), then re-run this command.\n\n"
                "If the browser is signed out, sign back in there first.\n"
                "If you'd rather skip the browser entirely, use "
                "[cyan]notebooklm login[/cyan] (Playwright flow)."
            ),
        )
    except httpx.RequestError as e:
        # Distinct from "signed out / stale" branches above: a network
        # failure means every profile probe is likely to fail the same way.
        # Fan-out callers use quiet=True and must still see the exception so
        # they can abort instead of soft-skipping all profiles.
        if quiet:
            raise
        return NetworkFailure(
            code="NETWORK_ERROR",
            message=(
                f"[red]Account discovery failed (network error):[/red] {e}\n"
                "Check your internet connection and try again."
            ),
        )

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
