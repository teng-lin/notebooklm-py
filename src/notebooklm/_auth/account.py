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

    Args:
        storage_path: Path to ``storage_state.json``. ``None`` means the
            profile is loaded from ``NOTEBOOKLM_AUTH_JSON`` (no sibling to
            promote from — env-auth profiles skip promotion entirely).

    Returns:
        Parsed metadata dict, or ``{}`` if no record is present.
    """
    if storage_path is None:
        return {}
    in_band = _read_in_band_account(storage_path)
    if in_band:
        return in_band
    if promote_legacy_account(storage_path):
        return _read_in_band_account(storage_path)
    return {}


def promote_legacy_account(storage_path: Path) -> bool:
    """One-shot migration: move a legacy sibling ``account`` record in-band.

    The pre-v0.5.0 two-file layout stored account metadata (``authuser`` /
    ``email``) in the sibling ``context.json``. This helper promotes that
    record into ``storage_state.json`` via the canonical storage writer and
    strips the legacy key, so :func:`read_account_metadata` (in-band-only)
    never loses a user's account binding.

    Ordering is crash-safe: the in-band embed commits first (atomic, under
    the canonical storage lock), the legacy strip second (atomic, under the
    context lock). A crash in between leaves both records present; the next
    call takes the strip-only branch. When the in-band record already
    exists, any stale legacy key is stripped as residue cleanup (privacy:
    the old account email must not live on at rest after a re-login).

    Best-effort by design — called from :func:`read_account_metadata` (the
    account chokepoint) on every read where in-band is absent, so a promotion
    failure must degrade to the pre-promotion state, never break the read.
    Returns ``True`` only when a legacy record was embedded.

    Args:
        storage_path: Path to ``storage_state.json`` (must be a real file
            path — env-auth profiles have no sibling and are skipped by the
            caller).
    """
    legacy = _read_legacy_account(storage_path)
    if not legacy:
        return False
    promoted = False
    if not _read_in_band_account(storage_path):
        raw_authuser = legacy.get("authuser")
        authuser = raw_authuser if type(raw_authuser) is int and raw_authuser >= 0 else 0
        raw_email = legacy.get("email")
        email = raw_email.strip() if isinstance(raw_email, str) and raw_email.strip() else None
        try:
            from . import storage_writer  # local import: avoid the account<->writer cycle

            storage_writer.update_account_metadata(storage_path, authuser=authuser, email=email)
            promoted = True
        except Exception as e:  # noqa: BLE001 — load path must not fail on promotion
            logger.debug("legacy account promotion failed at %s: %s", storage_path, e)
            return False
    _drop_legacy_account_key(storage_path)
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
