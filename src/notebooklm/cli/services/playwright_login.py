"""Playwright-driven Google login service (ADR-0008 click-to-service extraction).

Owns the entire Playwright fast path for ``notebooklm login`` (the rookiepy
``--browser-cookies`` path stays in :mod:`notebooklm.cli.services.login`). The
Click handler stays a thin orchestrator over this service.

Presentation / exit / async-runner side effects are inverted behind the
:class:`LoginIO` Protocol: callers inject a concrete sink (the command-layer
:mod:`notebooklm.cli.playwright_login_io`) so this module imports no
``..rendering`` / ``..error_handler`` / ``..runtime`` command modules (#1391,
ADR-0008 level-2-import boundary). Pre-flight helpers return typed outcomes
(:class:`Conflict`, :class:`PreparedPaths`, :class:`PathError`); the command
wrappers render + exit. Entry points: :class:`PlaywrightLoginPlan`,
:func:`run_playwright_login`, :func:`prepare_login_paths`,
:func:`validate_login_flag_conflicts`,
:func:`filter_storage_state_cookies_by_domain_policy`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Awaitable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn, Protocol
from urllib.parse import urlparse

import httpx

from ...config import get_base_host, get_base_url
from ...io import atomic_write_json
from ...paths import get_browser_profile_dir, get_storage_path

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext, Page

logger = logging.getLogger(__name__)


class LoginIO(Protocol):
    """Caller-injected sink for the Playwright login flow's side effects.

    The command layer (:mod:`notebooklm.cli.playwright_login_io`) injects a
    concrete impl so this service never imports the presentation
    (``..rendering``), exit-policy (``..error_handler``), or async-runner
    (``..runtime``) command modules directly (ADR-0008 boundary). ``emit``
    forwards to ``console.print`` (``*args, **kwargs`` pass through verbatim,
    incl. ``markup=False``); ``fail`` forwards to ``exit_with_code`` (raises
    ``SystemExit``); ``run_async`` forwards to ``run_async``.
    """

    def emit(self, *args: Any, **kwargs: Any) -> None: ...

    def fail(self, code: int) -> NoReturn: ...

    def run_async(self, coro: Awaitable[Any]) -> Any: ...


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
# Subprocess output sanitisation
# ---------------------------------------------------------------------------
#
# Captured stderr/stdout from a Playwright subprocess (e.g. the install-failure
# path below) can leak two classes of noise into the console:
#   1. Environment-variable VALUES — Playwright forwards the parent env, so a
#      secret (PSIDTS, API tokens, auth-source / SAPISID cookie material)
#      interpolated into a traceback lands verbatim in ``result.stderr``.
#   2. ANSI control sequences — pip/playwright progress bars + colour codes.
# ``redact_subprocess_output`` strips both. Env-var redaction is conservative:
# empty / single-char / boolean-ish / path-separator constants are skipped to
# avoid false positives across normal stderr lines.

# CSI: ESC '[' parameter-bytes intermediate-bytes final-byte
# OSC: ESC ']' ... (BEL | ESC '\\')
# Plus a catch-all for any remaining two-byte C1 Fe sequence (the
# 0x40-0x5F final-byte range). CSI and OSC are stripped first so this
# only fires on leftovers (PM ``ESC ^``, APC ``ESC _``, ST ``ESC \``,
# etc.).
_ANSI_CSI_PATTERN = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")
_ANSI_OSC_PATTERN = re.compile(r"\x1B\][^\x07\x1B]*(?:\x07|\x1B\\)")
_ANSI_OTHER_PATTERN = re.compile(r"\x1B[@-_]")

# Env-var values we never redact even if they happen to be set (they would
# produce noisy false positives on every line that mentions a path / boolean).
# Single-character values (``/``, ``.``, ``*``, ``0``, ``1``, ``y``, ``n``)
# don't appear here — they are already excluded by ``_REDACTION_MIN_VALUE_LEN``.
_REDACTION_SAFE_VALUES = frozenset(
    {
        "",
        "..",
        "true",
        "false",
        "True",
        "False",
        "TRUE",
        "FALSE",
        "yes",
        "no",
        "on",
        "off",
    }
)

# Skip env values shorter than this — substring matches on 2-char strings
# false-positive across the bytes Playwright prints.
_REDACTION_MIN_VALUE_LEN = 3


def _strip_ansi(text: str) -> str:
    """Remove ANSI CSI / OSC / two-byte escape sequences from ``text``."""
    text = _ANSI_CSI_PATTERN.sub("", text)
    text = _ANSI_OSC_PATTERN.sub("", text)
    text = _ANSI_OTHER_PATTERN.sub("", text)
    return text


def _expand_nested_secret_values(value: str) -> Iterator[str]:
    """Yield ``value`` plus any nested string leaves if it parses as JSON.

    Env values supplied as inline JSON (the auth-source env var being
    the canonical example) carry serialised dicts whose leaf strings
    (cookie tokens, refresh tokens) are the actual secrets. If a
    subprocess re-emits the parsed nested value rather than the whole
    JSON blob, exact-string matching against the original env value
    would miss the leak. Walk JSON objects/arrays here to add every
    leaf string to the redaction candidate set.

    Non-JSON values yield just themselves (and only if they pass the
    caller's length / safe-value filter).
    """
    yield value
    stripped = value.strip()
    if not stripped or stripped[0] not in "{[":
        return
    try:
        parsed = json.loads(stripped)
    except (ValueError, TypeError):
        return

    stack: list[Any] = [parsed]
    while stack:
        node = stack.pop()
        if isinstance(node, str):
            yield node
        elif isinstance(node, dict):
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)


def redact_subprocess_output(text: str, env: Mapping[str, str] | None = None) -> str:
    """Sanitise captured subprocess ``stdout`` / ``stderr`` before printing.

    Runs in two passes (detail in the inline comments + module header):

    1. **Strip ANSI control sequences** FIRST so a secret split by an inert
       reset (``"abc\\x1b[0m123"`` → ``"abc123"``) is reassembled before the
       exact-match redactor runs — otherwise it would miss it.
    2. **Replace each non-trivial env-var value** (plus any JSON-nested leaf
       strings) with ``<redacted>``. The env is snapshotted from ``os.environ``
       unless ``env`` is supplied. Values under
       :data:`_REDACTION_MIN_VALUE_LEN` and the :data:`_REDACTION_SAFE_VALUES`
       constants are skipped; candidates are tried longest-first so a longer
       secret containing a shorter one is redacted as one token.

    Returns the sanitised string; the input is not mutated.
    """
    if not text:
        return text

    # Pass 1: strip ANSI BEFORE redaction so a secret broken up by reset
    # codes (``"abc\x1b[0m123"`` → ``"abc123"``) is reassembled and
    # redactable by the exact-match pass below.
    text = _strip_ansi(text)

    # ``dict(os.environ)`` defends against another thread mutating the process
    # environment mid-iteration (rare).
    source_env: Mapping[str, str] = dict(os.environ) if env is None else env

    # Candidate set: every env value + any JSON-nested leaf strings, plus the
    # ansi-stripped form of each (the env value may carry embedded control
    # bytes; ``text`` is already stripped). Sorted longest-first below so a
    # longer secret is redacted before any shorter prefix.
    candidates: set[str] = set()
    for raw_value in source_env.values():
        if not isinstance(raw_value, str):
            continue
        for nested in _expand_nested_secret_values(raw_value):
            for variant in (nested, _strip_ansi(nested)):
                if (
                    len(variant) >= _REDACTION_MIN_VALUE_LEN
                    and variant not in _REDACTION_SAFE_VALUES
                ):
                    candidates.add(variant)

    for value in sorted(candidates, key=len, reverse=True):
        if value in text:
            text = text.replace(value, "<redacted>")

    return text


# ---------------------------------------------------------------------------
# Cookie-domain filter (kept here — only the Playwright path consumes it)
# ---------------------------------------------------------------------------


def filter_storage_state_cookies_by_domain_policy(
    state: dict[str, Any],
    *,
    include_optional: bool = False,
    include_domains: set[str] | None = None,
) -> dict[str, Any]:
    """Filter a Playwright ``storage_state`` dict to the configured cookie-domain policy.

    The Playwright login flow captures every cookie the browser context holds.
    Without this filter, sibling-product cookies (``mail.google.com``,
    ``myaccount.google.com``, ``docs.google.com``, ``.youtube.com``) the user
    happens to be signed into leak into the persisted ``storage_state.json``
    and inflate the blast radius. This applies the same allowlist the rookiepy
    path uses (:func:`_build_google_cookie_domains`) at write time so both
    login paths produce equivalent on-disk state, opt-in via
    ``--include-domains=...``. The match is exact-against-allowlist with
    leading-dot/no-dot equivalence (``http.cookiejar`` may normalize either);
    sibling subdomains are deliberately NOT matched by a broad ``.google.com``
    suffix — that's the bug being fixed.

    Args:
        state: Playwright ``storage_state`` dict (``BrowserContext.storage_state()``).
        include_optional: When ``True``, opt in to every label in
            :data:`notebooklm._auth.cookie_policy.OPTIONAL_COOKIE_DOMAINS_BY_LABEL`.
        include_domains: Optional-domain labels to opt in (``"all"`` = every
            label). Mirrors the rookiepy path semantics.

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
    io: LoginIO,
    *,
    page_html: str | None = None,
    quiet: bool = False,
) -> bool:
    """Populate ``notebooklm.account`` from Playwright storage when unambiguous.

    Used immediately after interactive Playwright login and by file-backed
    ``auth refresh`` as a repair path for older Playwright-created storage
    states. Ambiguous multi-account states are left unbound after clearing
    stale metadata. ``io`` carries the presentation / async-runner sink;
    ``quiet`` stays a service-level parameter (the Protocol has no silencing
    concept). Returns ``True`` when metadata was written, ``False`` when it
    was cleared or left absent.
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
            io.emit("[dim]Identifying Google account...[/dim]")
        jar = build_httpx_cookies_from_storage(storage_path)
        accounts = io.run_async(enumerate_accounts(jar))
        selected, reason = _select_playwright_account(accounts, active_email=active_email)
        if selected is None:
            clear_account_metadata(storage_path)
            if not quiet:
                io.emit(
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
            io.emit(
                "[yellow]Warning: account metadata was not written. "
                "NotebookLM auth still saved, but multi-account routing may "
                "fall back to authuser=0. "
                f"{ACCOUNT_METADATA_REMEDIATION} Details: {exc}[/yellow]"
            )
        return False

    if not quiet:
        io.emit(f"[green]Account:[/green] {selected.email}")
    return True


# ---------------------------------------------------------------------------
# Platform / browser pre-flight helpers
# ---------------------------------------------------------------------------


@contextmanager
def windows_playwright_event_loop() -> Iterator[None]:
    """Temporarily restore the default event loop policy for Playwright on Windows.

    Playwright's sync API spawns the browser via subprocess, which needs
    ``ProactorEventLoop`` on Windows. The CLI sets
    ``WindowsSelectorEventLoopPolicy`` globally (issue #79), incompatible with
    that path; this swaps the policy in for the Playwright section and restores
    it on exit. No-op on non-Windows platforms.
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


def ensure_chromium_installed(io: LoginIO) -> None:
    """Check if Chromium is installed and install if needed.

    Runs ``playwright install --dry-run chromium`` to detect a missing browser,
    then auto-installs. Silently proceeds on any error so Playwright handles it
    during launch. Both subprocess calls are timeout-bounded (30 s dry-run,
    300 s install) so a network-stalled CLI cannot hang ``notebooklm login``;
    ``TimeoutExpired`` is a pre-flight failure — the warning surfaces and login
    continues. ``io`` carries the presentation / exit sink (an install failure
    exits 1 via ``io.fail``; the ``except SystemExit: raise`` re-raise keeps
    that terminal path intact).
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
            # The dry-run probe succeeded but didn't see a "will download"
            # marker; nothing to do. If the probe printed an unexpected
            # diagnostic to stderr, surface a sanitised version at debug
            # level so operators can investigate without leaking env values.
            if result.stderr:
                logger.debug(
                    "playwright install --dry-run stderr: %s",
                    redact_subprocess_output(result.stderr),
                )
            return

        io.emit("[yellow]Chromium browser not installed. Installing now...[/yellow]")
        install_result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if install_result.returncode != 0:
            # Surface the (sanitised) tail of stderr/stdout so the user
            # has something to act on without us echoing raw env values
            # or ANSI progress bars from the playwright CLI.
            # ``redact_subprocess_output`` strips control codes and env
            # values before printing.
            #
            # Prefer stderr when it has substantive content; otherwise
            # fall back to stdout. Compare on the STRIPPED value so a
            # stderr that sanitises down to whitespace doesn't shadow a
            # stdout line carrying the actionable failure.
            sanitised_stderr = redact_subprocess_output(install_result.stderr or "").strip()
            sanitised_stdout = redact_subprocess_output(install_result.stdout or "").strip()
            diagnostic_tail = sanitised_stderr or sanitised_stdout
            io.emit(
                "[red]Failed to install Chromium browser.[/red]\n"
                f'Run manually: "{sys.executable}" -m playwright install chromium'
            )
            if diagnostic_tail:
                # markup=False: the captured CLI output is not Rich markup
                # and may contain stray ``[``/``]`` characters.
                io.emit(
                    f"[dim]Subprocess output (sanitised):[/dim]\n{diagnostic_tail}",
                    markup=False,
                )
            io.fail(1)
        io.emit("[green]Chromium installed successfully.[/green]\n")
    except SystemExit:
        raise
    except subprocess.TimeoutExpired as exc:
        # Network stall during download or a hung subprocess; surface the
        # diagnostic and let Playwright handle the real launch error.
        io.emit(
            f"[dim]Warning: Chromium pre-flight check timed out after "
            f"{exc.timeout}s. Proceeding anyway.[/dim]"
        )
    except Exception as e:
        # FileNotFoundError: playwright CLI not found but sync_playwright imported
        # Other exceptions: dry-run check failed — let Playwright handle it during launch.
        io.emit(f"[dim]Warning: Chromium pre-flight check failed: {e}. Proceeding anyway.[/dim]")


def recover_page(context: BrowserContext, io: LoginIO) -> Page:
    """Get a fresh page from a persistent browser context.

    Used when the current page reference is stale (TargetClosedError); a new
    page in a persistent context inherits all cookies and storage. Returns a
    new ``Page``, or raises ``SystemExit`` (via ``io.fail``) if the
    context/browser is dead; re-raises the original ``PlaywrightError`` for
    non-TargetClosed failures. ``io`` supplies both emit + fail.
    """
    from playwright.sync_api import Error as PlaywrightError

    try:
        return context.new_page()
    except PlaywrightError as exc:
        error_str = str(exc)
        if TARGET_CLOSED_ERROR in error_str:
            logger.error("Browser context is dead, cannot recover page: %s", error_str)
            io.emit(BROWSER_CLOSED_HELP)
            io.fail(1)
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


@dataclass(frozen=True)
class Conflict:
    """A ``login`` flag conflict; ``message`` is the styled line the wrapper emits before exit 1."""

    message: str


@dataclass(frozen=True)
class PreparedPaths:
    """Resolved storage + browser-profile paths; ``fresh_cleared`` flags a ``--fresh`` wipe."""

    storage_path: Path
    browser_profile: Path
    fresh_cleared: bool


@dataclass(frozen=True)
class PathError:
    """A ``--fresh`` wipe failure; ``message`` is the styled block the wrapper emits before exit 1."""

    message: str


def validate_login_flag_conflicts(
    *,
    browser_cookies: str | None,
    account_email: str | None,
    all_accounts: bool,
    update: bool,
    profile_name: str | None,
    storage: str | None,
) -> Conflict | None:
    """Enforce ``login`` flag mutual-exclusion rules.

    Returns the first :class:`Conflict` (carrying the styled error message the
    command layer emits before exiting 1), or ``None`` when valid. The
    env-supplied-auth check stays in the ``login`` orchestrator — it is an
    environment vs file-auth conflict, distinct from flag mutual-exclusion.
    """
    if browser_cookies is None and (
        account_email is not None or all_accounts or profile_name is not None
    ):
        return Conflict(
            "[red]Error: --account, --all-accounts, and --profile-name "
            "require --browser-cookies.[/red]"
        )
    if all_accounts and (account_email is not None or profile_name is not None):
        return Conflict(
            "[red]Error: --all-accounts cannot be combined with --account or --profile-name.[/red]"
        )
    if all_accounts and storage:
        return Conflict(
            "[red]Error: --all-accounts writes one profile per account "
            "and cannot be combined with --storage.[/red]"
        )
    if update and not all_accounts:
        return Conflict("[red]Error: --update only applies to --all-accounts.[/red]")
    return None


def prepare_login_paths(
    profile: str | None, storage: str | None, fresh: bool
) -> PreparedPaths | PathError:
    """Resolve storage and browser-profile paths for the Playwright login flow.

    Clears the cached browser profile on ``--fresh`` (returning
    :class:`PathError` on OSError so the command layer exits 1), then creates
    both parent dirs with platform-aware permissions. Returns
    :class:`PreparedPaths` on success (``fresh_cleared`` flags whether the
    wipe ran, so the wrapper emits the cleared-session line).
    """
    if storage:
        storage_path = Path(storage)
    elif profile:
        storage_path = get_storage_path(profile=profile)
    else:
        storage_path = get_storage_path()
    browser_profile = get_browser_profile_dir()

    fresh_cleared = False
    if fresh and browser_profile.exists():
        try:
            shutil.rmtree(browser_profile)
            fresh_cleared = True
        except OSError as exc:
            logger.error("Failed to clear browser profile %s: %s", browser_profile, exc)
            return PathError(
                f"[red]Cannot clear browser profile: {exc}[/red]\n"
                "Close any open browser windows and try again.\n"
                f"If the problem persists, manually delete: {browser_profile}"
            )

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

    return PreparedPaths(
        storage_path=storage_path,
        browser_profile=browser_profile,
        fresh_cleared=fresh_cleared,
    )


# ---------------------------------------------------------------------------
# Playwright entry point
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlaywrightLoginPlan:
    """Frozen description of one Playwright login attempt.

    Fields:
        browser: Channel; ``"chromium"`` or any :data:`CHANNEL_BROWSERS` key
            (``"chrome"``, ``"msedge"``).
        browser_profile: Persistent-context dir Playwright launches against
            (survives across attempts so the session persists).
        storage_path: Destination for the captured ``storage_state.json``.
        include_domains: Optional ``--include-domains`` labels; ``None`` /
            empty means "only required Google cookies + regional ccTLDs."
    """

    browser: str
    browser_profile: Path
    storage_path: Path
    include_domains: set[str] | None = None


def run_playwright_login(plan: PlaywrightLoginPlan, io: LoginIO) -> None:
    """Drive the Playwright-based Google login and persist storage state.

    Imports Playwright lazily (``io.fail(1)`` + install hint on ImportError),
    runs the chromium pre-flight for the bundled browser, opens a persistent
    context, retries navigation on transient connection errors, waits for
    login, pins ``.google.com`` cookies, applies the cookie-domain allowlist,
    atomically writes ``storage_state.json``, and writes account metadata when
    the active account can be identified safely. ``io`` carries every
    presentation / exit / async-runner side effect.
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
        # markup=False below so Rich keeps the literal `[browser]` pip extra.
        if browser in CHANNEL_BROWSERS:
            install_hint = '  pip install "notebooklm-py[browser]"'
        else:
            install_hint = '  pip install "notebooklm-py[browser]"\n  playwright install chromium'
        io.emit("[red]Playwright not installed. Run:[/red]")
        io.emit(install_hint, markup=False)
        io.fail(1)

    # Pre-flight check: verify Chromium browser is installed (system Chrome
    # and Edge are checked at launch time by Playwright's channel routing).
    if browser == "chromium":
        ensure_chromium_installed(io)

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
    io.emit(f"[dim]Profile: {profile_name}[/dim]")
    io.emit(f"[yellow]Opening {browser_label} for Google login...[/yellow]")
    io.emit(f"[dim]Using persistent profile: {browser_profile}[/dim]")

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

            page = context.pages[0] if context.pages else recover_page(context, io)

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
                            page = recover_page(context, io)

                        backoff_seconds = attempt  # Linear backoff: 1s, 2s
                        logger.debug(
                            "Retryable error on attempt %d/%d: %s",
                            attempt,
                            LOGIN_MAX_RETRIES,
                            error_str,
                        )
                        if is_target_closed:
                            io.emit(
                                f"[yellow]Browser page closed "
                                f"(attempt {attempt}/{LOGIN_MAX_RETRIES}). "
                                f"Retrying with fresh page...[/yellow]"
                            )
                        else:
                            io.emit(
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
                        io.emit(BROWSER_CLOSED_HELP)
                        io.fail(1)
                    elif is_retryable:
                        logger.error(
                            f"Failed to connect to NotebookLM after {LOGIN_MAX_RETRIES} attempts. "
                            f"Last error: {error_str}"
                        )
                        io.emit(connection_error_help())
                        io.fail(1)
                    else:
                        logger.debug("Non-retryable error: %s", error_str)
                        raise

            if url_matches_base_host(page.url):
                # Persistent browser profile already has a valid session.
                io.emit("[green]Already logged in.[/green]")
            else:
                io.emit("\n[bold green]Instructions:[/bold green]")
                io.emit("1. Complete the Google login in the browser window")
                io.emit("2. Authentication will be saved automatically once login is detected\n")
                io.emit("[dim]Waiting for login (up to 5 minutes)...[/dim]")
                try:
                    page.wait_for_url(f"{get_base_url()}/**", timeout=300_000)
                except PlaywrightTimeout:
                    io.emit(
                        "[red]Login not detected within 5 minutes.[/red]\n"
                        "Try again with: notebooklm login"
                    )
                    io.fail(1)
                except PlaywrightError as exc:
                    # Browser/tab closed during the wait. Cannot resume a
                    # partially completed SSO form, so surface the same
                    # help text other browser-closed paths use.
                    if TARGET_CLOSED_ERROR in str(exc):
                        io.emit(BROWSER_CLOSED_HELP)
                        io.fail(1)
                    raise
                io.emit("[green]Login detected.[/green]")

            active_page_html = _capture_page_html(page)

            # Force .google.com cookies for regional users (e.g. UK lands on
            # .google.co.uk). "commit" resolves once response headers (incl.
            # Set-Cookie) are processed, before a client-side redirect can
            # interrupt. See #214.
            recovered_during_cookie_forcing = False
            for url in [GOOGLE_ACCOUNTS_URL, f"{get_base_url()}/"]:
                try:
                    page.goto(url, wait_until="commit")
                except PlaywrightError as exc:
                    error_str = str(exc)
                    if TARGET_CLOSED_ERROR in error_str:
                        # Page was destroyed (e.g. user switched accounts) -- get fresh page
                        page = recover_page(context, io)
                        recovered_during_cookie_forcing = True
                        try:
                            page.goto(url, wait_until="commit")
                        except PlaywrightError as inner_exc:
                            if TARGET_CLOSED_ERROR in str(inner_exc):
                                io.emit(BROWSER_CLOSED_HELP)
                                io.fail(1)
                            elif not is_navigation_interrupted_error(inner_exc):
                                raise
                    elif not is_navigation_interrupted_error(error_str):
                        raise

            # Defense-in-depth: wait_for_url proved we reached the host, but the
            # cookie-forcing round-trip above can land us back on
            # accounts.google.com if the session was invalidated mid-flow (rare).
            # Auto-detect is non-interactive, so fail fast with a clear next step.
            if not url_matches_base_host(page.url):
                io.emit(
                    f"[red]Unexpected URL after login: {page.url}[/red]\n"
                    "Authentication may be incomplete. "
                    "Try: notebooklm login --fresh"
                )
                io.fail(1)

            if recovered_during_cookie_forcing:
                active_page_html = _capture_page_html(page)

            # Atomic write with chmod 0o600 — Playwright's path= writes directly
            # (non-atomic + world-readable window). Apply the same cookie-domain
            # allowlist the rookiepy path uses so sibling-product cookies (mail,
            # myaccount, docs, youtube) the user is signed into in the same
            # browser session don't leak into ``storage_state.json`` (opt-in via
            # ``--include-domains=...``).
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
                    io.emit(
                        f"[red]{label} not found.[/red]\n"
                        f"Install from: {install_url}\n"
                        "Or use the default Chromium browser: notebooklm login"
                    )
                    io.fail(1)
            # Diagnostic stays at debug level; the bare ``raise`` propagates to
            # ``handle_errors`` → friendly ``Unexpected error: <msg>`` + exit 2.
            logger.debug("Login failed: %s", e, exc_info=True)
            raise
        finally:
            if context:
                context.close()

    if should_repair_account_metadata:
        repair_playwright_account_metadata(storage_path, io, page_html=account_metadata_page_html)


__all__ = [
    "BROWSER_CLOSED_HELP",
    "CHANNEL_BROWSERS",
    "GOOGLE_ACCOUNTS_URL",
    "LOGIN_MAX_RETRIES",
    "RETRYABLE_CONNECTION_ERRORS",
    "TARGET_CLOSED_ERROR",
    "Conflict",
    "LoginIO",
    "PathError",
    "PlaywrightLoginPlan",
    "PreparedPaths",
    "connection_error_help",
    "ensure_chromium_installed",
    "filter_storage_state_cookies_by_domain_policy",
    "is_navigation_interrupted_error",
    "prepare_login_paths",
    "recover_page",
    "redact_subprocess_output",
    "repair_playwright_account_metadata",
    "run_playwright_login",
    "url_matches_base_host",
    "validate_login_flag_conflicts",
    "windows_playwright_event_loop",
]
