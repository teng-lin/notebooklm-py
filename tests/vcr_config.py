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

Sanitization
------------
Scrub patterns and the byte-count re-derivation helper both live in
:mod:`tests.cassette_patterns` (T8.A4 + T8.D7). This module deliberately holds
NO regex literals so we can never drift between "what the recorder scrubs" and
"what the cassette guard inspects". :func:`scrub_request` / :func:`scrub_response`
here are thin wrappers that delegate to
:func:`tests.cassette_patterns.scrub_string` and
:func:`tests.cassette_patterns.recompute_chunk_prefix`.

Keepalive-poke disable (T8.D4)
------------------------------
Every test that carries ``@pytest.mark.vcr`` (directly or via a module-level
``pytestmark``) automatically runs with
``NOTEBOOKLM_DISABLE_KEEPALIVE_POKE=1`` via the
``_disable_keepalive_poke_for_vcr`` autouse fixture in
:mod:`tests.integration.conftest`. This silences the layer-1
``accounts.google.com/RotateCookies`` keepalive — none of the cassettes
recorded before that poke landed contain it, so leaving it enabled would
produce a guaranteed cassette mismatch on every replay.

If you need a VCR test that actually captures or asserts on ``RotateCookies``
traffic (e.g. a future cassette recording the keepalive itself), opt out with
the ``@pytest.mark.no_keepalive_disable`` marker — the autouse fixture will
leave the env var alone and let the poke fire.
"""

import importlib.util
import json
import os
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
scrub_string = _cassette_patterns.scrub_string


def _is_vcr_record_mode() -> bool:
    """Return True if VCR record mode is enabled via environment.

    Reads ``NOTEBOOKLM_VCR_RECORD`` and treats the case-insensitive values
    ``"1"``, ``"true"``, and ``"yes"`` as enabling record mode. Any other
    value (including unset/empty) returns False.

    Single source of truth for record-mode env-var parsing — both this
    module's VCR-instance config and ``tests/integration/conftest.py``
    consume this helper to avoid drift between the two checks.
    """
    return os.environ.get("NOTEBOOKLM_VCR_RECORD", "").lower() in ("1", "true", "yes")


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
_record_mode = "new_episodes" if _is_vcr_record_mode() else "none"

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
