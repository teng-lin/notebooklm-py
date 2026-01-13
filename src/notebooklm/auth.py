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


def extract_cookies_from_storage(storage_state: dict[str, Any]) -> dict[str, str]:
    """Extract Google cookies from Playwright storage state for NotebookLM auth.

    Filters cookies to include those from .google.com, notebooklm.google.com,
    and .googleusercontent.com domains. The googleusercontent.com cookies are
    needed for authenticated media downloads.

    Args:
        storage_state: Parsed JSON from Playwright's storage state file.

    Returns:
        Dict mapping cookie names to values.

    Raises:
        ValueError: If required cookies (SID) are missing from storage state.
    """
    cookies = {}

    for cookie in storage_state.get("cookies", []):
        domain = cookie.get("domain", "")
        if domain in ALLOWED_COOKIE_DOMAINS:
            name = cookie.get("name")
            if name:
                cookies[name] = cookie.get("value", "")

    missing = MINIMUM_REQUIRED_COOKIES - set(cookies.keys())
    if missing:
        raise ValueError(
            f"Missing required cookies: {missing}\nRun 'notebooklm login' to authenticate."
        )

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

    Uses suffix matching with leading dots to ensure proper subdomain validation.
    The leading dot in suffixes (e.g., '.google.com') provides the boundary check:
    - 'lh3.google.com' ends with '.google.com' → True (valid subdomain)
    - 'evil-google.com' does NOT end with '.google.com' → False (not a subdomain)

    Args:
        domain: Cookie domain to check (e.g., '.google.com', 'lh3.google.com')

    Returns:
        True if domain is allowed for downloads.
    """
    # Exact match against the primary allowlist
    if domain in ALLOWED_COOKIE_DOMAINS:
        return True

    # Suffixes for allowed download domains (leading dot provides boundary check)
    allowed_suffixes = (
        ".google.com",
        ".googleusercontent.com",
        ".usercontent.google.com",
    )

    # Check if domain matches or is a subdomain of allowed suffixes
    return any(domain == suffix or domain.endswith(suffix) for suffix in allowed_suffixes)


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
