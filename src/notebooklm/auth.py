"""Authentication handling for NotebookLM API."""

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import httpx


# Minimum required cookies (must have at least SID for basic auth)
MINIMUM_REQUIRED_COOKIES = {"SID"}

# Cookie domains to extract from storage state
ALLOWED_COOKIE_DOMAINS = {".google.com", "notebooklm.google.com"}

# Default path for Playwright storage state (shared with notebooklm-tools skill)
DEFAULT_STORAGE_PATH = Path.home() / ".notebooklm" / "storage_state.json"


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
    async def from_storage(cls, path: Optional[Path] = None) -> "AuthTokens":
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
    """Extract all Google cookies from Playwright storage state for NotebookLM auth."""
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
            f"Missing required cookies: {missing}\n"
            f"Run 'notebooklm login' to authenticate."
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
        if "accounts.google.com" in final_url or "accounts.google.com" in html:
            raise ValueError(
                "Authentication expired or invalid. "
                "Run 'notebooklm login' to re-authenticate."
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
        if "accounts.google.com" in final_url or "accounts.google.com" in html:
            raise ValueError(
                "Authentication expired or invalid. "
                "Run 'notebooklm login' to re-authenticate."
            )
        raise ValueError(
            f"Session ID not found in HTML. Final URL: {final_url}\n"
            "This may indicate the page structure has changed."
        )
    return match.group(1)


def load_auth_from_storage(path: Optional[Path] = None) -> dict[str, str]:
    """Load Google cookies from Playwright storage state file."""
    storage_path = path or DEFAULT_STORAGE_PATH

    if not storage_path.exists():
        raise FileNotFoundError(
            f"Storage file not found: {storage_path}\n"
            f"Run 'notebooklm login' to authenticate first."
        )

    storage_state = json.loads(storage_path.read_text())
    return extract_cookies_from_storage(storage_state)


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
        if "accounts.google.com" in final_url:
            raise ValueError(
                "Authentication expired or invalid. "
                "Redirected to: " + final_url + "\n"
                "Run 'notebooklm login' to re-authenticate."
            )

        csrf = extract_csrf_from_html(response.text, final_url)
        session_id = extract_session_id_from_html(response.text, final_url)

        return csrf, session_id


# Browser profile directory for persistent login
BROWSER_PROFILE_DIR = Path.home() / ".notebooklm" / "browser_profile"


async def download_with_browser(
    url: str,
    output_path: str,
    timeout: float = 60.0,
) -> str:
    """Download a file using Playwright browser with Google authentication.

    This uses the persistent browser profile to download files from Google's
    content servers (lh3.googleusercontent.com, contribution.usercontent.google.com)
    which require cross-domain authentication that httpx cannot provide.

    Args:
        url: The URL to download (must be a Google content URL)
        output_path: Path to save the downloaded file
        timeout: Download timeout in seconds

    Returns:
        The output path if successful

    Raises:
        ImportError: If Playwright is not installed
        ValueError: If download fails or authentication is required
        TimeoutError: If download times out
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        raise ImportError(
            "Playwright is required for downloading media files.\n"
            "Install with: pip install playwright && playwright install chromium"
        )

    output_file = Path(output_path)
    output_file.parent.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        # Use persistent context with saved profile for Google auth
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(BROWSER_PROFILE_DIR),
            headless=True,
            accept_downloads=True,
        )

        try:
            page = await context.new_page()

            # Set up download handling
            download_path = None

            async def handle_download(download):
                nonlocal download_path
                download_path = await download.path()

            page.on("download", handle_download)

            # Navigate to the URL - this triggers download for media files
            response = await page.goto(url, timeout=timeout * 1000)

            # Check if we got redirected to login
            if response and "accounts.google.com" in page.url:
                raise ValueError(
                    "Authentication required. Run 'notebooklm login' to re-authenticate."
                )

            # For direct content (not download), save the response body
            if download_path is None:
                # Check content type
                content_type = response.headers.get("content-type", "") if response else ""

                if "text/html" in content_type:
                    # This is likely a login page
                    raise ValueError(
                        "Download failed: received HTML instead of media file. "
                        "Authentication may have expired. Run 'notebooklm login'."
                    )

                # Save the response body directly
                content = await response.body() if response else b""
                if not content:
                    raise ValueError("Download failed: empty response")

                output_file.write_bytes(content)
            else:
                # Move downloaded file to output path
                import shutil
                shutil.move(download_path, output_path)

            return output_path

        finally:
            await context.close()
