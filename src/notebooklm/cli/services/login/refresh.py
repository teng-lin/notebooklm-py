"""Refresh + bulk-login drivers + post-login server-language sync.

Top of the leaf-ward DAG: imports from :mod:`.browser_accounts`,
:mod:`.cookie_writes`, and :mod:`.profile_targets`. Owns:

- ``_login_browser_cookies_single`` — extract one account into a profile.
- ``_login_all_accounts_from_browser`` — extract every signed-in account.
- ``_login_with_browser_cookies`` — single-jar default-account login.
- ``_refresh_from_browser_cookies`` — repair account drift for the
  active profile.
- ``_sync_server_language_to_config`` — fetch server language setting
  after login and persist locally. **Legacy import path preservation:**
  37+ patch sites monkeypatch ``notebooklm.cli.session_cmd._sync_server_language_to_config``.
  The session module's ``from .services.login import _sync_server_language_to_config``
  resolves via the package's ``__init__.py`` re-export of this function.
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
    read_account_metadata,
)
from ....client import NotebookLMClient
from ....io import atomic_write_json
from ....paths import get_storage_path
from ...error_handler import exit_with_code
from ...language_cmd import set_language
from ...rendering import console
from ...runtime import run_async
from .browser_accounts import _enumerate_browser_accounts, _read_browser_cookies
from .cookie_writes import _select_account, _select_refresh_account, _write_extracted_cookies
from .profile_targets import (
    _profiles_by_account_email,
    _resolve_all_accounts_target,
    _validate_profile_name,
    email_to_profile_name,
)

logger = logging.getLogger(__name__)


def _login_browser_cookies_single(
    browser_cookies: str,
    *,
    storage: str | None,
    account_email: str | None,
    profile_name: str | None,
    active_profile: str | None,
    include_domains: set[str] | None = None,
) -> None:
    """Extract one account from ``--browser-cookies`` into a profile.

    Resolves the target storage path:

    - ``--storage`` wins outright.
    - ``--profile-name`` selects a sibling profile under the home dir.
    - ``--account`` defaults the new profile to the email's local-part
      when the user did not pass ``--profile-name``.
    - Otherwise we write to the active profile (existing behavior).
    """
    explicit_storage = Path(storage) if storage else None

    if account_email is None and profile_name is None:
        # Path 1: existing behavior — extract default account into active profile.
        resolved_storage = explicit_storage or get_storage_path(profile=active_profile)
        _login_with_browser_cookies(
            resolved_storage,
            browser_cookies,
            active_profile,
            include_domains=include_domains,
        )
        return

    # Path 2: targeted extraction. We need the email to derive a profile name
    # when --profile-name is omitted.
    per_profile_cookies, accounts = _enumerate_browser_accounts(
        browser_cookies, include_domains=include_domains
    )
    selected = _select_account(accounts, account_email=account_email)

    target_profile = profile_name or email_to_profile_name(selected.email)
    if profile_name is not None:
        _validate_profile_name(target_profile)

    target_storage = explicit_storage or get_storage_path(profile=target_profile)

    _write_extracted_cookies(
        per_profile_cookies[selected.browser_profile],
        storage_path=target_storage,
        profile=target_profile if not explicit_storage else active_profile,
        authuser=selected.authuser,
        email=selected.email,
    )


def _login_all_accounts_from_browser(
    browser_cookies: str,
    *,
    update: bool = False,
    include_domains: set[str] | None = None,
) -> None:
    """Extract every signed-in Google account into its own profile.

    Args:
        browser_cookies: rookiepy browser alias forwarded to
            :func:`_enumerate_browser_accounts`.
        update: When True and the natural profile name for an account
            (e.g. ``alice`` for ``alice@gmail.com``) already exists but has
            no account metadata — or its metadata matches the same email —
            adopt that profile in place rather than allocating a suffixed
            ``alice-2``. Profiles whose metadata already binds a *different*
            email are still given a suffix to avoid clobbering them. Useful
            for users who hand-created profiles via plain ``notebooklm
            login --profile NAME`` before extending to ``--all-accounts``.
        include_domains: Forwarded to :func:`_enumerate_browser_accounts`.
    """
    from ....paths import list_profiles

    per_profile_cookies, accounts = _enumerate_browser_accounts(
        browser_cookies, include_domains=include_domains
    )
    if not accounts:
        console.print("[yellow]No accounts discovered.[/yellow]")
        return

    console.print(f"\n[bold]Found {len(accounts)} accounts.[/bold] Saving profiles:")
    # Reuse a profile when its account metadata already points at the same
    # email. This makes repeated --all-accounts runs idempotent and lets a
    # later run update authuser if Google's account indices shifted. Only
    # allocate a suffix when the desired profile name belongs to a different
    # account or a hand-created profile with no account metadata.
    existing_profiles = list_profiles()
    existing_profiles_set = set(existing_profiles)
    profiles_by_email = _profiles_by_account_email(existing_profiles)
    unavailable: set[str] = set(existing_profiles)
    claimed: set[str] = set()
    for account in accounts:
        base_name = email_to_profile_name(account.email)
        target_profile = profiles_by_email.get(account.email.casefold())
        if target_profile is None or target_profile in claimed:
            target_profile = _resolve_all_accounts_target(
                base_name=base_name,
                account_email=account.email,
                existing_profiles=existing_profiles_set,
                unavailable=unavailable,
                claimed=claimed,
                update=update,
            )
        unavailable.add(target_profile)
        claimed.add(target_profile)

        target_storage = get_storage_path(profile=target_profile)
        _write_extracted_cookies(
            per_profile_cookies[account.browser_profile],
            storage_path=target_storage,
            profile=target_profile,
            authuser=account.authuser,
            email=account.email,
        )


def _refresh_from_browser_cookies(
    browser_name: str,
    *,
    storage_path: Path,
    profile: str | None,
    quiet: bool,
    include_domains: set[str] | None = None,
) -> None:
    """Refresh the active profile from browser cookies, repairing account drift."""
    per_profile_cookies, accounts = _enumerate_browser_accounts(
        browser_name, verbose=not quiet, include_domains=include_domains
    )
    if not accounts:
        console.print(f"[red]No signed-in Google accounts found in {browser_name}.[/red]")
        exit_with_code(1)

    metadata = read_account_metadata(storage_path)
    selected = _select_refresh_account(accounts, metadata, browser_name)
    _write_extracted_cookies(
        per_profile_cookies[selected.browser_profile],
        storage_path=storage_path,
        profile=profile,
        authuser=selected.authuser,
        email=selected.email,
        quiet=True,
    )

    if not quiet:
        console.print(
            f"[green]ok[/green] refreshed from {browser_name}: {storage_path}\n"
            f"[green]account[/green] {selected.email}"
        )


def _login_with_browser_cookies(
    storage_path: Path,
    browser_name: str,
    profile: str | None = None,
    *,
    authuser: int = 0,
    email: str | None = None,
    include_domains: set[str] | None = None,
) -> None:
    """Extract Google cookies from an installed browser via rookiepy.

    Args:
        storage_path: Where to write storage_state.json.
        browser_name: "auto" to use rookiepy.load(), or a specific browser name.
        profile: Profile name (forwarded to verification step).
        authuser: Internal Google account index fallback for this profile.
        email: Optional account email to record for stable routing.
        include_domains: Optional ``--include-domains`` label set forwarded
            to :func:`_read_browser_cookies`.
    """
    raw_cookies = _read_browser_cookies(browser_name, include_domains=include_domains)

    storage_state = convert_rookiepy_cookies_to_storage_state(raw_cookies)
    try:
        extract_cookies_from_storage(storage_state)  # validates SID is present
    except ValueError as e:
        console.print(
            "[red]No valid Google authentication cookies found.[/red]\n"
            f"{e}\n\n"
            "Make sure you are logged into Google in your browser."
        )
        exit_with_code(1)

    # Create parent directory (avoid mode= on Windows to prevent ACL issues)
    try:
        storage_path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write with chmod 0o600 — avoids non-atomic + world-readable
        # window from plain write_text + post-hoc chmod.
        atomic_write_json(storage_path, storage_state)
        if sys.platform != "win32":
            # On Unix: ensure directory has restrictive permissions
            # (atomic_write_json handles the file mode).
            storage_path.parent.chmod(0o700)
    except OSError as e:
        logger.error("Failed to save authentication to %s: %s", storage_path, e)
        console.print(f"[red]Failed to save authentication to {storage_path}.[/red]\nDetails: {e}")
        exit_with_code(1)

    # Record account metadata so future calls target the same Google account.
    # Even on a default-account login (authuser=0, no email), remove stale
    # metadata so refreshed cookies cannot keep routing to an older account.
    if authuser or email:
        from ....auth import write_account_metadata

        try:
            write_account_metadata(storage_path, authuser=authuser, email=email)
        except OSError as e:
            logger.error("Failed to save account metadata for %s: %s", storage_path, e)
            console.print(
                f"[yellow]Warning: cookies saved but account metadata write failed.[/yellow]\n"
                f"Details: {e}"
            )
    else:
        from ....auth import clear_account_metadata

        try:
            clear_account_metadata(storage_path)
        except OSError as e:
            logger.warning("Failed to clear stale account metadata for %s: %s", storage_path, e)

    saved_msg = f"\n[green]Authentication saved to:[/green] {storage_path}"
    if email:
        saved_msg += f"\n[green]Account:[/green] {email}"
    console.print(saved_msg)

    # Verify that cookies work.
    try:
        run_async(fetch_tokens_with_domains(storage_path, profile))
        logger.info("Cookies verified successfully")
        console.print("[green]Cookies verified successfully.[/green]")
    except ValueError as e:
        # Cookie validation failed - the extracted cookies are invalid
        logger.error("Extracted cookies are invalid: %s", e)
        console.print(
            "[red]Warning: Extracted cookies failed validation.[/red]\n"
            "The cookies may be expired or malformed.\n"
            f"Error: {e}\n\n"
            "Saved anyway, but you may need to re-run login if these are invalid."
        )
    except httpx.RequestError as e:
        # Network error - can't verify but cookies might be OK
        logger.warning("Could not verify cookies due to network error: %s", e)
        console.print(
            "[yellow]Warning: Could not verify cookies (network issue).[/yellow]\n"
            "Cookies saved but may not be working.\n"
            "Try running 'notebooklm ask' to test authentication."
        )
    except Exception as e:
        # Unexpected error - log it fully
        logger.exception("Unexpected error verifying cookies: %s: %s", type(e).__name__, e)
        console.print(
            f"[yellow]Warning: Unexpected error during verification: {e}[/yellow]\n"
            "Cookies saved but please verify with 'notebooklm auth check --test'"
        )

    _sync_server_language_to_config()


def _sync_server_language_to_config() -> None:
    """Fetch server language setting and persist to local config.

    Called after login to ensure the local config reflects the server's
    global language setting. This prevents generate commands from defaulting
    to 'en' when the user has configured a different language on the server.

    Non-critical: logs errors at debug level to avoid blocking login.
    """

    async def _fetch() -> Any:
        async with await NotebookLMClient.from_storage() as client:
            return await client.settings.get_output_language()

    try:
        server_lang = run_async(_fetch())
        if server_lang:
            set_language(server_lang)
    except Exception as e:
        logger.debug("Failed to sync server language to config: %s", e)
        console.print(
            "[dim]Warning: Could not sync language setting. "
            "Run 'notebooklm language get' to sync manually.[/dim]"
        )
