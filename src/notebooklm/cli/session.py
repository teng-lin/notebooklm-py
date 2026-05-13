"""Session and context management CLI commands.

Commands:
    login   Log in to NotebookLM via browser
    use     Set the current notebook context
    status  Show current context
    clear   Clear current notebook context
"""

import asyncio
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import click
import httpx
from rich.table import Table

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext, Page
    from rich.console import Console

from .._env import get_base_host, get_base_url
from ..auth import (
    ALLOWED_COOKIE_DOMAINS,
    GOOGLE_REGIONAL_CCTLDS,
    convert_rookiepy_cookies_to_storage_state,
    extract_cookies_from_storage,
    fetch_tokens_with_domains,
    read_account_metadata,
)
from ..client import NotebookLMClient
from ..paths import (
    get_browser_profile_dir,
    get_context_path,
    get_path_info,
    get_storage_path,
)
from .helpers import (
    clear_context,
    console,
    get_auth_tokens,
    get_current_notebook,
    json_output_response,
    resolve_notebook_id,
    run_async,
    set_current_notebook,
)
from .language import set_language
from .profile import _validate_profile_name, email_to_profile_name

logger = logging.getLogger(__name__)

GOOGLE_ACCOUNTS_URL = "https://accounts.google.com/"

# Retryable Playwright connection errors
RETRYABLE_CONNECTION_ERRORS = ("ERR_CONNECTION_CLOSED", "ERR_CONNECTION_RESET")
LOGIN_MAX_RETRIES = 3
# Playwright TargetClosedError substring — matches the default message from
# Playwright's TargetClosedError class (introduced in v1.41). If a future
# version changes this message, the error will propagate unhandled (safe fallback).
TARGET_CLOSED_ERROR = "Target page, context or browser has been closed"
_NAVIGATION_INTERRUPTED_MARKERS = (
    "navigation interrupted",
    "interrupted by another navigation",
)
BROWSER_CLOSED_HELP = (
    "[red]The browser window was closed during login.[/red]\n"
    "This can happen when switching Google accounts in a persistent browser session.\n\n"
    "Try:\n"
    "  1. Run: notebooklm login --fresh\n"
    "  2. Or run: notebooklm auth logout && notebooklm login"
)


def _connection_error_help() -> str:
    """Return login connection troubleshooting text for the configured host."""
    base_host = get_base_host()
    return (
        "[red]Failed to connect to NotebookLM after multiple retries.[/red]\n"
        "This may be caused by:\n"
        "  • Network connectivity issues\n"
        f"  • Firewall or VPN blocking {base_host}\n"
        "  • Corporate proxy interfering with the connection\n"
        "  • Google rate limiting (too many login attempts)\n\n"
        "Try:\n"
        "  1. Check your internet connection\n"
        "  2. Disable VPN/proxy temporarily\n"
        "  3. Wait a few minutes before retrying\n"
        f"  4. Check if {base_host} is accessible in your browser"
    )


def _is_navigation_interrupted_error(error: str | Exception) -> bool:
    """Return True for Playwright navigation races that are safe to ignore."""
    error_str = str(error).lower()
    return any(marker in error_str for marker in _NAVIGATION_INTERRUPTED_MARKERS)


def _url_matches_base_host(url: str) -> bool:
    """Return True when ``url`` is on the configured NotebookLM host."""
    current_host = (urlparse(url).hostname or "").lower()
    return current_host == get_base_host().lower()


# Browsers launched via Playwright's `channel` parameter (system-installed,
# not the bundled Chromium). Maps channel name -> (display label, install URL).
# Used for the --browser option, the launch banner, and the not-installed
# error path. The bundled "chromium" choice is intentionally absent.
_CHANNEL_BROWSERS: dict[str, tuple[str, str]] = {
    "msedge": ("Microsoft Edge", "https://www.microsoft.com/edge"),
    "chrome": ("Google Chrome", "https://www.google.com/chrome"),
}


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


def _handle_rookiepy_error(e: Exception, browser_name: str) -> None:
    """Print a user-friendly error for rookiepy exceptions."""
    msg = str(e).lower()
    if "lock" in msg or "database" in msg:
        console.print(
            f"[red]Could not read {browser_name} cookies: browser database is locked.[/red]\n"
            "Close your browser and try again."
        )
    elif "permission" in msg or "access" in msg:
        console.print(
            f"[red]Permission denied reading {browser_name} cookies.[/red]\n"
            "You may need to grant Terminal/Python access to your browser profile directory."
        )
    elif "keychain" in msg or "decrypt" in msg:
        console.print(
            f"[red]Could not decrypt {browser_name} cookies.[/red]\n"
            "On macOS, allow Keychain access when prompted, or try a different browser."
        )
    else:
        console.print(f"[red]Failed to read cookies from {browser_name}:[/red] {e}")


def _enumerate_browser_accounts(
    browser_name: str, *, verbose: bool = True
) -> tuple[list[Any], list[Any]]:
    """Read cookies from ``browser_name`` and discover signed-in accounts.

    Args:
        browser_name: rookiepy browser alias.
        verbose: Forwarded to :func:`_read_browser_cookies` to suppress the
            human-readable progress line in JSON-output paths.

    Returns:
        ``(raw_cookies, accounts)`` — the original rookiepy cookies and the
        list of :class:`notebooklm.auth.Account` records discovered via
        per-index ``?authuser=N`` probing.

    Raises:
        SystemExit: On rookiepy failure, missing required cookies, or
            authuser=0 not returning a signed-in account.
    """
    from ..auth import (
        build_cookie_jar,
        enumerate_accounts,
        extract_cookies_with_domains,
    )

    raw_cookies = _read_browser_cookies(browser_name, verbose=verbose)
    storage_state = convert_rookiepy_cookies_to_storage_state(raw_cookies)
    try:
        extract_cookies_from_storage(storage_state)
    except ValueError as e:
        console.print(
            "[red]No valid Google authentication cookies found.[/red]\n"
            f"{e}\n\n"
            "Make sure you are logged into Google in your browser."
        )
        raise SystemExit(1) from None

    cookie_map = extract_cookies_with_domains(storage_state)
    jar = build_cookie_jar(cookies=cookie_map)
    try:
        accounts = run_async(enumerate_accounts(jar))
    except ValueError:
        # Cookies are present but Google rejected them (passive sign-in
        # redirected to the account chooser, or RotateCookies returned 401).
        # The on-disk cookie store the browser persists is too stale for
        # Google to refresh server-side. User has to refresh it themselves.
        console.print(
            f"[red]Account discovery failed: {browser_name}'s saved cookies are "
            f"too stale for Google to re-authenticate.[/red]\n\n"
            "Refresh them by opening the browser and visiting a Google site "
            "(e.g. https://notebooklm.google.com), then re-run this command.\n\n"
            "If the browser is signed out, sign back in there first.\n"
            "If you'd rather skip the browser entirely, use "
            "[cyan]notebooklm login[/cyan] (Playwright flow)."
        )
        raise SystemExit(1) from None
    except httpx.RequestError as e:
        console.print(
            f"[red]Account discovery failed (network error):[/red] {e}\n"
            "Check your internet connection and try again."
        )
        raise SystemExit(1) from None
    return raw_cookies, accounts


def _login_browser_cookies_single(
    browser_cookies: str,
    *,
    storage: str | None,
    account_email: str | None,
    profile_name: str | None,
    active_profile: str | None,
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
        _login_with_browser_cookies(resolved_storage, browser_cookies, active_profile)
        return

    # Path 2: targeted extraction. We need the email to derive a profile name
    # when --profile-name is omitted.
    raw_cookies, accounts = _enumerate_browser_accounts(browser_cookies)
    selected = _select_account(accounts, account_email=account_email)

    target_profile = profile_name or email_to_profile_name(selected.email)
    if profile_name is not None:
        _validate_profile_name(target_profile)

    target_storage = explicit_storage or get_storage_path(profile=target_profile)

    _write_extracted_cookies(
        raw_cookies,
        storage_path=target_storage,
        profile=target_profile if not explicit_storage else active_profile,
        authuser=selected.authuser,
        email=selected.email,
    )


def _profiles_by_account_email(profile_names: list[str]) -> dict[str, str]:
    """Return existing profiles keyed by account metadata email."""
    from ..auth import read_account_metadata

    profiles_by_email: dict[str, str] = {}
    for profile in profile_names:
        metadata = read_account_metadata(get_storage_path(profile=profile))
        email = metadata.get("email")
        if isinstance(email, str) and email:
            # list_profiles() is sorted, so this also prefers the unsuffixed
            # profile over older duplicate suffixes such as alice-2.
            profiles_by_email.setdefault(email, profile)
    return profiles_by_email


def _next_available_profile_name(base_name: str, unavailable: set[str]) -> str:
    """Return ``base_name`` or the next ``-N`` suffix not in ``unavailable``."""
    if base_name not in unavailable:
        return base_name

    suffix = 2
    while True:
        candidate = f"{base_name}-{suffix}"
        if candidate not in unavailable:
            return candidate
        suffix += 1


def _login_all_accounts_from_browser(browser_cookies: str) -> None:
    """Extract every signed-in Google account into its own profile."""
    from ..paths import list_profiles

    raw_cookies, accounts = _enumerate_browser_accounts(browser_cookies)
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
    profiles_by_email = _profiles_by_account_email(existing_profiles)
    unavailable: set[str] = set(existing_profiles)
    claimed: set[str] = set()
    for account in accounts:
        base_name = email_to_profile_name(account.email)
        target_profile = profiles_by_email.get(account.email)
        if target_profile is None or target_profile in claimed:
            target_profile = _next_available_profile_name(base_name, unavailable | claimed)
        unavailable.add(target_profile)
        claimed.add(target_profile)

        target_storage = get_storage_path(profile=target_profile)
        _write_extracted_cookies(
            raw_cookies,
            storage_path=target_storage,
            profile=target_profile,
            authuser=account.authuser,
            email=account.email,
        )


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
        raise SystemExit(1)
    return next(a for a in accounts if a.is_default)


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
        raise SystemExit(1) from None

    try:
        storage_path.parent.mkdir(parents=True, exist_ok=True)
        storage_path.write_text(
            json.dumps(storage_state, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        if sys.platform != "win32":
            storage_path.parent.chmod(0o700)
            storage_path.chmod(0o600)
    except OSError as e:
        logger.error("Failed to save authentication to %s: %s", storage_path, e)
        console.print(f"[red]Failed to save authentication to {storage_path}.[/red]\nDetails: {e}")
        raise SystemExit(1) from None

    from ..auth import write_account_metadata

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
        raise SystemExit(1)

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
        raise SystemExit(1)

    return next((account for account in accounts if account.is_default), accounts[0])


def _refresh_from_browser_cookies(
    browser_name: str,
    *,
    storage_path: Path,
    profile: str | None,
    quiet: bool,
) -> None:
    """Refresh the active profile from browser cookies, repairing account drift."""
    raw_cookies, accounts = _enumerate_browser_accounts(browser_name, verbose=not quiet)
    if not accounts:
        console.print(f"[red]No signed-in Google accounts found in {browser_name}.[/red]")
        raise SystemExit(1)

    metadata = read_account_metadata(storage_path)
    selected = _select_refresh_account(accounts, metadata, browser_name)
    _write_extracted_cookies(
        raw_cookies,
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


def _build_google_cookie_domains() -> list[str]:
    """Return the list of Google cookie domains we ask cookie extractors to read."""
    domains = list(ALLOWED_COOKIE_DOMAINS)
    for cctld in GOOGLE_REGIONAL_CCTLDS:
        domain = f".google.{cctld}"
        if domain not in domains:
            domains.append(domain)
    return domains


def _read_firefox_container_cookies(
    container_spec: str, *, verbose: bool = True
) -> list[dict[str, Any]]:
    """Load Google cookies from a specific Firefox Multi-Account Container.

    Bypasses rookiepy because rookiepy 0.5.6 does not filter on
    ``originAttributes`` and silently merges every container's cookies (see
    issue #366 / #367). We talk to ``cookies.sqlite`` directly via the
    helpers in :mod:`notebooklm._firefox_containers`.

    Args:
        container_spec: The part after ``firefox::`` (e.g. ``"Work"`` or
            ``"none"`` for the no-container default).
        verbose: When False, suppress the progress line; used by
            ``auth inspect --json``.

    Returns:
        Rookiepy-shape cookie dicts (compatible with
        :func:`convert_rookiepy_cookies_to_storage_state`).

    Raises:
        SystemExit: With a friendly message on any failure (no Firefox
            installed, unknown container, locked DB, …).
    """
    from .._firefox_containers import (
        extract_firefox_container_cookies,
        find_firefox_profile_path,
        resolve_container_id,
    )

    profile_path = find_firefox_profile_path()
    if profile_path is None:
        console.print(
            "[red]Could not locate a Firefox profile.[/red]\n"
            "Looked for profiles.ini in the standard Firefox locations. "
            "If you have Firefox installed in a non-standard location, the "
            "container-aware extractor cannot find it. Drop the '::<container>' "
            "suffix to fall back to rookiepy's autodetection."
        )
        raise SystemExit(1)

    try:
        container_id = resolve_container_id(profile_path, container_spec)
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        raise SystemExit(1) from None

    if verbose:
        if container_id == "none":
            console.print("[yellow]Reading cookies from Firefox (no container)...[/yellow]")
        else:
            console.print(
                f"[yellow]Reading cookies from Firefox container "
                f"'{container_spec}' (userContextId={container_id})...[/yellow]"
            )

    domains = _build_google_cookie_domains()
    try:
        return extract_firefox_container_cookies(profile_path, container_id, domains=domains)
    except FileNotFoundError as e:
        console.print(f"[red]{e}[/red]")
        raise SystemExit(1) from None
    except (OSError, RuntimeError) as e:
        _handle_rookiepy_error(e, "firefox")
        raise SystemExit(1) from None
    except sqlite3.DatabaseError as e:
        console.print(f"[red]Failed to read Firefox cookies database:[/red] {e}")
        raise SystemExit(1) from None


def _maybe_warn_firefox_containers_in_use() -> None:
    """Emit a one-line warning when unscoped ``firefox`` is risky.

    Triggers when ``cookies.sqlite`` has at least one row whose
    ``originAttributes`` carries a ``userContextId=`` field — i.e. the user
    really stored cookies inside some container. Cookie-driven (not
    ``containers.json``-driven) so stock built-in containers count just the
    same as user-created ones; First-Party-Isolation cookies (which only
    carry ``firstPartyDomain=``) do not trigger.

    Any probe failure is swallowed inside ``has_container_cookies_in_use``.
    """
    from .._firefox_containers import (
        find_firefox_profile_path,
        has_container_cookies_in_use,
    )

    profile_path = find_firefox_profile_path()
    if profile_path is None:
        return
    if has_container_cookies_in_use(profile_path):
        console.print(
            "[yellow]Warning: this Firefox profile has cookies stored inside "
            "a Multi-Account Container, but '--browser-cookies firefox' "
            "merges every container into one jar. If your Google session "
            "lives inside a container, re-run with "
            "[cyan]--browser-cookies 'firefox::<container-name>'[/cyan] "
            "(or [cyan]'firefox::none'[/cyan] for the no-container "
            "default).[/yellow]"
        )


def _read_browser_cookies(browser_name: str, *, verbose: bool = True) -> list[dict[str, Any]]:
    """Load Google cookies from an installed browser via rookiepy.

    Wraps rookiepy import + dispatch + error handling so multiple commands
    (``login --browser-cookies``, ``auth inspect``) share one code path.

    Args:
        browser_name: ``"auto"`` to use ``rookiepy.load()``, a specific
            browser alias from :data:`_ROOKIEPY_BROWSER_ALIASES`, or
            ``"firefox::<container-name>"`` (or ``"firefox::none"``) to
            extract from a single Firefox Multi-Account Container, bypassing
            rookiepy entirely.
        verbose: When False, suppress the "Reading cookies from …" progress
            line. Used by ``auth inspect --json`` to keep stdout pure JSON.

    Returns:
        Raw cookie dicts as returned by rookiepy (or by the Firefox
        container extractor, which mirrors rookiepy's shape).

    Raises:
        SystemExit: With a user-friendly message printed to console on any
            rookiepy import / dispatch / read failure.
    """
    # Firefox container syntax: ``firefox::<name>`` or ``firefox::none``.
    # Routed to a direct sqlite3 reader because rookiepy does not honor
    # ``originAttributes`` — see issue #367.
    if browser_name.lower().startswith("firefox::"):
        container_spec = browser_name.split("::", 1)[1].strip()
        if not container_spec:
            # Empty spec would silently fall through to an unfiltered SELECT —
            # i.e. the merged-jar bug this feature exists to prevent. Reject.
            console.print(
                "[red]Empty Firefox container specifier in --browser-cookies.[/red]\n"
                "Use [cyan]firefox::<container-name>[/cyan] (e.g. 'firefox::Work') or "
                "[cyan]firefox::none[/cyan] for the no-container default."
            )
            raise SystemExit(1)
        return _read_firefox_container_cookies(container_spec, verbose=verbose)

    try:
        import rookiepy
    except ImportError:
        console.print(
            "[red]rookiepy is not installed.[/red]\n"
            "Install it with:\n"
            "  pip install 'notebooklm-py[cookies]'\n"
            "or directly:\n"
            "  pip install rookiepy"
        )
        raise SystemExit(1) from None

    domains = _build_google_cookie_domains()

    if browser_name == "auto":
        if verbose:
            console.print(
                "[yellow]Reading cookies from installed browser (auto-detect)...[/yellow]"
            )
        try:
            return rookiepy.load(domains=domains)
        except (OSError, RuntimeError) as e:
            _handle_rookiepy_error(e, "auto-detect")
            raise SystemExit(1) from None

    canonical = _ROOKIEPY_BROWSER_ALIASES.get(browser_name.lower())
    if canonical is None:
        console.print(
            f"[red]Unknown browser: '{browser_name}'[/red]\n"
            f"Supported: {', '.join(sorted(_ROOKIEPY_BROWSER_ALIASES))}"
        )
        raise SystemExit(1)
    if verbose:
        console.print(f"[yellow]Reading cookies from {browser_name}...[/yellow]")
    browser_fn = getattr(rookiepy, canonical, None)
    if browser_fn is None or not callable(browser_fn):
        console.print(
            f"[red]rookiepy does not support '{canonical}' on this platform.[/red]\n"
            "Check that rookiepy is properly installed: pip install rookiepy"
        )
        raise SystemExit(1)
    try:
        cookies = browser_fn(domains=domains)
    except (OSError, RuntimeError) as e:
        _handle_rookiepy_error(e, browser_name)
        raise SystemExit(1) from None

    # Back-compat warning: unscoped 'firefox' silently merges cookies from
    # every Multi-Account Container. Skip when ``verbose=False`` so callers
    # like ``auth inspect --json`` don't pollute stdout before their JSON.
    if canonical == "firefox" and verbose:
        _maybe_warn_firefox_containers_in_use()

    return cookies


def _login_with_browser_cookies(
    storage_path: Path,
    browser_name: str,
    profile: str | None = None,
    *,
    authuser: int = 0,
    email: str | None = None,
) -> None:
    """Extract Google cookies from an installed browser via rookiepy.

    Args:
        storage_path: Where to write storage_state.json.
        browser_name: "auto" to use rookiepy.load(), or a specific browser name.
        profile: Profile name (forwarded to verification step).
        authuser: Internal Google account index fallback for this profile.
        email: Optional account email to record for stable routing.
    """
    raw_cookies = _read_browser_cookies(browser_name)

    storage_state = convert_rookiepy_cookies_to_storage_state(raw_cookies)
    try:
        extract_cookies_from_storage(storage_state)  # validates SID is present
    except ValueError as e:
        console.print(
            "[red]No valid Google authentication cookies found.[/red]\n"
            f"{e}\n\n"
            "Make sure you are logged into Google in your browser."
        )
        raise SystemExit(1) from None

    # Create parent directory (avoid mode= on Windows to prevent ACL issues)
    try:
        storage_path.parent.mkdir(parents=True, exist_ok=True)
        storage_path.write_text(
            json.dumps(storage_state, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        if sys.platform != "win32":
            # On Unix: ensure both directory and file have restrictive permissions
            storage_path.parent.chmod(0o700)
            storage_path.chmod(0o600)
    except OSError as e:
        logger.error("Failed to save authentication to %s: %s", storage_path, e)
        console.print(f"[red]Failed to save authentication to {storage_path}.[/red]\nDetails: {e}")
        raise SystemExit(1) from None

    # Record account metadata so future calls target the same Google account.
    # Even on a default-account login (authuser=0, no email), remove stale
    # metadata so refreshed cookies cannot keep routing to an older account.
    if authuser or email:
        from ..auth import write_account_metadata

        try:
            write_account_metadata(storage_path, authuser=authuser, email=email)
        except OSError as e:
            logger.error("Failed to save account metadata for %s: %s", storage_path, e)
            console.print(
                f"[yellow]Warning: cookies saved but account metadata write failed.[/yellow]\n"
                f"Details: {e}"
            )
    else:
        from ..auth import clear_account_metadata

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
        logger.warning("Unexpected error verifying cookies: %s: %s", type(e).__name__, e)
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

    async def _fetch():
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


@contextmanager
def _windows_playwright_event_loop() -> Iterator[None]:
    """Temporarily restore default event loop policy for Playwright on Windows.

    Playwright's sync API uses subprocess to spawn the browser, which requires
    ProactorEventLoop on Windows. However, we set WindowsSelectorEventLoopPolicy
    globally to fix CLI hanging issues (#79). This context manager temporarily
    restores the default policy for Playwright, then switches back.

    On non-Windows platforms, this is a no-op.

    Yields:
        None

    Example:
        with _windows_playwright_event_loop():
            with sync_playwright() as p:
                # Browser operations work on Windows
                ...
    """
    if sys.platform != "win32":
        yield
        return

    # Save current policy and restore default (ProactorEventLoop) for Playwright
    original_policy = asyncio.get_event_loop_policy()
    asyncio.set_event_loop_policy(asyncio.DefaultEventLoopPolicy())
    try:
        yield
    finally:
        # Restore WindowsSelectorEventLoopPolicy for other async operations
        asyncio.set_event_loop_policy(original_policy)


def _ensure_chromium_installed() -> None:
    """Check if Chromium is installed and install if needed.

    This pre-flight check runs `playwright install --dry-run chromium` to detect
    if the browser needs installation, then auto-installs if necessary.

    Silently proceeds on any errors - Playwright will handle them during launch.
    """
    try:
        result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "--dry-run", "chromium"],
            capture_output=True,
            text=True,
        )
        # Check if dry-run indicates browser needs installing
        stdout_lower = result.stdout.lower()
        if "chromium" not in stdout_lower or "will download" not in stdout_lower:
            return

        console.print("[yellow]Chromium browser not installed. Installing now...[/yellow]")
        install_result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            capture_output=True,
            text=True,
        )
        if install_result.returncode != 0:
            console.print(
                "[red]Failed to install Chromium browser.[/red]\n"
                f'Run manually: "{sys.executable}" -m playwright install chromium'
            )
            raise SystemExit(1)
        console.print("[green]Chromium installed successfully.[/green]\n")
    except SystemExit:
        raise
    except Exception as e:
        # FileNotFoundError: playwright CLI not found but sync_playwright imported
        # Other exceptions: dry-run check failed - let Playwright handle it during launch
        console.print(
            f"[dim]Warning: Chromium pre-flight check failed: {e}. Proceeding anyway.[/dim]"
        )


def _recover_page(context: "BrowserContext", console: "Console") -> "Page":
    """Get a fresh page from a persistent browser context.

    Used when the current page reference is stale (TargetClosedError).
    A new page in a persistent context inherits all cookies and storage.

    Returns a new Page, or raises SystemExit if the context/browser is dead.
    Raises the original PlaywrightError for non-TargetClosed failures.
    """
    from playwright.sync_api import Error as PlaywrightError

    try:
        return context.new_page()
    except PlaywrightError as exc:
        error_str = str(exc)
        if TARGET_CLOSED_ERROR in error_str:
            logger.error("Browser context is dead, cannot recover page: %s", error_str)
            console.print(BROWSER_CLOSED_HELP)
            raise SystemExit(1) from exc
        # Not a TargetClosedError — don't mask the real problem
        logger.error("Failed to create new page for recovery: %s", error_str)
        raise


def register_session_commands(cli):
    """Register session commands on the main CLI group."""

    @cli.command("login")
    @click.option(
        "--storage",
        type=click.Path(),
        default=None,
        help="Where to save storage_state.json (default: profile-specific location)",
    )
    @click.option(
        "--browser",
        type=click.Choice(["chromium", *_CHANNEL_BROWSERS], case_sensitive=False),
        default="chromium",
        help=(
            "Browser to use for login (default: chromium). "
            "Use 'chrome' for system Google Chrome (workaround when bundled "
            "Chromium crashes, e.g. macOS 15+), 'msedge' for Microsoft Edge."
        ),
    )
    @click.option(
        "--browser-cookies",
        "browser_cookies",
        default=None,
        is_flag=False,
        flag_value="auto",
        help=(
            "Read cookies from an installed browser instead of launching Playwright. "
            "Optionally specify browser: chrome, firefox, brave, edge, safari, arc, ... "
            "For Firefox Multi-Account Containers, target a specific container with "
            "'firefox::<container-name>' (or 'firefox::none' for the default). "
            "Requires: pip install 'notebooklm-py[cookies]'"
        ),
    )
    @click.option(
        "--account",
        "account_email",
        default=None,
        help=(
            "Pick a signed-in Google account by email when several are present "
            "in the browser. Only valid with --browser-cookies."
        ),
    )
    @click.option(
        "--all-accounts",
        "all_accounts",
        is_flag=True,
        default=False,
        help=(
            "Extract every Google account signed in to the browser into its own "
            "profile (auto-named from each account's email). Only valid with "
            "--browser-cookies."
        ),
    )
    @click.option(
        "--profile-name",
        "profile_name",
        default=None,
        help=(
            "Name to give the new profile when extracting a non-default account. "
            "Defaults to the account email's local-part. Only valid with "
            "--browser-cookies."
        ),
    )
    @click.option(
        "--fresh",
        is_flag=True,
        default=False,
        help="Start with a clean browser session (deletes cached browser profile). Use to switch Google accounts.",
    )
    @click.pass_context
    def login(
        ctx,
        storage,
        browser,
        browser_cookies,
        account_email,
        all_accounts,
        profile_name,
        fresh,
    ):
        """Log in to NotebookLM via browser.

        Opens a browser window for Google login. Authentication is saved
        automatically once login is detected (no terminal interaction needed).

        Use --browser chrome if the bundled Chromium crashes (e.g. macOS 15+).
        Use --browser msedge if your organization requires Microsoft Edge for SSO.

        Note: Cannot be used when NOTEBOOKLM_AUTH_JSON is set (use file-based
        auth or unset the env var first).
        """
        # Check for conflicting env var
        if os.environ.get("NOTEBOOKLM_AUTH_JSON"):
            console.print(
                "[red]Error: Cannot run 'login' when NOTEBOOKLM_AUTH_JSON is set.[/red]\n"
                "The NOTEBOOKLM_AUTH_JSON environment variable provides inline authentication,\n"
                "which conflicts with browser-based login that saves to a file.\n\n"
                "Either:\n"
                "  1. Unset NOTEBOOKLM_AUTH_JSON and run 'login' again\n"
                "  2. Continue using NOTEBOOKLM_AUTH_JSON for authentication"
            )
            raise SystemExit(1)

        if browser_cookies is None and (
            account_email is not None or all_accounts or profile_name is not None
        ):
            console.print(
                "[red]Error: --account, --all-accounts, and --profile-name "
                "require --browser-cookies.[/red]"
            )
            raise SystemExit(1)
        if all_accounts and (account_email is not None or profile_name is not None):
            console.print(
                "[red]Error: --all-accounts cannot be combined with "
                "--account or --profile-name.[/red]"
            )
            raise SystemExit(1)
        if all_accounts and storage:
            console.print(
                "[red]Error: --all-accounts writes one profile per account "
                "and cannot be combined with --storage.[/red]"
            )
            raise SystemExit(1)

        # rookiepy fast-path: skip Playwright entirely
        if browser_cookies is not None:
            if fresh:
                console.print(
                    "[yellow]Warning: --fresh has no effect with --browser-cookies "
                    "(no browser profile is used).[/yellow]"
                )
            if all_accounts:
                _login_all_accounts_from_browser(browser_cookies)
                return
            active_profile = ctx.obj.get("profile") if ctx.obj else None
            _login_browser_cookies_single(
                browser_cookies,
                storage=storage,
                account_email=account_email,
                profile_name=profile_name,
                active_profile=active_profile,
            )
            return

        profile = ctx.obj.get("profile") if ctx.obj else None
        storage_path = (
            Path(storage)
            if storage
            else get_storage_path(profile=profile)
            if profile
            else get_storage_path()
        )
        browser_profile = get_browser_profile_dir()

        if fresh and browser_profile.exists():
            try:
                shutil.rmtree(browser_profile)
                console.print("[yellow]Cleared cached browser session (--fresh)[/yellow]")
            except OSError as exc:
                logger.error("Failed to clear browser profile %s: %s", browser_profile, exc)
                console.print(
                    f"[red]Cannot clear browser profile: {exc}[/red]\n"
                    "Close any open browser windows and try again.\n"
                    f"If the problem persists, manually delete: {browser_profile}"
                )
                raise SystemExit(1) from exc

        if sys.platform == "win32":
            # On Windows < Python 3.13, mode= is ignored by mkdir(). On
            # Python 3.13+, mode= applies Windows ACLs that can be overly
            # restrictive (0o700 blocks other same-user processes). Skip mode
            # and chmod entirely; Windows inherits ACLs from the parent.
            storage_path.parent.mkdir(parents=True, exist_ok=True)
            browser_profile.mkdir(parents=True, exist_ok=True)
        else:
            storage_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            storage_path.parent.chmod(0o700)
            browser_profile.mkdir(parents=True, exist_ok=True, mode=0o700)
            browser_profile.chmod(0o700)

        try:
            from playwright.sync_api import Error as PlaywrightError
            from playwright.sync_api import TimeoutError as PlaywrightTimeout
            from playwright.sync_api import sync_playwright
        except ImportError:
            # NOTE: passing markup=False so rich does not interpret `[browser]` as a style tag
            # (which would strip it, leaving the user with `pip install "notebooklm-py"` — no extras).
            if browser in _CHANNEL_BROWSERS:
                install_hint = '  pip install "notebooklm-py[browser]"'
            else:
                install_hint = (
                    '  pip install "notebooklm-py[browser]"\n  playwright install chromium'
                )
            console.print("[red]Playwright not installed. Run:[/red]")
            console.print(install_hint, markup=False)
            raise SystemExit(1) from None

        # Pre-flight check: verify Chromium browser is installed (system Chrome
        # and Edge are checked at launch time by Playwright's channel routing).
        if browser == "chromium":
            _ensure_chromium_installed()

        from ..paths import resolve_profile

        profile_name = resolve_profile()
        channel_info = _CHANNEL_BROWSERS.get(browser)
        browser_label = channel_info[0] if channel_info else "Chromium"
        console.print(f"[dim]Profile: {profile_name}[/dim]")
        console.print(f"[yellow]Opening {browser_label} for Google login...[/yellow]")
        console.print(f"[dim]Using persistent profile: {browser_profile}[/dim]")

        # Use context manager to restore ProactorEventLoop for Playwright on Windows
        # (fixes #89: NotImplementedError on Windows Python 3.12)
        with _windows_playwright_event_loop(), sync_playwright() as p:
            launch_kwargs: dict[str, Any] = {
                "user_data_dir": str(browser_profile),
                "headless": False,
                "args": [
                    "--disable-blink-features=AutomationControlled",
                    "--password-store=basic",  # Avoid macOS keychain encryption for headless compatibility
                ],
                "ignore_default_args": ["--enable-automation"],
            }
            if browser in _CHANNEL_BROWSERS:
                launch_kwargs["channel"] = browser

            context = None
            try:
                context = p.chromium.launch_persistent_context(**launch_kwargs)

                page = context.pages[0] if context.pages else _recover_page(context, console)

                # Retry navigation on transient connection errors with backoff
                for attempt in range(1, LOGIN_MAX_RETRIES + 1):
                    try:
                        page.goto(f"{get_base_url()}/", timeout=30000)
                        break
                    except PlaywrightError as exc:
                        error_str = str(exc)
                        is_retryable = any(
                            code in error_str for code in RETRYABLE_CONNECTION_ERRORS
                        )
                        is_target_closed = TARGET_CLOSED_ERROR in error_str

                        # Check if we should retry
                        if (is_retryable or is_target_closed) and attempt < LOGIN_MAX_RETRIES:
                            # For TargetClosedError, get a fresh page reference
                            if is_target_closed:
                                page = _recover_page(context, console)

                            backoff_seconds = attempt  # Linear backoff: 1s, 2s
                            logger.debug(
                                "Retryable error on attempt %d/%d: %s",
                                attempt,
                                LOGIN_MAX_RETRIES,
                                error_str,
                            )
                            if is_target_closed:
                                console.print(
                                    f"[yellow]Browser page closed "
                                    f"(attempt {attempt}/{LOGIN_MAX_RETRIES}). "
                                    f"Retrying with fresh page...[/yellow]"
                                )
                            else:
                                console.print(
                                    f"[yellow]Connection interrupted "
                                    f"(attempt {attempt}/{LOGIN_MAX_RETRIES}). "
                                    f"Retrying in {backoff_seconds}s...[/yellow]"
                                )
                                time.sleep(backoff_seconds)
                        elif is_target_closed:
                            # Exhausted retries on browser-closed errors
                            logger.error(
                                "Browser closed during login after %d attempts. Last error: %s",
                                LOGIN_MAX_RETRIES,
                                error_str,
                            )
                            console.print(BROWSER_CLOSED_HELP)
                            raise SystemExit(1) from exc
                        elif is_retryable:
                            # Exhausted retries on network errors
                            logger.error(
                                f"Failed to connect to NotebookLM after {LOGIN_MAX_RETRIES} attempts. "
                                f"Last error: {error_str}"
                            )
                            console.print(_connection_error_help())
                            raise SystemExit(1) from exc
                        else:
                            # Non-retryable error - re-raise immediately
                            logger.debug("Non-retryable error: %s", error_str)
                            raise

                if _url_matches_base_host(page.url):
                    # Persistent browser profile already has a valid session.
                    console.print("[green]Already logged in.[/green]")
                else:
                    console.print("\n[bold green]Instructions:[/bold green]")
                    console.print("1. Complete the Google login in the browser window")
                    console.print(
                        "2. Authentication will be saved automatically once login is detected\n"
                    )
                    console.print("[dim]Waiting for login (up to 5 minutes)...[/dim]")
                    try:
                        page.wait_for_url(f"{get_base_url()}/**", timeout=300_000)
                    except PlaywrightTimeout:
                        console.print(
                            "[red]Login not detected within 5 minutes.[/red]\n"
                            "Try again with: notebooklm login"
                        )
                        raise SystemExit(1) from None
                    except PlaywrightError as exc:
                        # Browser/tab closed during the wait. Cannot resume a
                        # partially completed SSO form, so surface the same
                        # help text other browser-closed paths use.
                        if TARGET_CLOSED_ERROR in str(exc):
                            console.print(BROWSER_CLOSED_HELP)
                            raise SystemExit(1) from exc
                        raise
                    console.print("[green]Login detected.[/green]")

                # Force .google.com cookies for regional users (e.g. UK lands on
                # .google.co.uk). Use "commit" to resolve once response headers
                # (including Set-Cookie) are processed, before any client-side
                # JS redirect can interrupt. See #214.
                for url in [GOOGLE_ACCOUNTS_URL, f"{get_base_url()}/"]:
                    try:
                        page.goto(url, wait_until="commit")
                    except PlaywrightError as exc:
                        error_str = str(exc)
                        if TARGET_CLOSED_ERROR in error_str:
                            # Page was destroyed (e.g. user switched accounts) -- get fresh page
                            page = _recover_page(context, console)
                            try:
                                page.goto(url, wait_until="commit")
                            except PlaywrightError as inner_exc:
                                if TARGET_CLOSED_ERROR in str(inner_exc):
                                    # Recovered page also dead -- context/browser is gone
                                    console.print(BROWSER_CLOSED_HELP)
                                    raise SystemExit(1) from inner_exc
                                elif not _is_navigation_interrupted_error(inner_exc):
                                    raise
                        elif not _is_navigation_interrupted_error(error_str):
                            raise

                # Defense-in-depth: wait_for_url proved we reached the host,
                # but the cookie-forcing round-trip above can land us back on
                # accounts.google.com if the session was invalidated mid-flow
                # (rare, but the old interactive path defended against this
                # via a "save anyway?" confirm). Auto-detect is non-interactive,
                # so fail fast with a clear next step instead.
                if not _url_matches_base_host(page.url):
                    console.print(
                        f"[red]Unexpected URL after login: {page.url}[/red]\n"
                        "Authentication may be incomplete. "
                        "Try: notebooklm login --fresh"
                    )
                    raise SystemExit(1)

                context.storage_state(path=str(storage_path))
                from ..auth import clear_account_metadata

                try:
                    clear_account_metadata(storage_path)
                except OSError as exc:
                    logger.warning(
                        "Failed to clear stale account metadata for %s: %s",
                        storage_path,
                        exc,
                    )
                # Restrict permissions to owner only (contains sensitive cookies)
                if sys.platform != "win32":
                    # chmod is a no-op on Windows (and can confuse ACLs)
                    storage_path.chmod(0o600)

            except Exception as e:
                # Handle browser launch errors specially (context will be None if launch failed)
                if context is None and browser in _CHANNEL_BROWSERS:
                    err = str(e).lower()
                    is_not_found = any(
                        marker in err
                        for marker in (
                            "executable doesn't exist",
                            "is not found at",
                            "no such file",
                            "failed to launch",
                        )
                    )
                    if is_not_found:
                        label, install_url = _CHANNEL_BROWSERS[browser]
                        logger.error("%s not found: %s", label, e)
                        console.print(
                            f"[red]{label} not found.[/red]\n"
                            f"Install from: {install_url}\n"
                            "Or use the default Chromium browser: notebooklm login"
                        )
                        raise SystemExit(1) from e
                logger.error("Login failed: %s", e, exc_info=True)
                raise
            finally:
                # Always close the browser context to prevent resource leaks
                if context:
                    context.close()

        console.print(f"\n[green]Authentication saved to:[/green] {storage_path}")

        # Sync server language setting to local config so generate commands
        # respect the user's global language preference (fixes #121)
        _sync_server_language_to_config()

    @cli.command("use")
    @click.argument("notebook_id")
    @click.pass_context
    def use_notebook(ctx, notebook_id):
        """Set the current notebook context.

        Once set, all commands will use this notebook by default.
        You can still override by passing --notebook explicitly.

        Supports partial IDs - 'notebooklm use abc' matches 'abc123...'

        \b
        Example:
          notebooklm use nb123
          notebooklm ask "what is this about?"   # Uses nb123
          notebooklm generate video "a fun explainer"  # Uses nb123
        """
        try:
            auth = get_auth_tokens(ctx)

            async def _get():
                async with NotebookLMClient(auth) as client:
                    # Resolve partial ID to full ID
                    resolved_id = await resolve_notebook_id(client, notebook_id)
                    nb = await client.notebooks.get(resolved_id)
                    return nb, resolved_id

            nb, resolved_id = run_async(_get())

            created_str = nb.created_at.strftime("%Y-%m-%d") if nb.created_at else None
            set_current_notebook(resolved_id, nb.title, nb.is_owner, created_str)

            table = Table()
            table.add_column("ID", style="cyan")
            table.add_column("Title", style="green")
            table.add_column("Owner")
            table.add_column("Created", style="dim")

            created = created_str or "-"
            owner_status = "Owner" if nb.is_owner else "Shared"
            table.add_row(nb.id, nb.title, owner_status, created)

            console.print(table)

        except FileNotFoundError:
            set_current_notebook(notebook_id)
            table = Table()
            table.add_column("ID", style="cyan")
            table.add_column("Title", style="green")
            table.add_column("Owner")
            table.add_column("Created", style="dim")
            table.add_row(notebook_id, "-", "-", "-")
            console.print(table)
        except click.ClickException:
            # Re-raise click exceptions (from resolve_notebook_id)
            raise
        except Exception as e:
            set_current_notebook(notebook_id)
            table = Table()
            table.add_column("ID", style="cyan")
            table.add_column("Title", style="green")
            table.add_column("Owner")
            table.add_column("Created", style="dim")
            table.add_row(notebook_id, f"Warning: {str(e)}", "-", "-")
            console.print(table)

    @cli.command("status")
    @click.option("--json", "json_output", is_flag=True, help="Output as JSON")
    @click.option("--paths", "show_paths", is_flag=True, help="Show resolved file paths")
    def status(json_output, show_paths):
        """Show current context (active notebook and conversation).

        Use --paths to see where configuration files are located
        (useful for debugging NOTEBOOKLM_HOME).
        """
        context_file = get_context_path()
        notebook_id = get_current_notebook()

        # Handle --paths flag
        if show_paths:
            path_info = get_path_info()
            if json_output:
                json_output_response({"paths": path_info})
                return

            table = Table(title="Configuration Paths")
            table.add_column("File", style="dim")
            table.add_column("Path", style="cyan")
            table.add_column("Source", style="green")

            table.add_row(
                "Profile",
                path_info.get("profile", "default"),
                path_info.get("profile_source", ""),
            )
            table.add_row("Home Directory", path_info["home_dir"], path_info["home_source"])
            table.add_row("Profile Directory", path_info.get("profile_dir", ""), "")
            table.add_row("Storage State", path_info["storage_path"], "")
            table.add_row("Context", path_info["context_path"], "")
            table.add_row("Browser Profile", path_info["browser_profile_dir"], "")

            # Show if NOTEBOOKLM_AUTH_JSON is set
            if os.environ.get("NOTEBOOKLM_AUTH_JSON"):
                console.print(
                    "[yellow]Note: NOTEBOOKLM_AUTH_JSON is set (inline auth active)[/yellow]\n"
                )

            console.print(table)
            return

        if notebook_id:
            try:
                data = json.loads(context_file.read_text(encoding="utf-8"))
                title = data.get("title", "-")
                is_owner = data.get("is_owner", True)
                created_at = data.get("created_at", "-")
                conversation_id = data.get("conversation_id")

                if json_output:
                    json_data = {
                        "has_context": True,
                        "notebook": {
                            "id": notebook_id,
                            "title": title if title != "-" else None,
                            "is_owner": is_owner,
                        },
                        "conversation_id": conversation_id,
                    }
                    json_output_response(json_data)
                    return

                table = Table(title="Current Context")
                table.add_column("Property", style="dim")
                table.add_column("Value", style="cyan")

                table.add_row("Notebook ID", notebook_id)
                table.add_row("Title", str(title))
                owner_status = "Owner" if is_owner else "Shared"
                table.add_row("Ownership", owner_status)
                table.add_row("Created", created_at)
                if conversation_id:
                    table.add_row("Conversation", conversation_id)
                else:
                    table.add_row("Conversation", "[dim]None (will auto-select on next ask)[/dim]")
                console.print(table)
            except (OSError, json.JSONDecodeError):
                if json_output:
                    json_data = {
                        "has_context": True,
                        "notebook": {
                            "id": notebook_id,
                            "title": None,
                            "is_owner": None,
                        },
                        "conversation_id": None,
                    }
                    json_output_response(json_data)
                    return

                table = Table(title="Current Context")
                table.add_column("Property", style="dim")
                table.add_column("Value", style="cyan")
                table.add_row("Notebook ID", notebook_id)
                table.add_row("Title", "-")
                table.add_row("Ownership", "-")
                table.add_row("Created", "-")
                table.add_row("Conversation", "[dim]None[/dim]")
                console.print(table)
        else:
            if json_output:
                json_data = {
                    "has_context": False,
                    "notebook": None,
                    "conversation_id": None,
                }
                json_output_response(json_data)
                return

            console.print(
                "[yellow]No notebook selected. Use 'notebooklm use <id>' to set one.[/yellow]"
            )

    @cli.command("clear")
    def clear_cmd():
        """Clear current notebook context."""
        clear_context()
        console.print("[green]Context cleared[/green]")

    @cli.group("auth")
    def auth_group():
        """Authentication management commands."""
        pass

    @auth_group.command("logout")
    def auth_logout():
        """Log out by clearing saved authentication.

        Removes both the saved cookie file (storage_state.json) and the
        cached browser profile. After logout, run 'notebooklm login' to
        authenticate with a different Google account.

        \b
        Examples:
          notebooklm auth logout           # Clear auth for active profile
          notebooklm -p work auth logout   # Clear auth for 'work' profile
        """
        # Warn if env-based auth will remain active after logout
        if os.environ.get("NOTEBOOKLM_AUTH_JSON"):
            console.print(
                "[yellow]Note: NOTEBOOKLM_AUTH_JSON is set — env-based auth will "
                "remain active after logout. Unset it to fully log out.[/yellow]"
            )

        storage_path = get_storage_path()
        browser_profile = get_browser_profile_dir()

        removed_any = False

        # Remove storage_state.json
        if storage_path.exists():
            try:
                storage_path.unlink()
                removed_any = True
            except OSError as exc:
                logger.error("Failed to remove auth file %s: %s", storage_path, exc)
                console.print(
                    f"[red]Cannot remove auth file: {exc}[/red]\n"
                    "Close any running notebooklm commands and try again.\n"
                    f"If the problem persists, manually delete: {storage_path}"
                )
                raise SystemExit(1) from exc

        # Remove browser profile directory
        if browser_profile.exists():
            try:
                shutil.rmtree(browser_profile)
                removed_any = True
            except OSError as exc:
                logger.error("Failed to remove browser profile %s: %s", browser_profile, exc)
                partial = (
                    "[yellow]Note: Auth file was removed, but browser profile "
                    "could not be deleted.[/yellow]\n"
                    if removed_any
                    else ""
                )
                console.print(
                    f"{partial}"
                    f"[red]Cannot remove browser profile: {exc}[/red]\n"
                    "Close any open browser windows and try again.\n"
                    f"If the problem persists, manually delete: {browser_profile}"
                )
                raise SystemExit(1) from exc

        # Clear cached notebook / conversation context so post-logout commands
        # don't silently reuse IDs from the previous account. When logout is
        # part of the account-switch flow (see _ACCOUNT_MISMATCH_HINT in
        # rpc/decoder.py), leaving context.json behind would cause the next
        # `ask` / `use` to target the old account's notebook and surface
        # misleading not-found / permission errors.
        try:
            if clear_context(clear_account=True):
                removed_any = True
        except OSError as exc:
            context_file = get_context_path()
            logger.error("Failed to remove context file %s: %s", context_file, exc)
            console.print(
                f"[red]Cannot remove context file: {exc}[/red]\n"
                "Close any running notebooklm commands and try again.\n"
                f"If the problem persists, manually delete: {context_file}"
            )
            raise SystemExit(1) from exc

        if removed_any:
            console.print("[green]Logged out.[/green] Run 'notebooklm login' to sign in again.")
        else:
            console.print("[yellow]No active session found.[/yellow] Already logged out.")

    @auth_group.command("inspect")
    @click.option(
        "--browser",
        "browser_name",
        default="auto",
        help=(
            "Browser to read cookies from (chrome, firefox, brave, edge, "
            "safari, arc, ...). 'auto' picks the first one rookiepy can read. "
            "Requires: pip install 'notebooklm-py[cookies]'"
        ),
    )
    @click.option("--json", "json_output", is_flag=True, help="Output as JSON")
    def auth_inspect(browser_name, json_output):
        """List Google accounts visible to a browser's cookie store.

        Read-only — never writes to disk. Use this before
        ``notebooklm login --browser-cookies <browser> --account <email>`` to
        see which account emails are available.

        \b
        Examples:
          notebooklm auth inspect --browser chrome
          notebooklm auth inspect --browser firefox --json
        """
        _, accounts = _enumerate_browser_accounts(browser_name, verbose=not json_output)
        if json_output:
            json_output_response(
                {
                    "browser": browser_name,
                    "accounts": [{"email": a.email, "is_default": a.is_default} for a in accounts],
                }
            )
            return
        console.print(f"\n[bold]Browser:[/bold] {browser_name}")
        console.print(f"[bold]Found {len(accounts)} signed-in Google account(s):[/bold]\n")
        table = Table(show_header=True, header_style="bold")
        table.add_column("email")
        table.add_column("default", justify="center")
        for a in accounts:
            table.add_row(
                a.email,
                "[green]✓[/green]" if a.is_default else "",
            )
        console.print(table)
        console.print(
            "\n[dim]Note: --browser-cookies <browser> reads cookies from the "
            "browser's default user-data profile only. Accounts in other "
            "browser profiles will not appear here.[/dim]\n"
            "Pick one with: [cyan]notebooklm login --browser-cookies "
            f"{browser_name} --account EMAIL[/cyan]\n"
            "Or extract them all: [cyan]notebooklm login --browser-cookies "
            f"{browser_name} --all-accounts[/cyan]"
        )

    @auth_group.command("check")
    @click.option(
        "--test", "test_fetch", is_flag=True, help="Test token fetch (makes network request)"
    )
    @click.option("--json", "json_output", is_flag=True, help="Output as JSON")
    @click.pass_context
    def auth_check(ctx, test_fetch, json_output):
        """Check authentication status and diagnose issues.

        Validates that authentication is properly configured by checking:
        - Storage file exists and is readable
        - JSON structure is valid
        - Required cookies (SID) are present
        - Cookie domains are correct

        Use --test to also verify tokens can be fetched from NotebookLM
        (requires network access).

        \b
        Examples:
          notebooklm auth check           # Quick local validation
          notebooklm auth check --test    # Full validation with network test
          notebooklm auth check --json    # Machine-readable output
        """
        from ..auth import extract_cookies_from_storage, fetch_tokens_with_domains

        profile = ctx.obj.get("profile") if ctx.obj else None
        storage_path = get_storage_path(profile=profile)
        has_env_var = bool(os.environ.get("NOTEBOOKLM_AUTH_JSON"))
        has_home_env = bool(os.environ.get("NOTEBOOKLM_HOME"))

        checks: dict[str, bool | None] = {
            "storage_exists": False,
            "json_valid": False,
            "cookies_present": False,
            "sid_cookie": False,
            "token_fetch": None,  # None = not tested, True/False = result
        }

        # Determine auth source for display
        if has_env_var:
            auth_source = "NOTEBOOKLM_AUTH_JSON"
        elif has_home_env:
            auth_source = f"$NOTEBOOKLM_HOME ({storage_path})"
        else:
            auth_source = f"file ({storage_path})"

        details: dict[str, Any] = {
            "storage_path": str(storage_path),
            "auth_source": auth_source,
            "cookies_found": [],
            "cookie_domains": [],
            "error": None,
        }

        # Check 1: Storage exists
        if has_env_var:
            checks["storage_exists"] = True
        else:
            checks["storage_exists"] = storage_path.exists()

        if not checks["storage_exists"]:
            details["error"] = f"Storage file not found: {storage_path}"
            _output_auth_check(checks, details, json_output)
            return

        # Check 2: JSON valid
        try:
            if has_env_var:
                storage_state = json.loads(os.environ["NOTEBOOKLM_AUTH_JSON"])
            else:
                storage_state = json.loads(storage_path.read_text(encoding="utf-8"))
            checks["json_valid"] = True
        except json.JSONDecodeError as e:
            details["error"] = f"Invalid JSON: {e}"
            _output_auth_check(checks, details, json_output)
            return

        # Check 3: Cookies present
        try:
            cookies = extract_cookies_from_storage(storage_state)
            checks["cookies_present"] = True
            checks["sid_cookie"] = "SID" in cookies
            details["cookies_found"] = list(cookies.keys())

            # Build detailed cookie-by-domain mapping for debugging
            cookies_by_domain: dict[str, list[str]] = {}
            for cookie in storage_state.get("cookies", []):
                domain = cookie.get("domain", "")
                name = cookie.get("name", "")
                if domain and name and "google" in domain.lower():
                    cookies_by_domain.setdefault(domain, []).append(name)

            details["cookies_by_domain"] = cookies_by_domain
            details["cookie_domains"] = sorted(cookies_by_domain.keys())
        except ValueError as e:
            details["error"] = str(e)
            _output_auth_check(checks, details, json_output)
            return

        # Check 4: Token fetch (optional)
        if test_fetch:
            try:
                token_path = None if has_env_var else storage_path
                csrf, session_id = run_async(fetch_tokens_with_domains(token_path, profile))
                checks["token_fetch"] = True
                details["csrf_length"] = len(csrf)
                details["session_id_length"] = len(session_id)
            except Exception as e:
                checks["token_fetch"] = False
                details["error"] = f"Token fetch failed: {e}"

        _output_auth_check(checks, details, json_output)

    def _output_auth_check(checks: dict, details: dict, json_output: bool):
        """Output auth check results."""
        all_passed = all(v is True for v in checks.values() if v is not None)

        if json_output:
            json_output_response(
                {
                    "status": "ok" if all_passed else "error",
                    "checks": checks,
                    "details": details,
                }
            )
            return

        # Rich output
        table = Table(title="Authentication Check")
        table.add_column("Check", style="dim")
        table.add_column("Status")
        table.add_column("Details", style="cyan")

        def status_icon(val):
            if val is None:
                return "[dim]⊘ skipped[/dim]"
            return "[green]✓ pass[/green]" if val else "[red]✗ fail[/red]"

        table.add_row(
            "Storage exists",
            status_icon(checks["storage_exists"]),
            details["auth_source"],
        )
        table.add_row(
            "JSON valid",
            status_icon(checks["json_valid"]),
            "",
        )
        table.add_row(
            "Cookies present",
            status_icon(checks["cookies_present"]),
            f"{len(details.get('cookies_found', []))} cookies" if checks["cookies_present"] else "",
        )
        table.add_row(
            "SID cookie",
            status_icon(checks["sid_cookie"]),
            ", ".join(details.get("cookie_domains", [])[:3]) or "",
        )
        table.add_row(
            "Token fetch",
            status_icon(checks["token_fetch"]),
            "use --test to check" if checks["token_fetch"] is None else "",
        )

        console.print(table)

        # Show detailed cookie breakdown by domain
        cookies_by_domain = details.get("cookies_by_domain", {})
        if cookies_by_domain:
            console.print()  # Blank line
            cookie_table = Table(title="Cookies by Domain")
            cookie_table.add_column("Domain", style="cyan")
            cookie_table.add_column("Cookies")

            # Key auth cookies to highlight
            key_cookies = {"SID", "HSID", "SSID", "APISID", "SAPISID", "SIDCC"}

            def format_cookie_name(name: str) -> str:
                if name in key_cookies:
                    return f"[green]{name}[/green]"
                if name.startswith("__Secure-"):
                    return f"[blue]{name}[/blue]"
                return f"[dim]{name}[/dim]"

            for domain in sorted(cookies_by_domain.keys()):
                cookie_names = cookies_by_domain[domain]
                formatted = [format_cookie_name(name) for name in sorted(cookie_names)]
                cookie_table.add_row(domain, ", ".join(formatted))

            console.print(cookie_table)

        if details.get("error"):
            console.print(f"\n[red]Error:[/red] {details['error']}")

        if all_passed:
            console.print("\n[green]Authentication is valid.[/green]")
        elif not checks["storage_exists"]:
            console.print("\n[yellow]Run 'notebooklm login' to authenticate.[/yellow]")
        elif checks["token_fetch"] is False:
            console.print(
                "\n[yellow]Cookies may be expired. Run 'notebooklm login' to refresh.[/yellow]"
            )

    @auth_group.command("refresh")
    @click.option(
        "--browser-cookies",
        "--browser-cookie",
        "browser_cookies",
        default=None,
        is_flag=False,
        flag_value="auto",
        help=(
            "Re-extract cookies from an installed browser and match the profile "
            "account from context.json. Optionally specify browser: chrome, "
            "firefox, brave, edge, safari, arc, ..."
        ),
    )
    @click.option(
        "--quiet", "-q", is_flag=True, help="Suppress success output (only print on error)"
    )
    @click.pass_context
    def auth_refresh(ctx, browser_cookies, quiet):
        """Refresh stored cookies by exercising the auth path once.

        One-shot keepalive: opens a session, runs the layer-1 poke against
        ``accounts.google.com`` to elicit ``__Secure-1PSIDTS`` rotation,
        fetches CSRF + session ID from ``notebooklm.google.com`` (discarded;
        their side effect is the cookie jar), and persists the rotated jar
        to ``storage_state.json`` on close. Designed to be scheduled by the
        OS (launchd / systemd / cron) so that an otherwise-idle profile
        does not stale out between user-driven calls.

        Cadence: 15-20 minutes is the recommended interval. Tighter is
        wasteful; significantly looser may cross the SIDTS server-side
        validity window for your account/region.

        Transient errors (e.g. ``httpx.RequestError`` from a flaky network)
        are surfaced as exit 1 rather than retried in-process; the OS
        scheduler's next firing is the retry mechanism.

        \b
        Examples:
          notebooklm auth refresh                 # one-shot, exit 0/1
          notebooklm auth refresh --browser-cookies chrome
          notebooklm --profile work auth refresh  # against a named profile
          watch -n 1200 notebooklm auth refresh   # quick in-terminal loop

        See docs/troubleshooting.md ("Cookie freshness for long-running /
        unattended use") for launchd / systemd / cron recipes.
        """
        from ..auth import fetch_tokens_with_domains

        # NOTEBOOKLM_AUTH_JSON has no writable backing store, so a keepalive
        # poke would rotate SIDTS server-side but the rotated value would
        # vanish on process exit — silent no-op in cron. Refuse with a clear
        # message instead of pretending to succeed.
        if os.environ.get("NOTEBOOKLM_AUTH_JSON"):
            click.echo(
                "Error: 'auth refresh' is incompatible with NOTEBOOKLM_AUTH_JSON. "
                "The keepalive needs a writable storage_state.json to persist "
                "rotated cookies. Either unset NOTEBOOKLM_AUTH_JSON for this "
                "process and use a profile-backed storage file, or arrange for "
                "the env var to be refreshed externally.",
                err=True,
            )
            raise SystemExit(1)

        profile = ctx.obj.get("profile") if ctx.obj else None
        storage_path = get_storage_path(profile=profile)

        if browser_cookies is not None:
            try:
                _refresh_from_browser_cookies(
                    browser_cookies,
                    storage_path=storage_path,
                    profile=profile,
                    quiet=quiet,
                )
            except Exception as exc:
                if isinstance(exc, SystemExit):
                    raise
                click.echo(f"Error: {type(exc).__name__}: {exc}", err=True)
                raise SystemExit(1) from exc
            return

        try:
            run_async(fetch_tokens_with_domains(storage_path, profile))
        except Exception as exc:
            click.echo(f"Error: {type(exc).__name__}: {exc}", err=True)
            raise SystemExit(1) from exc

        if not quiet:
            console.print(f"[green]ok[/green] refreshed: {storage_path}")
