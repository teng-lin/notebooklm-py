"""Session and context management CLI commands.

Commands:
    login   Log in to NotebookLM via browser
    use     Set the current notebook context
    status  Show current context
    clear   Clear current notebook context
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import click
from rich.table import Table

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext, Page
    from rich.console import Console

from ..client import NotebookLMClient
from ..config import get_base_host, get_base_url
from ..exceptions import AuthError, NotebookNotFoundError
from ..io import atomic_write_json
from ..paths import (
    get_browser_profile_dir,
    get_context_path,
    get_path_info,
    get_storage_path,
)
from .auth_runtime import handle_auth_error, run_client_workflow
from .context import (
    _current_storage_override,
    clear_context,
    get_current_notebook,
    set_current_notebook,
)
from .error_handler import _output_error, exit_with_code, handle_errors
from .rendering import console, json_output_response
from .resolve import resolve_notebook_id
from .runtime import run_async

# Direct imports replace the D1-PR-3-retired forwarding wrappers; see ADR-008.
# Several of these names also serve as ``notebooklm.cli.session_cmd.*`` monkeypatch
# surfaces for tests that pre-date ADR-008's services-side patching convention
# (e.g. ``_sync_server_language_to_config``, ``_login_browser_cookies_single``,
# ``_refresh_from_browser_cookies``, ``_enumerate_browser_accounts``).
#
# The names tagged ``F401`` below are *only* patch surfaces — they are not
# called from this module's body, but tests bind them on the
# ``notebooklm.cli.session_cmd`` namespace either via direct import
# (``test_cookie_domain_split.py``, ``test_auth_subcommands.py``) or via the
# dual-patch fixture in ``tests/_fixtures/cli_session.py`` (whose
# ``patch_session_login_dual`` requires the name to exist on both modules).
from .services.login import (
    _build_google_cookie_domains,
    _enumerate_browser_accounts,
    _enumerate_one_jar,  # noqa: F401 — patch surface only
    _login_all_accounts_from_browser,
    _login_browser_cookies_single,
    _login_with_browser_cookies,  # noqa: F401 — patch surface only
    _parse_include_domains,
    _refresh_from_browser_cookies,
    _resolve_optional_cookie_domains,  # noqa: F401 — patch surface only
    _select_account,  # noqa: F401 — patch surface only
    _sync_server_language_to_config,
    _warn_missing_optional_domains,
    _write_extracted_cookies,  # noqa: F401 — patch surface only
)

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


async def fetch_tokens_with_domains(*args: Any, **kwargs: Any) -> Any:
    """Patch-compatible forwarding wrapper for auth token refresh helpers."""
    from ..auth import fetch_tokens_with_domains as auth_fetch_tokens_with_domains

    return await auth_fetch_tokens_with_domains(*args, **kwargs)


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


def _use_notebook_table() -> Table:
    t = Table()
    t.add_column("ID", style="cyan")
    t.add_column("Title", style="green")
    t.add_column("Owner")
    t.add_column("Created", style="dim")
    return t


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


def _filter_storage_state_cookies_by_domain_policy(
    state: dict[str, Any],
    *,
    include_optional: bool = False,
    include_domains: set[str] | None = None,
) -> dict[str, Any]:
    """Filter a Playwright ``storage_state`` dict to the configured cookie-domain policy (P1-17).

    The rookiepy / ``--browser-cookies`` extraction path asks Chrome only for
    cookies on the explicit domain allowlist from
    :func:`_build_google_cookie_domains` — so sibling-product cookies the user
    happens to be signed into (``mail.google.com``, ``myaccount.google.com``,
    ``docs.google.com``, ``.youtube.com``) never reach ``storage_state.json``
    unless the user opts in via ``--include-domains=...``.

    The Playwright login flow, by contrast, captures every cookie the browser
    context holds. Without this filter, sibling-product cookies leak into the
    persisted ``storage_state.json`` and inflate the blast radius if the file
    is ever read by an attacker. This helper applies the same allowlist at
    write time so both login paths produce equivalent on-disk state.

    The match is exact-against-allowlist with leading-dot/no-dot equivalence
    (``http.cookiejar`` may normalize either form). Sibling-product subdomains
    are deliberately not matched by a broad ``.google.com`` suffix check —
    that's the bug we're fixing.

    Args:
        state: Playwright ``storage_state`` dict (output of
            ``BrowserContext.storage_state()``).
        include_optional: When ``True``, opt in to every label in
            :data:`notebooklm._auth.cookie_policy.OPTIONAL_COOKIE_DOMAINS_BY_LABEL`.
        include_domains: Set of optional-domain labels to opt in. ``"all"``
            is accepted as a shortcut for every label. Mirrors the rookiepy
            path semantics.

    Returns:
        A new ``storage_state`` dict with ``cookies`` filtered and ``origins``
        copied verbatim. The input dict is not mutated.
    """
    allowed_list = _build_google_cookie_domains(
        include_optional=include_optional, include_domains=include_domains
    )
    # Set membership for O(1) lookup over the (modest but non-trivial) regional
    # ccTLD list. Build a strip-leading-dot equivalence set so cookies stored
    # with the opposite dot prefix still match. ``http.cookiejar`` normalization
    # is the reason both ``accounts.google.com`` and ``.accounts.google.com``
    # are listed verbatim in :data:`REQUIRED_COOKIE_DOMAINS`; the equivalence
    # set is a defense-in-depth check in case a Playwright capture stores a
    # cookie with only one of the two forms.
    allowed: frozenset[str] = frozenset(allowed_list)
    allowed_stripped: frozenset[str] = frozenset(d.lstrip(".") for d in allowed_list)

    def _is_allowed(domain: str) -> bool:
        return domain in allowed or domain.lstrip(".") in allowed_stripped

    filtered_cookies = [
        cookie for cookie in state.get("cookies", []) if _is_allowed(cookie.get("domain", ""))
    ]
    return {
        "cookies": filtered_cookies,
        "origins": list(state.get("origins", [])),
    }


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
            exit_with_code(1)
        console.print("[green]Chromium installed successfully.[/green]\n")
    except SystemExit:
        raise
    except Exception as e:
        # FileNotFoundError: playwright CLI not found but sync_playwright imported
        # Other exceptions: dry-run check failed - let Playwright handle it during launch
        console.print(
            f"[dim]Warning: Chromium pre-flight check failed: {e}. Proceeding anyway.[/dim]"
        )


def _recover_page(context: BrowserContext, console: Console) -> Page:
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
            exit_with_code(1)
        # Not a TargetClosedError — don't mask the real problem
        logger.error("Failed to create new page for recovery: %s", error_str)
        raise


def _validate_login_flag_conflicts(
    *,
    browser_cookies: str | None,
    account_email: str | None,
    all_accounts: bool,
    update: bool,
    profile_name: str | None,
    storage: str | None,
) -> None:
    """Enforce ``login`` flag mutual-exclusion rules.

    Emits a styled error and ``exit_with_code(1)`` on the first conflict.
    The ``NOTEBOOKLM_AUTH_JSON`` env-var check is intentionally not handled
    here: it is an environment vs file-auth conflict, distinct from flag
    mutual-exclusion, and stays in the ``login`` orchestrator.
    """
    if browser_cookies is None and (
        account_email is not None or all_accounts or profile_name is not None
    ):
        console.print(
            "[red]Error: --account, --all-accounts, and --profile-name "
            "require --browser-cookies.[/red]"
        )
        exit_with_code(1)
    if all_accounts and (account_email is not None or profile_name is not None):
        console.print(
            "[red]Error: --all-accounts cannot be combined with --account or --profile-name.[/red]"
        )
        exit_with_code(1)
    if all_accounts and storage:
        console.print(
            "[red]Error: --all-accounts writes one profile per account "
            "and cannot be combined with --storage.[/red]"
        )
        exit_with_code(1)
    if update and not all_accounts:
        console.print("[red]Error: --update only applies to --all-accounts.[/red]")
        exit_with_code(1)


def _prepare_login_paths(
    profile: str | None, storage: str | None, fresh: bool
) -> tuple[Path, Path]:
    """Resolve storage and browser-profile paths for the Playwright login flow.

    Clears the cached browser profile on ``--fresh`` (exiting 1 on OSError),
    then creates both parent directories with platform-aware permissions.
    Returns ``(storage_path, browser_profile)``.
    """
    if storage:
        storage_path = Path(storage)
    elif profile:
        storage_path = get_storage_path(profile=profile)
    else:
        storage_path = get_storage_path()
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
            exit_with_code(1)

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

    return storage_path, browser_profile


def _run_playwright_login(
    *,
    browser: str,
    browser_profile: Path,
    storage_path: Path,
    include_domains: set[str] | None = None,
) -> None:
    """Drive the Playwright-based Google login and persist storage state.

    Imports Playwright lazily (raising ``SystemExit(1)`` with an install hint
    on ImportError), runs the chromium pre-flight when the bundled browser is
    selected, opens a persistent context, retries navigation on transient
    connection errors, waits for login completion, pins ``.google.com``
    cookies, applies the cookie-domain allowlist filter (P1-17), atomically
    writes ``storage_state.json``, and clears stale account metadata.

    Args:
        include_domains: Optional set of ``--include-domains`` labels. When
            ``None`` or empty, only :data:`REQUIRED_COOKIE_DOMAINS` and the
            regional ccTLDs survive the filter. Mirrors the rookiepy path so
            both login surfaces produce equivalent on-disk state.
    """
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
            install_hint = '  pip install "notebooklm-py[browser]"\n  playwright install chromium'
        console.print("[red]Playwright not installed. Run:[/red]")
        console.print(install_hint, markup=False)
        exit_with_code(1)

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
                    is_retryable = any(code in error_str for code in RETRYABLE_CONNECTION_ERRORS)
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
                        exit_with_code(1)
                    elif is_retryable:
                        # Exhausted retries on network errors
                        logger.error(
                            f"Failed to connect to NotebookLM after {LOGIN_MAX_RETRIES} attempts. "
                            f"Last error: {error_str}"
                        )
                        console.print(_connection_error_help())
                        exit_with_code(1)
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
                    exit_with_code(1)
                except PlaywrightError as exc:
                    # Browser/tab closed during the wait. Cannot resume a
                    # partially completed SSO form, so surface the same
                    # help text other browser-closed paths use.
                    if TARGET_CLOSED_ERROR in str(exc):
                        console.print(BROWSER_CLOSED_HELP)
                        exit_with_code(1)
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
                                exit_with_code(1)
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
                exit_with_code(1)

            # Atomic write with chmod 0o600 — Playwright's path= argument
            # writes directly (non-atomic + world-readable window).
            #
            # P1-17: apply the same cookie-domain allowlist that the rookiepy
            # path uses (``_build_google_cookie_domains``) so sibling-product
            # cookies (mail, myaccount, docs, youtube) the user happens to be
            # signed into in the same browser session don't leak into
            # ``storage_state.json``. Opt-in via ``--include-domains=...``
            # mirrors the rookiepy semantics.
            # Playwright's ``storage_state()`` returns a ``TypedDict``-shaped
            # value; widen to a plain ``dict[str, Any]`` for the filter helper
            # (which is independently testable without the Playwright import).
            playwright_state = context.storage_state()
            filtered_state: dict[str, Any] = _filter_storage_state_cookies_by_domain_policy(
                dict(playwright_state), include_domains=include_domains
            )
            atomic_write_json(storage_path, filtered_state)
            from ..auth import clear_account_metadata

            try:
                clear_account_metadata(storage_path)
            except OSError as exc:
                logger.warning(
                    "Failed to clear stale account metadata for %s: %s",
                    storage_path,
                    exc,
                )

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
                    exit_with_code(1)
            # Downgraded from ``logger.error(..., exc_info=True)``:
            # the previous traceback dump duplicated whatever ``handle_errors``
            # already shows the user. Keep the diagnostic available at
            # debug level (-vv) without flooding stderr by default. The
            # bare ``raise`` propagates to ``handle_errors`` which converts
            # it to a friendly ``Unexpected error: <msg>`` line + exit 2.
            logger.debug("Login failed: %s", e, exc_info=True)
            raise
        finally:
            # Always close the browser context to prevent resource leaks
            if context:
                context.close()


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
            "For Chromium-family profiles, target one with 'chrome::<profile>' "
            "(e.g. 'chrome::Profile 1' or 'brave::Work'). "
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
        "--update",
        "update",
        is_flag=True,
        default=False,
        help=(
            "With --all-accounts: when an account's natural profile name "
            "(e.g. 'alice' for alice@gmail.com) already exists but has no "
            "account metadata, update that profile in place instead of "
            "creating a suffixed 'alice-2'. Profiles that already bind a "
            "different email are still given a suffix to avoid clobbering. "
            "Only valid with --all-accounts."
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
    @click.option(
        "--include-domains",
        "include_domains_raw",
        multiple=True,
        default=(),
        help=(
            "Opt in to extracting sibling-product cookies (default: required "
            "Google auth/Drive cookies only). Pass labels comma-separated or "
            "repeat the flag: --include-domains=youtube,docs OR "
            "--include-domains=youtube --include-domains=docs. Supported "
            "labels: youtube, docs, myaccount, mail, all."
        ),
    )
    @click.pass_context
    def login(
        ctx,
        storage,
        browser,
        browser_cookies,
        account_email,
        all_accounts,
        update,
        profile_name,
        fresh,
        include_domains_raw,
    ):
        """Log in to NotebookLM via browser.

        Opens a browser window for Google login. Authentication is saved
        automatically once login is detected (no terminal interaction needed).

        Use --browser chrome if the bundled Chromium crashes (e.g. macOS 15+).
        Use --browser msedge if your organization requires Microsoft Edge for SSO.

        Note: Cannot be used when NOTEBOOKLM_AUTH_JSON is set (use file-based
        auth or unset the env var first).
        """
        # Wrap entire body in handle_errors so unexpected failures (e.g.
        # Playwright internal crashes that bubble out of the catch-all
        # except-block in _run_playwright_login) emit a friendly
        # 'Unexpected error: <msg>' line + exit 2 instead of a Python
        # traceback. Existing ``exit_with_code(N)`` calls inside the
        # body propagate unchanged — handle_errors does not intercept
        # SystemExit.
        with handle_errors():
            if os.environ.get("NOTEBOOKLM_AUTH_JSON"):
                console.print(
                    "[red]Error: Cannot run 'login' when NOTEBOOKLM_AUTH_JSON is set.[/red]\n"
                    "The NOTEBOOKLM_AUTH_JSON environment variable provides inline authentication,\n"
                    "which conflicts with browser-based login that saves to a file.\n\n"
                    "Either:\n"
                    "  1. Unset NOTEBOOKLM_AUTH_JSON and run 'login' again\n"
                    "  2. Continue using NOTEBOOKLM_AUTH_JSON for authentication"
                )
                exit_with_code(1)

            _validate_login_flag_conflicts(
                browser_cookies=browser_cookies,
                account_email=account_email,
                all_accounts=all_accounts,
                update=update,
                profile_name=profile_name,
                storage=storage,
            )

            # Parse + validate --include-domains. Raises click.BadParameter on
            # unknown labels (Click converts that to a non-zero exit + stderr
            # message).
            include_domains = _parse_include_domains(include_domains_raw)

            # rookiepy fast-path: skip Playwright entirely
            if browser_cookies is not None:
                if fresh:
                    console.print(
                        "[yellow]Warning: --fresh has no effect with --browser-cookies "
                        "(no browser profile is used).[/yellow]"
                    )
                # Warn only on the rookiepy path — Playwright does not consult
                # _build_google_cookie_domains, so the migration note would be
                # noise there.
                _warn_missing_optional_domains(include_domains)
                if all_accounts:
                    _login_all_accounts_from_browser(
                        browser_cookies,
                        update=update,
                        include_domains=include_domains,
                    )
                    return
                active_profile = ctx.obj.get("profile") if ctx.obj else None
                _login_browser_cookies_single(
                    browser_cookies,
                    storage=storage,
                    account_email=account_email,
                    profile_name=profile_name,
                    active_profile=active_profile,
                    include_domains=include_domains,
                )
                return

            # P1-17: the Playwright path now applies the same cookie-domain
            # allowlist as the rookiepy path. ``--include-domains`` opts the
            # named sibling-product domains back in; default keeps only
            # ``REQUIRED_COOKIE_DOMAINS`` + regional ccTLDs.
            profile = ctx.obj.get("profile") if ctx.obj else None
            storage_path, browser_profile = _prepare_login_paths(profile, storage, fresh)
            _run_playwright_login(
                browser=browser,
                browser_profile=browser_profile,
                storage_path=storage_path,
                include_domains=include_domains,
            )
            console.print(f"\n[green]Authentication saved to:[/green] {storage_path}")

            # Sync server language setting to local config so generate commands
            # respect the user's global language preference (fixes #121)
            _sync_server_language_to_config()

    @cli.command("use")
    @click.argument("notebook_id")
    @click.option(
        "--force",
        is_flag=True,
        default=False,
        help=(
            "Skip the existence check and persist the notebook ID even if "
            "verification fails. Use for offline work or debugging."
        ),
    )
    @click.option("--json", "json_output", is_flag=True, help="Output as JSON")
    @click.pass_context
    def use_notebook(ctx, notebook_id, force, json_output):
        """Set the current notebook context.

        Once set, all commands will use this notebook by default.
        You can still override by passing --notebook explicitly.

        Supports partial IDs - 'notebooklm use abc' matches 'abc123...'

        By default, the notebook must exist on the server; a typo or
        unreachable backend results in a non-zero exit and the saved
        context is left untouched. Pass --force to bypass verification.

        \b
        Example:
          notebooklm use nb123
          notebooklm ask "what is this about?"   # Uses nb123
          notebooklm generate video "a fun explainer"  # Uses nb123
        """
        # --force path: persist immediately without any RPC verification.
        # Useful when the network is unavailable or for debugging.
        if force:
            set_current_notebook(notebook_id)
            if json_output:
                # Surface the new active notebook id as the primary signal
                # so script callers can pipe `notebooklm use --json` straight
                # into downstream automation. ``verified: false`` mirrors the
                # "(not verified — --force)" cell in text mode.
                json_output_response(
                    {
                        "active_notebook_id": notebook_id,
                        "success": True,
                        "verified": False,
                    }
                )
                return
            table = _use_notebook_table()
            table.add_row(notebook_id, "(not verified — --force)", "-", "-")
            console.print(table)
            return

        async def _get(client: NotebookLMClient):
            # Resolve partial ID to full ID. Forward ``json_output`` so the
            # resolver's "Matched: …" partial-ID diagnostic routes to stderr
            # in --json mode and stdout stays pure parseable JSON for scripted
            # callers.
            resolved_id = await resolve_notebook_id(client, notebook_id, json_output=json_output)
            nb = await client.notebooks.get(resolved_id)
            return nb, resolved_id

        def _handle_use_verification_error(exc: Exception):
            if isinstance(exc, click.ClickException):
                # Re-raise click exceptions (from resolve_notebook_id —
                # partial-id ambiguity or "no match"). These already exit
                # non-zero with a clear message and never reach persistence.
                raise exc
            if isinstance(exc, NotebookNotFoundError):
                # Server confirmed the notebook does not exist. Fail closed:
                # do not persist anything to context.json.
                _output_error(
                    f"Error: Notebook {notebook_id!r} not found. "
                    "Run 'notebooklm list' to see available notebooks, "
                    "or pass --force to bypass verification.",
                    "NOT_FOUND",
                    json_output,
                    1,
                )
                raise AssertionError("unreachable")
            if isinstance(exc, AuthError):
                # Auth expired (e.g. SID/SSID cookies stale). Route through
                # the canonical "Run notebooklm login" UX instead of the
                # generic verification-failed fallback.
                handle_auth_error(json_output)
                raise AssertionError("unreachable")

            # All other failures (network errors, RPC errors, etc.) also fail
            # closed — we cannot confirm the notebook exists, so refuse to
            # persist. --force is the documented escape hatch.
            _output_error(
                f"Error: Could not verify notebook {notebook_id!r}: {exc}. "
                "Pass --force to persist without verification.",
                "VERIFICATION_FAILED",
                json_output,
                1,
            )
            raise AssertionError("unreachable")

        nb, resolved_id = run_client_workflow(
            ctx,
            command_name="session_use",
            json_output=json_output,
            body=_get,
            client_factory=NotebookLMClient,
            body_error_handler=_handle_use_verification_error,
        )

        created_str = nb.created_at.strftime("%Y-%m-%d") if nb.created_at else None
        set_current_notebook(resolved_id, nb.title, nb.is_owner, created_str)

        if json_output:
            # Scriptable envelope surfaces the new active notebook id plus
            # enough metadata that callers don't have to round-trip through
            # `notebooklm status --json` to render a confirmation.
            json_output_response(
                {
                    "active_notebook_id": resolved_id,
                    "success": True,
                    "verified": True,
                    "notebook": {
                        "id": resolved_id,
                        "title": nb.title,
                        "is_owner": nb.is_owner,
                        "created_at": nb.created_at.isoformat() if nb.created_at else None,
                    },
                }
            )
            return

        table = _use_notebook_table()

        created = created_str or "-"
        owner_status = "Owner" if nb.is_owner else "Shared"
        table.add_row(nb.id, nb.title, owner_status, created)

        console.print(table)

    @cli.command("status")
    @click.option("--json", "json_output", is_flag=True, help="Output as JSON")
    @click.option("--paths", "show_paths", is_flag=True, help="Show resolved file paths")
    def status(json_output, show_paths):
        """Show current context (active notebook and conversation).

        Use --paths to see where configuration files are located
        (useful for debugging NOTEBOOKLM_HOME).
        """
        # Reuse the shared helper so the same ``--storage`` resolution + path
        # canonicalization runs here as in ``_get_context_value`` and friends.
        # Keeps a single source of truth for "which context file does
        # ``--storage`` map to?" and avoids duplicating the normalization
        # logic (string→Path, expanduser, resolve) at every call site.
        storage_override = _current_storage_override()
        context_file = get_context_path(storage_path=storage_override)
        notebook_id = get_current_notebook()

        # Handle --paths flag
        if show_paths:
            path_info = get_path_info(storage_path=storage_override)
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
          notebooklm auth logout                       # Clear auth for active profile
          notebooklm -p work auth logout               # Clear auth for 'work' profile
          notebooklm --storage A.json auth logout      # Clear the override auth file
        """
        # Warn if env-based auth will remain active after logout
        if os.environ.get("NOTEBOOKLM_AUTH_JSON"):
            console.print(
                "[yellow]Note: NOTEBOOKLM_AUTH_JSON is set — env-based auth will "
                "remain active after logout. Unset it to fully log out.[/yellow]"
            )

        # When ``--storage <path>`` is active, that path IS the auth file. Using
        # the profile's storage_state.json instead would silently leave the
        # actual session credentials in place — see coderabbit feedback on #467.
        storage_override = _current_storage_override()
        storage_path = storage_override if storage_override is not None else get_storage_path()
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
                exit_with_code(1)

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
                exit_with_code(1)

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
            # Reuse the storage_override computed above so the diagnostic line
            # points at the actual sibling-context file when ``--storage`` is
            # active (matches the path that ``clear_context`` just tried).
            context_file = get_context_path(storage_path=storage_override)
            logger.error("Failed to remove context file %s: %s", context_file, exc)
            console.print(
                f"[red]Cannot remove context file: {exc}[/red]\n"
                "Close any running notebooklm commands and try again.\n"
                f"If the problem persists, manually delete: {context_file}"
            )
            exit_with_code(1)

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
            "Use 'chrome::<profile>' for one Chromium profile or "
            "'firefox::<container>' for one Firefox container. "
            "Requires: pip install 'notebooklm-py[cookies]'"
        ),
    )
    @click.option(
        "--include-domains",
        "include_domains_raw",
        multiple=True,
        default=(),
        help=(
            "Opt in to enumerating accounts via sibling-product cookies. "
            "Same syntax as 'notebooklm login --include-domains'. By "
            "default this command only consults required Google auth "
            "cookies, which is sufficient for account discovery on every "
            "tested path."
        ),
    )
    @click.option("--json", "json_output", is_flag=True, help="Output as JSON")
    @click.option(
        "-v",
        "--verbose",
        "verbose",
        is_flag=True,
        default=False,
        help=(
            "Also show which browser user-profile each account's cookies came "
            "from. Useful for Chromium-family browsers with multiple "
            "user-profiles."
        ),
    )
    def auth_inspect(browser_name, include_domains_raw, json_output, verbose):
        """List Google accounts visible to a browser's cookie store.

        Read-only — never writes to disk. Use this before
        ``notebooklm login --browser-cookies <browser> --account <email>`` to
        see which account emails are available.

        For Chromium-family browsers (chrome, brave, edge, …) with multiple
        user-profiles, accounts from every populated profile are surfaced and
        deduped by email. Pass ``-v`` to see the originating user-profile per
        account, or ``--json`` for a structured ``browser_profile`` field.
        Use ``chrome::<profile-name-or-directory>`` to inspect only one
        Chromium user-profile.

        \b
        Examples:
          notebooklm auth inspect --browser chrome
          notebooklm auth inspect --browser 'chrome::Profile 1'
          notebooklm auth inspect --browser chrome -v
          notebooklm auth inspect --browser firefox --json
        """
        include_domains = _parse_include_domains(include_domains_raw)
        _, accounts = _enumerate_browser_accounts(
            browser_name, verbose=not json_output, include_domains=include_domains
        )
        if json_output:
            json_output_response(
                {
                    "browser": browser_name,
                    "accounts": [
                        {
                            "email": a.email,
                            "is_default": a.is_default,
                            "browser_profile": a.browser_profile,
                        }
                        for a in accounts
                    ],
                }
            )
            return
        console.print(f"\n[bold]Browser:[/bold] {browser_name}")
        console.print(f"[bold]Found {len(accounts)} signed-in Google account(s):[/bold]\n")
        show_browser_profile = verbose and any(a.browser_profile for a in accounts)
        table = Table(show_header=True, header_style="bold")
        table.add_column("email")
        if show_browser_profile:
            table.add_column(f"{browser_name} user")
        table.add_column("default", justify="center")
        for a in accounts:
            row = [a.email]
            if show_browser_profile:
                row.append(a.browser_profile or "")
            row.append("[green]✓[/green]" if a.is_default else "")
            table.add_row(*row)
        console.print(table)
        hint = (
            f"Pick one with: [cyan]notebooklm login --browser-cookies "
            f"{browser_name} --account EMAIL[/cyan]\n"
            f"Or extract them all: [cyan]notebooklm login --browser-cookies "
            f"{browser_name} --all-accounts[/cyan]"
        )
        if not verbose and any(a.browser_profile for a in accounts):
            hint = (
                "[dim]Pass -v to see which browser user-profile each account "
                "came from.[/dim]\n" + hint
            )
        console.print("\n" + hint)

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
        - Required cookies (SID + ``__Secure-1PSIDTS``) are present
        - Cookie domains are correct

        Use --test to also verify tokens can be fetched from NotebookLM
        (requires network access).

        \b
        Examples:
          notebooklm auth check           # Quick local validation
          notebooklm auth check --test    # Full validation with network test
          notebooklm auth check --json    # Machine-readable output
        """
        from ..auth import extract_cookies_from_storage

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
        except (OSError, UnicodeDecodeError) as e:
            # ``storage_path.read_text(encoding="utf-8")`` can raise:
            #   - ``OSError`` subclasses (PermissionError, IsADirectoryError,
            #     FileNotFoundError if the earlier ``exists()`` race-lost, …)
            #   - ``UnicodeDecodeError`` if the file exists but isn't valid
            #     UTF-8 (a corrupted or truncated storage_state.json).
            # Route both through the same structured renderer so --json
            # callers get a parseable ``status: "error"`` envelope (with
            # non-zero exit) instead of a raw Python traceback.
            details["error"] = f"Storage unreadable: {e}"
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
            # When checks fail, the JSON payload reports status="error" — the
            # process exit code must agree so callers can fail-fast on
            # `notebooklm auth check --json`.
            if not all_passed:
                exit_with_code(1)
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
            "firefox, brave, edge, safari, arc, ... Use 'chrome::<profile>' "
            "for one Chromium profile or 'firefox::<container>' for one "
            "Firefox container."
        ),
    )
    @click.option(
        "--include-domains",
        "include_domains_raw",
        multiple=True,
        default=(),
        help=(
            "Forward to the browser-cookie reader (only meaningful with "
            "--browser-cookies). Same syntax as 'notebooklm login "
            "--include-domains'."
        ),
    )
    @click.option(
        "--quiet", "-q", is_flag=True, help="Suppress success output (only print on error)"
    )
    @click.pass_context
    def auth_refresh(ctx, browser_cookies, include_domains_raw, quiet):
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
        # Wrap the entire body in handle_errors: typed exceptions
        # (AuthError, NetworkError, ValidationError, ...) get user-friendly
        # one-liners + hints; unexpected exceptions become 'Unexpected error:
        # <msg>' (exit 2) instead of leaking ``type(exc).__name__`` into the
        # user message. Existing ``exit_with_code(N)`` calls inside the body
        # propagate unchanged — handle_errors does not intercept SystemExit.
        with handle_errors():
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
                exit_with_code(1)

            include_domains = _parse_include_domains(include_domains_raw)
            if include_domains and browser_cookies is None:
                click.echo(
                    "Error: --include-domains only applies when --browser-cookies "
                    "is also set (the keepalive-only path does not re-extract cookies).",
                    err=True,
                )
                exit_with_code(1)

            profile = ctx.obj.get("profile") if ctx.obj else None
            storage_path = get_storage_path(profile=profile)

            if browser_cookies is not None:
                _refresh_from_browser_cookies(
                    browser_cookies,
                    storage_path=storage_path,
                    profile=profile,
                    quiet=quiet,
                    include_domains=include_domains,
                )
                return

            run_async(fetch_tokens_with_domains(storage_path, profile))

            if not quiet:
                console.print(f"[green]ok[/green] refreshed: {storage_path}")
