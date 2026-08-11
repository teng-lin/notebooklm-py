"""Google account discovery and compatibility repair adapters."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from urllib.parse import urlencode

import httpx

from .._env import get_base_url
from .._url_utils import is_google_auth_redirect
from .account_repair import _compose_account_repair_service
from .account_types import Account, PlaywrightAccountRepairResult

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


def _select_playwright_account(
    accounts: list[Account],
    *,
    active_email: str | None,
) -> tuple[Account | None, str | None]:
    """Select the account Playwright just logged into, or an ambiguity reason."""
    if active_email:
        normalized = active_email.casefold()
        matches = [
            account
            for account in accounts
            if isinstance(account.email, str) and account.email.casefold() == normalized
        ]
        if len(matches) == 1:
            return matches[0], None
        if matches:
            return None, f"multiple discovered accounts matched {active_email}"
        return None, f"current NotebookLM page email {active_email} was not discovered"

    if len(accounts) == 1:
        return accounts[0], None
    if accounts:
        return (
            None,
            "multiple Google accounts were discovered but the active page email was unavailable",
        )
    return None, "no Google accounts were discovered"


async def _enumerate_accounts_for_repair(
    cookie_jar: httpx.Cookies,
    poke_session: Callable[[httpx.AsyncClient, Path | None], Awaitable[None]],
) -> list[Account]:
    """Normalize the keyword-only network seam for account repair."""
    return await enumerate_accounts(cookie_jar, poke_session=poke_session)


def _select_account_for_repair(
    accounts: list[Account],
    active_email: str | None,
) -> tuple[Account | None, str | None]:
    """Normalize the keyword-only selection seam for account repair."""
    return _select_playwright_account(accounts, active_email=active_email)


def _extract_active_email_for_repair(html: str) -> str | None:
    """Keep active-email extraction late-bound in this network module."""
    return extract_email_from_html(html)


async def repair_account_metadata_from_playwright_storage(
    storage_path: Path,
    *,
    page_html: str | None = None,
) -> PlaywrightAccountRepairResult:
    """Populate ``notebooklm.account`` from Playwright storage when unambiguous."""
    service = _compose_account_repair_service(
        enumerate_accounts=_enumerate_accounts_for_repair,
        select_account=_select_account_for_repair,
        extract_active_email=_extract_active_email_for_repair,
    )
    return await service.repair(storage_path, page_html=page_html)
