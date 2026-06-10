"""Transport-neutral browser launch -> capture -> filter -> persist core.

This module owns the *neutral* heart of the Playwright login flow: it launches a
persistent-context browser against a profile dir, navigates to the configured
NotebookLM base URL, waits for the session to land on that host, forces the
``.google.com`` cookies for regional users, captures
``BrowserContext.storage_state()``, applies the cookie-domain allowlist, and
atomically persists ``storage_state.json``. It carries no Click / Rich / CLI
coupling — presentation, interactive prompting, account-selection, exit-code
policy, and human-readable error hints stay in the CLI adapter
(:mod:`notebooklm.cli.services.playwright_login`) per ADR-0021. The few moments
the core must surface a line or abort are inverted behind the
:class:`BrowserCaptureIO` Protocol (the same shape as the CLI's ``LoginIO``),
so a headless caller can inject a silent / raising sink.

**Shared core for two callers.** This is the single launch/capture/persist
primitive used by BOTH:

1. the existing **interactive** ``notebooklm login`` Playwright flow
   (``interactive=True, headless=False`` — the only mode wired today); and
2. a future **headless re-auth** layer (layer-3 auth recovery): when NotebookLM
   cookies are fully dead but a persistent browser profile still holds a live
   Google session, drive a *headless* browser to silently re-mint cookies.

**Locked design decision for the future headless feature (inherited by P2).**
Headless re-auth is EXPLICIT by default via
``client.refresh_auth(allow_headless=True)``; a mid-RPC auto-fire happens only
when ``NOTEBOOKLM_HEADLESS_REAUTH=1`` is set in the environment. The
``headless`` / ``interactive`` parameters and their branch points exist here so
P2 can wire the headless arm without re-carving this core, but P1 ships
refactor-only: the headless arm is an explicit guard
(:func:`_reject_unsupported_mode`) and nothing changes for current callers.

``playwright`` is imported lazily (function-local) so importing this module
without the ``browser`` extra never fails — mirroring the deferral the CLI flow
has always used.
"""

from __future__ import annotations

import asyncio
import logging
import reprlib
import sys
import time
from collections.abc import Awaitable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn, Protocol
from urllib.parse import urlparse

from .._atomic_io import atomic_write_json
from ..config import get_base_host, get_base_url
from ..exceptions import HeadlessLoginRequiredError
from .cookie_policy import build_cookie_domain_allowlist

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext, Page

logger = logging.getLogger(__name__)


class BrowserCaptureIO(Protocol):
    """Caller-injected sink for the neutral browser-capture core's side effects.

    Identical in shape to the CLI's ``LoginIO`` so the interactive adapter can
    pass its concrete sink straight through. ``emit`` forwards a presentation
    line (``*args, **kwargs`` pass through verbatim, incl. ``markup=False``);
    ``fail`` aborts the flow (the CLI maps it to ``SystemExit`` via
    ``exit_with_code``); ``run_async`` drives an awaitable to completion.

    Note: :func:`run_browser_capture` itself never calls ``run_async`` — only
    the adapter's post-capture ``repair_playwright_account_metadata`` does.
    ``run_async`` stays on this Protocol purely to keep it shape-compatible with
    ``LoginIO`` so one concrete sink satisfies both layers; a future
    ``BrowserCaptureIO`` impl that never reaches account-metadata repair may
    supply a trivial ``run_async``.
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

# Browsers launched via Playwright's ``channel`` parameter (system-installed,
# not the bundled Chromium). Maps channel name -> (display label, install URL).
# Used for the --browser option, the launch banner, and the not-installed
# error path. The bundled "chromium" choice is intentionally absent.
CHANNEL_BROWSERS: dict[str, tuple[str, str]] = {
    "msedge": ("Microsoft Edge", "https://www.microsoft.com/edge"),
    "chrome": ("Google Chrome", "https://www.google.com/chrome"),
}


# ---------------------------------------------------------------------------
# Cookie-domain filter (neutral; both login paths consume it at write time)
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

    Two hardening behaviors (#1513) ride on top of the allowlist:

    * **Malformed rows are skipped, not raised.** rookiepy / Playwright can
      emit malformed rows; a non-dict entry, a cookie whose ``domain`` is not
      a str, or a cookie whose ``name`` is not a non-empty str (all malformed
      under Playwright's own ``storage_state`` schema) is dropped with one
      bounded ``logger.warning`` per row (``reprlib`` preview) instead of
      crashing the whole persist.
    * **Exact-identity duplicate dedup.** Rows are keyed by their full
      RFC 6265 identity ``(name, domain, path)`` (path normalized via
      ``or "/"``, matching every loader). For exact-identity duplicates —
      where only metadata such as ``value`` / ``expires`` / flags can differ —
      the **last occurrence in input order wins** and replaces the earlier row
      in place, kept whole (fields are never merged). This mirrors the
      persistence-merge rule in
      :func:`notebooklm._auth.storage.save_cookies_to_storage`, where the
      newer observation overwrites the stored row for the same
      ``(name, domain, path)`` key.

      Same-name rows on *different* domains or paths are deliberately ALL
      kept: cross-domain same-name resolution is a **load-time** concern (the
      flat loaders :func:`notebooklm._auth.cookies.extract_cookies_from_storage`
      / :func:`notebooklm._auth.cookies.flatten_cookie_map` rank by
      ``_auth_domain_priority``). Deduping by bare name at write time would
      starve the ``(name, domain, path)``-keyed runtime loader
      (:func:`notebooklm._auth.cookies.build_httpx_cookies_from_storage`),
      which legitimately holds e.g. the per-product ``OSID`` cookie on
      ``notebooklm.google.com`` and ``myaccount.google.com`` as distinct jar
      entries.

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
    allowed_list = build_cookie_domain_allowlist(
        include_optional=include_optional, include_domains=include_domains
    )
    allowed: frozenset[str] = frozenset(allowed_list)
    allowed_stripped: frozenset[str] = frozenset(d.lstrip(".") for d in allowed_list)

    def _is_allowed(domain: str) -> bool:
        return domain in allowed or domain.lstrip(".") in allowed_stripped

    filtered_cookies: list[dict[str, Any]] = []
    index_by_identity: dict[tuple[str, str, Any], int] = {}

    for cookie in state.get("cookies", []):
        if not isinstance(cookie, dict):
            # reprlib bounds the preview without materialising the full repr.
            logger.warning(
                "Skipping malformed storage_state cookie entry (not a dict): %s",
                reprlib.repr(cookie),
            )
            continue
        domain = cookie.get("domain", "")
        if not isinstance(domain, str):
            logger.warning(
                "Skipping storage_state cookie with non-str domain: %s",
                reprlib.repr(cookie),
            )
            continue
        name = cookie.get("name")
        if not isinstance(name, str) or not name:
            logger.warning(
                "Skipping storage_state cookie with missing/empty/non-str name: %s",
                reprlib.repr(cookie),
            )
            continue
        # ``path`` participates in the dedup identity below and is normalized
        # with ``or "/"``; a present-but-non-str path (int, list) would slip
        # past that and later crash http.cookiejar/httpx path matching, so
        # treat it as malformed. ``None``/absent is fine — it normalizes to
        # the root path, matching the loaders.
        path = cookie.get("path")
        if path is not None and not isinstance(path, str):
            logger.warning(
                "Skipping storage_state cookie with non-str path: %s",
                reprlib.repr(cookie),
            )
            continue
        if not _is_allowed(domain):
            continue

        # Full RFC 6265 identity. ``or "/"`` mirrors the path normalization
        # the loaders and the save_cookies_to_storage merge key use, so an
        # empty-path twin can't survive as a phantom duplicate row.
        identity = (name, domain, path or "/")
        existing = index_by_identity.get(identity)
        if existing is None:
            index_by_identity[identity] = len(filtered_cookies)
            filtered_cookies.append(cookie)
        else:
            # Exact-identity duplicate: the later observation wins whole,
            # replacing the earlier row in place — mirroring the
            # save_cookies_to_storage merge, where the newer observation
            # overwrites the stored row for the same (name, domain, path) key.
            logger.debug(
                "Cookie %s: exact-identity duplicate on (%s, %s); keeping later observation",
                name,
                domain,
                identity[2],
            )
            filtered_cookies[existing] = cookie

    return {
        "cookies": filtered_cookies,
        "origins": list(state.get("origins", [])),
    }


# ---------------------------------------------------------------------------
# Platform / page-recovery / URL helpers (neutral)
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


def recover_page(context: BrowserContext, io: BrowserCaptureIO) -> Page:
    """Get a fresh page from a persistent browser context.

    Used when the current page reference is stale (TargetClosedError); a new
    page in a persistent context inherits all cookies and storage. Returns a
    new ``Page``, or aborts (via ``io.fail``) if the context/browser is dead;
    re-raises the original ``PlaywrightError`` for non-TargetClosed failures.
    ``io`` supplies both emit + fail.
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
# Neutral capture core: launch -> navigate -> capture -> filter -> persist
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BrowserCapturePlan:
    """Frozen description of one browser-capture attempt.

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


@dataclass(frozen=True)
class CaptureResult:
    """Outcome of a successful capture.

    ``page_html`` is the HTML of the final NotebookLM page (or ``None`` if it
    could not be read), carried out so the interactive adapter can resolve the
    active account for metadata repair without re-touching the (now-closed)
    browser.
    """

    page_html: str | None


def ensure_playwright_available(io: BrowserCaptureIO, *, browser: str) -> None:
    """Abort with an install hint if the Playwright sync API cannot be imported.

    Surfaced as a standalone check (rather than only failing inside
    :func:`run_browser_capture`) so the CLI adapter can run it *before* its
    launch banner — preserving the historical ordering where a missing
    ``browser`` extra produces only the install hint, with no banner. The hint
    text branches on ``browser``: a system ``channel`` (chrome / msedge) only
    needs the ``[browser]`` extra, while the bundled chromium also needs
    ``playwright install chromium``. ``playwright`` is imported lazily here too.
    """
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        # markup=False below so Rich keeps the literal `[browser]` pip extra.
        if browser in CHANNEL_BROWSERS:
            install_hint = '  pip install "notebooklm-py[browser]"'
        else:
            install_hint = '  pip install "notebooklm-py[browser]"\n  playwright install chromium'
        io.emit("[red]Playwright not installed. Run:[/red]")
        io.emit(install_hint, markup=False)
        io.fail(1)


def _reject_unsupported_mode(*, headless: bool, interactive: bool, io: BrowserCaptureIO) -> None:
    """Guard the supported ``(headless, interactive)`` combinations.

    Two arms are wired:

    * ``interactive=True, headless=False`` — the interactive ``notebooklm
      login`` flow (a human completes the Google SSO in a visible browser).
    * ``interactive=False, headless=True`` — the layer-3 headless re-auth flow
      (P2): an unattended browser harvests a still-live Google session from the
      persistent profile, with NO human to wait on.

    Any other combination (interactive + headless, or non-interactive +
    non-headless) is a programmer error: a visible-but-unattended browser would
    hang waiting for a human who isn't there, and a headless-but-interactive
    flow is contradictory. Refuse loudly so a caller cannot silently get a
    half-wired flow.

    ``io`` is accepted but deliberately unused: this is a programmer-facing
    guard that raises ``NotImplementedError`` (not an end-user condition routed
    through ``io.fail``).
    """
    if interactive and not headless:
        return
    if headless and not interactive:
        return
    _ = io  # programmer-facing guard; not an ``io.fail`` end-user condition
    raise NotImplementedError(
        "Unsupported browser-capture mode "
        f"(headless={headless}, interactive={interactive}). "
        "Only interactive=True/headless=False (interactive login) and "
        "interactive=False/headless=True (headless re-auth) are supported."
    )


def run_browser_capture(
    plan: BrowserCapturePlan,
    io: BrowserCaptureIO,
    *,
    headless: bool = False,
    interactive: bool = True,
) -> CaptureResult:
    """Launch a browser, capture + filter + persist NotebookLM storage state.

    The neutral core shared by the interactive CLI login and the future headless
    re-auth layer. Imports Playwright lazily (``io.fail(1)`` + install hint on
    ImportError), opens a persistent context against ``plan.browser_profile``,
    retries navigation on transient connection errors, waits for login (in the
    interactive arm), pins ``.google.com`` cookies, applies the cookie-domain
    allowlist, and atomically writes ``storage_state.json``. Returns a
    :class:`CaptureResult` carrying the final page HTML so the caller can repair
    account metadata. ``io`` carries every presentation / exit / async-runner
    side effect; presentation/interactive niceties stay in the adapter.

    The chromium pre-flight (``playwright install``) is intentionally NOT run
    here — it is a CLI-install concern owned by the adapter, run before this
    core is entered.
    """
    _reject_unsupported_mode(headless=headless, interactive=interactive, io=io)

    browser = plan.browser
    browser_profile = plan.browser_profile
    storage_path = plan.storage_path
    include_domains = plan.include_domains

    # Fail fast with the install hint when the ``browser`` extra is absent. The
    # CLI adapter runs this earlier (before its banner); calling it again here
    # is cheap and keeps the contract intact for any direct ``_auth`` caller.
    ensure_playwright_available(io, browser=browser)
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import TimeoutError as PlaywrightTimeout
    from playwright.sync_api import sync_playwright

    def _capture_page_html(page: Any) -> str | None:
        try:
            content = page.content()
        except PlaywrightError as exc:
            logger.debug("Could not read Playwright page content for account metadata: %s", exc)
            return None
        return content if isinstance(content, str) else None

    captured_page_html: str | None = None

    # Use context manager to restore ProactorEventLoop for Playwright on Windows
    # (fixes #89: NotImplementedError on Windows Python 3.12)
    with windows_playwright_event_loop(), sync_playwright() as p:
        launch_kwargs: dict[str, Any] = {
            "user_data_dir": str(browser_profile),
            "headless": headless,
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

            if headless:
                # Layer-3 headless re-auth: there is NO human to complete a
                # login form, so we never wait. Classify the landing instead:
                #   * lands on the NotebookLM host  → the persistent profile
                #     still holds a live Google session; proceed to capture.
                #   * redirected to a login page    → the profile's Google
                #     session is ALSO dead; fail loudly (raise) rather than
                #     hang. ``HeadlessLoginRequiredError`` is the typed,
                #     honest signal the caller maps to a FAILED outcome.
                if not url_matches_base_host(page.url):
                    logger.warning(
                        "Headless re-auth: landed off-host after navigation "
                        "(the persisted browser profile's Google session is "
                        "likely expired); cannot silently re-mint cookies."
                    )
                    raise HeadlessLoginRequiredError(
                        "Headless re-auth could not reach NotebookLM: the "
                        "persisted browser profile's Google session is "
                        "expired. Run 'notebooklm login' to re-authenticate."
                    )
            elif url_matches_base_host(page.url):
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
            captured_page_html = active_page_html

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
            # Last-resort TargetClosed mapping for anything that escapes the
            # in-flow guards (recover_page, the navigation retry loop,
            # wait_for_url, cookie-forcing) — in practice the final
            # ``context.storage_state()`` capture (#1514). Those paths already
            # map TargetClosed to BROWSER_CLOSED_HELP + exit 1; mirror them
            # here so the user gets the same friendly help instead of the
            # exit-2 bug-report hint. (The launch branch above never falls
            # through for handled not-found errors — it io.fail(1)s — and
            # launch failures are not TargetClosed.)
            if isinstance(e, PlaywrightError) and TARGET_CLOSED_ERROR in str(e):
                io.emit(BROWSER_CLOSED_HELP)
                io.fail(1)
            # For everything else, the diagnostic stays at debug level; the bare
            # ``raise`` propagates to ``handle_errors`` → friendly
            # ``Unexpected error: <msg>`` + exit 2.
            logger.debug("Login failed: %s", e, exc_info=True)
            raise
        finally:
            if context:
                context.close()

    return CaptureResult(page_html=captured_page_html)


__all__ = [
    "BROWSER_CLOSED_HELP",
    "CHANNEL_BROWSERS",
    "GOOGLE_ACCOUNTS_URL",
    "LOGIN_MAX_RETRIES",
    "RETRYABLE_CONNECTION_ERRORS",
    "TARGET_CLOSED_ERROR",
    "BrowserCaptureIO",
    "BrowserCapturePlan",
    "CaptureResult",
    "connection_error_help",
    "ensure_playwright_available",
    "filter_storage_state_cookies_by_domain_policy",
    "is_navigation_interrupted_error",
    "recover_page",
    "run_browser_capture",
    "url_matches_base_host",
    "windows_playwright_event_loop",
]
