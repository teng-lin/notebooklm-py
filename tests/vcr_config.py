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
:mod:`tests.cassette_patterns`. This module deliberately holds
NO regex literals so we can never drift between "what the recorder scrubs" and
"what the cassette guard inspects". :func:`scrub_request` / :func:`scrub_response`
here are thin wrappers that delegate to
:func:`tests.cassette_patterns.scrub_string` and
:func:`tests.cassette_patterns.recompute_chunk_prefix`.

Keepalive-poke disable
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
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import vcr


def _load_sibling(module_name: str, file_name: str) -> Any:
    """Load a sibling module under ``tests/`` by file path.

    ``from tests.cassette_patterns import ...`` now resolves inside pytest via
    ``pythonpath = ["."]`` (pyproject, #1482); loading by file path is kept as a
    ``sys.path``-independent fallback so this module also works when imported
    outside pytest, the same idiom ``tests/unit/test_cookie_redaction.py`` uses
    to import this very file.
    """
    spec = importlib.util.spec_from_file_location(
        module_name, Path(__file__).resolve().parent / file_name
    )
    if spec is None or spec.loader is None:  # pragma: no cover - import wiring guard
        raise ImportError(f"Could not load {file_name} next to vcr_config.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_cassette_patterns = _load_sibling("tests_cassette_patterns", "cassette_patterns.py")
recompute_chunk_prefix = _cassette_patterns.recompute_chunk_prefix
scrub_string = _cassette_patterns.scrub_string
scrub_cookie_header = _cassette_patterns.scrub_cookie_header
scrub_set_cookie = _cassette_patterns.scrub_set_cookie
build_synthetic_error_response = _cassette_patterns.build_synthetic_error_response
synthetic_error_cassette_name = _cassette_patterns.synthetic_error_cassette_name
SYNTHETIC_ERROR_CASSETTE_PREFIX = _cassette_patterns.SYNTHETIC_ERROR_CASSETTE_PREFIX
VALID_ERROR_MODES = _cassette_patterns.VALID_ERROR_MODES

# env var name shared with :mod:`notebooklm._error_injection`. Kept in
# sync as a local copy so the VCR-only replay path (which does not import
# :mod:`notebooklm._error_injection`) can still parse the env var without
# dragging the production module in. Unit tests in
# ``tests/unit/test_vcr_config.py`` import ``ERROR_INJECT_ENV_VAR`` directly
# from the canonical home — this duplication covers ONLY the VCR-replay
# path, not the unit-test path.
ERROR_INJECT_ENV_VAR = "NOTEBOOKLM_VCR_RECORD_ERRORS"


def _is_vcr_record_mode() -> bool:
    """Return True if VCR record mode is enabled via environment.

    Reads ``NOTEBOOKLM_VCR_RECORD`` and treats the case-insensitive values
    ``"1"``, ``"true"``, and ``"yes"`` as enabling record mode. Any other
    value (including unset/empty) returns False.

    Single source of truth for record-mode env-var parsing — both this
    module's VCR-instance config and ``tests/integration/conftest.py``
    consume this helper to avoid drift between the two checks.
    """
    return os.environ.get("NOTEBOOKLM_VCR_RECORD", "").casefold() in ("1", "true", "yes")


def get_error_injection_mode() -> str | None:
    """Return the active synthetic-error mode from the environment, or ``None``.

    Reads ``NOTEBOOKLM_VCR_RECORD_ERRORS`` and validates the value against
    :data:`VALID_ERROR_MODES`. Unset, empty, or unrecognized values resolve to
    ``None`` so plumbing never crashes on a typo — the unit tests assert the
    typo path explicitly. The value comparison is case-insensitive.

    This helper mirrors ``_get_error_injection_mode`` in
    :mod:`notebooklm._error_injection`; both sides validate against the
    same canonical set in :mod:`tests.cassette_patterns` so they cannot
    drift.
    """
    # ``.casefold()`` (not ``.lower()``) for parity with ``_is_vcr_record_mode``
    # and the project's Unicode-aware case-insensitive rule — ASCII-identical
    # for VALID_ERROR_MODES, so this is consistency hygiene (#1268).
    raw = os.environ.get(ERROR_INJECT_ENV_VAR, "").strip().casefold()
    if not raw:
        return None
    return raw if raw in VALID_ERROR_MODES else None


def scrub_request(request: Any) -> Any:
    """Scrub sensitive data from recorded HTTP request.

    Handles:
    - Cookie headers
    - URL query parameters (session IDs)
    - Request body (CSRF tokens)
    """
    # Scrub Cookie header. ``scrub_string`` runs first for the name-anchored
    # session-cookie / auth-token patterns and the JSON-shape coverage; then
    # ``scrub_cookie_header`` makes a NAME-AGNOSTIC pass that clears EVERY
    # remaining cookie pair's value (the ``_ga`` / ``_gcl_au`` / ``AEC`` class
    # that is not on the cookie allowlist). Both are idempotent, so the order
    # is safe and the name-agnostic pass adds coverage without weakening the
    # existing scrubbing.
    if "Cookie" in request.headers:
        request.headers["Cookie"] = scrub_cookie_header(scrub_string(request.headers["Cookie"]))

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


def _substitute_synthetic_error(response: dict[str, Any]) -> dict[str, Any]:
    """defense-in-depth synthetic-error substitution.

    When ``NOTEBOOKLM_VCR_RECORD_ERRORS`` resolves to a valid mode (see
    :data:`VALID_ERROR_MODES`), rewrite the response shape to the canonical
    synthetic-error shape from :mod:`tests.cassette_patterns`.

    The error-injection middleware in
    :mod:`notebooklm._middleware.error_injection` already substitutes
    the live response BEFORE it reaches VCR, so in normal recording this hook
    sees the synthetic shape already. This pass exists so that:

    1. Tests that bypass the production transport (e.g. direct
       ``notebooklm_vcr.use_cassette`` with a hand-built ``httpx.AsyncClient``)
       still record synthetic shapes when the env var is set.
    2. The substitution is observable from cassette-only paths in CI without
       requiring access to the production transport.

    Returns ``response`` unchanged when the env var is unset or the value is
    not a recognized mode.
    """
    mode = get_error_injection_mode()
    if mode is None:
        return response
    status_code, body_bytes, headers = build_synthetic_error_response(mode)
    response["status"] = {"code": status_code, "message": ""}
    response["body"] = {"string": body_bytes}
    # Preserve any incoming headers (e.g. Content-Length VCR fills in) but
    # overlay our synthetic ones so the Content-Type / Retry-After hints land
    # on the recorded shape.
    out_headers = response.get("headers", {})
    if not isinstance(out_headers, dict):
        out_headers = {}
    for k, v in headers.items():
        out_headers[k] = [v]
    response["headers"] = out_headers
    return response


def _ci_header_keys(headers: dict[str, Any], name: str) -> list[str]:
    """Return ALL keys in ``headers`` matching ``name`` case-insensitively.

    Google serves HTTP/2, whose header names are lowercase (``set-cookie``),
    while older cassettes were recorded with title-case (``Set-Cookie``). A
    case-sensitive ``name in headers`` check misses the lowercase form and
    silently leaves live ``Set-Cookie`` session tokens unscrubbed — the
    ``test_cassette_shapes`` guard would catch the leak, but the scrubber must
    prevent it, not merely rely on detection. Returns *every* matching key (not
    just the first) so a dict carrying both ``Set-Cookie`` and ``set-cookie``
    gets every token-bearing entry scrubbed, not one.
    """
    lowered = name.lower()
    return [key for key in headers if key.lower() == lowered]


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
    assertion in ``tests/_guardrails/test_cassette_shapes.py`` and the decoder's
    tolerance branch would log a warning on every replay. The helper is a
    no-op on bodies that don't look chunked, so it's safe to call
    unconditionally.

    Synthetic-error recording: when ``NOTEBOOKLM_VCR_RECORD_ERRORS`` is set to a valid mode,
    :func:`_substitute_synthetic_error` runs FIRST so that downstream scrub
    steps see the canonical synthetic shape rather than whatever the wire
    produced (the error-injection middleware in
    :mod:`notebooklm._middleware.error_injection` normally already
    substituted, but this pass closes the loop for VCR-only test paths).
    """
    # synthetic-error substitution (no-op when env var unset).
    response = _substitute_synthetic_error(response)

    # Scrub response body
    body = response.get("body", {})
    if "string" in body:
        content = body["string"]
        if isinstance(content, bytes):
            try:
                decoded = content.decode("utf-8")
                scrubbed = scrub_string(decoded)
                # Re-derive chunk byte-counts after scrubbing.
                rederived = recompute_chunk_prefix(scrubbed)
                body["string"] = rederived.encode("utf-8")
            except UnicodeDecodeError:
                pass  # Binary content (audio, images), skip scrubbing
        else:
            scrubbed = scrub_string(content)
            # Re-derive chunk byte-counts after scrubbing.
            rederived = recompute_chunk_prefix(scrubbed)
            body["string"] = rederived

    # Scrub Set-Cookie headers (may contain session tokens). ``scrub_string``
    # runs first for the name-anchored / auth-token patterns; then
    # ``scrub_set_cookie`` makes a NAME-AGNOSTIC pass that clears the leading
    # cookie pair's value while preserving the cookie attributes (Path / Domain
    # / Expires / Secure / HttpOnly / ...). Both passes are idempotent.
    headers = response.get("headers", {})
    for cookie_key in _ci_header_keys(headers, "Set-Cookie"):
        cookies = headers[cookie_key]
        if isinstance(cookies, list):
            headers[cookie_key] = [scrub_set_cookie(scrub_string(c)) for c in cookies]
        elif isinstance(cookies, str):
            headers[cookie_key] = scrub_set_cookie(scrub_string(cookies))

    # Scrub resumable-upload session tokens echoed in response headers. The
    # ``X-Goog-Upload-(Control-)URL`` values embed ``upload_id=<token>`` (handled
    # by ``scrub_string``'s upload_id pattern); ``X-GUploader-UploadID`` carries a
    # bare token with no anchor, so its value is replaced wholesale. Without this
    # a fresh recording leaks per-upload tokens past ``scrub_response`` (the guard
    # catches them, but the scrubber should prevent them — not just detect).
    for _name in ("X-Goog-Upload-URL", "X-Goog-Upload-Control-URL"):
        for _key in _ci_header_keys(headers, _name):
            _vals = headers[_key]
            headers[_key] = (
                [scrub_string(v) for v in _vals] if isinstance(_vals, list) else scrub_string(_vals)
            )
    for _uploader_key in _ci_header_keys(headers, "X-GUploader-UploadID"):
        _vals = headers[_uploader_key]
        headers[_uploader_key] = (
            ["SCRUBBED_UPLOAD_ID" for _ in _vals]
            if isinstance(_vals, list)
            else "SCRUBBED_UPLOAD_ID"
        )

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


# Volatile keys that the matcher recursively strips from any dict-shaped node
# inside the decoded ``f.req`` payload before comparison. These are per-request
# values that the server generates fresh on each call (timestamps, request IDs,
# nonces) and which would otherwise cause every replay to fail the matcher.
#
# Kept as a frozenset so the membership test stays O(1) and the value is
# trivially copyable into the test suite for the fallback-path assertions.
# Lowercase comparison is performed in :func:`_strip_volatile` so synonyms like
# ``RequestId`` / ``request_id`` / ``requestID`` all hit the same entry.
_FREQ_VOLATILE_KEYS: frozenset[str] = frozenset(
    {
        "timestamp",
        "clienttimestamp",
        "servertimestamp",
        "requestid",
        "request_id",
        "_reqid",
        "reqid",
        "nonce",
        "clientnonce",
    }
)

# UUID v4 placeholder used by :func:`_strip_volatile` to fold session-drift
# UUIDs (notebook IDs, source IDs, project IDs) onto a canonical value. The
# matcher's intent is to catch **structural** drift (different RPC id, different
# arg shape, different non-UUID values) — but in practice cassettes are recorded
# against one notebook UUID and replayed against a different one with the same
# structural shape. Treating UUID-shaped leaves as effectively volatile (per the
# spec's "widen the volatile-key scrub list" escape hatch in P1-3) keeps the
# matcher robust across recording sessions while still failing on meaningful
# drift like RPC id changes or non-UUID arg drift.
#
# The placeholder string carries the same length as a real UUID v4 so any
# byte-count assertions downstream stay accurate. ``_NORMALIZED`` is not a
# substring of any cassette UUID we've ever recorded — chosen so a search for
# this placeholder in a cassette flags only the normalization, not real data.
_UUID_PLACEHOLDER = "00000000-0000-0000-0000-_NORMALIZED"

# Canonical UUID v4 string regex. Matches the 8-4-4-4-12 hex layout used by
# every UUID NotebookLM emits (notebook IDs, source IDs, artifact IDs, project
# IDs, conversation IDs all share this shape). Anchored to word boundaries so a
# UUID embedded in a larger string still normalizes but a hex string of similar
# length (e.g. a session token) does not accidentally match.
_UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)


def _normalize_uuids(value: str) -> str:
    """Replace every UUID v4 substring in ``value`` with :data:`_UUID_PLACEHOLDER`.

    Used by :func:`_strip_volatile` on string leaves so two requests carrying
    different but structurally-equivalent UUIDs still match. See the
    :data:`_UUID_PLACEHOLDER` docstring for the rationale on why UUIDs are
    treated as functionally volatile by this matcher.

    Empty strings are also folded onto the placeholder so a cassette
    recorded with a notebook UUID still matches a replay where no fixture
    UUID was bound to the env (the live request would send an empty-string
    identifier in that slot). This is the same "incidental drift, not a
    meaningful mismatch" reasoning the UUID normalization itself uses.
    """
    if value == "":
        return _UUID_PLACEHOLDER
    return _UUID_RE.sub(_UUID_PLACEHOLDER, value)


def _strip_volatile(node: Any) -> Any:
    """Return a normalized copy of ``node`` ready for matcher comparison.

    Three transformations run recursively over the decoded ``f.req`` payload:

    1. **Drop volatile dict keys** — any key whose lowercased name is in
       :data:`_FREQ_VOLATILE_KEYS` is removed entirely (``timestamp``,
       ``requestId``, ``nonce``, ...).
    2. **Normalize UUID and empty-string leaves** — every UUID v4 substring
       and every empty string is replaced with :data:`_UUID_PLACEHOLDER` so
       notebook / source / artifact UUIDs that drift across recording
       sessions still match (this is the spec's "widen the volatile-key
       scrub list" escape hatch from P1-3).
    3. **Preserve everything else** — lists keep their order, non-UUID
       strings, numbers, booleans, and ``None`` pass through unchanged.
       This means meaningful drift (different RPC ids, different non-UUID
       arg values, different arg shapes) is still caught.

    See the :func:`_freq_body_matcher` docstring for the full design.
    """
    if isinstance(node, dict):
        return {
            k: _strip_volatile(v)
            for k, v in node.items()
            if not (isinstance(k, str) and k.lower() in _FREQ_VOLATILE_KEYS)
        }
    if isinstance(node, list):
        return [_strip_volatile(v) for v in node]
    if isinstance(node, str):
        return _normalize_uuids(node)
    return node


# Single token every leaf collapses to under :func:`_shape_only`. Module-level
# constant so the unit tests can import it directly to assert the structural-
# skeleton contract. Update the placeholder string in lockstep with the test
# suite if it ever needs to change (low likelihood — the value is internal).
_LEAF = "_FREQ_LEAF"


def _shape_only(node: Any) -> Any:
    """Return a structural skeleton of ``node`` — preserves nesting, drops values.

    Used for the **batchexecute** matcher path where leaf-value drift is
    common between recording sessions and the test suite:

    - Cassettes were recorded with one set of fixture values (notebook UUIDs,
      page sizes, free-form note titles, source URLs, ``null`` placeholders
      for optional slots, ...) and the tests now run with different fixture
      values that may fill or null those same slots differently.
    - The recorded RESPONSE is the asset of interest — replaying it back
      for the new request is the entire point of cassette replay. The
      matcher only needs to confirm "same RPC kind, same nesting shape",
      not "same leaf values". ``rpcids`` (URL-query matcher) already gates
      RPC identity; this matcher is the body-level structural double-check.

    Transformations:

    1. Recursively walk lists and dicts; preserve list LENGTHS and dict
       KEY SETS (after volatile-key stripping). This is what the matcher
       actually compares.
    2. Strip volatile dict keys (per :data:`_FREQ_VOLATILE_KEYS`).
    3. Replace every non-container leaf — including ``None`` — with
       :data:`_LEAF`. ``None`` is folded too because in practice the
       biggest source of test/cassette drift is "this optional slot was
       ``null`` at record time and is filled at replay time (or
       vice-versa)". The matcher's job stops at confirming list shape;
       fill-vs-null is the test's own assertion territory.

    The streaming-chat matcher path deliberately uses :func:`_strip_volatile`
    (which preserves non-UUID string and number leaves AND ``None``) instead
    because the chat-shape tests rely on slot-7 notebook-id equality and
    slot-4 conv-id drift — see ``test_vcr_config.py`` for the exhaustive
    slot-by-slot contract.
    """
    if isinstance(node, dict):
        return {
            k: _shape_only(v)
            for k, v in node.items()
            if not (isinstance(k, str) and k.lower() in _FREQ_VOLATILE_KEYS)
        }
    if isinstance(node, list):
        return [_shape_only(v) for v in node]
    return _LEAF


_CREATE_ARTIFACT_RPC_ID = "R7cb6c"
_CREATE_ARTIFACT_OPTIONS_SENTINEL = {"_CREATE_ARTIFACT_CLIENT_OPTIONS": True}
_CREATE_ARTIFACT_OLD_CLIENT_OPTIONS = [2]
_CREATE_ARTIFACT_LIVE_CLIENT_OPTIONS = [
    2,
    None,
    None,
    [1, None, None, None, None, None, None, None, None, None, [1]],
    [[1, 4, 8, 2, 3, 6]],
]


def _is_create_artifact_client_options(node: Any) -> bool:
    """Return whether ``node`` is a known CREATE_ARTIFACT options envelope.

    Older cassettes recorded the compact ``[2]`` first param. Live UI captures
    on 2026-06-15 send ``[2, None, None, ..., [[1, 4, 8, 2, 3, 6]]]`` instead.
    Both identify the same client-options slot for matcher purposes; the actual
    artifact spec remains matched structurally.
    """
    if not isinstance(node, list):
        return False
    return node in (
        _CREATE_ARTIFACT_OLD_CLIENT_OPTIONS,
        _CREATE_ARTIFACT_LIVE_CLIENT_OPTIONS,
    )


def _normalize_create_artifact_options(decoded_outer: Any) -> Any:
    """Canonicalize old/new CREATE_ARTIFACT client-options shapes for VCR replay."""
    if not isinstance(decoded_outer, list):
        return decoded_outer

    normalized_outer: list[Any] = []
    for batch in decoded_outer:
        if not isinstance(batch, list):
            normalized_outer.append(batch)
            continue
        normalized_batch: list[Any] = []
        for entry in batch:
            if (
                isinstance(entry, list)
                and len(entry) >= 2
                and entry[0] == _CREATE_ARTIFACT_RPC_ID
                and isinstance(entry[1], list)
                and entry[1]
                and _is_create_artifact_client_options(entry[1][0])
            ):
                args = list(entry[1])
                args[0] = _CREATE_ARTIFACT_OPTIONS_SENTINEL
                normalized_batch.append([entry[0], args, *entry[2:]])
            else:
                normalized_batch.append(entry)
        normalized_outer.append(normalized_batch)
    return normalized_outer


def _normalize_freq_string(body: str) -> str | None:
    """Extract and lightly-normalize the raw ``f.req`` value from a form body.

    Returns ``None`` when the body is not form-encoded or does not contain an
    ``f.req`` field. The returned string is the raw ``f.req`` value with
    surrounding whitespace stripped — used as the fallback comparison key when
    JSON parsing of the envelope fails (malformed payload, unexpected shape).
    """
    qs = parse_qs(body)
    f_req_values = qs.get("f.req", [])
    if not f_req_values:
        return None
    f_req = f_req_values[0]
    if not f_req:
        return None
    return f_req.strip()


def _freq_body_matcher(r1: Any, r2: Any) -> bool:
    """Match form-encoded RPC requests by their decoded ``f.req`` payload.

    Now wired into the **default** ``match_on`` tuple so every cassette
    replay benefits from body-level disambiguation, not just the streaming
    endpoints. Two shapes are recognized:

    1. **Streaming-chat envelope** ``[null, "<inner_json>"]`` where the inner
       JSON is a positional parameter list. Used by the streaming chat
       endpoint. Volatile slot 4 (``conversation_id``) is dropped — the
       server assigns a fresh conversation_id on each ask and the client
       echoes it back, so equality there would break every cassette replay.
       Slot 7 (``notebook_id``) and the param-count are still checked.
    2. **Batchexecute envelope** ``[[[rpc_id, args_json, null, "generic"]]]``
       where ``args_json`` is itself a JSON-encoded list of positional
       arguments. Used by the standard batchexecute POST. After decoding
       the inner ``args_json`` we hand the result to :func:`_shape_only`,
       which strips volatile dict keys (:data:`_FREQ_VOLATILE_KEYS`) and
       folds every non-container leaf — strings, numbers, booleans,
       ``None`` — onto :data:`_LEAF` before comparison. The matcher then
       fails on structural drift (different arg-list lengths, different
       nesting depths, different non-volatile dict key sets) and passes
       on leaf-value drift (different fixture UUIDs, different free-form
       text, ``null`` vs filled in the same slot). RPC-id equality is
       enforced upstream by the URL ``rpcids`` matcher, so collapsing
       the RPC-id leaf here is intentional and documented in the
       :func:`_shape_only` docstring. This shape-only comparison is the
       spec's "widen the volatile-key scrub list" escape hatch from P1-3
       — strict leaf matching would break cassette replay across
       recording sessions; see the PR body for the full trade-off.

    Robustness rules:

    - If **both** bodies lack a parseable ``f.req`` field (e.g. GET requests,
      uploads, the matcher invoked on a multipart-bodied request), return
      ``True`` so the other ``match_on`` matchers (``method`` / ``path`` /
      ``rpcids`` / ...) drive the decision. Returning ``False`` here would
      incorrectly block every non-RPC request the cassette contains.
    - If **exactly one** body carries ``f.req``, return ``False`` — the two
      requests are structurally different.
    - If JSON parsing of the envelope fails on either side (malformed payload
      from a weird recorder, future shape we don't know about), fall back to
      a normalized **string** comparison of the raw ``f.req`` value. This
      keeps the matcher conservative: byte-identical bodies still match,
      different bodies still mismatch, but we no longer crash or silently
      accept the wrong cassette entry.

    Returns:
        ``True`` if the two requests are considered the same interaction,
        ``False`` otherwise.
    """

    def _decode_freq_envelope(request: Any) -> tuple[str | None, Any | None]:
        """Return ``(raw_f_req, parsed_payload)`` for a request.

        - ``raw_f_req`` is the URL-decoded ``f.req`` value (string), or
          ``None`` if the body lacks ``f.req`` entirely.
        - ``parsed_payload`` is one of:
          * ``("chat", params_list)`` — streaming-chat shape recognized.
          * ``("batch", outer_list)`` — batchexecute shape recognized.
          * ``("raw", f_req)`` — JSON parse succeeded but shape is unfamiliar;
             matcher falls back to comparing the raw normalized string.
          * ``None`` — JSON parse failed entirely; same string fallback.
        """
        body = request.body
        if not body:
            return None, None
        if isinstance(body, bytes):
            try:
                body = body.decode("utf-8")
            except UnicodeDecodeError:
                return None, None

        f_req = _normalize_freq_string(body)
        if f_req is None:
            return None, None

        try:
            outer = json.loads(f_req)
        except (json.JSONDecodeError, ValueError, TypeError):
            return f_req, None

        # Streaming-chat envelope: [null, "<inner_json>"].
        if (
            isinstance(outer, list)
            and len(outer) >= 2
            and outer[0] is None
            and isinstance(outer[1], str)
        ):
            try:
                params = json.loads(outer[1])
            except (json.JSONDecodeError, ValueError, TypeError):
                return f_req, None
            if not isinstance(params, list):
                return f_req, None
            return f_req, ("chat", params)

        # Batchexecute envelope: [[[rpc_id, args_json, ..., ...]]].
        if isinstance(outer, list) and len(outer) >= 1 and isinstance(outer[0], list):
            # Decode the inner ``args_json`` string slot (index 1 of each
            # [rpc_id, args_json, ...] triple/quad) into structured form so
            # volatile-key stripping can reach inside.
            try:
                decoded_outer: list[Any] = []
                for batch in outer:
                    if not isinstance(batch, list):
                        decoded_outer.append(batch)
                        continue
                    decoded_batch: list[Any] = []
                    for entry in batch:
                        if (
                            isinstance(entry, list)
                            and len(entry) >= 2
                            and isinstance(entry[1], str)
                        ):
                            try:
                                inner_args = json.loads(entry[1])
                                decoded_batch.append([entry[0], inner_args, *entry[2:]])
                            except (json.JSONDecodeError, ValueError, TypeError):
                                # Inner args wasn't JSON — keep the raw string
                                # so equality still distinguishes different
                                # arg payloads.
                                decoded_batch.append(entry)
                        else:
                            decoded_batch.append(entry)
                    decoded_outer.append(decoded_batch)
                return f_req, ("batch", _normalize_create_artifact_options(decoded_outer))
            except (TypeError, IndexError):
                return f_req, None

        # Unknown envelope shape — defer to raw string compare.
        return f_req, ("raw", f_req)

    raw1, payload1 = _decode_freq_envelope(r1)
    raw2, payload2 = _decode_freq_envelope(r2)

    # If neither side carries ``f.req`` at all, defer to other matchers.
    if raw1 is None and raw2 is None:
        return True
    # Exactly one carries ``f.req`` — structurally different.
    if raw1 is None or raw2 is None:
        return False

    # Both sides have ``f.req``. If either failed to parse, fall back to a
    # normalized string compare on the raw value.
    if payload1 is None or payload2 is None:
        return raw1 == raw2

    kind1, data1 = payload1
    kind2, data2 = payload2

    # Mismatched envelope shape — structurally different.
    if kind1 != kind2:
        return False

    if kind1 == "chat":
        # Streaming-chat: drop the volatile conversation_id at slot 4 and
        # compare the rest of the param list with non-UUID leaves intact.
        # The chat path keeps non-UUID strings (note titles, questions,
        # notebook_id at slot 7) so the existing
        # ``test_freq_matcher_notebook_id_mismatch_at_slot_seven`` contract
        # holds — only UUIDs and empty strings normalize via
        # ``_strip_volatile``/``_normalize_uuids``, and volatile dict keys
        # are stripped from any nested dicts.
        if len(data1) != len(data2):
            return False
        # Slot 4 (conversation_id) is the existing exemption — replace both
        # sides with a shared sentinel so the comparison ignores it without
        # relying on the leaf normalization (the recorded conv_id and the
        # replay's are both UUIDs that would normalize identically anyway,
        # but the sentinel makes the slot-4 exemption explicit in the code).
        SENTINEL = object()
        c1 = list(data1)
        c2 = list(data2)
        if len(c1) >= 5:
            c1[4] = SENTINEL
            c2[4] = SENTINEL
        return _strip_volatile(c1) == _strip_volatile(c2)

    if kind1 == "batch":
        # Batchexecute: structural-skeleton comparison via :func:`_shape_only`.
        # Catches the regression class P1-3 targets — different arg counts,
        # different nesting depths, missing or extra positional slots, missing
        # or extra non-volatile dict keys — while staying robust against leaf
        # drift between recording sessions: different fixture UUIDs, different
        # free-form text, ``null`` vs filled in the same slot, different page
        # sizes all match because every leaf collapses to ``_LEAF``. RPC id
        # equality is already enforced by the URL ``rpcids`` matcher, so
        # collapsing the RPC id leaf here is intentional. See the
        # :func:`_shape_only` docstring for the full trade-off rationale.
        return _shape_only(data1) == _shape_only(data2)

    # ``raw`` kind: fall back to raw string compare.
    return raw1 == raw2


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
    # Match requests by method and path, plus body-level disambiguators:
    #   - ``rpcids`` disambiguates batchexecute POSTs by their query-string
    #     ``rpcids`` parameter (all batchexecute POSTs share the URL path).
    #   - ``freq`` disambiguates by the decoded form-body ``f.req`` payload.
    #     It is a no-op when neither side has ``f.req`` (defers to the other
    #     matchers), so it is safe to enable globally. When it does parse, it
    #     catches artifact-ID / notebook-ID / source-ID drift between record
    #     and replay that ``rpcids`` alone cannot see (e.g. two ``gArtLc``
    #     POSTs against different artifact UUIDs share the same rpcids value
    #     but carry different ``f.req`` bodies).
    match_on=["method", "scheme", "host", "port", "path", "rpcids", "freq"],
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
# ``freq`` is wired into the default ``match_on`` tuple above. The matcher
# returns ``True`` (defer to other matchers) when neither request carries an
# ``f.req`` field, so endpoints that don't use ``f.req`` are unaffected; when
# both sides carry ``f.req``, the matcher decodes the form-body envelope and
# compares the normalized structure (with volatile keys like ``timestamp`` /
# ``requestId`` stripped).
notebooklm_vcr.register_matcher("freq", _freq_body_matcher)
