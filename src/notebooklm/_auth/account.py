"""Google account discovery and profile metadata helpers for authentication."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
from filelock import FileLock

from .._atomic_io import atomic_write_json
from .._env import get_base_url
from .._url_utils import is_google_auth_redirect

logger = logging.getLogger("notebooklm.auth")


@dataclass(frozen=True)
class Account:
    """A Google account discovered via authuser=N probing.

    Attributes:
        authuser: The integer index used in ``?authuser=N`` URL parameters.
            Index 0 is the default account; subsequent indices follow the
            order Google reports for the browser session.
        email: The account's email address as it appears in the NotebookLM
            page's ``WIZ_global_data`` block.
        is_default: True only for the account at ``authuser=0``.
        browser_profile: For Chromium-family browsers with multiple
            user-data profiles, the on-disk directory name (``"Default"``,
            ``"Profile 1"``) the cookies came from. ``None`` for non-chromium
            browsers and for the legacy single-jar path where source isn't
            tracked.
    """

    authuser: int
    email: str
    is_default: bool
    browser_profile: str | None = None


# Hard cap on how many ``authuser`` indices to probe before giving up.
# Google supports up to ~10 simultaneously signed-in accounts in a browser
# session; ten covers every realistic case and bounds the worst-case probe.
MAX_AUTHUSER_PROBE = 10

# Bound on how long promote_legacy_account's write will contend for the
# storage lock (#2103 PR-0 review). Deliberately short, unlike the usual 90s
# full-file-RMW deadline: promote_legacy_account now runs inside
# read_account_metadata, called from many `async` code paths that assume a
# fast, lock-free read (client.get_account_email, token-route resolution).
# Promotion is best-effort by design — a failed/timed-out promotion falls
# back to the legacy record, never breaks the read — so giving up fast under
# contention and taking that fallback is strictly the right trade-off.
_PROMOTION_LOCK_DEADLINE_SECONDS = 2.0

# Per-process log-spam throttle for promotion failures (#2103 PR-0 review):
# read_account_metadata (and transitively this) is now on the request path
# (token-route resolution reads it per RPC call), so a persistently failing
# promotion must not warn twice per request forever. Values are canonical
# storage_path strings that have already logged one WARNING; unbounded
# growth is not a concern — real deployments have a handful of profiles, not
# an unbounded set of distinct paths.
_PROMOTION_WARNED_PATHS: set[str] = set()

# Local-parts of well-known non-user emails that NotebookLM may embed in page
# chrome (footer links, support contacts) and must not be misread as the
# active account. Combined with ``_NON_USER_EMAIL_DOMAINS`` so we only drop
# the address when *both* match — otherwise legitimate Workspace users like
# ``support@customer.com`` would be filtered out.
_NON_USER_EMAIL_LOCALS = frozenset(
    {
        "abuse",
        "feedback",
        "info",
        "mail-noreply",
        "googlemail-noreply",
        "no-reply",
        "noreply",
        "press",
        "privacy",
        "support",
    }
)
_NON_USER_EMAIL_DOMAINS = frozenset({"google.com", "accounts.google.com", "gmail.com"})

# Match a quoted email address, e.g. ``"alice@example.com"``. Mirrors how
# emails appear in the page's WIZ_global_data JSON.
_EMAIL_RE = re.compile(r'"([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})"')


def extract_email_from_html(html: str) -> str | None:
    """Extract the active user's email from a NotebookLM page response.

    Returns the first plausible Google account email found in the HTML,
    skipping addresses that look like Google's own contact endpoints
    (e.g. ``support@google.com``, ``noreply@accounts.google.com``).

    Args:
        html: Page HTML from ``<configured base URL>/?authuser=N``.

    Returns:
        The account's email, or ``None`` if no plausible address was found
        (typically because the response was a login redirect or the page
        structure changed).
    """
    for match in _EMAIL_RE.finditer(html):
        email = match.group(1)
        local, _, domain = email.partition("@")
        if local.lower() in _NON_USER_EMAIL_LOCALS and domain.lower() in _NON_USER_EMAIL_DOMAINS:
            continue
        return email
    return None


# Chromium-style User-Agent for ``enumerate_accounts``. Without a real-browser
# UA, Google serves a stripped-down page that omits the WIZ_global_data block
# (and therefore the active user's email), and ``extract_email_from_html``
# returns None — looking like "no signed-in account". Empirically validated
# against ``<configured base URL>/?authuser=N``.
_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
)


async def _probe_authuser(client: httpx.AsyncClient, n: int) -> str | None:
    """Probe one ``authuser`` index and return the active email or ``None``.

    Returns ``None`` for auth-redirect or unparseable responses; lets the
    caller decide whether that means "past the last account" or a real error.
    HTTP transport errors propagate.

    Only checks the *final* URL for an auth redirect. The page body is not
    scanned because a healthy NotebookLM page legitimately contains many
    ``accounts.google.com`` links (account chooser, manage-account menu)
    that would fool ``contains_google_auth_redirect``.
    """
    response = await client.get(
        f"{get_base_url()}/?{authuser_query(n)}",
        headers={"User-Agent": _BROWSER_UA, "Accept": "text/html,*/*"},
    )
    if response.status_code != 200:
        return None
    if is_google_auth_redirect(str(response.url)):
        return None
    return extract_email_from_html(response.text)


async def enumerate_accounts(
    cookie_jar: httpx.Cookies,
    *,
    max_authuser: int = MAX_AUTHUSER_PROBE,
    poke_session: Callable[[httpx.AsyncClient, Path | None], Awaitable[None]] | None = None,
) -> list[Account]:
    """Enumerate Google accounts visible to the given cookie jar.

    Probes ``<configured base URL>/?authuser=N`` (see
    :func:`~notebooklm._env.get_base_url`) for ``N`` in
    ``0..max_authuser`` and parses the active user's email from each response.

    Stop condition: when the email at index ``N>0`` matches the email at
    index 0, Google has silently fallen back to the default account, meaning
    ``N`` is past the real count. Without this check the caller would record
    duplicate phantom accounts; Google does not redirect to login in this
    case.

    Args:
        cookie_jar: ``httpx.Cookies`` jar with auth cookies. Not mutated.
        max_authuser: Hard cap on indices probed (default
            :data:`MAX_AUTHUSER_PROBE`).
        poke_session: Optional freshness hook run before probes. The public
            ``notebooklm.auth`` facade passes the standard keepalive hook.

    Returns:
        Accounts ordered by ``authuser`` index. ``is_default`` is true for
        index 0 only.

    Raises:
        ValueError: If ``authuser=0`` itself does not return a signed-in
            account (cookies expired or invalid).
        httpx.HTTPError: If the HTTP transport fails.
    """
    from .._curl_cffi_transport import resolve_transport_factory

    async with resolve_transport_factory()(
        cookies=cookie_jar,
        follow_redirects=True,
        timeout=httpx.Timeout(10.0, read=60.0),
    ) as client:
        # The browser's on-disk cookie DB rotates ``__Secure-1PSIDTS`` every
        # few minutes, but only when Chrome itself is actively running. A
        # ``--browser-cookies`` extraction against an idle Chrome lands here
        # with a stale SIDTS — the SID is fine, but the app host
        # responds with a redirect to ``accounts.google.com`` and we'd
        # incorrectly conclude the user is signed out. Poke once to fetch
        # fresh SIDTS via Set-Cookie before the probes start.
        if poke_session is not None:
            await poke_session(client, None)
        default_email = await _probe_authuser(client, 0)
        if default_email is None:
            raise ValueError(
                "Authentication expired or invalid; "
                "authuser=0 did not return a signed-in account. "
                "Run 'notebooklm login' to re-authenticate."
            )
        accounts = [Account(authuser=0, email=default_email, is_default=True)]
        for n in range(1, max_authuser + 1):
            email = await _probe_authuser(client, n)
            if email is None or email == default_email:
                break
            accounts.append(Account(authuser=n, email=email, is_default=False))
        return accounts


_ACCOUNT_CONTEXT_KEY = "account"

# The unified atomic profile-state format embeds account metadata
# inside ``storage_state.json`` under a ``notebooklm`` namespace key, so
# a single ``atomic_write_json`` covers both cookies and account in one
# crash-safe commit. ``version`` is bumped only when the in-band schema
# changes incompatibly — version 1 is the initial shape.
_STORAGE_NAMESPACE_KEY = "notebooklm"
_STORAGE_NAMESPACE_VERSION = 1


def _account_context_path(storage_path: Path) -> Path:
    """Return the context.json path that annotates ``storage_path``.

    Legacy two-file layout: this sibling held ``account`` metadata before
    the unified format embedded it in ``storage_state.json``. Post-migration,
    it keeps CLI context state (``notebook_id``, ``conversation_id``) but no
    longer stores the ``account`` key.
    """
    return storage_path.with_name("context.json")


def _read_in_band_account(storage_path: Path) -> dict[str, Any]:
    """Read account metadata from inside ``storage_state.json``.

    Returns ``{}`` when the namespace key is missing, malformed, or the file
    cannot be read. Callers fall back to the legacy sibling ``context.json``.
    """
    if not storage_path.exists():
        return {}
    try:
        data = json.loads(storage_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.debug("in-band account read failed at %s: %s", storage_path, e)
        return {}
    return read_account_metadata_from_storage_state(data)


def read_account_metadata_from_storage_state(storage_state: Any) -> dict[str, Any]:
    """Read in-band account metadata from parsed Playwright storage state."""
    if not isinstance(storage_state, dict):
        return {}
    namespace = storage_state.get(_STORAGE_NAMESPACE_KEY)
    if not isinstance(namespace, dict):
        return {}
    account = namespace.get(_ACCOUNT_CONTEXT_KEY)
    return account if isinstance(account, dict) else {}


def _read_legacy_account(storage_path: Path) -> dict[str, Any]:
    """Read the pre-v0.5.0 sibling ``context.json`` account record.

    Consumed ONLY by :func:`promote_legacy_account` (the one-shot in-band
    migration). Never a standing read path — see ``read_account_metadata``.
    """
    context_path = _account_context_path(storage_path)
    if not context_path.exists():
        return {}
    try:
        data = json.loads(context_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.debug("account metadata read failed at %s: %s", context_path, e)
        return {}
    if not isinstance(data, dict):
        return {}
    account = data.get(_ACCOUNT_CONTEXT_KEY)
    return account if isinstance(account, dict) else {}


def read_account_metadata(storage_path: Path | None) -> dict[str, Any]:
    """Read profile account metadata, self-healing a legacy two-file profile.

    Unified layout: account metadata lives inside ``storage_state.json``
    under the ``notebooklm`` namespace key. This reader never returns a raw
    pass-through of the pre-v0.5.0 sibling ``context.json`` record — a
    standing read fallback that silently trusted an unmigrated legacy value
    was the wrong-account hazard #2103's PR-0 closes (a legacy ``authuser``
    the fallback missed, or a stale one it kept trusting forever, could
    silently route requests to a different signed-in Google account).

    Instead, every call is the single chokepoint for
    :func:`promote_legacy_account`: the fast path (in-band already present —
    the overwhelming majority of calls, once any profile has been read once)
    costs one dict lookup; only a not-yet-migrated legacy profile pays for the
    one-shot promotion (a context.json read, and — the FIRST time only — an
    atomic in-band write). This is strictly safer than the pre-PR-0 behavior:
    the result is always genuinely in-band truth, migrated once and durably,
    rather than a value re-derived from an unmigrated file on every call.

    The ``account`` object records the Google ``authuser`` index used when
    the profile was authenticated. Profiles from before account-binding
    shipped (and profiles for users with a single Google account) have no
    account metadata and use ``authuser=0``.

    A transient promotion failure (disk full, permission error, lock
    timeout) does NOT collapse to ``{}`` — this reader falls back to the
    legacy record it already read successfully, sanitized identically to
    what promotion would have embedded (see
    :func:`_sanitize_legacy_account_record`). Returning ``{}`` on a write
    failure would reintroduce, via a different trigger, the exact
    wrong-account-routing hazard this migration exists to close.

    Args:
        storage_path: Path to ``storage_state.json``. ``None`` means the
            profile is loaded from ``NOTEBOOKLM_AUTH_JSON`` (no sibling to
            promote from — env-auth profiles skip promotion entirely).

    Returns:
        Parsed metadata dict, or ``{}`` only when no legacy OR in-band
        record exists at all.
    """
    if storage_path is None:
        return {}
    in_band = _read_in_band_account(storage_path)
    if in_band:
        return in_band
    legacy = _read_legacy_account(storage_path)
    if not legacy:
        return {}
    promote_legacy_account(storage_path)
    # Re-check in-band regardless of promote_legacy_account's return value —
    # another caller may have promoted+scrubbed this same profile concurrently
    # even when OUR call found "nothing to do" (a benign race, not a failure).
    # Only fall back to the legacy record already in hand when in-band is
    # STILL absent after that recheck: promotion for THIS profile genuinely
    # did not land (a transient write/lock error — logged inside
    # promote_legacy_account). Returning {} here instead of the record we
    # already read successfully would collapse a transient infra failure into
    # "no account", silently causing exactly the wrong-account-routing hazard
    # this migration exists to close.
    in_band_after = _read_in_band_account(storage_path)
    if in_band_after:
        return in_band_after
    return _sanitize_legacy_account_record(legacy)


def _sanitize_legacy_account_record(legacy: dict[str, Any]) -> dict[str, Any]:
    """Normalize a raw legacy ``context.json[account]`` dict identically to how
    :func:`promote_legacy_account` sanitizes it before embedding in-band, so a
    caller sees the same values whether promotion succeeded or (on a
    transient write failure) the caller fell back to the legacy record
    directly. Mirrors ``get_authuser_for_storage`` / ``get_account_email_for_storage``'s
    own sanitization rules."""
    raw_authuser = legacy.get("authuser")
    result: dict[str, Any] = {
        "authuser": raw_authuser if type(raw_authuser) is int and raw_authuser >= 0 else 0
    }
    raw_email = legacy.get("email")
    if isinstance(raw_email, str) and raw_email.strip():
        result["email"] = raw_email.strip()
    return result


def promote_legacy_account(storage_path: Path) -> bool:
    """One-shot migration: move a legacy sibling ``account`` record in-band.

    The pre-v0.5.0 two-file layout stored account metadata (``authuser`` /
    ``email``) in the sibling ``context.json``. This helper promotes that
    record into ``storage_state.json`` via the canonical storage writer and
    strips the legacy key, so :func:`read_account_metadata` never loses a
    user's account binding — it prefers the promoted in-band record but
    falls back to the (still-present-on-failure) legacy record if this
    function returns ``False`` because a write attempt failed rather than
    because there was nothing to promote.

    Ordering is crash-safe for the BINDING (never lost), not for the RESIDUE
    (not guaranteed promptly cleaned up): the in-band embed commits first
    (atomic, under the canonical storage lock), the legacy strip second
    (atomic, under the context lock). A crash in between leaves both records
    present — the account binding is never lost either way, which is the
    correctness property this function exists for. But the NEXT call does
    NOT reliably take a strip-only branch: :func:`read_account_metadata`'s
    fast path (``if in_band: return in_band``) returns as soon as in-band is
    present and never calls this function again, so a crash-mid-flight
    residue can survive indefinitely rather than being cleaned up on the very
    next read. This is a privacy nicety, not a correctness gap (the
    authoritative binding is the in-band record, already committed) — it is
    NOT worth a ``context.json`` existence probe on every read's fast path to
    close eagerly. A subsequent ``write_account_metadata`` /
    ``clear_account_metadata`` call for the same profile does still strip it
    (both call ``_drop_legacy_account_key`` unconditionally).

    When the in-band record already exists for another reason — including a
    CONCURRENT fresh login/account-switch that won a race against THIS call
    (see ``only_if_absent`` below) — any stale legacy key IS stripped
    immediately as residue cleanup (privacy: the old account email must not
    live on at rest after a re-login); that path runs through this function,
    not through the fast path above.

    Race-safe: the write is issued with ``only_if_absent=True``, so the
    decision "is in-band still empty" is made under the SAME lock as the
    write, not by a separate unlocked check beforehand. Without that, a
    concurrent fresh login could commit its new record in the gap between
    this function's own (now-removed) unlocked check and its locked write,
    and this call would silently overwrite it with the stale legacy values
    it had already captured — reintroducing the wrong-account hazard this
    whole migration exists to close, via a race instead of a stale read.

    Best-effort by design — called from :func:`read_account_metadata` (the
    account chokepoint) on every read where in-band is absent, so a promotion
    failure must degrade to the pre-promotion state, never break the read.
    Returns ``True`` only when a legacy record was embedded by THIS call
    (``False`` both when there was nothing to promote and when a concurrent
    writer won the race — either way no action was needed from this call).

    Never creates ``storage_state.json``: if it doesn't exist yet, promotion
    is skipped (returning ``False``) rather than synthesizing a cookie-less
    file to embed into. Without this guard, a plain READ (``profile list``,
    ``auth check``) on a legacy profile whose cookies had never been captured
    (or had been removed, e.g. by the ``COOKIE_VALIDATION_FAILED`` not-exists
    contract) would itself CREATE a persistent ``storage_state.json`` with no
    cookies at all — and ``_app/profile.py``'s ``authenticated=storage.exists()``
    check runs immediately after calling this (transitively, via
    ``read_account_metadata``), so it would flip from correctly reporting "not
    authenticated" to incorrectly reporting "authenticated" for a profile with
    zero cookies, purely as a side effect of having been looked at.

    Args:
        storage_path: Path to ``storage_state.json`` (must be a real file
            path — env-auth profiles have no sibling and are skipped by the
            caller).
    """
    if not storage_path.exists():
        return False
    legacy = _read_legacy_account(storage_path)
    if not legacy:
        return False
    sanitized = _sanitize_legacy_account_record(legacy)
    try:
        from . import storage_writer  # local import: avoid the account<->writer cycle

        # only_if_absent=True: the decision "should this write happen" is made
        # HERE, under the writer's own lock, not by a separate unlocked
        # _read_in_band_account check beforehand — a check-then-act split
        # would let a concurrent fresh login/account-switch land in the gap
        # and then be silently overwritten by these stale legacy values.
        #
        # deadline_seconds is short (NOT the usual 90s full-file-RMW deadline):
        # this call now runs inside read_account_metadata, which many `async`
        # callers (client.get_account_email, token-route resolution) treat as
        # a fast, lock-free read. On contention, give up quickly and take the
        # legacy-record fallback rather than freeze an event loop for up to
        # 90s over a best-effort migration.
        promoted = storage_writer.update_account_metadata(
            storage_path,
            authuser=sanitized["authuser"],
            email=sanitized.get("email"),
            only_if_absent=True,
            deadline_seconds=_PROMOTION_LOCK_DEADLINE_SECONDS,
        )
    except Exception as e:  # noqa: BLE001 — load path must not fail on promotion
        # WARNING (once per path per process), not debug: a persistent cause
        # (read-only profile dir, full disk) means every read of this profile
        # falls back to the legacy record (read_account_metadata) — an
        # operator needs a default-visible signal that migration is stuck
        # failing, not one gated behind -v/--debug. But this now runs on the
        # request path (token-route resolution reads it per RPC call, via
        # get_authuser_for_storage / get_account_email_for_storage), so an
        # UNTHROTTLED warning on a persistently failing profile would log
        # twice per request forever — throttled to the first occurrence per
        # path per process; every subsequent failure for the same path still
        # logs, at debug, so -v/--debug retains full detail.
        canonical = str(storage_path)
        if canonical not in _PROMOTION_WARNED_PATHS:
            _PROMOTION_WARNED_PATHS.add(canonical)
            logger.warning(
                "Legacy account promotion failed for %s: %s (further failures for this "
                "profile are logged at debug level)",
                storage_path,
                e,
            )
        else:
            logger.debug("Legacy account promotion failed for %s: %s", storage_path, e)
        return False
    # Reached whether we promoted or lost a race to a concurrent writer —
    # either way in-band now holds a real record, so the legacy residue is
    # safe (and, for privacy, necessary) to scrub. _drop_legacy_account_key
    # already swallows every realistic failure internally (OSError family,
    # including filelock.Timeout — a TimeoutError/OSError subclass — and
    # JSONDecodeError), but this call is NOT inside the try/except above, and
    # read_account_metadata calls this function with no try/except of its
    # own, trusting it never to raise. Wrap defensively anyway: an embed that
    # already committed must not be erased by an unexpected exception in an
    # unrelated cosmetic cleanup step — that would violate this function's
    # own "never break the read" contract for a reason that has nothing to do
    # with the embed's success.
    try:
        _drop_legacy_account_key(storage_path)
    except Exception as e:  # noqa: BLE001 — cosmetic cleanup must not undo a committed embed
        logger.warning("Legacy account context cleanup failed for %s: %s", storage_path, e)
    if promoted:
        logger.info("Promoted legacy account metadata in-band for %s", storage_path)
    return promoted


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


def format_authuser_value(authuser: int = 0, account_email: str | None = None) -> str:
    """Return the explicit NotebookLM auth routing value.

    Google accepts either an integer account index or the account email in the
    ``authuser`` field. Email is stable across browser account reordering, so it
    wins when available; otherwise callers retain the existing integer behavior.
    """
    if account_email:
        stripped = account_email.strip()
        if stripped:
            return stripped
    return str(authuser)


def authuser_query(authuser: int = 0, account_email: str | None = None) -> str:
    """Return a URL-encoded ``authuser=...`` query string."""
    return urlencode({"authuser": format_authuser_value(authuser, account_email)})


def _drop_legacy_account_key(storage_path: Path) -> None:
    """Scrub the legacy ``account`` key from the sibling ``context.json``.

    Preserves all other CLI context state (``notebook_id``,
    ``conversation_id``, …). Best-effort: a failure here does not abort the
    in-band write. Since the legacy READ path was removed (the reader is
    in-band-only; :func:`promote_legacy_account` owns the migration), this
    survives purely as a privacy scrub — a stale legacy key would leave the
    account email at rest forever with no reader and no writer to remove it.
    Called by ``write_account_metadata`` / ``clear_account_metadata`` /
    ``promote_legacy_account`` (this module) and ``storage_writer.replace_from_login``
    (the CLI login writer, after its own atomic write).
    """
    context_path = _account_context_path(storage_path)
    if not context_path.exists():
        return
    lock_path = context_path.with_suffix(context_path.suffix + ".lock")
    try:
        with FileLock(str(lock_path), timeout=10.0):
            if not context_path.exists():
                return
            try:
                data = json.loads(context_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as e:
                logger.debug("legacy account-key cleanup skipped at %s: %s", context_path, e)
                return
            if not isinstance(data, dict) or _ACCOUNT_CONTEXT_KEY not in data:
                return
            del data[_ACCOUNT_CONTEXT_KEY]
            if data:
                atomic_write_json(context_path, data)
            else:
                context_path.unlink()
    except OSError as e:
        # Best-effort migration; the in-band reader wins.
        logger.debug("legacy account-key cleanup failed at %s: %s", context_path, e)


def write_account_metadata(storage_path: Path, *, authuser: int, email: str | None = None) -> None:
    """Persist account metadata atomically inside ``storage_state.json``.

    The account record lands under the ``notebooklm`` namespace key so the
    (cookies, account) pair commits together via a single
    :func:`atomic_write_json`. An external reader observing the file
    mid-update sees either the fully-old or fully-new commit — never a mix.

    The legacy sibling ``context.json[account]`` is best-effort cleaned up
    after the in-band write succeeds. CLI context state in the same file
    (``notebook_id`` / ``conversation_id``) is preserved.

    Args:
        storage_path: Path to ``storage_state.json``. The file is created
            with empty ``cookies`` / ``origins`` arrays if missing — matching
            the previous semantics of "writing account metadata never fails
            because cookies haven't been written yet."
        authuser: ``authuser`` index used when extracting cookies for this
            profile (0 for the default account).
        email: Optional account email to record alongside the index.
    """
    # The in-band ``storage_state.json`` write is delegated to the canonical
    # storage writer (which owns the atomic write, the unified storage lock, and
    # the parent-dir/file permission contract). This function stays here as the
    # ``notebooklm.auth``-exported facade symbol; it keeps its raise-on-lock-
    # failure semantics (the writer raises ``LockUnavailableError`` — the
    # documented replacement for the former ``filelock.Timeout``).
    from . import storage_writer  # local import: avoid the account<->writer cycle

    storage_writer.update_account_metadata(storage_path, authuser=authuser, email=email)

    # Best-effort: drop the legacy account key from sibling context.json so
    # the next reader doesn't see the same data in two places.
    _drop_legacy_account_key(storage_path)


def _load_storage_state_for_write(storage_path: Path) -> dict[str, Any]:
    """Read ``storage_state.json`` for a read-modify-write under the lock.

    Returns a synthetic empty document if the file is missing — matches
    the earlier behavior where account writes never failed just because the
    cookie file hadn't been written yet. Corruption is fatal because the
    primary cookie data can't be recovered from account metadata; surface
    a ``RuntimeError`` so the caller can prompt the user to re-run login.
    """
    if not storage_path.exists():
        return {"cookies": [], "origins": []}
    try:
        loaded = json.loads(storage_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise RuntimeError(f"storage state at {storage_path} is corrupted: {e}") from e
    if not isinstance(loaded, dict):
        raise RuntimeError(
            f"storage state at {storage_path} has unexpected shape: {type(loaded).__name__}"
        )
    return loaded


def clear_account_metadata(storage_path: Path | None) -> None:
    """Remove account metadata from both in-band and legacy locations.

    Holds a sibling ``.lock`` file via :class:`filelock.FileLock` so
    concurrent ``write_account_metadata`` calls serialize against the
    migration cleanup.
    """
    if storage_path is None:
        return
    # 1. Strip the in-band record from ``storage_state.json``.
    _clear_in_band_account(storage_path)
    # 2. Strip the legacy sibling record too (back-compat with old installs).
    _drop_legacy_account_key(storage_path)


def _clear_in_band_account(storage_path: Path) -> None:
    """Remove the ``notebooklm.account`` key from ``storage_state.json``.

    Delegates the in-band ``storage_state.json`` mutation to the canonical
    storage writer (best-effort: it swallows lock unavailability and read/parse
    errors, matching the pre-refactor semantics). No-op if the file is missing,
    unreadable, or doesn't carry an in-band record.
    """
    from . import storage_writer  # local import: avoid the account<->writer cycle

    storage_writer.clear_in_band_account(storage_path)
