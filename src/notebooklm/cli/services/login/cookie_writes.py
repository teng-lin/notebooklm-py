"""Cookie write + account-selection helpers.

Owns the path that persists extracted cookies to ``storage_state.json``
(``_write_extracted_cookies``) and the two account-selection helpers
(``_select_account`` for the ``--browser-cookies``-driven targeted
extraction, ``_select_refresh_account`` for the refresh-from-cached path).

Failure shape: every public helper here returns either its success value
OR a :class:`.outcomes.BrowserCookieOutcome` subclass on failure. Failure
paths no longer own exit policy; callers (the auth-inspect command, the
``login --browser-cookies`` refresh driver) dispatch on the outcome.
Nonfatal warnings still render through a tiny local emitter so text-mode
CLI behavior remains compatible.

Imports from :mod:`.outcomes`. The DAG (``test_login_package_dag.py``)
also allows edges to :mod:`.browser_accounts` and
:mod:`.cookie_domains` for future use, but neither is currently needed:
``_write_extracted_cookies`` and the selectors operate on already-loaded
cookie data + already-discovered accounts, and the selectors do not need
to query the cookie-domain policy themselves.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from ....auth import (
    cookie_names_from_storage,
    fetch_tokens_with_domains,
    missing_cookies_hint,
    validate_with_recovery,
)
from ....io import atomic_write_json
from .outcomes import (
    BrowserCookieOutcome,
    CookieValidationFailure,
)

if TYPE_CHECKING:
    from .io_seam import LoginIO

logger = logging.getLogger(__name__)


def _emit_warning(io: LoginIO, message: str) -> None:
    """Render a nonfatal text-mode warning through the caller-injected sink.

    Routes the warning line through ``io.emit`` so this service never imports
    the command layer's ``...rendering`` module (ADR-0008 level-3 boundary,
    #1393), while keeping the failure paths typed (outcomes, not exits).
    """
    io.emit(message)


def _select_account(
    io: LoginIO,
    accounts: list[Any],
    *,
    account_email: str | None,
) -> Any | BrowserCookieOutcome:
    """Pick the requested account from a discovery result.

    Email is the user-facing selector because it is stable across browser
    account reordering. Without an email, select the browser's default account.

    Returns either the selected account (success) or a
    :class:`.outcomes.CookieValidationFailure` outcome with a
    human-readable message when no accounts were discovered or the
    requested email is absent. The default-account fallback still emits
    the legacy nonfatal warning (via the injected ``io`` sink) and then
    returns the first account.
    """
    if not accounts:
        return CookieValidationFailure(
            code="NO_ACCOUNTS_FOUND",
            message=(
                "[red]No signed-in Google accounts found.[/red]\n"
                "Sign in to a Google account in your browser and try again."
            ),
        )

    if account_email:
        requested = account_email.strip().casefold()
        for account in accounts:
            if account.email.casefold() == requested:
                return account
        available = ", ".join(a.email for a in accounts)
        return CookieValidationFailure(
            code="ACCOUNT_NOT_FOUND",
            message=(
                f"[red]Account {account_email} not found among signed-in accounts.[/red]\n"
                f"Available accounts: {available}"
            ),
        )
    default_account = next((a for a in accounts if a.is_default), None)
    if default_account is not None:
        return default_account

    # No default marker — fall back to the first account. This is a
    # nonfatal success-path warning, so keep the previous text-mode output.
    _emit_warning(
        io,
        "[yellow]Warning: Browser account list did not mark a default account; "
        f"using {accounts[0].email}.[/yellow]",
    )
    logger.warning(
        "Browser account list did not mark a default account; using %s.",
        accounts[0].email,
    )
    return accounts[0]


def _select_refresh_account(
    accounts: list[Any],
    metadata: dict[str, Any],
    browser_name: str,
) -> Any | BrowserCookieOutcome:
    """Select the browser account that should refresh the active profile.

    ``context.json`` stores both the account email (stable identity) and an
    internal fallback index. If the browser's account order changed, email wins
    and the caller rewrites the cached index.

    Returns the selected account on success, or a
    :class:`.outcomes.CookieValidationFailure` on failure (no accounts
    discovered, metadata email not present in browser, stale authuser
    with no email to repair from).
    """
    if not accounts:
        return CookieValidationFailure(
            code="NO_ACCOUNTS_FOUND",
            message=(
                f"[red]No signed-in Google accounts found in {browser_name}.[/red]\n"
                "Sign in to a Google account in your browser and try again."
            ),
        )

    expected_email = metadata.get("email")
    if isinstance(expected_email, str) and expected_email.strip():
        normalized = expected_email.strip().casefold()
        for account in accounts:
            if isinstance(account.email, str) and account.email.casefold() == normalized:
                return account
        available = ", ".join(a.email for a in accounts) or "none"
        return CookieValidationFailure(
            code="PROFILE_ACCOUNT_MISSING",
            message=(
                f"[red]Profile account {expected_email} is not signed in to "
                f"{browser_name}.[/red]\n"
                f"Available accounts: {available}\n"
                f"Run [cyan]notebooklm auth inspect --browser {browser_name}[/cyan] "
                "or sign that account back into the browser."
            ),
        )

    raw_authuser = metadata.get("authuser")
    if isinstance(raw_authuser, int) and raw_authuser >= 0:
        for account in accounts:
            if account.authuser == raw_authuser:
                return account
        return CookieValidationFailure(
            code="PROFILE_ACCOUNT_MISSING",
            message=(
                "[red]Profile stores an old account route, but that browser account "
                "is no longer available and context.json has no account email to "
                "repair from.[/red]\n"
                f"Run [cyan]notebooklm auth inspect --browser {browser_name}[/cyan], then "
                f"[cyan]notebooklm login --browser-cookies {browser_name} "
                "--account EMAIL[/cyan]."
            ),
        )

    return next((account for account in accounts if account.is_default), accounts[0])


def _write_extracted_cookies(
    io: LoginIO,
    raw_cookies: list[dict[str, Any]],
    *,
    storage_path: Path,
    profile: str | None,
    authuser: int,
    email: str,
    quiet: bool = False,
) -> BrowserCookieOutcome | None:
    """Write a previously-loaded rookiepy cookie set to ``storage_path``.

    Bypasses :func:`_read_browser_cookies` because the caller already has
    the cookies in hand (e.g. ``--all-accounts`` reads once and writes N
    profiles).

    Returns ``None`` on success, or a
    :class:`.outcomes.BrowserCookieOutcome` subclass on failure
    (validation failure or disk-write failure). The success-path
    confirmation print is emitted by the caller; nonfatal metadata and
    verification warnings are routed through the injected ``io`` sink to
    preserve the historical text-mode behavior, and ``io.run_async`` drives
    the cookie-verification probe.
    """
    storage_state, validation_error = validate_with_recovery(raw_cookies)
    if validation_error is not None:
        cookie_names = cookie_names_from_storage(storage_state)
        hint = missing_cookies_hint(cookie_names)
        return CookieValidationFailure(
            code="COOKIE_VALIDATION_FAILED",
            message=(
                "[red]No valid Google authentication cookies found.[/red]\n"
                f"{validation_error}\n\n"
                f"{hint}"
            ),
        )

    try:
        storage_path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write with chmod 0o600 — avoids non-atomic + world-readable
        # window from plain write_text + post-hoc chmod.
        atomic_write_json(storage_path, storage_state)
        if sys.platform != "win32":
            storage_path.parent.chmod(0o700)
    except OSError as e:
        # G6: redact the bound exception in the log line (use the type
        # name) so subprocess stderr / payload data captured in ``e`` is
        # not persisted in caller log destinations.
        logger.error("Failed to save authentication to %s: %s", storage_path, type(e).__name__)
        return CookieValidationFailure(
            code="STORAGE_WRITE_FAILED",
            message=(f"[red]Failed to save authentication to {storage_path}.[/red]\nDetails: {e}"),
        )

    from ....auth import write_account_metadata

    try:
        write_account_metadata(storage_path, authuser=authuser, email=email)
    except OSError as e:
        # Non-fatal: cookies are already written. Log the redacted type,
        # but preserve the previous user-facing warning text.
        logger.warning("Failed to save account metadata for %s: %s", storage_path, type(e).__name__)
        _emit_warning(
            io,
            f"[yellow]Warning: cookies saved but account metadata write failed.[/yellow]\n"
            f"Details: {e}",
        )

    # Success-path confirmation print is the caller's job. We log a
    # debug breadcrumb so operators can correlate the write without
    # parsing the user-facing console output.
    if not quiet:
        logger.debug("wrote cookies for %s to %s", email, storage_path)

    # Verify cookies for the active account. Verification failures are
    # not fatal (the existing behavior warned but did not exit), so we
    # log a WARNING and return None.
    try:
        io.run_async(fetch_tokens_with_domains(storage_path, profile))
    except ValueError as e:
        logger.warning("Extracted cookies for %s failed verification: %s", email, e)
        _emit_warning(io, f"    [yellow]Warning: cookies for {email} failed verification.[/yellow]")
    except httpx.RequestError as e:
        logger.warning("Could not verify cookies for %s: %s", email, e)
        _emit_warning(
            io,
            f"    [yellow]Warning: could not verify cookies for {email} (network).[/yellow]",
        )
    return None
