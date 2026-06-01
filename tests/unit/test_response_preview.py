"""Tests for the response-preview truncation helper used in RPC errors.

The library truncates raw RPC response bodies in error contexts so they are
safe to display in logs / CLI output. ``NOTEBOOKLM_DEBUG=1`` is the opt-in to
preserve the full body for deep debugging.
"""

from __future__ import annotations

import pytest

from notebooklm.exceptions import RPCError, _truncate_response_preview


class TestTruncateResponsePreview:
    """Behavior of the ``_truncate_response_preview`` helper."""

    def test_long_body_default_truncates_to_80_plus_ellipsis(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Long body with no NOTEBOOKLM_DEBUG env returns 80 chars + '...'."""
        monkeypatch.delenv("NOTEBOOKLM_DEBUG", raising=False)
        body = "x" * 200
        result = _truncate_response_preview(body)
        assert result is not None
        assert result.endswith("...")
        # Exactly 80 chars before the "..." suffix.
        assert result[:-3] == "x" * 80
        assert len(result) == 83

    def test_debug_env_one_returns_full_body(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """NOTEBOOKLM_DEBUG=1 disables truncation entirely."""
        monkeypatch.setenv("NOTEBOOKLM_DEBUG", "1")
        body = "y" * 500
        result = _truncate_response_preview(body)
        assert result == body
        assert len(result) == 500  # type: ignore[arg-type]

    def test_debug_env_zero_still_truncates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """NOTEBOOKLM_DEBUG=0 (explicit zero) must NOT disable truncation.

        Common env-var footgun: users sometimes set ``=0`` expecting it to mean
        "off", but the contract is strictly ``=1`` to opt-in. Guard against
        regression where any truthy-looking string disables truncation.
        """
        monkeypatch.setenv("NOTEBOOKLM_DEBUG", "0")
        body = "z" * 200
        result = _truncate_response_preview(body)
        assert result is not None
        assert result == "z" * 80 + "..."

    def test_short_body_unchanged_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Bodies <= 80 chars are returned unchanged when env is unset."""
        monkeypatch.delenv("NOTEBOOKLM_DEBUG", raising=False)
        body = "short body"
        assert _truncate_response_preview(body) == body

    def test_short_body_unchanged_with_debug(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Bodies <= 80 chars are returned unchanged even with debug enabled."""
        monkeypatch.setenv("NOTEBOOKLM_DEBUG", "1")
        body = "short body"
        assert _truncate_response_preview(body) == body

    def test_exactly_80_chars_unchanged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Boundary: a body of exactly 80 chars is not truncated."""
        monkeypatch.delenv("NOTEBOOKLM_DEBUG", raising=False)
        body = "a" * 80
        assert _truncate_response_preview(body) == body

    def test_none_input_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """None input short-circuits to None regardless of env."""
        monkeypatch.delenv("NOTEBOOKLM_DEBUG", raising=False)
        assert _truncate_response_preview(None) is None

        monkeypatch.setenv("NOTEBOOKLM_DEBUG", "1")
        assert _truncate_response_preview(None) is None


class TestRPCErrorIntegration:
    """End-to-end: constructing an RPCError truncates the stored body."""

    def test_rpc_error_truncates_raw_response(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """RPCError.raw_response is truncated to 80 chars + '...' by default."""
        monkeypatch.delenv("NOTEBOOKLM_DEBUG", raising=False)
        long_body = "Q" * 300
        err = RPCError("boom", raw_response=long_body)
        assert err.raw_response is not None
        assert len(err.raw_response) == 83
        assert err.raw_response.endswith("...")
        assert err.raw_response[:-3] == "Q" * 80

    def test_rpc_error_full_body_with_debug(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """NOTEBOOKLM_DEBUG=1 preserves the full body on RPCError."""
        monkeypatch.setenv("NOTEBOOKLM_DEBUG", "1")
        long_body = "Q" * 300
        err = RPCError("boom", raw_response=long_body)
        assert err.raw_response == long_body
