"""Extraction utilities for cookies, tokens, and account info."""

import logging
import re
from typing import Any
from dataclasses import dataclass
import httpx

from ..exceptions import AuthExtractionError
from .._url_utils import is_google_auth_redirect, contains_google_auth_redirect
from .._env import get_base_url
from . import (
    MINIMUM_REQUIRED_COOKIES,
    _validate_required_cookies,
    _auth_domain_priority,
    _is_allowed_auth_domain,
)
logger = logging.getLogger(__name__)

def convert_rookiepy_cookies_to_storage_state(
    rookiepy_cookies: list[dict],
) -> dict[str, Any]:
    """Convert rookiepy cookie dicts to Playwright storage_state.json format.

    Key mappings:
    - ``http_only`` → ``httpOnly`` (snake_case to camelCase)
    - ``expires=None`` → ``expires=-1`` (Playwright convention for session cookies)
    - ``sameSite`` always ``"None"`` for cross-site Google cookies

    Args:
        rookiepy_cookies: List of cookie dicts from any ``rookiepy.*()`` call.
            Required keys: ``domain``, ``name``, ``value``.

    Returns:
        Dict matching storage_state.json schema: ``{"cookies": [...], "origins": []}``.
        Cookies missing required fields or from non-Google domains are silently skipped.
    """
    converted = []
    for cookie in rookiepy_cookies:
        domain = cookie.get("domain", "")
        name = cookie.get("name", "")
        value = cookie.get("value", "")

        # Validate required fields
        if not name or not value or not domain:
            continue

        if not _is_allowed_auth_domain(domain):
            continue

        path = cookie.get("path", "/")
        http_only = cookie.get("http_only", False)
        secure = cookie.get("secure", False)
        expires = cookie.get("expires")

        converted.append(
            {
                "name": name,
                "value": value,
                "domain": domain,
                "path": path,
                "expires": expires if expires is not None else -1,
                "httpOnly": http_only,
                "secure": secure,
                "sameSite": "None",
            }
        )
    return {"cookies": converted, "origins": []}


def extract_cookies_from_storage(storage_state: dict[str, Any]) -> dict[str, str]:
    """Extract Google cookies from Playwright storage state.

    Filters cookies to allowed Google domains, resolving duplicates
    based on domain priority (base .google.com > notebooklm > regional).

    Args:
        storage_state: Parsed JSON from Playwright's storage state file.

    Returns:
        Dict mapping cookie names to values.

    Raises:
        ValueError: If required cookies (SID) are missing from storage state.

    Example:
        >>> storage = {"cookies": [
        ...     {"name": "SID", "value": "regional", "domain": ".google.com.sg"},
        ...     {"name": "SID", "value": "base", "domain": ".google.com"},
        ... ]}
        >>> cookies = extract_cookies_from_storage(storage)
        >>> cookies["SID"]
        'base'  # .google.com wins regardless of list order
    """
    cookies = {}
    cookie_domains: dict[str, str] = {}  # Track which domain each cookie came from
    cookie_priorities: dict[str, int] = {}

    for cookie in storage_state.get("cookies", []):
        domain = cookie.get("domain", "")
        name = cookie.get("name")
        if not _is_allowed_auth_domain(domain) or not name:
            continue

        # Resolve duplicates using domain priority.
        priority = _auth_domain_priority(domain)
        if name not in cookies or priority > cookie_priorities[name]:
            if name in cookies:
                logger.debug(
                    "Cookie %s: using %s value (overriding %s)",
                    name,
                    domain,
                    cookie_domains[name],
                )
            cookies[name] = cookie.get("value", "")
            cookie_domains[name] = domain
            cookie_priorities[name] = priority
        else:
            logger.debug(
                "Cookie %s: ignoring duplicate from %s (keeping %s)",
                name,
                domain,
                cookie_domains[name],
            )

    # Log extraction summary for debugging
    if cookie_domains:
        unique_domains = sorted(set(cookie_domains.values()))
        logger.debug(
            "Extracted %d cookies from domains: %s", len(cookies), ", ".join(unique_domains)
        )
        if "SID" in cookie_domains:
            logger.debug("SID cookie from domain: %s", cookie_domains["SID"])

    # Build diagnostics lazily for failure paths only.
    cookie_names = set(cookies.keys())
    extras: list[str] = []
    if not MINIMUM_REQUIRED_COOKIES.issubset(cookie_names):
        all_domains = {c.get("domain", "") for c in storage_state.get("cookies", [])}
        google_domains = sorted(d for d in all_domains if "google" in d.lower())
        found_names = list(cookies.keys())[:5]
        if found_names:
            extras.append(f"Found cookies: {found_names}{'...' if len(cookies) > 5 else ''}")
        if google_domains:
            extras.append(f"Google domains in storage: {google_domains}")
    _validate_required_cookies(cookie_names, extra_diagnostics=extras)

    return cookies


def _build_wiz_field_patterns(key: str) -> list[re.Pattern[str]]:
    """Build regex patterns to locate a Wiz field.
    
    Tries canonical double-quoted, single-quoted, and HTML-escaped formats
    in priority order, handling JSON escapes correctly.
    """
    escaped = re.escape(key)
    return [
        # 1. Canonical double-quoted: "key":"value"  (or  "key" : "value")
        #    Captures escaped quotes: "key":"a\"b" -> a\"b
        re.compile(rf'"{escaped}"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"'),
        # 2. Single-quoted variant: 'key':'value' with escaped-quote support.
        re.compile(rf"'{escaped}'\s*:\s*'([^'\\]*(?:\\.[^'\\]*)*)'"),
        # 3. HTML-escaped: &quot;key&quot;:&quot;value&quot;
        #    Tempered dot so the value can contain other entities like &amp;.
        re.compile(rf"&quot;{escaped}&quot;\s*:\s*&quot;((?:(?!&quot;).)*)&quot;"),
    ]


def extract_wiz_field(html: str, key: str, *, strict: bool = True) -> str | None:
    """Extract a value from the embedded WIZ_global_data object in HTML.
    
    Tolerates various quoting formats (double, single, HTML-escaped).
    Returns the value, or None if strict=False and no match is found.

    Args:
        html: The page HTML to search.
        key: Field name to extract from ``WIZ_global_data``.
        strict: When True (default) and no pattern matches, raise
            :class:`AuthExtractionError` with a sanitized preview. When False,
            return ``None`` on drift so callers can fall back gracefully.

    Returns:
        The extracted value (possibly empty), or ``None`` when ``strict=False``
        and no pattern matched.

    Raises:
        AuthExtractionError: ``strict=True`` and the key was not found.
    """
    for pattern in _build_wiz_field_patterns(key):
        match = pattern.search(html)
        if match is not None:
            return match.group(1)
    if strict:
        raise AuthExtractionError(key, html)
    return None


def extract_csrf_from_html(html: str, final_url: str = "") -> str:
    """
    Extract CSRF token (SNlM0e) from NotebookLM page HTML.

    The CSRF token is embedded in the page's WIZ_global_data JavaScript object.
    It's required for all RPC calls to prevent cross-site request forgery.

    Args:
        html: Page HTML content from notebooklm.google.com
        final_url: The final URL after redirects (for error messages)

    Returns:
        CSRF token value (typically starts with "AF1_QpN-")

    Raises:
        ValueError: Raised if the token is missing or if the page is a login redirect.
    """
    # Try extracting with multiple quoting variants
    token = extract_wiz_field(html, "SNlM0e", strict=False)
    if token is not None:
        return token
    # Distinguish between auth expiration and page structure changes
    if is_google_auth_redirect(final_url) or contains_google_auth_redirect(html):
        raise ValueError(
            "Authentication expired or invalid. Run 'notebooklm login' to re-authenticate."
        )
    raise ValueError(
        f"CSRF token not found in HTML. Final URL: {final_url}\n"
        "This may indicate the page structure has changed."
    )


def extract_session_id_from_html(html: str, final_url: str = "") -> str:
    """
    Extract session ID (FdrFJe) from NotebookLM page HTML.

    The session ID is embedded in the page's WIZ_global_data JavaScript object.
    It's passed in URL query parameters for RPC calls.

    Args:
        html: Page HTML content from notebooklm.google.com
        final_url: The final URL after redirects (for error messages)

    Returns:
        Session ID value

    Raises:
        ValueError: Raised if the session ID is missing or if the page is a login redirect.
    """
    sid = extract_wiz_field(html, "FdrFJe", strict=False)
    if sid is not None:
        return sid
    if is_google_auth_redirect(final_url) or contains_google_auth_redirect(html):
        raise ValueError(
            "Authentication expired or invalid. Run 'notebooklm login' to re-authenticate."
        )
    raise ValueError(
        f"Session ID not found in HTML. Final URL: {final_url}\n"
        "This may indicate the page structure has changed."
    )


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


# Max authuser indices to probe (Google supports ~10 concurrent accounts).
MAX_AUTHUSER_PROBE = 10

# System email locals to ignore (must match with _NON_USER_EMAIL_DOMAINS).
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

# Matches email inside WIZ_global_data JSON.
_EMAIL_RE = re.compile(r'"([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})"')


def extract_email_from_html(html: str) -> str | None:
    """Extract the active user's email from a NotebookLM page response.

    Returns the first plausible Google account email found in the HTML,
    skipping system contact endpoints.

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


# Requires a real-browser UA to ensure Google serves the WIZ_global_data block.
_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
)


async def _probe_authuser(client: httpx.AsyncClient, n: int) -> str | None:
    """Probe one ``authuser`` index to find the active email.
    
    Returns ``None`` if redirected or parsing fails.
    """
    from . import authuser_query
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
    cookie_jar: httpx.Cookies, *, max_authuser: int = MAX_AUTHUSER_PROBE
) -> list[Account]:
    """Enumerate Google accounts by probing ?authuser=N.
    
    Stops probing when a duplicate email is found, indicating a fallback
    to the default account.

    Args:
        cookie_jar: ``httpx.Cookies`` jar with auth cookies. Not mutated.
        max_authuser: Hard cap on indices probed (default
            :data:`MAX_AUTHUSER_PROBE`).

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
        # Poke once to refresh potentially stale __Secure-1PSIDTS cookies
        # to prevent immediate auth redirects.
        from . import _poke_session
        await _poke_session(client, None)
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

