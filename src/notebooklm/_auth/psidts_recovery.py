"""Inline ``__Secure-1PSIDTS`` recovery for the cookie-load preflight (issue #865).

Background:

``__Secure-1PSIDTS`` is the rotating freshness partner of ``__Secure-1PSID``.
It is minted reliably only by the dedicated ``accounts.google.com/RotateCookies``
POST that the keepalive loop in :mod:`notebooklm._auth.keepalive` uses. The
Playwright login flow substitutes two passive ``goto()`` navigations, which
Google does not always answer with ``Set-Cookie: __Secure-1PSIDTS``. When that
happens, ``storage_state.json`` is saved without PSIDTS, the Tier-1 preflight
in :mod:`notebooklm._auth.cookie_policy` rejects the next CLI invocation, and
the keepalive recovery path (which would heal the state in one POST) is
unreachable because it only runs inside an opened ``Session`` — a closed loop.

The header comment on ``MINIMUM_REQUIRED_COOKIES`` has always described PSIDTS
as ``directly accepted by Google's homepage check, OR recoverable via the
RotateCookies POST when other auth cookies are intact``. This module wires the
recoverable arm of that policy into the cold-start load path: when ``SID`` is
present and a valid secondary binding (``OSID``, or ``APISID + SAPISID``) is
intact but PSIDTS is missing, fire one ``RotateCookies`` POST, persist the
rotated cookies to disk via the existing snapshot/delta save, and let the
preflight retry.

The hard-Tier-1 classification of PSIDTS in ``MINIMUM_REQUIRED_COOKIES`` is
intentionally preserved so any caller that bypasses :func:`_recover_psidts_inline`
still sees a strict reject. Future work (option B, tracked separately): demote
PSIDTS to Tier 2 outright and rely on a session-open prime to mint it before
the first RPC. That change touches the lifecycle ordering and is out of scope
for this fix.
"""

from __future__ import annotations

import http.cookiejar
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import httpx

from . import cookie_policy as _cookie_policy
from . import cookies as _auth_cookies
from . import keepalive as _keepalive
from . import storage as _auth_storage

# ----------------------------------------------------------------------------
# Cross-module helpers (module-scope aliases)
# ----------------------------------------------------------------------------
# These are documented at module scope to make the cross-module surface
# explicit, matching the precedent at ``_auth/cookies.py:34-40``. The
# underscored names remain module-private to their owning modules
# (``_cookie_policy``, ``_keepalive``, ``_auth_cookies``); this module
# consumes them through documented local aliases.
#
# Tests must patch these aliases at this module's path, not at the
# canonical owner's path, because aliases are import-time bound.
# ----------------------------------------------------------------------------
_has_valid_secondary_binding = _cookie_policy._has_valid_secondary_binding
_is_allowed_auth_domain = _cookie_policy._is_allowed_auth_domain
_auth_domain_priority = _cookie_policy._auth_domain_priority
_rotation_lock_path = _keepalive._rotation_lock_path
_file_lock_try_exclusive = _keepalive._file_lock_try_exclusive
_try_claim_rotation = _keepalive._try_claim_rotation
_KEEPALIVE_POKE_TIMEOUT = _keepalive._KEEPALIVE_POKE_TIMEOUT
_KEEPALIVE_ROTATE_HEADERS = _keepalive._KEEPALIVE_ROTATE_HEADERS
_KEEPALIVE_ROTATE_BODY = _keepalive._KEEPALIVE_ROTATE_BODY
_load_storage_state = _auth_cookies._load_storage_state
_storage_entry_to_cookie = _auth_cookies._storage_entry_to_cookie

logger = logging.getLogger("notebooklm.auth")

_PSIDTS_COOKIE = "__Secure-1PSIDTS"


def _psidts_needs_recovery(
    cookie_names: set[str],
    cookie_expiry: dict[str, Any],
    *,
    now: float | None = None,
) -> bool:
    """True when ``__Secure-1PSIDTS`` is absent OR present-but-EXPIRED.

    The recovery precondition originally keyed purely on name presence
    (``_PSIDTS_COOKIE in cookie_names``), so an idle Chrome session whose
    PSIDTS row is still on disk but already past its ``expires`` epoch silently
    skipped the one ``RotateCookies`` POST that would heal it — cold-start then
    failed hard at the first authed GET.

    Expiry semantics mirror the storage round-trip in
    :func:`notebooklm._auth.cookies._storage_entry_to_cookie`:

    - ``expires`` of ``None`` or ``-1`` is a *session* cookie (Playwright
      convention) — never treated as expired, so recovery does NOT fire.
    - a numeric ``expires`` strictly less than ``now`` (default ``time.time()``)
      is past its lifetime → treat the cookie as ABSENT so recovery fires.
    - a numeric ``expires`` at or in the future → cookie is fresh; recovery is
      skipped (current behavior).

    Args:
        cookie_names: Set of cookie names present on the source state.
        cookie_expiry: ``name -> expires`` view over the same entries.
        now: Injectable wall-clock seconds for deterministic tests; defaults
            to :func:`time.time` at call time.

    Returns:
        ``True`` if recovery should proceed (PSIDTS missing or expired),
        ``False`` if a present, unexpired PSIDTS makes recovery a no-op.
    """
    if _PSIDTS_COOKIE not in cookie_names:
        return True
    expires = cookie_expiry.get(_PSIDTS_COOKIE)
    if expires in (None, -1):
        # Session cookie — no expiry to compare against; treat as present.
        return False
    if not isinstance(expires, (int, float)) or isinstance(expires, bool):
        # Unparseable expiry: fall back to the legacy name-presence behavior
        # (present → skip) rather than firing a possibly-needless POST.
        return False
    reference = time.time() if now is None else now
    return expires < reference


def _index_recovery_cookies(
    entries: list[dict[str, Any]],
) -> tuple[set[str], dict[str, Any]]:
    """Build domain-filtered ``(cookie_names, cookie_expiry)`` views for the gate.

    Only entries on an allowed auth domain (:func:`_is_allowed_auth_domain`)
    are indexed — this matches the jar-building filter in
    :func:`_attempt_rotation` / :func:`recover_psidts_in_memory`, so a stray
    ``__Secure-1PSIDTS`` / ``SID`` on an unrelated domain can't falsely satisfy
    the precondition and skip the heal.

    When the same name appears on multiple allowed domains, the highest
    :func:`_auth_domain_priority` tier wins (``.google.com`` > regional > …),
    mirroring :func:`notebooklm._auth.cookies.flatten_cookie_map`. Tiers are
    strictly distinct, so the resolved expiry is deterministic regardless of
    storage_state ordering; within a single tier the first occurrence wins.

    An entry must carry a non-empty ``name`` *and* ``value`` to be indexed: a
    nameless/valueless cookie can't be meaningfully present on either the
    file-based or in-memory recovery path.
    """
    cookie_names: set[str] = set()
    cookie_expiry: dict[str, Any] = {}
    name_priority: dict[str, int] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name or not entry.get("value"):
            continue
        if not _is_allowed_auth_domain(entry.get("domain", "") or ""):
            continue
        priority = _auth_domain_priority(entry.get("domain", "") or "")
        if name not in cookie_names or priority > name_priority[name]:
            cookie_names.add(name)
            cookie_expiry[name] = entry.get("expires")
            name_priority[name] = priority
    return cookie_names, cookie_expiry


def _resolve_recovery_path(path: Path | str | None) -> Path | None:
    """Resolve the effective file path for recovery, or ``None`` to decline.

    Mirrors ``_load_storage_state`` (`_auth/cookies.py`) precedence:

    - explicit ``path`` argument → use as-is (cast ``str`` to ``Path``)
    - ``NOTEBOOKLM_AUTH_JSON`` env-var set → return ``None`` (no writeable
      backing store; tracked as future-work in this module's docstring)
    - otherwise → fall back to :func:`notebooklm.paths.get_storage_path`,
      so ``load_auth_from_storage()`` with no args still triggers recovery
      on the default profile file (issue #865 critical-path coverage).
    """
    if path:
        return Path(path)
    if os.environ.get("NOTEBOOKLM_AUTH_JSON"):
        return None
    from ..paths import get_storage_path

    return get_storage_path()


def _recover_psidts_inline(path: Path | str | None) -> bool:
    """Attempt a one-shot ``RotateCookies`` POST to mint ``__Secure-1PSIDTS``.

    Pre-conditions (all must hold; otherwise return ``False`` without firing):

    1. ``SID`` present in ``storage_path``.
    2. ``__Secure-1PSIDTS`` absent in ``storage_path``, OR present but past its
       ``expires`` epoch (a ``-1``/``None`` session-cookie expiry counts as
       present, not expired — see :func:`_psidts_needs_recovery`).
    3. Secondary binding intact (``OSID``, or ``APISID + SAPISID``). Google
       rejects ``RotateCookies`` requests that lack these — see
       :func:`notebooklm._auth.cookie_policy._has_valid_secondary_binding`.
    4. Cross-process rotation flock available
       (:func:`notebooklm._auth.keepalive._file_lock_try_exclusive` against
       :func:`notebooklm._auth.keepalive._rotation_lock_path`). Mirrors
       ``_poke_session``'s outer guard so concurrent cold-start CLI
       processes don't each fire the POST.
    5. In-process rotation throttle slot available
       (:func:`notebooklm._auth.keepalive._try_claim_rotation`). Inner guard
       against same-process duplicates (e.g. two callers on the same loop).

    On success the rotated cookies are merged into the file at ``storage_path``
    via :func:`notebooklm._auth.storage.save_cookies_to_storage` (snapshot/delta
    semantics, atomic write, cross-process file lock). ``save_cookies_to_storage``
    is contract-bound to return ``False`` (not raise) on a persistence failure;
    we check the return value and surface the persist failure as ``False`` so
    the caller's preflight retry sees the unhealed state and re-raises honestly.
    On any failure the function returns ``False`` and the caller's original
    ``ValueError`` stands.

    Args:
        path: Path to ``storage_state.json``, or ``None`` to resolve the
            default profile file. When ``NOTEBOOKLM_AUTH_JSON`` is set the
            function declines because there is no writeable backing store
            to persist the rotated cookies to (tracked future-work).

    Returns:
        ``True`` if PSIDTS is now persisted on disk; ``False`` otherwise.
    """
    storage_path = _resolve_recovery_path(path)
    if storage_path is None:
        logger.debug(
            "PSIDTS recovery skipped: env-var auth (NOTEBOOKLM_AUTH_JSON) "
            "has no writeable backing store"
        )
        return False

    state = _read_storage_for_recovery(storage_path)
    if state is None:
        return False
    cookie_entries, cookie_names, cookie_expiry = state

    if "SID" not in cookie_names:
        logger.debug("PSIDTS recovery skipped: SID missing — session is truly broken")
        return False
    if not _psidts_needs_recovery(cookie_names, cookie_expiry):
        return False
    if not _has_valid_secondary_binding(cookie_names):
        logger.debug(
            "PSIDTS recovery skipped: secondary binding incomplete "
            "(need OSID, or both APISID and SAPISID)"
        )
        return False
    # Cross-process flock first. Two simultaneous cold-start CLI invocations
    # would each pass the in-process throttle (which is keyed on a per-process
    # dict) and both fire ``RotateCookies``. The flock matches the outer guard
    # ``_poke_session`` uses; a held lock means the other process is rotating
    # right now.
    rotate_lock_path = _rotation_lock_path(storage_path)
    if rotate_lock_path is None:
        # Defense-in-depth: ``_rotation_lock_path`` only returns None when its
        # argument is None, and we've early-returned above when path is None.
        # Fall through to the in-process guard alone, matching the keepalive's
        # equivalent branch.
        return _attempt_rotation(storage_path, cookie_entries)

    with _file_lock_try_exclusive(rotate_lock_path) as acquired:
        if not acquired:
            # Holder may already have healed the file by the time they
            # released the lock. Re-read once before declining so the caller's
            # retry sees the heal instead of a stale ``ValueError``.
            healed = _is_psidts_persisted(storage_path)
            logger.debug(
                "PSIDTS recovery skipped: %s held by another process (healed=%s)",
                rotate_lock_path,
                healed,
            )
            return healed
        # Re-read inside the lock: another process may have completed its
        # rotation + save between our top-of-function precondition check and
        # acquiring this flock. Mirrors ``_poke_session``'s "one last disk
        # recheck" pattern at ``_auth/keepalive.py:283-290``. Re-validate the
        # FULL precondition set against the fresh state (not just PSIDTS-present)
        # so a concurrent write that dropped SID or the secondary binding
        # can't slip a doomed POST through.
        fresh = _read_storage_for_recovery(storage_path)
        if fresh is None:
            return False
        fresh_entries, fresh_names, fresh_expiry = fresh
        if not _psidts_needs_recovery(fresh_names, fresh_expiry):
            logger.debug(
                "PSIDTS recovery skipped: file healed by another process while waiting for flock"
            )
            return True
        if "SID" not in fresh_names:
            logger.debug("PSIDTS recovery skipped: SID missing after flock acquisition")
            return False
        if not _has_valid_secondary_binding(fresh_names):
            logger.debug(
                "PSIDTS recovery skipped: secondary binding incomplete after flock acquisition"
            )
            return False
        return _attempt_rotation(storage_path, fresh_entries)


def _read_storage_for_recovery(
    storage_path: Path,
) -> tuple[list[dict], set[str], dict[str, Any]] | None:
    """Load + filter + name-index storage_state for the recovery preconditions.

    Returns ``(cookie_entries, cookie_names, cookie_expiry)`` on success, or
    ``None`` on any load/parse failure (caller treats this as "decline
    recovery"). ``cookie_names`` / ``cookie_expiry`` are domain-filtered,
    priority-resolved views over the same entries (see
    :func:`_index_recovery_cookies`) so the precondition gate can treat a
    present-but-expired PSIDTS as absent (see :func:`_psidts_needs_recovery`).
    ``cookie_entries`` is the unfiltered list — the jar builder in
    :func:`_attempt_rotation` applies its own domain filter. The narrow
    exception scope catches the documented raise sites of ``_load_storage_state``
    (``OSError`` for missing file, ``json.JSONDecodeError`` for malformed JSON)
    and lets unexpected ``ValueError`` propagate as an implementation bug.
    """
    try:
        storage_state = _load_storage_state(storage_path)
    except (OSError, json.JSONDecodeError) as exc:
        logger.debug("PSIDTS recovery skipped: cannot read %s: %s", storage_path, exc)
        return None
    raw_entries = storage_state.get("cookies", [])
    if not isinstance(raw_entries, list):
        return None
    cookie_entries: list[dict] = [entry for entry in raw_entries if isinstance(entry, dict)]
    cookie_names, cookie_expiry = _index_recovery_cookies(cookie_entries)
    return cookie_entries, cookie_names, cookie_expiry


def _is_psidts_persisted(storage_path: Path) -> bool:
    """Quick re-read: is a fresh ``__Secure-1PSIDTS`` currently on disk?

    Used after a held-flock skip to detect when another process has just healed
    the file. Treats any load/parse failure as "not persisted" rather than
    raising — the caller will retry. A present-but-expired PSIDTS counts as
    *not* persisted (mirrors :func:`_psidts_needs_recovery`) so a stale on-disk
    row doesn't masquerade as a heal.
    """
    state = _read_storage_for_recovery(storage_path)
    if state is None:
        return False
    _, names, expiry = state
    return not _psidts_needs_recovery(names, expiry)


def _attempt_rotation(storage_path: Path, cookie_entries: list[dict]) -> bool:
    """Fire one ``RotateCookies`` POST and persist the rotated cookies.

    Inner half of :func:`_recover_psidts_inline` — the steps that run after
    every guard (preconditions, cross-process flock) has passed. Split out so
    the cross-process flock context manager has one clean exit point.
    """
    if not _try_claim_rotation(storage_path):
        logger.debug(
            "PSIDTS recovery skipped: %s claimed by another in-process caller",
            storage_path,
        )
        return False

    # Build the cookie jar manually so the validator (which would raise) is
    # bypassed. Mirrors ``build_httpx_cookies_from_storage`` without the
    # ``_validate_required_cookies`` call.
    jar = httpx.Cookies()
    for entry in cookie_entries:
        if not entry.get("name") or not entry.get("value"):
            continue
        if not _is_allowed_auth_domain(entry.get("domain", "")):
            continue
        jar.jar.set_cookie(_storage_entry_to_cookie(entry))

    # ``httpx.Client(cookies=jar)`` copies the source jar into a private client
    # jar; Set-Cookie responses land in ``client.cookies``, not in ``jar``. So
    # we snapshot and check the *client's* jar, mirroring how the async
    # keepalive in ``_session_lifecycle.save_cookies`` reads ``client.cookies``.
    try:
        with httpx.Client(
            cookies=jar,
            follow_redirects=True,
            timeout=_KEEPALIVE_POKE_TIMEOUT,
        ) as client:
            snapshot = _auth_storage.snapshot_cookie_jar(client.cookies)
            response = client.post(
                _keepalive.KEEPALIVE_ROTATE_URL,
                headers=_KEEPALIVE_ROTATE_HEADERS,
                content=_KEEPALIVE_ROTATE_BODY,
            )
            response.raise_for_status()
            rotated_jar = client.cookies
            psidts_present = any(c.name == _PSIDTS_COOKIE for c in rotated_jar.jar)
    except httpx.HTTPError as exc:
        logger.debug("Inline PSIDTS recovery POST failed (non-fatal): %s", exc)
        return False

    if not psidts_present:
        logger.debug(
            "Inline PSIDTS recovery: RotateCookies returned 2xx but did not "
            "include %s — Google may be withholding the rotation",
            _PSIDTS_COOKIE,
        )
        return False

    # ``save_cookies_to_storage`` returns False (not raises) on every
    # persist-failure path: missing file, invalid payload, CAS conflict,
    # atomic-write failure (see ``_auth/storage.py:380-429``). The bare
    # ``except`` below catches the *unexpected* raises only (future refactor
    # could change the contract); the explicit return-value check is what
    # surfaces the documented failure modes.
    try:
        persisted = _auth_storage.save_cookies_to_storage(
            rotated_jar, storage_path, original_snapshot=snapshot
        )
    except Exception as exc:  # noqa: BLE001 - persistence failure is non-fatal here
        logger.warning("Inline PSIDTS recovery: persist to %s raised %s", storage_path, exc)
        return False

    if not persisted:
        logger.warning(
            "Inline PSIDTS recovery: save_cookies_to_storage returned False; "
            "on-disk state still lacks %s",
            _PSIDTS_COOKIE,
        )
        return False

    logger.info(
        "Recovered %s via inline RotateCookies POST and persisted to %s (issue #865)",
        _PSIDTS_COOKIE,
        storage_path,
    )
    return True


def _rookiepy_entry_to_cookie(entry: dict[str, Any]) -> http.cookiejar.Cookie:
    """Build an ``http.cookiejar.Cookie`` from a rookiepy cookie dict.

    Rookiepy returns cookies with snake_case field names (``http_only``); the
    Playwright storage_state mirror (:func:`notebooklm._auth.cookies._storage_entry_to_cookie`)
    uses camelCase (``httpOnly``). We need a parallel converter so the
    in-memory recovery path can build an ``httpx.Cookies`` jar straight from
    the rookiepy list — without first round-tripping through
    ``convert_rookiepy_cookies_to_storage_state`` (which would silently drop
    entries on domains we don't allowlist).
    """
    domain = entry.get("domain", "") or ""
    expires = entry.get("expires")
    expires_value = None if expires in (None, -1) else expires
    rest: dict[str, str] = {"HttpOnly": ""} if entry.get("http_only") else {}
    return http.cookiejar.Cookie(
        version=0,
        name=entry.get("name", "") or "",
        value=entry.get("value", "") or "",
        port=None,
        port_specified=False,
        domain=domain,
        domain_specified=bool(domain),
        domain_initial_dot=domain.startswith("."),
        path=entry.get("path") or "/",
        path_specified=True,
        secure=bool(entry.get("secure", False)),
        expires=expires_value,
        discard=expires_value is None,
        comment=None,
        comment_url=None,
        rest=rest,
    )


def recover_psidts_in_memory(rookiepy_cookies: list[dict[str, Any]]) -> bool:
    """In-memory ``__Secure-1PSIDTS`` recovery for the browser-extraction path.

    Variant of :func:`_recover_psidts_inline` for the CLI ``--browser-cookies``
    flows (``_enumerate_one_jar``, ``_write_extracted_cookies``) that operate
    on rookiepy cookie lists before any persistence. The file-based recovery
    is unreachable here because nothing has been written to disk yet — the
    cookies live only in the caller's in-memory list.

    Preconditions match the file-based recovery (see
    :func:`_recover_psidts_inline`):

    1. ``SID`` present.
    2. ``__Secure-1PSIDTS`` absent, OR present but past its ``expires`` epoch
       (a ``-1``/``None`` session-cookie expiry counts as present, not
       expired — see :func:`_psidts_needs_recovery`).
    3. Secondary binding intact (``OSID``, or ``APISID + SAPISID``).

    On success, mutates ``rookiepy_cookies`` to append the rotated
    ``__Secure-1PSIDTS`` (and ``__Secure-3PSIDTS``) entries in rookiepy's
    snake_case format. Downstream
    :func:`notebooklm._auth.cookies.convert_rookiepy_cookies_to_storage_state`
    picks them up on re-conversion.

    No file lock and no in-process throttle: the browser-extraction path is a
    one-shot CLI invocation that runs before any persistence. Concurrent CLI
    processes would each be operating on their own in-memory list — the
    cross-process flock in the file-based recovery exists to coordinate
    writes to a shared ``storage_state.json``, which doesn't apply here.

    Returns ``True`` if the rotation succeeded and the in-memory list now
    contains ``__Secure-1PSIDTS``; ``False`` otherwise.
    """
    cookie_names, cookie_expiry = _index_recovery_cookies(rookiepy_cookies)

    if "SID" not in cookie_names:
        logger.debug("In-memory PSIDTS recovery skipped: SID missing")
        return False
    if not _psidts_needs_recovery(cookie_names, cookie_expiry):
        return False
    if not _has_valid_secondary_binding(cookie_names):
        logger.debug(
            "In-memory PSIDTS recovery skipped: secondary binding incomplete "
            "(need OSID, or both APISID and SAPISID)"
        )
        return False

    jar = httpx.Cookies()
    for entry in rookiepy_cookies:
        if not isinstance(entry, dict):
            continue
        if not entry.get("name") or not entry.get("value"):
            continue
        if not _is_allowed_auth_domain(entry.get("domain", "")):
            continue
        jar.jar.set_cookie(_rookiepy_entry_to_cookie(entry))

    try:
        with httpx.Client(
            cookies=jar,
            follow_redirects=True,
            timeout=_KEEPALIVE_POKE_TIMEOUT,
        ) as client:
            response = client.post(
                _keepalive.KEEPALIVE_ROTATE_URL,
                headers=_KEEPALIVE_ROTATE_HEADERS,
                content=_KEEPALIVE_ROTATE_BODY,
            )
            response.raise_for_status()
            rotated_cookies = list(client.cookies.jar)
    except httpx.HTTPError as exc:
        logger.debug("In-memory PSIDTS recovery POST failed (non-fatal): %s", exc)
        return False

    psidts_present = any(c.name == _PSIDTS_COOKIE for c in rotated_cookies)
    if not psidts_present:
        logger.debug(
            "In-memory PSIDTS recovery: RotateCookies returned 2xx but did not "
            "include %s — Google may be withholding the rotation",
            _PSIDTS_COOKIE,
        )
        return False

    for cookie in rotated_cookies:
        if cookie.name not in {_PSIDTS_COOKIE, "__Secure-3PSIDTS"}:
            continue
        if not cookie.value or not cookie.domain:
            continue
        rookiepy_cookies.append(
            {
                "name": cookie.name,
                "value": cookie.value,
                "domain": cookie.domain,
                "path": cookie.path or "/",
                "expires": cookie.expires,
                "secure": True,
                "http_only": True,
            }
        )

    logger.info(
        "Recovered %s via in-memory RotateCookies POST (browser-cookies extraction)",
        _PSIDTS_COOKIE,
    )
    return True


def validate_with_recovery(
    rookiepy_cookies: list[dict[str, Any]],
) -> tuple[dict[str, Any], ValueError | None]:
    """Convert + validate rookiepy cookies, attempting recovery on failure.

    Shared helper for the three CLI browser-extraction entry points:
    :func:`notebooklm.cli.services.login.cookie_jar._enumerate_one_jar`,
    :func:`notebooklm.cli.services.login.cookie_writes._write_extracted_cookies`,
    and :func:`notebooklm.cli.services.login.refresh._login_with_browser_cookies`.
    Wraps :func:`notebooklm._auth.cookies.convert_rookiepy_cookies_to_storage_state`
    plus :func:`notebooklm._auth.cookies.extract_cookies_from_storage` with one
    retry through :func:`recover_psidts_in_memory` (issue #990). When the
    recovery preconditions hold (SID present, PSIDTS absent or expired,
    secondary binding intact — see :func:`_psidts_needs_recovery`), the
    rotated cookies are appended to ``rookiepy_cookies`` in place so
    downstream persistence picks them up.

    Lives in the auth subpackage rather than the CLI login package so both
    CLI call sites can route through ``notebooklm.auth`` without adding a
    new sibling-import edge to the login-package DAG.

    Returns:
        ``(storage_state, error)``. When ``error`` is ``None`` the validation
        succeeded (possibly after recovery); when not, ``error`` is the final
        ``ValueError`` and ``storage_state`` is the latest extraction attempt.
    """
    storage_state = _auth_cookies.convert_rookiepy_cookies_to_storage_state(rookiepy_cookies)
    try:
        _auth_cookies.extract_cookies_from_storage(storage_state)
        return storage_state, None
    except ValueError as initial:
        if not recover_psidts_in_memory(rookiepy_cookies):
            return storage_state, initial
        storage_state = _auth_cookies.convert_rookiepy_cookies_to_storage_state(rookiepy_cookies)
        try:
            _auth_cookies.extract_cookies_from_storage(storage_state)
            return storage_state, None
        except ValueError as final:
            return storage_state, final
