"""Tests for centralized CLI error handling."""

import json
from pathlib import Path

import pytest

import notebooklm.cli._encoding as encoding_module
from notebooklm.cli.error_handler import _output_error, handle_errors
from notebooklm.exceptions import (
    AuthError,
    ConfigurationError,
    NetworkError,
    NotebookLimitError,
    RateLimitError,
    RPCError,
    ValidationError,
)


class TestHandleErrorsExitCodes:
    """Test that exceptions produce correct exit codes."""

    def test_validation_error_exits_with_code_1(self):
        """ValidationError should exit with code 1 (user error)."""
        with pytest.raises(SystemExit) as exc_info, handle_errors():
            raise ValidationError("Invalid input")
        assert exc_info.value.code == 1

    def test_auth_error_exits_with_code_1(self):
        """AuthError should exit with code 1 (user error)."""
        with pytest.raises(SystemExit) as exc_info, handle_errors():
            raise AuthError("Token expired")
        assert exc_info.value.code == 1

    def test_config_error_exits_with_code_1(self):
        """ConfigurationError should exit with code 1 (user error)."""
        with pytest.raises(SystemExit) as exc_info, handle_errors():
            raise ConfigurationError("Missing config")
        assert exc_info.value.code == 1

    def test_network_error_exits_with_code_1(self):
        """NetworkError should exit with code 1 (user error)."""
        with pytest.raises(SystemExit) as exc_info, handle_errors():
            raise NetworkError("Connection failed")
        assert exc_info.value.code == 1

    def test_rate_limit_error_exits_with_code_1(self):
        """RateLimitError should exit with code 1 (user error)."""
        with pytest.raises(SystemExit) as exc_info, handle_errors():
            raise RateLimitError("Too many requests")
        assert exc_info.value.code == 1

    def test_unexpected_error_exits_with_code_2(self):
        """Unexpected exceptions should exit with code 2 (system error)."""
        with pytest.raises(SystemExit) as exc_info, handle_errors():
            raise RuntimeError("Unexpected bug")
        assert exc_info.value.code == 2


class TestHandleErrorsJsonOutput:
    """Test JSON error output format."""

    def test_validation_error_json_format(self, capsys):
        """ValidationError should produce correct JSON structure."""
        with pytest.raises(SystemExit), handle_errors(json_output=True):
            raise ValidationError("Invalid input")

        output = capsys.readouterr().out
        data = json.loads(output)
        assert data["error"] is True
        assert data["code"] == "VALIDATION_ERROR"
        assert "Invalid input" in data["message"]

    def test_rate_limit_error_json_includes_retry_after(self, capsys):
        """RateLimitError with retry_after should include it in JSON output."""
        with pytest.raises(SystemExit), handle_errors(json_output=True):
            raise RateLimitError("Too many requests", retry_after=30)

        output = capsys.readouterr().out
        data = json.loads(output)
        assert data["error"] is True
        assert data["code"] == "RATE_LIMITED"
        assert data["retry_after"] == 30
        assert "30s" in data["message"]

    def test_rate_limit_error_json_without_retry_after(self, capsys):
        """RateLimitError without retry_after should not include extra field."""
        with pytest.raises(SystemExit), handle_errors(json_output=True):
            raise RateLimitError("Too many requests")

        output = capsys.readouterr().out
        data = json.loads(output)
        assert data["error"] is True
        assert data["code"] == "RATE_LIMITED"
        assert "retry_after" not in data

    def test_rpc_error_verbose_includes_method_id(self, capsys):
        """RPCError with verbose=True should include method_id in JSON."""
        with pytest.raises(SystemExit), handle_errors(json_output=True, verbose=True):
            raise RPCError("RPC failed", method_id="abc123")

        output = capsys.readouterr().out
        data = json.loads(output)
        assert data["error"] is True
        assert data["code"] == "NOTEBOOKLM_ERROR"
        assert data["method_id"] == "abc123"

    def test_rpc_error_non_verbose_excludes_method_id(self, capsys):
        """RPCError without verbose should not include method_id."""
        with pytest.raises(SystemExit), handle_errors(json_output=True, verbose=False):
            raise RPCError("RPC failed", method_id="abc123")

        output = capsys.readouterr().out
        data = json.loads(output)
        assert data["error"] is True
        assert "method_id" not in data

    def test_notebook_limit_error_json_includes_quota_context(self, capsys):
        """NotebookLimitError should produce a specific JSON error code."""
        with pytest.raises(SystemExit), handle_errors(json_output=True):
            raise NotebookLimitError(499, limit=500)

        output = capsys.readouterr().out
        data = json.loads(output)
        assert data["error"] is True
        assert data["code"] == "NOTEBOOK_LIMIT"
        assert data["current_count"] == 499
        assert data["limit"] == 500
        assert "known_limits" not in data
        assert "method_id" not in data
        assert "rpc_code" not in data

    def test_unexpected_error_json_format(self, capsys):
        """Unexpected errors should produce UNEXPECTED_ERROR code."""
        with pytest.raises(SystemExit), handle_errors(json_output=True):
            raise RuntimeError("Something broke")

        output = capsys.readouterr().out
        data = json.loads(output)
        assert data["error"] is True
        assert data["code"] == "UNEXPECTED_ERROR"
        assert "Something broke" in data["message"]

    def test_error_handler_json_output_preserves_unicode(self, capsys):
        """CJK / emoji in error messages should be emitted as real UTF-8."""
        with pytest.raises(SystemExit), handle_errors(json_output=True):
            raise ValidationError("笔记本未找到 🔍")

        output = capsys.readouterr().out
        data = json.loads(output)
        assert "笔记本未找到 🔍" in data["message"]
        # Raw output must contain real CJK/emoji, not escaped sequences.
        assert "笔记本未找到" in output
        assert "🔍" in output
        assert "\\u" not in output

    def test_output_error_serializes_path_in_extra(self, capsys):
        """_output_error must not crash on non-primitive extras like pathlib.Path."""
        with pytest.raises(SystemExit) as exc_info:
            _output_error(
                "Bad path",
                "PATH_ERROR",
                json_output=True,
                exit_code=1,
                extra={"path": Path("tmp_test_path")},
            )

        assert exc_info.value.code == 1
        output = capsys.readouterr().out
        data = json.loads(output)
        assert data["error"] is True
        assert data["code"] == "PATH_ERROR"
        assert data["message"] == "Bad path"
        assert data["path"] == str(Path("tmp_test_path"))


class TestHandleErrorsTextOutput:
    """Test text error output with hints."""

    def test_auth_error_shows_hint(self, capsys):
        """AuthError should show re-authentication hint in text mode."""
        with pytest.raises(SystemExit), handle_errors(json_output=False):
            raise AuthError("Token expired")

        output = capsys.readouterr().err
        assert "Authentication error" in output
        assert "notebooklm login" in output

    def test_network_error_shows_hint(self, capsys):
        """NetworkError should show connection hint in text mode."""
        with pytest.raises(SystemExit), handle_errors(json_output=False):
            raise NetworkError("Connection refused")

        output = capsys.readouterr().err
        assert "Network error" in output
        assert "internet connection" in output

    def test_notebook_limit_error_text_includes_quota_context(self, capsys):
        """NotebookLimitError should show notebook count in text mode."""
        with pytest.raises(SystemExit), handle_errors(json_output=False):
            raise NotebookLimitError(499, limit=500)

        output = capsys.readouterr().err
        assert "notebook limit" in output.lower()
        assert "499/500" in output

    def test_unexpected_error_shows_bug_report_hint(self, capsys):
        """Unexpected errors should show bug report hint."""
        with pytest.raises(SystemExit), handle_errors(json_output=False):
            raise RuntimeError("Oops")

        output = capsys.readouterr().err
        assert "Unexpected error" in output
        assert "bug" in output.lower()
        assert "github" in output.lower()

    def test_hint_not_shown_in_json_mode(self, capsys):
        """Hints should not appear in JSON output."""
        with pytest.raises(SystemExit), handle_errors(json_output=True):
            raise AuthError("Token expired")

        output = capsys.readouterr().out
        data = json.loads(output)
        # Hint text should not be in the JSON structure
        assert "login" not in json.dumps(data).lower()

    def test_text_output_falls_back_when_stream_cannot_encode(self, monkeypatch):
        """Error reporting should not mask the original error with UnicodeEncodeError."""

        class DummyStderr:
            encoding = "cp950"

        calls = []

        def flaky_echo(message=None, **kwargs):
            err = kwargs.get("err", False)
            if not calls:
                calls.append((message, err))
                raise UnicodeEncodeError(
                    "cp950",
                    str(message),
                    0,
                    1,
                    "illegal multibyte sequence",
                )
            calls.append((message, err))

        monkeypatch.setattr(encoding_module.click, "echo", flaky_echo)
        monkeypatch.setattr(encoding_module.sys, "stderr", DummyStderr())

        with pytest.raises(SystemExit), handle_errors(json_output=False):
            raise RuntimeError("bad 🌐")

        assert calls[0] == ("Unexpected error: bad 🌐", True)
        assert calls[1] == ("Unexpected error: bad ?", True)


class TestHandleErrorsKeyboardInterrupt:
    """Test keyboard interrupt handling."""

    def test_keyboard_interrupt_exits_with_code_130(self):
        """KeyboardInterrupt should exit with code 130."""
        with pytest.raises(SystemExit) as exc_info, handle_errors():
            raise KeyboardInterrupt()
        assert exc_info.value.code == 130

    def test_keyboard_interrupt_json_format(self, capsys):
        """KeyboardInterrupt should produce CANCELLED code in JSON mode."""
        with pytest.raises(SystemExit), handle_errors(json_output=True):
            raise KeyboardInterrupt()

        output = capsys.readouterr().out
        data = json.loads(output)
        assert data["error"] is True
        assert data["code"] == "CANCELLED"
