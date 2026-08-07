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
unreachable because it only runs inside an opened ``NotebookLMClient`` — a closed loop.

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

import copy
import http.cookiejar
import json
import logging
import math
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import Any

import httpx

from . import cookie_policy as _cookie_policy
from . import cookie_semantics as _cookie_semantics
from . import cookies as _auth_cookies
from . import keepalive as _keepalive
from . import storage as _auth_storage
from .paths import resolve_auth_json_env

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
_has_rotatable_secondary_binding = _cookie_policy._has_rotatable_secondary_binding
_is_allowed_auth_domain = _cookie_policy._is_allowed_auth_domain
_rotation_lock_path = _keepalive._rotation_lock_path
_file_lock_try_exclusive = _keepalive._file_lock_try_exclusive
_try_claim_rotation = _keepalive._try_claim_rotation
# The RotateCookies POST itself is _keepalive's single wire contract (see the
# ``_ROTATE_POST_KWARGS`` block there); this module composes recovery policy
# around it and never re-assembles the request from the raw
# headers/body/timeout constants.
_rotate_post_sync = _keepalive._rotate_post_sync
_rotation_http_client = _keepalive._rotation_http_client
_load_storage_state = _auth_cookies._load_storage_state
_storage_entry_to_cookie = _auth_cookies._storage_entry_to_cookie
_safe_to_cookie = _auth_cookies._safe_to_cookie
_validate_cookie_shape = _auth_cookies._validate_cookie_shape

logger = logging.getLogger("notebooklm.auth")

_PSIDTS_COOKIE = "__Secure-1PSIDTS"
# Rows whose pre-POST value recovery observes for the compare-and-set that
# decides whether a rotated row may replace a stored one.  PSIDTS only: these
# are the cookies the gate reasons about as present/absent/unusable.
_RECOVERY_TARGET_COOKIE_NAMES = frozenset({_PSIDTS_COOKIE, "__Secure-3PSIDTS"})
# Rows merged back out of a rotated jar.  Strictly wider than the observation
# set — see the ``LSID`` rationale at the merge loop in
# :func:`recover_psidts_in_memory` (#1977).
_ROTATION_MERGE_COOKIE_NAMES = _RECOVERY_TARGET_COOKIE_NAMES | {"LSID"}


# Converter from a raw cookie entry to an ``http.cookiejar.Cookie``. Two shapes
# exist and differ only in the http-only field spelling: storage_state entries
# (camelCase ``httpOnly``, :func:`_storage_entry_to_cookie`) and rookiepy rows
# (snake_case ``http_only``, :func:`_rookiepy_entry_to_cookie`). Callers select
# the converter; nothing is round-tripped through
# ``convert_rookiepy_cookies_to_storage_state`` on the way.
_CookieConverter = Callable[[dict[str, Any]], http.cookiejar.Cookie]

# RFC 6265 §5.3 cookie identity: ``(name, domain, path)``.
_CookieIdentity = tuple[str, str, str]


def _cookie_header_names(header: str) -> set[str]:
    """Return the cookie NAMES present in a ``Cookie:`` request header.

    Parsing, never substring matching. ``_PSIDTS_COOKIE in header`` would
    false-positive on a lookalike name (``X__Secure-1PSIDTSY=v``) and on any
    cookie whose *value* embeds the literal (``NID=__Secure-1PSIDTS=oops``) —
    and cookie values on allowlisted domains are not shape-controlled by us,
    they come from Chrome. A false positive here makes the gate skip a needed
    heal, so the comparison has to be exact.
    """
    return {part.split("=", 1)[0].strip() for part in header.split(";") if "=" in part}


def _allowed_cookie_name(entry: Any) -> str | None:
    """Return a row's cookie NAME if the row is usable for recovery, else ``None``.

    The single row filter every recovery path shares — the two precondition
    predicates (:func:`_recovery_cookie_names`, :func:`_iter_routable_psidts_cookies`)
    and the request-jar builder (:func:`_build_recovery_jar`). Keeping them on one
    predicate is what makes "the gate reasons about the rows the POST will
    actually send" true by construction rather than by convention.

    A row qualifies when it is a dict carrying a non-empty string ``name``, a
    non-empty ``value``, and a domain on the auth allowlist. Cookie rows come
    from Chrome via rookiepy or from a hand-editable JSON file, so none of that
    is ours to guarantee: a nameless or valueless cookie isn't meaningfully
    present on any path, and the domain filter stops a stray ``SID`` from an
    unrelated site satisfying a precondition.

    ``name`` and ``domain`` are both type-checked, not merely truthiness-checked.
    :func:`_is_allowed_auth_domain` does string work, so a non-string domain
    (``123``, a dict) raises ``AttributeError``/``TypeError`` — and it would do so
    from inside the caller's ``except ValueError:`` handler, where it escapes as a
    bare traceback instead of an auth diagnostic. A missing or ``null`` domain
    normalizes to ``""``, which is simply not allowlisted.
    """
    normalized = _auth_cookies._sanitize_cookie_entry(entry)
    if normalized is None or not _is_allowed_auth_domain(normalized["domain"]):
        return None
    return normalized["name"]


def _recovery_cookie_names(entries: list[dict[str, Any]]) -> set[str]:
    """Return the cookie NAMES present on an allowed auth domain.

    Feeds the two name-presence preconditions — ``SID`` present, and
    :func:`_has_rotatable_secondary_binding` — which are deliberately domain-blind
    and expiry-blind, because that is exactly what the preflight they front
    (:func:`notebooklm._auth.cookie_policy._validate_required_cookies`) checks.

    No domain ranking is applied. The predecessor of this function
    (``_index_recovery_cookies``) ranked duplicate names by
    ``_auth_domain_priority``, but that ranking only ever selected which
    duplicate's ``expires`` landed in a parallel expiry map, never which name was
    recorded (``add`` on a set is idempotent). Expiry now lives in :func:`_is_expired` and is
    consumed by the two PSIDTS predicates, which resolve domain per RFC 6265
    instead of by tier, so the ranking has no remaining consumer here (#2057).
    """
    return {name for entry in entries if (name := _allowed_cookie_name(entry)) is not None}


def _is_expired(cookie: http.cookiejar.Cookie, now: float | None) -> bool:
    """``Cookie.is_expired`` against an optional injected clock.

    ``Cookie.expires`` is always an ``int`` after the constructor's
    ``int(float(...))`` coercion, and ``is_expired`` compares ``expires <= now``.
    For integer ``e``, ``e <= now`` iff ``e <= floor(now)`` — so flooring is
    exact, not lossy, and it satisfies the stub's ``int | None`` signature while
    letting callers pass the float seconds every other clock here uses.
    ``math.floor`` rather than ``int``: the latter truncates toward zero and
    would disagree for a negative ``now``.
    """
    return cookie.is_expired(None if now is None else math.floor(now))


def _iter_routable_psidts_cookies(
    entries: list[dict[str, Any]],
    *,
    to_cookie: _CookieConverter,
    now: float | None = None,
) -> Iterator[http.cookiejar.Cookie]:
    """Yield the ``__Secure-1PSIDTS`` cookies a request jar would actually carry.

    Feeds :func:`_psidts_routes_to_rotate` ONLY. This models the jar, so its
    rules are the jar's rules — which is exactly why :func:`_psidts_is_live` must
    NOT reuse it: that predicate models the caller's preflight instead, and the
    two disagree in ways that matter (see its docstring).

    Only ``__Secure-1PSIDTS`` rows are converted. Restricting by name *before*
    conversion is not just an optimization: ``http.cookiejar.Cookie.__init__``
    coerces eagerly (``int(float(expires))``), so a malformed ``expires`` on any
    unrelated sibling row would otherwise raise from inside a predicate that
    callers invoke from within an ``except ValueError:`` handler. Narrowing the
    conversion surface to the rows the question is actually about keeps the
    blast radius minimal, and the answer is identical either way because
    ``http.cookiejar`` evaluates each cookie independently.

    A row whose ``expires`` cannot be coerced (``""``, ``"never"``, ``nan``,
    ``inf``, a list) is skipped: an unconvertible row cannot be put in a jar, so
    it cannot be sent, so it must not hold the heal back. Recovery then fires,
    which is the safe direction — one throttled POST versus an unhealed session.

    Duplicate ``(name, domain, path)`` identities are resolved CONSERVATIVELY:
    an identity routes only when *every* row carrying it is unexpired.
    ``http.cookiejar`` keeps one cookie per identity — ``set_cookie`` assigns
    ``_cookies[domain][path][name]`` against the LITERAL domain string, with no
    leading-dot normalization — and lets a later row replace an earlier one, so a
    jar built from a duplicated identity depends on entry order, precisely the
    order-sensitivity this gate exists to remove. Poisoning from any dead row is
    order-independent *and* never over-optimistic, at the cost of a needless POST
    when a stale duplicate shadows a fresh one. A row that fails conversion has
    no identity and therefore cannot poison one.

    Note the identity tuple is deliberately NOT the leading-dot-equivalent key
    from :func:`notebooklm._auth.cookies._cookie_key_variants`. That equivalence
    exists for the disk CAS/merge stage; here it would manufacture a false
    negative, because ``.google.com`` and ``google.com`` both route yet the jar
    keeps them as two independent cookies, so a stale one would poison a fresh
    one the jar still sends.

    Args:
        entries: Raw cookie dicts (storage_state entries or rookiepy rows).
        to_cookie: Converter matching ``entries``' shape.
        now: Injectable wall-clock seconds for deterministic tests; defaults to
            the current time via :meth:`http.cookiejar.Cookie.is_expired`.

    Yields:
        One cookie per surviving identity, in first-seen identity order. The
        IDENTITY SET is order-independent; both the sequence and *which*
        duplicate occurrence is yielded are not — last-write-wins means
        ``[fresh_a, fresh_b]`` yields ``fresh_b`` and the reverse yields
        ``fresh_a``. Harmless while the only consumer reads a boolean, but a
        future consumer that takes the first element, or reads a cookie's
        VALUE, would reintroduce order-sensitivity.
    """
    live: dict[_CookieIdentity, http.cookiejar.Cookie] = {}
    dead: set[_CookieIdentity] = set()
    for entry in entries:
        if _allowed_cookie_name(entry) != _PSIDTS_COOKIE:
            continue
        cookie = _safe_to_cookie(entry, to_cookie)
        if cookie is None:
            continue
        identity = (cookie.name, cookie.domain, cookie.path)
        if _is_expired(cookie, now):
            dead.add(identity)
        else:
            # Last-write-wins, matching ``CookieJar.set_cookie``. The boolean the
            # predicates read is unaffected either way, but retaining the same
            # occurrence the jar would retain keeps this function an honest model
            # of the thing it stands in for.
            live[identity] = cookie
    for identity, cookie in live.items():
        if identity not in dead:
            yield cookie


def _build_recovery_jar(
    entries: list[dict[str, Any]], to_cookie: _CookieConverter
) -> httpx.Cookies:
    """Build the ``RotateCookies`` request jar from raw cookie rows.

    Built manually so the validator is bypassed — this mirrors
    ``build_httpx_cookies_from_storage`` without its
    ``_validate_required_cookies`` call, which would raise, and which recovery
    runs precisely because it already failed.

    Unlike :func:`_iter_routable_psidts_cookies` this keeps ALL usable rows, not just
    ``__Secure-1PSIDTS``: Google rejects a ``RotateCookies`` POST that does not
    carry ``SID`` plus the secondary binding. Expiry is not filtered here either
    — ``http.cookiejar`` drops expired cookies when it builds the header. Rows
    that fail conversion are skipped rather than raised on
    (see :func:`_safe_to_cookie`).
    """
    jar = httpx.Cookies()
    for entry in entries:
        if _allowed_cookie_name(entry) is None:
            continue
        cookie = _safe_to_cookie(entry, to_cookie)
        if cookie is not None:
            jar.jar.set_cookie(cookie)
    return jar


def _psidts_is_live(
    entries: list[dict[str, Any]],
    *,
    to_cookie: _CookieConverter,
    now: float | None = None,
) -> bool:
    """Is ANY ``__Secure-1PSIDTS`` present and not known-expired on an allowed domain?

    This is the "did the heal land?" question, and it models the caller's
    RETRIED PREFLIGHT — not the request jar. That preflight
    (:func:`notebooklm._auth.cookie_policy._validate_required_cookies`) is
    domain-blind name presence. Asking the rotate-URL question here would report
    "not healed" for a PSIDTS persisted only on the app host, re-raising an
    authentication error over a session that actually works — a false negative
    in the direction that hurts (issue #2057).

    Deliberately a plain existential, NOT the jar model in
    :func:`_iter_routable_psidts_cookies`. Two rules from there are wrong here,
    and both cause the same harmful false negative:

    - **No duplicate-identity poisoning.** A stale twin sharing a row's
      ``(name, domain, path)`` does not stop a name-presence preflight from
      passing. Poisoning here is not merely pessimistic, it is a PERMANENT
      failure loop: ``save_cookies_to_storage`` CAS-matches the first stored row
      and leaves the stale twin on disk, so every subsequent load would fire a
      POST, write to disk, then re-raise "Missing required cookies" for a
      session whose preflight passes. The twin is issue #1523's data shape;
      #1523 fixed the producer, and nothing on the load/save path removes an
      existing one.
    - **An unparseable ``expires`` counts as PRESENT.** The row is on disk and
      the preflight will see it. This mirrors the predecessor gate, which
      treated an uninterpretable expiry as present rather than guessing.

    It IS deliberately stricter than the preflight in two respects, both in the
    safe direction:

    - a row known to be expired does not count, though the preflight ignores
      ``expires`` entirely. Dropping that would regress issue #1273 — a no-op
      save over a stale on-disk row would report success and fake a heal.
    - a row with an empty ``value`` does not count, though
      :func:`~notebooklm._auth.cookies.extract_cookies_from_storage` gates on
      name alone. Unreachable in practice: the POST writes a real value, and
      :func:`_build_recovery_jar` skips valueless rows anyway.

    The invariant to hold is an implication, not an equality: live ⇒ the
    preflight passes on the PSIDTS half (it says nothing about ``SID``). "The
    preflight" here means
    :func:`~notebooklm._auth.cookies.extract_cookies_from_storage`, the
    name-presence loader. The sibling loader
    ``_build_httpx_cookies_from_storage_strict`` converts every row and can
    still reject a state this accepts, because a row whose ``expires`` will not
    coerce raises there — a pre-existing gap in the shared loader, tracked
    separately, not something this predicate can close.

    Note routable ⇒ live still holds, since a routable row is unexpired,
    allowlisted and non-empty — which is what makes the fused "healed by another
    process" return in :func:`_recover_psidts_inline` sound.
    """
    for entry in entries:
        try:
            normalized = _validate_cookie_shape(entry)
        except _cookie_semantics.CookieRowError:
            continue
        if normalized["name"] != _PSIDTS_COOKIE or not _is_allowed_auth_domain(
            normalized["domain"]
        ):
            continue
        cookie = _safe_to_cookie(entry, to_cookie)
        if cookie is not None and not _is_expired(cookie, now):
            return True
        # A structurally valid row with malformed expiry is still present on
        # disk and the name-only retry validator will see it.  It must count
        # as live here without ever attempting to construct a Cookie from the
        # malformed value a second time.
        try:
            _cookie_semantics.normalize_cookie_expiry(normalized.get("expires"))
        except _cookie_semantics.CookieRowError:
            return True
    return False


def _psidts_routes_to_rotate(
    entries: list[dict[str, Any]],
    *,
    to_cookie: _CookieConverter,
    now: float | None = None,
) -> bool:
    """Would the ``Cookie:`` header sent to ``KEEPALIVE_ROTATE_URL`` carry PSIDTS?

    This is the "should we fire the POST?" question, and it is asked of the same
    RFC 6265 machinery that will build the real request. The predecessor gate
    ranked a single domain-blind global winner by ``_auth_domain_priority`` and
    read that winner's expiry — but the POST it gates is routed, so the gate and
    the action could answer different questions about the same cookie set. A
    PSIDTS scoped to ``.notebooklm.google.com`` (or, post-rebrand,
    ``.notebook.google.com``) never reaches ``accounts.google.com``, while a
    host-scoped one on ``accounts.google.com`` — which does route — sat in the
    *lowest* priority tier (issue #2057).

    Expiry is decided by :func:`_iter_routable_psidts_cookies` against ``now``, then
    stripped from the probe copies. ``CookieJar.add_cookie_header`` resets its
    own clock from ``time.time()`` and re-applies its expiry policy on top of
    whatever it is given, so an injected ``now`` would otherwise be a
    tightening-only seam — it could mark a cookie expired but never keep one
    fresh, and a test asserting "fresh at now=200" would silently fail against
    the real wall clock. The probes exist only to answer the DOMAIN question.
    """
    probes = []
    for cookie in _iter_routable_psidts_cookies(entries, to_cookie=to_cookie, now=now):
        probe = copy.copy(cookie)
        probe.expires = None
        probes.append(probe)
    return _cookies_route_psidts(probes)


def _cookies_route_psidts(cookies: Iterable[http.cookiejar.Cookie]) -> bool:
    """Would a jar holding ``cookies`` send ``__Secure-1PSIDTS`` to the rotate URL?

    The shared jar probe behind both the pre-POST gate
    (:func:`_psidts_routes_to_rotate`, which hands it expiry-stripped copies) and
    the post-POST mint check in :func:`_attempt_rotation` /
    :func:`recover_psidts_in_memory` (which hand it the live response jar, expiry
    intact, so a rotation that somehow arrives already-expired does not count).
    """
    jar = httpx.Cookies()
    found = False
    for cookie in cookies:
        if cookie.name != _PSIDTS_COOKIE or not cookie.value:
            continue
        jar.jar.set_cookie(cookie)
        found = True
    if not found:
        return False
    request = httpx.Request("POST", _keepalive.KEEPALIVE_ROTATE_URL)
    jar.set_cookie_header(request)
    return _PSIDTS_COOKIE in _cookie_header_names(request.headers.get("cookie", ""))


def _resolve_recovery_path(path: Path | str | None) -> Path | None:
    """Resolve the effective file path for recovery, or ``None`` to decline.

    Mirrors ``_load_storage_state`` (`_auth/cookies.py`) precedence:

    - explicit ``path`` argument → use as-is (cast ``str`` to ``Path``)
    - ``NOTEBOOKLM_AUTH_JSON`` env-var set → return ``None`` (no writeable
      backing store; tracked as future-work in this module's docstring)
    - otherwise → fall back to :func:`notebooklm.paths.get_storage_path`,
      so ``load_auth_from_storage()`` with no args still triggers recovery
      on the default profile file (issue #865 critical-path coverage).

    The env-var test is *presence*, matching ``_load_storage_state`` (both now
    share :func:`notebooklm._auth.paths.resolve_auth_json_env`), not truthiness.
    They used to disagree, and an empty-string value fell through the crack: the
    loader took its env branch and raised "set but empty" without ever
    inspecting a cookie, while this resolver saw a falsy value and handed back
    the **default profile file**. Recovery then fired a ``RotateCookies`` POST
    and persisted rotated cookies to a profile the caller had deliberately
    bypassed. Found while analysing which callers can reach the gate for issue
    #2057.
    """
    if path is not None:
        return Path(path)
    if resolve_auth_json_env() is not None:
        return None
    from ..paths import get_storage_path

    return get_storage_path()


def _recover_psidts_inline(path: Path | str | None) -> bool:
    """Attempt a one-shot ``RotateCookies`` POST to mint ``__Secure-1PSIDTS``.

    Pre-conditions (all must hold; otherwise return ``False`` without firing):

    1. ``SID`` present in ``storage_path``.
    2. No ``__Secure-1PSIDTS`` in ``storage_path`` ROUTES to
       ``KEEPALIVE_ROTATE_URL`` — absent, expired, or scoped to a domain that
       never reaches ``accounts.google.com`` (a ``-1``/``None`` session-cookie
       expiry counts as present, not expired — see
       :func:`_psidts_routes_to_rotate`).
    3. Secondary binding intact (``OSID``, or ``APISID + SAPISID``). Google
       rejects ``RotateCookies`` requests that lack these — see
       :func:`notebooklm._auth.cookie_policy._has_rotatable_secondary_binding`
       (rotation eligibility — deliberately weaker than the strict
       :func:`~notebooklm._auth.cookie_policy._has_valid_secondary_binding`
       session-validity rule).
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
    semantics, atomic write, cross-process file lock). The save's coarse bool is
    an unreliable heal signal in both directions, so :func:`_attempt_rotation`
    re-reads disk (:func:`_psidts_save_succeeded`) as the sole arbiter of whether
    a fresh PSIDTS actually landed — a concurrent sibling-cookie CAS rejection
    must not invert a healthy heal, and a no-op save over a stale row must not
    fake one (issue #1273). A genuine persist failure surfaces as ``False`` so
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
    cookie_entries, cookie_names = state

    if "SID" not in cookie_names:
        logger.debug("PSIDTS recovery skipped: SID missing — session is truly broken")
        return False
    if _psidts_routes_to_rotate(cookie_entries, to_cookie=_storage_entry_to_cookie):
        return False
    if not _has_rotatable_secondary_binding(cookie_names):
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
            healed = _is_psidts_routed_on_disk(storage_path)
            logger.debug(
                "PSIDTS recovery skipped: %s held by another process (healed=%s)",
                rotate_lock_path,
                healed,
            )
            return healed
        # Re-read inside the lock: another process may have completed its
        # rotation + save between our top-of-function precondition check and
        # acquiring this flock. Mirrors ``_poke_session``'s "one last disk
        # recheck" pattern at ``_auth/keepalive.py:283-290``. The remaining
        # preconditions are re-validated against the fresh state so a concurrent
        # write that dropped SID or the secondary binding can't slip a doomed
        # POST through. Note the PSIDTS check comes FIRST and returns early, so
        # the "healed by another process" branch does not re-check SID or the
        # binding — that ordering predates this gate and is harmless: the caller
        # simply retries its preflight, which then fails honestly on SID.
        fresh = _read_storage_for_recovery(storage_path)
        if fresh is None:
            return False
        fresh_entries, fresh_names = fresh
        if _psidts_routes_to_rotate(fresh_entries, to_cookie=_storage_entry_to_cookie):
            # Nothing to fire. Reporting this as a heal is sound because
            # routed ⇒ live: a cookie that appears in the header for
            # ``accounts.google.com`` is by construction unexpired and on an
            # allowed domain, which is exactly what :func:`_psidts_is_live` asks.
            logger.debug(
                "PSIDTS recovery skipped: file healed by another process while waiting for flock"
            )
            return True
        if "SID" not in fresh_names:
            logger.debug("PSIDTS recovery skipped: SID missing after flock acquisition")
            return False
        if not _has_rotatable_secondary_binding(fresh_names):
            logger.debug(
                "PSIDTS recovery skipped: secondary binding incomplete after flock acquisition"
            )
            return False
        return _attempt_rotation(storage_path, fresh_entries)


def _read_storage_for_recovery(
    storage_path: Path,
) -> tuple[list[dict], set[str]] | None:
    """Load + filter + name-index storage_state for the recovery preconditions.

    Returns ``(cookie_entries, cookie_names)`` on success, or ``None`` on any
    load/parse failure (caller treats this as "decline recovery").
    ``cookie_names`` is the domain-filtered name view used by the ``SID`` and
    secondary-binding preconditions (see :func:`_recovery_cookie_names`).
    ``cookie_entries`` is the unfiltered list — both the PSIDTS predicates
    (:func:`_psidts_routes_to_rotate`, :func:`_psidts_is_live`) and the request-jar
    builder :func:`_build_recovery_jar` apply their own row filter, and the
    predicates need the raw ``expires`` that a name-only view cannot carry.

    The narrow exception scope catches the documented raise sites of
    ``_load_storage_state`` (``OSError`` for missing file,
    ``json.JSONDecodeError`` for malformed JSON) and lets unexpected
    ``ValueError`` propagate as an implementation bug.
    """
    try:
        storage_state = _load_storage_state(storage_path)
    except _auth_cookies.StorageStateValidationError as exc:
        logger.debug(
            "PSIDTS recovery skipped: invalid storage state (%s)",
            type(exc).__name__,
        )
        return None
    except (OSError, json.JSONDecodeError) as exc:
        logger.debug(
            "PSIDTS recovery skipped: cannot read storage state (%s)",
            type(exc).__name__,
        )
        return None
    if not isinstance(storage_state, dict):
        return None
    raw_entries = storage_state.get("cookies", [])
    if not isinstance(raw_entries, list):
        return None
    cookie_entries: list[dict] = [entry for entry in raw_entries if isinstance(entry, dict)]
    return cookie_entries, _recovery_cookie_names(cookie_entries)


def _is_psidts_persisted(storage_path: Path) -> bool:
    """Quick re-read: is a live ``__Secure-1PSIDTS`` currently on disk?

    Used after a held-flock skip to detect when another process has just healed
    the file, and after our own save to decide whether the heal landed. Treats
    any load/parse failure as "not persisted" rather than raising — the caller
    will retry. A present-but-expired PSIDTS counts as *not* persisted so a
    stale on-disk row doesn't masquerade as a heal.

    Asks :func:`_psidts_is_live` — the domain-blind "did it land?" question —
    NOT the routed "should we fire?" question. The two are deliberately
    different; see :func:`_psidts_is_live` for why routing here would introduce
    a false negative.
    """
    state = _read_storage_for_recovery(storage_path)
    if state is None:
        return False
    entries, _ = state
    return _psidts_is_live(entries, to_cookie=_storage_entry_to_cookie)


def _is_psidts_routed_on_disk(storage_path: Path) -> bool:
    """Return whether the current disk state routes PSIDTS to RotateCookies."""
    state = _read_storage_for_recovery(storage_path)
    if state is None:
        return False
    entries, _ = state
    return _psidts_routes_to_rotate(entries, to_cookie=_storage_entry_to_cookie)


def _psidts_save_succeeded(
    result: _auth_storage.CookieSaveResult | bool, storage_path: Path
) -> bool:
    """Did a fresh ``__Secure-1PSIDTS`` actually land on disk after the save?

    The coarse ``CookieSaveResult.ok`` bool from
    :func:`~notebooklm._auth.storage.save_cookies_to_storage` is *not* a reliable
    proxy for "PSIDTS healed", in either direction:

    - ``ok`` is ``False`` whenever *any* key is CAS-rejected, even when a fresh
      PSIDTS is on disk — both the benign "an unrelated sibling cookie lost the
      race, our PSIDTS wrote through" case (issue #1273) and the "our PSIDTS
      delta was rejected because a sibling already persisted a fresh PSIDTS
      first" case leave disk healthy.
    - ``ok`` is ``True`` even when the save was a no-op that left a *stale*
      PSIDTS untouched: if ``RotateCookies`` 200s without minting a new cookie,
      the expired on-disk row lingers in the request jar, the delta is empty,
      and the save reports success while disk is still unhealed.

    So disk — not the save bool — is the sole arbiter. Re-read it via
    :func:`_is_psidts_persisted` (the domain-blind "did it land?" question, NOT
    the routed precondition gate — see :func:`_psidts_is_live`) and accept the
    heal iff a live PSIDTS is stored, so a stale or expired row (ours or a
    sibling's) doesn't masquerade as a heal. On a decline, the coarse ``result``
    is folded into a diagnostic warning here (the only thing it is used for) so
    callers stay a single boolean branch.
    """
    if _is_psidts_persisted(storage_path):
        return True
    logger.warning(
        "Inline PSIDTS recovery: %s did not persist (save ok=%s); on-disk state still lacks it",
        _PSIDTS_COOKIE,
        getattr(result, "ok", result),
    )
    return False


def _recovery_observation(
    entries: Iterable[Any],
) -> _auth_storage.RecoveryCookieObservation:
    """Capture value-only recovery observations before the RotateCookies POST."""
    observed: dict[_auth_storage.CookieSnapshotKey, set[str | None]] = {}
    for raw_entry in entries:
        # Recovery must observe the identity even when the source row cannot
        # enter a request jar.  Empty/non-string values and malformed expiry
        # are exactly the rows a fresh RotateCookies response must replace.
        # Keep the observation value-free for unusable rows; a non-empty value
        # is retained only for exact CAS matching.
        if not isinstance(raw_entry, dict):
            continue
        name = raw_entry.get("name")
        domain = raw_entry.get("domain")
        path = raw_entry.get("path")
        if (
            not isinstance(name, str)
            or not name
            or name not in _RECOVERY_TARGET_COOKIE_NAMES
            or not isinstance(domain, str)
            or not domain
            or not _is_allowed_auth_domain(domain)
            or (path is not None and not isinstance(path, str))
        ):
            continue
        key = _auth_storage.CookieSnapshotKey(name, domain, path or "/")
        value = raw_entry.get("value")
        observed.setdefault(key, set()).add(value if isinstance(value, str) and value else None)
    return {key: frozenset(values) for key, values in observed.items()}


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

    jar = _build_recovery_jar(cookie_entries, _storage_entry_to_cookie)

    # ``httpx.Client(cookies=jar)`` copies the source jar into a private client
    # jar; Set-Cookie responses land in ``client.cookies``, not in ``jar``. So
    # we snapshot and check the *client's* jar, mirroring how the async
    # keepalive in ``_runtime.lifecycle.save_cookies`` reads ``client.cookies``.
    observation = _recovery_observation(cookie_entries)
    try:
        with _rotation_http_client(jar) as client:
            snapshot = _auth_storage.snapshot_cookie_jar(client.cookies)
            _rotate_post_sync(client)
            rotated_jar = client.cookies
            # ROUTABLE, not merely present-by-name. The request jar still holds
            # whatever PSIDTS was already on disk, so a name-only check would see
            # that pre-existing cookie and report a mint that never happened —
            # defeating the withheld-rotation detection below. Asking whether a
            # PSIDTS now routes to the rotate URL is also proof of NEWNESS here:
            # we only reach this line because the gate found nothing routable, so
            # anything routable now must have arrived in the response.
            psidts_minted = _cookies_route_psidts(rotated_jar.jar)
    except httpx.HTTPError as exc:
        logger.debug(
            "Inline PSIDTS recovery POST failed (non-fatal): %s",
            type(exc).__name__,
        )
        return False

    if not psidts_minted:
        logger.debug(
            "Inline PSIDTS recovery: RotateCookies returned 2xx but did not "
            "mint a %s that routes to %s — Google may be withholding the rotation",
            _PSIDTS_COOKIE,
            _keepalive.KEEPALIVE_ROTATE_URL,
        )
        return False

    # ``save_cookies_to_storage`` returns a falsy result (not raises) on every
    # persist-failure path: missing file, invalid payload, CAS conflict,
    # atomic-write failure (see ``_auth/storage.py:380-429``). The bare
    # ``except`` below catches the *unexpected* raises only (future refactor
    # could change the contract).
    #
    # We ask for the detailed ``CookieSaveResult`` (``return_result=True``) for
    # the diagnostic log only — the coarse bool is an unreliable heal signal in
    # both directions (a sibling-cookie CAS rejection falsely reads ``ok=False``
    # while PSIDTS healed; a no-op save over a stale PSIDTS falsely reads
    # ``ok=True``). Whether PSIDTS actually healed is decided by re-reading disk
    # in :func:`_psidts_save_succeeded` (issue #1273).
    try:
        result = _auth_storage.save_cookies_to_storage(
            rotated_jar,
            storage_path,
            original_snapshot=snapshot,
            recovery_observation=observation,
            return_result=True,
        )
    except Exception as exc:  # noqa: BLE001 - persistence failure is non-fatal here
        logger.warning(
            "Inline PSIDTS recovery: persist raised %s",
            type(exc).__name__,
        )
        return False

    if not _psidts_save_succeeded(result, storage_path):
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
    normalized = _cookie_semantics.sanitize_cookie_entry(entry)
    return _auth_cookies._cookie_from_normalized_entry(normalized, http_only_key="http_only")


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
    2. No ``__Secure-1PSIDTS`` ROUTES to ``KEEPALIVE_ROTATE_URL`` — absent,
       expired, or scoped to a domain that never reaches
       ``accounts.google.com`` (a ``-1``/``None`` session-cookie expiry counts
       as present, not expired — see :func:`_psidts_routes_to_rotate`).
    3. Secondary binding intact (``OSID``, or ``APISID + SAPISID``).

    On success, mutates ``rookiepy_cookies`` so the rotated
    ``__Secure-1PSIDTS`` (and ``__Secure-3PSIDTS``) entries are present in
    rookiepy's snake_case format. A rotated cookie that shares its
    ``(name, domain, path)`` identity with an existing row REPLACES that row
    in place (the rotation is the value we want to persist); otherwise it is
    appended. This keeps exactly one row per identity so split-state recovery
    cannot write a duplicate ``__Secure-3PSIDTS`` row to ``storage_state.json``
    (issue #1523). Downstream
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
    cookie_names = _recovery_cookie_names(rookiepy_cookies)

    if "SID" not in cookie_names:
        logger.debug("In-memory PSIDTS recovery skipped: SID missing")
        return False
    if _psidts_routes_to_rotate(rookiepy_cookies, to_cookie=_rookiepy_entry_to_cookie):
        return False
    if not _has_rotatable_secondary_binding(cookie_names):
        logger.debug(
            "In-memory PSIDTS recovery skipped: secondary binding incomplete "
            "(need OSID, or both APISID and SAPISID)"
        )
        return False

    jar = _build_recovery_jar(rookiepy_cookies, _rookiepy_entry_to_cookie)

    try:
        with _rotation_http_client(jar) as client:
            _rotate_post_sync(client)
            rotated_cookies = list(client.cookies.jar)
    except httpx.HTTPError as exc:
        logger.debug(
            "In-memory PSIDTS recovery POST failed (non-fatal): %s",
            type(exc).__name__,
        )
        return False

    # ROUTABLE, not merely present-by-name — see the matching check in
    # :func:`_attempt_rotation`. The request jar carries any PSIDTS the caller
    # already had, so a name-only test would accept a withheld rotation.
    if not _cookies_route_psidts(rotated_cookies):
        logger.debug(
            "In-memory PSIDTS recovery: RotateCookies returned 2xx but did not "
            "mint a %s that routes to %s — Google may be withholding the rotation",
            _PSIDTS_COOKIE,
            _keepalive.KEEPALIVE_ROTATE_URL,
        )
        return False

    # Index the source rows by RFC 6265 identity so the replace-in-place
    # contract above can be honoured. Split-state recovery (#1523) is what makes
    # it necessary: __Secure-1PSIDTS is missing/expired (so recovery fires) while
    # a fresh __Secure-3PSIDTS is already present — RotateCookies rotates BOTH,
    # and a blind append leaves a duplicate __Secure-3PSIDTS (and a stale
    # __Secure-1PSIDTS twin) row with no analog in any real browser jar. Letting
    # the rotated occurrence win mirrors the last-occurrence-wins dedup in
    # ``filter_storage_state_cookies_by_domain_policy`` (#1513). ``path or "/"``
    # matches the normalization the loaders and save_cookies_to_storage use.
    index_by_identity: dict[_auth_storage.CookieSnapshotKey, list[int]] = {}
    for pos, entry in enumerate(rookiepy_cookies):
        identity = _auth_storage._stored_cookie_snapshot_key(entry)
        if identity is None:
            continue
        index_by_identity.setdefault(identity, []).append(pos)

    # ``LSID`` rides along: when ``OSID`` is absent the fallback binding needs
    # all three of ``APISID``, ``SAPISID`` and ``LSID`` (#1977) — ``LSID`` is one
    # of the three, not half of a pair. This path allows the POST on ``APISID``+``SAPISID``
    # alone — so a rotation that *supplies* the missing ``LSID`` is exactly the
    # case worth keeping. Dropping it here would discard the cookie that makes
    # the set usable. The file-backed ``_attempt_rotation`` already persists the
    # whole rotated jar and so never had this gap.
    replacements: dict[int, dict[str, Any]] = {}
    removals: set[int] = set()
    for cookie in rotated_cookies:
        if cookie.name not in _ROTATION_MERGE_COOKIE_NAMES:
            continue
        if not cookie.value or not cookie.domain:
            continue
        rotated_entry = {
            "name": cookie.name,
            "value": cookie.value,
            "domain": cookie.domain,
            "path": cookie.path or "/",
            "expires": cookie.expires,
            "secure": True,
            "http_only": True,
        }
        identity = _auth_storage.CookieSnapshotKey(cookie.name, cookie.domain, cookie.path or "/")
        existing = index_by_identity.get(identity)
        if not existing:
            rookiepy_cookies.append(rotated_entry)
        else:
            # Same (name, domain, path) already present — overwrite one row and
            # remove every exact duplicate so malformed/stale twins cannot be
            # serialized again. Domain variants remain literal and independent.
            replacements[existing[0]] = rotated_entry
            removals.update(existing[1:])

    for pos, entry in replacements.items():
        rookiepy_cookies[pos] = entry
    for pos in sorted(removals, reverse=True):
        del rookiepy_cookies[pos]

    logger.info(
        "Recovered %s via in-memory RotateCookies POST (browser-cookies extraction)",
        _PSIDTS_COOKIE,
    )
    return True


# The browser-extraction validator lives in a leaf so this recovery module stays
# under the repository's 1,000-line module budget.  Import it lazily to keep the
# public compatibility name while avoiding a module-level circular import.
def validate_with_recovery(
    rookiepy_cookies: list[dict[str, Any]],
) -> tuple[dict[str, Any], ValueError | None]:
    """Delegate browser-state validation to its size-bounded leaf module."""
    from .browser_cookie_recovery import validate_with_recovery as _validate_with_recovery

    return _validate_with_recovery(rookiepy_cookies)
