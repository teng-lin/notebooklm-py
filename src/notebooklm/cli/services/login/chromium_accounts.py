"""Chromium-family cookie helpers (fan-out + scoped reads).

Contains the chromium multi-profile fan-out (``Default`` + ``Profile 1``
+ …) and the explicit ``chrome::<selector>`` reader. Imports from
:mod:`.cookie_jar` (shared ``_enumerate_one_jar``),
:mod:`.rookiepy_errors` (friendly rookiepy error messages), and
:mod:`.cookie_domains` (domain-list builder).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx

from ...._app.login_cookie import Account, project_browser_account
from .cookie_domains import _build_google_cookie_domains
from .cookie_jar import _enumerate_one_jar
from .outcomes import BrowserCookieOutcome, CookieValidationFailure, NetworkFailure
from .rookiepy_errors import _handle_rookiepy_error

if TYPE_CHECKING:
    from .io_seam import LoginIO

# Shared rookiepy-not-installed message — kept identical to the single-jar
# path (``browser_accounts._read_browser_cookies``) so the user sees the
# same install hint regardless of which Chromium path raised it.
_ROOKIEPY_NOT_INSTALLED_MESSAGE = (
    "[red]rookiepy is not installed.[/red]\n"
    "Install it with:\n"
    "  pip install 'notebooklm-py[cookies]'\n"
    "or directly:\n"
    "  pip install rookiepy"
)


def _emit_progress(io: LoginIO, message: str) -> None:
    """Emit a verbose-mode progress line through the caller-injected sink.

    Routes the text-mode "Reading cookies from ..." status lines
    (byte-for-byte unchanged) through ``io.emit`` so this service never
    imports the command layer's ``...rendering`` module (ADR-0008 level-3
    boundary, #1393).
    """
    io.emit(message)


def _chromium_profiles_module() -> Any:
    import importlib

    return importlib.import_module("notebooklm.cli._chromium_profiles")


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
    io: LoginIO,
    browser_name: str,
    profile_selector: str,
    *,
    verbose: bool,
    include_domains: set[str] | None,
) -> tuple[Any, list[dict[str, Any]]] | BrowserCookieOutcome:
    """Read cookies from one explicit Chromium profile selector.

    Returns ``(profile, cookies)`` on success, or a
    :class:`.outcomes.BrowserCookieOutcome` on failure. The command layer
    (or :func:`refresh._exit_on_outcome`) renders ``outcome.message`` and
    exits; this keeps presentation + exit policy out of ``cli/services``.
    The ``io`` sink carries the verbose progress line.
    """
    chromium_profiles = _chromium_profiles_module()

    try:
        profile = chromium_profiles.resolve_chromium_profile(browser_name, profile_selector)
    except ValueError as e:
        return CookieValidationFailure(code="CHROMIUM_PROFILE_INVALID", message=f"[red]{e}[/red]")

    domains = _build_google_cookie_domains(include_domains=include_domains)
    if verbose:
        _emit_progress(
            io,
            f"[yellow]Reading cookies from {profile.browser} profile "
            f"'{profile.human_name}' (directory: {profile.directory_name})...[/yellow]",
        )

    try:
        cookies = chromium_profiles.read_chromium_profile_cookies(profile, domains=domains)
    except ImportError:
        return CookieValidationFailure(
            code="ROOKIEPY_NOT_INSTALLED", message=_ROOKIEPY_NOT_INSTALLED_MESSAGE
        )
    except (OSError, RuntimeError) as e:
        return CookieValidationFailure(
            code="COOKIE_READ_FAILED",
            message=_handle_rookiepy_error(e, f"{profile.browser} profile '{profile.human_name}'"),
        )

    return profile, cookies


def _enumerate_chromium_profiles_fanout(
    io: LoginIO,
    browser_name: str,
    profiles: list[Any],
    *,
    verbose: bool,
    include_domains: set[str] | None,
    validate_before_probe: bool = True,
) -> tuple[dict[str | None, list[dict[str, Any]]], list[Account]] | BrowserCookieOutcome:
    """Fan out account discovery across multiple Chromium user-data profiles.

    Reads cookies from each profile's own ``Cookies`` SQLite DB and probes
    ``?authuser=N`` per profile. Aggregates accounts across profiles and
    dedupes by email (first occurrence wins — typically ``Default``, then
    ``Profile 1``, ``Profile 2``, … in numeric order; duplicates are dropped
    with a console warning so the user can investigate).

    Returns ``(per_profile_cookies, accounts)`` on success, or a
    :class:`.outcomes.BrowserCookieOutcome` on failure. The command layer
    renders ``outcome.message`` and exits — presentation + exit policy stay
    out of ``cli/services``. The ``io`` sink carries the verbose progress and
    per-profile skip/dedupe lines.
    """
    chromium_profiles = domains = names = name_labels = item = None
    per_profile_cookies = read_failures = aggregated = None
    seen_emails: dict[str, str] | None = None
    profile = raw = jar_result = accounts = account = projected_account = None
    first_profile = first_error = None
    successful_reads = 0
    global_default_assigned = is_default = False
    read_cookies = enumerate_jar = project_account = emit_progress = None
    try:
        chromium_profiles = _chromium_profiles_module()
        domains = _build_google_cookie_domains(include_domains=include_domains)
        read_cookies = chromium_profiles.read_chromium_profile_cookies
        enumerate_jar = _enumerate_one_jar
        project_account = project_browser_account
        emit_progress = _emit_progress

        if verbose:
            name_labels = []
            for item in profiles:
                name_labels.append(f"'{item.human_name}'")
            names = ", ".join(name_labels)
            emit_progress(
                io,
                f"[yellow]Reading cookies from {len(profiles)} {browser_name} "
                f"user-profiles: {names}[/yellow]",
            )

        per_profile_cookies = {}
        read_failures = []
        seen_emails = {}
        aggregated = []

        for profile in profiles:
            try:
                raw = read_cookies(profile, domains=domains)
            except ImportError:
                return CookieValidationFailure(
                    code="ROOKIEPY_NOT_INSTALLED",
                    message=_ROOKIEPY_NOT_INSTALLED_MESSAGE,
                )
            except (OSError, RuntimeError) as exc:
                read_failures.append((profile.human_name, exc))
                if verbose:
                    emit_progress(
                        io,
                        f"  [yellow]skipping {browser_name} profile "
                        f"'{profile.human_name}': {exc}[/yellow]",
                    )
                continue

            successful_reads += 1
            try:
                jar_result = enumerate_jar(
                    raw,
                    browser_name,
                    browser_profile=profile.directory_name,
                    quiet=True,
                    validate_before_probe=validate_before_probe,
                    io=io,
                )
            except httpx.RequestError as exc:
                return NetworkFailure(
                    code="NETWORK_ERROR",
                    message=(
                        f"[red]Account discovery failed (network error):[/red] {exc}\n"
                        "Check your internet connection and try again."
                    ),
                )
            if isinstance(jar_result, BrowserCookieOutcome):
                if verbose:
                    emit_progress(
                        io,
                        f"  [dim]no signed-in Google accounts in '{profile.human_name}'[/dim]",
                    )
                continue
            accounts = jar_result

            per_profile_cookies[profile.directory_name] = raw
            for account in accounts:
                if account.email in seen_emails:
                    if verbose:
                        emit_progress(
                            io,
                            f"  [yellow]warning: {account.email} also appears in "
                            f"'{profile.human_name}'; using cookies from "
                            f"'{seen_emails[account.email]}'[/yellow]",
                        )
                    continue
                seen_emails[account.email] = profile.directory_name
                is_default = account.is_default and not global_default_assigned
                if is_default:
                    global_default_assigned = True
                projected_account = project_account(
                    account,
                    browser_profile=account.browser_profile,
                    is_default=is_default,
                )
                aggregated.append(projected_account)

        if not aggregated:
            if successful_reads == 0 and read_failures:
                first_profile, first_error = read_failures[0]
                return CookieValidationFailure(
                    code="COOKIE_READ_FAILED",
                    message=(
                        f"[red]Could not read cookies from any {browser_name} "
                        "user-profile.[/red]\n"
                        f"First error ({first_profile}): {first_error}\n"
                        "Close the browser or unlock its cookie store, then try again."
                    ),
                )
            return CookieValidationFailure(
                code="NO_ACCOUNTS_FOUND",
                message=(
                    f"[red]No signed-in Google accounts found across {len(profiles)} "
                    f"{browser_name} user-profiles.[/red]\n"
                    "Sign in to a Google account in your browser and try again."
                ),
            )

        return per_profile_cookies, aggregated
    finally:
        del io, browser_name, profiles, verbose, include_domains, validate_before_probe
        del chromium_profiles, domains, names, name_labels, item
        del per_profile_cookies, read_failures
        del seen_emails, aggregated, profile, raw, jar_result, accounts, account
        del projected_account, first_profile, first_error, successful_reads
        del global_default_assigned, is_default, read_cookies, enumerate_jar
        del project_account, emit_progress
