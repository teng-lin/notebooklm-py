"""Layer-3 headless re-auth: silently re-mint dead NotebookLM cookies.

This is the **deepest** auth-recovery layer. When NotebookLM's first-party
cookies (``SID`` / ``__Secure-1PSIDTS`` / …) are fully dead — the homepage GET
302s to ``accounts.google.com`` and even L1 token refresh and L2 PSIDTS
rotation cannot help — the user's persistent *browser profile* may still hold a
live Google SSO session (it outlives ``storage_state.json``). L3 drives a
**headless** browser against that profile to re-mint NotebookLM cookies without
a human, then lets the normal auth path retry.

Recovery layering (deepest last):

* **L1** — token / CSRF refresh (homepage GET re-extracts ``SNlM0e`` /
  ``FdrFJe``; :func:`notebooklm._auth.session.refresh_auth_session`).
* **L2** — ``__Secure-1PSIDTS`` rotation via the ``RotateCookies`` POST
  (:mod:`notebooklm._auth.keepalive` / :mod:`notebooklm._auth.psidts_recovery`).
* **L3** — headless browser re-auth (this module). Fired only after L1/L2
  cannot help, and only when explicitly allowed.

**Locked design decision (inherited from the P1 browser-capture core).**
Headless re-auth is EXPLICIT by default via
``client.refresh_auth(allow_headless=True)``; a mid-RPC auto-fire happens
ONLY when ``NOTEBOOKLM_HEADLESS_REAUTH=1`` is set in the environment. L3 never
auto-fires by default. With no opt-in and no profile the behavior is
byte-identical to the pre-L3 terminal "Run 'notebooklm login'" path.

**SECURITY — local-unattended-only.** The persistent browser profile is an
**account-equivalent credential**: a live Google session, longer-lived than
``storage_state.json``. L3 must NOT become the auth story for a remote / hosted
MCP server — it is for a local, unattended agent/worker on the operator's own
machine. It reuses the existing cookie-domain allowlist
(:func:`notebooklm._auth.browser_capture.filter_storage_state_cookies_by_domain_policy`)
on the captured ``storage_state`` and widens neither credential storage nor
logging (never logs a captured cookie value — only the typed outcome).

**Honest, typed outcomes.** Unlike a sibling project that silently returns
``None``, this layer distinguishes three states and NEVER reports success on
dead tokens:

* :attr:`HeadlessReauthStatus.UNAVAILABLE` — L3 could not even be attempted
  (opt-in off, no reusable profile, or ``playwright`` not installed).
* :attr:`HeadlessReauthStatus.FAILED` — L3 ran but the profile's Google
  session is ALSO dead (the headless browser was redirected to the Google
  login page), or the capture otherwise failed.
* :attr:`HeadlessReauthStatus.SUCCESS` — fresh cookies were captured, filtered,
  and atomically persisted to ``storage_state.json``.

``playwright`` stays lazily imported (only the neutral capture core touches it);
importing this module without the ``browser`` extra never fails.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

from ..exceptions import HeadlessLoginRequiredError
from .browser_capture import BrowserCapturePlan, run_browser_capture

if TYPE_CHECKING:
    from collections.abc import Awaitable

logger = logging.getLogger("notebooklm.auth")

# Opt-in env var that lets the mid-RPC auth cascade auto-fire L3. Read here and
# in the client wiring. Set to ``"1"`` to allow a *mid-call* headless re-auth;
# unset (or any other value) means L3 only fires via the explicit
# ``client.refresh_auth(allow_headless=True)`` entry point. The locked design
# decision: never auto-fire by default.
NOTEBOOKLM_HEADLESS_REAUTH_ENV = "NOTEBOOKLM_HEADLESS_REAUTH"

# Per-storage-path single-flight for the (blocking, sync) browser drive.
#
# The mid-RPC cascade already coalesces through
# ``AuthRefreshCoordinator.await_refresh``, but the explicit
# ``client.refresh_auth(allow_headless=True)`` entry point bypasses that
# coordinator, and several clients (or processes-within-a-process) can target
# the same profile. This registry guarantees that, within ONE process, at most
# ONE browser drives a given ``storage_state.json`` at a time: concurrent
# callers serialize on a per-resolved-path ``threading.Lock``, and a follower
# that sees the storage file freshly rewritten by the leader while it waited
# SKIPS its own browser (coalesces) instead of launching a redundant one.
#
# ``_DRIVE_REGISTRY_LOCK`` makes the get-or-create of a per-path lock atomic
# across the worker threads ``asyncio.to_thread`` may use. This is a
# best-effort, single-process guard; cross-process coordination (two CLI
# invocations) is out of scope here — they each own their own browser, the same
# way the interactive ``notebooklm login`` flow does.
#
# Lifetime: this registry is keyed on the resolved storage path and is never
# pruned, but it is bounded by the number of DISTINCT storage paths a process
# ever re-auths against — i.e. the profile count, typically one. The same
# never-pruned-but-profile-bounded shape as ``_REFRESH_GENERATIONS`` /
# ``_LAST_POKE_ATTEMPT_MONOTONIC`` elsewhere in the auth layer; a long-running
# process does not accumulate entries from RPC traffic, only from new profiles.
_DRIVE_REGISTRY_LOCK = threading.Lock()
_DRIVE_LOCKS_BY_PATH: dict[str, threading.Lock] = {}


def headless_reauth_env_enabled(env: dict[str, str] | None = None) -> bool:
    """True when ``NOTEBOOKLM_HEADLESS_REAUTH=1`` opts into mid-RPC L3 auto-fire.

    ``env`` defaults to :data:`os.environ`; injectable for deterministic tests.
    Only the exact value ``"1"`` enables it, mirroring the strict opt-in
    convention used by ``_REFRESH_ATTEMPTED_ENV`` and the keepalive env gates.
    """
    source = os.environ if env is None else env
    return source.get(NOTEBOOKLM_HEADLESS_REAUTH_ENV) == "1"


class HeadlessReauthStatus(Enum):
    """Terminal classification of one L3 headless re-auth attempt.

    Three mutually-exclusive states keep failure honest (a sibling project
    collapsed all of these into a silent ``None``):

    * ``UNAVAILABLE`` — the attempt was declined before driving a browser
      (opt-in off, no reusable profile, or ``playwright`` missing). The caller
      should fall through to the existing terminal "Run 'notebooklm login'"
      message unchanged — L3 simply does not apply here.
    * ``FAILED`` — a browser ran but re-auth did not succeed: most importantly
      the profile's Google session is ALSO dead (headless landed on the Google
      login page), or the capture/persist failed. NEVER reported on dead
      tokens being healed; this means the operator genuinely must re-login.
    * ``SUCCESS`` — fresh cookies were captured, domain-filtered, and persisted.
    """

    UNAVAILABLE = "unavailable"
    FAILED = "failed"
    SUCCESS = "success"


@dataclass(frozen=True)
class HeadlessReauthResult:
    """Typed outcome of :func:`attempt_headless_reauth`.

    Attributes:
        status: The terminal :class:`HeadlessReauthStatus`.
        reason: Short, human-readable, credential-free explanation (safe to
            log / surface). Never contains a cookie value or token.
        storage_path: The ``storage_state.json`` that was (re)written on
            ``SUCCESS``; ``None`` otherwise.
    """

    status: HeadlessReauthStatus
    reason: str
    storage_path: Path | None = None

    @property
    def succeeded(self) -> bool:
        """``True`` only for :attr:`HeadlessReauthStatus.SUCCESS`."""
        return self.status is HeadlessReauthStatus.SUCCESS


class _SilentRaisingCaptureIO:
    """A ``BrowserCaptureIO`` sink for the unattended headless arm.

    There is NO human in the L3 path, so the interactive niceties are inverted
    to silent / raising behavior:

    * ``emit`` swallows presentation lines (the neutral core emits a few
      "Already logged in." style lines meant for an interactive console; under
      headless re-auth they are noise). Lines are dropped, never logged, so a
      future core change that put a credential in an ``emit`` line could not
      leak through this sink.
    * ``fail`` raises :class:`HeadlessLoginRequiredError` instead of exiting the
      process — an unattended library call must never call ``sys.exit``. The
      neutral core routes user-facing aborts through ``io.fail``; mapping them
      to this exception lets :func:`attempt_headless_reauth` classify them as
      :attr:`HeadlessReauthStatus.FAILED` rather than hanging or exiting.
    * ``run_async`` is never reached on the capture path (the core only calls it
      from the interactive adapter's account-metadata repair, which the
      headless arm does not run); it raises if somehow invoked so a contract
      drift is loud rather than silent.
    """

    def emit(self, *args: Any, **kwargs: Any) -> None:
        # Intentionally silent: no human console, and never log emit content.
        return None

    def fail(self, code: int) -> NoReturn:
        raise HeadlessLoginRequiredError(
            "Headless re-auth aborted by the capture core "
            f"(exit code {code}); the persisted browser profile's Google "
            "session is likely dead. Run 'notebooklm login' to re-authenticate."
        )

    def run_async(self, coro: Awaitable[Any]) -> Any:
        # Not reached on the headless capture path (no account-metadata repair).
        raise RuntimeError(
            "run_async is not supported on the headless re-auth IO sink "
            "(the headless arm performs no account-metadata repair)."
        )


def _resolve_reusable_profile(
    *,
    browser_profile: Path | None,
    profile: str | None,
) -> Path | None:
    """Resolve the persistent browser-profile dir, or ``None`` if not reusable.

    The whole L3 layer rests on a reusable profile whose Google session
    outlives the NotebookLM cookies. The profile is the persistent-context dir
    ``notebooklm login`` launches against
    (:func:`notebooklm.paths.get_browser_profile_dir`), so we resolve the same
    path and require it to already exist and be a directory — a missing /
    empty profile means there is no Google session to harvest and L3 declines.

    Args:
        browser_profile: Explicit profile dir (e.g. from a ``--storage`` flow);
            takes precedence when supplied.
        profile: Profile name to resolve via ``get_browser_profile_dir`` when
            no explicit dir is given.

    Returns:
        The existing profile directory, or ``None`` when no reusable profile is
        present on disk.
    """
    if browser_profile is not None:
        candidate = browser_profile
    else:
        # Imported lazily to avoid a module-load edge with paths/config.
        from ..paths import get_browser_profile_dir

        candidate = get_browser_profile_dir(profile)

    # A persistent context needs a populated profile dir to hold a live Google
    # session. An absent or non-directory path has no session to harvest.
    if not candidate.is_dir():
        return None
    # An empty dir (e.g. created by a path-prep step but never logged into) has
    # no Chrome session state. ``next(iterdir)`` is cheap and avoids treating a
    # freshly-mkdir'd profile as reusable.
    try:
        next(candidate.iterdir())
    except StopIteration:
        return None
    except OSError as exc:  # unreadable dir — treat as no reusable profile
        logger.debug("Headless re-auth: profile dir %s not readable: %s", candidate, exc)
        return None
    return candidate


def _playwright_installed() -> bool:
    """True when the ``browser`` extra (``playwright.sync_api``) can be imported.

    Mirrors the lazy-import probe :func:`attempt_headless_reauth` runs before
    driving a browser, so readiness and the real attempt agree on availability.
    Kept function-local so importing this module never needs ``playwright``.
    """
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return False
    return True


@dataclass(frozen=True)
class HeadlessReauthReadiness:
    """Credential-free readiness snapshot for the L3 headless re-auth fallback.

    Surfaced by ``doctor`` so an operator can tell — *before* a dead-cookie
    wall is hit — whether the unattended L3 recovery could even run. It carries
    NO cookie/token/session content: only two booleans plus a derived,
    human-readable ``detail``.

    Attributes:
        profile_present: A reusable persistent browser profile dir exists on
            disk for the active storage path (the prerequisite L3 harvests a
            live Google SSO session from). Resolved by the same
            :func:`_resolve_reusable_profile` the real attempt uses.
        playwright_installed: The ``browser`` extra is importable, so a headless
            browser can actually be driven.
    """

    profile_present: bool
    playwright_installed: bool

    @property
    def available(self) -> bool:
        """True only when BOTH prerequisites for an L3 attempt are in place.

        Never asserts the profile's Google session is live — that can only be
        known by driving the browser, which the readiness probe does not do —
        and does not consider the opt-in, which is a call-time decision.
        """
        return self.profile_present and self.playwright_installed

    @property
    def detail(self) -> str:
        """Short, credential-free, actionable one-liner for the doctor row."""
        if self.available:
            return (
                "ready (persistent profile + playwright present; opt-in via "
                "NOTEBOOKLM_HEADLESS_REAUTH=1 or refresh_auth(allow_headless=True))"
            )
        missing: list[str] = []
        if not self.profile_present:
            missing.append("no reusable browser profile (run 'notebooklm login' once)")
        if not self.playwright_installed:
            missing.append("playwright not installed (add the 'browser' extra)")
        return "unavailable: " + "; ".join(missing)


def headless_reauth_readiness(
    *,
    browser_profile: Path | None = None,
    profile: str | None = None,
) -> HeadlessReauthReadiness:
    """Probe whether L3 headless re-auth *could* run, without driving a browser.

    A read-only, credential-free diagnostic for ``doctor``: it resolves the
    persistent browser-profile dir (the same way :func:`attempt_headless_reauth`
    does) and checks the lazy ``playwright`` import — but launches NOTHING and
    reads no cookie/session content. It deliberately does NOT assert the
    profile's Google session is live (only driving the browser can know that),
    nor does it consider the opt-in, which is a call-time decision.

    Args:
        browser_profile: Explicit persistent-profile dir; defaults to the
            profile's ``get_browser_profile_dir`` when ``None`` (same resolution
            as the real attempt).
        profile: Profile name used to resolve the browser-profile dir.

    Returns:
        A :class:`HeadlessReauthReadiness` with the two prerequisite booleans
        and a derived ``available`` / ``detail``.
    """
    resolved_profile = _resolve_reusable_profile(browser_profile=browser_profile, profile=profile)
    return HeadlessReauthReadiness(
        profile_present=resolved_profile is not None,
        playwright_installed=_playwright_installed(),
    )


def attempt_headless_reauth(
    *,
    storage_path: Path,
    allow_headless: bool,
    browser_profile: Path | None = None,
    profile: str | None = None,
    browser: str = "chromium",
    include_domains: set[str] | None = None,
    env: dict[str, str] | None = None,
) -> HeadlessReauthResult:
    """Attempt one layer-3 headless re-auth; return a typed, honest outcome.

    Decision logic (the gate the locked design decision pins):

    1. **Opt-in.** Proceed only when ``allow_headless`` is ``True`` OR
       ``NOTEBOOKLM_HEADLESS_REAUTH=1`` is set. Otherwise return ``UNAVAILABLE``
       — L3 NEVER fires by default. (Callers pass ``allow_headless=True`` for
       the explicit ``client.refresh_auth(allow_headless=True)`` entry, and
       gate the mid-RPC auto-fire on the env var via
       :func:`headless_reauth_env_enabled`.)
    2. **Reusable profile.** A persistent profile dir holding a (hopefully
       live) Google session must exist on disk (:func:`_resolve_reusable_profile`).
       No profile → ``UNAVAILABLE`` (fall through to the terminal message).
    3. **Drive the headless browser** through the neutral capture core
       (``headless=True, interactive=False``): launch the persistent context,
       navigate to the NotebookLM base URL, classify the landing — authenticated
       (lands on NotebookLM) → capture / domain-filter / atomically persist;
       redirected to the Google login page → the profile's session is ALSO dead
       → :class:`HeadlessLoginRequiredError` → ``FAILED`` (loud, never hangs).
    4. **Playwright missing** → ``UNAVAILABLE`` (the ``browser`` extra is not
       installed; nothing to drive).

    This function performs the *recovery*, not the retry. On ``SUCCESS`` the
    caller re-runs the normal auth path (L1 token refresh) which now finds the
    freshly-persisted cookies. It is a recovery, not the hot path, and the
    actual browser drive is single-flight-coalesced by the caller (the client
    wiring routes concurrent failing calls through the existing refresh
    single-flight so N failures spawn at most one browser).

    Args:
        storage_path: ``storage_state.json`` to (re)write on success.
        allow_headless: Explicit opt-in (the ``refresh_auth(allow_headless=)``
            value). When ``False``, only the env var can enable L3.
        browser_profile: Explicit persistent-profile dir; defaults to the
            profile's ``get_browser_profile_dir`` when ``None``.
        profile: Profile name used to resolve the browser-profile dir.
        browser: Playwright channel (``"chromium"`` / ``"chrome"`` / ``"msedge"``).
        include_domains: Optional cookie-domain opt-in labels, forwarded to the
            same allowlist filter the interactive login uses.
        env: Environment mapping for the opt-in check; defaults to
            :data:`os.environ`.

    Returns:
        A :class:`HeadlessReauthResult` with a distinct status for unavailable
        / failed / success. NEVER ``SUCCESS`` unless cookies were persisted.
    """
    if not (allow_headless or headless_reauth_env_enabled(env)):
        return HeadlessReauthResult(
            HeadlessReauthStatus.UNAVAILABLE,
            "headless re-auth not enabled "
            "(pass allow_headless=True or set NOTEBOOKLM_HEADLESS_REAUTH=1)",
        )

    resolved_profile = _resolve_reusable_profile(browser_profile=browser_profile, profile=profile)
    if resolved_profile is None:
        return HeadlessReauthResult(
            HeadlessReauthStatus.UNAVAILABLE,
            "no reusable browser profile on disk (run 'notebooklm login' once "
            "to create a persistent Google session)",
        )

    plan = BrowserCapturePlan(
        browser=browser,
        browser_profile=resolved_profile,
        storage_path=storage_path,
        include_domains=include_domains,
    )

    # Lazy-import probe: if the ``browser`` extra is absent, the neutral core's
    # ``ensure_playwright_available`` routes through ``io.fail`` →
    # ``HeadlessLoginRequiredError``. We want that case classified as
    # UNAVAILABLE (nothing to drive), distinct from a genuine dead-session
    # FAILED. Check import availability up front so the two are not conflated.
    if not _playwright_installed():
        return HeadlessReauthResult(
            HeadlessReauthStatus.UNAVAILABLE,
            "playwright is not installed (install the 'browser' extra to enable headless re-auth)",
        )

    # Per-storage-path single-flight: within this process, at most one browser
    # drives a given storage file at a time, and a follower that finds the file
    # freshly rewritten while it waited coalesces (skips its own browser). This
    # covers the explicit ``refresh_auth(allow_headless=True)`` entry and
    # multi-client callers, which do NOT pass through the mid-RPC coordinator's
    # single-flight.
    return _drive_capture_coalesced(plan)


def _get_drive_lock(storage_path: Path) -> threading.Lock:
    """Return the per-resolved-storage-path single-flight lock for the browser drive."""
    key = str(storage_path.expanduser().resolve())
    with _DRIVE_REGISTRY_LOCK:
        lock = _DRIVE_LOCKS_BY_PATH.get(key)
        if lock is None:
            lock = threading.Lock()
            _DRIVE_LOCKS_BY_PATH[key] = lock
        return lock


def _storage_mtime(storage_path: Path) -> float | None:
    """Best-effort mtime of the storage file, or ``None`` when absent/unreadable."""
    try:
        return storage_path.stat().st_mtime
    except OSError:
        return None


def _drive_capture_coalesced(plan: BrowserCapturePlan) -> HeadlessReauthResult:
    """Drive the headless capture under the per-path single-flight + freshness skip.

    Captures the storage mtime BEFORE acquiring the lock. After acquiring it, a
    follower whose storage file was rewritten by the leader while it waited
    (mtime advanced) skips its own browser and reports SUCCESS — so N concurrent
    callers spawn at most ONE browser per profile. The leader (and any follower
    whose wait did not yield a fresh file) drives the real capture.
    """
    storage_path = plan.storage_path
    pre_mtime = _storage_mtime(storage_path)
    lock = _get_drive_lock(storage_path)
    with lock:
        post_mtime = _storage_mtime(storage_path)
        if post_mtime is not None and (pre_mtime is None or post_mtime > pre_mtime):
            # A sibling leader re-minted while we waited; coalesce on its result.
            logger.info(
                "Layer-3 headless re-auth coalesced onto a concurrent re-mint "
                "(storage already refreshed); skipping a redundant browser."
            )
            return HeadlessReauthResult(
                HeadlessReauthStatus.SUCCESS,
                "coalesced onto a concurrent headless re-mint",
                storage_path=storage_path,
            )
        return _drive_capture(plan)


def _drive_capture(plan: BrowserCapturePlan) -> HeadlessReauthResult:
    """Run one headless ``run_browser_capture`` and map it to a typed outcome."""
    io = _SilentRaisingCaptureIO()
    logger.info(
        "Attempting layer-3 headless re-auth against persisted browser profile "
        "(opt-in honored); no cookie values are logged."
    )
    try:
        run_browser_capture(plan, io, headless=True, interactive=False)
    except HeadlessLoginRequiredError as exc:
        # The profile's Google session is also dead (redirected to login) or
        # the core aborted via io.fail. Honest FAILED — never masked as success.
        logger.warning("Layer-3 headless re-auth failed: %s", exc)
        return HeadlessReauthResult(
            HeadlessReauthStatus.FAILED,
            "the persisted browser profile's Google session is also expired",
        )
    except Exception as exc:  # noqa: BLE001 - recovery is best-effort
        # Any other capture failure (launch error, navigation failure,
        # filesystem error) is a non-fatal recovery failure: surface FAILED so
        # the caller falls back to the terminal message rather than crashing.
        # The message is the exception *type/string* (already redacted by the
        # exceptions layer); no cookie value reaches here.
        logger.warning("Layer-3 headless re-auth errored: %s", exc)
        return HeadlessReauthResult(
            HeadlessReauthStatus.FAILED,
            f"headless capture failed: {type(exc).__name__}",
        )

    logger.info("Layer-3 headless re-auth succeeded; re-minted cookies persisted.")
    return HeadlessReauthResult(
        HeadlessReauthStatus.SUCCESS,
        "re-minted NotebookLM cookies from the live browser-profile session",
        storage_path=plan.storage_path,
    )


__all__ = [
    "NOTEBOOKLM_HEADLESS_REAUTH_ENV",
    "HeadlessReauthReadiness",
    "HeadlessReauthResult",
    "HeadlessReauthStatus",
    "attempt_headless_reauth",
    "headless_reauth_env_enabled",
    "headless_reauth_readiness",
]
