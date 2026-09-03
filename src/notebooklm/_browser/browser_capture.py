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
from urllib.parse import urlparse

# Collaborators of :func:`heal_captured_state` (absorbed from
# ``browser_state_validation.py``, ADR-0033 PR 4.1): the sanitiser that shapes
# captured rows for the rookiepy contract, and the shared PSIDTS recovery that
# contract runs. These were already on this module's import path transitively,
# via the leaf that used to hold the bridge.
from .._auth import cookies as _auth_cookies
from .._auth import psidts_recovery as _psidts_recovery

# ``app_host_scope_note`` owns the both-personal-hosts cookie-scope caveat that
# every "open the app in your browser" instruction needs (it is appended to the
# binding-related hints in ``cookie_policy.missing_cookies_hint``). It is
# retained here as a private-package compatibility re-export. First-party
# adapters reach the canonical identity through the ``notebooklm.auth`` facade.
from .._auth.cookie_policy import app_host_scope_note
from .._auth.profile_account import DomainSelection
from .._auth.profile_document import ProfileDocument
from .._auth.profile_store import ProfileStore, RemintWriteRequest, ReplaceResult

# The storage-state cookie filter is WRITE-time policy and lives beside the
# writers applying it (ADR-0033 PR 4.2); retained for private compatibility.
from .._auth.storage import _safe_cookie_shape as _safe_cookie_shape
from .._auth.storage import filter_storage_state_cookies_by_domain_policy

# ``PERSONAL_APP_HOSTS`` is imported from ``_env`` rather than ``config``
# deliberately: it is not part of ``config.__all__``, and re-exporting it there
# just to reach it here would add a public export for an internal host fact.
# Importing ``_env`` directly is the established idiom (``_url_utils`` and
# friends do the same).
from .._env import PERSONAL_APP_HOSTS
from ..config import get_base_host, get_base_url
from ..exceptions import HeadlessLoginRequiredError, LockUnavailableError

# ``CHANNEL_BROWSERS`` and the launch-failure triage live in the
# ``browser_launch_errors`` leaf (ADR-0008). ``CHANNEL_BROWSERS`` is re-exported
# below because the capture implementation consumes it directly and existing
# private importers may still resolve it here. CLI discovery uses the auth facade.
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
    """Caller-injected sink for the neutral browser-capture core's side effects.

    ``emit`` forwards a presentation line (``*args, **kwargs`` pass through
    verbatim, incl. ``markup=False``); ``fail`` aborts the flow according to the
    injected adapter. First-party interactive callers arrive through the auth
    facade's private callback bridge, not through a direct CLI import.

    Note: :func:`run_browser_capture` itself never calls ``run_async`` — only
    post-capture app orchestration drives account repair. ``run_async`` remains
    on this private Protocol for structural compatibility; first-party capture
    bridges provide a loudly failing implementation.
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
    """Get a fresh page from a persistent browser context.

    Used when the current page reference is stale (TargetClosedError); a new
    page in a persistent context inherits all cookies and storage. Returns a
    new ``Page``, or aborts if the context/browser is dead;
    re-raises the original ``PlaywrightError`` for non-TargetClosed failures.
    ``io`` supplies both emit + fail. Headless callers receive a typed abort
    for a dead browser instead of the interactive ``io.fail`` exception.
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
    """Return the lowercased hostnames :func:`url_matches_base_host` accepts.

    Single source of truth for the accept set so the login-wait DEBUG line
    ("waiting for host X") can never drift from the predicate that actually
    ends the wait — the drift that made the ``notebook.google.com`` rebrand
    (#2017 / #2025 and friends) so expensive to triage.

    Selecting *either* personal host accepts *both* of them. Google's login
    flow may land on either one regardless of which we navigated to, so keying
    the accept set on the selected host alone would reject a perfectly good
    landing (and, on the alias, fail every login). Enterprise has no such
    alias, so it accepts only itself.
    """
    base_host = get_base_host().lower()
    if base_host in PERSONAL_APP_HOSTS:
        # Selected host first so the DEBUG line names the one we navigated to;
        # the rest sorted so the message is stable across runs.
        return (base_host, *sorted(PERSONAL_APP_HOSTS - {base_host}))
    return (base_host,)


def url_matches_base_host(url: str) -> bool:
    """Return True when ``url`` is on the configured NotebookLM host or personal-app alias."""
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
# ``notebooklm -vv login`` used to print nothing at all for the whole five-minute
# ``page.wait_for_url`` block below, so a login that never landed (e.g. Google's
# ``notebook.google.com`` rebrand) was indistinguishable from a user who simply
# walked away from the browser. Issues #2017 / #2022 / #2023 / #2025 / #2028 /
# #2030 / #2032 each needed manual triage that a single "navigated to X" line
# would have answered. See #2046.
#
# This section owns the Playwright-event side of that tracing. It holds no state
# and has no CLI / Click / Rich coupling (ADR-0021); it was a separate module
# only to keep this file under the ADR-0008 module-size budget, and its sole
# consumer has always been :func:`run_browser_capture` below.
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
    """Render ``url`` as ``scheme://host[:port]/`` — host only, nothing else.

    **This is deliberately a SECOND URL redactor, not a duplicate of
    ``_auth.extraction._safe_url``. Do not unify them.** ``_safe_url`` is built
    for *error messages about Google endpoints*: it keeps the path for any host
    outside a small Google-OAuth allowlist, on the reasoning that the path tells
    an operator which endpoint failed.

    That trade is wrong here. This formatter renders **arbitrary main-frame
    navigations observed during a live SSO flow**, and a Workspace tenant can
    federate to any identity provider — ``https://idp.example/sso/<assertion>``
    puts a one-time credential straight in the path of a host no allowlist can
    anticipate. `-vv` output is exactly what our issue template asks users to
    paste into public bug reports, so the safe default is to keep nothing but
    the host.

    Nothing is lost: the entire diagnostic this tracing exists to provide is
    *which host the browser is on* — "waiting for notebook.google.com, landed
    on notebooklm.google.com". The path never contributed to that answer.

    Userinfo (``https://TOKEN@host/``) is dropped by rebuilding from
    ``hostname``; query and fragment are dropped by never reading them.
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
    """Record that a tracing step failed, naming the exception TYPE only.

    Deliberately **not** ``exc_info=True``. Playwright exception messages
    routinely embed the offending URL (``net::ERR_ABORTED at https://…?f.sid=…``),
    and a rendered traceback bypasses :func:`trace_url` — the precise,
    structural redaction this section promises — leaving only the package's
    heuristic ``scrub_secrets`` backstop, which has no marker to match on an
    opaque OAuth grant carried in a URL *path*. The exception class is the
    entire diagnostic signal here (``TargetClosedError`` vs ``TypeError``);
    the message adds leak surface and nothing else.
    """
    logger.debug("Login wait: %s (%s)", what, type(exc).__name__)


def safe_page_url(page: Any) -> str:
    """Return ``page.url`` credential-stripped, or a placeholder if unreadable.

    Reading ``url`` off a Playwright page can raise once the page or browser is
    gone. A DEBUG diagnostic must never be the thing that turns a
    browser-closed login into an unhandled traceback, so every failure degrades
    to :data:`_UNREADABLE_URL` instead of propagating.
    """
    try:
        return trace_url(page.url)
    except Exception as exc:
        _log_suppressed("could not read the page URL", exc)
        return _UNREADABLE_URL


def _is_main_frame(frame: Any, main_frame: Any) -> bool:
    """True when ``frame`` is the page's top-level frame.

    Identity against ``page.main_frame`` is the fast path, but it is not the
    only test: if Playwright ever hands the listener a different wrapper object
    for the same underlying frame, an identity-only filter would silently drop
    *every* navigation — turning this diagnostic back into the silence it
    exists to fix. So fall back to the structural definition: only the top
    frame has no parent.
    """
    if main_frame is not None and frame is main_frame:
        return True
    return getattr(frame, "parent_frame", None) is None


@contextmanager
def log_observed_navigations(page: Any) -> Iterator[None]:
    """Log every main-frame navigation observed inside the block, at DEBUG.

    Guarantees that let this sit inside the five-minute login wait:

    * **Inert when DEBUG is off** — the listener is never attached, so no
      Playwright event plumbing runs and the wait is byte-for-byte unchanged.
    * **Never breaks the wait** — the callback swallows every exception, and a
      Playwright build without ``page.on`` degrades to a no-op block.
    * **Never leaks credentials** — URLs go through :func:`trace_url`, which
      keeps the host and drops everything else (path, query, fragment,
      userinfo), any of which can carry auth material mid-SSO — including on a
      third-party identity provider no allowlist could anticipate.

    Args:
        page: The Playwright ``Page`` being waited on. Typed ``Any`` because
            ``playwright`` is an optional (``browser`` extra) dependency this
            module must not import at module scope.
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
    """Read ``page.url`` unredacted for host matching, or ``""`` if unreadable.

    The redacted :func:`safe_page_url` is for *logging*; this is what the accept
    predicate runs on. Both degrade rather than raise — a dead page must not turn
    a browser-closed login into an unhandled traceback.
    """
    try:
        return page.url or ""
    except Exception as exc:
        _log_suppressed("could not read the page URL", exc)
        return ""


def wait_for_login_landing(
    page: Any,
    *,
    timeout_s: float,
    io: BrowserCaptureIO | None = None,
) -> int:
    """Block until ``page`` lands on an accepted login host; return tolerated failures.

    ``page.wait_for_url`` cannot be called once and trusted: Playwright's
    ``expect_navigation`` predicate returns True for *any* event carrying an
    ``error`` ("Any failed navigation results in a rejection", in its own
    words). So one failed main-frame navigation anywhere in Google's sign-in
    chain used to raise out of the five-minute wait, reach the CLI as
    "Unexpected error … report a bug" + exit 2, and discard a sign-in the human
    may have *already completed* — the browser could be sitting on the accepted
    host at the moment we gave up (#2257).

    Nothing about a failed navigation says the login failed; the usual causes
    are routine (a passkey ``ms-cxh://`` handoff, a ``204``, a download, a DNS
    or VPN blip). Each is tolerated and the wait re-arms on the REMAINING
    budget. ``TargetClosed`` and non-navigation errors propagate unchanged.

    Two properties worth keeping in mind when editing:

    * **The deadline alone is not a sufficient bound.** ``wait_for_url`` blocks
      between real navigations, so iterations look page-paced — but a page that
      fails *instantly* rejects with no delay and would spin out the timeout.
      :data:`MAX_TOLERATED_NAVIGATION_FAILURES` consecutive *immediate* failures
      is the real bound; the deadline bounds only the paced case.
    * **A committed error page is tolerated but cannot self-heal.** An abort
      leaves the document intact; ``ERR_NAME_NOT_RESOLVED`` and friends commit a
      Chromium error page. We wait either way, but only the first recovers
      alone — hence the notice.
    """
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    deadline = time.monotonic() + timeout_s
    # Seeded from the caller's value, not a clock read, so the common path hands
    # Playwright the timeout verbatim (``--browser-timeout 420`` must arrive as
    # 420_000, not 419_999.99). Only a re-arm consults the deadline.
    remaining_ms: float = timeout_s * 1000
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
            if url_matches_base_host(_current_url(page)):
                return tolerated
            raise PlaywrightTimeout(f"Timeout {timeout_s * 1000:.0f}ms exceeded.")
        attempt_started = time.monotonic()
        try:
            # The SPA never fires "load"; "commit" resolves as soon as the
            # accepted host is reached (#1697). Cookies are read later.
            page.wait_for_url(
                url_matches_base_host,
                wait_until="commit",
                timeout=remaining_ms,
            )
            return tolerated
        except PlaywrightTimeout:
            # Playwright's timeout is a task racing the ``navigated`` event, so
            # losing that race by a hair is possible: check whether the browser
            # landed anyway before reporting "not detected". Same reasoning as
            # the navigation-failure arm below — the accept predicate, not the
            # exception, decides whether we are done.
            if url_matches_base_host(_current_url(page)):
                return tolerated
            raise
        except PlaywrightError as exc:
            if not is_navigation_failure(exc):
                raise
            # The failed navigation may be a *later* hop than the one that landed
            # us, and the human can arrive between the rejection and the re-arm.
            # The accept predicate, not the exception, decides if we are done.
            if url_matches_base_host(_current_url(page)):
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
# Best-effort in-memory PSIDTS heal for Playwright-captured state, run by both
# capture arms below just before persistence. It was a separate module only to
# keep this file under the ADR-0008 module-size budget; both of its callers have
# always been in this file.
# ---------------------------------------------------------------------------


def heal_captured_state(state: dict[str, Any]) -> tuple[dict[str, Any], ValueError | None]:
    """Try one in-memory PSIDTS heal on captured rows; never discard the capture.

    Google does not always answer the login flow's passive ``goto()``
    navigations with ``Set-Cookie: __Secure-1PSIDTS`` (issue #865), so a
    completed browser sign-in can land a state that carries ``SID`` and the
    secondary binding but no usable PSIDTS. Running the shared rookiepy recovery
    contract here means the first command after ``login`` works instead of
    paying for a cold-start heal. The bridge adapts only the ``httpOnly``
    spelling; the converter preserves an existing Playwright ``sameSite`` and
    newly minted recovery cookies take its safe default.

    **The heal is best-effort and this function must not raise.** Returning the
    error instead lets the caller persist what the browser gave us: those
    cookies are the product of an SSO round-trip the user just completed, and
    the disk-based ``_recover_psidts_inline`` retries the heal on the next
    command. Raising would throw that session away on a withheld rotation or a
    transient network blip — strictly worse than the pre-#2061 behaviour of
    writing the imperfect state, and the same mistake as hardening a loader that
    has no heal behind it (#2082 review).

    Note the shape change on the success path: rows are rebuilt from
    ``_sanitized_auth_entries``, which requires a non-empty string ``value``, so
    an empty-valued row that cleared the domain filter is dropped rather than
    persisted verbatim. Auth cookies always carry a value, and a valueless row
    cannot enter a request jar anyway.

    Returns:
        ``(state, error)``. ``error`` is ``None`` when the captured rows already
        validated or the in-memory heal supplied what was missing; otherwise it
        is the final validation error and ``state`` is the caller's input,
        unchanged and still worth persisting.
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
    * ``interactive=False, headless=True`` - the layer-3 headless re-auth flow:
      an unattended browser harvests a still-live Google session from the
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

    The neutral core shared by the interactive CLI login and the layer-3
    headless re-auth profile-launch path. Imports Playwright lazily
    (``io.fail(1)`` + install hint on ImportError), opens a persistent context
    against ``plan.browser_profile``, retries navigation on transient connection
    errors, waits for login in the interactive arm, classifies the landing in
    the headless arm, pins ``.google.com`` cookies, applies the cookie-domain
    allowlist, and atomically writes ``storage_state.json``.

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
            elif url_matches_base_host(page.url) and navigation_committed:
                # Persistent browser profile already has a valid session. Gated on
                # a committed navigation: after the retry loop breaks on repeated
                # aborts, this URL is the restored tab's, not ours (#2260 review).
                io.emit("[green]Already logged in.[/green]")
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
                        wait_for_login_landing(page, timeout_s=timeout_s, io=io)
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
                    navigation_committed = True
                except PlaywrightError as exc:
                    error_str = str(exc)
                    if TARGET_CLOSED_ERROR in error_str:
                        # Page was destroyed (e.g. user switched accounts) -- get fresh page
                        page = recover_page(context, io, headless=headless)
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
            if not url_matches_base_host(page.url) or not navigation_committed:
                # ``trace_url``, not the raw value: a swallowed cookie-forcing
                # race can leave ``page.url`` on a credential-bearing SSO URL.
                io.emit(
                    f"[red]Unexpected URL after login: {safe_page_url(page)}[/red]\n"
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
            # NOT the writer's pass repeated — do not delete it as redundant
            # (ADR-0033 D3). It runs BEFORE ``heal_captured_state``, so the heal's
            # routing preflight and recovery-jar build see domain-filtered rows,
            # not sibling-product cookies + domain-variant name collisions (#2054).
            playwright_state = context.storage_state()
            filtered_state: dict[str, Any] = filter_storage_state_cookies_by_domain_policy(
                dict(playwright_state), include_domains=include_domains
            )
            filtered_state, heal_error = heal_captured_state(filtered_state)
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
            # Persist unconditionally. A failed heal must never discard the
            # sign-in the user just completed — the cookies are still the best
            # material available, and the disk-based cold-start recovery retries
            # from them on the next command.
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
    """Capture NotebookLM storage state by attaching to a running Chrome over CDP.

    An **alternative credential source** for layer-3 headless re-auth: instead
    of launching a dedicated persistent-context browser against our profile
    dir, attach (``playwright.chromium.connect_over_cdp``) to a Chrome the
    operator is *already* running and pointed us at via an explicit CDP
    endpoint. The motivation is freshness: a user's daily Chrome is
    continuously Google-refreshed, whereas our dedicated profile can go stale in
    the long-idle case — so the live browser is a stronger re-mint source.

    This arm performs the SAME landing classification as the headless launch
    arm (:func:`run_browser_capture` with ``headless=True``): navigate to the
    NotebookLM base URL, and if it does not land on the configured host, raise
    :class:`HeadlessLoginRequiredError` (the typed honest signal the caller
    maps to FAILED) rather than hang. On success it captures
    ``BrowserContext.storage_state()``, applies the same cookie-domain
    allowlist, and atomically persists ``storage_state.json``.

    **EXPLICIT / opt-in only.** ``cdp_url`` is an endpoint the operator
    provides; this never auto-discovers a browser. **LOCAL-UNATTENDED-ONLY** —
    a CDP endpoint is account-equivalent and this is NOT a remote / hosted MCP
    auth path; the local-only host check is enforced upstream in
    ``resolve_cdp_url`` (loopback hosts only). **Never logs a cookie value or
    the endpoint** (only the typed outcome).

    **Lifecycle (CRITICAL):** the attached Chrome belongs to the operator. We
    reuse its EXISTING browser context (which carries the live Google session)
    — never ``new_context`` (a fresh context would be logged out) — and create
    a TEMPORARY page we own for the navigation, closing ONLY that page in
    ``finally`` so the operator's own tabs are never navigated or closed.
    Teardown then only **disconnects** the Playwright client (``browser.close()``
    on a CDP-connected browser severs the connection without killing the user's
    Chrome). If the attached browser exposes no context, we fail loudly rather
    than fabricate one.

    Args:
        plan: Capture plan; ``browser`` / ``browser_profile`` are ignored on
            this arm (we attach to a running browser, not a profile dir), while
            ``storage_path`` / ``include_domains`` are honored identically.
        io: Side-effect sink. The headless caller injects a silent / raising
            sink; ``emit`` lines are dropped and never carry a cookie value.
        cdp_url: The operator-provided CDP endpoint (e.g.
            ``http://127.0.0.1:9222``) of an already-running Chrome started with
            ``--remote-debugging-port``.

    Returns:
        A :class:`CaptureResult` (``page_html`` best-effort, may be ``None``).

    Raises:
        HeadlessLoginRequiredError: the attached browser did not land on the
            NotebookLM host (its Google session cannot reach NotebookLM).
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

            # SAME landing classification as the headless launch arm: if we did
            # not land on the NotebookLM host, the attached browser's Google
            # session cannot reach NotebookLM — fail loudly (raise) rather than
            # capture a logged-out state.
            if not url_matches_base_host(page.url):
                logger.warning(
                    "CDP re-auth: landed off-host after navigation (the attached "
                    "browser's Google session cannot reach NotebookLM); cannot "
                    "re-mint cookies."
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
            playwright_state = context.storage_state()
            filtered_state: dict[str, Any] = filter_storage_state_cookies_by_domain_policy(
                dict(playwright_state), include_domains=include_domains
            )
            filtered_state, heal_error = heal_captured_state(filtered_state)
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
            # Persist unconditionally. A failed heal must never discard the
            # sign-in the user just completed — the cookies are still the best
            # material available, and the disk-based cold-start recovery retries
            # from them on the next command. And as in the launch arm, the
            # writer's own pass under the lock is ADR-0029's entry-path-
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
