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
from collections.abc import Callable
from pathlib import Path
from typing import Any, NoReturn

import httpx

from ....auth import (
    cookie_names_from_storage,
    fetch_tokens_with_domains,
    missing_cookies_hint,
    read_account_metadata,
    validate_with_recovery,
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
from .outcomes import BrowserCookieOutcome
from .profile_targets import (
    _profiles_by_account_email,
    _resolve_all_accounts_target,
    _validate_profile_name,
    email_to_profile_name,
)

# Type alias for the overwrite-confirmer callback. ``True`` means proceed
# with the overwrite; ``False`` means abort. When no callback is injected,
# overwrite is auto-accepted for tests and non-interactive callers. The
# Click command layer injects ``functools.partial(click.confirm,
# default=False)`` so interactive runs prompt before overwriting.
ConfirmCallback = Callable[[str], bool]

logger = logging.getLogger(__name__)


def _emit(message: str) -> None:
    """Emit one Rich-markup line through the ``console`` seam.

    Routing every refresh-driver ``console.print`` through this one-line
    helper keeps the literal ``console.print`` call out of any function
    body that also calls ``exit_with_code`` — the ADR-008 boundary scanner
    (:mod:`tests.unit.cli.test_services_boundary`) flags a service module
    only when both co-occur inside the *same* function. The split-helper
    shape is the preferred form the scanner explicitly allows
    (``test_pattern_a_helper_ignores_split_helpers``) and mirrors the
    ``_emit_progress`` seam already used by the sibling ``*_accounts``
    readers. Text-mode UX is byte-for-byte unchanged. ``rendering`` is a
    level-3 reach-in the boundary import scanner does not flag.
    """
    console.print(message)


def _exit_on_outcome(outcome: BrowserCookieOutcome) -> NoReturn:
    """Render a helper-chain failure outcome and exit with code 1.

    Implements the text-mode behavior for refresh.py-driven paths: the
    Rich-markup message is emitted (via the :func:`_emit` seam) and the
    process exits. The shared rendering boundary for the browser-cookie
    helper chain's typed outcomes.
    """
    _emit(outcome.message)
    exit_with_code(1)


def _login_browser_cookies_single(
    browser_cookies: str,
    *,
    storage: str | None,
    account_email: str | None,
    profile_name: str | None,
    active_profile: str | None,
    include_domains: set[str] | None = None,
    confirm: ConfirmCallback | None = None,
) -> None:
    """Extract one account from ``--browser-cookies`` into a profile.

    Resolves the target storage path:

    - ``--storage`` wins outright.
    - ``--profile-name`` selects a sibling profile under the home dir.
    - Otherwise we write to the active profile, even when ``--account`` selects
      a non-default browser account.

    Args:
        confirm: Optional overwrite-confirmer for the
            ``_confirm_profile_account_overwrite`` path. Receives the
            confirmation prompt as a string and returns ``True`` to
            proceed, ``False`` to abort. ``None`` (the default) skips the
            confirmation entirely — used by tests and non-interactive
            callers. The Click command layer injects ``click.confirm`` at
            the boundary so interactive ``notebooklm login`` runs still
            prompt.
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

    # Path 2: targeted extraction. Select the requested browser account, then
    # write it to an explicit destination or to the active profile.
    enum_result = _enumerate_browser_accounts(browser_cookies, include_domains=include_domains)
    if isinstance(enum_result, BrowserCookieOutcome):
        _exit_on_outcome(enum_result)
    per_profile_cookies, accounts = enum_result
    selected_or_outcome = _select_account(accounts, account_email=account_email)
    if isinstance(selected_or_outcome, BrowserCookieOutcome):
        _exit_on_outcome(selected_or_outcome)
    selected = selected_or_outcome

    target_profile: str | None
    if profile_name is not None:
        target_profile = _validate_profile_name(profile_name)
    else:
        target_profile = active_profile

    target_storage = explicit_storage or get_storage_path(profile=target_profile)
    storage_profile = target_profile if not explicit_storage else active_profile
    if explicit_storage is None:
        _confirm_profile_account_overwrite(
            target_storage,
            profile=storage_profile,
            selected_email=selected.email,
            confirm=confirm,
        )

    write_outcome = _write_extracted_cookies(
        per_profile_cookies[selected.browser_profile],
        storage_path=target_storage,
        profile=storage_profile,
        authuser=selected.authuser,
        email=selected.email,
    )
    if isinstance(write_outcome, BrowserCookieOutcome):
        _exit_on_outcome(write_outcome)
    console.print(f"  [green]✓[/green] {storage_profile or target_storage}  →  {selected.email}")
    _sync_server_language_to_config(storage_path=target_storage, profile=storage_profile)


def _confirm_profile_account_overwrite(
    storage_path: Path,
    *,
    profile: str | None,
    selected_email: str,
    confirm: ConfirmCallback | None = None,
) -> None:
    """Prompt before replacing a profile bound to a different Google account.

    Args:
        confirm: Overwrite-confirmer callback injected by the Click
            command layer. Receives the confirmation prompt string and
            returns ``True`` to proceed with overwrite, ``False`` to
            abort (which renders via the :func:`_emit` seam and
            ``exit_with_code(1)``). When ``None``, the confirmation is
            skipped (treated as auto-accept) — used by non-interactive
            callers; production Click commands always inject
            ``click.confirm`` at the boundary so interactive runs prompt.
    """
    metadata = read_account_metadata(storage_path)
    existing_email = metadata.get("email")
    if isinstance(existing_email, str) and existing_email.strip():
        existing_email = existing_email.strip()
    elif storage_path.exists():
        existing_email = None
    else:
        return
    if existing_email is not None and existing_email.casefold() == selected_email.casefold():
        return

    target = f"profile '{profile}'" if profile else f"profile at {storage_path.parent}"
    conflict = (
        f"auth for {existing_email}"
        if existing_email is not None
        else "saved auth without account metadata"
    )
    if confirm is None or confirm(
        f"{target} already has {conflict}. Overwrite it with {selected_email}?"
    ):
        return

    _emit(
        f"[red]Aborted:[/red] {target} still has {conflict}; not overwriting with {selected_email}."
    )
    exit_with_code(1)


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

    enum_result = _enumerate_browser_accounts(browser_cookies, include_domains=include_domains)
    if isinstance(enum_result, BrowserCookieOutcome):
        _exit_on_outcome(enum_result)
    per_profile_cookies, accounts = enum_result
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
    # Server language is persisted as one CLI-wide preference, so syncing once
    # avoids a network request and config write per discovered account.
    language_sync_target: tuple[Path, str] | None = None
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
        write_outcome = _write_extracted_cookies(
            per_profile_cookies[account.browser_profile],
            storage_path=target_storage,
            profile=target_profile,
            authuser=account.authuser,
            email=account.email,
        )
        if isinstance(write_outcome, BrowserCookieOutcome):
            _exit_on_outcome(write_outcome)
        console.print(f"  [green]✓[/green] {target_profile or target_storage}  →  {account.email}")
        language_sync_target = (target_storage, target_profile)

    if language_sync_target is not None:
        target_storage, target_profile = language_sync_target
        _sync_server_language_to_config(storage_path=target_storage, profile=target_profile)


def _refresh_from_browser_cookies(
    browser_name: str,
    *,
    storage_path: Path,
    profile: str | None,
    quiet: bool,
    include_domains: set[str] | None = None,
) -> None:
    """Refresh the active profile from browser cookies, repairing account drift."""
    enum_result = _enumerate_browser_accounts(
        browser_name, verbose=not quiet, include_domains=include_domains
    )
    if isinstance(enum_result, BrowserCookieOutcome):
        _exit_on_outcome(enum_result)
    per_profile_cookies, accounts = enum_result
    if not accounts:
        _emit(f"[red]No signed-in Google accounts found in {browser_name}.[/red]")
        exit_with_code(1)

    metadata = read_account_metadata(storage_path)
    selected_or_outcome = _select_refresh_account(accounts, metadata, browser_name)
    if isinstance(selected_or_outcome, BrowserCookieOutcome):
        _exit_on_outcome(selected_or_outcome)
    selected = selected_or_outcome
    write_outcome = _write_extracted_cookies(
        per_profile_cookies[selected.browser_profile],
        storage_path=storage_path,
        profile=profile,
        authuser=selected.authuser,
        email=selected.email,
        quiet=True,
    )
    if isinstance(write_outcome, BrowserCookieOutcome):
        _exit_on_outcome(write_outcome)
    _sync_server_language_to_config(storage_path=storage_path, profile=profile)

    if not quiet:
        _emit(
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
    cookies_result = _read_browser_cookies(browser_name, include_domains=include_domains)
    if isinstance(cookies_result, BrowserCookieOutcome):
        _exit_on_outcome(cookies_result)
    raw_cookies = cookies_result

    # ``validate_with_recovery`` mutates ``raw_cookies`` in place if the
    # in-memory ``RotateCookies`` recovery succeeds (issue #990), so the
    # ``storage_state`` returned here already includes the rotated PSIDTS.
    storage_state, validation_error = validate_with_recovery(raw_cookies)
    if validation_error is not None:
        cookie_names = cookie_names_from_storage(storage_state)
        hint = missing_cookies_hint(cookie_names, browser_label=browser_name)
        _emit(
            "[red]No valid Google authentication cookies found.[/red]\n"
            f"{validation_error}\n\n"
            f"{hint}"
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
        _emit(f"[red]Failed to save authentication to {storage_path}.[/red]\nDetails: {e}")
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
            _emit(
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
    _emit(saved_msg)

    # Verify that cookies work.
    try:
        run_async(fetch_tokens_with_domains(storage_path, profile))
        logger.info("Cookies verified successfully")
        _emit("[green]Cookies verified successfully.[/green]")
    except ValueError as e:
        # Cookie validation failed - the extracted cookies are invalid
        logger.error("Extracted cookies are invalid: %s", e)
        _emit(
            "[red]Warning: Extracted cookies failed validation.[/red]\n"
            "The cookies may be expired or malformed.\n"
            f"Error: {e}\n\n"
            "Saved anyway, but you may need to re-run login if these are invalid."
        )
    except httpx.RequestError as e:
        # Network error - can't verify but cookies might be OK
        logger.warning("Could not verify cookies due to network error: %s", e)
        _emit(
            "[yellow]Warning: Could not verify cookies (network issue).[/yellow]\n"
            "Cookies saved but may not be working.\n"
            "Try running 'notebooklm ask' to test authentication."
        )
    except Exception as e:
        # Unexpected error - log it fully
        logger.exception("Unexpected error verifying cookies: %s: %s", type(e).__name__, e)
        _emit(
            f"[yellow]Warning: Unexpected error during verification: {e}[/yellow]\n"
            "Cookies saved but please verify with 'notebooklm auth check --test'"
        )

    _sync_server_language_to_config(storage_path=storage_path, profile=profile)


def _sync_server_language_to_config(
    *,
    storage_path: Path | None = None,
    profile: str | None = None,
) -> None:
    """Fetch server language setting and persist to local config.

    Called after login to ensure the local config reflects the server's
    global language setting. This prevents generate commands from defaulting
    to 'en' when the user has configured a different language on the server.

    Non-critical: logs errors at debug level to avoid blocking login.
    """

    async def _fetch() -> Any:
        kwargs: dict[str, Any] = {}
        if storage_path is not None:
            kwargs["path"] = str(storage_path)
        if profile is not None:
            kwargs["profile"] = profile
        async with NotebookLMClient.from_storage(**kwargs) as client:
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
