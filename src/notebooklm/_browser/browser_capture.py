"""Transport-neutral browser launch, capture, heal, and persistence core.

The shared interactive and headless arms launch a persistent Playwright
context, navigate to the configured app host, capture storage state, apply the
cookie-domain policy, heal PSIDTS when possible, and persist through the native
``ProfileStore`` replacement. The alternative CDP arm uses the same landing,
filter, heal, and persistence rules against an operator-provided browser.

Presentation and exit policy stay behind :class:`BrowserCaptureIO`; Playwright
is imported lazily so the module remains importable without the browser extra.
Only ``interactive=True, headless=False`` and ``interactive=False,
headless=True`` are supported. Automatic headless recovery remains opt-in via
``NOTEBOOKLM_HEADLESS_REAUTH=1``.

ADR-0033 folded the login-wait trace and captured-state heal bridge into this
module. ``browser_launch_errors.py`` remains a cohesive pure classifier leaf and
is re-exported here for existing private import continuity.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from collections.abc import Awaitable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn, Protocol
from urllib.parse import urlencode, urlparse

# Captured-state sanitization and shared PSIDTS recovery (ADR-0033 PR 4.1).
from .._auth import cookies as _auth_cookies
from .._auth import psidts_recovery as _psidts_recovery

# Private compatibility re-export; adapters use the notebooklm.auth facade.
from .._auth.cookie_policy import app_host_scope_note
from .._auth.profile_account import DomainSelection
from .._auth.profile_document import ProfileDocument
from .._auth.profile_store import ProfileStore, RemintWriteRequest, ReplaceResult

# The storage-state cookie filter is WRITE-time policy and lives beside the
# writers applying it (ADR-0033 PR 4.2); retained for private compatibility.
from .._auth.storage import _safe_cookie_shape as _safe_cookie_shape
from .._auth.storage import filter_storage_state_cookies_by_domain_policy

# Host-family sets are internal _env facts, not new public config exports.
from .._env import ENTERPRISE_APP_HOSTS, PERSONAL_APP_HOSTS
from .._url_utils import is_cookie_mismatch_redirect, is_google_auth_redirect
from ..config import get_base_host, get_base_url
from ..exceptions import HeadlessLoginRequiredError, LockUnavailableError

# Pure launch classifier (ADR-0008); retain private compatibility re-exports.
from .browser_launch_errors import CHANNEL_BROWSERS, classify_launch_failure

# Navigation-failure classification lives in its own pure leaf (ADR-0008); these
# remain re-exported below for private compatibility.
from .navigation_errors import (
    TARGET_CLOSED_ERROR,
    is_navigation_failure,
    is_navigation_interrupted_error,
    is_navigation_race,
    navigation_error_code,
)

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext, Page

logger = logging.getLogger(__name__)


class BrowserCaptureIO(Protocol):
    """Caller-injected presentation, exit, and async side effects.

    ``emit`` forwards arguments verbatim, including ``markup=False``; ``fail``
    aborts through the adapter. Interactive callers use the auth facade's
    callback bridge rather than importing the CLI here. Capture never calls
    ``run_async``: it remains for compatibility with post-capture app account
    repair, and first-party capture bridges implement it as a loud failure.
    """

    def emit(self, *args: Any, **kwargs: Any) -> None: ...

    def fail(self, code: int) -> NoReturn: ...

    def run_async(self, coro: Awaitable[Any]) -> Any: ...


GOOGLE_ACCOUNTS_URL = "https://accounts.google.com/"

# Retryable Playwright connection errors. Tracked by string-fragment match
# because Playwright surfaces them in the error message rather than via
# typed exceptions.
RETRYABLE_CONNECTION_ERRORS = ("ERR_CONNECTION_CLOSED", "ERR_CONNECTION_RESET")


def replace_captured_profile(
    path: Path,
    state: dict[str, Any],
    *,
    carry_account: bool,
    include_domains: set[str] | None,
) -> ReplaceResult:
    """Persist browser capture through the native profile-store result."""
    request = RemintWriteRequest(
        source=ProfileDocument.decode(dict(state)),
        carry_account=carry_account,
        domain_selection=DomainSelection(
            include_domains=frozenset(include_domains or ()),
            include_optional=False,
        ),
    )
    return ProfileStore(path).replace_from_remint(request)


LOGIN_MAX_RETRIES = 3
# Ceiling on CONSECUTIVE IMMEDIATE failed navigations in one login wait: generous
# against a real sign-in, tight enough to stop a no-delay failure loop from
# spinning out the timeout. See :func:`wait_for_login_landing`.
MAX_TOLERATED_NAVIGATION_FAILURES = 20
# A wait that failed faster than this took no real time, so the page — not the
# human — produced it. Only such back-to-back failures count toward the cap.
INSTANT_FAILURE_SECONDS = 0.25
CAPTURE_SETTLE_SECONDS = 2.0
CAPTURE_POLL_MS = 500
SIGN_IN_CHECK_TIMEOUT_MS = 30_000
CAPTURE_SNAPSHOT_ATTEMPTS = 3
BROWSER_CLOSED_HELP = (
    "[red]The browser window was closed during login.[/red]\n"
    "This can happen when switching Google accounts in a persistent browser session.\n\n"
    "Try:\n"
    "  1. Run: notebooklm login --fresh\n"
    "  2. Or run: notebooklm auth logout && notebooklm login"
)


class _CaptureAbortKind(Enum):
    """Private categories for unattended capture infrastructure aborts."""

    BROWSER_CLOSED = "browser_closed"
    CONNECTION_EXHAUSTED = "connection_exhausted"


class _HeadlessCaptureAbort(RuntimeError):
    """Private typed abort raised by infrastructure failures in headless mode."""

    def __init__(self, kind: _CaptureAbortKind) -> None:
        self.kind = kind
        super().__init__(kind.value)


def _abort_capture(
    io: BrowserCaptureIO,
    *,
    headless: bool,
    kind: _CaptureAbortKind,
) -> NoReturn:
    """Abort a capture, retaining infrastructure type for unattended callers."""
    if headless:
        raise _HeadlessCaptureAbort(kind)
    io.fail(1)


# ---------------------------------------------------------------------------
# Platform / page-recovery / URL helpers (neutral)
# ---------------------------------------------------------------------------


@contextmanager
def windows_playwright_event_loop() -> Iterator[None]:
    """Temporarily restore Playwright's required event-loop policy on Windows.

    Its subprocesses need ``ProactorEventLoop``, while the CLI installs an
    incompatible ``WindowsSelectorEventLoopPolicy`` (#79). Restore the original
    policy on exit; do nothing on other platforms.
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


@contextmanager
def sync_playwright_context() -> Iterator[Any]:
    """Enter synchronous Playwright with its required Windows event-loop policy."""
    from playwright.sync_api import sync_playwright

    with windows_playwright_event_loop(), sync_playwright() as playwright:
        yield playwright


def recover_page(
    context: BrowserContext,
    io: BrowserCaptureIO,
    *,
    headless: bool = False,
) -> Page:
    """Replace a stale page, inheriting the persistent context's cookies.

    If the browser is dead, emit the closed-browser help and abort through the
    interactive adapter or the typed headless infrastructure error. Other
    Playwright failures propagate unchanged.
    """
    from playwright.sync_api import Error as PlaywrightError

    try:
        return context.new_page()
    except PlaywrightError as exc:
        error_str = str(exc)
        if TARGET_CLOSED_ERROR in error_str:
            logger.error("Browser context is dead, cannot recover page: %s", error_str)
            io.emit(BROWSER_CLOSED_HELP)
            _abort_capture(
                io,
                headless=headless,
                kind=_CaptureAbortKind.BROWSER_CLOSED,
            )
        logger.error("Failed to create new page for recovery: %s", error_str)
        raise


def accepted_login_hosts() -> tuple[str, ...]:
    """Return the lowercased app-family hosts used by matching and DEBUG tracing.

    Either personal host accepts both aliases: Google may land on either one
    regardless of the configured host (#2017 / #2025). Enterprise accepts only
    its current and legacy Google-identity hosts. Sharing this set prevents
    diagnostic instructions from drifting from the actual predicate.
    """
    base_host = get_base_host().lower()
    for hosts in (PERSONAL_APP_HOSTS, ENTERPRISE_APP_HOSTS):
        if base_host in hosts:
            # Selected host first; remaining aliases sorted for stable diagnostics.
            return (base_host, *sorted(hosts - {base_host}))
    return (base_host,)


def url_matches_base_host(url: str) -> bool:
    """Return whether ``url`` is on a host in the configured app family."""
    current_host = (urlparse(url).hostname or "").lower()
    return current_host in accepted_login_hosts()


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
# Login-wait DEBUG tracing (absorbed from ``login_wait_trace.py``, ADR-0033)
#
# Host-only event tracing diagnoses stalled sign-ins without exposing SSO grants
# or coupling this core to the CLI (ADR-0021, #2046).
# ---------------------------------------------------------------------------

# Stand-in when the page's URL cannot be read at all. Distinct from
# ``trace_url("")`` (which returns ``""``) so an operator reading the log can
# tell "the page was gone" apart from "the URL was empty".
_UNREADABLE_URL = "<unavailable>"

# Rendering for a URL that carries no host at all (``about:blank``, ``data:``,
# ``chrome-error://``). The scheme is the useful signal; whatever follows it can
# be arbitrary opaque data, so it is never reproduced.
_HOSTLESS_URL = "{scheme}:<no host>"


def trace_url(url: str) -> str:
    """Render only ``scheme://host[:port]/``; omit all credential-bearing parts.

    Keep this separate from ``_auth.extraction._safe_url``, whose endpoint
    errors retain paths outside a Google-OAuth allowlist. Login observes
    arbitrary Workspace SSO providers: a path such as ``/sso/<assertion>`` can
    carry a grant on any host. Public ``-vv`` issue reports therefore retain
    only the host, which is sufficient to diagnose landing problems. Rebuild
    from ``hostname`` to drop userinfo; never include query or fragment.
    """
    if not url:
        return ""
    parsed = urlparse(url)
    host = parsed.hostname
    if not host:
        return _HOSTLESS_URL.format(scheme=parsed.scheme or "<unknown>")
    # ``hostname`` strips the brackets off an IPv6 literal, so they have to go
    # back on before a port can be appended — otherwise
    # ``https://[2001:db8::1]:8443/`` renders as ``https://2001:db8::1:8443/``,
    # where the port is indistinguishable from the address's last group.
    rendered_host = f"[{host}]" if ":" in host else host
    netloc = f"{rendered_host}:{parsed.port}" if parsed.port is not None else rendered_host
    return f"{parsed.scheme}://{netloc}/"


def _log_suppressed(what: str, exc: BaseException) -> None:
    """Record only a tracing failure's exception type.

    Never include the message or ``exc_info``: Playwright errors contain URLs,
    and traceback formatting bypasses host-only redaction. Heuristic secret
    scrubbing cannot recognize opaque grants in arbitrary SSO paths.
    """
    logger.debug("Login wait: %s (%s)", what, type(exc).__name__)


def safe_page_url(page: Any) -> str:
    """Return the credential-stripped page URL, or a placeholder if unreadable.

    A dead page can reject URL reads; diagnostics must not replace the existing
    browser-closed routing with an unhandled traceback.
    """
    try:
        return trace_url(page.url)
    except Exception as exc:
        _log_suppressed("could not read the page URL", exc)
        return _UNREADABLE_URL


def _is_main_frame(frame: Any, main_frame: Any) -> bool:
    """Match the top-level frame by identity or absence of a parent.

    The structural fallback supports distinct wrappers for one underlying
    frame, so identity changes cannot silently drop every navigation.
    """
    if main_frame is not None and frame is main_frame:
        return True
    return getattr(frame, "parent_frame", None) is None


@contextmanager
def log_observed_navigations(page: Any) -> Iterator[None]:
    """Trace main-frame navigations at DEBUG without affecting the login wait.

    With DEBUG off, attach nothing. Guard every diagnostic read and callback,
    and tolerate missing event support. URLs use host-only ``trace_url``
    redaction because arbitrary SSO paths, queries, fragments, and userinfo may
    carry credentials. Playwright stays optional, so the page is typed ``Any``.
    """
    if not logger.isEnabledFor(logging.DEBUG):
        yield
        return

    # ``getattr`` only absorbs a MISSING attribute — a ``main_frame`` property
    # that *raises* (dead page) would propagate straight past the ``yield`` and
    # pre-empt the wait entirely, so the read itself is guarded.
    try:
        main_frame = getattr(page, "main_frame", None)
    except Exception as exc:
        _log_suppressed("could not read the page's main frame", exc)
        main_frame = None

    def _on_navigated(frame: Any) -> None:
        try:
            # Sub-frame navigations (SSO iframes, ad frames) are noise; the
            # login predicate only ever looks at the main frame's URL.
            if not _is_main_frame(frame, main_frame):
                return
            logger.debug("Login wait: navigated to %s", trace_url(getattr(frame, "url", "") or ""))
        except Exception as exc:
            _log_suppressed("could not read a navigation URL", exc)

    try:
        page.on("framenavigated", _on_navigated)
    except Exception as exc:
        _log_suppressed("navigation logging unavailable", exc)

    try:
        yield
    finally:
        # Detach unconditionally rather than gating on "did ``on`` return
        # cleanly". A registration that raised *after* recording the handler
        # would otherwise leak a listener onto a page the caller keeps using,
        # and an unnecessary detach is free: removing a handler that was never
        # registered fails locally and is swallowed right here.
        try:
            page.remove_listener("framenavigated", _on_navigated)
        except Exception as exc:
            _log_suppressed("could not detach the navigation listener", exc)


def _current_url(page: Any) -> str:
    """Return the raw page URL for matching, or ``""`` if unreadable.

    Propagate TargetClosed so callers retain infrastructure-abort routing.
    Diagnostic ``safe_page_url`` remains tolerant even when the browser is gone.
    """
    try:
        is_closed = getattr(page, "is_closed", None)
        if callable(is_closed) and is_closed() is True:
            from playwright.sync_api import Error as PlaywrightError

            # Playwright may retain the last URL after only the page closes.
            raise PlaywrightError(TARGET_CLOSED_ERROR)
        return page.url or ""
    except Exception as exc:
        if TARGET_CLOSED_ERROR in str(exc):
            raise
        _log_suppressed("could not read the page URL", exc)
        return ""


@dataclass(frozen=True)
class _CaptureCookieObservation:
    """Stable app URL and its browser-scoped SID availability."""

    url: str
    has_sid: bool


def _capture_cookie_observation(page: Any, context: Any) -> _CaptureCookieObservation | None:
    """Read a stable app URL and whether its browser cookie scope has SID.

    App hosts also serve anonymous pages (#2467). Read cookies eligible for
    the observed URL; Playwright owns live domain/path/secure/expiry eligibility.
    Exclude sibling-domain SID cookies. Use at most three
    URL-scoped snapshots as reads pump browser events. Do not require
    PSIDTS or DOM tokens: incomplete captures retain recovery (#865 / #2082).
    """
    for _ in range(CAPTURE_SNAPSHOT_ATTEMPTS):
        url = _current_url(page)
        if not url_matches_base_host(url):
            return None
        cookies = context.cookies([url])
        if _current_url(page) != url:
            continue
        has_sid = any(
            isinstance(cookie, dict)
            and cookie.get("name") == "SID"
            and isinstance(cookie.get("value"), str)
            and bool(cookie["value"])
            for cookie in cookies
        )
        return _CaptureCookieObservation(url=url, has_sid=has_sid)
    return None


def _capture_candidate_url(page: Any, context: Any) -> str | None:
    """Find an app URL with a browser-routable SID, without claiming liveness."""
    observation = _capture_cookie_observation(page, context)
    return observation.url if observation is not None and observation.has_sid else None


def _settle_capture_candidate(page: Any, context: Any, *, deadline: float) -> bool:
    """Briefly allow cookies to arrive on an app landing, under the caller's budget."""
    settle_deadline = min(deadline, time.monotonic() + CAPTURE_SETTLE_SECONDS)
    while True:
        if _capture_candidate_url(page, context) is not None:
            return True
        remaining_ms = (settle_deadline - time.monotonic()) * 1000
        if remaining_ms <= 0 or not url_matches_base_host(_current_url(page)):
            return False
        # Playwright's wait pumps browser events, including same-document cookie
        # arrivals. time.sleep would stall them, and an on-host wait_for_url
        # resolves immediately, producing a busy loop.
        page.wait_for_timeout(min(CAPTURE_POLL_MS, remaining_ms))


def _captured_sid_is_usable(state: dict[str, Any], page: Any, context: Any) -> bool:
    """Guard the exported jar for bootstrap and configured RPC routing.

    HTTPX projection checks the filtered/healed export, a separate boundary
    from Playwright's live eligibility. Recheck both after healing: synchronous
    browser calls can dispatch pending navigation or logout events.
    """
    for _ in range(CAPTURE_SNAPSHOT_ATTEMPTS):
        observed_url = _capture_candidate_url(page, context)
        if (
            observed_url is None
            or not _auth_cookies._storage_has_routable_cookie(state, "SID", f"{get_base_url()}/")
            or not _auth_cookies._storage_has_routable_cookie(state, "SID", observed_url)
        ):
            return False
        if _current_url(page) == observed_url:
            return True
    return False


def _refuse_incomplete_capture(io: BrowserCaptureIO, *, headless: bool) -> NoReturn:
    """Refuse before replacing an existing profile with an unusable SID capture."""
    message = (
        "Could not verify a stable Google cookie capture for NotebookLM. "
        "The saved authentication was not replaced. Complete Google sign-in "
        "and retry 'notebooklm login'."
    )
    if headless:
        raise HeadlessLoginRequiredError(message)
    io.emit(f"[red]{message}[/red]")
    io.fail(1)


def _refuse_signed_out_capture() -> NoReturn:
    """Refuse an unattended capture whose browser session is signed out (#2482)."""
    raise HeadlessLoginRequiredError(
        "The browser's Google session is signed out, so it cannot re-mint "
        "NotebookLM authentication. The saved authentication was not replaced. "
        "Run 'notebooklm login' to re-authenticate."
    )


def _browser_session_is_signed_out(context: Any) -> bool:
    """Ask the app's ``/login`` through the browser context whether it is signed out.

    An app-host landing with a ``SID`` cookie does not prove a live session:
    a signed-out ``GET /`` is an HTTP 200 landing page, so an expired profile
    passes the candidate check. ``/login`` still enforces a session -- signed in
    it redirects back to the app, signed out to ``accounts.google.com`` (the
    probe behind #2481). The request shares the context's cookies. Only a
    successful response that ended on a sign-in page returns ``True``; an HTTP
    error such as 403 or 429 on that host does not establish session state.

    This is a liveness check for the browser's Google session, not an account
    check: ``/login`` ignores ``authuser``, so it cannot say whether a stored
    non-default account is the one signed in. A closed browser is re-raised so
    the caller's abort routing handles it. Any other Playwright failure (timeout,
    network, redirect loop) keeps the existing behaviour and is logged as a
    warning, type only, because Playwright errors embed URLs.
    """
    from playwright.sync_api import Error as PlaywrightError

    try:
        response = context.request.get(f"{get_base_url()}/login", timeout=SIGN_IN_CHECK_TIMEOUT_MS)
        final_url = str(response.url)
        answered = bool(response.ok)
        try:
            response.dispose()
        except PlaywrightError as exc:
            _log_suppressed("sign-in check response release", exc)
    except PlaywrightError as exc:
        if TARGET_CLOSED_ERROR in str(exc):
            raise
        logger.warning(
            "Browser capture: could not confirm the browser's Google session is signed in "
            "(%s); continuing without that check.",
            type(exc).__name__,
        )
        return False
    # A CookieMismatch interstitial is also served from accounts.google.com but
    # is a cookie-scoping fault, not a signed-out session.
    return (
        answered
        and is_google_auth_redirect(final_url)
        and not is_cookie_mismatch_redirect(final_url)
    )


def wait_for_login_landing(
    page: Any,
    *,
    timeout_s: float,
    io: BrowserCaptureIO | None = None,
    context: Any = None,
    deadline: float | None = None,
) -> int:
    """Wait for an app landing with a routed SID; return tolerated failures.

    Playwright rejects ``wait_for_url`` on any failed main-frame navigation,
    including unrelated sign-in hops (#2257). A failure need not mean login
    failed, so recheck the candidate and re-arm on the remaining deadline.
    TargetClosed and unrelated errors propagate unchanged.

    Bound both paced and immediate failures: the deadline covers slow hops;
    ``MAX_TOLERATED_NAVIGATION_FAILURES`` caps only consecutive instant failures,
    which otherwise spin at full speed. Slow failures reset that streak. An
    aborted hop can self-heal; a committed Chromium error page generally cannot,
    so the first failure notice names the code.
    """
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    if context is None:
        context = page.context
    supplied_deadline = deadline is not None
    if deadline is None:
        deadline = time.monotonic() + timeout_s
    # Standalone callers seed from the timeout value, preserving the exact
    # initial Playwright budget. Capture supplies its existing deadline so the
    # initial settle and the sign-in continuation spend the same budget.
    remaining_ms: float = timeout_s * 1000
    if supplied_deadline:
        remaining_ms = min(remaining_ms, (deadline - time.monotonic()) * 1000)
    tolerated = 0
    # What the cap bounds, kept separate from the reported total: a cumulative
    # count would also clip the honest slow case — 21 failures spread over a
    # 30-minute ``--browser-timeout`` would abort a sign-in still in progress,
    # which is the very bug this function fixes.
    instant_failures = 0
    while True:
        if remaining_ms <= 0:
            # Same landing recheck as the PlaywrightTimeout arm: the accepted
            # navigation can commit between the failure arm's URL read and this
            # deadline test, and a completed sign-in must never be reported as a
            # timeout.
            if _capture_candidate_url(page, context) is not None:
                return tolerated
            raise PlaywrightTimeout(f"Timeout {timeout_s * 1000:.0f}ms exceeded.")
        attempt_started = time.monotonic()
        try:
            # The SPA never fires "load"; "commit" resolves as soon as the
            # accepted host is reached (#1697). The callback must remain pure:
            # reentrant synchronous cookie reads there can deadlock Playwright.
            page.wait_for_url(
                url_matches_base_host,
                wait_until="commit",
                timeout=remaining_ms,
            )
            if _capture_candidate_url(page, context) is not None:
                return tolerated
            instant_failures = 0
            remaining_ms = min(remaining_ms, (deadline - time.monotonic()) * 1000)
            if remaining_ms > 0:
                page.wait_for_timeout(min(CAPTURE_POLL_MS, remaining_ms))
            remaining_ms = min(remaining_ms, (deadline - time.monotonic()) * 1000)
        except PlaywrightTimeout:
            # Playwright's timeout is a task racing the ``navigated`` event, so
            # losing that race by a hair is possible: check whether the browser
            # landed anyway before reporting "not detected". Same reasoning as
            # the navigation-failure arm below — the accept predicate, not the
            # exception, decides whether we are done.
            if _capture_candidate_url(page, context) is not None:
                return tolerated
            raise
        except PlaywrightError as exc:
            if not is_navigation_failure(exc):
                raise
            # The failed navigation may be a *later* hop than the one that landed
            # us, and the human can arrive between the rejection and the re-arm.
            # The accept predicate, not the exception, decides if we are done.
            if _capture_candidate_url(page, context) is not None:
                return tolerated
            tolerated += 1
            # A failure that took real time is the page pacing us and RESETS the
            # streak; only back-to-back no-delay failures are the reload loop the
            # cap is for. The deadline bounds the paced case.
            if time.monotonic() - attempt_started < INSTANT_FAILURE_SECONDS:
                instant_failures += 1
            else:
                instant_failures = 0
            if instant_failures > MAX_TOLERATED_NAVIGATION_FAILURES:
                # Past this many with no delay between them, the page is not
                # racing — it is failing in a loop, and re-arming forever would
                # burn the rest of the timeout at full tilt and bury the cause.
                # Surface the last error so the caller's routing reports
                # something honest.
                logger.error(
                    "Login wait: gave up after %d consecutive immediate failed "
                    "navigations (%s); %d tolerated in total",
                    instant_failures,
                    navigation_error_code(exc) or type(exc).__name__,
                    tolerated,
                )
                # Say what happened BEFORE re-raising: the exception reaches the
                # CLI as "Unexpected error … please report a bug", and a captive
                # portal looping the sign-in is no defect of ours. Re-raising
                # rather than ``io.fail`` is deliberate — an unclassified
                # Playwright error must stay visible.
                if io is not None:
                    io.emit(
                        f"[red]The browser could not complete a navigation "
                        f"({navigation_error_code(exc) or 'repeated failures'}) "
                        f"after {tolerated} attempts.[/red]\n"
                        "A proxy, captive portal, or VPN interrupting the sign-in "
                        "flow is the usual cause.\n"
                        "To skip the browser entirely, read cookies from one you are "
                        "already signed in to: "
                        "[cyan]notebooklm login --browser-cookies[/cyan] "
                        "(needs the 'cookies' extra)."
                    )
                raise
            # The aborted hop is invisible to ``log_observed_navigations``:
            # Playwright only emits the public "framenavigated" event when the
            # event carries no error, so the -vv trace goes silent at exactly
            # the moment of interest. This is the line that fills that gap.
            logger.debug(
                "Login wait: tolerated a failed navigation (%s) on %s; still waiting",
                navigation_error_code(exc) or type(exc).__name__,
                safe_page_url(page),
            )
            if tolerated == 1 and io is not None:
                # Name the code: this also fires for a COMMITTED error page that
                # cannot self-heal, and ``ERR_BLOCKED_BY_ADMINISTRATOR`` reading
                # as a vague "interrupted" would cost the user their one clue.
                # The code carries no URL.
                code = navigation_error_code(exc)
                detail = f" ({code})" if code else ""
                io.emit(
                    f"[yellow]A navigation failed{detail}; still waiting for sign-in...[/yellow]"
                )
            # Re-arm on what is left, clamped so floating cancellation cannot
            # grow the caller's budget during any number of tolerated hops.
            remaining_ms = min(remaining_ms, (deadline - time.monotonic()) * 1000)


# ---------------------------------------------------------------------------
# Captured-state heal (absorbed from ``browser_state_validation.py``, ADR-0033)
#
# One best-effort in-memory heal before persistence, shared by capture arms.
# ---------------------------------------------------------------------------


def heal_captured_state(state: dict[str, Any]) -> tuple[dict[str, Any], ValueError | None]:
    """Try one best-effort in-memory PSIDTS heal, preserving declined captures.

    Passive login navigations can withhold PSIDTS despite SID and a secondary
    binding (#865). Reuse the rookiepy recovery contract to avoid a cold-start
    heal when possible. Adapt only ``httpOnly`` spelling; preserve existing
    ``sameSite`` and use the converter's safe default for newly minted cookies.

    Return errors instead of raising: a declined rotation or network blip must
    not discard SID-bearing sign-in material (#2082). Disk recovery may retry
    on the next command. Successful rows are rebuilt from sanitized entries,
    which discard empty/non-string values even if the domain filter kept them.

    Return ``(state, error)``: validated/healed rows and ``None`` on success;
    the original unchanged state and final validation error on decline.
    """
    rookiepy_rows: list[dict[str, Any]] = []
    for entry in _auth_cookies._sanitized_auth_entries(state):
        rookiepy_entry = dict(entry)
        rookiepy_entry["http_only"] = bool(entry.get("httpOnly", False))
        rookiepy_rows.append(rookiepy_entry)

    validated_state, error = _psidts_recovery.validate_with_recovery(rookiepy_rows)
    if error is not None:
        return state, error
    return {
        "cookies": validated_state["cookies"],
        "origins": list(state.get("origins", [])),
    }, None


# ---------------------------------------------------------------------------
# Neutral capture core: launch -> navigate -> capture -> filter -> persist
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BrowserCapturePlan:
    """Frozen description of one browser-capture attempt.
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
    login_timeout_s: int = 300


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
    """Abort with a browser-specific install hint if Playwright is unavailable.

    The adapter calls this before its launch banner, preserving the historical
    hint-only failure. System channels need the browser extra; bundled Chromium
    also needs its executable installed. ``markup=False`` keeps the literal
    ``[browser]`` extra, and the import remains lazy for optional dependencies.
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
    """Accept only interactive headed login and unattended headless re-auth.

    A visible unattended browser can hang, while headless interactive login is
    contradictory. Reject other pairings with ``NotImplementedError`` as a
    programmer error, rather than routing through the unused ``io`` adapter.
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
    """Launch, navigate, capture, filter, heal, and atomically persist auth state.

    Shared by interactive CLI login and layer-3 headless profile re-auth. Import
    Playwright lazily; retry transient connection failures; wait for a capture
    candidate interactively or settle briefly unattended. Pin ``.google.com``
    cookies, filter domains, and guard SID routing before and after one heal.
    The adapter owns the Chromium-install pre-flight before this core runs.
    """
    _reject_unsupported_mode(headless=headless, interactive=interactive, io=io)

    browser = plan.browser
    browser_profile = plan.browser_profile
    storage_path = plan.storage_path
    include_domains = plan.include_domains

    # Fail fast with the install hint when the ``browser`` extra is absent. The
    # app flow reaches the facade's availability capability earlier (before its
    # banner); calling it again here is cheap and keeps the contract intact for
    # any private caller.
    ensure_playwright_available(io, browser=browser)
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    def _capture_page_html(page: Any) -> str | None:
        try:
            content = page.content()
        except PlaywrightError as exc:
            logger.debug("Could not read Playwright page content for account metadata: %s", exc)
            return None
        return content if isinstance(content, str) else None

    captured_page_html: str | None = None

    with sync_playwright_context() as p:
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

            page = (
                context.pages[0] if context.pages else recover_page(context, io, headless=headless)
            )
            # Whether ANY navigation of ours has committed. A persistent context
            # restores tabs, so ``page.url`` may already be an accepted host that
            # no request of ours produced — and treating that as proof of login
            # captures whatever cookies the profile holds, valid or expired.
            navigation_committed = False

            # Retry navigation on transient connection errors with backoff
            for attempt in range(1, LOGIN_MAX_RETRIES + 1):
                try:
                    # wait_until="commit": the app host serves a streaming
                    # SPA that never fires the "load" event (readyState stays
                    # "interactive"), so Playwright's default wait_until="load"
                    # would block until timeout. "commit" resolves once response
                    # headers are processed -- enough to land on the host and
                    # classify page.url. See #1697 (and the #214 precedent below).
                    page.goto(f"{get_base_url()}/", wait_until="commit", timeout=30000)
                    navigation_committed = True
                    break
                except PlaywrightError as exc:
                    error_str = str(exc)
                    is_connection_error = any(
                        code in error_str for code in RETRYABLE_CONNECTION_ERRORS
                    )
                    is_target_closed = TARGET_CLOSED_ERROR in error_str
                    # Google's redirect can cancel this goto before it commits,
                    # which is the same benign class the login wait tolerates
                    # (#2257). Classified only after the two categories that own
                    # their own remediation, since ``ERR_CONNECTION_*`` is itself
                    # a ``net::ERR_*`` code and must keep the connection help.
                    is_nav_failure = (
                        not is_connection_error
                        and not is_target_closed
                        and is_navigation_race(error_str)
                    )
                    is_retryable = is_connection_error or is_nav_failure

                    if (is_retryable or is_target_closed) and attempt < LOGIN_MAX_RETRIES:
                        if is_target_closed:
                            page = recover_page(context, io, headless=headless)

                        backoff_seconds = attempt  # Linear backoff: 1s, 2s
                        # Code only: a ``goto`` failure embeds the URL, and
                        # ``scrub_secrets`` cannot mask credential material in a
                        # URL *path*. Same rule as :func:`_log_suppressed`.
                        logger.debug(
                            "Retryable error on attempt %d/%d: %s",
                            attempt,
                            LOGIN_MAX_RETRIES,
                            navigation_error_code(error_str) or type(exc).__name__,
                        )
                        if is_target_closed:
                            io.emit(
                                f"[yellow]Browser page closed "
                                f"(attempt {attempt}/{LOGIN_MAX_RETRIES}). "
                                f"Retrying with fresh page...[/yellow]"
                            )
                        elif is_nav_failure:
                            # No backoff: the navigation was superseded, not
                            # refused. There is no overloaded peer to wait for.
                            io.emit(
                                f"[yellow]Navigation interrupted "
                                f"(attempt {attempt}/{LOGIN_MAX_RETRIES}). "
                                f"Retrying...[/yellow]"
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
                        _abort_capture(
                            io,
                            headless=headless,
                            kind=_CaptureAbortKind.BROWSER_CLOSED,
                        )
                    elif is_nav_failure and interactive:
                        # INTERACTIVE ONLY — equivalently ``not headless``, since
                        # ``_reject_unsupported_mode`` rejects any other pairing.
                        # A human still has to sign in and the wait re-reads
                        # ``page.url``, so a bug report helps nobody.
                        # The headless arm must NOT take this path: nothing
                        # committed, so its landing check would read a STALE
                        # ``page.url`` — a restored tab on a NotebookLM URL passes
                        # and re-auth persists unvalidated cookies while reporting
                        # success. Falling through to ``raise`` is the honest
                        # pre-#2257 behaviour there.
                        logger.debug(
                            "Navigation kept being interrupted (%s) after %d attempts; "
                            "continuing to the landing check",
                            navigation_error_code(error_str) or "no net:: code",
                            LOGIN_MAX_RETRIES,
                        )
                        break
                    elif is_connection_error:
                        logger.error(
                            f"Failed to connect to NotebookLM after {LOGIN_MAX_RETRIES} attempts. "
                            f"Last error: {error_str}"
                        )
                        io.emit(connection_error_help())
                        _abort_capture(
                            io,
                            headless=headless,
                            kind=_CaptureAbortKind.CONNECTION_EXHAUSTED,
                        )
                    else:
                        # Code/type only — see the retry log above.
                        logger.debug(
                            "Non-retryable error: %s",
                            navigation_error_code(error_str) or type(exc).__name__,
                        )
                        raise

            login_deadline = time.monotonic() + plan.login_timeout_s
            if headless:
                # There is no human to complete a form. Allow only a brief
                # cookie settle, then fail with the existing typed outcome.
                # Host + SID permits capture; it does not prove server liveness.
                if not navigation_committed or not _settle_capture_candidate(
                    page, context, deadline=login_deadline
                ):
                    logger.warning(
                        "Headless re-auth: no app landing with a routed SID "
                        "after navigation; cannot silently re-mint cookies."
                    )
                    raise HeadlessLoginRequiredError(
                        "Headless re-auth could not reach NotebookLM: the "
                        "persisted browser profile has no usable Google "
                        "session cookies. Run 'notebooklm login' to re-authenticate."
                    )
            elif navigation_committed and _settle_capture_candidate(
                page, context, deadline=login_deadline
            ):
                io.emit("[dim]Capturing Google cookies...[/dim]")
            else:
                io.emit("\n[bold green]Instructions:[/bold green]")
                io.emit("1. Complete the Google login in the browser window")
                io.emit("2. Authentication will be saved automatically once login is detected\n")
                timeout_s = plan.login_timeout_s
                timeout_label = "5 minutes" if timeout_s == 300 else f"{timeout_s} seconds"
                io.emit(f"[dim]Waiting for login (up to {timeout_label})...[/dim]")
                # Name the accept set/start before blocking so a ``-vv`` paste
                # diagnoses a stuck login (#2046). Keep all diagnostic work
                # inside the explicit level gate.
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug(
                        "Login wait: accepting any of %s (currently on %s); timeout %ss",
                        ", ".join(accepted_login_hosts()),
                        safe_page_url(page),
                        timeout_s,
                    )
                try:
                    with log_observed_navigations(page):
                        # Anonymous app pages never redirect on their own. Give
                        # an initial on-app, SID-less landing one Google sign-in
                        # continuation; never steer an in-progress off-host SSO
                        # flow or repeatedly redirect a human entering credentials.
                        # A missing candidate can also mean a moving/off-host page;
                        # steer only a still-current stable no-SID observation.
                        observation = _capture_cookie_observation(page, context)
                        remaining_ms = (login_deadline - time.monotonic()) * 1000
                        if (
                            observation is not None
                            and not observation.has_sid
                            and _current_url(page) == observation.url
                            and remaining_ms > 0
                        ):
                            continuation = urlencode({"continue": f"{get_base_url()}/"})
                            try:
                                page.goto(
                                    f"{GOOGLE_ACCOUNTS_URL}ServiceLogin?{continuation}",
                                    wait_until="commit",
                                    timeout=remaining_ms,
                                )
                                navigation_committed = True
                            except PlaywrightError as exc:
                                # Owned navigation: tolerate superseded requests,
                                # not network/configuration faults (navigation_errors).
                                if not is_navigation_race(exc):
                                    raise
                        wait_for_login_landing(
                            page,
                            timeout_s=timeout_s,
                            io=io,
                            context=context,
                            deadline=login_deadline,
                        )
                        # A restored wait-only match adds no commit evidence.
                        # Cookie-forcing gotos may still establish a real commit;
                        # the post-forcing guard decides before export.
                except PlaywrightTimeout:
                    if logger.isEnabledFor(logging.DEBUG):
                        logger.debug(
                            "Login wait: timed out after %ss on %s",
                            timeout_s,
                            safe_page_url(page),
                        )
                    io.emit(
                        f"[red]Login not detected within {timeout_label}.[/red]\n"
                        "Try again with: notebooklm login\n"
                        "Already signed in to Google in Chrome? Retry with "
                        "[cyan]notebooklm login --browser chrome[/cyan] to reuse that "
                        "session (often detects immediately; also avoids "
                        "bundled-Chromium issues on macOS).\n"
                        "Or skip the browser launch entirely and read cookies from a "
                        "browser you are already signed in to: "
                        "[cyan]notebooklm login --browser-cookies[/cyan] "
                        "(needs the 'cookies' extra)."
                    )
                    io.fail(1)
                except PlaywrightError as exc:
                    # Browser/tab closed during the wait. Cannot resume a
                    # partially completed SSO form, so surface the same
                    # help text other browser-closed paths use.
                    if TARGET_CLOSED_ERROR in str(exc):
                        io.emit(BROWSER_CLOSED_HELP)
                        _abort_capture(
                            io,
                            headless=headless,
                            kind=_CaptureAbortKind.BROWSER_CLOSED,
                        )
                    raise
                io.emit("[dim]Capturing Google cookies...[/dim]")

            active_page_html = _capture_page_html(page)

            # Force .google.com cookies for regional users (e.g. UK lands on
            # .google.co.uk). "commit" resolves once response headers (incl.
            # Set-Cookie) are processed, before a client-side redirect can
            # interrupt. See #214.
            recovered_during_cookie_forcing = False
            for url in [GOOGLE_ACCOUNTS_URL, f"{get_base_url()}/"]:
                try:
                    page.goto(url, wait_until="commit")
                    navigation_committed = True
                except PlaywrightError as exc:
                    error_str = str(exc)
                    if TARGET_CLOSED_ERROR in error_str:
                        # Page was destroyed (e.g. user switched accounts) -- get fresh page
                        page = recover_page(context, io, headless=headless)
                        navigation_committed = False
                        recovered_during_cookie_forcing = True
                        try:
                            page.goto(url, wait_until="commit")
                            navigation_committed = True
                        except PlaywrightError as inner_exc:
                            if TARGET_CLOSED_ERROR in str(inner_exc):
                                io.emit(BROWSER_CLOSED_HELP)
                                _abort_capture(
                                    io,
                                    headless=headless,
                                    kind=_CaptureAbortKind.BROWSER_CLOSED,
                                )
                            elif not is_navigation_race(inner_exc):
                                raise
                    elif not is_navigation_race(error_str):
                        raise

            # Defense-in-depth: wait_for_url proved we reached the host, but the
            # cookie-forcing round-trip above can land us back on
            # accounts.google.com if the session was invalidated mid-flow (rare).
            # Auto-detect is non-interactive, so fail fast with a clear next step.
            if not url_matches_base_host(_current_url(page)):
                # ``trace_url``, not the raw value: a swallowed cookie-forcing
                # race can leave ``page.url`` on a credential-bearing SSO URL.
                io.emit(
                    f"[red]Unexpected URL after login: {safe_page_url(page)}[/red]\n"
                    "Authentication may be incomplete. "
                    "Try: notebooklm login --fresh"
                )
                if headless:
                    raise HeadlessLoginRequiredError(
                        "Headless re-auth did not finish on a NotebookLM page. "
                        "Run 'notebooklm login' to re-authenticate."
                    )
                io.fail(1)
            if not navigation_committed:
                message = (
                    "Login navigation never committed. The saved authentication was not "
                    "replaced. Retry: notebooklm login"
                )
                io.emit(f"[red]{message}[/red]")
                if headless:
                    raise HeadlessLoginRequiredError(message)
                io.fail(1)

            if recovered_during_cookie_forcing:
                active_page_html = _capture_page_html(page)

            # Atomic write with chmod 0o600 — Playwright's path= writes directly
            # (non-atomic + world-readable window). Apply the same cookie-domain
            # allowlist the rookiepy path uses so sibling-product cookies (mail,
            # myaccount, docs, youtube) the user is signed into in the same
            # browser session don't leak into ``storage_state.json`` (opt-in via
            # ``--include-domains=...``).
            # NOT the writer's pass repeated — do not delete it as redundant
            # (ADR-0033 D3). It runs BEFORE ``heal_captured_state``, so the heal's
            # routing preflight and recovery-jar build see domain-filtered rows,
            # not sibling-product cookies + domain-variant name collisions (#2054).
            if headless and _browser_session_is_signed_out(context):
                _refuse_signed_out_capture()
            playwright_state = context.storage_state()
            filtered_state: dict[str, Any] = filter_storage_state_cookies_by_domain_policy(
                dict(playwright_state), include_domains=include_domains
            )
            if not _captured_sid_is_usable(filtered_state, page, context):
                _refuse_incomplete_capture(io, headless=headless)
            filtered_state, heal_error = heal_captured_state(filtered_state)
            if not _captured_sid_is_usable(filtered_state, page, context):
                _refuse_incomplete_capture(io, headless=headless)
            # Persist through the canonical writer under the storage lock (fixes
            # [capture-2], the lockless re-mint write). The unattended
            # headless-launch arm re-mints against OUR OWN profile, so it carries
            # the existing account namespace forward (carry_account=True — fixes
            # [capture-1]); the interactive arm may have signed into a different
            # account, so it drops the stale binding (carry_account=False) and
            # the CLI adapter's repair re-establishes it. The writer filters
            # again under the lock: ADR-0029's entry-path-independent guarantee,
            # a DIFFERENT obligation from the pre-heal pass above (it holds for
            # callers that never filtered). Neither pass may be dropped.
            # A declined PSIDTS heal must not discard a SID-bearing capture:
            # the disk-based cold-start recovery retries from it (#865 / #2082).
            outcome = replace_captured_profile(
                storage_path,
                filtered_state,
                carry_account=headless,
                include_domains=include_domains,
            )
            if outcome.lock_unavailable:
                raise LockUnavailableError(
                    f"browser capture: storage lock unavailable at {storage_path}"
                )
            if heal_error is not None:
                logger.warning(
                    "Saved the captured session, but it has no usable "
                    "__Secure-1PSIDTS and the in-memory rotation did not supply "
                    "one (%s). The next command retries the heal from disk; if "
                    "authentication keeps failing, re-run 'notebooklm login'.",
                    heal_error,
                )
            captured_page_html = active_page_html

        except Exception as e:
            # Handle browser launch errors specially (context will be None if
            # launch failed). This covers the bundled Chromium too, not just the
            # system channels: before #2004 a bundled-launch failure had no
            # friendly branch at all and fell through to the bare ``raise``
            # below, surfacing as "Unexpected error: ... please report a bug".
            if context is None:
                launch_help = classify_launch_failure(browser, str(e))
                # Remediation prose is for a human, and only the interactive arm
                # has one. Short-circuiting the unattended L3 arm via ``io.fail``
                # would be actively harmful: the unattended sink maps remaining
                # user-facing ``io.fail`` paths to ``HeadlessLoginRequiredError``.
                # Those paths retain the existing dead-session classification;
                # infrastructure aborts use the private typed marker below.
                # Letting the original exception propagate instead lands it in
                # that caller's generic arm as an honest "headless capture
                # failed: <Type>" (it logs there; the fall-through ``logger.debug``
                # below keeps the traceback either way). See #2043.
                if launch_help is not None and interactive:
                    # ``exc_info`` because this branch never re-raises ``e``.
                    logger.error(
                        "Browser launch failed (browser=%s): %s", browser, e, exc_info=True
                    )
                    io.emit(launch_help)
                    io.fail(1)
            # Last-resort TargetClosed mapping for anything that escapes the
            # in-flow guards (recover_page, the navigation retry loop,
            # wait_for_url, cookie-forcing) — in practice the final
            # ``context.storage_state()`` capture (#1514). Those paths already
            # map TargetClosed to BROWSER_CLOSED_HELP + exit 1; mirror them
            # here so the user gets the same friendly help instead of the
            # exit-2 bug-report hint. (The launch branch above never falls
            # through for a classified launch failure — it io.fail(1)s — and
            # launch failures are not TargetClosed.)
            if isinstance(e, PlaywrightError) and TARGET_CLOSED_ERROR in str(e):
                io.emit(BROWSER_CLOSED_HELP)
                _abort_capture(
                    io,
                    headless=headless,
                    kind=_CaptureAbortKind.BROWSER_CLOSED,
                )
            # For everything else, the diagnostic stays at debug level; the bare
            # ``raise`` propagates to ``handle_errors`` → friendly
            # ``Unexpected error: <msg>`` + exit 2.
            logger.debug("Login failed: %s", e, exc_info=True)
            raise
        finally:
            if context:
                try:
                    context.close()
                except PlaywrightError as close_exc:
                    # A browser that died during capture can also reject
                    # teardown; do not let that replace the typed abort.
                    if TARGET_CLOSED_ERROR not in str(close_exc):
                        raise

    return CaptureResult(page_html=captured_page_html)


def run_cdp_capture(
    plan: BrowserCapturePlan,
    io: BrowserCaptureIO,
    *,
    cdp_url: str,
) -> CaptureResult:
    """Capture auth from an operator-provided Chrome CDP endpoint.

    This explicit, opt-in layer-3 source uses the same app/SID candidate,
    filtering, best-effort heal, and guarded atomic persistence as headless
    profile capture. It never waits for a human; an incomplete landing raises
    ``HeadlessLoginRequiredError``.

    CDP is account-equivalent and local unattended only, never a hosted/remote
    MCP auth path. ``resolve_cdp_url`` enforces loopback upstream. Do not
    rediscover endpoints, log the endpoint, or log cookie values.

    The attached Chrome belongs to the operator. Reuse its existing context,
    never create a fresh logged-out one, and fail if no context exists. Navigate
    and close only our temporary page, leaving the operator's tabs/context
    untouched. ``browser.close()`` disconnects the CDP client without killing
    Chrome. Preserve that ownership policy on every failure path.

    ``plan.browser`` and ``plan.browser_profile`` are ignored; storage path and
    domain selection retain their normal meaning. The headless IO sink drops
    presentation lines. Return best-effort final page HTML in ``CaptureResult``.
    """
    ensure_playwright_available(io, browser="chromium")
    from playwright.sync_api import Error as PlaywrightError

    storage_path = plan.storage_path
    include_domains = plan.include_domains
    captured_page_html: str | None = None

    def _capture_page_html(page: Any) -> str | None:
        try:
            content = page.content()
        except PlaywrightError as exc:
            logger.debug("Could not read CDP page content: %s", exc)
            return None
        return content if isinstance(content, str) else None

    with sync_playwright_context() as p:
        try:
            browser = p.chromium.connect_over_cdp(cdp_url)
        except PlaywrightError as exc:
            if TARGET_CLOSED_ERROR in str(exc):
                raise _HeadlessCaptureAbort(_CaptureAbortKind.BROWSER_CLOSED) from exc
            raise
        page = None
        try:
            # Reuse a context the operator's Chrome already holds — that context
            # carries the live Google session we are harvesting. We must NOT
            # create a fresh ``new_context``: a brand-new context would be
            # logged out (no session), so capturing from it would be useless and
            # could overwrite ``storage_state.json`` with a logged-out state. If
            # the attached browser exposes no context, fail loudly rather than
            # fabricate one.
            if not browser.contexts:
                raise HeadlessLoginRequiredError(
                    "CDP re-auth: the attached browser exposes no browser "
                    "context to harvest a session from. Open a tab in that "
                    "Chrome (or run 'notebooklm login')."
                )
            context = browser.contexts[0]

            # Create a TEMPORARY page we own for the navigation, and close ONLY
            # that page in ``finally`` — never the operator's own tabs/context.
            # This avoids navigating (and thereby disrupting) a tab the operator
            # is actively using.
            page = context.new_page()
            # wait_until="commit": same streaming-SPA reason as the headed login
            # arm -- the default "load" never fires on the app host, so
            # this CDP re-auth goto would otherwise waste 30s then TimeoutError
            # before landing classification. See #1697.
            page.goto(f"{get_base_url()}/", wait_until="commit", timeout=30000)

            # Same candidate classification as the headless launch arm: an app
            # host without a URL-scoped SID must not replace saved authentication.
            if not _settle_capture_candidate(
                page, context, deadline=time.monotonic() + CAPTURE_SETTLE_SECONDS
            ):
                logger.warning(
                    "CDP re-auth: no app landing with a routed SID after navigation; "
                    "cannot re-mint cookies."
                )
                raise HeadlessLoginRequiredError(
                    "CDP re-auth could not reach NotebookLM from the attached "
                    "browser: its Google session cannot reach NotebookLM. Sign "
                    "in to NotebookLM in that browser, or run 'notebooklm login'."
                )

            captured_page_html = _capture_page_html(page)

            # Same cookie-domain allowlist + atomic 0o600 write as every other
            # capture path, so the on-disk state is equivalent regardless of the
            # credential source. Capture from the operator's CONTEXT (its cookie
            # jar), not from our temporary page.
            # As in the launch arm, this pass feeds ``heal_captured_state``
            # filtered rows (ADR-0033 D3) — and matters most here: CDP attaches
            # to the operator's DAILY Chrome, the richest source of sibling-
            # product cookies and domain-variant name collisions (#2054).
            if _browser_session_is_signed_out(context):
                _refuse_signed_out_capture()
            playwright_state = context.storage_state()
            filtered_state: dict[str, Any] = filter_storage_state_cookies_by_domain_policy(
                dict(playwright_state), include_domains=include_domains
            )
            if not _captured_sid_is_usable(filtered_state, page, context):
                _refuse_incomplete_capture(io, headless=True)
            filtered_state, heal_error = heal_captured_state(filtered_state)
            if not _captured_sid_is_usable(filtered_state, page, context):
                _refuse_incomplete_capture(io, headless=True)
            # Persist through the canonical writer under the storage lock (fixes
            # [capture-2]). CDP attaches to the operator's DAILY Chrome, whose
            # account set may not match our stored binding — carrying it blindly
            # could misroute. Per the plan's CDP caveat, we take the no-resolve
            # fallback (carry_account=False): drop the stale binding to the
            # authuser=0 default rather than risk a wrong-account route. NOTE:
            # downstream account-metadata repair runs only on the CLI ``auth
            # refresh`` path (refresh_stored_session -> repair_after_refresh);
            # the library / mid-RPC CDP re-mint arm performs NO repair, so it
            # deliberately lands on authuser=0 here — behaviourally identical to
            # the pre-refactor whole-file overwrite (no regression). Full
            # stored-email re-resolution against the captured jar would be a
            # caller-side network lookup OUTSIDE this lock.
            # A declined PSIDTS heal must not discard a SID-bearing capture:
            # disk-based cold-start recovery retries from it. As in the launch
            # arm, the writer's own pass under the lock is ADR-0029's entry-path-
            # independent guarantee, not a repeat of the pre-heal pass above.
            outcome = replace_captured_profile(
                storage_path,
                filtered_state,
                carry_account=False,
                include_domains=include_domains,
            )
            if outcome.lock_unavailable:
                raise LockUnavailableError(
                    f"CDP capture: storage lock unavailable at {storage_path}"
                )
            if heal_error is not None:
                logger.warning(
                    "Saved the captured session, but it has no usable "
                    "__Secure-1PSIDTS and the in-memory rotation did not supply "
                    "one (%s). The next command retries the heal from disk; if "
                    "authentication keeps failing, re-run 'notebooklm login'.",
                    heal_error,
                )
        except PlaywrightError as exc:
            if TARGET_CLOSED_ERROR in str(exc):
                raise _HeadlessCaptureAbort(_CaptureAbortKind.BROWSER_CLOSED) from exc
            raise
        finally:
            # Close ONLY the temporary page we created — never the operator's
            # tabs or context.
            if page is not None:
                try:
                    page.close()
                except PlaywrightError as exc:
                    logger.debug("Could not close temporary CDP page: %s", type(exc).__name__)
            # CDP teardown: disconnect only. Per Playwright's ``Browser.close``
            # contract, a *connected* browser (``connect_over_cdp``, as here) is
            # NOT terminated — it "clears all created contexts belonging to this
            # browser and disconnects from the browser server." We never call
            # ``new_context`` (we reuse the operator's existing context), so this
            # clears none of the operator's contexts and only severs our
            # connection, leaving their Chrome + tabs running. (It only
            # force-quits a ``launch()``-obtained browser, which this never is.)
            browser.close()

    return CaptureResult(page_html=captured_page_html)


# What is NOT here, and why. This list was hand-simulating a package interface:
# several entries once existed only so callers had one import site for a name
# defined in another leaf. ADR-0033 PR 4.1 absorbed two of
# those leaves, so ``log_observed_navigations`` / ``safe_page_url`` / ``trace_url``
# (login-wait tracing) and ``heal_captured_state`` are now ordinary definitions of
# this module with no consumer outside it — nothing re-exports them, so they are
# not advertised here. The entries that remain are either owned here or are the
# compatibility re-exports annotated below.
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
    "accepted_login_hosts",
    # Re-exported from the cookie_policy leaf for private compatibility.
    "app_host_scope_note",
    # Re-exported from the browser_launch_errors leaf for private compatibility.
    "classify_launch_failure",
    "connection_error_help",
    "ensure_playwright_available",
    "filter_storage_state_cookies_by_domain_policy",
    "is_navigation_interrupted_error",
    "recover_page",
    "run_browser_capture",
    "run_cdp_capture",
    "sync_playwright_context",
    "url_matches_base_host",
    "windows_playwright_event_loop",
]
