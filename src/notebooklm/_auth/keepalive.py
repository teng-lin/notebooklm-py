"""Keepalive ``RotateCookies`` poke helpers for authentication.

This private module hosts the rotation throttle + POST that
``notebooklm.auth`` previously owned at module level. ``notebooklm.auth`` keeps
re-exporting compatibility names, but production no longer mirrors facade-level
rebindings; tests that substitute keepalive internals should patch this
canonical module directly.

Logger name is pinned to ``"notebooklm.auth"`` (NOT ``__name__``) so existing
``caplog`` assertions targeting ``notebooklm.auth`` keep matching the records
emitted from the moved bodies.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import random
import threading
import time
import weakref
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx

from . import paths as _auth_paths
from . import storage as _auth_storage

logger = logging.getLogger("notebooklm.auth")


# --- Keepalive constants -----------------------------------------------------
# Google's __Secure-1PSIDTS / __Secure-3PSIDTS cookies are the rotating freshness
# partners of __Secure-1PSID / __Secure-3PSID. Their server-side validity window
# is short (minutes-to-hours scale) and Google only emits a rotated value when
# the client asks the identity surface to rotate. Pure RPC traffic against
# the app host never triggers rotation, so a long-lived storage_state
# silently stales out and every subsequent call fails with the
# "Authentication expired or invalid" redirect (see issue #312).
#
# We POST to ``accounts.google.com/RotateCookies`` — the dedicated rotation
# endpoint Chrome itself calls for legacy cookie rotation. Empirically validated
# against both DBSC-bound (Playwright-minted) and unbound (Firefox-imported)
# profiles in #345: a single POST returns 200 and sets fresh
# ``__Secure-1PSIDTS`` / ``__Secure-3PSIDTS`` for either session type. The
# response body declares the next-rotation interval (`["identity.hfcr",600]` —
# 10 minutes), which sets the floor for how often this is worth firing.
KEEPALIVE_ROTATE_URL = "https://accounts.google.com/RotateCookies"
_KEEPALIVE_ROTATE_HEADERS = {
    "Content-Type": "application/json",
    "Origin": "https://accounts.google.com",
}
# Observed unbound RotateCookies request body — a placeholder pair Chrome sends
# when there is no DBSC binding token to attest. Validated across Gemini-API and
# the in-house experiments referenced in #345; kept in one place so it can be
# changed if Google ever changes the contract.
_KEEPALIVE_ROTATE_BODY = '[000,"-0000000000000000000"]'
_KEEPALIVE_POKE_TIMEOUT = 15.0
# Skip the poke if storage_state.json was rewritten within this window — protects
# accounts.google.com from rapid CLI loops (e.g. 10 sequential `notebooklm`
# invocations) that would each fire their own rotation. Google's own declared
# rotation cadence is 600 s, so 60 s is well under the useful interval.
_KEEPALIVE_RATE_LIMIT_SECONDS = 60.0
# Sub-second drift between ``time.time()`` and filesystem mtime can land a
# freshly-written file fractionally in the future on some platforms (notably
# Windows + older Python where the clock is coarser than NTFS mtime). Tolerate
# that without re-opening the "future mtime wedges the guard" bug.
_KEEPALIVE_PRECISION_TOLERANCE = 2.0

# Env-var name lives in ``notebooklm._auth.paths``; aliased here so the
# keepalive bodies can reference it without an extra module hop.
NOTEBOOKLM_DISABLE_KEEPALIVE_POKE_ENV = _auth_paths.NOTEBOOKLM_DISABLE_KEEPALIVE_POKE_ENV

# --- The single RotateCookies wire contract ----------------------------------
# Every rotation path — the layer-1/layer-2 keepalive pokes (async, live
# client), the file-based inline PSIDTS recovery, the in-memory
# browser-extraction recovery, and the master-token mint's completing leg —
# sends exactly this request through :func:`_rotate_post` /
# :func:`_rotate_post_sync` below. There is ONE contract; the sync/async pair
# exists only because httpx splits ``Client``/``AsyncClient``. A change to
# Google's RotateCookies protocol is made here and nowhere else (enforced by
# ``tests/_guardrails/test_rotate_wire_contract.py``).
#
# ``follow_redirects=True`` is defensive: empirically RotateCookies answers 200
# directly with the rotated Set-Cookie, but if Google ever routes a 30x through
# an identity hop we still pick up cookies from the terminal response.
_ROTATE_POST_KWARGS: dict[str, Any] = {
    "headers": _KEEPALIVE_ROTATE_HEADERS,
    "content": _KEEPALIVE_ROTATE_BODY,
    "follow_redirects": True,
    "timeout": _KEEPALIVE_POKE_TIMEOUT,
}


async def _rotate_post(client: httpx.AsyncClient) -> httpx.Response:
    """THE async ``RotateCookies`` POST. Wire only — no guards, no swallowing.

    Raises ``httpx.HTTPError`` on transport failure *and* on a 4xx/5xx status:
    httpx does not auto-raise on error statuses, and without the explicit
    ``raise_for_status`` a 429 or 5xx from Google would log nothing while the
    caller proceeds assuming the rotation happened.
    """
    response = await client.post(KEEPALIVE_ROTATE_URL, **_ROTATE_POST_KWARGS)
    response.raise_for_status()
    return response


def _rotate_post_sync(client: httpx.Client) -> httpx.Response:
    """Sync twin of :func:`_rotate_post` for the one-shot recovery clients."""
    response = client.post(KEEPALIVE_ROTATE_URL, **_ROTATE_POST_KWARGS)
    response.raise_for_status()
    return response


def _rotation_http_client(jar: httpx.Cookies) -> httpx.Client:
    """Ephemeral sync client for a one-shot rotation against a prepared jar.

    ``httpx.Client(cookies=jar)`` copies the source jar into a private client
    jar; the rotated ``Set-Cookie`` values land in ``client.cookies``, never in
    ``jar`` — callers read the client's jar after the POST.
    """
    return httpx.Client(cookies=jar, follow_redirects=True, timeout=_KEEPALIVE_POKE_TIMEOUT)


# In-process state for rotation throttling, keyed per-profile and per-loop.
#
# - Per-profile (``storage_path``) so a rotation against profile A doesn't
#   suppress profile B for the rate-limit window. A ``None`` key represents
#   env-var auth.
# - Per event loop because ``asyncio.Lock`` is loop-bound: a lock created in
#   loop X cannot be safely awaited from loop Y. Multiple ``asyncio.run()``
#   invocations in the same process, or worker threads each running their
#   own loop, would otherwise trip ``RuntimeError`` or leave waiters in
#   inconsistent state.
#
# The outer registry is a ``WeakKeyDictionary`` keyed on the loop *object* (not
# its ``id()``): when a loop is garbage-collected, its inner dict is reclaimed
# automatically. This bounds the lock cache for hosts that repeatedly create
# short-lived loops, and avoids the ``id()``-reuse hazard where a closed loop's
# stale lock could be returned to a new loop that happens to allocate at the
# same address.
#
# ``_POKE_STATE_LOCK`` (sync ``threading.Lock``) protects two module-level
# operations that must be atomic across threads:
#   1. ``_get_poke_lock``: get-or-create the per-(loop, profile) async lock
#      so two threads with their own loops don't race on dict insertion.
#   2. ``_try_claim_rotation``: atomic check-and-stamp of the per-profile
#      timestamp. Without this, two direct ``_rotate_cookies`` callers (e.g.
#      two layer-2 keepalive loops on the same profile, or a layer-1 +
#      layer-2 pair on different event loops) can each read a stale 0.0
#      and both fire the POST.
# It is held briefly, never across an ``await``, so it cannot deadlock against
# any asyncio primitive.
_POKE_STATE_LOCK = threading.Lock()
_POKE_LOCKS_BY_LOOP: weakref.WeakKeyDictionary[Any, dict[Path | None, asyncio.Lock]] = (
    weakref.WeakKeyDictionary()
)
# Monotonic timestamp of the last in-process poke *attempt* (success or
# failure), keyed by storage_path. Stamped under ``_POKE_STATE_LOCK`` inside
# ``_try_claim_rotation`` so the check-and-set is atomic across event loops
# and across direct ``_rotate_cookies`` callers. Failure-stampede protection
# comes for free: even a POST that times out has already claimed the slot,
# so 10 fanned-out callers don't each wait 15 s on a hung server.
_LAST_POKE_ATTEMPT_MONOTONIC: dict[Path | None, float] = {}

# Rotation sentinel path lives in ``notebooklm._auth.paths``; aliased here for
# white-box callers that reach ``notebooklm.auth._rotation_lock_path``.
_rotation_lock_path = _auth_paths._rotation_lock_path
# Canonicalization helper shared with the refresh flock derivation ([refresh-5]):
# the poke lock registry and the rotation throttle map key on the CANONICAL
# storage path so relative / symlinked / ``~`` representations of one file
# collapse to a single dedupe slot.
canonical_storage_key = _auth_paths.canonical_storage_key

# Cross-process file-lock primitives live in ``_auth.storage``. Aliased into
# this module's namespace so the keepalive bodies resolve them locally; tests
# that need to substitute the lock primitive should patch
# ``notebooklm._auth.keepalive._file_lock`` directly.
_file_lock = _auth_storage._file_lock


def _get_poke_lock(storage_path: Path | None) -> asyncio.Lock:
    """Return the ``asyncio.Lock`` for ``(running event loop, storage_path)``.

    Lazily created on first call from each loop/profile pair so the lock binds
    to the current loop. The dict mutation runs under the sync state lock so
    concurrent threads with their own loops don't tear the registry.
    """
    key = canonical_storage_key(storage_path)
    loop = asyncio.get_running_loop()
    with _POKE_STATE_LOCK:
        per_loop = _POKE_LOCKS_BY_LOOP.get(loop)
        if per_loop is None:
            per_loop = {}
            _POKE_LOCKS_BY_LOOP[loop] = per_loop
        lock = per_loop.get(key)
        if lock is None:
            lock = asyncio.Lock()
            per_loop[key] = lock
        return lock


def _try_claim_rotation(storage_path: Path | None) -> bool:
    """Atomic check-and-claim of the per-profile rotation slot.

    Returns ``True`` if the caller may proceed with the POST, ``False`` if
    another in-process call has claimed the slot within the rate-limit
    window. The claim and the timestamp update happen under one sync lock,
    so this is safe across event loops and across direct
    ``_rotate_cookies`` callers (layer-2 keepalive loops, etc.) — neither
    of which holds the per-loop async lock used by layer-1 ``_poke_session``.
    """
    key = canonical_storage_key(storage_path)
    with _POKE_STATE_LOCK:
        last = _LAST_POKE_ATTEMPT_MONOTONIC.get(key, 0.0)
        now = time.monotonic()
        if last > 0 and (now - last) < _KEEPALIVE_RATE_LIMIT_SECONDS:
            return False
        _LAST_POKE_ATTEMPT_MONOTONIC[key] = now
        return True


@contextlib.contextmanager
def _file_lock_try_exclusive(lock_path: Path) -> Iterator[bool]:
    """Non-blocking exclusive flock. Yields ``True`` if caller should proceed.

    Mirrors :func:`_file_lock_exclusive` but with ``LOCK_NB`` semantics:
      - genuine contention (another process holds the lock) → yield ``False``,
        caller skips its work (the holder is rotating; we don't need to)
      - lock infrastructure unavailable (read-only dir, NFS without flock,
        permission denied) → yield ``True``, caller **fails open** and
        proceeds without coordination, since waiting forever for an
        unworkable lock would permanently suppress rotation.
    """
    with _file_lock(lock_path, blocking=False, log_prefix="rotate lock") as state:
        # "held" → True (proceed, we own it); "unavailable" → True (fail open);
        # "contended" → False (someone else is rotating, skip).
        yield state != "contended"


async def _wait_for_refresh_holder(lock_path: Path) -> bool:
    """Bounded async wait for another PROCESS's refresh flock to release.

    The refresh-cmd flock is held by the winning process across its
    ``_run_refresh_cmd`` subprocess. A loser that fails the non-blocking acquire
    must not reload immediately: the winner has not yet written the fresh
    cookies, so an immediate reload re-reads the STALE file and the loser fails
    even though a refresh is moments away. Poll the flock with jittered async
    backoff (``asyncio.sleep`` keeps the event loop live) until it frees —
    meaning the winner finished writing — then let the caller reload.

    Reuses the shared bounded-acquire tuning (``storage`` deadline/initial/max
    delay). Returns ``True`` when the holder released (or the lock infra is
    unworkable → fail open) within the deadline, ``False`` on timeout (the caller
    falls back to a best-effort stale reload). No subprocess runs, no epoch bump.
    """
    deadline = time.monotonic() + _auth_storage._LOCK_ACQUIRE_DEADLINE_SECONDS
    delay = _auth_storage._LOCK_ACQUIRE_INITIAL_DELAY_SECONDS
    while True:
        with _file_lock_try_exclusive(lock_path) as acquired:
            if acquired:
                # Holder released (or the lock is unworkable → fail open): the
                # freshly-written file is now the best we can reload.
                return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            logger.debug("refresh flock still held at the bounded deadline; reloading best-effort")
            return False
        # Jittered exponential backoff, async so the event loop is not frozen.
        sleep_for = min(delay + random.uniform(0.0, delay), remaining)
        await asyncio.sleep(sleep_for)
        delay = min(delay * 2, _auth_storage._LOCK_ACQUIRE_MAX_DELAY_SECONDS)


def _is_recently_rotated(storage_path: Path | None) -> bool:
    """Return True if ``storage_path`` was modified within the rate-limit window.

    A meaningfully-future mtime (clock skew, NTP step, restored file, NFS drift)
    is treated as **not recent**: we'd rather fire one extra rotation than wedge
    the guard until wall time catches up. The lower bound is a small negative
    tolerance to absorb sub-second drift between ``time.time()`` and filesystem
    mtime resolution (notably Windows NTFS at lower clock granularity), which
    can otherwise classify a freshly-written file as future-dated. A
    missing/unreadable file falls through to the not-recent default.
    """
    if storage_path is None:
        return False
    try:
        mtime = storage_path.stat().st_mtime
    except OSError:
        return False
    age = time.time() - mtime
    return -_KEEPALIVE_PRECISION_TOLERANCE <= age <= _KEEPALIVE_RATE_LIMIT_SECONDS


async def _poke_session(client: httpx.AsyncClient, storage_path: Path | None = None) -> None:
    """Best-effort POST to ``accounts.google.com/RotateCookies`` to rotate SIDTS.

    Failures are logged at DEBUG and swallowed: this is purely a freshness
    optimisation. The caller's request to the app host is the
    authoritative health check.

    Three layered guards keep the POST from stampeding ``accounts.google.com``:

    1. **Disk mtime fast path.** If ``storage_state.json`` was rewritten within
       the rate-limit window, skip without any locking. Covers the common
       sequential-CLI case at zero cost.
    2. **In-process ``asyncio.Lock``.** Inside the lock, re-check the disk
       mtime (a sibling task may have rotated and saved during the wait) and
       a monotonic in-memory timestamp (a sibling may have rotated but not
       yet saved). Together these dedupe an ``asyncio.gather`` fan-out so
       only one POST fires per process per rate-limit window.
    3. **Cross-process non-blocking flock.** When ``storage_path`` is set, try
       to acquire ``.storage_state.json.rotate.lock`` with ``LOCK_NB``. If
       another process holds it, skip — they're rotating right now. This
       handles ``xargs -P``, parallel MCP workers, and similar parallel
       launches without queueing.

       Known gap: the flock is released as soon as the POST returns, but the
       caller's storage-state save happens *after* this function returns. A
       second process that starts in that narrow window observes the still-
       stale on-disk mtime and an unheld flock, and will fire its own POST.
       Worst case is two pokes back-to-back across processes — bounded, not
       a stampede. Closing this fully would require holding the flock past
       ``_poke_session`` until the save completes, which would entangle this
       throttle with the caller's lifecycle. Not worth the complexity here.

    Args:
        client: Live ``httpx.AsyncClient`` whose cookie jar should receive the
            rotated ``Set-Cookie``.
        storage_path: Optional path to the on-disk ``storage_state.json``. When
            provided, gates the poke via the disk mtime and the cross-process
            flock; when ``None`` (env-var auth) only the in-process serializer
            applies.

    Set ``NOTEBOOKLM_DISABLE_KEEPALIVE_POKE=1`` to disable (e.g., environments
    that block ``accounts.google.com``).
    """
    if os.environ.get(NOTEBOOKLM_DISABLE_KEEPALIVE_POKE_ENV) == "1":
        return
    if _is_recently_rotated(storage_path):
        logger.debug(
            "Keepalive RotateCookies skipped: %s rotated within %.0fs",
            storage_path,
            _KEEPALIVE_RATE_LIMIT_SECONDS,
        )
        return

    async with _get_poke_lock(storage_path):
        # Re-check after acquiring the per-(loop, profile) async lock — another
        # task in this loop may have rotated and persisted while we were waiting.
        if _is_recently_rotated(storage_path):
            logger.debug(
                "Keepalive RotateCookies skipped: storage refreshed while waiting for lock"
            )
            return

        rotate_lock_path = _rotation_lock_path(storage_path)
        if rotate_lock_path is None:
            # No on-disk path → cross-process flock has no anchor. The
            # atomic claim inside ``_rotate_cookies`` is the only gate.
            await _rotate_cookies(client, storage_path)
            return

        with _file_lock_try_exclusive(rotate_lock_path) as acquired:
            if not acquired:
                logger.debug(
                    "Keepalive RotateCookies skipped: %s held by another process",
                    rotate_lock_path,
                )
                return
            # One last disk recheck: another process may have completed its
            # rotation + save between our top-of-function check and acquiring
            # this flock.
            if _is_recently_rotated(storage_path):
                logger.debug(
                    "Keepalive RotateCookies skipped: storage refreshed before flock acquired"
                )
                return
            # ``_rotate_cookies`` does its own atomic claim — if another
            # in-process caller (e.g. a sibling layer-2 keepalive loop on a
            # different event loop) just claimed this profile, the POST is
            # skipped here too.
            await _rotate_cookies(client, storage_path)


async def _rotate_cookies(client: httpx.AsyncClient, storage_path: Path | None = None) -> None:
    """Fire the ``RotateCookies`` POST. Bare operation; no guards.

    Used directly by the layer-2 keepalive loop, which is already self-paced
    via ``keepalive_min_interval`` and does not need the layer-1 dedup
    serialization. ``_poke_session`` calls this through its guard stack.

    Honours ``NOTEBOOKLM_DISABLE_KEEPALIVE_POKE=1`` so a single env-var disables
    every rotation path (the layer-1 wrapper *and* the layer-2 loop).

    Stamps the per-profile attempt timestamp **before** the network await so
    that concurrent layer-1 callers (and concurrent layer-2 keepalive loops on
    other ``NotebookLMClient`` instances watching the same profile) see "this
    profile is rotating right now" and skip the POST. Stamping early covers:
      - the layer-1/layer-2 overlap where one is mid-flight and another arrives
      - failure stampedes — a 15 s timeout against a hung accounts.google.com
        does not let 10 fanned-out callers each wait the full timeout

    Does not propagate ``httpx.HTTPError``: this is a best-effort freshness
    call, not a health check.

    Args:
        client: Live ``httpx.AsyncClient`` whose cookie jar should receive the
            rotated ``Set-Cookie``.
        storage_path: Optional storage_state.json path used to key the
            in-process attempt timestamp by profile. ``None`` = env-var auth.
    """
    if os.environ.get(NOTEBOOKLM_DISABLE_KEEPALIVE_POKE_ENV) == "1":
        return
    # Atomic check-and-claim: another caller (a sibling layer-2 keepalive
    # loop, a layer-1 ``_poke_session`` on a different event loop, etc.) may
    # have already taken the slot for this profile within the rate-limit
    # window. ``_try_claim_rotation`` is the *only* authoritative gate;
    # everything above it in ``_poke_session`` is a fast-path optimisation.
    if not _try_claim_rotation(storage_path):
        logger.debug(
            "Keepalive RotateCookies skipped: %s claimed by another in-process caller",
            storage_path,
        )
        return
    try:
        await _rotate_post(client)
    except httpx.HTTPError as exc:
        logger.debug("Keepalive RotateCookies POST failed (non-fatal): %s", exc)
