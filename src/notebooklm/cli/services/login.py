"""CLI-internal login services for browser-cookie auth flows."""

from __future__ import annotations

import importlib
import logging
import re
import sqlite3
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import click
import httpx

if TYPE_CHECKING:
    from ...auth import Account

from ...auth import (
    GOOGLE_REGIONAL_CCTLDS,
    OPTIONAL_COOKIE_DOMAINS_BY_LABEL,
    REQUIRED_COOKIE_DOMAINS,
    convert_rookiepy_cookies_to_storage_state,
    extract_cookies_from_storage,
    fetch_tokens_with_domains,
    read_account_metadata,
)
from ...client import NotebookLMClient
from ...io import atomic_write_json
from ...paths import get_storage_path
from ..language import set_language
from ..rendering import console
from ..runtime import run_async

logger = logging.getLogger(__name__)


def _chromium_profiles_module() -> Any:
    return importlib.import_module("notebooklm.cli._chromium_profiles")


def _firefox_containers_module() -> Any:
    return importlib.import_module("notebooklm.cli._firefox_containers")


# Profile name validation: alphanumeric, hyphens, underscores. Must start with alphanum.
_PROFILE_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")


def _validate_profile_name(name: str) -> str:
    """Validate a profile name."""
    if not _PROFILE_NAME_RE.match(name):
        raise click.ClickException(
            f"Invalid profile name '{name}'. "
            "Use alphanumeric characters, hyphens, and underscores. Must start with a letter or digit."
        )
    return name


def email_to_profile_name(email: str, *, fallback: str = "account") -> str:
    """Derive a valid profile name from an email address.

    Profile names are restricted to ``[a-zA-Z0-9_-]`` (see
    :data:`_PROFILE_NAME_RE`) and must start with an alphanumeric character.
    Email local-parts routinely contain ``.``, ``+``, etc. that aren't
    allowed, so we rewrite them to hyphens.

    Examples::

        alice@example.com         -> "alice"
        alice.smith@example.com   -> "alice-smith"
        bob+work@gmail.com        -> "bob-work"
        teng.lin.9414@gmail.com   -> "teng-lin-9414"

    Args:
        email: Account email address.
        fallback: Profile name to use when sanitization yields an empty
            string or a name that does not start with an alphanum.

    Returns:
        A profile name guaranteed to satisfy :data:`_PROFILE_NAME_RE`.
    """
    local = email.split("@", 1)[0] if "@" in email else email
    sanitized = re.sub(r"[^a-zA-Z0-9_-]+", "-", local)
    sanitized = re.sub(r"-{2,}", "-", sanitized).strip("-_")
    if not sanitized or not _PROFILE_NAME_RE.match(sanitized):
        # The function's contract is "always returns a valid profile name", so
        # protect callers that pass a malformed fallback (e.g. "-tmp").
        return fallback if _PROFILE_NAME_RE.match(fallback) else "account"
    return sanitized


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
    from ...auth import (
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
        raise SystemExit(1) from None

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
        raise SystemExit(1) from None
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
            raise SystemExit(1) from None
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


def _enumerate_browser_accounts(
    browser_name: str,
    *,
    verbose: bool = True,
    include_domains: set[str] | None = None,
) -> tuple[dict[str | None, list[dict[str, Any]]], list[Account]]:
    """Read cookies from ``browser_name`` and discover signed-in accounts.

    For chromium-family browsers with multiple populated user-data profiles
    (``Default`` plus ``Profile 1``, ``Profile 2``, …), fans out across every
    profile and aggregates the discovered accounts, deduping by email.
    ``chrome::<profile-name-or-directory>`` scopes discovery to one profile.

    For non-chromium browsers, single-profile chromium installs, and the
    legacy path, falls back to a single rookiepy call — preserving every
    existing test mock and runtime behavior.

    Args:
        browser_name: rookiepy browser alias.
        verbose: Forwarded to :func:`_read_browser_cookies` to suppress the
            human-readable progress line in JSON-output paths.
        include_domains: Forwarded to :func:`_read_browser_cookies` to
            broaden the extraction set with sibling-product cookies. See
            :func:`_parse_include_domains`.

    Returns:
        ``(per_profile_cookies, accounts)``:

        * ``per_profile_cookies`` — dict keyed by :attr:`Account.browser_profile`
          (e.g. ``"Default"``, ``"Profile 1"``) mapping to the raw rookiepy
          cookies that yielded that profile's accounts. The legacy / single-jar
          path uses ``None`` as the key.
        * ``accounts`` — :class:`notebooklm.auth.Account` records, each tagged
          with the originating ``browser_profile``, deduped by email (first
          occurrence wins; later duplicates are dropped with a warning).

    Raises:
        SystemExit: On rookiepy failure, missing required cookies, or
            ``authuser=0`` not returning a signed-in account from every probed
            profile.
    """
    chromium_profiles = _chromium_profiles_module()

    scoped_chromium = _split_chromium_profile_browser_spec(browser_name)
    if scoped_chromium is not None:
        scoped_browser, profile_selector = scoped_chromium
        profile, raw_cookies = _read_chromium_profile_cookies_from_selector(
            scoped_browser,
            profile_selector,
            verbose=verbose,
            include_domains=include_domains,
        )
        accounts = _enumerate_one_jar(
            raw_cookies,
            profile.browser,
            browser_profile=profile.directory_name,
        )
        return {profile.directory_name: raw_cookies}, accounts

    # Chromium multi-profile fan-out — only kicks in when discovery surfaces
    # >1 populated profile. Single-profile installs and non-chromium browsers
    # take the legacy path below so all existing rookiepy mocks keep working.
    if chromium_profiles.is_chromium_browser(browser_name):
        profiles = chromium_profiles.discover_chromium_profiles(browser_name)
        if len(profiles) > 1:
            return _enumerate_chromium_profiles_fanout(
                browser_name,
                profiles,
                verbose=verbose,
                include_domains=include_domains,
            )

    raw_cookies = _read_browser_cookies(
        browser_name, verbose=verbose, include_domains=include_domains
    )
    accounts = _enumerate_one_jar(raw_cookies, browser_name, browser_profile=None)
    return {None: raw_cookies}, accounts


def _enumerate_chromium_profiles_fanout(
    browser_name: str,
    profiles: list[Any],
    *,
    verbose: bool,
    include_domains: set[str] | None,
) -> tuple[dict[str | None, list[dict[str, Any]]], list[Account]]:
    """Fan out account discovery across multiple Chromium user-data profiles.

    Reads cookies from each profile's own ``Cookies`` SQLite DB and probes
    ``?authuser=N`` per profile. Aggregates accounts across profiles and
    dedupes by email (first occurrence wins — typically ``Default``, then
    ``Profile 1``, ``Profile 2``, … in numeric order; duplicates are dropped
    with a console warning so the user can investigate).
    """
    chromium_profiles = _chromium_profiles_module()

    domains = _build_google_cookie_domains(include_domains=include_domains)

    if verbose:
        names = ", ".join(f"'{p.human_name}'" for p in profiles)
        console.print(
            f"[yellow]Reading cookies from {len(profiles)} {browser_name} "
            f"user-profiles: {names}[/yellow]"
        )

    from ...auth import Account

    per_profile_cookies: dict[str | None, list[dict[str, Any]]] = {}
    seen_emails: dict[str, str] = {}  # email -> winning browser_profile
    aggregated: list[Account] = []
    global_default_assigned = False

    for profile in profiles:
        try:
            raw = chromium_profiles.read_chromium_profile_cookies(profile, domains=domains)
        except ImportError:
            # rookiepy isn't installed — same friendly message the legacy
            # single-jar path prints (``_read_browser_cookies``). Abort fan-out
            # since every profile would fail the same way.
            console.print(
                "[red]rookiepy is not installed.[/red]\n"
                "Install it with:\n"
                "  pip install 'notebooklm-py[cookies]'\n"
                "or directly:\n"
                "  pip install rookiepy"
            )
            raise SystemExit(1) from None
        except (OSError, RuntimeError) as e:
            # One profile failing (e.g. a locked DB) shouldn't kill discovery
            # of the others. Surface a per-profile note and continue.
            if verbose:
                console.print(
                    f"  [yellow]skipping {browser_name} profile "
                    f"'{profile.human_name}': {e}[/yellow]"
                )
            continue

        try:
            accounts = _enumerate_one_jar(
                raw,
                browser_name,
                browser_profile=profile.directory_name,
                quiet=True,
            )
        except SystemExit:
            # ``_enumerate_one_jar`` exits the CLI on a stale-jar / missing-cookies
            # failure, but in fan-out mode an individual profile being signed
            # out is normal. Catch and continue.
            if verbose:
                console.print(
                    f"  [dim]no signed-in Google accounts in '{profile.human_name}'[/dim]"
                )
            continue
        except httpx.RequestError as e:
            # Network failure — every subsequent profile probe will hit the
            # same error, so abort the entire fan-out rather than collapse
            # the transport failure into per-profile "signed out" skips.
            console.print(
                f"[red]Account discovery failed (network error):[/red] {e}\n"
                "Check your internet connection and try again."
            )
            raise SystemExit(1) from None

        per_profile_cookies[profile.directory_name] = raw
        for account in accounts:
            if account.email in seen_emails:
                if verbose:
                    console.print(
                        f"  [yellow]warning: {account.email} also appears in "
                        f"'{profile.human_name}'; using cookies from "
                        f"'{seen_emails[account.email]}'[/yellow]"
                    )
                continue
            seen_emails[account.email] = profile.directory_name
            # ``is_default`` from ``_enumerate_one_jar`` is the per-jar
            # authuser=0 marker — every Chromium user-profile has its own.
            # For a unified cross-profile view, only the FIRST profile's
            # default carries the global default flag (typically Default's
            # primary Google account, matching what the user sees when they
            # open Chrome without explicitly picking a different profile).
            is_default = account.is_default and not global_default_assigned
            if is_default:
                global_default_assigned = True
            aggregated.append(
                Account(
                    authuser=account.authuser,
                    email=account.email,
                    is_default=is_default,
                    browser_profile=account.browser_profile,
                )
            )

    if not aggregated:
        console.print(
            f"[red]No signed-in Google accounts found across {len(profiles)} "
            f"{browser_name} user-profiles.[/red]\n"
            "Sign in to a Google account in your browser and try again."
        )
        raise SystemExit(1)

    return per_profile_cookies, aggregated


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


def _profiles_by_account_email(profile_names: list[str]) -> dict[str, str]:
    """Return existing profiles keyed by *casefolded* account metadata email.

    Keys are casefolded so that mixed-casing in stored ``context.json``
    metadata (``Alice@Gmail.com`` vs. an incoming ``alice@gmail.com``)
    doesn't cause us to miss the match and wrongly allocate a suffixed
    profile. Lookup callers must casefold their email key likewise.
    """
    from ...auth import read_account_metadata

    profiles_by_email: dict[str, str] = {}
    for profile in profile_names:
        metadata = read_account_metadata(get_storage_path(profile=profile))
        email = metadata.get("email")
        if isinstance(email, str) and email:
            # list_profiles() is sorted, so this also prefers the unsuffixed
            # profile over older duplicate suffixes such as alice-2.
            profiles_by_email.setdefault(email.casefold(), profile)
    return profiles_by_email


def _profile_account_email(profile: str) -> str | None:
    """Return the account email recorded in ``profile``'s ``context.json``.

    ``None`` when the profile has no account metadata at all (hand-created
    via plain ``notebooklm login --profile NAME``, or pre-dating the
    account-tracking feature). Used by ``--all-accounts --update`` to
    decide whether adopting a name-matching profile is safe.
    """
    from ...auth import read_account_metadata

    metadata = read_account_metadata(get_storage_path(profile=profile))
    email = metadata.get("email")
    return email if isinstance(email, str) and email else None


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
    from ...paths import list_profiles

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


def _resolve_all_accounts_target(
    *,
    base_name: str,
    account_email: str,
    existing_profiles: set[str],
    unavailable: set[str],
    claimed: set[str],
    update: bool,
) -> str:
    """Pick the destination profile when no email-metadata match exists.

    Without ``--update`` (default): always allocate the next available
    suffix (``alice``, ``alice-2``, …) — never touch a hand-created profile.

    With ``--update``: adopt the unsuffixed ``base_name`` profile in place
    if it exists AND has either (a) no account metadata at all or (b)
    metadata for the same email (defensive — that should have been picked
    up by ``profiles_by_email`` upstream, but the casefold mismatch case is
    cheap to handle). Profiles whose metadata binds a *different* email
    fall back to the suffix path to avoid clobbering them.
    """
    if update and base_name in existing_profiles and base_name not in claimed:
        existing_email = _profile_account_email(base_name)
        if existing_email is None or existing_email.casefold() == account_email.casefold():
            return base_name
    return _next_available_profile_name(base_name, unavailable | claimed)


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
    default_account = next((a for a in accounts if a.is_default), None)
    if default_account is not None:
        return default_account

    console.print(
        "[yellow]Warning: Browser account list did not mark a default account; "
        f"using {accounts[0].email}.[/yellow]"
    )
    return accounts[0]


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
        # Atomic write with chmod 0o600 — avoids non-atomic + world-readable
        # window from plain write_text + post-hoc chmod.
        atomic_write_json(storage_path, storage_state)
        if sys.platform != "win32":
            storage_path.parent.chmod(0o700)
    except OSError as e:
        logger.error("Failed to save authentication to %s: %s", storage_path, e)
        console.print(f"[red]Failed to save authentication to {storage_path}.[/red]\nDetails: {e}")
        raise SystemExit(1) from None

    from ...auth import write_account_metadata

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
    include_domains: set[str] | None = None,
) -> None:
    """Refresh the active profile from browser cookies, repairing account drift."""
    per_profile_cookies, accounts = _enumerate_browser_accounts(
        browser_name, verbose=not quiet, include_domains=include_domains
    )
    if not accounts:
        console.print(f"[red]No signed-in Google accounts found in {browser_name}.[/red]")
        raise SystemExit(1)

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


_INCLUDE_DOMAINS_ALL = "all"


def _parse_include_domains(values: tuple[str, ...]) -> set[str]:
    """Parse one or more ``--include-domains`` flag values into labels.

    Accepts both ``--include-domains=youtube --include-domains=docs`` and
    ``--include-domains=youtube,docs`` (and any mix). Whitespace around
    commas is tolerated. Empty fragments are dropped.

    Raises:
        click.BadParameter: if any label is not one of
            :data:`notebooklm.auth.OPTIONAL_COOKIE_DOMAINS_BY_LABEL` keys
            (or the literal ``"all"``).
    """
    labels: set[str] = set()
    for raw in values:
        for part in raw.split(","):
            label = part.strip().lower()
            if not label:
                continue
            labels.add(label)
    if not labels:
        return labels
    valid = set(OPTIONAL_COOKIE_DOMAINS_BY_LABEL) | {_INCLUDE_DOMAINS_ALL}
    bad = labels - valid
    if bad:
        supported = ", ".join(sorted(valid))
        raise click.BadParameter(
            f"unknown --include-domains label(s): {', '.join(sorted(bad))}. Supported: {supported}."
        )
    return labels


def _warn_missing_optional_domains(include_domains: set[str]) -> None:
    """Emit a migration warning when the default minimum-cookies set is used.

    The cookie-domain split narrows the default extraction set to
    :data:`REQUIRED_COOKIE_DOMAINS`. Users upgrading from the prior
    behavior need a heads-up that YouTube / Docs / myaccount / Mail
    cookies are no longer scraped at login. Telling them how to opt back
    in is the entire point of the warning.
    """
    if include_domains:
        return
    supported = ", ".join(sorted(OPTIONAL_COOKIE_DOMAINS_BY_LABEL))
    console.print(
        "[dim]Note: sibling-product cookies not included by default. "
        f"Pass --include-domains=<{supported}> (or =all) to extract them.[/dim]"
    )
    logger.info(
        "Login extracting REQUIRED_COOKIE_DOMAINS only (cookie-domain split default). "
        "Pass --include-domains=%s (or =all) to include sibling cookies.",
        supported,
    )


def _resolve_optional_cookie_domains(labels: set[str]) -> frozenset[str]:
    """Resolve ``--include-domains`` labels to the union of their domain sets.

    Contract: ``labels`` must be the output of
    :func:`_parse_include_domains`, which validates that every label is in
    :data:`OPTIONAL_COOKIE_DOMAINS_BY_LABEL` (or the literal ``"all"``).
    Callers are expected to surface the ``click.BadParameter`` from the
    parser before we ever reach this function; the dict lookup below is
    therefore unguarded by design.
    """
    if not labels:
        return frozenset()
    if _INCLUDE_DOMAINS_ALL in labels:
        return frozenset().union(*OPTIONAL_COOKIE_DOMAINS_BY_LABEL.values())
    selected: set[str] = set()
    for label in labels:
        # ``_parse_include_domains`` guarantees ``label`` is a valid key
        # (or ``"all"``, handled above). Unguarded lookup is intentional —
        # a KeyError here would be a bug in our own validation, not user
        # input.
        selected.update(OPTIONAL_COOKIE_DOMAINS_BY_LABEL[label])
    return frozenset(selected)


def _build_google_cookie_domains(
    *,
    include_optional: bool = False,
    include_domains: set[str] | None = None,
) -> list[str]:
    """Return the cookie-domain list fed to extractors (rookiepy / Firefox).

    Defaults to :data:`REQUIRED_COOKIE_DOMAINS` plus all known regional
    ``.google.<ccTLD>`` variants. Sibling-product cookies (YouTube, Docs,
    myaccount, Mail) are excluded unless the caller opts in via
    ``include_optional=True`` or a non-empty ``include_domains`` label
    set.

    Args:
        include_optional: When ``True``, include every optional sibling
            domain (equivalent to ``--include-domains=all``). Preserves
            the pre-split behavior for callers that still need the broad
            set.
        include_domains: Set of optional-domain labels (output of
            :func:`_parse_include_domains`). Each label expands via
            :data:`OPTIONAL_COOKIE_DOMAINS_BY_LABEL`. ``"all"`` is
            accepted as a shortcut for every label.

    Returns:
        List of cookie-domain strings (suitable for ``rookiepy.load(
        domains=...)`` or :func:`extract_firefox_container_cookies`).
    """
    selected_optional: frozenset[str]
    if include_domains:
        selected_optional = _resolve_optional_cookie_domains(include_domains)
    elif include_optional:
        selected_optional = frozenset().union(*OPTIONAL_COOKIE_DOMAINS_BY_LABEL.values())
    else:
        selected_optional = frozenset()

    domains: list[str] = list(REQUIRED_COOKIE_DOMAINS | selected_optional)
    for cctld in GOOGLE_REGIONAL_CCTLDS:
        domain = f".google.{cctld}"
        if domain not in domains:
            domains.append(domain)
    return domains


def _split_chromium_profile_browser_spec(browser_name: str) -> tuple[str, str] | None:
    """Return ``(browser, profile_selector)`` for Chromium ``browser::profile`` specs."""
    if "::" not in browser_name:
        return None

    browser_base, profile_selector = browser_name.split("::", 1)
    browser_base = browser_base.strip()
    if not browser_base:
        return None

    if not _chromium_profiles_module().is_chromium_browser(browser_base):
        return None
    return browser_base, profile_selector.strip()


def _read_chromium_profile_cookies_from_selector(
    browser_name: str,
    profile_selector: str,
    *,
    verbose: bool,
    include_domains: set[str] | None,
) -> tuple[Any, list[dict[str, Any]]]:
    """Read cookies from one explicit Chromium profile selector."""
    chromium_profiles = _chromium_profiles_module()

    try:
        profile = chromium_profiles.resolve_chromium_profile(browser_name, profile_selector)
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        raise SystemExit(1) from None

    domains = _build_google_cookie_domains(include_domains=include_domains)
    if verbose:
        console.print(
            f"[yellow]Reading cookies from {profile.browser} profile "
            f"'{profile.human_name}' (directory: {profile.directory_name})...[/yellow]"
        )

    try:
        cookies = chromium_profiles.read_chromium_profile_cookies(profile, domains=domains)
    except ImportError:
        console.print(
            "[red]rookiepy is not installed.[/red]\n"
            "Install it with:\n"
            "  pip install 'notebooklm-py[cookies]'\n"
            "or directly:\n"
            "  pip install rookiepy"
        )
        raise SystemExit(1) from None
    except (OSError, RuntimeError) as e:
        _handle_rookiepy_error(e, f"{profile.browser} profile '{profile.human_name}'")
        raise SystemExit(1) from None

    return profile, cookies


def _read_firefox_container_cookies(
    container_spec: str,
    *,
    verbose: bool = True,
    include_domains: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Load Google cookies from a specific Firefox Multi-Account Container.

    Bypasses rookiepy because rookiepy 0.5.6 does not filter on
    ``originAttributes`` and silently merges every container's cookies (see
    issue #366 / #367). We talk to ``cookies.sqlite`` directly via the
    helpers in :mod:`notebooklm.cli._firefox_containers`.

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
    firefox_containers = _firefox_containers_module()

    profile_path = firefox_containers.find_firefox_profile_path()
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
        container_id = firefox_containers.resolve_container_id(profile_path, container_spec)
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

    domains = _build_google_cookie_domains(include_domains=include_domains)
    try:
        return firefox_containers.extract_firefox_container_cookies(
            profile_path, container_id, domains=domains
        )
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
    firefox_containers = _firefox_containers_module()

    profile_path = firefox_containers.find_firefox_profile_path()
    if profile_path is None:
        return
    if firefox_containers.has_container_cookies_in_use(profile_path):
        console.print(
            "[yellow]Warning: this Firefox profile has cookies stored inside "
            "a Multi-Account Container, but '--browser-cookies firefox' "
            "merges every container into one jar. If your Google session "
            "lives inside a container, re-run with "
            "[cyan]--browser-cookies 'firefox::<container-name>'[/cyan] "
            "(or [cyan]'firefox::none'[/cyan] for the no-container "
            "default).[/yellow]"
        )


def _read_browser_cookies(
    browser_name: str,
    *,
    verbose: bool = True,
    include_domains: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Load Google cookies from an installed browser via rookiepy.

    Wraps rookiepy import + dispatch + error handling so multiple commands
    (``login --browser-cookies``, ``auth inspect``) share one code path.

    Args:
        browser_name: ``"auto"`` to use ``rookiepy.load()``, a specific
            browser alias from :data:`_ROOKIEPY_BROWSER_ALIASES`, or
            ``"chrome::<profile-name-or-directory>"`` for a single Chromium
            user-data profile, or
            ``"firefox::<container-name>"`` (or ``"firefox::none"``) to
            extract from a single Firefox Multi-Account Container, bypassing
            rookiepy entirely.
        verbose: When False, suppress the "Reading cookies from …" progress
            line. Used by ``auth inspect --json`` to keep stdout pure JSON.
        include_domains: Optional set of ``--include-domains`` labels
            (output of :func:`_parse_include_domains`) that broaden the
            extraction set with sibling-product cookies. ``None`` (the
            default) keeps the extraction tight to
            :data:`REQUIRED_COOKIE_DOMAINS`.

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
        return _read_firefox_container_cookies(
            container_spec, verbose=verbose, include_domains=include_domains
        )

    scoped_chromium = _split_chromium_profile_browser_spec(browser_name)
    if scoped_chromium is not None:
        scoped_browser, profile_selector = scoped_chromium
        _, cookies = _read_chromium_profile_cookies_from_selector(
            scoped_browser,
            profile_selector,
            verbose=verbose,
            include_domains=include_domains,
        )
        return cookies

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

    domains = _build_google_cookie_domains(include_domains=include_domains)

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
        raise SystemExit(1) from None

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
        raise SystemExit(1) from None

    # Record account metadata so future calls target the same Google account.
    # Even on a default-account login (authuser=0, no email), remove stale
    # metadata so refreshed cookies cannot keep routing to an older account.
    if authuser or email:
        from ...auth import write_account_metadata

        try:
            write_account_metadata(storage_path, authuser=authuser, email=email)
        except OSError as e:
            logger.error("Failed to save account metadata for %s: %s", storage_path, e)
            console.print(
                f"[yellow]Warning: cookies saved but account metadata write failed.[/yellow]\n"
                f"Details: {e}"
            )
    else:
        from ...auth import clear_account_metadata

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
