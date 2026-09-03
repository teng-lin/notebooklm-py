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
(:func:`notebooklm._browser.browser_capture.filter_storage_state_cookies_by_domain_policy`)
on the captured ``storage_state`` and widens neither credential storage nor
logging (never logs a captured cookie value — only the typed outcome).

**Honest, typed outcomes.** Unlike a sibling project that silently returns
``None``, this layer distinguishes three states and NEVER reports success on
dead tokens:

* :attr:`HeadlessReauthStatus.UNAVAILABLE` — L3 could not even be attempted
  (opt-in off, ``playwright`` not installed, or — on the default profile arm —
  no reusable profile).
* :attr:`HeadlessReauthStatus.FAILED` — L3 ran but the credential source's
  Google session is ALSO dead (the browser landed off the NotebookLM host),
  or the capture otherwise failed.
* :attr:`HeadlessReauthStatus.SUCCESS` — fresh cookies were captured, filtered,
  and atomically persisted to ``storage_state.json``.

**Alternative credential source — CDP attach.** Besides launching the dedicated
profile, L3 can attach to an operator-pointed already-running Chrome over the
Chrome DevTools Protocol (``cdp_url`` /
:data:`NOTEBOOKLM_HEADLESS_REAUTH_CDP_URL_ENV`), via
:func:`notebooklm._browser.browser_capture.run_cdp_capture`. This mitigates the
dedicated-profile-can-stale weakness — the operator's daily Chrome is
continuously Google-refreshed. It is EXPLICIT / opt-in (an endpoint the operator
provides, never auto-discovered), reuses the SAME landing classification and the
SAME cookie-domain allowlist, and stays under the SAME local-unattended-only
boundary (a CDP endpoint is account-equivalent; never a remote / hosted MCP auth
path, never logs a cookie value or the endpoint).

``playwright`` stays lazily imported (only the neutral capture core touches it);
importing this module without the ``browser`` extra never fails.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn, Protocol

from .._auth.recovery_rungs import HeadlessRungOutcome, HeadlessRungStatus
from ..exceptions import HeadlessLoginRequiredError
from ..paths import get_browser_profile_dir
from .browser_capture import (
    BrowserCaptureIO,
    BrowserCapturePlan,
    CaptureResult,
    _CaptureAbortKind,
    _HeadlessCaptureAbort,
    run_browser_capture,
    run_cdp_capture,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable

logger = logging.getLogger("notebooklm.auth")

# Opt-in env var that lets the mid-RPC auth cascade auto-fire L3. Read here and
# in the client wiring. Set to ``"1"`` to allow a *mid-call* headless re-auth;
# unset (or any other value) means L3 only fires via the explicit
# ``client.refresh_auth(allow_headless=True)`` entry point. The locked design
# decision: never auto-fire by default.
NOTEBOOKLM_HEADLESS_REAUTH_ENV = "NOTEBOOKLM_HEADLESS_REAUTH"

# Opt-in env var that points L3 at an ALREADY-RUNNING Chrome via the Chrome
# DevTools Protocol (CDP) instead of launching against the dedicated profile.
# When set to a CDP endpoint (e.g. ``http://127.0.0.1:9222``), the headless
# re-mint attaches to that browser — a freshness mitigation, since the
# operator's daily Chrome is continuously Google-refreshed where our dedicated
# profile can go stale in the long-idle case. The value is an endpoint the
# operator provides (never auto-discovered); LOCAL-UNATTENDED-ONLY, never a
# remote / hosted MCP auth path. An explicit ``cdp_url=`` argument takes
# precedence over this env var.
NOTEBOOKLM_HEADLESS_REAUTH_CDP_URL_ENV = "NOTEBOOKLM_HEADLESS_REAUTH_CDP_URL"


@dataclass
class _DriveRecord:
    """Per-(storage path, credential source) drive-coalescing state for the headless browser drive.

    The in-process coalescing primitive for one ``storage_state.json``. It
    bundles the drive lock that serializes browser drives with the typed
    outcome of the most recent COMPLETED drive, stamped with a monotonic
    drive-sequence number (``completed``). Followers coalesce on that typed
    outcome — never on file mtime — so a sibling write that merely touches the
    storage file can never be mistaken for a successful re-mint.

    **Why this is NOT** :mod:`notebooklm._auth.single_flight`. An earlier
    version of this docstring promised the opposite — that the structure was
    kept self-contained "so a later shared single-flight core can reuse it
    wholesale". That core landed (ADR-0030 c-PR2) and this record did not adopt
    it. The promise was wrong, not merely unfulfilled, and is retired here.

    ``single_flight`` coalesces callers that each have a RUNNING EVENT LOOP:
    ``claim()`` calls ``asyncio.get_running_loop()`` to create the leader
    ``asyncio.Task``. This drive has no loop at its coalescing point and
    structurally cannot get one. :func:`attempt_headless_reauth` is a SYNC
    entry in ``_browser``. Its installed :func:`headless_rung` adapter is the
    only production caller, and :mod:`notebooklm._auth.recovery` deliberately
    invokes that registered rung through ``asyncio.to_thread`` because the
    browser drive blocks. Inside that worker thread ``claim()`` raises ``RuntimeError:
    no running event loop`` (measured, not inferred; pinned by
    ``test_single_flight_is_unreachable_from_the_sync_drive_entry``).

    Neither escape route is free:

    * **Spin a throwaway loop** (``asyncio.run``) inside the sync entry just to
      reach ``claim()``. This coalesces correctly in a spike, but it makes the
      documented sync entry raise ``RuntimeError: asyncio.run() cannot be
      called from a running event loop`` for any caller that invokes it from
      inside a coroutine, and it spends a fresh
      event loop plus a second thread hop per browser drive (loop → to_thread →
      new loop → to_thread → blocking drive) to replace one ``threading.Lock``.
    * **Hoist the claim** into the async ``recovery.try_headless_reauth``. That
      narrows one thing this record exists for and complicates another.
      (1) *Coverage*: every PRODUCTION path already funnels through
      ``recovery.try_headless_reauth``, so a hoisted claim would still coalesce
      all of them. What it drops is the SYNC entry's own coalescing for direct,
      loop-less callers — today that is its whitebox tests
      (``test_concurrent_explicit_attempts_coalesce_to_one_browser``) and any
      future non-``recovery`` caller. The entry is private
      (``notebooklm._browser``, fenced off from the CLI by ``test_cli_boundary``),
      so that residual is small but real. (2) *Keying*: the ``source``
      discriminator (``"profile"`` vs ``"cdp"``) is resolved INSIDE
      :func:`attempt_headless_reauth` by :func:`resolve_cdp_url`, which also
      enforces the LOCAL-UNATTENDED-ONLY loopback boundary — ``recovery`` would
      have to CALL that gate itself to build a correct flight key (it is a pure
      module-level function, so this is a call, not a reimplementation — the
      real residual is that a rejected non-loopback endpoint resolves to
      ``None``, so the rejection warning would move), or
      collapse the two sources into one key (the exact cross-source false-FAILED
      bug the per-``(path, source)`` key fixes; see
      :meth:`HeadlessReauthState.drive_record`).

    For the record, what ``single_flight`` would NOT have broken: the typed
    closed-enum outcome rides a flight future unchanged; nothing jar-bearing is
    retained either way (a :class:`HeadlessReauthResult` carries status, reason
    and a path — no credential); and the [capture-3] stale-outcome guarantee is
    structural there too, since ``claim`` joins only a not-yet-done flight and
    prompt-pops settled ones. The blocker is the event loop, not the contract —
    so this record stays, with its own state lock and its own sequence, as the
    THREAD-level coalescer that ``single_flight``'s task-level model cannot be.

    Attributes:
        drive_lock: Serializes browser drives against this storage file; the
            leader holds it across the whole capture, followers block on it.
        _state_lock: Guards ``_completed`` / ``_last_outcome`` so a follower's
            pre-wait :meth:`snapshot_completed` (taken WITHOUT the drive lock)
            races safely against the leader's :meth:`publish` (under the drive
            lock).
        _completed: Monotonic count of drives that finished and published an
            outcome on this path. Bumped only by :meth:`publish`.
        _last_outcome: The typed result of the most recent completed drive, or
            ``None`` before any drive has completed.
    """

    drive_lock: threading.Lock
    _state_lock: threading.Lock
    _completed: int = 0
    _last_outcome: HeadlessReauthResult | None = None

    def snapshot_completed(self) -> int:
        """Return the completed-drive count for a pre-wait freshness snapshot.

        A follower calls this BEFORE blocking on :attr:`drive_lock`, then
        passes the value to :meth:`fresh_outcome_since` after acquiring the
        lock — accepting an outcome only from a drive that completed during its
        wait.
        """
        with self._state_lock:
            return self._completed

    def publish(self, outcome: HeadlessReauthResult) -> int:
        """Record a completed drive's typed outcome and bump the sequence.

        Called by the leader while holding :attr:`drive_lock`. Returns the new
        (post-bump) completed-drive count.
        """
        with self._state_lock:
            self._completed += 1
            self._last_outcome = outcome
            return self._completed

    def fresh_outcome_since(self, pre_completed: int) -> HeadlessReauthResult | None:
        """Return the leader's outcome only if a drive completed during the wait.

        Given the caller's pre-wait :meth:`snapshot_completed` value, return the
        most recent typed outcome iff the completed-drive count has advanced
        past it (a strictly-newer drive finished while the caller waited).
        Otherwise return ``None`` — a stale outcome from a previous drive cycle
        reads as "no outcome", so the caller drives its own browser.
        """
        with self._state_lock:
            if self._completed > pre_completed and self._last_outcome is not None:
                return self._last_outcome
            return None


# In-process drive coalescer for the (blocking, sync) browser drive.
#
# The mid-RPC cascade already coalesces through
# ``AuthRefreshCoordinator.await_refresh``, but the explicit
# ``client.refresh_auth(allow_headless=True)`` entry point bypasses that
# coordinator, and several clients (or processes-within-a-process) can target
# the same profile. This registry guarantees that, within ONE process, at most
# ONE browser drives a given ``storage_state.json`` at a time: concurrent
# callers serialize on a per-resolved-path drive lock, and a follower coalesces
# onto the LEADER'S TYPED OUTCOME — but ONLY an outcome from a drive that
# completed *during its own wait* — instead of launching a redundant browser.
#
# **Coalescing keys on the leader's outcome, never on file mtime.** An earlier
# design skipped the follower's browser when it observed the storage file's
# mtime advance while it waited. That was a false-SUCCESS hazard: an *unrelated*
# writer (a keepalive / PSIDTS rotation) advancing the same file's mtime made a
# follower report SUCCESS even when the leader's re-mint actually FAILED — the
# user then hit a confusing downstream auth error instead of the honest "Run
# 'notebooklm login'" path. Followers now accept only the leader's *typed*
# :class:`HeadlessReauthResult`, stamped with a monotonic drive-sequence
# number, and only when that sequence advanced during the wait.
#
# **Snapshot-before-wait (the in-process analogue of the #816 rule).** A
# follower snapshots the completed-drive count BEFORE blocking on the drive
# lock and accepts an outcome only from a *strictly-newer* completed drive. A
# stale outcome from a PREVIOUS drive cycle (cookies may have re-died since)
# reads as "no outcome", so the follower drives its own browser. A follower
# that cannot observe a fresh leader outcome (crashed leader, stale record)
# also drives its own — a redundant browser is SAFE, a false SUCCESS is NOT.
#
# :class:`HeadlessReauthState` owns the registry lock that makes get-or-create
# atomic across the worker threads ``asyncio.to_thread`` may use. This is a
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
@dataclass(eq=False)
class HeadlessReauthState:
    """Own the synchronous headless-drive registry for one process or test.

    Production uses :meth:`process_default`; tests construct a fresh instance.
    Keeping the registry and its lock together prevents test isolation from
    depending on rebinding module globals while preserving the thread-level
    coalescer used by the synchronous entry point.
    """

    _record_factory: Callable[[], _DriveRecord] = field(
        default=lambda: _DriveRecord(
            drive_lock=threading.Lock(),
            _state_lock=threading.Lock(),
        ),
        repr=False,
        compare=False,
    )
    _registry_lock: threading.Lock = field(
        default_factory=threading.Lock,
        init=False,
        repr=False,
        compare=False,
    )
    _records_by_key: dict[str, _DriveRecord] = field(
        default_factory=dict,
        init=False,
        repr=False,
        compare=False,
    )
    _active_reservations: int = field(default=0, init=False, repr=False, compare=False)

    @classmethod
    def process_default(cls) -> HeadlessReauthState:
        """Return the process-lifetime state used by production composition."""
        return _PROCESS_DEFAULT_STATE

    def drive_record(self, storage_path: Path, *, source: str) -> _DriveRecord:
        """Return the per-(resolved-storage-path, source) drive record."""
        key = f"{storage_path.expanduser().resolve()}\x00{source}"
        with self._registry_lock:
            return self._drive_record_locked(key)

    def _drive_record_locked(self, key: str) -> _DriveRecord:
        """Get or create ``key`` while the caller holds ``_registry_lock``."""
        record = self._records_by_key.get(key)
        if record is None:
            record = self._record_factory()
            self._records_by_key[key] = record
        return record

    @contextmanager
    def _reserve_drive_record(
        self,
        storage_path: Path,
        *,
        source: str,
    ) -> Iterator[_DriveRecord]:
        """Keep a caller-visible record live until its drive attempt settles.

        The reservation closes the gap between registry lookup and acquiring
        ``drive_lock``: lifecycle reset must reject both active drivers and
        callers that have obtained a record and are about to wait on it.
        """
        key = f"{storage_path.expanduser().resolve()}\x00{source}"
        with self._registry_lock:
            record = self._drive_record_locked(key)
            self._active_reservations += 1
        try:
            yield record
        finally:
            with self._registry_lock:
                self._active_reservations -= 1

    def reset_if_quiescent(self) -> None:
        """Clear settled records, refusing to erase an active browser drive.

        A caller reserves its record before waiting on the drive lock, so reset
        deterministically rejects both active drives and pending followers.
        """
        with self._registry_lock:
            if self._active_reservations or any(
                record.drive_lock.locked() for record in self._records_by_key.values()
            ):
                raise RuntimeError("cannot reset headless re-auth state while a drive is active")
            self._records_by_key.clear()


_PROCESS_DEFAULT_STATE = HeadlessReauthState()


class _ReusableProfileResolver(Protocol):
    def __call__(
        self,
        *,
        browser_profile: Path | None,
        profile: str | None,
    ) -> Path | None: ...


class _CdpResolver(Protocol):
    def __call__(
        self,
        cdp_url: str | None,
        env: dict[str, str] | None,
    ) -> str | None: ...


class _ProfileCapture(Protocol):
    def __call__(
        self,
        plan: BrowserCapturePlan,
        io: BrowserCaptureIO,
        *,
        headless: bool,
        interactive: bool,
    ) -> CaptureResult: ...


class _CdpCapture(Protocol):
    def __call__(
        self,
        plan: BrowserCapturePlan,
        io: BrowserCaptureIO,
        *,
        cdp_url: str,
    ) -> CaptureResult: ...


@dataclass(frozen=True)
class _HeadlessReauthDeps:
    """Narrow collaborators for one headless re-auth operation.

    The record contains gateways and owner state only. It never carries cookie,
    token, captured-state, or endpoint values, so no credential-bearing value
    can outlive operation settlement through this seam.
    """

    playwright_installed: Callable[[], bool]
    resolve_profile: _ReusableProfileResolver
    resolve_cdp: _CdpResolver
    run_profile_capture: _ProfileCapture
    run_cdp_capture: _CdpCapture
    state: HeadlessReauthState


def _production_deps() -> _HeadlessReauthDeps:
    """Compose the real call-time gateways for one production operation."""
    return _HeadlessReauthDeps(
        playwright_installed=_playwright_installed,
        resolve_profile=_resolve_reusable_profile,
        resolve_cdp=resolve_cdp_url,
        run_profile_capture=run_browser_capture,
        run_cdp_capture=run_cdp_capture,
        state=HeadlessReauthState.process_default(),
    )


def headless_reauth_env_enabled(env: dict[str, str] | None = None) -> bool:
    """True when ``NOTEBOOKLM_HEADLESS_REAUTH=1`` opts into mid-RPC L3 auto-fire.

    ``env`` defaults to :data:`os.environ`; injectable for deterministic tests.
    Only the exact value ``"1"`` enables it, mirroring the strict opt-in
    convention used by ``_REFRESH_ATTEMPTED_ENV`` and the keepalive env gates.
    """
    source = os.environ if env is None else env
    return source.get(NOTEBOOKLM_HEADLESS_REAUTH_ENV) == "1"


def _is_loopback_cdp_host(cdp_url: str) -> bool:
    """True only when ``cdp_url``'s host is a loopback address.

    The LOCAL-UNATTENDED-ONLY boundary in code: a CDP endpoint is
    account-equivalent, so the CDP arm must attach ONLY to a browser on the
    operator's own machine. We allow the loopback forms a locally-launched
    ``--remote-debugging-port`` Chrome binds to — ``localhost``, the
    ``127.0.0.0/8`` IPv4 loopback block, and the ``::1`` IPv6 loopback — and
    fail closed on everything else (a remote host, a LAN IP, ``0.0.0.0``, an
    unparseable value). This is what stops
    ``NOTEBOOKLM_HEADLESS_REAUTH_CDP_URL=http://remote-host:9222`` from turning
    L3 into a remote auth path.
    """
    from urllib.parse import urlparse

    # ``connect_over_cdp`` accepts an http(s) endpoint or a ws(s) one; both
    # carry the host in the netloc, so urlparse handles either. A bare
    # ``host:port`` (no scheme) parses with an empty hostname, so prepend a
    # scheme in that case to extract the host.
    try:
        parsed = urlparse(cdp_url if "//" in cdp_url else f"http://{cdp_url}")
        host = (parsed.hostname or "").lower()
    except ValueError:
        return False
    if host in {"localhost", "::1"}:
        return True
    try:
        import ipaddress

        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        # Not an IP literal and not ``localhost`` — e.g. a remote hostname.
        return False


def resolve_cdp_url(
    cdp_url: str | None = None,
    env: dict[str, str] | None = None,
) -> str | None:
    """Resolve the opt-in CDP endpoint, explicit-arg-first then env-var.

    An explicit ``cdp_url`` wins; otherwise read
    :data:`NOTEBOOKLM_HEADLESS_REAUTH_CDP_URL_ENV`. An empty / whitespace-only
    value is treated as unset (``None``) so a blank env var does not enable the
    CDP arm. The endpoint is always operator-provided — never auto-discovered.

    **LOCAL-ONLY enforcement.** A resolved endpoint whose host is NOT loopback
    (:func:`_is_loopback_cdp_host`) is rejected — ``None`` is returned, so the
    CDP arm never fires against a remote / LAN browser. The rejection is logged
    at WARNING WITHOUT the endpoint value (only that a non-local endpoint was
    declined), so the boundary is observable without leaking the operator's
    address. This keeps L3 a local-unattended-only credential path.
    """
    source = os.environ if env is None else env
    candidate = (
        cdp_url if cdp_url is not None else source.get(NOTEBOOKLM_HEADLESS_REAUTH_CDP_URL_ENV)
    )
    if candidate is None:
        return None
    stripped = candidate.strip()
    if not stripped:
        return None
    if not _is_loopback_cdp_host(stripped):
        logger.warning(
            "Ignoring a non-loopback CDP endpoint for headless re-auth: the CDP "
            "arm is local-unattended-only and attaches to a loopback browser "
            "only (the endpoint value is not logged)."
        )
        return None
    return stripped


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
      neutral core routes user-facing aborts through ``io.fail``; infrastructure
      aborts use a private typed marker so they cannot be mistaken for these
      dead-session paths.
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


def _capture_abort_reason(kind: _CaptureAbortKind) -> str:
    """Return a stable, non-sensitive public reason for an infrastructure abort."""
    if kind is _CaptureAbortKind.BROWSER_CLOSED:
        return (
            "headless capture failed: browser was closed during capture; "
            "retry headless re-authentication"
        )
    if kind is _CaptureAbortKind.CONNECTION_EXHAUSTED:
        return (
            "headless capture failed: connection retries were exhausted; "
            "check network connectivity and retry"
        )
    return "headless capture failed: infrastructure aborted the capture"


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
    # An empty or ownership-marker-only dir (created by path prep but never
    # logged into) has no Chrome session state.
    from ..paths import BROWSER_PROFILE_OWNERSHIP_MARKER

    try:
        next(
            entry for entry in candidate.iterdir() if entry.name != BROWSER_PROFILE_OWNERSHIP_MARKER
        )
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
    return _headless_reauth_readiness(
        browser_profile=browser_profile,
        profile=profile,
        resolve_profile=_resolve_reusable_profile,
        playwright_installed=_playwright_installed,
    )


def _headless_reauth_readiness(
    *,
    browser_profile: Path | None,
    profile: str | None,
    resolve_profile: _ReusableProfileResolver,
    playwright_installed: Callable[[], bool],
) -> HeadlessReauthReadiness:
    """Evaluate readiness through explicit, read-only prerequisite gateways."""
    resolved_profile = resolve_profile(browser_profile=browser_profile, profile=profile)
    return HeadlessReauthReadiness(
        profile_present=resolved_profile is not None,
        playwright_installed=playwright_installed(),
    )


def headless_rung(*, storage_path: Path, allow_headless: bool) -> HeadlessRungOutcome:
    """Adapt the browser-specific result to the neutral L3 recovery contract."""
    result = attempt_headless_reauth(
        storage_path=storage_path,
        allow_headless=allow_headless,
        browser_profile=get_browser_profile_dir(storage_path=storage_path),
    )
    status = {
        HeadlessReauthStatus.SUCCESS: HeadlessRungStatus.SUCCEEDED,
        HeadlessReauthStatus.UNAVAILABLE: HeadlessRungStatus.UNAVAILABLE,
        HeadlessReauthStatus.FAILED: HeadlessRungStatus.FAILED,
    }[result.status]
    return HeadlessRungOutcome(status, result.reason)


def attempt_headless_reauth(
    *,
    storage_path: Path,
    allow_headless: bool,
    browser_profile: Path | None = None,
    profile: str | None = None,
    browser: str = "chromium",
    include_domains: set[str] | None = None,
    cdp_url: str | None = None,
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
    2. **Playwright present.** The ``browser`` extra must be importable;
       otherwise ``UNAVAILABLE`` (nothing to drive).
    3. **Credential source.** Two alternative sources, both opt-in:

       * **CDP attach** (when ``cdp_url`` resolves, explicit arg or
         :data:`NOTEBOOKLM_HEADLESS_REAUTH_CDP_URL_ENV`): attach to an
         operator-pointed already-running Chrome
         (:func:`notebooklm._browser.browser_capture.run_cdp_capture`). The
         dedicated profile is NOT required on this path — the live browser is
         the credential source. This is the freshness mitigation for our
         dedicated-profile-can-stale weakness.
       * **Dedicated profile** (default): a persistent profile dir holding a
         (hopefully live) Google session must exist on disk
         (:func:`_resolve_reusable_profile`); no profile → ``UNAVAILABLE``.

    4. **Drive the headless browser**: navigate to the NotebookLM base URL,
       classify the landing — authenticated (lands on NotebookLM) → capture /
       domain-filter / atomically persist; redirected off-host → the source's
       Google session is ALSO dead → :class:`HeadlessLoginRequiredError` →
       ``FAILED`` (loud, never hangs). Both sources reuse the SAME landing
       classification and the SAME cookie-domain allowlist.

    This function performs the *recovery*, not the retry. On ``SUCCESS`` the
    caller re-runs the normal auth path (L1 token refresh) which now finds the
    freshly-persisted cookies. It is a recovery, not the hot path. Browser
    drives are coalesced in two places: mid-RPC callers join
    ``AuthRefreshCoordinator.await_refresh``'s loop-bound single-flight, and
    every caller of THIS function — including the bare-thread ones that have no
    event loop — serializes per ``(storage path, credential source)`` in
    :func:`_drive_capture_coalesced`. That second one is deliberately not
    :mod:`notebooklm._auth.single_flight`; :class:`_DriveRecord` records why.

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
        cdp_url: Optional explicit CDP endpoint of an already-running Chrome to
            attach to instead of launching the dedicated profile. ``None``
            falls back to :data:`NOTEBOOKLM_HEADLESS_REAUTH_CDP_URL_ENV`. When
            resolved, the CDP arm runs and the dedicated profile is not
            required. EXPLICIT / opt-in, LOCAL-UNATTENDED-ONLY.
        env: Environment mapping for the opt-in + CDP-URL checks; defaults to
            :data:`os.environ`.

    Returns:
        A :class:`HeadlessReauthResult` with a distinct status for unavailable
        / failed / success. NEVER ``SUCCESS`` unless cookies were persisted.
    """
    return _attempt_headless_reauth(
        storage_path=storage_path,
        allow_headless=allow_headless,
        browser_profile=browser_profile,
        profile=profile,
        browser=browser,
        include_domains=include_domains,
        cdp_url=cdp_url,
        env=env,
        deps=_production_deps(),
    )


def _attempt_headless_reauth(
    *,
    storage_path: Path,
    allow_headless: bool,
    browser_profile: Path | None,
    profile: str | None,
    browser: str,
    include_domains: set[str] | None,
    cdp_url: str | None,
    env: dict[str, str] | None,
    deps: _HeadlessReauthDeps,
) -> HeadlessReauthResult:
    """Run one attempt through explicit operation collaborators and owner state."""
    if not (allow_headless or headless_reauth_env_enabled(env)):
        return HeadlessReauthResult(
            HeadlessReauthStatus.UNAVAILABLE,
            "headless re-auth not enabled "
            "(pass allow_headless=True or set NOTEBOOKLM_HEADLESS_REAUTH=1)",
        )

    # Lazy-import probe: if the ``browser`` extra is absent, classify as
    # UNAVAILABLE (nothing to drive), distinct from a genuine dead-session
    # FAILED. Checked up front so the two are never conflated, and before the
    # profile/CDP resolution so a missing extra is reported the same way on
    # both arms.
    if not deps.playwright_installed():
        return HeadlessReauthResult(
            HeadlessReauthStatus.UNAVAILABLE,
            "playwright is not installed (install the 'browser' extra to enable headless re-auth)",
        )

    resolved_cdp_url = deps.resolve_cdp(cdp_url, env)
    if resolved_cdp_url is not None:
        # CDP arm: attach to the operator-pointed running Chrome. The dedicated
        # profile is NOT required here — the live browser is the credential
        # source (the freshness mitigation). ``browser_profile`` is irrelevant
        # on this path, so a placeholder ``storage_path.parent`` keeps the
        # frozen plan well-formed without resolving a profile dir.
        plan = BrowserCapturePlan(
            browser=browser,
            browser_profile=storage_path.parent,
            storage_path=storage_path,
            include_domains=include_domains,
        )
        return _drive_capture_coalesced(plan, deps=deps, cdp_url=resolved_cdp_url)

    resolved_profile = deps.resolve_profile(browser_profile=browser_profile, profile=profile)
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

    # Per-(storage path, source) coalescing: within this process, at most one
    # browser drives a given storage file PER CREDENTIAL SOURCE at a time — a
    # profile drive and a CDP drive against the same storage_state.json each
    # hold their own record, deliberately (see
    # HeadlessReauthState.drive_record). A follower
    # coalesces onto the
    # leader's TYPED outcome (only one produced by a drive that completed during
    # its wait) instead of launching a redundant browser. This covers the
    # explicit ``refresh_auth(allow_headless=True)`` entry and multi-client
    # callers, which do NOT pass through the mid-RPC coordinator's single-flight.
    return _drive_capture_coalesced(plan, deps=deps)


def _drive_capture_coalesced(
    plan: BrowserCapturePlan,
    *,
    deps: _HeadlessReauthDeps,
    cdp_url: str | None = None,
) -> HeadlessReauthResult:
    """Drive the headless capture under the per-(path, source) drive lock + outcome coalescing.

    Snapshots the completed-drive count BEFORE blocking on the drive lock
    (:meth:`_DriveRecord.snapshot_completed`). After acquiring the lock, a
    follower coalesces onto the leader's TYPED outcome only if a drive completed
    during its wait (:meth:`_DriveRecord.fresh_outcome_since` returns
    non-``None``) — so N genuine concurrent callers spawn at most ONE browser
    per storage file, and every follower reports exactly what the leader's drive
    produced (SUCCESS *or* FAILED), never a mtime-inferred false SUCCESS.

    A follower that observes no fresh outcome — a crashed leader, or only a
    stale outcome from a previous drive cycle whose cookies may have re-died —
    drives its own browser. Redundant is safe; false SUCCESS is not.

    The two credential sources (dedicated profile and ``cdp_url`` attach) use
    SEPARATE per-(path, source) records (CodeRabbit #4): both re-mint into the
    same ``storage_state.json``, but a follower must coalesce only onto an outcome
    from its OWN source — never accept the other source's FAILED result (a dead
    profile must not fail a live CDP attach, nor vice versa).
    """
    source = "cdp" if cdp_url is not None else "profile"
    with deps.state._reserve_drive_record(plan.storage_path, source=source) as record:
        # Snapshot BEFORE blocking on the drive lock: we accept an outcome only from
        # a drive that finishes DURING this wait (strictly-newer sequence).
        pre_completed = record.snapshot_completed()
        with record.drive_lock:
            fresh = record.fresh_outcome_since(pre_completed)
            if fresh is not None:
                # A sibling leader's drive completed while we waited; coalesce onto
                # its TYPED outcome (its status, whether SUCCESS or FAILED), not an
                # mtime heuristic. Skips a redundant browser without ever inferring
                # success from an unrelated file write.
                logger.info(
                    "Layer-3 headless re-auth coalesced onto a concurrent drive's "
                    "outcome (%s); skipping a redundant browser.",
                    fresh.status.value,
                )
                return fresh
            result = _drive_capture(plan, deps=deps, cdp_url=cdp_url)
            record.publish(result)
            return result


def _drive_capture(
    plan: BrowserCapturePlan,
    *,
    deps: _HeadlessReauthDeps,
    cdp_url: str | None = None,
) -> HeadlessReauthResult:
    """Run one headless capture (profile-launch or CDP-attach) → typed outcome.

    When ``cdp_url`` is set, attach to the operator's running Chrome via
    :func:`notebooklm._browser.browser_capture.run_cdp_capture`; otherwise launch
    the dedicated persistent profile via ``run_browser_capture``. Both arms map
    a clean run to SUCCESS, an off-host landing
    (:class:`HeadlessLoginRequiredError`) to FAILED, and any other capture
    failure to FAILED — never masking a dead session as success, and never
    logging a cookie value.
    """
    io = _SilentRaisingCaptureIO()
    if cdp_url is not None:
        logger.info(
            "Attempting layer-3 re-auth by attaching to a running Chrome over CDP "
            "(opt-in honored); no cookie values or endpoints are logged."
        )
        source_dead_reason = "the attached browser's Google session cannot reach NotebookLM"
        success_reason = "re-minted NotebookLM cookies from the attached running browser"
    else:
        logger.info(
            "Attempting layer-3 headless re-auth against persisted browser profile "
            "(opt-in honored); no cookie values are logged."
        )
        source_dead_reason = "the persisted browser profile's Google session is also expired"
        success_reason = "re-minted NotebookLM cookies from the live browser-profile session"

    try:
        if cdp_url is not None:
            deps.run_cdp_capture(plan, io, cdp_url=cdp_url)
        else:
            deps.run_profile_capture(plan, io, headless=True, interactive=False)
    except HeadlessLoginRequiredError as exc:
        # The source's Google session cannot reach NotebookLM (redirected) or
        # the core aborted via io.fail. Honest FAILED — never masked as success.
        logger.warning("Layer-3 re-auth failed: %s", exc)
        return HeadlessReauthResult(HeadlessReauthStatus.FAILED, source_dead_reason)
    except _HeadlessCaptureAbort as exc:
        # Only the private, stable category crosses into the public result.
        # Never expose the marker's text or a Playwright exception/endpoint.
        logger.warning("Layer-3 re-auth aborted: %s", exc.kind.value)
        return HeadlessReauthResult(
            HeadlessReauthStatus.FAILED,
            _capture_abort_reason(exc.kind),
        )
    except Exception as exc:  # noqa: BLE001 - recovery is best-effort
        # Any other capture failure (launch/attach error, navigation failure,
        # filesystem error) is a non-fatal recovery failure: surface FAILED so
        # the caller falls back to the terminal message rather than crashing.
        # The message is the exception *type* only; no cookie value or endpoint
        # reaches here.
        logger.warning("Layer-3 re-auth errored: %s", type(exc).__name__)
        return HeadlessReauthResult(
            HeadlessReauthStatus.FAILED,
            f"headless capture failed: {type(exc).__name__}",
        )

    logger.info("Layer-3 re-auth succeeded; re-minted cookies persisted.")
    return HeadlessReauthResult(
        HeadlessReauthStatus.SUCCESS,
        success_reason,
        storage_path=plan.storage_path,
    )


__all__ = [
    "NOTEBOOKLM_HEADLESS_REAUTH_CDP_URL_ENV",
    "NOTEBOOKLM_HEADLESS_REAUTH_ENV",
    "HeadlessReauthReadiness",
    "HeadlessReauthResult",
    "HeadlessReauthState",
    "HeadlessReauthStatus",
    "attempt_headless_reauth",
    "headless_reauth_env_enabled",
    "headless_reauth_readiness",
    "resolve_cdp_url",
]
