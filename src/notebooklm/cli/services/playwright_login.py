"""Playwright-driven Google login service (ADR-008 click-to-service extraction).

Owns the entire Playwright fast path for ``notebooklm login`` (the rookiepy
``--browser-cookies`` path stays in :mod:`notebooklm.cli.services.login`).
Extracted from :mod:`notebooklm.cli.session_cmd` in P3.T3 so the Click
handler can stay a thin orchestrator.

Public entry points
===================

* :class:`PlaywrightLoginPlan` — frozen dataclass describing one Playwright
  login attempt (browser channel, target paths, optional cookie-domain
  inclusion).
* :func:`run_playwright_login` — synchronous executor: opens the browser,
  drives the SSO flow, persists ``storage_state.json`` atomically.
* :func:`prepare_login_paths` — resolves storage + browser profile paths
  for a ``login`` invocation (including the ``--fresh`` profile wipe).
* :func:`validate_login_flag_conflicts` — flag mutual-exclusion gate.
* :func:`filter_storage_state_cookies_by_domain_policy` — applies the
  P1-17 cookie-domain allowlist to a Playwright ``storage_state`` dict.

The handler keeps the legacy ``_run_playwright_login`` / ``_prepare_login_paths``
patch surfaces (re-exported via ``session_cmd.py``) so existing tests
that ``patch("notebooklm.cli.session_cmd._run_playwright_login")`` keep
working byte-for-byte.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import httpx

from ...config import get_base_host, get_base_url
from ...io import atomic_write_json
from ...paths import get_browser_profile_dir, get_storage_path
from ..error_handler import exit_with_code
from ..rendering import console
from ..runtime import run_async

# Capture the original function references at module-import time so the
# ``_resolve_paths_helper`` precedence chain can compare each lookup
# against the never-changing original (not the live module-level
# binding, which may itself be patched by tests).
_ORIGINAL_GET_BROWSER_PROFILE_DIR = get_browser_profile_dir
_ORIGINAL_GET_STORAGE_PATH = get_storage_path


# Resolve path helpers via a test-aware precedence chain so patches at
# ``notebooklm.cli.session_cmd.<sym>``,
# ``notebooklm.cli.services.playwright_login.<sym>``, AND
# ``notebooklm.paths.<sym>`` all intercept the service-layer call.
#
# ``default`` is the import-time function reference closed over by the
# caller (the symbol imported at module-load time). Comparisons go
# against ``default`` (NOT against the live ``notebooklm.paths`` attribute)
# so a patch at the canonical source does not falsely flag a stale local
# binding as "patched" — rev-3 CodeRabbit feedback on #962.
#
# Precedence:
#   1. Service module's own binding if patched.
#   2. ``session_cmd``'s binding if patched.
#   3. Live ``notebooklm.paths`` value (which may itself be patched).
def _resolve_paths_helper(name: str, default):
    import sys as _sys

    from ... import paths as _paths_module

    service_mod = _sys.modules.get(__name__)
    if service_mod is not None:
        local = getattr(service_mod, name, default)
        if local is not default:
            return local
    session_cmd = _sys.modules.get("notebooklm.cli.session_cmd")
    if session_cmd is not None:
        from_session = getattr(session_cmd, name, default)
        if from_session is not default:
            return from_session
    return getattr(_paths_module, name, default)


if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext, Page
    from rich.console import Console

logger = logging.getLogger(__name__)

GOOGLE_ACCOUNTS_URL = "https://accounts.google.com/"

# Retryable Playwright connection errors. Tracked by string-fragment match
# because Playwright surfaces them in the error message rather than via
# typed exceptions.
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
ACCOUNT_METADATA_REMEDIATION = (
    "Run [cyan]notebooklm auth inspect --browser chrome -v[/cyan] "
    "or [cyan]notebooklm login --browser-cookies chrome --account EMAIL[/cyan]."
)

# Browsers launched via Playwright's ``channel`` parameter (system-installed,
# not the bundled Chromium). Maps channel name -> (display label, install URL).
# Used for the --browser option, the launch banner, and the not-installed
# error path. The bundled "chromium" choice is intentionally absent.
CHANNEL_BROWSERS: dict[str, tuple[str, str]] = {
    "msedge": ("Microsoft Edge", "https://www.microsoft.com/edge"),
    "chrome": ("Google Chrome", "https://www.google.com/chrome"),
}


# ---------------------------------------------------------------------------
# Cookie-domain filter (kept here because it's only consumed by the
# Playwright path — the rookiepy path applies its own allowlist upstream)
# ---------------------------------------------------------------------------


def filter_storage_state_cookies_by_domain_policy(
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
    # Late import to avoid a hard dependency cycle: services/login imports
    # services/cookie_domains, and the Playwright service has no cookie
    # domain policy of its own.
    from .login import _build_google_cookie_domains

    allowed_list = _build_google_cookie_domains(
        include_optional=include_optional, include_domains=include_domains
    )
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


# ---------------------------------------------------------------------------
# Playwright account metadata repair
# ---------------------------------------------------------------------------


def _select_playwright_account(
    accounts: list[Any],
    *,
    active_email: str | None,
) -> tuple[Any | None, str | None]:
    """Select the account Playwright just logged into, or return an ambiguity reason."""
    if active_email:
        normalized = active_email.casefold()
        matches = [
            account
            for account in accounts
            if isinstance(getattr(account, "email", None), str)
            and account.email.casefold() == normalized
        ]
        if len(matches) == 1:
            return matches[0], None
        if matches:
            return None, f"multiple discovered accounts matched {active_email}"
        return None, f"current NotebookLM page email {active_email} was not discovered"

    if len(accounts) == 1:
        return accounts[0], None
    if accounts:
        return (
            None,
            "multiple Google accounts were discovered but the active page email was unavailable",
        )
    return None, "no Google accounts were discovered"


def repair_playwright_account_metadata(
    storage_path: Path,
    *,
    page_html: str | None = None,
    quiet: bool = False,
) -> bool:
    """Populate ``notebooklm.account`` from Playwright storage when unambiguous.

    This is used immediately after interactive Playwright login and by
    file-backed ``auth refresh`` as a repair path for older Playwright-created
    storage states. Ambiguous multi-account states are deliberately left
    unbound after clearing any stale metadata.

    Returns:
        ``True`` when metadata was written, ``False`` when it was cleared or
        left absent.
    """
    from ...auth import (
        build_httpx_cookies_from_storage,
        clear_account_metadata,
        enumerate_accounts,
        extract_email_from_html,
        write_account_metadata,
    )

    active_email = extract_email_from_html(page_html) if isinstance(page_html, str) else None
    try:
        if not quiet:
            console.print("[dim]Identifying Google account...[/dim]")
        jar = build_httpx_cookies_from_storage(storage_path)
        accounts = run_async(enumerate_accounts(jar))
        selected, reason = _select_playwright_account(accounts, active_email=active_email)
        if selected is None:
            clear_account_metadata(storage_path)
            if not quiet:
                console.print(
                    "[yellow]Warning: account metadata was not written; "
                    f"{reason}. {ACCOUNT_METADATA_REMEDIATION}[/yellow]"
                )
            return False
        write_account_metadata(
            storage_path,
            authuser=selected.authuser,
            email=selected.email,
        )
    except (OSError, ValueError, RuntimeError, httpx.HTTPError) as exc:
        try:
            clear_account_metadata(storage_path)
        except Exception as clear_exc:
            logger.warning(
                "Failed to clear stale account metadata for %s: %s",
                storage_path,
                clear_exc,
            )
        if not quiet:
            console.print(
                "[yellow]Warning: account metadata was not written. "
                "NotebookLM auth still saved, but multi-account routing may "
                "fall back to authuser=0. "
                f"{ACCOUNT_METADATA_REMEDIATION} Details: {exc}[/yellow]"
            )
        return False

    if not quiet:
        console.print(f"[green]Account:[/green] {selected.email}")
    return True


# ---------------------------------------------------------------------------
# Platform / browser pre-flight helpers
# ---------------------------------------------------------------------------


@contextmanager
def windows_playwright_event_loop() -> Iterator[None]:
    """Temporarily restore the default event loop policy for Playwright on Windows.

    Playwright's sync API uses subprocess to spawn the browser, which requires
    ``ProactorEventLoop`` on Windows. The CLI sets
    ``WindowsSelectorEventLoopPolicy`` globally (issue #79) which is incompatible
    with that subprocess path. This context manager swaps the policy in for the
    Playwright section, then restores the selector policy on exit.

    No-op on non-Windows platforms.
    """
    if sys.platform != "win32":
        yield
        return

    original_policy = asyncio.get_event_loop_policy()
    asyncio.set_event_loop_policy(asyncio.DefaultEventLoopPolicy())
    try:
        yield
    finally:
        asyncio.set_event_loop_policy(original_policy)


def ensure_chromium_installed() -> None:
    """Check if Chromium is installed and install if needed.

    Runs ``playwright install --dry-run chromium`` to detect a missing browser,
    then auto-installs. Silently proceeds on any error so Playwright handles
    them during launch.

    Both subprocess calls are bounded by timeouts so a network-stalled
    Playwright CLI cannot hang ``notebooklm login`` indefinitely (rev-1
    CodeRabbit feedback on #962): 30 s for the dry-run probe, 300 s for
    the install. ``TimeoutExpired`` is treated as a pre-flight failure —
    the warning surfaces and login continues; Playwright will surface the
    real error during browser launch.
    """
    try:
        result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "--dry-run", "chromium"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        stdout_lower = result.stdout.lower()
        if "chromium" not in stdout_lower or "will download" not in stdout_lower:
            return

        console.print("[yellow]Chromium browser not installed. Installing now...[/yellow]")
        install_result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            capture_output=True,
            text=True,
            timeout=300,
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
    except subprocess.TimeoutExpired as exc:
        # Network stall during download or a hung subprocess; surface the
        # diagnostic and let Playwright handle the real launch error.
        console.print(
            f"[dim]Warning: Chromium pre-flight check timed out after "
            f"{exc.timeout}s. Proceeding anyway.[/dim]"
        )
    except Exception as e:
        # FileNotFoundError: playwright CLI not found but sync_playwright imported
        # Other exceptions: dry-run check failed — let Playwright handle it during launch.
        console.print(
            f"[dim]Warning: Chromium pre-flight check failed: {e}. Proceeding anyway.[/dim]"
        )


def recover_page(context: BrowserContext, console_: Console) -> Page:
    """Get a fresh page from a persistent browser context.

    Used when the current page reference is stale (TargetClosedError).
    A new page in a persistent context inherits all cookies and storage.

    Returns a new ``Page``, or raises ``SystemExit`` if the context/browser
    is dead. Re-raises the original ``PlaywrightError`` for non-TargetClosed
    failures.
    """
    from playwright.sync_api import Error as PlaywrightError

    try:
        return context.new_page()
    except PlaywrightError as exc:
        error_str = str(exc)
        if TARGET_CLOSED_ERROR in error_str:
            logger.error("Browser context is dead, cannot recover page: %s", error_str)
            console_.print(BROWSER_CLOSED_HELP)
            exit_with_code(1)
        logger.error("Failed to create new page for recovery: %s", error_str)
        raise


# ---------------------------------------------------------------------------
# Small URL helpers used by the Playwright SSO flow
# ---------------------------------------------------------------------------


def is_navigation_interrupted_error(error: str | Exception) -> bool:
    """Return True for Playwright navigation races that are safe to ignore."""
    error_str = str(error).lower()
    return any(marker in error_str for marker in _NAVIGATION_INTERRUPTED_MARKERS)


def url_matches_base_host(url: str) -> bool:
    """Return True when ``url`` is on the configured NotebookLM host."""
    current_host = (urlparse(url).hostname or "").lower()
    return current_host == get_base_host().lower()


def connection_error_help() -> str:
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


# ---------------------------------------------------------------------------
# Flag validation + path preparation
# ---------------------------------------------------------------------------


def validate_login_flag_conflicts(
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
    The env-supplied-auth check is intentionally not handled
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


def prepare_login_paths(profile: str | None, storage: str | None, fresh: bool) -> tuple[Path, Path]:
    """Resolve storage and browser-profile paths for the Playwright login flow.

    Clears the cached browser profile on ``--fresh`` (exiting 1 on OSError),
    then creates both parent directories with platform-aware permissions.
    Returns ``(storage_path, browser_profile)``.
    """
    # Resolve through the test-aware precedence chain so patches at
    # ``session_cmd``, ``services.playwright_login``, or
    # ``notebooklm.paths`` all reach this call site. The ``_ORIGINAL_*``
    # constants captured at import time are the never-changing references.
    _get_storage_path = _resolve_paths_helper("get_storage_path", _ORIGINAL_GET_STORAGE_PATH)
    _get_browser_profile_dir = _resolve_paths_helper(
        "get_browser_profile_dir", _ORIGINAL_GET_BROWSER_PROFILE_DIR
    )
    if storage:
        storage_path = Path(storage)
    elif profile:
        storage_path = _get_storage_path(profile=profile)
    else:
        storage_path = _get_storage_path()
    browser_profile = _get_browser_profile_dir()

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


# ---------------------------------------------------------------------------
# Playwright entry point
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlaywrightLoginPlan:
    """Frozen description of one Playwright login attempt.

    Fields:
        browser: Browser channel; one of ``"chromium"`` or any key of
            :data:`CHANNEL_BROWSERS` (``"chrome"``, ``"msedge"``).
        browser_profile: Persistent-context directory Playwright launches
            against. Survives across login attempts so the session
            persists for the user.
        storage_path: Destination for the captured ``storage_state.json``.
        include_domains: Optional ``--include-domains`` labels. ``None`` /
            empty means "only required Google cookies + regional ccTLDs."
    """

    browser: str
    browser_profile: Path
    storage_path: Path
    include_domains: set[str] | None = None


def run_playwright_login(plan: PlaywrightLoginPlan) -> None:
    """Drive the Playwright-based Google login and persist storage state.

    Imports Playwright lazily (raising ``SystemExit(1)`` with an install hint
    on ImportError), runs the chromium pre-flight when the bundled browser is
    selected, opens a persistent context, retries navigation on transient
    connection errors, waits for login completion, pins ``.google.com``
    cookies, applies the cookie-domain allowlist filter (P1-17), atomically
    writes ``storage_state.json``, and writes account metadata when the active
    Google account can be identified safely.
    """
    browser = plan.browser
    browser_profile = plan.browser_profile
    storage_path = plan.storage_path
    include_domains = plan.include_domains

    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import TimeoutError as PlaywrightTimeout
        from playwright.sync_api import sync_playwright
    except ImportError:
        # NOTE: passing markup=False so rich does not interpret `[browser]` as a style tag
        # (which would strip it, leaving the user with `pip install "notebooklm-py"` — no extras).
        if browser in CHANNEL_BROWSERS:
            install_hint = '  pip install "notebooklm-py[browser]"'
        else:
            install_hint = '  pip install "notebooklm-py[browser]"\n  playwright install chromium'
        console.print("[red]Playwright not installed. Run:[/red]")
        console.print(install_hint, markup=False)
        exit_with_code(1)

    # Pre-flight check: verify Chromium browser is installed (system Chrome
    # and Edge are checked at launch time by Playwright's channel routing).
    # Resolve via ``session_cmd`` so legacy tests that patch
    # ``notebooklm.cli.session_cmd._ensure_chromium_installed`` still
    # intercept the call (the symbol is re-exported there with an
    # underscore prefix).
    if browser == "chromium":
        import sys as _sys

        session_cmd = _sys.modules.get("notebooklm.cli.session_cmd")
        _ensure = (
            getattr(session_cmd, "_ensure_chromium_installed", ensure_chromium_installed)
            if session_cmd is not None
            else ensure_chromium_installed
        )
        _ensure()

    def _capture_page_html(page: Any) -> str | None:
        try:
            content = page.content()
        except PlaywrightError as exc:
            logger.debug("Could not read Playwright page content for account metadata: %s", exc)
            return None
        return content if isinstance(content, str) else None

    from ...paths import resolve_profile

    profile_name = resolve_profile()
    channel_info = CHANNEL_BROWSERS.get(browser)
    browser_label = channel_info[0] if channel_info else "Chromium"
    console.print(f"[dim]Profile: {profile_name}[/dim]")
    console.print(f"[yellow]Opening {browser_label} for Google login...[/yellow]")
    console.print(f"[dim]Using persistent profile: {browser_profile}[/dim]")

    account_metadata_page_html: str | None = None
    should_repair_account_metadata = False

    # Use context manager to restore ProactorEventLoop for Playwright on Windows
    # (fixes #89: NotImplementedError on Windows Python 3.12)
    with windows_playwright_event_loop(), sync_playwright() as p:
        launch_kwargs: dict[str, Any] = {
            "user_data_dir": str(browser_profile),
            "headless": False,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--password-store=basic",  # Avoid macOS keychain encryption for headless compatibility
            ],
            "ignore_default_args": ["--enable-automation"],
        }
        if browser in CHANNEL_BROWSERS:
            launch_kwargs["channel"] = browser

        context = None
        try:
            context = p.chromium.launch_persistent_context(**launch_kwargs)

            page = context.pages[0] if context.pages else recover_page(context, console)

            # Retry navigation on transient connection errors with backoff
            for attempt in range(1, LOGIN_MAX_RETRIES + 1):
                try:
                    page.goto(f"{get_base_url()}/", timeout=30000)
                    break
                except PlaywrightError as exc:
                    error_str = str(exc)
                    is_retryable = any(code in error_str for code in RETRYABLE_CONNECTION_ERRORS)
                    is_target_closed = TARGET_CLOSED_ERROR in error_str

                    if (is_retryable or is_target_closed) and attempt < LOGIN_MAX_RETRIES:
                        if is_target_closed:
                            page = recover_page(context, console)

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
                        logger.error(
                            "Browser closed during login after %d attempts. Last error: %s",
                            LOGIN_MAX_RETRIES,
                            error_str,
                        )
                        console.print(BROWSER_CLOSED_HELP)
                        exit_with_code(1)
                    elif is_retryable:
                        logger.error(
                            f"Failed to connect to NotebookLM after {LOGIN_MAX_RETRIES} attempts. "
                            f"Last error: {error_str}"
                        )
                        console.print(connection_error_help())
                        exit_with_code(1)
                    else:
                        logger.debug("Non-retryable error: %s", error_str)
                        raise

            if url_matches_base_host(page.url):
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

            active_page_html = _capture_page_html(page)

            # Force .google.com cookies for regional users (e.g. UK lands on
            # .google.co.uk). Use "commit" to resolve once response headers
            # (including Set-Cookie) are processed, before any client-side
            # JS redirect can interrupt. See #214.
            recovered_during_cookie_forcing = False
            for url in [GOOGLE_ACCOUNTS_URL, f"{get_base_url()}/"]:
                try:
                    page.goto(url, wait_until="commit")
                except PlaywrightError as exc:
                    error_str = str(exc)
                    if TARGET_CLOSED_ERROR in error_str:
                        # Page was destroyed (e.g. user switched accounts) -- get fresh page
                        page = recover_page(context, console)
                        recovered_during_cookie_forcing = True
                        try:
                            page.goto(url, wait_until="commit")
                        except PlaywrightError as inner_exc:
                            if TARGET_CLOSED_ERROR in str(inner_exc):
                                console.print(BROWSER_CLOSED_HELP)
                                exit_with_code(1)
                            elif not is_navigation_interrupted_error(inner_exc):
                                raise
                    elif not is_navigation_interrupted_error(error_str):
                        raise

            # Defense-in-depth: wait_for_url proved we reached the host,
            # but the cookie-forcing round-trip above can land us back on
            # accounts.google.com if the session was invalidated mid-flow
            # (rare, but the old interactive path defended against this
            # via a "save anyway?" confirm). Auto-detect is non-interactive,
            # so fail fast with a clear next step instead.
            if not url_matches_base_host(page.url):
                console.print(
                    f"[red]Unexpected URL after login: {page.url}[/red]\n"
                    "Authentication may be incomplete. "
                    "Try: notebooklm login --fresh"
                )
                exit_with_code(1)

            if recovered_during_cookie_forcing:
                active_page_html = _capture_page_html(page)

            # Atomic write with chmod 0o600 — Playwright's path= argument
            # writes directly (non-atomic + world-readable window).
            #
            # P1-17: apply the same cookie-domain allowlist that the rookiepy
            # path uses (``_build_google_cookie_domains``) so sibling-product
            # cookies (mail, myaccount, docs, youtube) the user happens to be
            # signed into in the same browser session don't leak into
            # ``storage_state.json``. Opt-in via ``--include-domains=...``
            # mirrors the rookiepy semantics.
            playwright_state = context.storage_state()
            filtered_state: dict[str, Any] = filter_storage_state_cookies_by_domain_policy(
                dict(playwright_state), include_domains=include_domains
            )
            atomic_write_json(storage_path, filtered_state)
            account_metadata_page_html = active_page_html
            should_repair_account_metadata = True

        except Exception as e:
            # Handle browser launch errors specially (context will be None if launch failed)
            if context is None and browser in CHANNEL_BROWSERS:
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
                    label, install_url = CHANNEL_BROWSERS[browser]
                    logger.error("%s not found: %s", label, e)
                    console.print(
                        f"[red]{label} not found.[/red]\n"
                        f"Install from: {install_url}\n"
                        "Or use the default Chromium browser: notebooklm login"
                    )
                    exit_with_code(1)
            # Keep the diagnostic available at debug level without flooding stderr
            # by default. The bare ``raise`` propagates to ``handle_errors`` which
            # converts it to a friendly ``Unexpected error: <msg>`` line + exit 2.
            logger.debug("Login failed: %s", e, exc_info=True)
            raise
        finally:
            if context:
                context.close()

    if should_repair_account_metadata:
        repair_playwright_account_metadata(storage_path, page_html=account_metadata_page_html)


__all__ = [
    "BROWSER_CLOSED_HELP",
    "CHANNEL_BROWSERS",
    "GOOGLE_ACCOUNTS_URL",
    "LOGIN_MAX_RETRIES",
    "RETRYABLE_CONNECTION_ERRORS",
    "TARGET_CLOSED_ERROR",
    "PlaywrightLoginPlan",
    "connection_error_help",
    "ensure_chromium_installed",
    "filter_storage_state_cookies_by_domain_policy",
    "is_navigation_interrupted_error",
    "prepare_login_paths",
    "recover_page",
    "repair_playwright_account_metadata",
    "run_playwright_login",
    "url_matches_base_host",
    "validate_login_flag_conflicts",
    "windows_playwright_event_loop",
]
