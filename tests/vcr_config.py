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

import importlib.util
import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import vcr


def _load_sibling(module_name: str, file_name: str) -> Any:
    """Load a sibling module under ``tests/`` by file path.

    The ``tests`` directory is not a Python package (no ``__init__.py``), so
    ``from tests.cassette_patterns import ...`` only works when the repo root
    happens to be on ``sys.path``. That holds in a fresh REPL but NOT inside
    pytest's per-module import, where the loader uses an isolated path that
    omits the repo root. Loading by file path bypasses ``sys.path`` entirely
    and is the same idiom ``tests/unit/test_cookie_redaction.py`` uses to
    import this very file.
    """
    spec = importlib.util.spec_from_file_location(
        module_name, Path(__file__).resolve().parent / file_name
    )
    assert spec is not None and spec.loader is not None, (
        f"Could not load {file_name} next to vcr_config.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_cassette_patterns = _load_sibling("tests_cassette_patterns", "cassette_patterns.py")
recompute_chunk_prefix = _cassette_patterns.recompute_chunk_prefix

# =============================================================================
# Sensitive data patterns to scrub from cassettes
# =============================================================================

# Google authentication cookies and tokens
# Uses capture groups where possible to preserve original names
SENSITIVE_PATTERNS: list[tuple[str, str]] = [
    # Session cookies (preserve name, scrub value).
    # The leading negative lookbehind ``(?<![A-Za-z0-9_-])`` anchors each pattern to a
    # cookie-name boundary so substrings like ``BSID=...`` (a legitimate non-protected
    # cookie that contains ``SID`` as a suffix) are NOT scrubbed. Without the
    # lookbehind the regex matches the ``SID=...`` tail of ``BSID=...`` and corrupts
    # benign fixture data. See ``tests/unit/test_cookie_redaction.py``.
    (r"(?<![A-Za-z0-9_-])SID=[^;]+", "SID=SCRUBBED"),
    (r"(?<![A-Za-z0-9_-])HSID=[^;]+", "HSID=SCRUBBED"),
    (r"(?<![A-Za-z0-9_-])SSID=[^;]+", "SSID=SCRUBBED"),
    (r"(?<![A-Za-z0-9_-])APISID=[^;]+", "APISID=SCRUBBED"),
    (r"(?<![A-Za-z0-9_-])SAPISID=[^;]+", "SAPISID=SCRUBBED"),
    (r"(?<![A-Za-z0-9_-])SIDCC=[^;]+", "SIDCC=SCRUBBED"),
    (r"(?<![A-Za-z0-9_-])OSID=[^;]+", "OSID=SCRUBBED"),
    # NID tracking cookie (Google network ID)
    (r"(?<![A-Za-z0-9_-])NID=[^;]+", "NID=SCRUBBED"),
    # Secure cookies - preserve original name (e.g., __Secure-1PSID=SCRUBBED).
    # The ``__Secure-`` / ``__Host-`` prefixes are already distinctive enough that no
    # legitimate cookie shares them, so no lookbehind is needed here.
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
    # PII scrubbing for Google account holder information
    # ==========================================================================
    # Broadened email scrub: common providers + idempotent on @example.com
    # (the replacement value itself contains @example.com, so a second pass is a no-op).
    # NOTE: we intentionally exclude @example.com from the match so SCRUBBED_EMAIL@example.com
    #   left from a previous scrub round-trips cleanly.
    (
        r'"[A-Za-z0-9._%+\-]+@(?:gmail|googlemail|google|anthropic|outlook|hotmail|yahoo|icloud|protonmail)\.com"',
        '"SCRUBBED_EMAIL@example.com"',
    ),
    # Unquoted-context fallback for raw email mentions (e.g. inside HTML/JS
    # chunks, mailto: hrefs, or rendered templates). Broadened to match the
    # same provider list as the JSON-quoted pattern above so the two stay in
    # sync — gemini-code-assist review thread on PR #477.
    (
        r"[a-zA-Z0-9._%+-]+@(?:gmail|googlemail|google|anthropic|outlook|hotmail|yahoo|icloud|protonmail)\.com",
        "SCRUBBED_EMAIL@example.com",
    ),
    # Display name in aria-label (generic - "Google Account:" prefix is specific enough)
    (r"Google Account: [^\"<]+", "Google Account: SCRUBBED_NAME"),
    # ----------------------------------------------------------------
    # Structural display-name scrub — JSON-key-anchored ONLY.
    # We do NOT use a broad ``>[A-Z][a-z]+\s[A-Z][a-z]+<`` pattern: that would
    # also clobber legitimate two-Capitalized-word fixture content such as
    # ``>Source Title<`` in source-rename cassettes. Anchoring on the JSON key
    # keeps the scrubber surgical.
    # ----------------------------------------------------------------
    (r'"displayName"\s*:\s*"[^"]+"', '"displayName":"SCRUBBED_NAME"'),
    (r'"givenName"\s*:\s*"[^"]+"', '"givenName":"SCRUBBED_NAME"'),
    (r'"familyName"\s*:\s*"[^"]+"', '"familyName":"SCRUBBED_NAME"'),
    # Legacy hard-coded patterns (kept for backward compat with existing cassettes
    # that were sanitized before the structural patterns above were added).
    # Display name in HTML tags (user-specific - add your name if recording new cassettes)
    (r">People Conf<", ">SCRUBBED_NAME<"),
    # Display name in JSON (user-specific - add your name if recording new cassettes)
    (r'"People Conf"', '"SCRUBBED_NAME"'),
    # ==========================================================================
    # Playwright ``storage_state.json`` cookie objects (JSON form, not Cookie-header form)
    # ==========================================================================
    # The patterns above (e.g., ``SID=[^;]+``) only match the ``Cookie: SID=...; ...``
    # header form. A serialized ``storage_state`` (``json.dumps`` of the dict Playwright
    # returns) instead carries ``{"name": "SID", "value": "<secret>", ...}`` objects, so
    # the header-form regexes never fire on a storage_state body and the secret leaks.
    # See ``tests/unit/test_cookie_redaction.py`` for the round-trip assertion.
    #
    # Playwright emits ``name`` before ``value`` in each cookie object. We still register
    # the reversed ordering defensively in case a fixture is hand-authored or a future
    # Playwright version reorders keys.
    #
    # The cookie-value match uses the "string with escapes" idiom
    # ``[^"\\]*(?:\\.[^"\\]*)*`` rather than the naive ``[^"]*``. A naive value class
    # terminates at the first ``"``, even when that quote is JSON-escaped (``\"``),
    # which would leave the tail of the value unredacted in the output (a sensitive
    # cookie value containing a literal quote would be silently leaked). The escape-
    # aware idiom consumes ``\"`` sequences correctly. Cookie names never contain
    # quotes in practice (ASCII identifiers), so the name alternation keeps the
    # simpler ``[^"]+`` class.
    (
        r'("name":\s*"(?:SID|HSID|SSID|APISID|SAPISID|SIDCC|OSID|NID|'
        r'__Secure-[^"]+|__Host-[^"]+)"\s*,\s*"value":\s*")[^"\\]*(?:\\.[^"\\]*)*(")',
        r"\1SCRUBBED\2",
    ),
    (
        r'("value":\s*")[^"\\]*(?:\\.[^"\\]*)*'
        r'("\s*,\s*"name":\s*"(?:SID|HSID|SSID|APISID|SAPISID|'
        r'SIDCC|OSID|NID|__Secure-[^"]+|__Host-[^"]+)")',
        r"\1SCRUBBED\2",
    ),
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

    After string scrubbing runs, ``recompute_chunk_prefix`` is invoked on the
    body to re-derive the ``<count>\\n<payload>\\n`` byte-count prefixes used
    by Google's chunked batchexecute responses. Scrubbing frequently changes
    payload length (e.g. ``21_digit_account_id`` -> ``SCRUBBED_USER_ID``); if
    we left the original counts in place the cassette would fail the byte-count
    assertion in ``tests/unit/test_cassette_shapes.py`` and the decoder's
    tolerance branch would log a warning on every replay. The helper is a
    no-op on bodies that don't look chunked, so it's safe to call
    unconditionally.
    """
    # Scrub response body
    body = response.get("body", {})
    if "string" in body:
        content = body["string"]
        if isinstance(content, bytes):
            try:
                decoded = content.decode("utf-8")
                scrubbed = scrub_string(decoded)
                # Re-derive chunk byte-counts after scrubbing (T8.D7).
                rederived = recompute_chunk_prefix(scrubbed)
                body["string"] = rederived.encode("utf-8")
            except UnicodeDecodeError:
                pass  # Binary content (audio, images), skip scrubbing
        else:
            scrubbed = scrub_string(content)
            # Re-derive chunk byte-counts after scrubbing (T8.D7).
            rederived = recompute_chunk_prefix(scrubbed)
            body["string"] = rederived

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
# Custom VCR Matchers
# =============================================================================


def _rpcids_matcher(r1, r2):
    """Match requests by the ``rpcids`` query parameter.

    All batchexecute POST requests share the same URL path.  Without this
    matcher VCR relies on sequential play-count ordering which is fragile
    (breaks on Windows CI).  Comparing ``rpcids`` makes matching deterministic.
    """
    qs1 = parse_qs(urlparse(r1.uri).query)
    qs2 = parse_qs(urlparse(r2.uri).query)
    assert qs1.get("rpcids") == qs2.get("rpcids")


def _freq_body_matcher(r1: Any, r2: Any) -> bool:
    """Match form-encoded streaming requests by their decoded ``f.req`` payload.

    This matcher is for **non-batchexecute streaming endpoints** (notably the
    streaming chat endpoint) that POST an ``application/x-www-form-urlencoded``
    body carrying an ``f.req`` field whose value is itself a JSON-encoded
    ``[null, "<inner_json>"]`` envelope. The inner JSON, once decoded, is a
    list of positional parameters whose structure is endpoint-specific.

    The default VCR matchers (``method``, ``scheme``, ``host``, ``port``,
    ``path``) cannot distinguish two streaming-chat POSTs because they share
    everything except the body. ``rpcids`` is a query-string concept and does
    not apply to streaming endpoints, so a body-aware matcher is required.

    Match rules:

    1. Both requests must decode to a parseable ``f.req`` param list. If
       neither body parses (e.g. this matcher was invoked for a non-streaming
       request), return ``True`` so the other ``match_on`` matchers
       (``method`` / ``path`` / etc.) drive the decision. If exactly one body
       parses, return ``False`` — the two requests are structurally different.
    2. **Param count** must match. A 9-param shape must not match a 5-param
       shape (catches the C3 stale-cassette regression class).
    3. **Notebook ID** at slot 7 (when the shape has at least 8 elements) must
       match. Two requests differing only in notebook_id are distinct
       interactions.

    Match rules **deliberately ignored**:

    - ``conversation_id`` (slot 4) — legitimately varies across replays. The
       server assigns a fresh conversation_id on each unique ask, and the
       client echoes it back on follow-ups; cassette replay would otherwise
       break on every recording.
    - Per-request nonces / counters at later slots — same rationale.

    This matcher is **opt-in per cassette** (not added to the default
    ``match_on`` list) because most endpoints do not send ``f.req`` and the
    matcher would either no-op or — worse — collapse to identity equality on
    every request.

    Returns:
        ``True`` if the two requests are considered the same interaction,
        ``False`` otherwise.
    """

    def _extract_freq(request: Any) -> list[Any] | None:
        body = request.body
        if not body:
            return None
        if isinstance(body, bytes):
            try:
                body = body.decode("utf-8")
            except UnicodeDecodeError:
                return None

        # Parse application/x-www-form-urlencoded
        qs = parse_qs(body)
        f_req_values = qs.get("f.req", [])
        if not f_req_values:
            return None
        f_req = f_req_values[0]
        if not f_req:
            return None

        try:
            # f.req is the JSON envelope [null, "<inner_json>"].
            outer = json.loads(f_req)
            if not isinstance(outer, list) or len(outer) < 2:
                return None
            inner = outer[1]
            if not isinstance(inner, str):
                return None
            params = json.loads(inner)
            if not isinstance(params, list):
                return None
            return params
        except (json.JSONDecodeError, TypeError, IndexError):
            return None

    p1 = _extract_freq(r1)
    p2 = _extract_freq(r2)

    # If neither side parses, defer to the other matchers (return True so this
    # matcher doesn't block). If exactly one parses, the requests are
    # structurally different — return False.
    if p1 is None or p2 is None:
        return p1 is None and p2 is None

    # Rule 1: param count must agree (catches C3 stale-cassette regression).
    if len(p1) != len(p2):
        return False

    # Rule 2: notebook_id at slot 7 must agree (when present). Two requests
    # carrying different notebook_ids are distinct interactions.
    return not (len(p1) >= 8 and p1[7] != p2[7])


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
    # Match requests by method and path, including rpcids for batchexecute.
    # All batchexecute POSTs share the same URL path; rpcids disambiguates them
    # deterministically (closes C1: replay-order fragility on Windows CI).
    match_on=["method", "scheme", "host", "port", "path", "rpcids"],
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

# Register custom matcher for rpcids-based request differentiation
notebooklm_vcr.register_matcher("rpcids", _rpcids_matcher)
# Opt-in matcher for streaming endpoints whose disambiguator lives in the
# form-encoded ``f.req`` body rather than the query string (e.g. streaming
# chat). Tests that need it add ``"freq"`` to a per-cassette ``match_on``
# override; it is intentionally NOT in the default list because most endpoints
# do not send ``f.req``.
notebooklm_vcr.register_matcher("freq", _freq_body_matcher)
