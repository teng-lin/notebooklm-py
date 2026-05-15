"""Unit tests for ``tests/vcr_config.py`` custom matchers and scrub hooks.

Two surfaces are covered here:

1. ``_freq_body_matcher`` (T8.A2) — decodes the form-encoded ``f.req`` payload
   that streaming endpoints (notably streaming chat) use to disambiguate
   otherwise identical POSTs. See the matcher's docstring for the full
   match-rule rationale.

2. ``recompute_chunk_prefix`` + ``scrub_response`` (T8.D7) — byte-count
   re-derivation that runs AFTER ``scrub_string`` substitutes sensitive
   values. Scrubbing routinely changes payload length (e.g. a 21-digit Google
   user ID -> the 16-char placeholder ``SCRUBBED_USER_ID``), which would
   otherwise leave the chunk header lines advertising the original byte count
   and break the cassette-shape lint plus the decoder's tolerance warning.
   See ``tests/cassette_patterns.py`` for the helper and
   ``tests/vcr_config.py`` for the wiring.
"""

from __future__ import annotations

import importlib.util
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import pytest

from notebooklm._core import (
    ERROR_INJECT_ENV_VAR,
    _get_error_injection_mode,
    _SyntheticErrorTransport,
)

# Load ``tests/vcr_config.py`` via ``importlib`` rather than mutating
# ``sys.path``. The ``tests`` directory is not a package (no ``__init__.py``),
# so a plain ``from tests.vcr_config import _freq_body_matcher`` fails; a
# ``sys.path`` insertion would work but is module-load-time side-effectful and
# would silently shadow any future top-level module named ``vcr_config``.
# Loading by file path keeps the dependency localized to this test module
# (mirrors the pattern used in ``tests/unit/test_cookie_redaction.py``).
_TESTS_DIR = Path(__file__).resolve().parent.parent


def _load_by_path(module_name: str, file_name: str) -> Any:
    spec = importlib.util.spec_from_file_location(module_name, _TESTS_DIR / file_name)
    assert spec is not None and spec.loader is not None, (
        f"Could not load {file_name} from {_TESTS_DIR}"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_cassette_patterns = _load_by_path("tests_cassette_patterns", "cassette_patterns.py")
_vcr_config = _load_by_path("tests_vcr_config", "vcr_config.py")

_freq_body_matcher: Callable[[Any, Any], bool] = _vcr_config._freq_body_matcher
recompute_chunk_prefix: Callable[[str], str] = _cassette_patterns.recompute_chunk_prefix
scrub_response: Callable[[dict[str, Any]], dict[str, Any]] = _vcr_config.scrub_response


# ---------------------------------------------------------------------------
# _freq_body_matcher — A2 streaming-chat matcher tests
# ---------------------------------------------------------------------------


class _StubRequest:
    """Minimal stand-in for a ``vcr.request.Request`` with just the ``body`` attr.

    The matcher reads only ``request.body``, so we don't need any of the other
    request plumbing for these unit tests.
    """

    def __init__(self, body: Any) -> None:
        self.body = body


def _build_freq_body(params: list[Any]) -> str:
    """Build the same ``application/x-www-form-urlencoded`` body shape the
    NotebookLM streaming-chat endpoint sends.

    The wire format is ``f.req=<url-encoded JSON envelope>&at=<csrf>`` where the
    JSON envelope is ``[null, "<inner_json>"]`` and ``<inner_json>`` is itself a
    JSON-encoded list of positional parameters.
    """
    inner_json = json.dumps(params, separators=(",", ":"))
    envelope = json.dumps([None, inner_json], separators=(",", ":"))
    return f"f.req={quote(envelope, safe='')}&at=mock_csrf_token"


# Canonical 9-param shape for the streaming-chat endpoint:
#   slot 0: leading null
#   slot 1: question text
#   slot 2: null
#   slot 3: feature bitmask
#   slot 4: conversation_id  <- legitimately varies; matcher MUST ignore
#   slot 5: null
#   slot 6: null
#   slot 7: notebook_id      <- matcher MUST check
#   slot 8: trailing flag
def _nine_params(
    question: str = "What is this notebook about?",
    conv_id: str = "conv_abc",
    notebook_id: str = "nb_xyz",
) -> list[Any]:
    return [None, question, None, [2], conv_id, None, None, notebook_id, 1]


def test_freq_matcher_identical_nine_param_match() -> None:
    """Two requests with the exact same 9-param shape match."""
    params = _nine_params()
    r1 = _StubRequest(_build_freq_body(params))
    r2 = _StubRequest(_build_freq_body(params))
    assert _freq_body_matcher(r1, r2) is True


def test_freq_matcher_param_count_mismatch_nine_vs_five() -> None:
    """A 9-param request must not match a 5-param request (C3 regression)."""
    nine = _nine_params()
    five = [None, "What is this notebook about?", None, [2], "conv_abc"]
    r1 = _StubRequest(_build_freq_body(nine))
    r2 = _StubRequest(_build_freq_body(five))
    assert _freq_body_matcher(r1, r2) is False


def test_freq_matcher_notebook_id_mismatch_at_slot_seven() -> None:
    """Differing notebook_id at slot 7 must NOT match (distinct interactions)."""
    p1 = _nine_params(notebook_id="nb_alpha")
    p2 = _nine_params(notebook_id="nb_beta")
    r1 = _StubRequest(_build_freq_body(p1))
    r2 = _StubRequest(_build_freq_body(p2))
    assert _freq_body_matcher(r1, r2) is False


def test_freq_matcher_conversation_id_difference_still_matches() -> None:
    """Differing conversation_id at slot 4 DOES match — conv_id varies per replay."""
    p1 = _nine_params(conv_id="conv_recorded_at_t1")
    p2 = _nine_params(conv_id="conv_recorded_at_t2")
    r1 = _StubRequest(_build_freq_body(p1))
    r2 = _StubRequest(_build_freq_body(p2))
    assert _freq_body_matcher(r1, r2) is True


def test_freq_matcher_handles_bytes_body() -> None:
    """The matcher should transparently decode ``bytes`` request bodies.

    VCR's request.body is bytes for recorded requests, so we exercise that path
    explicitly to prevent a TypeError regression in production replay.
    """
    params = _nine_params()
    body_text = _build_freq_body(params)
    r1 = _StubRequest(body_text.encode("utf-8"))
    r2 = _StubRequest(body_text.encode("utf-8"))
    assert _freq_body_matcher(r1, r2) is True


def test_freq_matcher_both_bodies_unparseable_defers_to_other_matchers() -> None:
    """Two requests neither carrying f.req return True (defer to other matchers).

    Covers the (unlikely) case where this opt-in matcher is consulted for a
    non-streaming request. Returning True keeps VCR's other matchers
    (method/path/etc.) in charge of the decision; returning False would
    incorrectly block matches on every non-streaming request the cassette
    contains.
    """
    r1 = _StubRequest("at=foo&other=bar")
    r2 = _StubRequest("at=baz&other=qux")
    assert _freq_body_matcher(r1, r2) is True


def test_freq_matcher_one_unparseable_one_parseable_rejects() -> None:
    """A parseable f.req body must not match a body that lacks f.req.

    Structurally different requests should not be silently collapsed even when
    one side is "no f.req at all".
    """
    parseable = _StubRequest(_build_freq_body(_nine_params()))
    no_f_req = _StubRequest("at=foo&other=bar")
    assert _freq_body_matcher(parseable, no_f_req) is False
    assert _freq_body_matcher(no_f_req, parseable) is False


# ---------------------------------------------------------------------------
# recompute_chunk_prefix — D7 byte-count re-derivation direct unit tests
# ---------------------------------------------------------------------------


def _response(body: str | bytes) -> dict[str, Any]:
    """Build a minimal VCR-shaped response dict for tests."""
    return {"body": {"string": body}, "headers": {}, "status": {"code": 200}}


def test_recompute_chunk_prefix_noop_on_plain_body():
    """Non-chunked bodies (HTML, empty, plain JSON) pass through unchanged."""
    assert recompute_chunk_prefix("") == ""
    assert recompute_chunk_prefix("<html>plain</html>") == "<html>plain</html>"
    assert recompute_chunk_prefix('{"json":"object"}') == '{"json":"object"}'


def test_recompute_chunk_prefix_corrects_synthetic_shrinkage():
    """Synthetic chunk that loses N bytes after scrubbing gets a corrected prefix.

    Models the exact T8.D7 acceptance scenario: a header advertising the
    pre-scrub byte count is rewritten to match the post-scrub payload.
    """
    # Pre-scrub the JSON-wrapped payload was 21 bytes (e.g.
    # ``[["long_user_id_xyz"]]`` is 21 chars/bytes) so the header said ``21``.
    # After scrub the payload is ``[["SCRUBBED_USER_ID"]]`` which is 22 bytes
    # (the JSON brackets and quotes count, not just the inner 16-char
    # placeholder). The helper must rewrite ``21`` -> ``22`` accordingly.
    stale = '21\n[["SCRUBBED_USER_ID"]]'
    rewritten = recompute_chunk_prefix(stale)
    expected_len = len('[["SCRUBBED_USER_ID"]]')  # == 22
    assert rewritten == f"{expected_len}\n" + '[["SCRUBBED_USER_ID"]]'


def test_recompute_chunk_prefix_is_idempotent():
    """Running the helper twice yields the same string (no creeping drift)."""
    body = '7\n[["x"]]\n3\nfoo\n'
    once = recompute_chunk_prefix(body)
    twice = recompute_chunk_prefix(once)
    assert once == twice


def test_recompute_chunk_prefix_preserves_xssi_prefix():
    """The ``)]}'\\n\\n`` XSSI marker is retained verbatim."""
    body = ")]}'\n\n5\nhello\n"
    rewritten = recompute_chunk_prefix(body)
    assert rewritten.startswith(")]}'\n\n")
    # Header for "hello" should be 5 (already correct, helper is idempotent).
    assert rewritten == ")]}'\n\n5\nhello\n"


def test_recompute_chunk_prefix_rewrites_multi_chunk_body():
    """All header lines in a multi-chunk body get individually re-derived."""
    # Chunk 1 advertises 99 but payload is 7 bytes; chunk 2 advertises 99 but
    # payload is 3 bytes. Both should be corrected independently.
    body = ')]}\'\n\n99\n["abc"]\n99\nfoo\n'
    rewritten = recompute_chunk_prefix(body)
    assert rewritten == ')]}\'\n\n7\n["abc"]\n3\nfoo\n'


def test_recompute_chunk_prefix_uses_utf8_byte_count():
    """Byte count is the UTF-8 byte length, not the character count.

    For non-ASCII payloads (emoji, accented characters) ``len(payload)`` differs
    from ``len(payload.encode("utf-8"))``. The on-wire protocol uses byte count,
    so the helper must too — matching what the decoder computes at
    ``decoder.py:228``.
    """
    # The emoji takes 4 UTF-8 bytes but is 1 Python char.
    payload = '["🚀"]'  # len() == 5, len(.encode()) == 8
    body = f"99\n{payload}"
    rewritten = recompute_chunk_prefix(body)
    expected = f"{len(payload.encode('utf-8'))}\n{payload}"
    assert rewritten == expected


def test_recompute_chunk_prefix_leaves_dangling_header_untouched():
    """A digit-only trailing line with no payload after it is preserved as-is.

    Without this guard the helper might rewrite a stray sentinel to ``0``,
    silently corrupting a recorded body that contains a digit-only final line
    for some other reason.
    """
    body = "5\nhello\n42"
    rewritten = recompute_chunk_prefix(body)
    # First header rewritten (already correct: 5 == len("hello"), but the
    # idempotence path also passes through). The trailing "42" has no payload
    # and must be left alone.
    assert rewritten.endswith("\n42")


def test_recompute_chunk_prefix_skips_consecutive_digit_lines():
    """Two digit-only lines in a row: the second is NOT treated as a payload.

    Defends against an edge case where a malformed body has multiple stacked
    headers; we'd rather leave it untouched than guess.
    """
    body = "10\n20\nhello"
    rewritten = recompute_chunk_prefix(body)
    # "10" is followed by "20" (another digit-only line) -> not rewritten.
    # "20" is followed by "hello" -> rewritten to 5.
    assert rewritten == "10\n5\nhello"


def test_recompute_chunk_prefix_payload_containing_digits_is_treated_as_payload():
    """A payload that *contains* digits but is not digit-only is rewritten correctly.

    Guards against an over-eager ``\\d+`` regex: the header detector uses
    ``\\A\\d+\\Z`` anchors so payloads like ``["123"]`` (digits surrounded by
    JSON punctuation) are recognized as non-header content and trigger the
    rewrite path. Surfaced by gemini-code-assist review of the T8.D7 patch.
    """
    body = '99\n["123","abc"]\n'
    rewritten = recompute_chunk_prefix(body)
    expected_payload = '["123","abc"]'
    assert rewritten == f"{len(expected_payload.encode('utf-8'))}\n{expected_payload}\n"


# ---------------------------------------------------------------------------
# scrub_response integration — the full scrub + re-derive pipeline
# ---------------------------------------------------------------------------


def test_scrub_response_rederives_chunk_count_after_string_substitution():
    """End-to-end: scrub_response on a chunked body with a sensitive value
    leaves the cassette with a self-consistent byte-count header.

    Uses the ``at=<csrf>`` -> ``at=SCRUBBED_CSRF`` substitution because that is
    one of the most commonly-fired replacements and is purely ASCII.
    """
    # Pre-scrub payload: 27 chars. Post-scrub: substitution shortens it.
    payload_in = '[["X","at=real_token_xyz123"]]'
    body_in = f"99\n{payload_in}"
    resp = _response(body_in)

    scrub_response(resp)

    rewritten = resp["body"]["string"]
    # The CSRF substitution will have fired; the new header must equal the new
    # payload length (UTF-8 byte count, here equal to char count for ASCII).
    header_str, _, payload_out = rewritten.partition("\n")
    assert payload_out  # scrubbing did not eat the payload
    assert int(header_str) == len(payload_out.encode("utf-8"))
    # And the sensitive token was actually scrubbed.
    assert "real_token_xyz123" not in rewritten
    assert "SCRUBBED_CSRF" in rewritten


def test_scrub_response_rederives_chunk_count_for_bytes_body():
    """The bytes-body code path also re-derives byte counts."""
    payload_in = '[["X","at=real_token_xyz123"]]'
    body_bytes = f"99\n{payload_in}".encode()
    resp = _response(body_bytes)

    scrub_response(resp)

    out = resp["body"]["string"]
    assert isinstance(out, bytes)
    decoded = out.decode("utf-8")
    header_str, _, payload_out = decoded.partition("\n")
    assert int(header_str) == len(payload_out.encode("utf-8"))
    assert b"real_token_xyz123" not in out
    assert b"SCRUBBED_CSRF" in out


def test_scrub_response_does_not_corrupt_non_chunked_html_body():
    """Bodies that aren't chunked (e.g. HTML login pages) are unaffected by
    the re-derivation step."""
    body_in = "<html><body>SID=real_sid_value</body></html>"
    resp = _response(body_in)

    scrub_response(resp)

    out = resp["body"]["string"]
    assert "real_sid_value" not in out
    assert "SCRUBBED" in out
    # Structure preserved: no spurious digit prefix introduced.
    assert out.startswith("<html>")


# ---------------------------------------------------------------------------
# T8.E10 — synthetic-error plumbing tests
# ---------------------------------------------------------------------------
#
# Three layers of coverage:
#
# 1. The cassette-patterns response builders return the right shapes for each
#    valid mode and raise on invalid mode.
# 2. The ``before_record_response`` hook in vcr_config.py performs a
#    defense-in-depth substitution when the env var is set, and is a no-op
#    when unset.
# 3. The ``_core.py`` transport wrapper substitutes the FIRST batchexecute
#    POST without touching non-batchexecute traffic, and is only constructed
#    when the env var resolves to a valid mode.

build_synthetic_error_response = _cassette_patterns.build_synthetic_error_response
synthetic_error_cassette_name = _cassette_patterns.synthetic_error_cassette_name
SYNTHETIC_ERROR_CASSETTE_PREFIX = _cassette_patterns.SYNTHETIC_ERROR_CASSETTE_PREFIX
VALID_ERROR_MODES = _cassette_patterns.VALID_ERROR_MODES
get_error_injection_mode_vcr = _vcr_config.get_error_injection_mode


# --- (1) response builder + filename helper ---------------------------------


@pytest.mark.parametrize(
    "mode,expected_status",
    [
        ("429", 429),
        ("5xx", 500),
        ("expired_csrf", 400),
    ],
)
def test_build_synthetic_error_response_status_codes(mode, expected_status):
    """Each mode returns the status code its corresponding exception path keys on.

    - 429 -> RateLimitError via the 429 retry budget exhausted path.
    - 500 -> ServerError via the 5xx retry budget exhausted path.
    - 400 -> ``is_auth_error`` treats 400/401/403 as auth-refresh triggers
      (NotebookLM returns 400 for expired CSRF, not 401/403).
    """
    status, body, headers = build_synthetic_error_response(mode)
    assert status == expected_status
    assert isinstance(body, bytes)
    assert body, "body must be non-empty"
    assert headers["Content-Type"].startswith("application/json")


def test_build_synthetic_error_response_429_has_retry_after():
    """The 429 shape carries a Retry-After header so the client's parser sees
    a numeric hint to consume (parsed via ``_parse_retry_after``)."""
    _, _, headers = build_synthetic_error_response("429")
    assert "Retry-After" in headers
    # Integer-seconds form so it round-trips through _parse_retry_after.
    assert headers["Retry-After"].isdigit()


def test_build_synthetic_error_response_invalid_mode():
    with pytest.raises(ValueError, match="Unknown synthetic error mode"):
        build_synthetic_error_response("418")


@pytest.mark.parametrize("mode", list(VALID_ERROR_MODES))
def test_synthetic_error_cassette_name_prefix(mode):
    """Cassette filenames generated through this plumbing must carry the
    canonical ``error_synthetic_`` prefix so a reader of tests/cassettes/ can
    tell them apart from real recordings at a glance."""
    name = synthetic_error_cassette_name(mode, "list_notebooks")
    assert name.startswith(SYNTHETIC_ERROR_CASSETTE_PREFIX)
    assert mode in name
    assert name.endswith(".yaml")


def test_synthetic_error_cassette_name_rejects_unknown_mode():
    with pytest.raises(ValueError):
        synthetic_error_cassette_name("teapot", "list_notebooks")


# --- (2) before_record_response substitution --------------------------------


def test_vcr_get_error_injection_mode_unset(monkeypatch):
    monkeypatch.delenv(ERROR_INJECT_ENV_VAR, raising=False)
    assert get_error_injection_mode_vcr() is None


@pytest.mark.parametrize("mode", ["429", "5xx", "expired_csrf"])
def test_vcr_get_error_injection_mode_valid(monkeypatch, mode):
    monkeypatch.setenv(ERROR_INJECT_ENV_VAR, mode)
    assert get_error_injection_mode_vcr() == mode


def test_vcr_get_error_injection_mode_case_insensitive(monkeypatch):
    monkeypatch.setenv(ERROR_INJECT_ENV_VAR, "5XX")
    assert get_error_injection_mode_vcr() == "5xx"


def test_vcr_get_error_injection_mode_typo_returns_none(monkeypatch):
    """A typo'd mode does not crash — it resolves to ``None`` so the rest of
    the plumbing acts as if the env var were unset. The unit tests catch
    typos via this same helper."""
    monkeypatch.setenv(ERROR_INJECT_ENV_VAR, "ratelimit")
    assert get_error_injection_mode_vcr() is None


@pytest.mark.parametrize("mode", ["429", "5xx", "expired_csrf"])
def test_scrub_response_substitutes_when_env_var_set(monkeypatch, mode):
    """When the env var resolves to a valid mode, ``scrub_response`` rewrites
    the response shape to the canonical synthetic body, regardless of what
    came in. This is the defense-in-depth layer below the transport wrapper."""
    monkeypatch.setenv(ERROR_INJECT_ENV_VAR, mode)
    incoming = {
        "status": {"code": 200, "message": "OK"},
        "headers": {"Content-Type": ["text/plain"]},
        "body": {"string": b"original wire response"},
    }
    out = scrub_response(incoming)
    expected_status, expected_body, _ = build_synthetic_error_response(mode)
    assert out["status"]["code"] == expected_status
    # Byte-for-byte equality — synthetic bodies never trigger scrub patterns or
    # chunk-prefix rewrites, so any mutation downstream is a regression.
    assert out["body"]["string"] == expected_body
    # Content-Type was overlaid with the synthetic value.
    assert any("json" in v.lower() for v in out["headers"]["Content-Type"])


def test_scrub_response_noop_when_env_var_unset(monkeypatch):
    """With the env var absent, ``scrub_response`` is byte-for-byte the same
    as before T8.E10 landed — only sensitive-data scrubbing runs."""
    monkeypatch.delenv(ERROR_INJECT_ENV_VAR, raising=False)
    incoming = {
        "status": {"code": 200, "message": "OK"},
        "headers": {"Content-Type": ["text/plain"]},
        "body": {"string": b"original wire response"},
    }
    out = scrub_response(incoming)
    # Substitution did NOT fire — status is preserved.
    assert out["status"]["code"] == 200
    # The body went through the scrub pipeline but the original payload is
    # still recognizable.
    assert b"original wire response" in out["body"]["string"]


# --- (3) _core.py transport wrapper -----------------------------------------


def test_core_get_error_injection_mode_unset(monkeypatch):
    monkeypatch.delenv(ERROR_INJECT_ENV_VAR, raising=False)
    assert _get_error_injection_mode() is None


@pytest.mark.parametrize("mode", ["429", "5xx", "expired_csrf"])
def test_core_get_error_injection_mode_valid(monkeypatch, mode):
    monkeypatch.setenv(ERROR_INJECT_ENV_VAR, mode)
    assert _get_error_injection_mode() == mode


def test_core_get_error_injection_mode_case_insensitive(monkeypatch):
    monkeypatch.setenv(ERROR_INJECT_ENV_VAR, "EXPIRED_CSRF")
    assert _get_error_injection_mode() == "expired_csrf"


def test_core_get_error_injection_mode_typo_returns_none(monkeypatch):
    """A typo resolves to ``None`` rather than crashing — recording runs
    should fail open so an operator can fix the typo without losing their
    in-flight cassette session."""
    monkeypatch.setenv(ERROR_INJECT_ENV_VAR, "ratelimit")
    assert _get_error_injection_mode() is None


class _RecordingInnerTransport(httpx.AsyncBaseTransport):
    """Inner transport that records calls so we can assert pass-through."""

    def __init__(self):
        self.calls: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        return httpx.Response(200, content=b"inner-response", request=request)

    async def aclose(self) -> None:
        return None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,expected_status",
    [("429", 429), ("5xx", 500), ("expired_csrf", 400)],
)
async def test_synthetic_transport_substitutes_batchexecute(mode, expected_status):
    """The wrapped transport returns a synthetic response for batchexecute
    POSTs without ever forwarding them to the inner transport."""
    inner = _RecordingInnerTransport()
    wrapper = _SyntheticErrorTransport(mode, inner)
    async with httpx.AsyncClient(transport=wrapper) as client:
        resp = await client.post(
            "https://notebooklm.google.com/_/LabsTailwindUi/data/batchexecute",
            content=b"f.req=...",
        )
    assert resp.status_code == expected_status
    assert resp.content  # synthetic body present
    assert inner.calls == []  # batchexecute did NOT pass through


@pytest.mark.asyncio
async def test_synthetic_transport_passes_non_batchexecute_through():
    """Non-batchexecute traffic (Scotty uploads, RotateCookies pokes, the
    homepage GET) MUST pass through unchanged — error-shape cassettes only
    target batchexecute RPCs."""
    inner = _RecordingInnerTransport()
    wrapper = _SyntheticErrorTransport("429", inner)
    async with httpx.AsyncClient(transport=wrapper) as client:
        resp = await client.get("https://notebooklm.google.com/")
    assert resp.status_code == 200
    assert resp.content == b"inner-response"
    assert len(inner.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS", "PUT", "DELETE"])
async def test_synthetic_transport_only_substitutes_post_on_batchexecute(method):
    """Even on the batchexecute path, only POST requests are substituted.

    Non-POST methods on /batchexecute (a hypothetical GET probe, an OPTIONS
    preflight, etc.) are out of scope for error-shape cassettes and must
    pass through unchanged. Closes CodeRabbit feedback on PR #638.
    """
    inner = _RecordingInnerTransport()
    wrapper = _SyntheticErrorTransport("429", inner)
    async with httpx.AsyncClient(transport=wrapper) as client:
        resp = await client.request(
            method,
            "https://notebooklm.google.com/_/LabsTailwindUi/data/batchexecute",
        )
    assert resp.status_code == 200
    assert resp.content == b"inner-response"
    assert len(inner.calls) == 1
    # ``_fired`` must NOT have been flipped by a non-POST request.
    assert wrapper._fired is False


@pytest.mark.asyncio
async def test_synthetic_transport_substitutes_every_batchexecute_call():
    """With ``always=True`` (the default), every batchexecute POST gets the
    synthetic response — important because the client's auth-refresh path
    retries the same RPC and both retries must see the synthetic shape."""
    inner = _RecordingInnerTransport()
    wrapper = _SyntheticErrorTransport("5xx", inner, always=True)
    async with httpx.AsyncClient(transport=wrapper) as client:
        for _ in range(3):
            resp = await client.post(
                "https://notebooklm.google.com/_/LabsTailwindUi/data/batchexecute",
                content=b"f.req=...",
            )
            assert resp.status_code == 500
    assert inner.calls == []


@pytest.mark.asyncio
async def test_synthetic_transport_always_false_fires_once():
    """``always=False`` substitutes only the FIRST batchexecute POST; later
    POSTs fall through to the inner transport. Useful for tests that want
    to assert the client recovers after a single transient failure."""
    inner = _RecordingInnerTransport()
    wrapper = _SyntheticErrorTransport("5xx", inner, always=False)
    async with httpx.AsyncClient(transport=wrapper) as client:
        first = await client.post(
            "https://notebooklm.google.com/_/LabsTailwindUi/data/batchexecute",
            content=b"f.req=...",
        )
        second = await client.post(
            "https://notebooklm.google.com/_/LabsTailwindUi/data/batchexecute",
            content=b"f.req=...",
        )
    assert first.status_code == 500
    assert second.status_code == 200  # passed through
    assert len(inner.calls) == 1


@pytest.mark.asyncio
async def test_synthetic_transport_no_op_when_env_var_unset_in_clientcore(
    monkeypatch,
):
    """End-to-end: when ``NOTEBOOKLM_VCR_RECORD_ERRORS`` is unset, the
    ``ClientCore.open()`` path constructs an ``httpx.AsyncClient`` WITHOUT
    the synthetic transport wrapper — production behavior unchanged.

    We don't actually call the network; we just inspect the underlying
    transport stack after ``open()`` to confirm no synthetic wrapper is in
    place. Uses a minimal ``AuthTokens`` instance to construct ``ClientCore``.
    """
    monkeypatch.delenv(ERROR_INJECT_ENV_VAR, raising=False)
    from notebooklm._core import ClientCore
    from notebooklm.auth import AuthTokens

    auth = AuthTokens(cookies={"SID": "t"}, csrf_token="c", session_id="s")
    core = ClientCore(auth)
    try:
        await core.open()
        assert core._http_client is not None
        # When unset, the AsyncClient is built without our wrapper. The
        # default httpx transport is some private class; we just assert
        # ours isn't in the chain.
        transport = core._http_client._transport
        assert not isinstance(transport, _SyntheticErrorTransport)
    finally:
        if core._http_client is not None:
            await core._http_client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["429", "5xx", "expired_csrf"])
async def test_synthetic_transport_wired_when_env_var_set_in_clientcore(monkeypatch, mode):
    """End-to-end: when the env var resolves to a valid mode,
    ``ClientCore.open()`` builds the AsyncClient with the synthetic wrapper
    in place. The transport's ``_mode`` attribute reflects the env var."""
    monkeypatch.setenv(ERROR_INJECT_ENV_VAR, mode)
    from notebooklm._core import ClientCore
    from notebooklm.auth import AuthTokens

    auth = AuthTokens(cookies={"SID": "t"}, csrf_token="c", session_id="s")
    core = ClientCore(auth)
    try:
        await core.open()
        assert core._http_client is not None
        transport = core._http_client._transport
        assert isinstance(transport, _SyntheticErrorTransport)
        assert transport._mode == mode
    finally:
        if core._http_client is not None:
            await core._http_client.aclose()


# --- (4) lazy-import resolution of the builder ------------------------------


@pytest.mark.asyncio
async def test_synthetic_transport_lazy_loads_builder():
    """The transport defers importing tests/cassette_patterns.py until the
    first batchexecute POST fires. This keeps the production module
    free of test-tree dependencies at import time."""
    inner = _RecordingInnerTransport()
    wrapper = _SyntheticErrorTransport("429", inner)
    assert wrapper._builder is None  # not yet resolved
    async with httpx.AsyncClient(transport=wrapper) as client:
        await client.post(
            "https://notebooklm.google.com/_/LabsTailwindUi/data/batchexecute",
            content=b"f.req=...",
        )
    assert wrapper._builder is not None  # resolved after first use


# --- (5) marker plumbing in tests/conftest.py --------------------------------


@pytest.mark.synthetic_error("429")
def test_synthetic_error_marker_sets_env_var():
    """The autouse fixture in tests/conftest.py applies the env var when the
    marker is present. The teardown reverts it via monkeypatch's standard
    cleanup."""
    assert os.environ.get(ERROR_INJECT_ENV_VAR) == "429"
    assert _get_error_injection_mode() == "429"


def test_synthetic_error_marker_absent_leaves_env_alone(monkeypatch):
    """Without the marker, the env var is whatever pytest's environment had
    when the test started — the fixture is a strict no-op."""
    monkeypatch.delenv(ERROR_INJECT_ENV_VAR, raising=False)
    assert _get_error_injection_mode() is None
