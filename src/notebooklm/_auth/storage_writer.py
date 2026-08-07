"""Canonical writer for ``storage_state.json`` (and sibling credential files).

This module is the **single sanctioned home** for mutations of
``storage_state.json``. It is the only module under :mod:`notebooklm._auth`
permitted to import the ``_atomic_io`` write primitives
(:func:`atomic_write_json` / :func:`replace_file_atomically`) and to perform the
final atomic write of a storage-state file. The boundary is enforced by
``tests/_guardrails/test_storage_writer_boundary.py`` (an AST guardrail plus an
equality-asserted allowlist of every module that imports ``atomic_write_json``).

Intent-shaped API (all synchronous, all serialize on the canonical storage lock,
all write via ``_atomic_io``):

* :func:`merge_cookie_delta` — the CAS delta merge relocated verbatim from
  ``storage.save_cookies_to_storage`` (kept as the monkeypatchable delegate
  seam in :mod:`notebooklm._auth.storage`). It is a **CAS** intent and
  therefore **fails open** on lock unavailability (status quo): availability
  wins, and the snapshot/delta CAS guards keep correctness.
* :func:`update_account_metadata` / :func:`clear_in_band_account` — the in-band
  account writers relocated from :mod:`notebooklm._auth.account`. These are
  **full-file RMW** intents: :func:`update_account_metadata` **fails closed**
  (raises :class:`LockUnavailableError`) because failing open could overwrite a
  concurrent CAS delta; :func:`clear_in_band_account` is best-effort cleanup and
  swallows lock unavailability, matching the pre-refactor semantics.
* :func:`replace_from_remint` — the full cookie-replace re-mint persister for the
  BROWSER-CAPTURE arms (L3 headless-launch + interactive + CDP), relocated from
  the bare ``atomic_write_json`` sites in :mod:`notebooklm._auth.browser_capture`.
  Applies the write-time domain filter internally under the lock, then either
  carries the existing ``notebooklm`` account namespace (``carry_account=True`` —
  the unattended profile-launch arm, closing [capture-1]) or drops the stale
  binding (``carry_account=False`` — the interactive arm, whose CLI adapter
  re-establishes it). **Fails closed** (returns
  :class:`WriteOutcome` with ``lock_unavailable``). Closes [capture-2].
* :func:`persist_minted_jar` — the master-token L4 re-mint persister relocated
  from :mod:`notebooklm._auth.master_token`, routed through ``_atomic_io`` (so it
  gains fsync durability + temp cleanup) while keeping its storage lock and its
  rebind-to-minted-account semantics. b-PR2 adds the write-time domain filter
  here (the L4 unfiltered-persist gap). **Fails closed.**
* :func:`write_master_token` — the ``master_token.json`` writer, now routed
  through ``_atomic_io`` **and** guarded by a bounded sibling lock (it was
  previously lockless). **Fails closed.**

Lock unification (see ADR-0029): the full-file RMW / re-mint intents drop
``filelock`` in favour of the project-internal ``storage._file_lock`` primitive
via a **platform-neutral bounded acquire** (:func:`_acquire_storage_lock`):
a non-blocking probe plus deadline/jitter retry (default 90 s), then the
per-intent failure policy above. The CAS merge keeps the status-quo blocking
``_file_lock_exclusive`` acquire (fail-open). An in-process ``threading.Lock``
keyed per canonical lock-path (ordering: in-process lock -> OS lock) is added in
``storage._file_lock`` itself so threads within one process serialize before the
OS lock; the distinct ``.{name}.rotate.lock`` sentinel is never collapsed into
the storage lock.

The fail-closed writers raise :class:`~notebooklm.exceptions.LockUnavailableError`
(public via ``notebooklm.exceptions`` / the ``notebooklm.auth`` facade). It
subclasses :class:`TimeoutError` — itself an :class:`OSError` — exactly mirroring
the ``filelock.Timeout`` MRO it replaces, so callers' existing
``except OSError`` / ``except TimeoutError`` arms (``_auth/recovery.py`` around
``persist_minted_jar``; the CLI login writers around ``write_account_metadata``)
keep catching a lock failure unchanged; only the exception type and the 10 s→90 s
bound differ.

Permission contract (POSIX): every writer ensures the parent directory is
``0700`` on creation and the file is ``0600`` (the latter via
:func:`atomic_write_json`'s default mode). On Windows we rely on
``%USERPROFILE%`` ACL inheritance.

Outcome types are **value-free by contract**: :class:`WriteOutcome` may carry
only an enum status — never cookie values, state dicts, jar objects, or caught
exceptions.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

# The canonical storage writer is the SOLE sanctioned user of the module-private
# ``_atomic_write_json_unchecked`` bypass: the public ``atomic_write_json`` now
# rejects ``storage_state.json`` paths (#1215-style runtime guard, b-PR3), and
# this module legitimately writes them under the canonical dotted lock. The
# boundary is equality-asserted in ``tests/_guardrails/test_storage_writer_boundary.py``.
from .._atomic_io import _atomic_write_json_unchecked as atomic_write_json

# ``LockUnavailableError`` is the public, canonical home for the fail-closed
# lock-failure exception (``notebooklm.exceptions`` — also re-exported on the
# ``notebooklm.auth`` facade). It subclasses ``TimeoutError`` (an ``OSError``),
# exactly mirroring the ``filelock.Timeout`` MRO it replaces, so existing
# ``except OSError`` arms keep catching a lock failure. Re-exported here for the
# writers that raise it.
from ..exceptions import LockUnavailableError
from .paths import _storage_state_lock_path, resolve_auth_json_env

# Bounded-acquire tuning is defined ONCE next to the ``_file_lock`` primitive in
# ``storage`` and shared here so both bounded paths (the blocking Windows
# ``msvcrt`` retry in ``storage._acquire_os_lock`` and this non-blocking-probe
# helper) honour the same 90 s deadline and jittered backoff. This top-level
# import is cycle-safe: ``storage`` does not import ``storage_writer`` at module
# scope (only lazily, inside function bodies).
from .storage import (
    _LOCK_ACQUIRE_DEADLINE_SECONDS,
    _LOCK_ACQUIRE_INITIAL_DELAY_SECONDS,
)

if TYPE_CHECKING:
    import httpx

    from .storage import CookieSaveResult, CookieSnapshot, RecoveryCookieObservation

__all__ = [
    "CLEAR_ACCOUNT",
    "KEEP_ACCOUNT",
    "AccountRecord",
    "LockUnavailableError",
    "LoginWriteOutcome",
    "LoginWriteStatus",
    "WriteOutcome",
    "WriteStatus",
    "clear_in_band_account",
    "merge_cookie_delta",
    "persist_minted_jar",
    "replace_from_login",
    "replace_from_remint",
    "update_account_metadata",
    "write_master_token",
]

logger = logging.getLogger("notebooklm.auth")

# The unified full-file RMW / re-mint writers replace ``filelock``'s blocking
# 10 s timeout with a platform-neutral bounded acquire (:func:`_acquire_storage_lock`):
# under real contention a caller retries a non-blocking probe with jittered
# exponential backoff up to ``_LOCK_ACQUIRE_DEADLINE_SECONDS`` before applying its
# per-intent failure policy. Those tuning constants live in ``storage`` (imported
# above) so this path and the blocking Windows ``msvcrt`` retry share one source.


class WriteStatus(Enum):
    """Closed-enum status for a full-file / RMW storage write."""

    OK = "ok"
    LOCK_UNAVAILABLE = "lock_unavailable"


@dataclass(frozen=True)
class WriteOutcome:
    """Value-free outcome for full-replace / RMW storage writers.

    Carries only an enum status — never cookie values, jars, state dicts, or
    caught exceptions — so it is always safe to ``repr``/log.
    """

    status: WriteStatus

    @property
    def ok(self) -> bool:
        return self.status is WriteStatus.OK

    @property
    def lock_unavailable(self) -> bool:
        return self.status is WriteStatus.LOCK_UNAVAILABLE


# ---------------------------------------------------------------------------
# Account-metadata sentinel for the login/import full-replace intent
# ---------------------------------------------------------------------------


class _AccountAction(Enum):
    """Sentinel actions for :func:`replace_from_login`'s ``account`` param."""

    KEEP = "keep"
    CLEAR = "clear"


#: Leave the account binding untouched — carry whatever the input state holds
#: (import-cookies has none, so the result carries none). The default.
KEEP_ACCOUNT = _AccountAction.KEEP
#: Drop any stale account binding (the refresh default-account login branch —
#: the user may have re-logged into a different Google account).
CLEAR_ACCOUNT = _AccountAction.CLEAR


@dataclass(frozen=True)
class AccountRecord:
    """An explicit account binding to embed in the ``notebooklm`` namespace.

    ``authuser`` is the internal Google account index; ``email`` is the stable
    routing identity (optional). Passed as ``replace_from_login(account=...)`` to
    embed the binding in the SAME atomic write as the cookies (replacing the
    former separate ``write_account_metadata`` step, which had its own lock and a
    partial-failure window).
    """

    authuser: int
    email: str | None = None


# The ``account`` argument sentinel: KEEP_ACCOUNT | CLEAR_ACCOUNT | AccountRecord.
AccountArg = _AccountAction | AccountRecord


class LoginWriteStatus(Enum):
    """Closed-enum status for a login/import full-replace storage write."""

    OK = "ok"
    LOCK_UNAVAILABLE = "lock_unavailable"
    REQUIRED_COOKIES_DROPPED = "required_cookies_dropped"


@dataclass(frozen=True)
class LoginWriteOutcome:
    """Value-free outcome for :func:`replace_from_login`.

    Carries only an enum status, cookie **names** (keys, never values), and a
    filesystem path — never cookie values, jars, state dicts, or caught
    exceptions — so it is always safe to ``repr``/log.

    * ``missing_required`` — names of ``MINIMUM_REQUIRED_COOKIES`` that the
      write-time domain filter dropped (only set on ``REQUIRED_COOKIES_DROPPED``).
    * ``present_names`` — names surviving the filter, so the CLI can build the
      same ``missing_cookies_hint`` #2086 produced without re-reading disk.
    * ``backup_path`` — path of the ``.bak`` copy taken inside the lock for the
      import flavour (``None`` when no backup was taken).
    """

    status: LoginWriteStatus
    missing_required: tuple[str, ...] = ()
    present_names: tuple[str, ...] = ()
    backup_path: Path | None = None

    @property
    def ok(self) -> bool:
        return self.status is LoginWriteStatus.OK

    @property
    def lock_unavailable(self) -> bool:
        return self.status is LoginWriteStatus.LOCK_UNAVAILABLE

    @property
    def required_cookies_dropped(self) -> bool:
        return self.status is LoginWriteStatus.REQUIRED_COOKIES_DROPPED


def _ensure_secure_parent_dir(path: Path) -> None:
    """Ensure ``path.parent`` exists and is ``0700`` on POSIX.

    Closes the master-token path's mode-less ``mkdir(parents=True)`` gap. The
    chmod is applied UNCONDITIONALLY (not only when this call creates the dir),
    restoring the pre-refactor self-heal that ``cli/services/login/cookie_writes.py``
    performed after every successful write: a credentials directory loosened by a
    backup / restore / sync tool (e.g. to 0755) is re-tightened to 0700 on the
    next login / refresh, so session-cookie files never sit under a
    world-traversable parent. Windows is skipped (POSIX modes are a no-op there
    and can confuse ACL inheritance from ``%USERPROFILE%``).
    """
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    if sys.platform != "win32":
        with contextlib.suppress(OSError):
            os.chmod(parent, 0o700)


@contextlib.contextmanager
def _acquire_storage_lock(
    lock_path: Path,
    *,
    log_prefix: str,
    deadline_seconds: float = _LOCK_ACQUIRE_DEADLINE_SECONDS,
) -> Iterator[str]:
    """Platform-neutral **bounded** exclusive acquire of a storage sentinel lock.

    Non-blocking probe (via ``storage._file_lock(blocking=False)``, which takes
    the per-path in-process ``threading.Lock`` before the OS lock) plus a
    deadline/jitter retry loop. Yields one of:

    * ``"held"`` — the lock is held; released when the ``with`` block exits.
    * ``"unavailable"`` — the deadline elapsed under contention, or the lock
      infrastructure failed (read-only dir, NFS without flock, fd exhaustion).

    The caller maps ``"unavailable"`` to its per-intent policy: fail-open
    callers proceed, fail-closed callers raise :class:`LockUnavailableError`.
    """
    from . import storage as _storage  # lazy: avoid the storage<->writer cycle

    deadline = time.monotonic() + deadline_seconds
    delay = _LOCK_ACQUIRE_INITIAL_DELAY_SECONDS
    while True:
        with _storage._file_lock(lock_path, blocking=False, log_prefix=log_prefix) as state:
            if state == "held":
                yield "held"
                return
            if state == "unavailable":
                # Infrastructure failure — no amount of retrying will help.
                yield "unavailable"
                return
            # state == "contended": another holder (thread or process) has it.
        # Jittered exponential backoff (shared with ``_acquire_os_lock``'s
        # Windows retry via ``storage._sleep_backoff`` — one tuning site).
        next_delay = _storage._sleep_backoff(delay, deadline)
        if next_delay is None:
            logger.debug(
                "%s: bounded storage-lock acquire exceeded %.0fs deadline; giving up",
                log_prefix,
                deadline_seconds,
            )
            yield "unavailable"
            return
        delay = next_delay


# ---------------------------------------------------------------------------
# CAS delta merge (relocated from ``storage.save_cookies_to_storage``)
# ---------------------------------------------------------------------------


def merge_cookie_delta(
    cookie_jar: httpx.Cookies,
    path: Path | None = None,
    *,
    original_snapshot: CookieSnapshot | None = None,
    recovery_observation: RecoveryCookieObservation | None = None,
    return_result: bool = False,
) -> bool | CookieSaveResult:
    """CAS snapshot/delta merge of ``cookie_jar`` into ``storage_state.json``.

    Relocated verbatim (behaviour-preserving) from
    ``storage.save_cookies_to_storage``; that function remains the public,
    monkeypatchable delegate seam. The ``original_snapshot=None`` legacy-warning
    branch stays on the delegate so its ``stacklevel`` still points at the
    caller.

    This is a **CAS** intent: on lock unavailability it **fails open** (status
    quo — the snapshot/delta CAS guards preserve correctness), driven by
    ``storage._file_lock_exclusive``. The full signature (incl.
    ``recovery_observation``) and the :class:`CookieSaveResult` return with
    ``cas_rejected_keys`` are load-bearing for the PSIDTS-recovery and
    cookie-persistence baseline callers.
    """
    from . import storage as _storage  # lazy: avoid the storage<->writer cycle

    cookie_save_result = _storage.CookieSaveResult
    cookie_save_return = _storage._cookie_save_return

    if path is None and resolve_auth_json_env() is not None:
        logger.debug("Skipping cookie sync: Auth loaded from NOTEBOOKLM_AUTH_JSON env var")
        return cookie_save_return(cookie_save_result(True), return_result=return_result)

    if path is None:
        logger.debug("Skipping cookie sync: No storage file path available")
        return cookie_save_return(cookie_save_result(True), return_result=return_result)

    lock_path = _storage_state_lock_path(path)
    with _storage._file_lock_exclusive(lock_path):
        if not path.exists():
            logger.debug("Skipping cookie sync: Storage file not found at %s", path)
            return cookie_save_return(cookie_save_result(False), return_result=return_result)

        try:
            storage_data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(
                "Failed to read storage state for cookie sync: %s",
                type(e).__name__,
            )
            return cookie_save_return(cookie_save_result(False), return_result=return_result)

        cookies = storage_data.get("cookies") if isinstance(storage_data, dict) else None
        if not isinstance(cookies, list):
            logger.warning(
                "storage_state at %s has an invalid 'cookies' key/payload; "
                "rotated cookies will not be persisted",
                path,
            )
            return cookie_save_return(cookie_save_result(False), return_result=return_result)

        if original_snapshot is None:
            updated_count = _storage._merge_cookies_legacy(cookie_jar, storage_data)
            cas_rejected_keys: frozenset[Any] = frozenset()
        else:
            updated_count, cas_rejected_keys = _storage._merge_cookies_with_snapshot(
                cookie_jar,
                storage_data,
                original_snapshot,
                recovery_observation=recovery_observation,
            )

        if updated_count == 0:
            # A CAS rejection with no other successful work means disk does
            # not reflect our intent; the caller must not advance baseline.
            return cookie_save_return(
                cookie_save_result(not cas_rejected_keys, cas_rejected_keys),
                return_result=return_result,
            )

        try:
            atomic_write_json(path, storage_data)
            logger.debug("Successfully synced %d refreshed cookies to %s", updated_count, path)
            # Even on a successful disk write, if any CAS arm rejected work,
            # disk diverges from ``post`` for at least one key — caller must
            # not advance baseline.
            return cookie_save_return(
                cookie_save_result(not cas_rejected_keys, cas_rejected_keys),
                return_result=return_result,
            )
        except Exception as e:
            logger.warning(
                "Failed to write updated cookies to %s: %s",
                path,
                type(e).__name__,
            )
            return cookie_save_return(cookie_save_result(False), return_result=return_result)


# ---------------------------------------------------------------------------
# In-band account writers (relocated from ``account.py``)
# ---------------------------------------------------------------------------


def update_account_metadata(
    storage_path: Path,
    *,
    authuser: int,
    email: str | None = None,
    only_if_absent: bool = False,
    deadline_seconds: float = _LOCK_ACQUIRE_DEADLINE_SECONDS,
) -> bool:
    """Persist account metadata atomically inside ``storage_state.json``.

    Relocated from ``account.write_account_metadata`` (the in-band write only —
    the sibling ``context.json`` cleanup ``_drop_legacy_account_key`` stays in
    ``account.py``). Full-file RMW intent: **fails closed**, raising
    :class:`LockUnavailableError` on lock unavailability.

    ``only_if_absent`` closes a check-then-act race in
    :func:`account.promote_legacy_account`: that caller reads the legacy
    record and checks whether an in-band record is already present — both
    OUTSIDE this function's lock — before deciding to call this function at
    all. Without a re-check taken under the SAME lock as the write, a
    concurrent fresh login/account-switch (``write_account_metadata``,
    ``replace_from_login``) landing in that unlocked gap would commit its new
    record first, and this call would then unconditionally overwrite it with
    the stale legacy values the caller captured before the gap — silently
    re-clobbering a just-completed account switch with a promotion nobody
    asked to happen. ``write_account_metadata`` (an intentional overwrite —
    the whole point of a real login) always passes ``only_if_absent=False``
    (the default); only the promotion caller opts in.

    ``deadline_seconds`` lets an opportunistic caller bound how long it will
    contend for the lock. The default (90s, matching every other full-file
    RMW intent) is right for an intentional login. It is WRONG for
    :func:`account.promote_legacy_account`: that function now runs inside
    :func:`account.read_account_metadata`, which many callers — including
    ``async`` code paths (``client.get_account_email``, token-route
    resolution) — call assuming a fast, lock-free read. Blocking one of those
    for up to 90s on lock contention would freeze the event loop for far
    longer than the "read" it thinks it's doing. Promotion is best-effort by
    design (a failed promotion falls back to the legacy record, never breaks
    the read — see ``promote_legacy_account``), so a short deadline that gives
    up fast and takes that fallback is strictly the right trade-off; only the
    promotion caller passes a short one.

    Returns:
        ``True`` if a write happened; ``False`` if ``only_if_absent`` was set
        and an in-band record was already present under the lock (no-op —
        the caller's stale values were correctly discarded).
    """
    from . import account as _account  # lazy: avoid the account<->writer cycle

    account_payload: dict[str, Any] = {"authuser": authuser}
    if email:
        account_payload["email"] = email

    lock_path = _storage_state_lock_path(storage_path)
    _ensure_secure_parent_dir(storage_path)
    with _acquire_storage_lock(
        lock_path, log_prefix="write_account_metadata", deadline_seconds=deadline_seconds
    ) as state:
        if state != "held":
            raise LockUnavailableError(
                f"write_account_metadata: storage lock unavailable at {lock_path}"
            )
        data = _account._load_storage_state_for_write(storage_path)
        namespace = data.get(_account._STORAGE_NAMESPACE_KEY)
        if not isinstance(namespace, dict):
            namespace = {}
        elif only_if_absent and isinstance(namespace.get(_account._ACCOUNT_CONTEXT_KEY), dict):
            return False
        namespace["version"] = _account._STORAGE_NAMESPACE_VERSION
        namespace[_account._ACCOUNT_CONTEXT_KEY] = account_payload
        data[_account._STORAGE_NAMESPACE_KEY] = namespace
        atomic_write_json(storage_path, data)
        return True


def clear_in_band_account(storage_path: Path) -> None:
    """Remove the ``notebooklm.account`` key from ``storage_state.json``.

    Relocated from ``account._clear_in_band_account``. Best-effort cleanup:
    swallows lock unavailability and read/parse errors, matching the
    pre-refactor semantics (the reader falls back to the legacy record). No-op if
    the file is missing, unreadable, or carries no in-band record.
    """
    from . import account as _account  # lazy: avoid the account<->writer cycle

    if not storage_path.exists():
        return
    lock_path = _storage_state_lock_path(storage_path)
    _ensure_secure_parent_dir(storage_path)
    with _acquire_storage_lock(lock_path, log_prefix="clear_account_metadata") as state:
        if state != "held":
            # Best-effort: the same failure mode the old filelock OSError arm
            # swallowed. The legacy reader still resolves the account record.
            logger.debug("in-band account clear skipped: storage lock unavailable at %s", lock_path)
            return
        try:
            data = json.loads(storage_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.debug("in-band account clear skipped at %s: %s", storage_path, e)
            return
        if not isinstance(data, dict):
            return
        namespace = data.get(_account._STORAGE_NAMESPACE_KEY)
        if not isinstance(namespace, dict) or _account._ACCOUNT_CONTEXT_KEY not in namespace:
            return
        del namespace[_account._ACCOUNT_CONTEXT_KEY]
        if set(namespace.keys()) <= {"version"}:
            del data[_account._STORAGE_NAMESPACE_KEY]
        else:
            data[_account._STORAGE_NAMESPACE_KEY] = namespace
        atomic_write_json(storage_path, data)


# ---------------------------------------------------------------------------
# Browser-capture re-mint (relocated from ``browser_capture.py``)
# ---------------------------------------------------------------------------


def replace_from_remint(
    path: Path,
    captured_state: dict[str, Any],
    *,
    carry_account: bool,
    include_domains: set[str] | None = None,
) -> WriteOutcome:
    """Full cookie replace for a browser-capture re-mint, under the storage lock.

    The single sanctioned persist for the :mod:`notebooklm._auth.browser_capture`
    arms (interactive login, L3 headless-launch re-auth, CDP re-auth). Replaces
    ``storage_state.json``'s cookies with ``captured_state`` — a re-mint is a
    brand-new session, so cookies are *replaced*, never merged. Full-file replace
    intent: **fails closed**, returning ``WriteOutcome(lock_unavailable)`` on lock
    unavailability so the capture caller can surface/retry rather than race a
    concurrent keepalive write ([capture-2]).

    Everything below happens **inside** the canonical storage lock:

    * The write-time domain filter
      (:func:`filter_storage_state_cookies_by_domain_policy`) is applied so
      sibling-product cookies never reach disk. ``include_domains`` carries the
      interactive ``--include-domains`` opt-in through unchanged; the default
      policy preserves trusted Google roots (``*.googleusercontent.com`` / Drive
      etc.), matching main's preserve-trusted-roots behavior. The filter is
      idempotent, so a caller that pre-filtered with the same ``include_domains``
      is not narrowed further.
    * Account namespace handling branches on ``carry_account``:

      - ``carry_account=True`` (unattended profile-launch arm): the existing
        ``notebooklm`` namespace is read from the current file and CARRIED OVER
        into the new state, so an in-place re-mint against our own profile no
        longer destroys the account binding ([capture-1]).
      - ``carry_account=False`` (interactive arm, and the CDP no-resolve
        fallback): the stale binding is DROPPED — the user may have signed into a
        different account. On the INTERACTIVE login arm the CLI adapter's
        ``repair_playwright_account_metadata`` re-establishes it immediately
        after the write. On the library / mid-RPC CDP arm there is NO such
        repair, so it lands on the authuser=0 default (repair happens only via
        CLI ``auth refresh``); carrying a stale index blindly would instead
        relocate [capture-1], so authuser=0 is the deliberate safe fallback.

    CDP arm caveat: CDP attaches to the operator's daily Chrome, whose account
    set may not match the stored binding. The CALLER re-resolves the stored email
    against the captured jar (any network lookup happens OUTSIDE this held lock)
    and passes the verdict as ``carry_account``; on no-resolve it passes
    ``carry_account=False`` rather than carry a possibly-misrouting index.

    Args:
        path: Destination ``storage_state.json``.
        captured_state: The (already healed) captured storage-state dict.
        carry_account: Whether to carry the existing account namespace forward.
        include_domains: Optional ``--include-domains`` opt-in labels, applied by
            the internal filter (mirrors the capture caller's filter call).

    Returns:
        :class:`WriteOutcome` — ``ok`` on success, ``lock_unavailable`` if the
        bounded storage-lock acquire timed out / the lock infra failed.
    """
    from . import account as _account  # lazy: avoid the account<->writer cycle
    from ._browser_cookie_filter import (  # noqa: PLC0415 (leaf; avoid import cycle)
        filter_storage_state_cookies_by_domain_policy,
    )

    _ensure_secure_parent_dir(path)
    lock_path = _storage_state_lock_path(path)
    with _acquire_storage_lock(lock_path, log_prefix="replace_from_remint") as state:
        if state != "held":
            return WriteOutcome(WriteStatus.LOCK_UNAVAILABLE)

        # Carry the existing account namespace BEFORE overwriting (read under the
        # same lock so it can't tear against a concurrent writer).
        carried_namespace: dict[str, Any] | None = None
        if carry_account and path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                existing = None
            if isinstance(existing, dict):
                namespace = existing.get(_account._STORAGE_NAMESPACE_KEY)
                if isinstance(namespace, dict):
                    carried_namespace = namespace

        # Write-time domain filter (preserve-trusted-roots). Returns a fresh
        # ``{"cookies": [...], "origins": []}`` — the captured browser state
        # never carries our ``notebooklm`` namespace, so it is only (re)attached
        # from the carried value below.
        filtered = filter_storage_state_cookies_by_domain_policy(
            dict(captured_state), include_domains=include_domains
        )
        if carried_namespace is not None:
            filtered[_account._STORAGE_NAMESPACE_KEY] = carried_namespace
        atomic_write_json(path, filtered)
    return WriteOutcome(WriteStatus.OK)


# ---------------------------------------------------------------------------
# Login / import full-replace (hoisted from the CLI ``cli/services/login`` and
# ``cli/_cookie_import`` writers — the #2086 filter + revalidation move HERE)
# ---------------------------------------------------------------------------


def replace_from_login(
    path: Path,
    state: dict[str, Any],
    *,
    include_domains: set[str] | None,
    include_optional: bool = False,
    account: AccountArg = KEEP_ACCOUNT,
    backup: bool = False,
    io_policy: object | None = None,
) -> LoginWriteOutcome:
    """Full cookie replace for the CLI login / import flows, under the storage lock.

    The single sanctioned persist for ``notebooklm login --browser-cookies``,
    ``notebooklm auth refresh --browser-cookies``, and ``notebooklm auth
    import-cookies``. Replaces ``storage_state.json``'s cookies with ``state`` —
    a login is a brand-new session, so cookies are *replaced*, never merged.
    Everything below happens **inside** the canonical storage lock; the writer
    **fails closed** (``LoginWriteOutcome(lock_unavailable)``) so a caller can
    surface/retry rather than race a concurrent keepalive write.

    Under the lock, in order:

    1. **Write-time domain filter.** ``state``'s cookies are run through
       :func:`filter_storage_state_cookies_by_domain_policy` (hoisted from the
       #2086 CLI call sites) so sibling-product cookies never reach disk.
       ``include_domains`` / ``include_optional`` carry the CLI opt-ins through;
       the default policy preserves trusted Google roots
       (``*.googleusercontent.com`` / Drive) — main's preserve-trusted-roots
       behaviour. The filter is idempotent, so a caller (import) that pre-filtered
       with the same opts is not narrowed further.
    2. **Post-filter required-cookie revalidation.** ``MINIMUM_REQUIRED_COOKIES``
       is re-checked on the FILTERED names. If a required cookie's only copy sat
       on a now-dropped domain, the writer returns
       ``LoginWriteOutcome(required_cookies_dropped, ...)`` and writes NOTHING —
       preserving #2086's contract (the CLI maps this to
       ``CookieValidationFailure(code="COOKIE_VALIDATION_FAILED")`` + ``io.fail(1)``
       + ``not storage_path.exists()``). Both ``missing_required`` and
       ``present_names`` are value-free cookie NAMES.
    3. **Account metadata**, embedded in the same atomic write via the ``account``
       sentinel:

       - :data:`KEEP_ACCOUNT` (default; the import flavour) — carry whatever
         ``state`` already holds in the ``notebooklm`` namespace (import has none,
         so the result carries none). No account key is synthesised.
       - :data:`CLEAR_ACCOUNT` (the refresh default-account login branch) — no
         account binding is written, so stale routing cannot survive.
       - :class:`AccountRecord` (the targeted login branches) — the
         ``{authuser, email}`` binding is embedded, replacing the former separate
         ``write_account_metadata`` step (one atomic write, no partial-failure
         window).
    4. **Opt-in recording.** The resolved ``include_domains`` (and
       ``include_optional``) are recorded in the ``notebooklm`` namespace so a
       future merge-gate narrowing can consult per-profile opt-ins (plan §b.5);
       additive — old readers ignore unknown namespace keys.
    5. **Import backup.** When ``backup=True`` (the import flavour), a pre-overwrite
       ``.bak`` copy of any existing target is taken INSIDE the lock (0600 on
       POSIX) so it cannot race a concurrent keepalive write; its path is returned
       in the outcome.

    Args:
        path: Destination ``storage_state.json``.
        state: The captured / coerced storage-state dict to persist.
        include_domains: ``--include-domains`` opt-in labels (or ``None``).
        include_optional: Persist all optional sibling-product domains (the
            import-cookies flavour).
        account: Account-metadata action (see above).
        backup: Take a pre-overwrite ``.bak`` backup inside the lock (import).
        io_policy: Reserved for a future per-intent lock/IO policy override;
            currently unused (accepted for forward-compatible call sites).

    Returns:
        :class:`LoginWriteOutcome`.
    """
    del io_policy  # reserved; see docstring
    from . import account as _account  # lazy: avoid the account<->writer cycle
    from ._browser_cookie_filter import (  # noqa: PLC0415 (leaf; avoid import cycle)
        filter_storage_state_cookies_by_domain_policy,
    )
    from .cookie_policy import (  # noqa: PLC0415 (leaf; avoid import cycle)
        MINIMUM_REQUIRED_COOKIES,
        cookie_names_from_storage,
    )

    _ensure_secure_parent_dir(path)
    lock_path = _storage_state_lock_path(path)
    with _acquire_storage_lock(lock_path, log_prefix="replace_from_login") as lock_state:
        if lock_state != "held":
            return LoginWriteOutcome(LoginWriteStatus.LOCK_UNAVAILABLE)

        # (1) Write-time domain filter (preserve-trusted-roots). Returns a fresh
        # ``{"cookies": [...], "origins": []}`` — the browser/import state never
        # carries our ``notebooklm`` namespace.
        filtered = filter_storage_state_cookies_by_domain_policy(
            dict(state), include_optional=include_optional, include_domains=include_domains
        )

        # (2) Post-filter required-cookie revalidation on the FILTERED names.
        present = cookie_names_from_storage(filtered)
        missing_required = tuple(sorted(MINIMUM_REQUIRED_COOKIES.difference(present)))
        if missing_required:
            # Count-only breadcrumb — never cookie names or values.
            logger.debug(
                "replace_from_login: %d required cookie(s) dropped by the write-time "
                "domain policy for %s; writing nothing",
                len(missing_required),
                path,
            )
            return LoginWriteOutcome(
                LoginWriteStatus.REQUIRED_COOKIES_DROPPED,
                missing_required=missing_required,
                present_names=tuple(sorted(present)),
            )

        # (3) + (4) Build the ``notebooklm`` namespace (account + opt-ins).
        namespace: dict[str, Any] = {}
        if account is KEEP_ACCOUNT:
            existing_ns = state.get(_account._STORAGE_NAMESPACE_KEY)
            if isinstance(existing_ns, dict):
                namespace = dict(existing_ns)
        elif isinstance(account, AccountRecord):
            payload: dict[str, Any] = {"authuser": account.authuser}
            if account.email:
                payload["email"] = account.email
            namespace[_account._ACCOUNT_CONTEXT_KEY] = payload
        # CLEAR_ACCOUNT: leave the account key absent.
        if include_domains:
            namespace["include_domains"] = sorted(include_domains)
        if include_optional:
            namespace["include_optional"] = True
        if namespace:
            namespace.setdefault("version", _account._STORAGE_NAMESPACE_VERSION)
            filtered[_account._STORAGE_NAMESPACE_KEY] = namespace

        # (5) Import backup, inside the lock, before overwriting.
        backup_path: Path | None = None
        if backup and path.exists():
            candidate = path.with_name(path.name + ".bak")
            shutil.copy2(path, candidate)
            # ``copy2`` preserves the SOURCE mode; force 0600 so a backup of a
            # legacy/world-readable storage_state never leaks credentials at rest.
            if sys.platform != "win32":
                with contextlib.suppress(OSError):
                    os.chmod(candidate, 0o600)
            backup_path = candidate

        atomic_write_json(path, filtered)
    # Outside the storage lock (its own sibling ``.lock``, matching
    # ``write_account_metadata``'s ordering): the in-band write just committed
    # (or explicitly cleared) the account binding.
    #
    # KEEP_ACCOUNT (the import-cookies default) with NO existing in-band record
    # to carry is NOT an intentional "no account" decision — it means the caller
    # (typically a fresh browser/import jar with no ``notebooklm`` namespace of
    # its own) never considered the account question at all. Scrubbing the
    # legacy sibling unconditionally in that case PERMANENTLY DESTROYS a
    # pre-v0.5.0 profile's only copy of its account binding: nothing was
    # embedded in-band, and the legacy record is gone from disk with no reader
    # left to find it (verified: `auth import-cookies` on such a profile drops
    # authuser 3 -> 0 irrecoverably). Promote instead — it embeds a legacy
    # record if one exists (then scrubs it) and is a safe no-op otherwise.
    #
    # An EXPLICIT decision — CLEAR_ACCOUNT, or an AccountRecord that did embed —
    # must still scrub directly: CLEAR_ACCOUNT means the caller deliberately
    # wants no account bound, and promoting there would resurrect a binding
    # that was just intentionally cleared.
    if account is KEEP_ACCOUNT and _account._ACCOUNT_CONTEXT_KEY not in namespace:
        _account.promote_legacy_account(path)
    else:
        _account._drop_legacy_account_key(path)
    return LoginWriteOutcome(LoginWriteStatus.OK, backup_path=backup_path)


# ---------------------------------------------------------------------------
# Master-token writers (relocated from ``master_token.py``)
# ---------------------------------------------------------------------------


def persist_minted_jar(
    path: Path,
    jar: httpx.Cookies,
    *,
    email: str | None,
    force: bool = False,
    refuse_unknown_owner: bool = True,
) -> None:
    """Replace the cookies in ``storage_state.json`` with a freshly-minted jar.

    Relocated from ``master_token.persist_minted_jar``, now routed through
    :func:`atomic_write_json` (fsync durability + temp cleanup, closing
    [storage-F5]) while keeping the storage lock it already held and its
    rebind-to-minted-account namespace semantics. Old cookies are *replaced*, not
    merged — a re-mint is a brand-new session. Full-file replace intent:
    **fails closed**.

    b-PR2 additionally applies the write-time domain filter
    (:func:`filter_storage_state_cookies_by_domain_policy`, default policy —
    preserve-trusted-roots) to the minted cookies before they reach disk, closing
    the L4 unfiltered-persist gap. The rebind to the minted account
    (``authuser=0`` + the minted ``email``) is unaffected: the filter only
    narrows the cookie rows, never the account namespace.

    #2103 PR-2 D6: the authoritative account-ownership guard lives HERE, under
    the storage-write lock this function already holds — not only in
    :func:`notebooklm._auth.master_token.assert_account_writable`'s pre-mint
    advisory check, which cannot see a caller that mints and persists directly
    (the documented low-level recipe does exactly that) and cannot close the
    TOCTOU window between a pre-check and this write. Existing storage bound to
    a DIFFERENT recorded email than ``email`` always raises
    :class:`notebooklm._auth.master_token.MasterTokenError` unless ``force``.
    No existing storage: proceeds unconditionally (nothing to protect yet).

    ``refuse_unknown_owner`` (default ``True``) additionally refuses existing
    storage with NO recorded owner at all, unless ``force``. Callers re-minting
    from a master token ALREADY paired with this exact ``storage_path`` (the
    L4 recovery rung, the no-prompt operator re-mint —
    :func:`notebooklm._auth.master_token.remint_from_stored_token`) pass
    ``refuse_unknown_owner=False``: that pairing was already trusted when the
    token was first bootstrapped for this profile, so an account-less
    profile (never bound to an ``--account``, e.g. a cookie-only
    ``import-cookies`` profile — empirically the COMMON case, not the "rare"
    one D6 originally assumed) must not lose mid-session self-recovery. A
    caller *selecting* an account for the first time
    (:func:`notebooklm._auth.master_token.bootstrap_from_oauth_token`) keeps
    the default: minting into an existing, unrecorded-owner profile is
    exactly the ambiguous case worth refusing without an explicit ``force``.
    """
    from . import account as _account  # noqa: PLC0415 (avoid the account<->writer cycle)
    from . import master_token as _master_token  # lazy: avoid import cycle
    from ._browser_cookie_filter import (  # noqa: PLC0415 (leaf; avoid import cycle)
        filter_storage_state_cookies_by_domain_policy,
    )

    _ensure_secure_parent_dir(path)
    lock_path = _storage_state_lock_path(path)
    with _acquire_storage_lock(lock_path, log_prefix="persist_minted_jar") as state:
        if state != "held":
            raise LockUnavailableError(
                f"persist_minted_jar: storage lock unavailable at {lock_path}"
            )
        data: dict[str, Any] = {}
        existed = path.exists()
        if existed:
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                data = loaded if isinstance(loaded, dict) else {}
            except json.JSONDecodeError:
                data = {}
        if existed and force:
            logger.debug("persist_minted_jar: force=True bypasses the account-ownership guard.")
        elif existed:
            existing_owner = _account.read_account_metadata_from_storage_state(data).get("email")
            existing_owner = existing_owner.strip() if isinstance(existing_owner, str) else None
            if not existing_owner:
                if refuse_unknown_owner:
                    raise _master_token.MasterTokenError(
                        "This profile has no recorded account owner; refusing to overwrite "
                        "its session with a freshly minted one without force=True."
                    )
                logger.debug(
                    "persist_minted_jar: existing storage has no recorded owner; proceeding "
                    "with refuse_unknown_owner=False (re-mint from a token already paired "
                    "with this storage_path, not a fresh account selection)."
                )
            elif existing_owner.casefold() != (email or "").casefold():
                raise _master_token.MasterTokenError(
                    f"This profile already belongs to {existing_owner}, but the mint is "
                    f"for {email or '(no account)'}. Minting here would overwrite "
                    f"{existing_owner}'s session and master token. Pass force=True to "
                    "overwrite this profile intentionally."
                )
        # Apply the write-time domain filter to the minted jar (L4 gap): the
        # minted cookies were previously persisted raw. Default policy — trusted
        # Google roots are preserved (main's preserve-trusted-roots behavior).
        minted_state = _master_token.storage_state_from_jar(jar)
        filtered_minted = filter_storage_state_cookies_by_domain_policy(minted_state)
        data["cookies"] = filtered_minted["cookies"]
        data.setdefault("origins", [])
        ns_raw = data.get("notebooklm")
        ns: dict[str, Any] = ns_raw if isinstance(ns_raw, dict) else {}
        ns["version"] = 1
        ns["account"] = {"authuser": 0, **({"email": email} if email else {})}
        data["notebooklm"] = ns
        atomic_write_json(path, data)


def write_master_token(path: Path, *, email: str, master_token: str, android_id: str) -> None:
    """Persist a ``master_token.json`` record at mode 0600 (full-account credential).

    Relocated from ``master_token.write_master_token``, now routed through
    :func:`atomic_write_json` (atomic + fsync-durable + temp cleanup) and guarded
    by a bounded sibling ``.master_token.json.lock`` — it was previously lockless
    (part of [storage-F5]). RMW intent: **fails closed**.
    """
    from . import master_token as _master_token  # lazy: avoid import cycle

    _ensure_secure_parent_dir(path)
    payload = {
        "version": _master_token._MASTER_TOKEN_VERSION,
        "email": email,
        "android_id": android_id,
        "master_token": master_token,
    }
    # Sibling dotted lock for the credential file (distinct from the profile's
    # storage-state lock — a different file).
    lock_path = _storage_state_lock_path(path)
    with _acquire_storage_lock(lock_path, log_prefix="write_master_token") as state:
        if state != "held":
            raise LockUnavailableError(f"write_master_token: lock unavailable at {lock_path}")
        atomic_write_json(path, payload)
