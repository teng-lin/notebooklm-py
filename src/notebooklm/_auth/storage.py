"""Cookie storage snapshot/delta persistence helpers for authentication."""

from __future__ import annotations

import contextlib
import errno
import json
import logging
import os
import sys
import warnings
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple, TypeAlias

import httpx

from .._atomic_io import atomic_write_json
from . import cookie_policy as _cookie_policy
from . import cookies as _auth_cookies

logger = logging.getLogger("notebooklm.auth")

CookieKey: TypeAlias = _auth_cookies.CookieKey
_cookie_is_http_only = _auth_cookies._cookie_is_http_only
_cookie_key_variants = _auth_cookies._cookie_key_variants
_cookie_to_storage_state = _auth_cookies._cookie_to_storage_state
_find_cookie_for_storage = _auth_cookies._find_cookie_for_storage
_is_allowed_cookie_domain = _cookie_policy._is_allowed_cookie_domain


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


@dataclass(frozen=True)
class CookieSaveResult:
    """Detailed result for callers that need to maintain a save baseline."""

    ok: bool
    cas_rejected_keys: frozenset[CookieSnapshotKey] = frozenset()


_LOCK_CONTENTION_ERRNOS = {errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES}


@contextlib.contextmanager
def _file_lock(lock_path: Path, *, blocking: bool, log_prefix: str) -> Iterator[str]:
    """Cross-process exclusive lock on ``lock_path``.

    Yields one of:
      - ``"held"``  — the lock is held; release it on exit.
      - ``"contended"`` — non-blocking acquire saw the lock held elsewhere.
        Only ever yielded when ``blocking=False``.
      - ``"unavailable"`` — lock infrastructure failed (cannot mkdir, cannot
        open the sentinel, NFS without flock support). Caller should
        **fail open** (proceed without coordination) rather than retry forever.

    Wrappers translate this tristate into bool. Distinguishing contention from
    infrastructure failure matters: a non-blocking caller should **skip** on
    contention (someone else is rotating) but **proceed** on infrastructure
    failure (otherwise a read-only auth dir would permanently suppress
    rotation).
    """
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as exc:
        # Read-only directory, permission denied, ENOSPC, etc. Yield
        # "unavailable" so the wrapper can fail open.
        logger.debug("%s: lock file unavailable %s (%s)", log_prefix, lock_path, exc)
        yield "unavailable"
        return
    locked = False
    state = "unavailable"
    try:
        try:
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(fd, msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                op = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
                fcntl.flock(fd, op)
            locked = True
            state = "held"
        except OSError as exc:
            if not blocking and exc.errno in _LOCK_CONTENTION_ERRNOS:
                # Non-blocking acquire bounced because another process holds
                # the lock — this is the "skip" signal.
                state = "contended"
                logger.debug("%s: lock contended (%s)", log_prefix, exc)
            else:
                # NFS without flock, kernel quirk, etc. Caller should fail open.
                state = "unavailable"
                logger.debug("%s: lock op unavailable (%s)", log_prefix, exc)
        yield state
    finally:
        if locked:
            try:
                if sys.platform == "win32":
                    import msvcrt

                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError as exc:
                logger.debug("%s: failed to release file lock (%s)", log_prefix, exc)
        os.close(fd)


# Dedupe contract (T7.G7): best-effort under threads, exactly-once on a single
# event loop. ``_file_lock_exclusive`` below reads ``_FLOCK_UNAVAILABLE_WARNED``
# and sets it to ``True`` in one synchronous block with no intervening
# ``await``, so concurrent coroutines on one loop cannot interleave between
# the check and the set — the warning fires exactly once per process. Under
# genuine OS threads (out of scope per the documented concurrency contract),
# duplicate warnings are possible. We accept that rather than serialize a
# logging side-effect behind a lock for an unsupported configuration.
#
# Note: ``functools.lru_cache`` and ``logging.LoggerAdapter`` do NOT solve
# this — ``lru_cache`` memoizes return values, not the ``logger.warning``
# side-effect; ``LoggerAdapter`` only rewrites records, it does not filter
# duplicates.
_FLOCK_UNAVAILABLE_WARNED = False


@contextlib.contextmanager
def _file_lock_exclusive(lock_path: Path) -> Iterator[None]:
    """Blocking cross-process exclusive lock on ``lock_path``.

    Multiple Python processes that all save to the same ``storage_state.json``
    (e.g. a long-running ``NotebookLMClient(keepalive=...)`` worker plus a
    cron-driven ``notebooklm auth refresh``) would otherwise race on the read-
    merge-write cycle and lose updates. The lock is held on a sentinel file
    sibling to the storage file (``.storage_state.json.lock``), since locking
    the storage file itself would interfere with the atomic temp-rename below.

    The lock is per-process: threads within one process aren't serialized —
    that's the intra-process ``threading.Lock`` in ``ClientCore``. If the
    lock can't be acquired (e.g. NFS where flock semantics vary, read-only
    parent dir, fd exhaustion), the save proceeds anyway; correctness in
    that mode is best-effort and relies on the snapshot/delta CAS guards in
    :func:`_merge_cookies_with_snapshot` alone. The first time this
    fallback fires per process emits a WARNING so operators learn their
    deployment is running without cross-process coordination.
    """
    global _FLOCK_UNAVAILABLE_WARNED
    with _file_lock(lock_path, blocking=True, log_prefix="save_cookies_to_storage") as state:
        if state == "unavailable" and not _FLOCK_UNAVAILABLE_WARNED:
            _FLOCK_UNAVAILABLE_WARNED = True
            logger.warning(
                "Cross-process file lock unavailable at %s; cookie saves will "
                "proceed without cross-process coordination and rely solely on "
                "snapshot/delta CAS guards. Common causes: NFS without flock "
                "support, read-only parent directory, fd exhaustion. (Logged "
                "once per process.)",
                lock_path,
            )
        yield


def snapshot_cookie_jar(cookie_jar: httpx.Cookies) -> CookieSnapshot:
    """Capture an open-time snapshot of an httpx cookie jar.

    Snapshots are the input to the dirty-flag/delta merge in
    :func:`save_cookies_to_storage`: at save time, only cookies whose
    in-memory value differs from the snapshot — plus cookies absent from
    the jar but present in the snapshot (deletions) — are propagated to
    disk. Cookies the in-process code never touched are left to whatever
    a sibling process may have written (closes the §3.4.1
    stale-overwrite-fresh hazard).

    The key shape is path-aware ``(name, domain, path)`` (also closes
    §3.4.2). Cookies with no name or no domain are skipped — the storage
    format requires both.

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
    variants = {key}
    if key.domain.startswith("."):
        variants.add(CookieSnapshotKey(key.name, key.domain[1:], key.path))
    else:
        variants.add(CookieSnapshotKey(key.name, f".{key.domain}", key.path))
    return variants


def _stored_cookie_snapshot_key(stored_cookie: dict[str, Any]) -> CookieSnapshotKey | None:
    """Build a path-aware snapshot key from a Playwright storage_state cookie."""
    name = stored_cookie.get("name")
    domain = stored_cookie.get("domain", "")
    if not name or not domain:
        return None
    path = stored_cookie.get("path") or "/"
    return CookieSnapshotKey(name, domain, path)


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
      race documented in ``docs/auth-keepalive.md`` §3.4.1 and emits a
      ``DeprecationWarning``. Kept only as a public-API back-compat shim
      for callers outside this repo; every first-party caller passes
      ``original_snapshot``.
    - **Snapshot/delta (``original_snapshot`` provided)**: only cookies
      whose in-memory persisted tuple differs from the snapshot are written, and
      cookies present in the snapshot but no longer in the jar are
      deleted from disk. Cookies the in-process code never touched are
      left untouched on disk so a sibling-process write survives.
      Path-aware ``(name, domain, path)`` keys are used here (also closes
      §3.4.2).

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
    if (
        not path
        and "NOTEBOOKLM_AUTH_JSON" in os.environ
        and os.environ["NOTEBOOKLM_AUTH_JSON"].strip()
    ):
        logger.debug("Skipping cookie sync: Auth loaded from NOTEBOOKLM_AUTH_JSON env var")
        return _cookie_save_return(CookieSaveResult(True), return_result=return_result)

    if not path:
        logger.debug("Skipping cookie sync: No storage file path available")
        return _cookie_save_return(CookieSaveResult(True), return_result=return_result)

    if original_snapshot is None:
        warnings.warn(
            "save_cookies_to_storage called without original_snapshot; the "
            "legacy full-merge path is vulnerable to the stale-overwrite-fresh "
            "race (docs/auth-keepalive.md §3.4.1). Pass an original_snapshot "
            "captured via snapshot_cookie_jar() at jar-open time.",
            DeprecationWarning,
            stacklevel=2,
        )

    lock_path = path.with_name(f".{path.name}.lock")
    with _file_lock_exclusive(lock_path):
        if not path.exists():
            logger.debug("Skipping cookie sync: Storage file not found at %s", path)
            return _cookie_save_return(CookieSaveResult(False), return_result=return_result)

        try:
            storage_data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("Failed to read storage state for cookie sync: %s", e)
            return _cookie_save_return(CookieSaveResult(False), return_result=return_result)

        cookies = storage_data.get("cookies") if isinstance(storage_data, dict) else None
        if not isinstance(cookies, list) or any(not isinstance(cookie, dict) for cookie in cookies):
            logger.warning(
                "storage_state at %s has an invalid 'cookies' key/payload; "
                "rotated cookies will not be persisted",
                path,
            )
            return _cookie_save_return(CookieSaveResult(False), return_result=return_result)

        if original_snapshot is None:
            updated_count = _merge_cookies_legacy(cookie_jar, storage_data)
            cas_rejected_keys: frozenset[CookieSnapshotKey] = frozenset()
        else:
            updated_count, cas_rejected_keys = _merge_cookies_with_snapshot(
                cookie_jar, storage_data, original_snapshot
            )

        if updated_count == 0:
            # A CAS rejection with no other successful work means disk does
            # not reflect our intent; the caller must not advance baseline.
            return _cookie_save_return(
                CookieSaveResult(not cas_rejected_keys, cas_rejected_keys),
                return_result=return_result,
            )

        try:
            atomic_write_json(path, storage_data)
            logger.debug("Successfully synced %d refreshed cookies to %s", updated_count, path)
            # Even on a successful disk write, if any CAS arm rejected work,
            # disk diverges from ``post`` for at least one key — caller must
            # not advance baseline.
            return _cookie_save_return(
                CookieSaveResult(not cas_rejected_keys, cas_rejected_keys),
                return_result=return_result,
            )
        except Exception as e:
            logger.warning("Failed to write updated cookies to %s: %s", path, e)
            return _cookie_save_return(CookieSaveResult(False), return_result=return_result)


def _merge_cookies_legacy(cookie_jar: httpx.Cookies, storage_data: dict[str, Any]) -> int:
    """Legacy merge: trust in-memory whenever it differs from disk.

    Vulnerable to the stale-overwrite-fresh race (§3.4.1). Kept only for
    callers that have not yet opted into snapshot semantics. New callers
    must pass ``original_snapshot`` to :func:`save_cookies_to_storage`.

    Returns:
        Number of cookie entries added or modified in ``storage_data``.
    """
    cookies_by_key: dict[CookieKey, Any] = {
        (cookie.name, cookie.domain, cookie.path or "/"): cookie
        for cookie in cookie_jar.jar
        if cookie.name and cookie.domain and _is_allowed_cookie_domain(cookie.domain)
    }

    updated_count = 0
    stored_keys: set[CookieKey] = set()
    for stored_cookie in storage_data["cookies"]:
        name = stored_cookie.get("name")
        domain = stored_cookie.get("domain", "")
        if not name or not domain:
            continue

        key: CookieKey = (name, domain, stored_cookie.get("path") or "/")
        stored_keys.update(_cookie_key_variants(key))
        refreshed_cookie = _find_cookie_for_storage(cookies_by_key, key, stored_cookie.get("value"))
        if refreshed_cookie is None:
            continue

        new_expires = refreshed_cookie.expires if refreshed_cookie.expires is not None else -1
        changed = (
            stored_cookie.get("value") != refreshed_cookie.value
            or stored_cookie.get("expires") != new_expires
        )
        if changed:
            stored_cookie["value"] = refreshed_cookie.value
            stored_cookie["expires"] = new_expires
            # Normalize present-but-empty ``"path": ""`` to ``"/"`` so the row
            # we write matches the path normalization used to build the
            # identity key one block up (and used by every loader). Without
            # the trailing ``or "/"`` an on-disk row with ``"path": ""`` would
            # survive across save cycles while every other code path treats
            # it as ``"/"``.
            stored_cookie["path"] = refreshed_cookie.path or stored_cookie.get("path") or "/"
            stored_cookie["secure"] = refreshed_cookie.secure
            stored_cookie["httpOnly"] = _cookie_is_http_only(refreshed_cookie)
            updated_count += 1

    for key, cookie in cookies_by_key.items():
        if key in stored_keys:
            continue
        storage_data["cookies"].append(_cookie_to_storage_state(cookie))
        updated_count += 1

    return updated_count


def _merge_cookies_with_snapshot(
    cookie_jar: httpx.Cookies,
    storage_data: dict[str, Any],
    original_snapshot: CookieSnapshot,
) -> tuple[int, frozenset[CookieSnapshotKey]]:
    """Snapshot/delta merge: write only what this process actually changed.

    Closes §3.4.1 (stale-overwrite-fresh) and §3.4.2 (path collapse):

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
    current_snapshot = snapshot_cookie_jar(cookie_jar)

    # Path-aware index of jar cookies for delta application. Restricting to
    # _is_allowed_cookie_domain matches the legacy save's allowlist gate so
    # this PR doesn't inadvertently widen the persisted-domain set.
    # Filter ``cookie.value is not None`` to mirror ``snapshot_cookie_jar``: a
    # value-less cookie is treated as a deletion (absent from this index, absent
    # from ``current_snapshot``) rather than a delta that would write ``null``
    # to disk.
    cookies_by_snapshot_key = {
        CookieSnapshotKey(cookie.name, cookie.domain, cookie.path or "/"): cookie
        for cookie in cookie_jar.jar
        if (
            cookie.name
            and cookie.domain
            and cookie.value is not None
            and _is_allowed_cookie_domain(cookie.domain)
        )
    }

    deltas: dict[CookieSnapshotKey, Any] = {}
    for snapshot_key, cookie in cookies_by_snapshot_key.items():
        if original_snapshot.get(snapshot_key) != current_snapshot.get(snapshot_key):
            deltas[snapshot_key] = cookie

    deletion_candidates: set[CookieSnapshotKey] = {
        snapshot_key
        for snapshot_key in original_snapshot
        if snapshot_key not in current_snapshot
        # Only delete cookies the merge would otherwise be allowed to write.
        # Snapshot may include sibling-product domains the allowlist filters
        # out at write time; treating those as deletions would silently drop
        # disk entries we never persisted to begin with.
        and _is_allowed_cookie_domain(snapshot_key.domain)
    }

    updated_count = 0
    cas_rejected_keys: set[CookieSnapshotKey] = set()

    # Apply deltas + deletions to the existing storage entries in place.
    new_cookies: list[dict[str, Any]] = []
    matched_delta_keys: set[CookieSnapshotKey] = set()
    for stored_cookie in storage_data["cookies"]:
        stored_key = _stored_cookie_snapshot_key(stored_cookie)
        if stored_key is None:
            new_cookies.append(stored_cookie)
            continue

        # Find the delta (or deletion) that maps to this stored entry.
        # Match leading-dot domain variants so e.g. snapshot
        # ``.accounts.google.com`` lines up with stored ``accounts.google.com``.
        # A delta wins over a deletion: if the same stored entry matches
        # both (which can happen when httpx normalized one variant), we
        # prefer to update rather than drop, because dropping would lose
        # the rotation we just applied.
        matched_delta_cookie = None
        matched_delta_key: CookieSnapshotKey | None = None
        for variant in _cookie_snapshot_key_variants(stored_key):
            if variant in deltas:
                matched_delta_cookie = deltas[variant]
                matched_delta_key = variant
                break

        if matched_delta_cookie is not None:
            if matched_delta_key is None:  # pragma: no cover - loop invariant
                raise RuntimeError("matched_delta_cookie set without matched_delta_key")
            # CAS-guard for value updates: if our snapshot had this key in any
            # leading-dot variant and disk's current value differs from the
            # snapshot value, a sibling process has rewritten the row between
            # our open and our save. Preserve their write rather than clobber,
            # unless disk has already converged to our current value; in that
            # case the save intent is satisfied and the caller may advance its
            # baseline.
            # Variant-aware lookup mirrors the delta match above: if the snapshot
            # was keyed on ``accounts.google.com`` but the matched delta key is
            # the leading-dot variant, a plain ``.get(matched_delta_key)`` would
            # miss the entry and silently bypass the CAS.
            snapshot_entry = next(
                (
                    original_snapshot[variant]
                    for variant in _cookie_snapshot_key_variants(matched_delta_key)
                    if variant in original_snapshot
                ),
                None,
            )
            stored_value = stored_cookie.get("value")
            if (
                snapshot_entry is not None
                and stored_value != snapshot_entry.value
                and stored_value != matched_delta_cookie.value
            ):
                logger.debug(
                    "Skipped CAS-guarded value update of %s on %s: disk value "
                    "differs from snapshot (sibling write preserved)",
                    matched_delta_key.name,
                    matched_delta_key.domain,
                )
                cas_rejected_keys.add(matched_delta_key)
                matched_delta_keys.add(matched_delta_key)
                new_cookies.append(stored_cookie)
                continue
            if snapshot_entry is None and stored_value != matched_delta_cookie.value:
                logger.debug(
                    "Skipped CAS-guarded value update of new cookie %s on %s: "
                    "disk row already exists (sibling write preserved)",
                    matched_delta_key.name,
                    matched_delta_key.domain,
                )
                cas_rejected_keys.add(matched_delta_key)
                matched_delta_keys.add(matched_delta_key)
                new_cookies.append(stored_cookie)
                continue
            new_expires = (
                matched_delta_cookie.expires if matched_delta_cookie.expires is not None else -1
            )
            stored_cookie["value"] = matched_delta_cookie.value
            stored_cookie["expires"] = new_expires
            # Mirror :func:`_merge_cookies_legacy`: ``or "/"`` normalizes a
            # present-but-empty ``"path": ""`` so the written row matches the
            # path normalization used by the identity key and every loader.
            stored_cookie["path"] = matched_delta_cookie.path or stored_cookie.get("path") or "/"
            stored_cookie["secure"] = matched_delta_cookie.secure
            stored_cookie["httpOnly"] = _cookie_is_http_only(matched_delta_cookie)
            matched_delta_keys.add(matched_delta_key)
            updated_count += 1
            new_cookies.append(stored_cookie)
            continue

        deletion_match = next(
            (
                variant
                for variant in _cookie_snapshot_key_variants(stored_key)
                if variant in deletion_candidates
            ),
            None,
        )
        if deletion_match is not None:
            # CAS-guard: only drop the disk row if its value still matches
            # what we observed at snapshot time. A sibling process may have
            # rewritten this key between our open and our save; clobbering
            # their fresh value with our local eviction would resurrect the
            # exact stale-overwrite-fresh hazard the snapshot path exists
            # to close (just inverted — deletion-of-fresh instead of
            # value-write-of-stale).
            snapshot_value = original_snapshot[deletion_match].value
            if stored_cookie.get("value") == snapshot_value:
                updated_count += 1
                continue  # drop the entry from disk
            cas_rejected_keys.add(deletion_match)

        new_cookies.append(stored_cookie)

    # Append delta cookies that didn't match any existing storage entry
    # (genuinely new cookies acquired during the session).
    for snapshot_key, cookie in deltas.items():
        if snapshot_key in matched_delta_keys:
            continue
        new_cookies.append(_cookie_to_storage_state(cookie))
        updated_count += 1

    storage_data["cookies"] = new_cookies
    return updated_count, frozenset(cas_rejected_keys)
