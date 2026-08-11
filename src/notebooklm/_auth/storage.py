"""Profile persistence for authentication: storage state, its lock, and its writers.

This v0.x compatibility facade originated by merging three cap-split files —
``storage.py`` (snapshot/CAS merge math + the file-lock primitive),
``storage_writer.py`` (the canonical writer, ADR-0029), and
``storage_transaction.py`` (the write transaction template, ADR-0031 Stage 3) —
under ADR-0033's sanctioned-merge policy. Later phases moved concrete storage
and migration ownership behind the signatures retained here.
ADR-0033 PR 4.2 then relocated ``_browser_cookie_filter.py``'s write-time
cookie-domain filter in as well — misfiled under a ``browser_`` name, but write
policy applied by three of the writers here — retiring three more lazy imports.
ADR-0033 PR 5.2 initially relocated the account-record persistence helpers from
``_auth/account.py``. Phase 8 extracts their concrete two-file resolution,
sanitization, promotion, scheduler/exit lifecycle, and sibling scrub into
``profile_migration.py``; section 7b below now contains only the literal v0.x
adapters and projections.
``account.py`` keeps the NETWORK identity half (``enumerate_accounts``,
``_probe_authuser``, page-email extraction, the routing-value formatters) and
imports this module at module scope; the edge is now one-way.

The file is organised in labelled sections mirroring the former modules:

1. **Lock compatibility wrappers** — :func:`_file_lock` and blocking
   :func:`_file_lock_exclusive`, routed through the dependency-bottom
   :class:`~notebooklm._auth.storage_lock.StorageLockManager`.
2. **Transaction compatibility aliases** — secure-parent preparation,
   :func:`in_storage_transaction`, and its three lock-unavailable policies now
   have their real definitions in :mod:`notebooklm._auth.profile_store`.
4. **Snapshot types** — the path-aware cookie identity/value tuples and
   :class:`CookieSaveResult`.
5. **CAS + merge math** — snapshotting, the legacy and snapshot/delta merges, and
   :func:`save_cookies_to_storage`, the ADR-0029-pinned monkeypatchable delegate
   seam (``_runtime/lifecycle.py`` late-binds it; ~20 test files patch it).
6. **Writer outcome types** — the value-free enums/records the intent writers
   return.
7. **Write-time cookie-domain compatibility aliases** — the implementation and
   value-free malformed-row diagnostics live in the dependency-bottom
   :mod:`notebooklm._auth.cookie_filter` leaf.
8. **Temporary policy writers and adapters** — profile intents and the
   master-token intent, all routed through typed owners.
9. **Account compatibility adapters** (labelled ``SECTION 7b``) — the v0.x raw
   signatures and result projections. The adapters construct the path-owned
   store and concrete services from :mod:`notebooklm._auth.profile_migration`;
   they do not own the two-read algorithm, sanitization, promotion policy,
   scheduler/``atexit`` lifecycle, or ``context.json`` I/O.

This module remains the v0.x compatibility and policy façade. The sealed
atomic capability now lives in :mod:`notebooklm._auth.credential_io`, and
:class:`notebooklm._auth.profile_store.ProfileStore` owns profile reads and the
cookie transactions. :mod:`notebooklm._auth.profile_migration` owns legacy
account resolution, promotion, lifecycle, context I/O, and post-write
composition. The compatibility adapters below reach only the appropriate typed
owner.
No context lock or promotion registry state remains in this facade.

Intent-shaped API (all synchronous, all serialize on the canonical storage lock,
all write through the typed credential commit spine):

* :func:`merge_cookie_delta` — the compatibility adapter behind
  :func:`save_cookies_to_storage`. It is a **CAS** intent and therefore **fails
  open** on lock unavailability (status quo), delegated to ``ProfileStore``.
* :func:`update_account_metadata` / :func:`clear_in_band_account` — the in-band
  account writers relocated from :mod:`notebooklm._auth.account`. These are
  **full-file RMW** intents: :func:`update_account_metadata` **fails closed**
  (raises :class:`LockUnavailableError`) because failing open could overwrite a
  concurrent CAS delta; :func:`clear_in_band_account` is best-effort cleanup and
  swallows lock unavailability, matching the pre-refactor semantics.
* :func:`replace_from_remint` — the full cookie-replace re-mint persister for the
  BROWSER-CAPTURE arms (L3 headless-launch + interactive + CDP), relocated from
  the bare ``atomic_write_json`` sites in :mod:`notebooklm._auth.browser_capture`.
  The thin adapter delegates filtering, namespace carry/drop, and the bounded
  transaction to :class:`ProfileStore`. **Fails closed** (returns
  :class:`WriteOutcome` with ``lock_unavailable``). Closes [capture-2].
* :func:`replace_from_login` — the login/import full-replace, whose write-time
  domain filter and required-cookie revalidation run inside the lock.
  **Fails closed.**
* :func:`persist_minted_jar` — the master-token L4 compatibility adapter; the
  path-owned store now owns its fail-closed replacement transaction.
* :func:`write_master_token` — the ``master_token.json`` writer, now routed
  through ``_atomic_io`` **and** guarded by a bounded sibling lock (it was
  previously lockless). **Fails closed.**

Lock unification (ADR-0029) gives full-file intents a platform-neutral bounded
acquire (90 s) while cookie CAS retains its blocking fail-open acquire.
``StorageLockManager`` serializes threads before the OS lock using the exact raw
lock-path spelling; the distinct rotate sentinel is never collapsed into it.
Fail-closed writers raise public ``LockUnavailableError``, whose
``TimeoutError``/``OSError`` ancestry preserves existing catch behavior.

Permission contract (POSIX): bounded writers ensure the parent directory is
``0700`` and typed commits write ``0600`` files. Cookie saves intentionally keep
the manager's raw blocking-lock parent creation without an additional chmod.
On Windows we rely on ``%USERPROFILE%`` ACL inheritance.

Outcome types are **value-free by contract**: :class:`WriteOutcome` may carry
only an enum status — never cookie values, state dicts, jar objects, or caught
exceptions.
"""

from __future__ import annotations

import contextlib
import logging
import sys as sys
import warnings
from collections.abc import Iterator
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, NamedTuple, TypeAlias, cast

import httpx

# ``LockUnavailableError`` is the public, canonical home for the fail-closed
# lock-failure exception (``notebooklm.exceptions`` — also re-exported on the
# ``notebooklm.auth`` facade). It subclasses ``TimeoutError`` (an ``OSError``),
# exactly mirroring the ``filelock.Timeout`` MRO it replaces, so existing
# ``except OSError`` arms keep catching a lock failure. Re-exported here for the
# writers that raise it.
from ..exceptions import LockUnavailableError
from . import cookie_merge as _cookie_merge
from . import cookies as _auth_cookies
from .cookie_filter import (
    _safe_cookie_shape as _safe_cookie_shape,
)
from .cookie_filter import (
    filter_storage_state_cookies_by_domain_policy as filter_storage_state_cookies_by_domain_policy,
)
from .cookie_types import Cookie, CookieIdentity, CookieJar
from .master_token_file import MasterTokenFile
from .master_token_types import MasterToken, MasterTokenError
from .paths import resolve_auth_json_env
from .profile_account import (
    ClearAccount,
    DomainSelection,
    KeepAccount,
    ProfileAccount,
    SetAccount,
)
from .profile_document import ProfileDocument
from .profile_migration import (
    AccountMetadataWriter,
    LegacyAccount,
    LegacyAccountMigrator,
    LegacyPromotionScheduler,
    LoginProfileWriter,
    Promoted,
)
from .profile_migration import (
    _drop_legacy_account_key as _drop_legacy_account_key,
)
from .profile_store import (
    CookieMergeDisposition,
    LoginWriteRequest,
    MintedSessionWriteRequest,
    ProfileStore,
    RemintWriteRequest,
    ReplaceStatus,
    in_storage_transaction,
    raise_on_lock_unavailable,
    report_on_lock_unavailable,
    skip_on_lock_unavailable,
)
from .profile_store import (
    _ensure_secure_parent_dir as _ensure_secure_parent_dir,
)
from .profile_store import (
    _LockUnavailablePolicy as _LockUnavailablePolicy,
)
from .profile_store import (
    _MintedSessionOwnershipRefused as _MintedSessionOwnershipRefused,
)
from .storage_lock import LockRequest, StorageLockManager

logger = logging.getLogger("notebooklm.auth")

CookieKey: TypeAlias = _auth_cookies.CookieKey
_cookie_is_http_only = _auth_cookies._cookie_is_http_only

__all__ = [
    "CLEAR_ACCOUNT",
    "KEEP_ACCOUNT",
    "AccountRecord",
    "CookieSaveResult",
    "LockUnavailableError",
    "LoginWriteOutcome",
    "LoginWriteStatus",
    "WriteOutcome",
    "WriteStatus",
    "advance_cookie_snapshot_after_save",
    "clear_account_metadata",
    "clear_in_band_account",
    "get_account_email_for_storage",
    "get_authuser_for_storage",
    "in_storage_transaction",
    "merge_cookie_delta",
    "persist_minted_jar",
    "promote_legacy_account",
    "raise_on_lock_unavailable",
    "read_account_metadata",
    "read_account_metadata_from_storage_state",
    "replace_from_login",
    "replace_from_remint",
    "report_on_lock_unavailable",
    "resolve_account_identity",
    "save_cookies_to_storage",
    "skip_on_lock_unavailable",
    "snapshot_cookie_jar",
    "update_account_metadata",
    "write_account_metadata",
    "write_master_token",
]


# ==========================================================================
# SECTION 1 — LOCK COMPATIBILITY WRAPPERS
# The lock manager owns process/OS mechanics; these retain the v0.x seams.
# ==========================================================================


_STORAGE_LOCKS = StorageLockManager.process_default()


@contextlib.contextmanager
def _file_lock(lock_path: Path, *, blocking: bool, log_prefix: str) -> Iterator[str]:
    """Delegate one v0.x string-valued acquisition to the shared manager."""
    request = LockRequest(path=lock_path, blocking=blocking, operation=log_prefix)
    with _STORAGE_LOCKS.acquire(request) as state:
        yield state.value


@contextlib.contextmanager
def _file_lock_exclusive(lock_path: Path) -> Iterator[None]:
    """Blocking cross-process exclusive lock on ``lock_path``.

    Multiple Python processes that all save to the same ``storage_state.json``
    (e.g. a long-running ``NotebookLMClient(keepalive=...)`` worker plus a
    cron-driven ``notebooklm auth refresh``) would otherwise race on the read-
    merge-write cycle and lose updates. The lock is held on a sentinel file
    sibling to the storage file (``.storage_state.json.lock``, derived by
    :func:`notebooklm._auth.paths._storage_state_lock_path`), since locking the
    storage file itself would interfere with the atomic temp-rename below.

    Every ``storage_state.json`` mutation reaches the path-owned
    :class:`~notebooklm._auth.profile_store.ProfileStore` and its shared storage
    lock manager. No ``filelock.FileLock`` holder of this sentinel remains, so
    the old cross-mechanism POSIX interop is no longer load-bearing. The
    separate sibling ``context.json.lock`` FileLock is owned by
    :class:`~notebooklm._auth.profile_migration.LegacyAccountContext`.

    The lock is per-process: threads within one process aren't serialized —
    that's the intra-process ``threading.Lock`` held by the client. If the
    lock can't be acquired (e.g. NFS where flock semantics vary, read-only
    parent dir, fd exhaustion), the save proceeds anyway; correctness in
    that mode is best-effort and relies on the snapshot/delta CAS guards in
    :func:`_merge_cookies_with_snapshot` alone. The first time this
    fallback fires per process emits a WARNING so operators learn their
    deployment is running without cross-process coordination.
    """
    with _file_lock(lock_path, blocking=True, log_prefix="save_cookies_to_storage") as state:
        if state == "unavailable" and _STORAGE_LOCKS._claim_cookie_warning():
            logger.warning(
                "Cross-process file lock unavailable at %s; cookie saves will "
                "proceed without cross-process coordination and rely solely on "
                "snapshot/delta CAS guards. Common causes: NFS without flock "
                "support, read-only parent directory, fd exhaustion. (Logged "
                "once per process.)",
                lock_path,
            )
        yield


# ==========================================================================
# SECTION 4 — SNAPSHOT TYPES
# Path-aware cookie identity/value tuples and the detailed save result.
# ==========================================================================


class CookieSnapshotKey(NamedTuple):
    """Path-aware cookie identity used by the snapshot/delta save machinery.

    RFC 6265 treats ``path`` as part of cookie identity: two cookies with the
    same ``(name, domain)`` but different paths are distinct entries. The
    snapshot/delta path widens the legacy ``(name, domain)`` key (still used
    elsewhere for back-compat — see ``CookieKey``) to ``(name, domain, path)``
    so that path-scoped cookies (e.g. ``OSID`` on a per-product path) survive
    a load → save round trip and so that a sibling-process write to a
    different-path variant of the same name is not silently overwritten.
    """

    name: str
    domain: str
    path: str


class CookieSnapshotValue(NamedTuple):
    """Snapshot value tuple: ``(value, expires, secure, http_only)``.

    Widened from a bare ``str`` so that a ``Set-Cookie`` which keeps the same
    value but renews ``expires`` (or flips ``secure`` / ``httpOnly``) still
    registers as a delta. The legacy save path compared ``expires`` directly
    and would write the new expiry through; the snapshot path previously
    keyed on value alone and silently dropped attribute-only refreshes.
    """

    value: str
    expires: int | None
    secure: bool
    http_only: bool


CookieSnapshot: TypeAlias = dict[CookieSnapshotKey, CookieSnapshotValue]
# ``None`` is a private observation marker for a pre-existing target row whose
# value was empty, missing, or non-string.  It lets recovery replace an unusable
# row while still treating a newly-written non-empty sibling as a CAS conflict.
RecoveryCookieObservation: TypeAlias = dict[CookieSnapshotKey, frozenset[str | None]]


@dataclass(frozen=True)
class CookieSaveResult:
    """Detailed result for callers that need to maintain a save baseline."""

    ok: bool
    cas_rejected_keys: frozenset[CookieSnapshotKey] = frozenset()


# ==========================================================================
# SECTION 5 — CAS + MERGE MATH (and the pinned delegate seam)
# Snapshotting, baseline advancement, the legacy and snapshot/delta merges, and
# ``save_cookies_to_storage`` — the ADR-0029-pinned monkeypatchable delegate.
# ==========================================================================


def snapshot_cookie_jar(cookie_jar: httpx.Cookies) -> CookieSnapshot:
    """Capture an open-time snapshot of an httpx cookie jar.

    Snapshots are the input to the dirty-flag/delta merge in
    :func:`save_cookies_to_storage`: at save time, only cookies whose
    in-memory value differs from the snapshot — plus cookies absent from
    the jar but present in the snapshot (deletions) — are propagated to
    disk. Cookies the in-process code never touched are left to whatever
    a sibling process may have written (closes the Appendix A2
    stale-overwrite-fresh hazard).

    The key shape is path-aware ``(name, domain, path)`` (also closes
    the Appendix A2 path-collapse hazard). Cookies with no name or no domain
    are skipped — the storage format requires both.

    Args:
        cookie_jar: The httpx.Cookies object to snapshot.

    Returns:
        Mapping of ``CookieSnapshotKey -> CookieSnapshotValue`` capturing
        each cookie's value and the attributes the storage_state schema
        persists (``expires``, ``secure``, ``httpOnly``).
    """
    return {
        CookieSnapshotKey(cookie.name, cookie.domain, cookie.path or "/"): CookieSnapshotValue(
            value=cookie.value,
            expires=cookie.expires,
            secure=bool(cookie.secure),
            http_only=_cookie_is_http_only(cookie),
        )
        for cookie in cookie_jar.jar
        if cookie.name and cookie.domain and cookie.value is not None
    }


def _cookie_snapshot_key_variants(key: CookieSnapshotKey) -> set[CookieSnapshotKey]:
    """Return equivalent host/domain snapshot keys for leading-dot domains.

    Mirrors :func:`_cookie_key_variants` but preserves the path component so
    storage entries on the same path match snapshot entries regardless of
    whether ``http.cookiejar`` normalized the domain to a leading dot.
    """
    identity = CookieIdentity(key.name, key.domain, key.path)
    return {
        CookieSnapshotKey(variant.name, variant.domain, variant.path)
        for variant in _cookie_merge.equivalent_identities(identity)
    }


def _stored_cookie_snapshot_key(stored_cookie: Any) -> CookieSnapshotKey | None:
    """Build a path-aware snapshot key from a Playwright storage_state cookie."""
    identity = _cookie_merge.stored_cookie_identity(stored_cookie)
    if identity is None:
        return None
    return CookieSnapshotKey(identity.name, identity.domain, identity.path)


def advance_cookie_snapshot_after_save(
    original_snapshot: CookieSnapshot | None,
    post_save_snapshot: CookieSnapshot,
    cas_rejected_keys: frozenset[CookieSnapshotKey],
) -> CookieSnapshot | None:
    """Advance save baseline for successful keys while preserving rejected ones.

    A save can partially succeed: one cookie delta may write through while a
    sibling-process CAS conflict rejects another. Advancing the whole baseline
    would lose the rejected delta; keeping the whole old baseline would replay
    already-written deltas and wedge future saves. This helper advances every
    key to ``post_save_snapshot`` except the CAS-rejected keys, which retain
    their old baseline value or absence. Rejected keys are matched through
    leading-dot variants because the merge path can reject a normalized variant
    of the key captured in ``original_snapshot``.
    """
    if original_snapshot is None:
        return None

    advanced = dict(post_save_snapshot)
    for key in cas_rejected_keys:
        original_key = next(
            (
                variant
                for variant in _cookie_snapshot_key_variants(key)
                if variant in original_snapshot
            ),
            None,
        )
        for variant in _cookie_snapshot_key_variants(key):
            advanced.pop(variant, None)
        if original_key is not None:
            advanced[original_key] = original_snapshot[original_key]
    return advanced


def _cookie_save_return(
    result: CookieSaveResult, *, return_result: bool
) -> bool | CookieSaveResult:
    """Return either the detailed save result or its public bool projection."""
    return result if return_result else result.ok


def save_cookies_to_storage(
    cookie_jar: httpx.Cookies,
    path: Path | None = None,
    *,
    original_snapshot: CookieSnapshot | None = None,
    recovery_observation: RecoveryCookieObservation | None = None,
    return_result: bool = False,
) -> bool | CookieSaveResult:
    """Save an updated httpx.Cookies jar back to Playwright storage_state.json.

    This ensures that when Google issues short-lived token refreshes (e.g.
    during 302 redirects to accounts.google.com), those updated cookies are
    serialized back to disk so the session remains valid across CLI invocations.

    If auth was loaded from an environment variable (no file), this is a no-op.

    Cross-process safety: the read-merge-write cycle is wrapped in an OS-level
    file lock (``.storage_state.json.lock``) so concurrent writers from
    different Python processes (e.g. an in-process ``NotebookLMClient`` keepalive
    plus a cron-driven ``notebooklm auth refresh``) serialize cleanly rather
    than tearing or losing updates.

    Two merge modes:

    - **Legacy (``original_snapshot=None``)**: every in-memory cookie whose
      value differs from disk wins. Vulnerable to the stale-overwrite-fresh
      race documented in ``docs/auth-cookie-lifecycle.md`` Appendix A2 and emits a
      ``RuntimeWarning`` safety advisory about that race (this is a permanent
      back-compat shim, not a scheduled deprecation, so the advisory is a
      ``RuntimeWarning`` and is not silenced by ``NOTEBOOKLM_QUIET_DEPRECATIONS``).
      Kept only as a public-API back-compat shim for callers outside this repo;
      every first-party caller passes ``original_snapshot``.
    - **Snapshot/delta (``original_snapshot`` provided)**: only cookies
      whose in-memory persisted tuple differs from the snapshot are written, and
      cookies present in the snapshot but no longer in the jar are
      deleted from disk. Cookies the in-process code never touched are
      left untouched on disk so a sibling-process write survives.
      Path-aware ``(name, domain, path)`` keys are used here (also closes
      the Appendix A2 path-collapse hazard).

    Args:
        cookie_jar: The httpx.Cookies object containing the latest cookies.
        path: Path to storage_state.json. If None, cookie sync is skipped.
        original_snapshot: Open-time snapshot from
            :func:`snapshot_cookie_jar`. When provided, only deltas and
            deletions relative to the snapshot are persisted.
        return_result: Internal escape hatch for callers that need CAS-rejected
            keys to maintain a per-cookie baseline. Public callers should use
            the default bool return.

    Returns:
        ``True`` if the disk state now reflects the caller's intent (write
        succeeded, was a successful no-op, or the call was a deliberate skip
        because auth was loaded from an env var). ``False`` if an I/O error
        prevented the save or a CAS guard preserved a sibling-process write.
        With ``return_result=True``, callers can inspect CAS-rejected keys and
        advance their baseline for the keys that did write through.
    """
    if original_snapshot is None and path is not None:
        # NOT a deprecation: the original_snapshot=None form is a *permanent*
        # public-API back-compat shim (docs/auth-cookie-lifecycle.md Appendix A2),
        # not a scheduled removal — every in-tree caller already passes a
        # snapshot. The warning is a runtime safety advisory about the
        # stale-overwrite-fresh race that path is vulnerable to, so it is a
        # RuntimeWarning, not a DeprecationWarning. It is therefore outside
        # ADR-0018's scope: no NOTEBOOKLM_QUIET_DEPRECATIONS gate, no removal
        # version, and emitted directly here rather than via warn_deprecated.
        # Emitted on THIS delegate (not the relocated merge body) so
        # ``stacklevel=2`` still points at the caller.
        warnings.warn(
            "save_cookies_to_storage called without original_snapshot; the "
            "legacy full-merge path is vulnerable to the stale-overwrite-fresh "
            "race (docs/auth-cookie-lifecycle.md Appendix A2). Pass an original_snapshot "
            "captured via snapshot_cookie_jar() at jar-open time.",
            RuntimeWarning,
            stacklevel=2,
        )

    # Canonical patch seam: the CAS delta merge body lives in
    # :func:`merge_cookie_delta` (section 5 below). This module-level
    # ``save_cookies_to_storage`` symbol stays here as the monkeypatchable
    # delegate (~18 test files patch it; ``_runtime/lifecycle.py`` late-binds it).
    # Before ADR-0033's persistence merge the delegate reached the body through a
    # function-local ``from . import storage_writer``; it is now a same-module call.
    return merge_cookie_delta(
        cookie_jar,
        path,
        original_snapshot=original_snapshot,
        recovery_observation=recovery_observation,
        return_result=return_result,
    )


def _cookie_jar_for_merge(cookie_jar: httpx.Cookies, *, include_none: bool) -> CookieJar:
    """Adapt a live jar without filtering domains or freezing legacy tuples."""
    return CookieJar.from_live_httpx_for_merge(cookie_jar, include_none=include_none)


def _cookie_jar_from_snapshot(snapshot: CookieSnapshot) -> CookieJar:
    return CookieJar(
        Cookie(
            name=key.name,
            domain=key.domain,
            path=key.path,
            value=value.value,
            expires=value.expires,
            secure=value.secure,
            http_only=value.http_only,
        )
        for key, value in snapshot.items()
    )


def _recovery_observation_value(
    observation: RecoveryCookieObservation | None,
) -> _cookie_merge.RecoveryObservation | None:
    if observation is None:
        return None
    return _cookie_merge.RecoveryObservation(
        {
            CookieIdentity(key.name, key.domain, key.path): frozenset(values)
            for key, values in observation.items()
        }
    )


def _install_decided_document(
    storage_data: dict[str, Any], document: ProfileDocument | None
) -> None:
    if document is None:
        return
    decided = document.to_json()
    storage_data.clear()
    storage_data.update(decided)


def _merge_cookies_legacy(cookie_jar: httpx.Cookies, storage_data: dict[str, Any]) -> int:
    """Legacy merge: trust in-memory whenever it differs from disk.

    Vulnerable to the stale-overwrite-fresh race (Appendix A2). Kept only for
    callers that have not yet opted into snapshot semantics. New callers
    must pass ``original_snapshot`` to :func:`save_cookies_to_storage`.

    Returns:
        Number of cookie entries added or modified in ``storage_data``.
    """
    decision = _cookie_merge.decide_legacy_cookie_overlay(
        stored=ProfileDocument.decode(storage_data),
        observation=_cookie_jar_for_merge(cookie_jar, include_none=True),
    )
    _install_decided_document(storage_data, decision.document)
    return decision.updated_rows


def _merge_cookies_with_snapshot(
    cookie_jar: httpx.Cookies,
    storage_data: dict[str, Any],
    original_snapshot: CookieSnapshot,
    *,
    recovery_observation: RecoveryCookieObservation | None = None,
) -> tuple[int, frozenset[CookieSnapshotKey]]:
    """Snapshot/delta merge: write only what this process actually changed.

    Closes the Appendix A2 stale-overwrite-fresh and path-collapse hazards:

    - **Deltas (CAS-guarded for keys in the snapshot)**: cookies in the
      jar whose snapshot tuple (``value, expires, secure, http_only``)
      differs from ``original_snapshot`` are written to disk **only if**
      the on-disk value still matches the snapshot value. If disk has
      rotated since open time, a sibling process has written it; we
      preserve their write rather than clobber it with our local
      rotation. New cookies acquired during the session are written only
      when no same-key storage row exists yet; an existing row means a
      sibling acquired the same cookie first. Comparing the full snapshot
      tuple keeps attribute-only refreshes (same value, new ``expires``)
      flowing to disk, but CAS remains value-only because attribute-only
      sibling drift is routine session metadata and should not wedge later
      value rotations.
    - **Deletions (CAS-guarded)**: a key present in the snapshot but
      absent from the jar is dropped from disk **only if** the on-disk
      value still matches the snapshot value — symmetric with the
      value-update CAS above. An ``Max-Age=0`` that evicted our
      locally-expired copy must not erase the sibling's freshly-issued
      replacement.
    - **Untouched**: cookies in the jar whose tuple matches the snapshot
      are not written, so a sibling-process write to the same key
      survives. Cookies on disk that are not in the snapshot are also
      left alone (they belong to a sibling process or another path).

    Args:
        cookie_jar: Current in-memory cookie jar.
        storage_data: Mutable storage_state.json dict (modified in place).
        original_snapshot: Open-time snapshot of the same jar.

    Returns:
        Tuple of ``(updated_count, cas_rejected_keys)``:

        - ``updated_count``: cookie entries added, modified, or removed
          (drives whether the temp-write step runs).
        - ``cas_rejected_keys``: keys whose CAS check rejected a delta or
          deletion. Caller uses this to advance the baseline only for keys
          that were actually written or already matched.
    """
    decision = _cookie_merge.decide_cookie_merge(
        stored=ProfileDocument.decode(storage_data),
        observation=_cookie_jar_for_merge(cookie_jar, include_none=False),
        baseline=_cookie_jar_from_snapshot(original_snapshot),
        recovery_observation=_recovery_observation_value(recovery_observation),
    )
    for identity, kind in decision._conflicts:
        if kind == "existing":
            logger.debug(
                "Skipped CAS-guarded value update of %s on %s: disk value "
                "differs from snapshot (sibling write preserved)",
                identity.name,
                identity.domain,
            )
        else:
            logger.debug(
                "Skipped CAS-guarded value update of new cookie %s on %s: "
                "disk row already exists (sibling write preserved)",
                identity.name,
                identity.domain,
            )
    _install_decided_document(storage_data, decision.document)
    rejected = frozenset(
        CookieSnapshotKey(identity.name, identity.domain, identity.path)
        for identity in decision.rejected
    )
    return decision.updated_rows, rejected


# ==========================================================================
# SECTION 6 — WRITER OUTCOME TYPES
# Value-free status enums and records the intent writers return.
# ==========================================================================


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


# ==========================================================================
# SECTION 7 — THE INTENT WRITERS
# The temporary v0.x policy bodies for profile and sibling credential writes.
# Profile commits use the typed credential capability; cookie transactions are
# owned by ``ProfileStore`` above this compatibility layer.
# ==========================================================================


# --- CAS delta merge (behind ``save_cookies_to_storage``) -------------------


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
    ``save_cookies_to_storage``; that function remains the public,
    monkeypatchable delegate seam. The ``original_snapshot=None`` legacy-warning
    branch stays on the delegate so its ``stacklevel`` still points at the
    caller.

    This is a **CAS** intent: on lock unavailability it **fails open** (status
    quo — the snapshot/delta CAS guards preserve correctness), delegated to
    :class:`ProfileStore`'s blocking cookie transaction. The full signature (incl.
    ``recovery_observation``) and the :class:`CookieSaveResult` return with
    ``cas_rejected_keys`` are load-bearing for the PSIDTS-recovery and
    cookie-persistence baseline callers.
    """
    if path is None and resolve_auth_json_env() is not None:
        logger.debug("Skipping cookie sync: Auth loaded from NOTEBOOKLM_AUTH_JSON env var")
        return _cookie_save_return(CookieSaveResult(True), return_result=return_result)

    if path is None:
        logger.debug("Skipping cookie sync: No storage file path available")
        return _cookie_save_return(CookieSaveResult(True), return_result=return_result)

    store = ProfileStore(path)
    if original_snapshot is None:
        result = store.merge_legacy_cookie_observation(
            _cookie_jar_for_merge(cookie_jar, include_none=True)
        )
    else:
        result = store.merge_cookie_observation(
            _cookie_jar_for_merge(cookie_jar, include_none=False),
            baseline=_cookie_jar_from_snapshot(original_snapshot),
            recovery_observation=_recovery_observation_value(recovery_observation),
        )

    rejected = frozenset(
        CookieSnapshotKey(identity.name, identity.domain, identity.path)
        for identity in result.rejected
    )
    projected = CookieSaveResult(
        result.disposition in {CookieMergeDisposition.APPLIED, CookieMergeDisposition.NO_CHANGE},
        rejected,
    )
    return _cookie_save_return(projected, return_result=return_result)


# --- In-band account intent adapters (relocated from ``account.py``) ---------
# These v0.x projections delegate directly to ProfileStore. Cross-file policy,
# promotion lifecycle, and sibling cleanup live in profile_migration.


def update_account_metadata(
    storage_path: Path,
    *,
    authuser: int,
    email: str | None = None,
    only_if_absent: bool = False,
) -> bool:
    """Project the v0.x raw account arguments through the path-owned store.

    ``False`` remains reserved for an ``only_if_absent`` race lost to a
    non-empty in-band record; lock and commit failures still escape.
    """
    return bool(
        ProfileStore(storage_path).update_account(
            ProfileAccount(authuser=authuser, email=email),
            only_if_absent=only_if_absent,
        )
    )


def clear_in_band_account(storage_path: Path) -> None:
    """Project the v0.x best-effort clear through the path-owned store."""
    ProfileStore(storage_path).clear_account()


# ==========================================================================
# SECTION 7b — V0.X LEGACY ACCOUNT COMPATIBILITY ADAPTERS ONLY
# profile_migration owns resolution, sanitization, promotion, scheduling,
# atexit drain, context locking/I/O, and post-write reconciliation.
# ==========================================================================


_ACCOUNT_CONTEXT_KEY = "account"
_STORAGE_NAMESPACE_KEY = "notebooklm"
_STORAGE_NAMESPACE_VERSION = 1


def read_account_metadata_from_storage_state(storage_state: Any) -> dict[str, Any]:
    """Read in-band account metadata from parsed Playwright storage state."""
    if not isinstance(storage_state, dict):
        return {}
    namespace = storage_state.get(_STORAGE_NAMESPACE_KEY)
    if not isinstance(namespace, dict):
        return {}
    account = namespace.get(_ACCOUNT_CONTEXT_KEY)
    return account if isinstance(account, dict) else {}


def read_account_metadata(storage_path: Path | None) -> dict[str, Any]:
    """Resolve the v0.x account projection and schedule legacy promotion."""
    if storage_path is None:
        return {}
    store = ProfileStore(storage_path)
    migrator = LegacyAccountMigrator()
    resolution, compatibility = migrator._resolve_with_projection(store)
    if isinstance(resolution, LegacyAccount):
        LegacyPromotionScheduler.process_default().schedule(store, migrator)
    return compatibility


def promote_legacy_account(storage_path: Path) -> bool:
    """Promote one legacy sibling through the canonical migration service."""
    result = LegacyAccountMigrator().promote(ProfileStore(storage_path))
    return isinstance(result, Promoted)


def get_authuser_for_storage(storage_path: Path | None) -> int:
    """Return the ``authuser`` index recorded for a profile, defaulting to 0.

    Profiles without account metadata (legacy single-account installs and
    fresh logins that never set an authuser) are treated as ``authuser=0``,
    preserving existing behavior.

    Returns:
        Non-negative ``authuser`` index. Malformed values fall back to 0.
    """
    raw = read_account_metadata(storage_path).get("authuser")
    if isinstance(raw, int) and raw >= 0:
        return raw
    return 0


def get_account_email_for_storage(storage_path: Path | None) -> str | None:
    """Return the persisted account email for stable routing, if available."""
    raw = read_account_metadata(storage_path).get("email")
    if isinstance(raw, str):
        email = raw.strip()
        if email:
            return email
    return None


def resolve_account_identity(
    *,
    has_env_auth: bool,
    storage_path: Path | None = None,
    env_auth_storage_state: Any = None,
) -> dict[str, Any]:
    """Resolve the persisted ``{email, authuser}`` identity for a profile.

    Consolidates a sanitization recipe that used to be duplicated verbatim at
    ``cli/auth_runtime.py::get_auth_tokens`` and ``_app/auth_check.py::_account_info``
    (auth cross-boundary ledger shrink, follow-up to #2103): both callers read the
    in-band account record then apply the identical authuser/email cleanup — an
    ``int`` authuser clamped to ``>= 0`` (default 0; ``bool`` excluded since it is
    an ``int`` subclass), and an email stripped-or-``None``.

    The two callers differ only in WHERE the record comes from, not in what they
    do with it: env-var auth carries no profile directory, so the caller must pass
    its own already-parsed ``env_auth_storage_state`` (``_app/`` never reads
    ``os.environ`` directly, and ``cli/auth_runtime.py`` already has the CLI's
    consolidated ``read_env_auth_json()`` payload in hand by the time it gets
    here); file-based auth resolves straight from ``storage_path`` via
    :func:`read_account_metadata`.
    """
    if has_env_auth:
        meta = read_account_metadata_from_storage_state(env_auth_storage_state)
    else:
        meta = read_account_metadata(storage_path)
    raw_email = meta.get("email")
    email = raw_email.strip() if isinstance(raw_email, str) else ""
    raw_authuser = meta.get("authuser")
    authuser = raw_authuser if type(raw_authuser) is int and raw_authuser >= 0 else 0
    return {"email": email or None, "authuser": authuser}


def write_account_metadata(storage_path: Path, *, authuser: int, email: str | None = None) -> None:
    """Persist account metadata, then reconcile the legacy sibling."""
    store = ProfileStore(storage_path)
    AccountMetadataWriter(store, LegacyAccountMigrator()).write(
        ProfileAccount(authuser=authuser, email=email)
    )


def clear_account_metadata(storage_path: Path | None) -> None:
    """Clear account metadata, then reconcile the legacy sibling."""
    if storage_path is None:
        return
    store = ProfileStore(storage_path)
    AccountMetadataWriter(store, LegacyAccountMigrator()).clear()


# --- Browser-capture re-mint (relocated from browser_capture.py) --------


def replace_from_remint(
    path: Path,
    captured_state: dict[str, Any],
    *,
    carry_account: bool,
    include_domains: set[str] | None = None,
) -> WriteOutcome:
    """Project the compatibility re-mint writer through ``ProfileStore``."""
    source = ProfileDocument.decode(dict(captured_state))
    selection = DomainSelection(
        include_domains=frozenset(include_domains or ()),
        include_optional=False,
    )
    result = ProfileStore(path).replace_from_remint(
        RemintWriteRequest(
            source=source,
            carry_account=carry_account,
            domain_selection=selection,
        )
    )
    if result.status is ReplaceStatus.APPLIED:
        return WriteOutcome(WriteStatus.OK)
    if result.status is ReplaceStatus.LOCK_UNAVAILABLE:
        return WriteOutcome(WriteStatus.LOCK_UNAVAILABLE)
    raise AssertionError("unreachable replace status")


# --- Login / import full-replace -------------------------------------------
# Hoisted from the CLI ``cli/services/login`` and ``cli/_cookie_import``
# writers — the #2086 filter + revalidation moved HERE.


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
    del io_policy
    source = ProfileDocument.decode(dict(state))
    selection = DomainSelection(
        include_domains=frozenset(include_domains or ()),
        include_optional=include_optional,
    )
    directive: KeepAccount | SetAccount | ClearAccount
    if account is KEEP_ACCOUNT:
        directive = KeepAccount()
    elif isinstance(account, AccountRecord):
        directive = SetAccount(ProfileAccount(account.authuser, account.email))
    else:
        directive = ClearAccount()
    request = LoginWriteRequest(
        source=source,
        domain_selection=selection,
        account=directive,
        backup=backup,
    )
    store = ProfileStore(path)
    result = LoginProfileWriter(store, LegacyAccountMigrator()).write(request)

    if result.status is ReplaceStatus.LOCK_UNAVAILABLE:
        return LoginWriteOutcome(LoginWriteStatus.LOCK_UNAVAILABLE)
    if result.status is ReplaceStatus.REQUIRED_COOKIES_DROPPED:
        return LoginWriteOutcome(
            LoginWriteStatus.REQUIRED_COOKIES_DROPPED,
            missing_required=result.missing_required,
            present_names=result.present_names,
        )

    return LoginWriteOutcome(LoginWriteStatus.OK, backup_path=result.backup_path)


# --- Master-token writers (relocated from ``master_token.py``) --------------


def persist_minted_jar(
    path: Path,
    jar: httpx.Cookies,
    *,
    email: str | None,
    force: bool = False,
    refuse_unknown_owner: bool = True,
) -> None:
    """Snapshot and delegate one freshly minted full-session replacement."""
    required_order = ("SID", "APISID", "SAPISID")
    ordered_cookies = sorted(
        jar.jar,
        key=lambda cookie: (
            required_order.index(cookie.name)
            if cookie.name in required_order
            else len(required_order),
            cookie.name,
            cookie.domain,
            cookie.path or "/",
        ),
    )
    cookies = CookieJar(
        Cookie(
            name=cookie.name,
            value=cast(str, cookie.value),
            domain=cookie.domain,
            path=cookie.path or "/",
            expires=cookie.expires,
            http_only=_cookie_is_http_only(cookie),
            secure=cookie.secure,
            same_site="None",
        )
        for cookie in ordered_cookies
    )
    request = MintedSessionWriteRequest(cookies, email, force, refuse_unknown_owner)
    refusal_message: str | None = None
    try:
        ProfileStore(path).replace_minted_session(request)
    except _MintedSessionOwnershipRefused as exc:
        refusal_message = str(exc)
    if refusal_message is not None:
        raise MasterTokenError(refusal_message)


def write_master_token(path: Path, *, email: str, master_token: str, android_id: str) -> None:
    """Persist a ``master_token.json`` record at mode 0600 (full-account credential).

    Relocated from ``master_token.write_master_token``, now routed through the
    typed master-token commit spine (atomic + fsync-durable + temp cleanup) and
    guarded by a bounded sibling ``.master_token.json.lock`` — it was previously lockless
    (part of [storage-F5]). RMW intent: **fails closed**.
    """
    MasterTokenFile(path).write(
        MasterToken(email=email, android_id=android_id, secret=master_token)
    )
