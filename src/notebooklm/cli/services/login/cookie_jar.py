"""Shared cookie-jar enumeration helper.

Contains :func:`_enumerate_one_jar` — probes one rookie-cookies cookie set
against ``?authuser=N`` to return tagged :class:`Account` records. Both
the legacy single-jar path (``_read_browser_cookies``) and the Chromium
multi-profile fan-out path call this helper.

Also owns :data:`_ROOKIE_COOKIES_BROWSER_ALIASES` — the user-facing browser
name → rookie-cookies function-name map (referenced by
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

from typing import TYPE_CHECKING, Any, cast

from ...._app.login_cookie import (
    Account,
    BrowserCookieProbeFailure,
    BrowserCookieProbeRequest,
    BrowserCookieProbeSuccess,
    ProbeRunner,
    _browser_cookie_validation_failure,
    probe_browser_cookie_jar,
)
from ....auth import app_host_scope_note, validate_with_recovery
from ....config import get_base_host
from .io_seam import resolve_login_io
from .outcomes import (
    BrowserCookieOutcome,
    CookieValidationFailure,
    NetworkFailure,
    StaleCookies,
)

if TYPE_CHECKING:
    from .io_seam import LoginIO

# Maps user-facing browser names to rookie-cookies function names.
_ROOKIE_COOKIES_BROWSER_ALIASES: dict[str, str] = {
    "arc": "arc",
    "brave": "brave",
    "chrome": "chrome",
    "chromium": "chromium",
    "edge": "edge",
    "firefox": "firefox",
    "ie": "internet_explorer",
    "librewolf": "librewolf",
    "octo": "octo_browser",
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
    validate_before_probe: bool = True,
    io: LoginIO | None = None,
) -> list[Account] | BrowserCookieOutcome:
    """Probe ``?authuser=N`` against one cookie set and return tagged Accounts.

    Shared by both the legacy single-jar path and the chromium multi-profile
    fan-out path. ``browser_profile`` annotates the resulting Accounts so the
    fan-out caller can route writes back to the right source.

    Args:
        raw_cookies: rookie-cookies cookie dicts for one source.
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
        validate_before_probe: When true, run the normal route-aware cookie
            validation/recovery before account enumeration. ``auth inspect``
            sets this false to preserve its historical network-error
            precedence, then validates after a successful probe.

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
    request = result = resolved_io = paragraphs = scope_note = probe_runner = None
    try:
        resolved_io = resolve_login_io(io)
        request = BrowserCookieProbeRequest(
            raw_cookies=raw_cookies,
            browser_name=browser_name,
            browser_profile=browser_profile,
            quiet=quiet,
            validate_before_probe=validate_before_probe,
        )
        probe_runner = cast(ProbeRunner, resolved_io.run_async)
        result = probe_browser_cookie_jar(
            request,
            run_probe=probe_runner,
            validate_with_recovery=validate_with_recovery,
        )
        if isinstance(result, BrowserCookieProbeSuccess):
            return list(result.accounts)
        assert isinstance(result, BrowserCookieProbeFailure)
        if result.code == "COOKIE_VALIDATION":
            return _project_cookie_validation_failure(
                result,
                browser_name=browser_name,
                quiet=quiet,
            )
        if result.code == "STALE_COOKIES":
            if quiet:
                return StaleCookies(
                    code="STALE_COOKIES",
                    message=(
                        f"Saved cookies for {browser_name} are too stale for Google "
                        "to re-authenticate."
                    ),
                )
            paragraphs = [
                f"[red]Account discovery failed: {browser_name}'s saved cookies are "
                f"too stale for Google to re-authenticate.[/red]",
                "Refresh them by opening the browser and visiting "
                f"https://{get_base_host()} (the host this client probed), then "
                "re-run this command.",
            ]
            scope_note = app_host_scope_note()
            if scope_note:
                paragraphs.append(scope_note)
            paragraphs.append(
                "If the browser is signed out, sign back in there first.\n"
                "If you'd rather skip the browser entirely, use "
                "[cyan]notebooklm login[/cyan] (Playwright flow)."
            )
            return StaleCookies(code="STALE_COOKIES", message="\n\n".join(paragraphs))
        return NetworkFailure(
            code="NETWORK_ERROR",
            message=(
                f"[red]Account discovery failed (network error):[/red] {result.detail}\n"
                "Check your internet connection and try again."
            ),
        )
    finally:
        del raw_cookies, browser_name, browser_profile, io, request, result
        del quiet, validate_before_probe, resolved_io, paragraphs, scope_note, probe_runner


def _project_cookie_validation_failure(
    failure: BrowserCookieProbeFailure,
    *,
    browser_name: str,
    quiet: bool,
) -> CookieValidationFailure:
    """Render the current CLI outcome from a neutral validation failure."""
    result = None
    try:
        if quiet:
            result = CookieValidationFailure(
                code="COOKIE_VALIDATION_FAILED",
                message=f"No valid Google authentication cookies found in {browser_name}.",
            )
            return result
        result = CookieValidationFailure(
            code="COOKIE_VALIDATION_FAILED",
            message=(
                "[red]No valid Google authentication cookies found.[/red]\n"
                f"{failure.detail}\n\n{failure.hint}"
            ),
        )
        return result
    finally:
        del failure, browser_name, quiet, result


def _cookie_validation_failure(
    storage_state: dict[str, Any],
    validation_error: ValueError,
    *,
    browser_name: str,
    quiet: bool,
) -> CookieValidationFailure:
    """Retained exact-signature adapter for direct and patched legacy callers."""
    failure = result = None
    try:
        failure = _browser_cookie_validation_failure(
            storage_state,
            validation_error,
            browser_name=browser_name,
        )
        result = _project_cookie_validation_failure(
            failure,
            browser_name=browser_name,
            quiet=quiet,
        )
        return result
    finally:
        del storage_state, validation_error, browser_name, quiet, failure, result
