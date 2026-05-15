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
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote

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
