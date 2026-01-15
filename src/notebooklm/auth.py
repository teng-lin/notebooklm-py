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

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from ._url_utils import contains_google_auth_redirect, is_google_auth_redirect
from .paths import get_storage_path

logger = logging.getLogger(__name__)

# Minimum required cookies (must have at least SID for basic auth)
MINIMUM_REQUIRED_COOKIES = {"SID"}

# Cookie domains to extract from storage state
# Includes googleusercontent.com for authenticated media downloads
ALLOWED_COOKIE_DOMAINS = {
    ".google.com",
    "notebooklm.google.com",
    ".googleusercontent.com",
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

# Default path for Playwright storage state
# Note: Use get_storage_path() for dynamic resolution with NOTEBOOKLM_HOME support
DEFAULT_STORAGE_PATH = get_storage_path()


@dataclass
class AuthTokens:
    """Authentication tokens for NotebookLM API.

    Attributes:
        cookies: Dict of required Google auth cookies
        csrf_token: CSRF token (SNlM0e) extracted from page
        session_id: Session ID (FdrFJe) extracted from page
    """

    cookies: dict[str, str]
    csrf_token: str
    session_id: str

    @property
    def cookie_header(self) -> str:
        """Generate Cookie header value for HTTP requests.

        Returns:
            Semicolon-separated cookie string (e.g., "SID=abc; HSID=def")
        """
        return "; ".join(f"{k}={v}" for k, v in self.cookies.items())

    @classmethod
    async def from_storage(cls, path: Path | None = None) -> "AuthTokens":
        """Create AuthTokens from Playwright storage state file.

        This is the recommended way to create AuthTokens for programmatic use.
        It loads cookies from storage and fetches CSRF/session tokens automatically.

        Args:
            path: Path to storage_state.json. If None, uses default location
                  (~/.notebooklm/storage_state.json).

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
        """
        cookies = load_auth_from_storage(path)
        csrf_token, session_id = await fetch_tokens(cookies)
        return cls(cookies=cookies, csrf_token=csrf_token, session_id=session_id)


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

    Includes exact matches against ALLOWED_COOKIE_DOMAINS plus regional
    Google domains (e.g., .google.com.sg, .google.co.uk, .google.de) where
    SID cookies may be set for users in those regions.

    Args:
        domain: Cookie domain to check (e.g., '.google.com', '.google.com.sg')

    Returns:
        True if domain is allowed for auth cookies.
    """
    # Check if domain is in the primary allowlist or is a valid Google domain (base or regional)
    return domain in ALLOWED_COOKIE_DOMAINS or _is_google_domain(domain)


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
        2. Regional domains - used as fallback when base domain cookie is missing

        This prevents non-deterministic behavior where dict iteration order would
        determine which cookie value wins. See PR #34 for the bug this fixes.

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

    for cookie in storage_state.get("cookies", []):
        domain = cookie.get("domain", "")
        name = cookie.get("name")
        if not _is_allowed_auth_domain(domain) or not name:
            continue

        # Prioritize .google.com cookies over regional domains (e.g., .google.de)
        # to prevent wrong cookie values when the same name exists in multiple domains
        is_base_domain = domain == ".google.com"
        if name not in cookies or is_base_domain:
            if name in cookies and is_base_domain:
                logger.debug(
                    "Cookie %s: using .google.com value (overriding %s)",
                    name,
                    cookie_domains[name],
                )
            cookies[name] = cookie.get("value", "")
            cookie_domains[name] = domain
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
        return json.loads(path.read_text())

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

    return json.loads(storage_path.read_text())


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
    """Check if a cookie domain is allowed for downloads.

    Uses a combination of:
    1. Exact matches against ALLOWED_COOKIE_DOMAINS
    2. Valid Google domains (including regional like .google.com.sg, .google.co.uk)
    3. Suffix matching for Google subdomains (lh3.google.com, etc.)
    4. Suffix matching for googleusercontent.com domains

    Args:
        domain: Cookie domain to check (e.g., '.google.com', 'lh3.google.com')

    Returns:
        True if domain is allowed for downloads.
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
    cookie_names = set()

    for cookie in storage_state.get("cookies", []):
        domain = cookie.get("domain", "")
        name = cookie.get("name", "")
        value = cookie.get("value", "")

        # Only include cookies from explicitly allowed domains
        if _is_allowed_cookie_domain(domain) and name and value:
            cookies.set(name, value, domain=domain)
            cookie_names.add(name)

    # Validate that essential cookies are present
    missing = MINIMUM_REQUIRED_COOKIES - cookie_names
    if missing:
        raise ValueError(
            f"Missing required cookies for downloads: {missing}\n"
            f"Run 'notebooklm login' to re-authenticate."
        )

    return cookies


async def fetch_tokens(cookies: dict[str, str]) -> tuple[str, str]:
    """Fetch CSRF token and session ID from NotebookLM homepage.

    Makes an authenticated request to NotebookLM and extracts the required
    tokens from the page HTML.

    Args:
        cookies: Dict of Google auth cookies

    Returns:
        Tuple of (csrf_token, session_id)

    Raises:
        httpx.HTTPError: If request fails
        ValueError: If tokens cannot be extracted from response
    """
    logger.debug("Fetching CSRF and session tokens from NotebookLM")
    cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items())

    async with httpx.AsyncClient() as client:
        response = await client.get(
            "https://notebooklm.google.com/",
            headers={"Cookie": cookie_header},
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

        logger.debug("Authentication tokens obtained successfully")
        return csrf, session_id
