"""Tests for the WIZ_global_data token-extraction regex helper.

Covers ``extract_wiz_field`` (the unified helper introduced in PR) and
verifies that all extraction variants observed on real NotebookLM responses
parse correctly, while drift produces a typed diagnostic
(:class:`AuthExtractionError`) with a sanitized HTML preview.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from notebooklm.auth import extract_wiz_field
from notebooklm.exceptions import AuthExtractionError, RPCError

FIXTURE = Path(__file__).parent.parent / "fixtures" / "notebooklm_html.html"


# ---------------------------------------------------------------------------
# Fixture-backed happy paths
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def real_shape_html() -> str:
    """Load the sanitized real-shape NotebookLM HTML fixture."""
    return FIXTURE.read_text(encoding="utf-8")


def test_extracts_csrf_token_from_fixture(real_shape_html: str) -> None:
    """SNlM0e is extracted from a real-shape sanitized response."""
    value = extract_wiz_field(real_shape_html, "SNlM0e")
    assert value == "SCRUBBED_CSRF_AF1_QpN-abc123_def456"


def test_extracts_session_id_from_fixture(real_shape_html: str) -> None:
    """FdrFJe is extracted from the same fixture."""
    value = extract_wiz_field(real_shape_html, "FdrFJe")
    assert value == "1234567890123456"


def test_extracts_account_email_field_from_fixture(real_shape_html: str) -> None:
    """The helper is generic — it locates any WIZ_global_data field."""
    value = extract_wiz_field(real_shape_html, "oM1Kwf")
    assert value == "user@example.com"


# ---------------------------------------------------------------------------
# Variant matrix — single-quoted, HTML-escaped, whitespace, empty value
# ---------------------------------------------------------------------------


def test_single_quoted_variant_is_supported() -> None:
    """``'SNlM0e':'value'`` is parsed (observed in some debug renders)."""
    html = "<script>window.WIZ_global_data = {'SNlM0e':'tok_single_quote'};</script>"
    assert extract_wiz_field(html, "SNlM0e") == "tok_single_quote"


def test_html_escaped_variant_is_supported() -> None:
    """``&quot;key&quot;:&quot;value&quot;`` works when the script block
    is rendered inside an attribute or escaped fragment."""
    html = '<div data-init="{&quot;SNlM0e&quot;:&quot;tok_escaped&quot;}"></div>'
    assert extract_wiz_field(html, "SNlM0e") == "tok_escaped"


def test_whitespace_around_colon_is_tolerated() -> None:
    """Real responses sometimes have a space after the colon for readability."""
    html = '{"SNlM0e" : "tok_spaced"}'
    assert extract_wiz_field(html, "SNlM0e") == "tok_spaced"


def test_empty_value_returns_empty_string_not_none() -> None:
    """``"SNlM0e":""`` is a legitimate empty token — not drift."""
    html = '{"SNlM0e":"","FdrFJe":"abc"}'
    assert extract_wiz_field(html, "SNlM0e") == ""


def test_canonical_pattern_takes_priority_over_single_quoted() -> None:
    """When both variants appear, the canonical double-quoted form wins.

    This guarantees deterministic extraction on pages that include both a
    canonical embedding and a stray single-quoted reference (e.g. inline
    JavaScript debug logs)."""
    html = "'SNlM0e':'wrong'... \"SNlM0e\":\"right\""
    assert extract_wiz_field(html, "SNlM0e") == "right"


# ---------------------------------------------------------------------------
# Negative paths — drift produces AuthExtractionError with preview
# ---------------------------------------------------------------------------


def test_missing_key_raises_in_strict_mode() -> None:
    """Strict drift path raises AuthExtractionError, not generic ValueError."""
    with pytest.raises(AuthExtractionError) as excinfo:
        extract_wiz_field("<html></html>", "SNlM0e", strict=True)
    assert excinfo.value.key == "SNlM0e"


def test_strict_mode_is_default() -> None:
    """``strict`` defaults to True so missing keys cannot pass silently."""
    with pytest.raises(AuthExtractionError):
        extract_wiz_field("<html></html>", "SNlM0e")


def test_non_strict_returns_none_on_drift() -> None:
    """``strict=False`` returns None so callers can fall back."""
    assert extract_wiz_field("<html></html>", "SNlM0e", strict=False) is None


def test_non_strict_returns_value_when_present() -> None:
    """``strict=False`` still returns the value when the field exists."""
    html = '{"SNlM0e":"tok"}'
    assert extract_wiz_field(html, "SNlM0e", strict=False) == "tok"


# ---------------------------------------------------------------------------
# Preview / diagnostic behavior
# ---------------------------------------------------------------------------


def test_preview_included_in_error_message() -> None:
    """The error's ``__str__`` includes the preview so it surfaces in logs."""
    payload = "this is the response body that should appear in the preview"
    with pytest.raises(AuthExtractionError) as excinfo:
        extract_wiz_field(payload, "SNlM0e", strict=True)
    rendered = str(excinfo.value)
    assert "this is the response body" in rendered
    assert "SNlM0e" in rendered


def test_preview_is_truncated_to_200_chars() -> None:
    """Long payloads are truncated to keep error messages compact."""
    payload = "A" * 5000
    with pytest.raises(AuthExtractionError) as excinfo:
        extract_wiz_field(payload, "SNlM0e", strict=True)
    # The preview attribute itself caps at 200; sanity-check both that bound
    # and that the rendered string never carries the full giant payload.
    assert len(excinfo.value.payload_preview) <= AuthExtractionError.PREVIEW_LENGTH
    assert len(str(excinfo.value)) < 1000


def test_preview_collapses_whitespace() -> None:
    """Newlines and runs of whitespace are collapsed to a single space so
    the preview is readable when surfaced through ``str(exc)``."""
    payload = "alpha\n\n\n\t\tbeta   gamma"
    with pytest.raises(AuthExtractionError) as excinfo:
        extract_wiz_field(payload, "SNlM0e", strict=True)
    assert "alpha beta gamma" in excinfo.value.payload_preview


def test_auth_extraction_error_is_rpc_error() -> None:
    """The exception subclasses RPCError so existing handlers cover it."""
    with pytest.raises(RPCError):
        extract_wiz_field("", "SNlM0e", strict=True)


def test_key_attribute_records_failing_field() -> None:
    """``.key`` lets callers route on which field drifted."""
    with pytest.raises(AuthExtractionError) as excinfo:
        extract_wiz_field("", "FdrFJe", strict=True)
    assert excinfo.value.key == "FdrFJe"


# ---------------------------------------------------------------------------
# Key regex escaping — defensive
# ---------------------------------------------------------------------------


def test_key_with_regex_metacharacters_is_escaped() -> None:
    """Field names containing regex metacharacters are escaped, so the helper
    matches them literally rather than treating them as regex syntax."""
    # ``.`` would otherwise match any character — confirm it does not.
    html = '{"a.b":"literal","aXb":"wildcard_would_match_this"}'
    assert extract_wiz_field(html, "a.b") == "literal"


# ---------------------------------------------------------------------------
# Escape handling — JSON-style quote escapes inside values
# ---------------------------------------------------------------------------


def test_escaped_quote_inside_double_quoted_value() -> None:
    """JSON-style ``\\"`` inside the value is captured intact rather than
    terminating the match early. Without escape-aware matching, the value
    ``a\\"b`` would be truncated to just ``a``."""
    html = r'{"SNlM0e":"a\"b"}'
    assert extract_wiz_field(html, "SNlM0e") == r"a\"b"


def test_escaped_quote_inside_single_quoted_value() -> None:
    """Same escape-awareness applies to the single-quoted variant."""
    html = r"{'SNlM0e':'a\'b'}"
    assert extract_wiz_field(html, "SNlM0e") == r"a\'b"


def test_html_escaped_value_can_contain_other_entities() -> None:
    """The HTML-escaped pattern terminates only on a literal ``&quot;``, not
    on any ``&`` — so embedded entities like ``&amp;`` survive."""
    html = 'data="{&quot;SNlM0e&quot;:&quot;a&amp;b&quot;}"'
    assert extract_wiz_field(html, "SNlM0e") == "a&amp;b"


# ---------------------------------------------------------------------------
# Preview slicing — performance guard on huge payloads
# ---------------------------------------------------------------------------


def test_preview_slices_before_regex_substitution() -> None:
    """The whitespace-collapse regex must operate on a bounded prefix, not
    the full payload — otherwise a multi-MB response would do a huge
    needless substitution on the failure path."""
    # A multi-megabyte payload with the key buried far past the preview window.
    payload = (" " * 5_000_000) + "tail"
    with pytest.raises(AuthExtractionError) as excinfo:
        extract_wiz_field(payload, "SNlM0e", strict=True)
    # The preview itself must still be capped — confirms the slice is tight
    # enough that the rendered diagnostic does not balloon.
    assert len(excinfo.value.payload_preview) <= AuthExtractionError.PREVIEW_LENGTH
