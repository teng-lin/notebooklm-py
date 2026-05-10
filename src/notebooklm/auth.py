"""Authentication handling for NotebookLM API.

This module provides authentication utilities for the NotebookLM client:

1. **Cookie-based Authentication**: Loads Google cookies from Playwright storage
   state files created by `notebooklm login`.

2. **Token Extraction**: Fetches CSRF (SNlM0e) and session (FdrFJe) tokens from
   the NotebookLM homepage, required for all RPC calls.

3. **Download Cookies**: Provides httpx-compatible cookies with domain info for
   authenticated downloads from Google content servers.

Usage:
    # Recommended: Use AuthTokens.from_storage() for full initialization
    auth = await AuthTokens.from_storage()
    async with NotebookLMClient(auth) as client:
        ...

    # For authenticated downloads
    cookies = load_httpx_cookies()
    async with httpx.AsyncClient(cookies=cookies) as client:
        response = await client.get(url)

Security Notes:
    - Storage state files contain sensitive session cookies
    - Path traversal protection is enforced on all file operations
"""

import asyncio
import contextlib
import errno
import http.cookiejar
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import weakref
from collections.abc import Iterator
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeAlias

import httpx

from ._url_utils import contains_google_auth_redirect, is_google_auth_redirect
from .paths import get_storage_path, resolve_profile

logger = logging.getLogger(__name__)

CookieKey: TypeAlias = tuple[str, str]
DomainCookieMap: TypeAlias = dict[CookieKey, str]
FlatCookieMap: TypeAlias = dict[str, str]
CookieInput: TypeAlias = DomainCookieMap | FlatCookieMap

# Minimum required cookies (must have at least SID for basic auth)
MINIMUM_REQUIRED_COOKIES = {"SID"}

# Cookie domains to extract from storage state.
#
# Includes:
#   - notebooklm.google.com (the API host)
#   - .google.com / accounts.google.com (auth + token refresh)
#   - .googleusercontent.com (authenticated media downloads)
#   - sibling Google products (YouTube, Drive, Docs, myaccount, mail) so future
#     auth/rotation flows that traverse those domains have cookies available.
#     See issue #360 for the rationale; these are not load-bearing in any
#     current code path but make the allowlist symmetric with what a logged-in
#     browser session actually carries.
#
# This set is also fed verbatim to ``rookiepy.load(domains=...)`` by
# ``_login_with_browser_cookies``, so adding a domain here automatically
# extends what we ask the browser for at login time.
ALLOWED_COOKIE_DOMAINS = {
    ".google.com",
    "google.com",  # Host-only Domain=google.com cookies (rare but possible)
    # Playwright storage_state may preserve the leading dot for NotebookLM cookies.
    ".notebooklm.google.com",
    "notebooklm.google.com",
    ".googleusercontent.com",
    "accounts.google.com",  # Required for token refresh redirects
    ".accounts.google.com",  # http.cookiejar may normalize Domain=accounts.google.com
    # Sibling Google products — auth/rotation flows may traverse these.
    # Both dotted and non-dotted variants are listed so that http.cookiejar
    # normalization (which can add a leading dot) doesn't drop a cookie at the
    # next extraction; same defensive pattern as accounts.google.com above.
    ".youtube.com",
    "youtube.com",
    "accounts.youtube.com",
    ".accounts.youtube.com",
    "drive.google.com",
    ".drive.google.com",
    "docs.google.com",
    ".docs.google.com",
    "myaccount.google.com",
    ".myaccount.google.com",
    # Optional — not load-bearing in any current flow, but kept for symmetry
    # with what a logged-in browser session actually holds.
    "mail.google.com",
    ".mail.google.com",
}

# Regional Google ccTLDs where Google may set auth cookies
# Users in these regions may have SID cookies on regional domains instead of .google.com
# Format: suffix after ".google." (e.g., "com.sg" for ".google.com.sg")
#
# Categories:
# - com.XX: Country-code second-level domains (Singapore, Australia, Brazil, etc.)
# - co.XX: Country domains using .co (UK, Japan, India, Korea, etc.)
# - XX: Single ccTLD countries (Germany, France, Italy, etc.)
GOOGLE_REGIONAL_CCTLDS = frozenset(
    {
        # .google.com.XX pattern (country-code second-level domains)
        "com.sg",  # Singapore
        "com.au",  # Australia
        "com.br",  # Brazil
        "com.mx",  # Mexico
        "com.ar",  # Argentina
        "com.hk",  # Hong Kong
        "com.tw",  # Taiwan
        "com.my",  # Malaysia
        "com.ph",  # Philippines
        "com.vn",  # Vietnam
        "com.pk",  # Pakistan
        "com.bd",  # Bangladesh
        "com.ng",  # Nigeria
        "com.eg",  # Egypt
        "com.tr",  # Turkey
        "com.ua",  # Ukraine
        "com.co",  # Colombia
        "com.pe",  # Peru
        "com.sa",  # Saudi Arabia
        "com.ae",  # UAE
        # .google.co.XX pattern (countries using .co second-level)
        "co.uk",  # United Kingdom
        "co.jp",  # Japan
        "co.in",  # India
        "co.kr",  # South Korea
        "co.za",  # South Africa
        "co.nz",  # New Zealand
        "co.id",  # Indonesia
        "co.th",  # Thailand
        "co.il",  # Israel
        "co.ve",  # Venezuela
        "co.cr",  # Costa Rica
        "co.ke",  # Kenya
        "co.ug",  # Uganda
        "co.tz",  # Tanzania
        "co.ma",  # Morocco
        "co.ao",  # Angola
        "co.mz",  # Mozambique
        "co.zw",  # Zimbabwe
        "co.bw",  # Botswana
        # .google.XX pattern (single ccTLD countries)
        "cn",  # China
        "de",  # Germany
        "fr",  # France
        "it",  # Italy
        "es",  # Spain
        "nl",  # Netherlands
        "pl",  # Poland
        "ru",  # Russia
        "ca",  # Canada
        "be",  # Belgium
        "at",  # Austria
        "ch",  # Switzerland
        "se",  # Sweden
        "no",  # Norway
        "dk",  # Denmark
        "fi",  # Finland
        "pt",  # Portugal
        "gr",  # Greece
        "cz",  # Czech Republic
        "ro",  # Romania
        "hu",  # Hungary
        "ie",  # Ireland
        "sk",  # Slovakia
        "bg",  # Bulgaria
        "hr",  # Croatia
        "si",  # Slovenia
        "lt",  # Lithuania
        "lv",  # Latvia
        "ee",  # Estonia
        "lu",  # Luxembourg
        "cl",  # Chile
        "cat",  # Catalonia (special case - 3 letter)
    }
)


@dataclass
class AuthTokens:
    """Authentication tokens for NotebookLM API.

    Attributes:
        cookies: Dict of required Google auth cookies keyed by (name, domain)
        csrf_token: CSRF token (SNlM0e) extracted from page
        session_id: Session ID (FdrFJe) extracted from page
        storage_path: Path to the storage_state.json file, if file-based auth was used
        cookie_jar: Domain-preserving httpx.Cookies jar. Preferred over flat cookies dict
            for HTTP operations as it retains original cookie domains (e.g.,
            .googleusercontent.com vs .google.com).
    """

    cookies: DomainCookieMap
    csrf_token: str
    session_id: str
    storage_path: Path | None = None
    cookie_jar: httpx.Cookies | None = None

    def __post_init__(self) -> None:
        """Normalize legacy flat cookie mappings into domain-keyed mappings."""
        self.cookies = normalize_cookie_map(self.cookies)
        if self.cookie_jar is None:
            self.cookie_jar = build_cookie_jar(cookies=self.cookies, storage_path=self.storage_path)

    @property
    def cookie_header(self) -> str:
        """Generate Cookie header value for HTTP requests.

        Returns:
            Semicolon-separated cookie string (e.g., "SID=abc; HSID=def")
        """
        return "; ".join(f"{k}={v}" for k, v in self.flat_cookies.items())

    @property
    def flat_cookies(self) -> FlatCookieMap:
        """Return a legacy name→value cookie mapping.

        When the same cookie name exists on multiple domains, the base
        ``.google.com`` value wins for compatibility with the previous flat
        representation. Domain-aware HTTP operations should use ``cookie_jar``
        or ``cookies`` directly instead.
        """
        return flatten_cookie_map(self.cookies)

    @classmethod
    async def from_storage(
        cls, path: Path | None = None, profile: str | None = None
    ) -> "AuthTokens":
        """Create AuthTokens from Playwright storage state file.

        This is the recommended way to create AuthTokens for programmatic use.
        It loads cookies from storage and fetches CSRF/session tokens automatically.

        Args:
            path: Path to storage_state.json. If provided, takes precedence over profile.
            profile: Profile name to load auth from (e.g., "work", "personal").
                If None, uses the active profile (from CLI flag, env var, or config).

        Returns:
            Fully initialized AuthTokens ready for API calls.

        Raises:
            FileNotFoundError: If storage file doesn't exist
            ValueError: If required cookies are missing or tokens can't be extracted
            httpx.HTTPError: If token fetch request fails

        Example:
            auth = await AuthTokens.from_storage()
            async with NotebookLMClient(auth) as client:
                notebooks = await client.list_notebooks()

            # Load from a specific profile
            auth = await AuthTokens.from_storage(profile="work")
        """
        if path is None and (profile is not None or "NOTEBOOKLM_AUTH_JSON" not in os.environ):
            path = get_storage_path(profile=profile)

        # Build the cookie jar via the lossless loader so path/secure/httpOnly
        # survive into the live jar. The earlier
        # extract_cookies_with_domains -> build_cookie_jar pipeline only carried
        # (name, domain) -> value and dropped the same attributes the load
        # paths in #365 fixed.
        jar = build_httpx_cookies_from_storage(path)
        csrf_token, session_id, _ = await _fetch_tokens_with_refresh(jar, path, profile)

        # Persist any refreshed cookies from the token fetch
        save_cookies_to_storage(jar, path)
        cookies = _cookie_map_from_jar(jar)

        return cls(
            cookies=cookies,
            csrf_token=csrf_token,
            session_id=session_id,
            storage_path=path,
            cookie_jar=jar,
        )


def normalize_cookie_map(cookies: CookieInput | None) -> DomainCookieMap:
    """Normalize flat or domain-aware cookie maps into (name, domain) keys."""
    normalized: DomainCookieMap = {}
    if not cookies:
        return normalized

    for key, value in cookies.items():
        if isinstance(key, tuple):
            name, domain = key
        else:
            name, domain = key, ".google.com"
        if name:
            normalized[(name, domain or ".google.com")] = value
    return normalized


def flatten_cookie_map(cookies: CookieInput | None) -> FlatCookieMap:
    """Flatten domain-aware cookies for legacy raw Cookie header callers."""
    flat: FlatCookieMap = {}

    for (name, domain), value in normalize_cookie_map(cookies).items():
        is_base_domain = domain == ".google.com"
        if name not in flat or is_base_domain:
            flat[name] = value

    return flat


def _is_google_domain(domain: str) -> bool:
    """Check if a cookie domain is a valid Google domain.

    Uses a whitelist approach to validate Google domains including:
    - Base domain: .google.com
    - Regional .google.com.XX: .google.com.sg, .google.com.au, etc.
    - Regional .google.co.XX: .google.co.uk, .google.co.jp, etc.
    - Regional .google.XX: .google.de, .google.fr, etc.

    This function is used by both auth cookie extraction and download cookie
    validation to ensure consistent domain handling across the codebase.

    Args:
        domain: Cookie domain to check (e.g., '.google.com', '.google.com.sg')

    Returns:
        True if domain is a valid Google domain.

    Note:
        Uses an explicit whitelist (GOOGLE_REGIONAL_CCTLDS) rather than regex
        to prevent false positives from invalid or malicious domains.
    """
    # Base Google domain
    if domain == ".google.com":
        return True

    # Check regional Google domains using whitelist
    if domain.startswith(".google."):
        suffix = domain[8:]  # Remove ".google." prefix
        return suffix in GOOGLE_REGIONAL_CCTLDS

    return False


def _is_allowed_auth_domain(domain: str) -> bool:
    """Check if a cookie domain is allowed for auth cookie extraction.

    Thin alias of :func:`_is_allowed_cookie_domain`. Both auth-jar building
    and download-cookie loading (and the persistence path that filters which
    cookies get saved back) share a single allowlist policy:

    1. Exact match against :data:`ALLOWED_COOKIE_DOMAINS` (covers the API host,
       sibling Google products like YouTube/Drive/Docs/myaccount, and the
       leading-dot variants ``http.cookiejar`` may normalize to).
    2. Regional Google ccTLDs (``.google.com.sg``, ``.google.co.uk``,
       ``.google.de``, …) where SID cookies may be set for users in those
       regions.
    3. Suffix matches for Google subdomains (``lh3.google.com``,
       ``accounts.google.com``) and ``.googleusercontent.com`` /
       ``.usercontent.google.com`` for authenticated media downloads.

    The previous strict / broad split (#334 / fea8315) created an asymmetry
    where ``save_cookies_to_storage`` would persist cookies that the next
    extraction would silently drop. Issue #360 collapsed both filters into
    this single policy.

    Args:
        domain: Cookie domain to check (e.g., '.google.com', '.google.com.sg')

    Returns:
        True if domain is allowed for auth/download cookies.
    """
    return _is_allowed_cookie_domain(domain)


def _auth_domain_priority(domain: str) -> int:
    """Return duplicate-cookie priority for allowed auth domains.

    Higher value wins. Tiers are distinct so the resolved cookie is fully
    deterministic regardless of storage_state ordering.
    """
    if domain == ".google.com":
        return 4
    if domain == ".notebooklm.google.com":
        return 3
    if domain == "notebooklm.google.com":
        return 2
    if _is_google_domain(domain):
        return 1
    # Allowlisted but unranked domains (e.g. .googleusercontent.com) fall through.
    return 0


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
    """Extract Google cookies from Playwright storage state for NotebookLM auth.

    Filters cookies to include those from .google.com, notebooklm.google.com,
    .googleusercontent.com domains, and regional Google domains
    (e.g., .google.com.sg, .google.com.au). The regional domains are needed
    because Google sets SID cookies on country-specific domains for users
    in those regions.

    Cookie Priority Rules:
        When the same cookie name exists on multiple domains (e.g., SID on both
        .google.com and .google.com.sg), we use this priority order:

        1. .google.com (base domain) - ALWAYS preferred when present
        2. .notebooklm.google.com (Playwright canonical NotebookLM subdomain)
        3. notebooklm.google.com (no-dot NotebookLM subdomain)
        4. Regional domains (e.g. .google.de, .google.com.sg, .google.co.uk)
        5. Other allowlisted domains (e.g. .googleusercontent.com)

        Within a single priority tier, the first occurrence in the list wins;
        later duplicates at the same tier are ignored. Tiers are distinct so the
        outcome is deterministic regardless of storage_state ordering. See PR #34
        for the bug this fixes.

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

        # Prioritize stable domain classes over storage_state ordering to prevent
        # wrong cookie values when the same name exists in multiple domains.
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

    missing = MINIMUM_REQUIRED_COOKIES - set(cookies.keys())
    if missing:
        # Provide more helpful error message with diagnostic info
        all_domains = {c.get("domain", "") for c in storage_state.get("cookies", [])}
        google_domains = sorted(d for d in all_domains if "google" in d.lower())
        found_names = list(cookies.keys())[:5]

        error_parts = [f"Missing required cookies: {missing}"]
        if found_names:
            error_parts.append(f"Found cookies: {found_names}{'...' if len(cookies) > 5 else ''}")
        if google_domains:
            error_parts.append(f"Google domains in storage: {google_domains}")
        error_parts.append("Run 'notebooklm login' to authenticate.")
        raise ValueError("\n".join(error_parts))

    return cookies


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
        ValueError: If token pattern not found in HTML
    """
    # Match "SNlM0e": "<token>" or "SNlM0e":"<token>" pattern
    match = re.search(r'"SNlM0e"\s*:\s*"([^"]+)"', html)
    if not match:
        # Check if we were redirected to login page
        if is_google_auth_redirect(final_url) or contains_google_auth_redirect(html):
            raise ValueError(
                "Authentication expired or invalid. Run 'notebooklm login' to re-authenticate."
            )
        raise ValueError(
            f"CSRF token not found in HTML. Final URL: {final_url}\n"
            "This may indicate the page structure has changed."
        )
    return match.group(1)


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
        ValueError: If session ID pattern not found in HTML
    """
    # Match "FdrFJe": "<session_id>" or "FdrFJe":"<session_id>" pattern
    match = re.search(r'"FdrFJe"\s*:\s*"([^"]+)"', html)
    if not match:
        if is_google_auth_redirect(final_url) or contains_google_auth_redirect(html):
            raise ValueError(
                "Authentication expired or invalid. Run 'notebooklm login' to re-authenticate."
            )
        raise ValueError(
            f"Session ID not found in HTML. Final URL: {final_url}\n"
            "This may indicate the page structure has changed."
        )
    return match.group(1)


def _load_storage_state(path: Path | None = None) -> dict[str, Any]:
    """Load Playwright storage state from file or environment variable.

    This is a shared helper used by load_auth_from_storage() and load_httpx_cookies()
    to avoid code duplication.

    Precedence:
    1. Explicit path argument (from --storage CLI flag)
    2. NOTEBOOKLM_AUTH_JSON environment variable (inline JSON, no file needed)
    3. File at $NOTEBOOKLM_HOME/storage_state.json (or ~/.notebooklm/storage_state.json)

    Args:
        path: Path to storage_state.json. If provided, takes precedence over env vars.

    Returns:
        Parsed storage state dict.

    Raises:
        FileNotFoundError: If storage file doesn't exist (when using file-based auth).
        ValueError: If JSON is malformed or empty.
    """
    # 1. Explicit path takes precedence (from --storage CLI flag)
    if path:
        if not path.exists():
            raise FileNotFoundError(
                f"Storage file not found: {path}\nRun 'notebooklm login' to authenticate first."
            )
        return json.loads(path.read_text(encoding="utf-8"))

    # 2. Check for inline JSON env var (CI-friendly, no file writes needed)
    # Note: Use 'in' check instead of walrus to catch empty string case
    if "NOTEBOOKLM_AUTH_JSON" in os.environ:
        auth_json = os.environ["NOTEBOOKLM_AUTH_JSON"].strip()
        if not auth_json:
            raise ValueError(
                "NOTEBOOKLM_AUTH_JSON environment variable is set but empty.\n"
                "Provide valid Playwright storage state JSON or unset the variable."
            )
        try:
            storage_state = json.loads(auth_json)
        except json.JSONDecodeError as e:
            raise ValueError(
                f"Invalid JSON in NOTEBOOKLM_AUTH_JSON environment variable: {e}\n"
                f"Ensure the value is valid Playwright storage state JSON."
            ) from e
        # Validate structure
        if not isinstance(storage_state, dict) or "cookies" not in storage_state:
            raise ValueError(
                "NOTEBOOKLM_AUTH_JSON must contain valid Playwright storage state "
                "with a 'cookies' key.\n"
                'Expected format: {"cookies": [{"name": "SID", "value": "...", ...}]}'
            )
        return storage_state

    # 3. Fall back to file (respects NOTEBOOKLM_HOME)
    storage_path = get_storage_path()

    if not storage_path.exists():
        raise FileNotFoundError(
            f"Storage file not found: {storage_path}\nRun 'notebooklm login' to authenticate first."
        )

    return json.loads(storage_path.read_text(encoding="utf-8"))


def load_auth_from_storage(path: Path | None = None) -> dict[str, str]:
    """Load Google cookies from storage.

    Loads authentication cookies with the following precedence:
    1. Explicit path argument (from --storage CLI flag)
    2. NOTEBOOKLM_AUTH_JSON environment variable (inline JSON, no file needed)
    3. File at $NOTEBOOKLM_HOME/storage_state.json (or ~/.notebooklm/storage_state.json)

    Args:
        path: Path to storage_state.json. If provided, takes precedence over env vars.

    Returns:
        Dict mapping cookie names to values (e.g., {"SID": "...", "HSID": "..."}).

    Raises:
        FileNotFoundError: If storage file doesn't exist (when using file-based auth).
        ValueError: If required cookies (SID) are missing or JSON is malformed.

    Example:
        # CLI flag takes precedence
        cookies = load_auth_from_storage(Path("/custom/path.json"))

        # Or use NOTEBOOKLM_AUTH_JSON for CI/CD (no file writes needed)
        # export NOTEBOOKLM_AUTH_JSON='{"cookies":[...]}'
        cookies = load_auth_from_storage()
    """
    storage_state = _load_storage_state(path)
    return extract_cookies_from_storage(storage_state)


def _is_allowed_cookie_domain(domain: str) -> bool:
    """Canonical cookie-domain allowlist for both auth and downloads.

    This is the single source of truth for "is this cookie domain one we
    accept?". Both the auth-extraction path and the download path go through
    here — :func:`_is_allowed_auth_domain` is a thin alias preserved for
    call-site readability. See issue #360 for why the split was collapsed.

    A domain is allowed if any of the following holds:

    1. Exact match against :data:`ALLOWED_COOKIE_DOMAINS` (the API host,
       sibling Google products like ``.youtube.com`` / ``drive.google.com`` /
       ``docs.google.com`` / ``myaccount.google.com``, ``accounts.google.com``,
       and the leading-dot variants ``http.cookiejar`` may normalize to).
    2. Valid Google domain via :func:`_is_google_domain` (regional ccTLDs:
       ``.google.com.sg``, ``.google.co.uk``, ``.google.de``, …).
    3. Subdomain of ``.google.com``, ``.googleusercontent.com``, or
       ``.usercontent.google.com`` (e.g. ``lh3.google.com``,
       ``lh3.googleusercontent.com``).

    The leading-dot suffix check ensures lookalikes like ``evil-google.com``
    are rejected.

    Args:
        domain: Cookie domain to check (e.g., '.google.com', 'lh3.google.com')

    Returns:
        True if domain is allowed for auth/download cookies.
    """
    # Exact match against the primary allowlist
    if domain in ALLOWED_COOKIE_DOMAINS:
        return True

    # Check if it's a valid Google domain (base or regional)
    # This handles .google.com, .google.com.sg, .google.co.uk, .google.de, etc.
    if _is_google_domain(domain):
        return True

    # Suffixes for allowed download domains (leading dot provides boundary check)
    # - Subdomains of .google.com (e.g., lh3.google.com, accounts.google.com)
    # - googleusercontent.com domains for media downloads
    allowed_suffixes = (
        ".google.com",
        ".googleusercontent.com",
        ".usercontent.google.com",
    )

    # Check if domain is a subdomain of allowed suffixes
    # The leading dot ensures 'evil-google.com' does NOT match
    return any(domain.endswith(suffix) for suffix in allowed_suffixes)


def load_httpx_cookies(path: Path | None = None) -> "httpx.Cookies":
    """Load cookies as an httpx.Cookies object for authenticated downloads.

    Unlike load_auth_from_storage() which returns a simple dict, this function
    returns a proper httpx.Cookies object with domain information preserved.
    This is required for downloads that follow redirects across Google domains.

    Supports the same precedence as load_auth_from_storage():
    1. Explicit path argument (from --storage CLI flag)
    2. NOTEBOOKLM_AUTH_JSON environment variable
    3. File at $NOTEBOOKLM_HOME/storage_state.json

    Args:
        path: Path to storage_state.json. If provided, takes precedence over env vars.

    Returns:
        httpx.Cookies object with all Google cookies.

    Raises:
        FileNotFoundError: If storage file doesn't exist (when using file-based auth).
        ValueError: If required cookies are missing or JSON is malformed.
    """
    storage_state = _load_storage_state(path)

    cookies = httpx.Cookies()
    cookie_names: set[str] = set()

    for entry in storage_state.get("cookies", []):
        domain = entry.get("domain", "")
        name = entry.get("name", "")
        value = entry.get("value", "")

        # Only include cookies from explicitly allowed domains
        if _is_allowed_cookie_domain(domain) and name and value:
            cookies.jar.set_cookie(_storage_entry_to_cookie(entry))
            cookie_names.add(name)

    # Validate that essential cookies are present
    missing = MINIMUM_REQUIRED_COOKIES - cookie_names
    if missing:
        raise ValueError(
            f"Missing required cookies for downloads: {missing}\n"
            f"Run 'notebooklm login' to re-authenticate."
        )

    return cookies


def extract_cookies_with_domains(
    storage_state: dict[str, Any],
) -> DomainCookieMap:
    """Extract Google cookies from storage state preserving original domains.

    Unlike extract_cookies_from_storage() which returns a simple dict of
    name->value, this function returns a dict of (name, domain)->value tuples
    to preserve the original cookie domains. This is required for building
    proper httpx.Cookies jars that handle cross-domain redirects correctly.

    Args:
        storage_state: Parsed JSON from Playwright's storage state file.

    Returns:
        Dict mapping (cookie_name, domain) tuples to values.
        Example: {("SID", ".google.com"): "abc123", ("HSID", ".google.com"): "def456"}

    Raises:
        ValueError: If required cookies (SID) are missing from storage state.
    """
    cookie_map: DomainCookieMap = {}

    for cookie in storage_state.get("cookies", []):
        domain = cookie.get("domain", "")
        name = cookie.get("name")
        value = cookie.get("value", "")

        if not _is_allowed_auth_domain(domain) or not name or not value:
            continue

        key = (name, domain)
        if key not in cookie_map:
            cookie_map[key] = value

    # Validate required cookies exist (any domain)
    cookie_names = {name for name, _ in cookie_map}
    missing = MINIMUM_REQUIRED_COOKIES - cookie_names
    if missing:
        raise ValueError(
            f"Missing required cookies: {missing}\nRun 'notebooklm login' to authenticate."
        )

    return cookie_map


def build_httpx_cookies_from_storage(path: Path | None = None) -> "httpx.Cookies":
    """Build an httpx.Cookies jar with original domains preserved.

    This function loads cookies from storage and creates a proper httpx.Cookies
    jar with the original domains intact. This is critical for cross-domain
    redirects (e.g., to accounts.google.com for token refresh) to work correctly.

    Args:
        path: Path to storage_state.json. If provided, takes precedence over env vars.

    Returns:
        httpx.Cookies jar with all cookies set to their original domains.

    Raises:
        FileNotFoundError: If storage file doesn't exist.
        ValueError: If required cookies are missing or JSON is malformed.
    """
    storage_state = _load_storage_state(path)

    cookies = httpx.Cookies()
    # Dedup by (name, domain) to stay symmetric with save_cookies_to_storage,
    # which keys cookies_by_key on the same pair. Cookie identity per RFC 6265
    # is (name, domain, path), but the save side cannot represent multiple
    # path-scoped siblings yet — so the load side keeps a compatible model
    # rather than constructing pairs that would silently collapse on save.
    seen_keys: set[CookieKey] = set()
    for entry in storage_state.get("cookies", []):
        domain = entry.get("domain", "")
        name = entry.get("name")
        value = entry.get("value", "")
        if not _is_allowed_auth_domain(domain) or not name or not value:
            continue
        key = (name, domain)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        cookies.jar.set_cookie(_storage_entry_to_cookie(entry))

    cookie_names = {name for name, _ in seen_keys}
    missing = MINIMUM_REQUIRED_COOKIES - cookie_names
    if missing:
        raise ValueError(
            f"Missing required cookies: {missing}\nRun 'notebooklm login' to authenticate."
        )

    return cookies


def build_cookie_jar(
    cookies: CookieInput | None = None,
    storage_path: Path | None = None,
) -> httpx.Cookies:
    """Build an httpx.Cookies jar with original domains preserved.

    This is the SINGLE authoritative place to construct cookie jars.

    Priority:
    1. If storage_path exists, load from storage with original domains
    2. Otherwise, use provided cookies while preserving domain keys. Legacy
       flat mappings are assigned to .google.com for backward compatibility.

    Args:
        cookies: Domain-aware (name, domain) cookie dict, or legacy flat
            name-to-value cookie dict.
        storage_path: Path to storage_state.json with domain metadata.

    Returns:
        httpx.Cookies jar populated with auth cookies.
    """
    # If we have a storage file, use it for domain-accurate cookies
    if storage_path and storage_path.exists():
        return build_httpx_cookies_from_storage(storage_path)

    jar = httpx.Cookies()
    for (name, domain), value in normalize_cookie_map(cookies).items():
        jar.set(name, value, domain=domain)
    return jar


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
    lock can't be acquired (e.g. NFS where flock semantics vary), the save
    proceeds anyway; correctness on NFS is best-effort.
    """
    with _file_lock(lock_path, blocking=True, log_prefix="save_cookies_to_storage"):
        yield


def save_cookies_to_storage(cookie_jar: httpx.Cookies, path: Path | None = None) -> None:
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

    Args:
        cookie_jar: The httpx.Cookies object containing the latest cookies.
        path: Path to storage_state.json. If None, cookie sync is skipped.
    """
    if (
        not path
        and "NOTEBOOKLM_AUTH_JSON" in os.environ
        and os.environ["NOTEBOOKLM_AUTH_JSON"].strip()
    ):
        logger.debug("Skipping cookie sync: Auth loaded from NOTEBOOKLM_AUTH_JSON env var")
        return

    if not path:
        logger.debug("Skipping cookie sync: No storage file path available")
        return

    lock_path = path.with_name(f".{path.name}.lock")
    with _file_lock_exclusive(lock_path):
        if not path.exists():
            logger.debug("Skipping cookie sync: Storage file not found at %s", path)
            return

        try:
            storage_data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("Failed to read storage state for cookie sync: %s", e)
            return

        if not isinstance(storage_data, dict) or "cookies" not in storage_data:
            return

        cookies_by_key = {
            (cookie.name, cookie.domain): cookie
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

            key = (name, domain)
            stored_keys.update(_cookie_key_variants(key))
            refreshed_cookie = _find_cookie_for_storage(
                cookies_by_key, key, stored_cookie.get("value")
            )
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
                stored_cookie["path"] = refreshed_cookie.path or stored_cookie.get("path", "/")
                stored_cookie["secure"] = refreshed_cookie.secure
                stored_cookie["httpOnly"] = _cookie_is_http_only(refreshed_cookie)
                updated_count += 1

        for key, cookie in cookies_by_key.items():
            if key in stored_keys:
                continue
            storage_data["cookies"].append(_cookie_to_storage_state(cookie))
            updated_count += 1

        if updated_count > 0:
            temp_path: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    "w",
                    encoding="utf-8",
                    dir=path.parent,
                    prefix=f".{path.name}.",
                    suffix=".tmp",
                    delete=False,
                ) as temp_file:
                    temp_file.write(json.dumps(storage_data, indent=2, ensure_ascii=False))
                    temp_path = Path(temp_file.name)
                os.chmod(temp_path, 0o600)
                temp_path.replace(path)
                logger.debug("Successfully synced %d refreshed cookies to %s", updated_count, path)
            except Exception as e:
                logger.warning("Failed to write updated cookies to %s: %s", path, e)
                if temp_path is not None:
                    try:
                        temp_path.unlink(missing_ok=True)
                    except Exception as cleanup_err:
                        logger.debug("Failed to clean up temp file %s: %s", temp_path, cleanup_err)


def _cookie_is_http_only(cookie: Any) -> bool:
    """Return whether an http.cookiejar.Cookie has the HttpOnly marker."""
    try:
        return bool(
            cookie.has_nonstandard_attr("HttpOnly") or cookie.has_nonstandard_attr("httponly")
        )
    except AttributeError:
        return False


def _cookie_to_storage_state(cookie: Any) -> dict[str, Any]:
    """Convert an http.cookiejar.Cookie to a Playwright storage_state cookie."""
    return {
        "name": cookie.name,
        "value": cookie.value,
        "domain": cookie.domain,
        "path": cookie.path or "/",
        "expires": cookie.expires if cookie.expires is not None else -1,
        "httpOnly": _cookie_is_http_only(cookie),
        "secure": cookie.secure,
        "sameSite": "None",
    }


def _storage_entry_to_cookie(entry: dict[str, Any]) -> http.cookiejar.Cookie:
    """Construct a faithful ``http.cookiejar.Cookie`` from a storage_state entry.

    ``httpx.Cookies.set(name, value, domain=...)`` accepts only those three
    fields, so cookies loaded that way drop ``path``, ``secure``, and
    ``httpOnly``. Each load+save round-trip would erode attributes until disk
    stabilized at ``Path=/``, ``secure=false``, ``httpOnly=false`` — silently
    breaking ``__Host-`` prefix invariants and any future server-enforced
    attribute. This helper is the load-side mirror of
    :func:`_cookie_to_storage_state` so the round-trip is lossless. See #365.
    """
    domain = entry.get("domain", "") or ""
    expires = entry.get("expires")
    expires_value = None if expires in (None, -1) else expires
    # _cookie_is_http_only checks key presence via has_nonstandard_attr; the
    # value is irrelevant. Use "" instead of None so the typed signature
    # ``rest: Mapping[str, str]`` is honored.
    rest: dict[str, str] = {"HttpOnly": ""} if entry.get("httpOnly") else {}
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


def _cookie_key_variants(key: CookieKey) -> set[CookieKey]:
    """Return equivalent host/domain cookie keys for leading-dot domains."""
    name, domain = key
    variants = {key}
    if domain.startswith("."):
        variants.add((name, domain[1:]))
    else:
        variants.add((name, f".{domain}"))
    return variants


def _find_cookie_for_storage(
    cookies_by_key: dict[CookieKey, Any], key: CookieKey, stored_value: str | None
) -> Any | None:
    """Find the best refreshed cookie for a stored cookie key.

    http.cookiejar normalizes ``Domain=accounts.google.com`` to
    ``.accounts.google.com``. If both the original host-only key and the
    normalized domain key exist, prefer the value that differs from storage
    because that is the refreshed Set-Cookie value.
    """
    candidates = [
        cookie
        for variant in _cookie_key_variants(key)
        if (cookie := cookies_by_key.get(variant)) is not None
    ]
    if not candidates:
        return None

    for cookie in candidates:
        if cookie.value != stored_value:
            return cookie
    return candidates[0]


def _replace_cookie_jar(target: httpx.Cookies, source: httpx.Cookies) -> None:
    """Replace target jar contents with source jar contents."""
    if target is source:
        return
    target.jar.clear()
    for cookie in source.jar:
        target.jar.set_cookie(cookie)


NOTEBOOKLM_REFRESH_CMD_ENV = "NOTEBOOKLM_REFRESH_CMD"
_REFRESH_ATTEMPTED_ENV = "_NOTEBOOKLM_REFRESH_ATTEMPTED"
# The ContextVar prevents same-task retry loops in the parent process. The env
# flag is passed only to child refresh commands so recursive CLI calls skip refresh.
_REFRESH_ATTEMPTED_CONTEXT: ContextVar[bool] = ContextVar(
    "_REFRESH_ATTEMPTED_CONTEXT", default=False
)
_REFRESH_LOCK = asyncio.Lock()
_REFRESH_GENERATIONS: dict[str, int] = {}
_AUTH_ERROR_SIGNALS = (
    "authentication expired",
    "redirected to",
    "run 'notebooklm login'",
)


def _should_try_refresh(err: Exception) -> bool:
    """True when an auth failure should trigger NOTEBOOKLM_REFRESH_CMD."""
    if _REFRESH_ATTEMPTED_CONTEXT.get() or os.environ.get(_REFRESH_ATTEMPTED_ENV) == "1":
        return False
    if not os.environ.get(NOTEBOOKLM_REFRESH_CMD_ENV):
        return False
    msg = str(err).lower()
    return any(sig in msg for sig in _AUTH_ERROR_SIGNALS)


async def _run_refresh_cmd(storage_path: Path | None = None, profile: str | None = None) -> None:
    """Run ``NOTEBOOKLM_REFRESH_CMD`` to refresh stored cookies.

    Raises:
        RuntimeError: If the refresh command is missing, times out, or exits
            non-zero.
    """
    cmd = os.environ.get(NOTEBOOKLM_REFRESH_CMD_ENV)
    if not cmd:
        raise RuntimeError(f"{NOTEBOOKLM_REFRESH_CMD_ENV} is not set; cannot refresh cookies.")
    refresh_env = os.environ.copy()
    refresh_env[_REFRESH_ATTEMPTED_ENV] = "1"
    refresh_env["NOTEBOOKLM_REFRESH_PROFILE"] = resolve_profile(profile)
    refresh_env["NOTEBOOKLM_REFRESH_STORAGE_PATH"] = str(
        storage_path or get_storage_path(profile=profile)
    )
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=60,
            env=refresh_env,
        )
    except (subprocess.TimeoutExpired, OSError) as refresh_err:
        raise RuntimeError(
            f"{NOTEBOOKLM_REFRESH_CMD_ENV} failed to execute: {refresh_err}"
        ) from refresh_err
    if result.returncode != 0:
        output = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"{NOTEBOOKLM_REFRESH_CMD_ENV} exited {result.returncode}: {output}")
    logger.info("NotebookLM cookies refreshed via %s", NOTEBOOKLM_REFRESH_CMD_ENV)


async def _fetch_tokens_with_refresh(
    cookie_jar: httpx.Cookies,
    storage_path: Path | None = None,
    profile: str | None = None,
) -> tuple[str, str, bool]:
    """Fetch tokens, optionally running NOTEBOOKLM_REFRESH_CMD on auth expiry."""
    try:
        csrf, session_id = await _fetch_tokens_with_jar(cookie_jar, storage_path)
        return csrf, session_id, False
    except ValueError as err:
        if not _should_try_refresh(err):
            raise
        logger.warning(
            "NotebookLM auth failed (%s). Running %s to refresh cookies.",
            err,
            NOTEBOOKLM_REFRESH_CMD_ENV,
        )
        refresh_storage_path = storage_path or get_storage_path(profile=profile)
        refresh_key = str(refresh_storage_path)
        refresh_generation = _REFRESH_GENERATIONS.get(refresh_key, 0)
        refresh_token = _REFRESH_ATTEMPTED_CONTEXT.set(True)
        try:
            async with _REFRESH_LOCK:
                if _REFRESH_GENERATIONS.get(refresh_key, 0) == refresh_generation:
                    await _run_refresh_cmd(refresh_storage_path, profile)
                    _REFRESH_GENERATIONS[refresh_key] = refresh_generation + 1
                fresh_jar = build_httpx_cookies_from_storage(refresh_storage_path)
                _replace_cookie_jar(cookie_jar, fresh_jar)
            csrf, session_id = await _fetch_tokens_with_jar(cookie_jar, refresh_storage_path)
            return csrf, session_id, True
        finally:
            _REFRESH_ATTEMPTED_CONTEXT.reset(refresh_token)


def _cookie_map_from_jar(cookie_jar: httpx.Cookies) -> DomainCookieMap:
    """Extract a domain-aware auth cookie map from an httpx cookie jar."""
    return {
        (cookie.name, cookie.domain): cookie.value
        for cookie in cookie_jar.jar
        if cookie.name
        and cookie.domain
        and cookie.value is not None
        and _is_allowed_auth_domain(cookie.domain)
    }


def _update_cookie_input(target: CookieInput, fresh: DomainCookieMap) -> None:
    """Update caller-provided cookies in place while preserving key style."""
    use_domain_keys = any(isinstance(key, tuple) for key in target)
    target.clear()
    if use_domain_keys:
        target.update(fresh)
    else:
        target.update(flatten_cookie_map(fresh))  # type: ignore[arg-type]


# --- Keepalive poke ----------------------------------------------------------
# Google's __Secure-1PSIDTS / __Secure-3PSIDTS cookies are the rotating freshness
# partners of __Secure-1PSID / __Secure-3PSID. Their server-side validity window
# is short (minutes-to-hours scale) and Google only emits a rotated value when
# the client asks the identity surface to rotate. Pure RPC traffic against
# notebooklm.google.com never triggers rotation, so a long-lived storage_state
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
NOTEBOOKLM_DISABLE_KEEPALIVE_POKE_ENV = "NOTEBOOKLM_DISABLE_KEEPALIVE_POKE"
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
_POKE_LOCKS_BY_LOOP: "weakref.WeakKeyDictionary[Any, dict[Path | None, asyncio.Lock]]" = (
    weakref.WeakKeyDictionary()
)
# Monotonic timestamp of the last in-process poke *attempt* (success or
# failure), keyed by storage_path. Stamped under ``_POKE_STATE_LOCK`` inside
# ``_try_claim_rotation`` so the check-and-set is atomic across event loops
# and across direct ``_rotate_cookies`` callers. Failure-stampede protection
# comes for free: even a POST that times out has already claimed the slot,
# so 10 fanned-out callers don't each wait 15 s on a hung server.
_LAST_POKE_ATTEMPT_MONOTONIC: dict[Path | None, float] = {}


def _get_poke_lock(storage_path: Path | None) -> asyncio.Lock:
    """Return the ``asyncio.Lock`` for ``(running event loop, storage_path)``.

    Lazily created on first call from each loop/profile pair so the lock binds
    to the current loop. The dict mutation runs under the sync state lock so
    concurrent threads with their own loops don't tear the registry.
    """
    loop = asyncio.get_running_loop()
    with _POKE_STATE_LOCK:
        per_loop = _POKE_LOCKS_BY_LOOP.get(loop)
        if per_loop is None:
            per_loop = {}
            _POKE_LOCKS_BY_LOOP[loop] = per_loop
        lock = per_loop.get(storage_path)
        if lock is None:
            lock = asyncio.Lock()
            per_loop[storage_path] = lock
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
    with _POKE_STATE_LOCK:
        last = _LAST_POKE_ATTEMPT_MONOTONIC.get(storage_path, 0.0)
        now = time.monotonic()
        if last > 0 and (now - last) < _KEEPALIVE_RATE_LIMIT_SECONDS:
            return False
        _LAST_POKE_ATTEMPT_MONOTONIC[storage_path] = now
        return True


def _rotation_lock_path(storage_path: Path | None) -> Path | None:
    """Sibling sentinel used by ``_poke_session`` for cross-process coordination.

    Distinct from the ``.storage_state.json.lock`` used by ``save_cookies_to_storage``
    so a long-running save doesn't block rotations or vice versa.
    """
    if storage_path is None:
        return None
    return storage_path.with_name(f".{storage_path.name}.rotate.lock")


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
    optimisation. The caller's request to notebooklm.google.com is the
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
        # ``follow_redirects=True`` is defensive: empirically RotateCookies
        # answers 200 directly with the rotated Set-Cookie, but if Google ever
        # routes a 30x through an identity hop we still pick up cookies from
        # the terminal response.
        response = await client.post(
            KEEPALIVE_ROTATE_URL,
            headers=_KEEPALIVE_ROTATE_HEADERS,
            content=_KEEPALIVE_ROTATE_BODY,
            follow_redirects=True,
            timeout=_KEEPALIVE_POKE_TIMEOUT,
        )
        # httpx does not auto-raise on 4xx/5xx; without this, a 429 or 5xx from
        # Google would log nothing and the caller would proceed assuming the
        # rotation happened.
        response.raise_for_status()
    except httpx.HTTPError as exc:
        logger.debug("Keepalive RotateCookies POST failed (non-fatal): %s", exc)


async def _fetch_tokens_with_jar(
    cookie_jar: httpx.Cookies, storage_path: Path | None = None
) -> tuple[str, str]:
    """Internal: fetch CSRF and session tokens using a pre-built cookie jar.

    This is the single implementation for all token-fetch paths. All public
    functions (fetch_tokens, fetch_tokens_with_domains) delegate to this.

    Before fetching tokens, makes a best-effort POST to accounts.google.com to
    rotate __Secure-1PSIDTS; see ``_poke_session``. The poke may be skipped if
    ``storage_path`` was modified within the rate-limit window — that path
    relies on the existing on-disk cookies still being fresh.

    Args:
        cookie_jar: httpx.Cookies jar with auth cookies (domain-preserving or fallback).
        storage_path: Optional storage_state.json path, forwarded to
            ``_poke_session`` to gate the rotation poke.

    Returns:
        Tuple of (csrf_token, session_id)

    Raises:
        httpx.HTTPError: If request fails
        ValueError: If tokens cannot be extracted from response
    """
    logger.debug("Fetching CSRF and session tokens from NotebookLM")

    async with httpx.AsyncClient(cookies=cookie_jar) as client:
        await _poke_session(client, storage_path)

        response = await client.get(
            "https://notebooklm.google.com/",
            follow_redirects=True,
            timeout=30.0,
        )
        response.raise_for_status()

        final_url = str(response.url)

        # Check if we were redirected to login
        if is_google_auth_redirect(final_url):
            raise ValueError(
                "Authentication expired or invalid. "
                "Redirected to: " + final_url + "\n"
                "Run 'notebooklm login' to re-authenticate."
            )

        csrf = extract_csrf_from_html(response.text, final_url)
        session_id = extract_session_id_from_html(response.text, final_url)

        # httpx copies the input Cookies object into the client. Copy any
        # redirect Set-Cookie updates back to the caller's jar before it is
        # persisted.
        _replace_cookie_jar(cookie_jar, client.cookies)

        logger.debug("Authentication tokens obtained successfully")
        return csrf, session_id


async def fetch_tokens(
    cookies: CookieInput, storage_path: Path | None = None, profile: str | None = None
) -> tuple[str, str]:
    """Fetch tokens from a cookie mapping. For backward compatibility.

    Prefer AuthTokens.from_storage() which preserves cookie domains. If
    ``NOTEBOOKLM_REFRESH_CMD`` is set and auth has expired, the command is run
    through the platform shell, cookies are reloaded from ``storage_path`` or
    the active profile storage path, and token fetch is retried once. Refresh
    commands receive ``NOTEBOOKLM_REFRESH_STORAGE_PATH`` and
    ``NOTEBOOKLM_REFRESH_PROFILE`` in their environment.

    Args:
        cookies: Google auth cookies. Mutated in place on refresh.
        storage_path: Optional storage_state.json path to reload after refresh.
        profile: Optional profile name exposed to the refresh command.

    Returns:
        Tuple of (csrf_token, session_id)

    Raises:
        httpx.HTTPError: If request fails
        ValueError: If tokens cannot be extracted from response
        RuntimeError: If ``NOTEBOOKLM_REFRESH_CMD`` is set but fails
    """
    jar = build_cookie_jar(cookies=cookies, storage_path=storage_path)
    csrf, session_id, refreshed = await _fetch_tokens_with_refresh(jar, storage_path, profile)
    if refreshed:
        fresh = _cookie_map_from_jar(jar)
        _update_cookie_input(cookies, fresh)
    return csrf, session_id


async def fetch_tokens_with_domains(
    path: Path | None = None, profile: str | None = None
) -> tuple[str, str]:
    """Fetch tokens with domain-preserving cookies from storage.

    Used by CLI helpers. Loads storage, builds jar, fetches tokens, optionally
    runs NOTEBOOKLM_REFRESH_CMD on auth expiry, and persists any refreshed
    cookies back.

    Args:
        path: Path to storage_state.json. If provided, takes precedence over env vars.
        profile: Optional profile name exposed to the refresh command.

    Returns:
        Tuple of (csrf_token, session_id)

    Raises:
        FileNotFoundError: If storage file doesn't exist.
        httpx.HTTPError: If request fails.
        ValueError: If tokens cannot be extracted from response.
        RuntimeError: If ``NOTEBOOKLM_REFRESH_CMD`` is set but fails.
    """
    if path is None and (profile is not None or "NOTEBOOKLM_AUTH_JSON" not in os.environ):
        path = get_storage_path(profile=profile)
    jar = build_httpx_cookies_from_storage(path)
    csrf, session_id, _ = await _fetch_tokens_with_refresh(jar, path, profile)
    save_cookies_to_storage(jar, path)
    return csrf, session_id
