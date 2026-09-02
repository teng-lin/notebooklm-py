"""Playwright-driven Google login service (ADR-0008 click-to-service extraction).

Owns the interactive ``notebooklm login`` Playwright fast path (the rookie-cookies
``--browser-cookies`` path stays in :mod:`notebooklm.cli.services.login`). The
Click handler stays a thin orchestrator over this service.

This module is the **CLI adapter** over the transport-neutral browser-capture
core in :mod:`notebooklm._auth.browser_capture` (ADR-0021): the neutral
launch -> navigate -> capture -> filter -> persist sequence lives in the
``_auth`` core, reachable by the client runtime and by the headless re-auth
layer; the interactive / presentation concerns — the chromium install
pre-flight, flag-conflict validation, path preparation, account-metadata
repair, and the human-readable error hints — stay here. The neutral helpers and
constants the core owns (``run_browser_capture``,
``filter_storage_state_cookies_by_domain_policy``, ``recover_page``,
``windows_playwright_event_loop``, the retry constants, ``BROWSER_CLOSED_HELP``,
…) are re-exported below so existing import paths and test patch seams keep
resolving.

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

import inspect
import logging
import shutil
import subprocess
import sys
from collections.abc import Awaitable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn, Protocol

import httpx

from ..._app.profile import (
    ProfileRepairRequest,
    repair_playwright_account,
)
from ..._auth.browser_capture import (
    BROWSER_CLOSED_HELP,
    CHANNEL_BROWSERS,
    GOOGLE_ACCOUNTS_URL,
    LOGIN_MAX_RETRIES,
    RETRYABLE_CONNECTION_ERRORS,
    TARGET_CLOSED_ERROR,
    BrowserCapturePlan,
    CaptureResult,
    connection_error_help,
    ensure_playwright_available,
    filter_storage_state_cookies_by_domain_policy,
    is_navigation_interrupted_error,
    recover_page,
    run_browser_capture,
    url_matches_base_host,
    windows_playwright_event_loop,
)
from ...paths import (
    BROWSER_PROFILE_OWNERSHIP_MARKER,
    browser_profile_is_owned,
    get_browser_profile_dir,
    get_storage_path,
)
from .playwright_redaction import redact_subprocess_output

logger = logging.getLogger(__name__)


class LoginIO(Protocol):
    """Caller-injected sink for the Playwright login flow's side effects.

    The command layer (:mod:`notebooklm.cli.playwright_login_io`) injects a
    concrete impl so this service never imports the presentation
    (``..rendering``), exit-policy (``..error_handler``), or async-runner
    (``..runtime``) command modules directly (ADR-0008 boundary). ``emit``
    forwards to ``console.print`` (``*args, **kwargs`` pass through verbatim,
    incl. ``markup=False``); ``fail`` forwards to ``exit_with_code`` (raises
    ``SystemExit``); ``run_async`` forwards to ``run_async``. Shape-compatible
    with the neutral core's
    :class:`notebooklm._auth.browser_capture.BrowserCaptureIO`, so the same
    concrete sink drives both layers.
    """

    def emit(self, *args: Any, **kwargs: Any) -> None: ...

    def fail(self, code: int) -> NoReturn: ...

    def run_async(self, coro: Awaitable[Any]) -> Any: ...


ACCOUNT_METADATA_REMEDIATION = (
    "Run [cyan]notebooklm auth inspect --browser chrome -v[/cyan] "
    "or [cyan]notebooklm login --browser-cookies chrome --account EMAIL[/cyan]."
)


# ---------------------------------------------------------------------------
# Playwright account metadata repair (interactive-adjacent; stays in adapter)
# ---------------------------------------------------------------------------


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

    The coarse app operation owns the public repair invocation and neutral
    result classification; this wrapper owns only ``LoginIO`` presentation.
    ``io.run_async`` itself is still wrapped here (not just the coroutine's own
    handling): a scheduling ``RuntimeError`` must degrade to the historical
    best-effort warning rather than aborting login/refresh.
    """
    request: ProfileRepairRequest | None = None
    operation = None
    result = None
    try:
        if not quiet:
            io.emit("[dim]Identifying Google account...[/dim]")
        request = ProfileRepairRequest(storage_path=storage_path, page_html=page_html)
        operation = repair_playwright_account(request)
        try:
            try:
                result = io.run_async(operation)
            except BaseException:
                if inspect.getcoroutinestate(operation) == inspect.CORO_CREATED:
                    operation.close()
                raise
        except (OSError, ValueError, RuntimeError, httpx.HTTPError) as exc:
            if not quiet:
                io.emit(
                    "[yellow]Warning: account metadata was not written. "
                    "NotebookLM auth still saved, but multi-account routing may "
                    "fall back to authuser=0. "
                    f"{ACCOUNT_METADATA_REMEDIATION} Details: {exc}[/yellow]"
                )
            return False
        if result.status == "WRITTEN":
            if not quiet:
                io.emit(f"[green]Account:[/green] {result.email}")
            return True
        if not quiet:
            if result.status == "ERROR":
                io.emit(
                    "[yellow]Warning: account metadata was not written. "
                    "NotebookLM auth still saved, but multi-account routing may "
                    "fall back to authuser=0. "
                    f"{ACCOUNT_METADATA_REMEDIATION} Details: {result.detail}[/yellow]"
                )
            else:
                io.emit(
                    "[yellow]Warning: account metadata was not written; "
                    f"{result.detail}. {ACCOUNT_METADATA_REMEDIATION}[/yellow]"
                )
        return False
    finally:
        del storage_path, io, page_html, request, operation, result


# ---------------------------------------------------------------------------
# Chromium install pre-flight (CLI-install concern; stays in the adapter)
# ---------------------------------------------------------------------------

CHROMIUM_PRESENT_MARKER = "notebooklm-chromium-present"
CHROMIUM_MISSING_MARKER = "notebooklm-chromium-missing"

# Detection source for the pre-flight, run in an isolated interpreter.
#
# It asks Playwright itself where the bundled Chromium build lives
# (``BrowserType.executable_path``, a stable public API) and tests that path
# on disk, printing one of the markers above.
#
# It deliberately does NOT parse ``playwright install --dry-run`` output
# (#2031): that command prints the same ``browser:`` / ``Install location:`` /
# ``Download url:`` block whether or not the browser is on disk — it reports
# where a build *would* go, not whether it is there — so it cannot answer this
# question in either direction. Its human-readable text is also not a stable
# interface (the historical ``"will download"`` marker this pre-flight matched
# on is emitted by no shipping Playwright release).
#
# Running it out-of-process keeps the check timeout-bounded (an in-process
# ``sync_playwright()`` start cannot be interrupted), starts the driver with a
# clean default event-loop policy on Windows, and leaves no Playwright state in
# the CLI process before the real launch.
CHROMIUM_PROBE_SOURCE = f"""\
import os
import sys

from playwright.sync_api import sync_playwright

with sync_playwright() as playwright:
    path = playwright.chromium.executable_path
sys.stdout.write(
    "{CHROMIUM_PRESENT_MARKER}" if path and os.path.exists(path) else "{CHROMIUM_MISSING_MARKER}"
)
"""


def ensure_chromium_installed(io: LoginIO) -> None:
    """Check if Chromium is installed and install if needed.

    Resolves Playwright's bundled-Chromium executable path in an isolated
    interpreter (:data:`CHROMIUM_PROBE_SOURCE`) and auto-installs when that
    path is absent. Silently proceeds on any error — including an unreadable
    probe answer — so Playwright handles it during launch. Both subprocess
    calls are timeout-bounded (30 s probe, 300 s install) so a network-stalled
    CLI cannot hang ``notebooklm login``; ``TimeoutExpired`` is a pre-flight
    failure — the warning surfaces and login continues. ``io`` carries the
    presentation / exit sink (an install failure exits 1 via ``io.fail``; the
    ``except SystemExit: raise`` re-raise keeps that terminal path intact).
    """
    try:
        result = subprocess.run(
            [sys.executable, "-c", CHROMIUM_PROBE_SOURCE],
            capture_output=True,
            text=True,
            timeout=30,
        )
        saw_present = CHROMIUM_PRESENT_MARKER in result.stdout
        saw_missing = CHROMIUM_MISSING_MARKER in result.stdout
        # Install only on an unambiguous "missing" answer from a probe that
        # ran to completion. Anything else — the browser is on disk, the probe
        # exited non-zero, or it returned an answer we cannot read (printed
        # neither marker, or printed both) — means "nothing to do": installing
        # on a guess would re-download on every login, and a genuinely missing
        # browser still raises an actionable Playwright error at launch.
        chromium_missing = result.returncode == 0 and saw_missing and not saw_present
        if not chromium_missing:
            # If the probe printed an unexpected diagnostic to stderr, surface
            # a sanitised version at debug level so operators can investigate
            # without leaking env values.
            if result.stderr:
                logger.debug(
                    "chromium pre-flight probe stderr: %s",
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
        # FileNotFoundError: the interpreter that runs the probe is gone.
        # Other exceptions: the probe could not run — let Playwright handle it
        # during launch.
        io.emit(f"[dim]Warning: Chromium pre-flight check failed: {e}. Proceeding anyway.[/dim]")


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
    both parent dirs with platform-aware permissions. Explicit-storage browser
    dirs created here receive an ownership marker; ``--fresh`` refuses to
    delete an existing unowned sidecar. Returns
    :class:`PreparedPaths` on success (``fresh_cleared`` flags whether the
    wipe ran, so the wrapper emits the cleared-session line).
    """
    if storage:
        storage_path = Path(storage)
        browser_profile = get_browser_profile_dir(storage_path=storage_path)
    elif profile:
        storage_path = get_storage_path(profile=profile)
        browser_profile = get_browser_profile_dir(profile=profile)
    else:
        storage_path = get_storage_path()
        browser_profile = get_browser_profile_dir()

    fresh_cleared = False
    if fresh and browser_profile.exists():
        if storage and not browser_profile_is_owned(storage_path, browser_profile):
            return PathError(
                "[red]Refusing to delete an unowned browser profile.[/red]\n"
                "The directory is not recognized as NotebookLM-managed:\n"
                f"{browser_profile}\n"
                "Move it aside, or remove it manually only if you know it is safe."
            )
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

    browser_profile_existed = browser_profile.exists()
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

    if storage and not browser_profile_existed:
        (browser_profile / BROWSER_PROFILE_OWNERSHIP_MARKER).touch()

    return PreparedPaths(
        storage_path=storage_path,
        browser_profile=browser_profile,
        fresh_cleared=fresh_cleared,
    )


# ---------------------------------------------------------------------------
# Playwright entry point (interactive CLI adapter over the neutral core)
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
        login_timeout_s: Human-interaction window before headed login fails.
    """

    browser: str
    browser_profile: Path
    storage_path: Path
    include_domains: set[str] | None = None
    login_timeout_s: int = 300


def run_playwright_login(plan: PlaywrightLoginPlan, io: LoginIO) -> None:
    """Drive the interactive Playwright Google login and persist storage state.

    The CLI adapter over
    :func:`notebooklm._auth.browser_capture.run_browser_capture`: it runs the
    chromium install pre-flight for the bundled browser, prints the launch
    banner, delegates the launch -> navigate -> capture -> filter ->
    atomic-persist sequence to the neutral core (``interactive=True,
    headless=False``), then writes account metadata when the active account can
    be identified safely. ``io`` carries every presentation / exit / async-runner
    side effect and satisfies the core's ``BrowserCaptureIO`` Protocol.
    """
    browser = plan.browser

    # Fail fast with the install hint (and no banner) when the ``browser`` extra
    # is absent — preserves the historical "import-check before any banner"
    # ordering now that the lazy Playwright import lives in the neutral core.
    ensure_playwright_available(io, browser=browser)

    # Pre-flight check: verify Chromium browser is installed (system Chrome
    # and Edge are checked at launch time by Playwright's channel routing).
    if browser == "chromium":
        ensure_chromium_installed(io)

    from ...paths import resolve_profile

    profile_name = resolve_profile()
    channel_info = CHANNEL_BROWSERS.get(browser)
    browser_label = channel_info[0] if channel_info else "Chromium"
    io.emit(f"[dim]Profile: {profile_name}[/dim]")
    io.emit(f"[yellow]Opening {browser_label} for Google login...[/yellow]")
    io.emit(f"[dim]Using persistent profile: {plan.browser_profile}[/dim]")

    result: CaptureResult = run_browser_capture(
        BrowserCapturePlan(
            browser=plan.browser,
            browser_profile=plan.browser_profile,
            storage_path=plan.storage_path,
            include_domains=plan.include_domains,
            login_timeout_s=plan.login_timeout_s,
        ),
        io,
        headless=False,
        interactive=True,
    )

    repair_playwright_account_metadata(plan.storage_path, io, page_html=result.page_html)


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
