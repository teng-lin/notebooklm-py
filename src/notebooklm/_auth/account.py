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

from .._atomic_io import atomic_update_json, atomic_write_json
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
        html: Page HTML from ``notebooklm.google.com/?authuser=N``.

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
# against ``notebooklm.google.com/?authuser=N``.
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

    Probes ``https://notebooklm.google.com/?authuser=N`` for ``N`` in
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
    async with httpx.AsyncClient(
        cookies=cookie_jar,
        follow_redirects=True,
        timeout=httpx.Timeout(10.0, read=60.0),
    ) as client:
        # The browser's on-disk cookie DB rotates ``__Secure-1PSIDTS`` every
        # few minutes, but only when Chrome itself is actively running. A
        # ``--browser-cookies`` extraction against an idle Chrome lands here
        # with a stale SIDTS — the SID is fine, but ``notebooklm.google.com``
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


def _account_context_path(storage_path: Path) -> Path:
    """Return the context.json path that annotates ``storage_path``."""
    return storage_path.with_name("context.json")


def read_account_metadata(storage_path: Path | None) -> dict[str, Any]:
    """Read profile account metadata from ``context.json``.

    The ``account`` object records the Google ``authuser`` index used when
    the profile was authenticated. Profiles from before this feature shipped
    (and profiles for users with a single Google account) have no account
    metadata and use ``authuser=0``.

    Args:
        storage_path: Path to ``storage_state.json``. The sibling
            ``context.json`` stores account metadata. ``None`` means the
            profile is loaded from ``NOTEBOOKLM_AUTH_JSON``.

    Returns:
        Parsed metadata dict, or ``{}`` if the file is missing, unreadable,
        or malformed. Callers should treat a missing ``authuser`` key as 0.
    """
    if storage_path is None:
        return {}
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


def write_account_metadata(storage_path: Path, *, authuser: int, email: str | None = None) -> None:
    """Persist profile account metadata inside sibling ``context.json``.

    Uses :func:`atomic_update_json` so concurrent CLI invocations (e.g., a
    ``login`` while ``use`` is in flight) cannot lose updates by writing
    stale snapshots of ``context.json`` over each other.

    Args:
        storage_path: Path to ``storage_state.json``. The sibling
            ``context.json`` is created or updated.
        authuser: ``authuser`` index used when extracting cookies for this
            profile (0 for the default account).
        email: Optional account email to record alongside the index.
    """
    context_path = _account_context_path(storage_path)
    payload: dict[str, Any] = {"authuser": authuser}
    if email:
        payload["email"] = email

    def _set_account(data: dict[str, Any]) -> dict[str, Any]:
        data[_ACCOUNT_CONTEXT_KEY] = payload
        return data

    # ``recover_from_corrupt=True`` keeps the empty-dict fallback **inside**
    # the file lock. An outside-the-lock unlink-and-retry would race a
    # concurrent process that wrote a valid payload between our raise and
    # our retry, causing us to delete their good write (see PR #465 review).
    # Account metadata is unrecoverable from corrupt JSON, so silent reset
    # under the lock is the right behaviour.
    atomic_update_json(context_path, _set_account, recover_from_corrupt=True)


def clear_account_metadata(storage_path: Path | None) -> None:
    """Remove account metadata from sibling ``context.json`` if present.

    Holds the same sibling ``.lock`` file used by :func:`atomic_update_json`
    so concurrent ``write_account_metadata`` / context-mutation calls don't
    lose updates against our clear-and-maybe-delete.
    """
    if storage_path is None:
        return
    context_path = _account_context_path(storage_path)
    if not context_path.exists():
        return
    lock_path = context_path.with_suffix(context_path.suffix + ".lock")
    context_path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(lock_path), timeout=10.0):
        # Re-check existence under the lock — another writer may have
        # removed it between the early-return check and the lock acquire.
        if not context_path.exists():
            return
        try:
            data = json.loads(context_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.debug("account metadata clear skipped for %s: %s", context_path, e)
            return
        if not isinstance(data, dict) or _ACCOUNT_CONTEXT_KEY not in data:
            return
        del data[_ACCOUNT_CONTEXT_KEY]
        if data:
            # atomic_update_json would re-acquire the lock; use the atomic
            # write directly since we already hold the lock here.
            atomic_write_json(context_path, data)
        else:
            context_path.unlink()
