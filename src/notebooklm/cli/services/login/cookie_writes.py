"""Cookie write + account-selection helpers.

Owns the path that persists extracted cookies to ``storage_state.json``
(``_write_extracted_cookies``) and the two account-selection helpers
(``_select_account`` for the ``--browser-cookies``-driven targeted
extraction, ``_select_refresh_account`` for the refresh-from-cached path).

Imports from :mod:`.browser_accounts` (allowed-but-not-required per
the DAG; the writer itself does not call into the browser-account
discovery dispatchers) and :mod:`.cookie_domains` (likewise).
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

import httpx

from ....auth import (
    convert_rookiepy_cookies_to_storage_state,
    extract_cookies_from_storage,
    fetch_tokens_with_domains,
)
from ....io import atomic_write_json
from ...error_handler import exit_with_code
from ...rendering import console
from ...runtime import run_async

logger = logging.getLogger(__name__)


def _select_account(
    accounts: list[Any],
    *,
    account_email: str | None,
) -> Any:
    """Pick the requested account from a discovery result.

    Email is the user-facing selector because it is stable across browser
    account reordering. Without an email, select the browser's default account.
    """
    if account_email:
        requested = account_email.strip().casefold()
        for account in accounts:
            if account.email.casefold() == requested:
                return account
        available = ", ".join(a.email for a in accounts)
        console.print(
            f"[red]Account {account_email} not found among signed-in accounts.[/red]\n"
            f"Available accounts: {available}"
        )
        exit_with_code(1)
    default_account = next((a for a in accounts if a.is_default), None)
    if default_account is not None:
        return default_account

    console.print(
        "[yellow]Warning: Browser account list did not mark a default account; "
        f"using {accounts[0].email}.[/yellow]"
    )
    return accounts[0]


def _select_refresh_account(
    accounts: list[Any], metadata: dict[str, Any], browser_name: str
) -> Any:
    """Select the browser account that should refresh the active profile.

    ``context.json`` stores both the account email (stable identity) and an
    internal fallback index. If the browser's account order changed, email wins
    and the caller rewrites the cached index.
    """
    expected_email = metadata.get("email")
    if isinstance(expected_email, str) and expected_email.strip():
        normalized = expected_email.strip().casefold()
        for account in accounts:
            if isinstance(account.email, str) and account.email.casefold() == normalized:
                return account
        available = ", ".join(a.email for a in accounts) or "none"
        console.print(
            f"[red]Profile account {expected_email} is not signed in to {browser_name}.[/red]\n"
            f"Available accounts: {available}\n"
            f"Run [cyan]notebooklm auth inspect --browser {browser_name}[/cyan] "
            "or sign that account back into the browser."
        )
        exit_with_code(1)

    raw_authuser = metadata.get("authuser")
    if isinstance(raw_authuser, int) and raw_authuser >= 0:
        for account in accounts:
            if account.authuser == raw_authuser:
                return account
        console.print(
            "[red]Profile stores an old account route, but that browser account "
            "is no longer available and context.json has no account email to repair from.[/red]\n"
            f"Run [cyan]notebooklm auth inspect --browser {browser_name}[/cyan], then "
            f"[cyan]notebooklm login --browser-cookies {browser_name} --account EMAIL[/cyan]."
        )
        exit_with_code(1)

    return next((account for account in accounts if account.is_default), accounts[0])


def _write_extracted_cookies(
    raw_cookies: list[dict[str, Any]],
    *,
    storage_path: Path,
    profile: str | None,
    authuser: int,
    email: str,
    quiet: bool = False,
) -> None:
    """Write a previously-loaded rookiepy cookie set to ``storage_path``.

    Bypasses :func:`_read_browser_cookies` because the caller already has the
    cookies in hand (e.g. ``--all-accounts`` reads once and writes N profiles).
    """
    storage_state = convert_rookiepy_cookies_to_storage_state(raw_cookies)
    try:
        extract_cookies_from_storage(storage_state)
    except ValueError as e:
        console.print(
            "[red]No valid Google authentication cookies found.[/red]\n"
            f"{e}\n\n"
            "Make sure you are logged into Google in your browser."
        )
        exit_with_code(1)

    try:
        storage_path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write with chmod 0o600 — avoids non-atomic + world-readable
        # window from plain write_text + post-hoc chmod.
        atomic_write_json(storage_path, storage_state)
        if sys.platform != "win32":
            storage_path.parent.chmod(0o700)
    except OSError as e:
        logger.error("Failed to save authentication to %s: %s", storage_path, e)
        console.print(f"[red]Failed to save authentication to {storage_path}.[/red]\nDetails: {e}")
        exit_with_code(1)

    from ....auth import write_account_metadata

    try:
        write_account_metadata(storage_path, authuser=authuser, email=email)
    except OSError as e:
        logger.error("Failed to save account metadata for %s: %s", storage_path, e)
        console.print(
            f"[yellow]Warning: cookies saved but account metadata write failed.[/yellow]\n"
            f"Details: {e}"
        )

    if not quiet:
        console.print(f"  [green]✓[/green] {profile or storage_path}  →  {email}")

    # Verify cookies for the active account.
    try:
        run_async(fetch_tokens_with_domains(storage_path, profile))
    except ValueError as e:
        logger.warning("Extracted cookies for %s failed verification: %s", email, e)
        console.print(f"    [yellow]Warning: cookies for {email} failed verification.[/yellow]")
    except httpx.RequestError as e:
        logger.warning("Could not verify cookies for %s: %s", email, e)
        console.print(
            f"    [yellow]Warning: could not verify cookies for {email} (network).[/yellow]"
        )
