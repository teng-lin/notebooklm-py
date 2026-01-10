"""VCR.py configuration for recording and replaying HTTP interactions.

This module provides VCR.py configuration for deterministic, offline testing
against recorded API responses. Use this when you want to:

1. Record real API interactions during development
2. Create regression tests from actual API responses
3. Run tests without network access or rate limits

Usage:
    from tests.vcr_config import notebooklm_vcr

    @notebooklm_vcr.use_cassette('my_test.yaml')
    async def test_something():
        async with NotebookLMClient(auth) as client:
            result = await client.notebooks.list()

Recording new cassettes:
    1. Set NOTEBOOKLM_VCR_RECORD=1 (or =true, =yes)
    2. Run the test with valid authentication
    3. Cassette is saved to tests/cassettes/
    4. Verify sensitive data is scrubbed before committing

CI Strategy:
    - PR checks: Use cassettes (fast, deterministic, no auth needed)
    - Nightly: Run with real API to detect drift (NOTEBOOKLM_VCR_RECORD=1)

When to use VCR vs pytest-httpx:
    - pytest-httpx: Crafted test responses for specific scenarios
    - VCR.py: Recorded real responses for regression testing
"""

import os
import re
from typing import Any

import vcr

# =============================================================================
# Sensitive data patterns to scrub from cassettes
# =============================================================================

# Google authentication cookies and tokens
# Uses capture groups where possible to preserve original names
SENSITIVE_PATTERNS: list[tuple[str, str]] = [
    # Session cookies (preserve name, scrub value)
    (r"SID=[^;]+", "SID=SCRUBBED"),
    (r"HSID=[^;]+", "HSID=SCRUBBED"),
    (r"SSID=[^;]+", "SSID=SCRUBBED"),
    (r"APISID=[^;]+", "APISID=SCRUBBED"),
    (r"SAPISID=[^;]+", "SAPISID=SCRUBBED"),
    (r"SIDCC=[^;]+", "SIDCC=SCRUBBED"),
    (r"OSID=[^;]+", "OSID=SCRUBBED"),
    # NID tracking cookie (Google network ID)
    (r"NID=[^;]+", "NID=SCRUBBED"),
    # Secure cookies - preserve original name (e.g., __Secure-1PSID=SCRUBBED)
    (r"(__Secure-[^=]+)=[^;]+", r"\1=SCRUBBED"),
    (r"(__Host-[^=]+)=[^;]+", r"\1=SCRUBBED"),
    # CSRF and session tokens in HTML/JSON (WIZ_global_data format)
    (r'"SNlM0e"\s*:\s*"[^"]+"', '"SNlM0e":"SCRUBBED_CSRF"'),
    (r'"FdrFJe"\s*:\s*"[^"]+"', '"FdrFJe":"SCRUBBED_SESSION"'),
    # Session ID in URL query params
    (r"f\.sid=[^&]+", "f.sid=SCRUBBED"),
    # CSRF token in request body (form-encoded: at=value)
    (r"at=[A-Za-z0-9_-]+", "at=SCRUBBED_CSRF"),
    # CSRF token in JSON response (echoed by httpbin or in error messages)
    (r'"at"\s*:\s*"[^"]+"', '"at":"SCRUBBED_CSRF"'),
    # ==========================================================================
    # PII and sensitive data in WIZ_global_data (HTML/JSON responses)
    # ==========================================================================
    # User email address (specific field)
    (r'"oPEP7c"\s*:\s*"[^"]+"', '"oPEP7c":"SCRUBBED_EMAIL"'),
    # Google User IDs (21-digit account identifiers)
    (r'"S06Grb"\s*:\s*"[^"]+"', '"S06Grb":"SCRUBBED_USER_ID"'),
    (r'"W3Yyqf"\s*:\s*"[^"]+"', '"W3Yyqf":"SCRUBBED_USER_ID"'),
    (r'"qDCSke"\s*:\s*"[^"]+"', '"qDCSke":"SCRUBBED_USER_ID"'),
    # Google API keys (browser-side, but still sensitive)
    (r'"B8SWKb"\s*:\s*"[^"]+"', '"B8SWKb":"SCRUBBED_API_KEY"'),
    (r'"VqImj"\s*:\s*"[^"]+"', '"VqImj":"SCRUBBED_API_KEY"'),
    # OAuth client ID
    (r'"QGcrse"\s*:\s*"[^"]+"', '"QGcrse":"SCRUBBED_CLIENT_ID"'),
    (r'"iQJtYd"\s*:\s*"[^"]+"', '"iQJtYd":"SCRUBBED_PROJECT_ID"'),
    # ==========================================================================
    # Generic email scrubbing (catches emails in HTML and other JSON fields)
    # ==========================================================================
    # Email in any context (conservative pattern for common email formats)
    (r"peopleconf@gmail\.com", "SCRUBBED_EMAIL@example.com"),
    # Display name in HTML (People Conf)
    (r">People Conf<", ">SCRUBBED_NAME<"),
    (r'"People Conf"', '"SCRUBBED_NAME"'),
]


def scrub_string(text: str) -> str:
    """Apply all sensitive pattern replacements to a string."""
    for pattern, replacement in SENSITIVE_PATTERNS:
        text = re.sub(pattern, replacement, text)
    return text


def scrub_request(request: Any) -> Any:
    """Scrub sensitive data from recorded HTTP request.

    Handles:
    - Cookie headers
    - URL query parameters (session IDs)
    - Request body (CSRF tokens)
    """
    # Scrub Cookie header
    if "Cookie" in request.headers:
        request.headers["Cookie"] = scrub_string(request.headers["Cookie"])

    # Scrub URL (contains f.sid session parameter)
    if request.uri:
        request.uri = scrub_string(request.uri)

    # Scrub request body (contains at= CSRF token)
    if request.body:
        if isinstance(request.body, bytes):
            try:
                decoded = request.body.decode("utf-8")
                request.body = scrub_string(decoded).encode("utf-8")
            except UnicodeDecodeError:
                pass  # Binary content, skip scrubbing
        else:
            request.body = scrub_string(request.body)

    return request


def scrub_response(response: dict[str, Any]) -> dict[str, Any]:
    """Scrub sensitive data from recorded HTTP response.

    Handles:
    - Response body (may contain tokens in JSON or echoed headers)
    - Response headers (Set-Cookie headers may contain session tokens)
    - Both string and bytes response bodies
    """
    # Scrub response body
    body = response.get("body", {})
    if "string" in body:
        content = body["string"]
        if isinstance(content, bytes):
            try:
                decoded = content.decode("utf-8")
                body["string"] = scrub_string(decoded).encode("utf-8")
            except UnicodeDecodeError:
                pass  # Binary content (audio, images), skip scrubbing
        else:
            body["string"] = scrub_string(content)

    # Scrub Set-Cookie headers (may contain session tokens)
    headers = response.get("headers", {})
    if "Set-Cookie" in headers:
        cookies = headers["Set-Cookie"]
        if isinstance(cookies, list):
            headers["Set-Cookie"] = [scrub_string(c) for c in cookies]
        elif isinstance(cookies, str):
            headers["Set-Cookie"] = scrub_string(cookies)

    return response


# =============================================================================
# VCR Configuration
# =============================================================================

# Determine record mode from environment
# Set NOTEBOOKLM_VCR_RECORD=1 (or =true, =yes) to record new cassettes
_record_env = os.environ.get("NOTEBOOKLM_VCR_RECORD", "").lower()
_record_mode = "new_episodes" if _record_env in ("1", "true", "yes") else "none"

# Main VCR instance for notebooklm-py tests
notebooklm_vcr = vcr.VCR(
    # Cassette storage location
    cassette_library_dir="tests/cassettes",
    # Record mode: 'none' = only replay (CI), 'new_episodes' = record if missing
    record_mode=_record_mode,
    # Match requests by method and path, NOT query params (contain session IDs)
    match_on=["method", "scheme", "host", "port", "path"],
    # Scrub sensitive data before recording
    before_record_request=scrub_request,
    before_record_response=scrub_response,
    # Filter these headers entirely (don't record them at all)
    filter_headers=[
        "Authorization",
        "X-Goog-AuthUser",
        "X-Client-Data",  # Chrome user data header
    ],
    # Decode compressed responses for easier inspection
    decode_compressed_response=True,
)
