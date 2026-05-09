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
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeAlias

import httpx

from ._url_utils import contains_google_auth_redirect, is_google_auth_redirect
from .paths import get_storage_path

logger = logging.getLogger(__name__)

CookieKey: TypeAlias = tuple[str, str]
DomainCookieMap: TypeAlias = dict[CookieKey, str]
FlatCookieMap: TypeAlias = dict[str, str]
CookieInput: TypeAlias = DomainCookieMap | FlatCookieMap

# Minimum required cookies (must have at least SID for basic auth)
MINIMUM_REQUIRED_COOKIES = {"SID"}

# Cookie domains to extract from storage state
# Includes googleusercontent.com for authenticated media downloads
ALLOWED_COOKIE_DOMAINS = {
    ".google.com",
    # Playwright storage_state may preserve the leading dot for NotebookLM cookies.
    ".notebooklm.google.com",
    "notebooklm.google.com",
    ".googleusercontent.com",
    "accounts.google.com",  # Required for token refresh redirects
    ".accounts.google.com",  # http.cookiejar may normalize Domain=accounts.google.com
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
            from .paths import get_storage_path

            path = get_storage_path(profile=profile)

        storage_state = _load_storage_state(path)
        cookies = extract_cookies_with_domains(storage_state)

        # Build domain-preserving jar and use it for token fetch
        jar = build_cookie_jar(cookies=cookies)
        csrf_token, session_id = await _fetch_tokens_with_jar(jar)

        # Persist any refreshed cookies from the token fetch
        save_cookies_to_storage(jar, path)

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
    cookie_map = extract_cookies_with_domains(storage_state)

    cookies = httpx.Cookies()
    for (name, domain), value in cookie_map.items():
        cookies.set(name, value, domain=domain)

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


def save_cookies_to_storage(cookie_jar: httpx.Cookies, path: Path | None = None) -> None:
    """Save an updated httpx.Cookies jar back to Playwright storage_state.json.

    This ensures that when Google issues short-lived token refreshes (e.g.
    during 302 redirects to accounts.google.com), those updated cookies are
    serialized back to disk so the session remains valid across CLI invocations.

    If auth was loaded from an environment variable (no file), this is a no-op.

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
        refreshed_cookie = _find_cookie_for_storage(cookies_by_key, key, stored_cookie.get("value"))
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
                temp_file.write(json.dumps(storage_data, indent=2))
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


async def _fetch_tokens_with_jar(cookie_jar: httpx.Cookies) -> tuple[str, str]:
    """Internal: fetch CSRF and session tokens using a pre-built cookie jar.

    This is the single implementation for all token-fetch paths. All public
    functions (fetch_tokens, fetch_tokens_with_domains) delegate to this.

    Args:
        cookie_jar: httpx.Cookies jar with auth cookies (domain-preserving or fallback).

    Returns:
        Tuple of (csrf_token, session_id)

    Raises:
        httpx.HTTPError: If request fails
        ValueError: If tokens cannot be extracted from response
    """
    logger.debug("Fetching CSRF and session tokens from NotebookLM")

    async with httpx.AsyncClient(cookies=cookie_jar) as client:
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


async def fetch_tokens(cookies: CookieInput) -> tuple[str, str]:
    """Fetch tokens from flat cookie dict. For backward compatibility.

    Prefer AuthTokens.from_storage() which preserves cookie domains.

    Args:
        cookies: Dict of Google auth cookies (name→value, no domain info).

    Returns:
        Tuple of (csrf_token, session_id)

    Raises:
        httpx.HTTPError: If request fails
        ValueError: If tokens cannot be extracted from response
    """
    jar = build_cookie_jar(cookies=cookies)
    return await _fetch_tokens_with_jar(jar)


async def fetch_tokens_with_domains(path: Path | None = None) -> tuple[str, str]:
    """Fetch tokens with domain-preserving cookies from storage.

    Used by CLI helpers. Loads storage, builds jar, fetches tokens,
    and persists any refreshed cookies back.

    Args:
        path: Path to storage_state.json. If provided, takes precedence over env vars.

    Returns:
        Tuple of (csrf_token, session_id)

    Raises:
        FileNotFoundError: If storage file doesn't exist.
        httpx.HTTPError: If request fails.
        ValueError: If tokens cannot be extracted from response.
    """
    jar = build_httpx_cookies_from_storage(path)
    result = await _fetch_tokens_with_jar(jar)
    save_cookies_to_storage(jar, path)
    return result
