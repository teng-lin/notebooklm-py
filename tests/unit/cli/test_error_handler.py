"""Tests for centralized CLI error handling."""

import json
from pathlib import Path

import pytest

import notebooklm.cli._encoding as encoding_module
from notebooklm.cli.error_handler import (
    _output_error,
    emit_cancelled_and_exit,
    exit_with_code,
    handle_errors,
)
from notebooklm.exceptions import (
    ArtifactNotFoundError,
    ArtifactPendingTimeoutError,
    AuthError,
    ConfigurationError,
    LabelNotFoundError,
    MindMapNotFoundError,
    NetworkError,
    NotebookLimitError,
    NotebookNotFoundError,
    NoteNotFoundError,
    NotFoundError,
    RateLimitError,
    RPCError,
    SourceNotFoundError,
    ValidationError,
)
from notebooklm.types import GenerationStatus


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

    def test_exit_with_code_is_canonical_raw_exit_path(self):
        """Callers that already emitted output can still exit through error_handler."""
        with pytest.raises(SystemExit) as exc_info:
            exit_with_code(75)

        assert exc_info.value.code == 75


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

    def test_artifact_timeout_json_includes_poll_context(self, capsys):
        """ArtifactTimeoutError should be a user error with structured context."""
        with pytest.raises(SystemExit) as exc_info, handle_errors(json_output=True):
            raise ArtifactPendingTimeoutError(
                "nb_123",
                "task_123",
                600.0,
                last_status="pending",
                status_history=("pending",),
                status_transitions=(
                    GenerationStatus(
                        "task_123",
                        "pending",
                        metadata={"raw_status": "completed", "media_ready": False},
                    ),
                ),
            )

        assert exc_info.value.code == 1
        output = capsys.readouterr().out
        data = json.loads(output)
        assert data["error"] is True
        assert data["code"] == "ARTIFACT_TIMEOUT"
        assert data["notebook_id"] == "nb_123"
        assert data["task_id"] == "task_123"
        assert data["timeout_seconds"] == 600.0
        assert data["last_status"] == "pending"
        assert data["status_history"] == ["pending"]
        assert data["status_transitions"] == [
            {
                "task_id": "task_123",
                "status": "pending",
                "url": None,
                "error": None,
                "error_code": None,
                "metadata": {"raw_status": "completed", "media_ready": False},
            }
        ]
        assert data["stalled_phase"] == "pending"

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


class TestHandleErrorsNotFound:
    """The ``*NotFoundError`` family emits the typed ``NOT_FOUND`` envelope.

    Regression guard for issue #1364: before the dedicated ``except
    NotFoundError`` branch, any ``*NotFoundError`` reaching the centralized
    handler fell through to the generic ``NOTEBOOKLM_ERROR`` catch-all. The
    handler now emits ``code="NOT_FOUND"`` with exit ``1`` (matching the
    per-command ``source``/``artifact``/``note get`` convention) and surfaces
    the missing resource id in the JSON ``extra`` block.
    """

    # (exception, native id key, id value) for each concrete subclass. All
    # five derive ``(NotFoundError, RPCError, <Domain>Error)`` so the umbrella
    # ``except NotFoundError`` catches every one.
    _CASES = [
        (NotebookNotFoundError("nb_123"), "notebook_id", "nb_123"),
        (SourceNotFoundError("src_456"), "source_id", "src_456"),
        (ArtifactNotFoundError("art_789", "audio"), "artifact_id", "art_789"),
        (NoteNotFoundError("note_111"), "note_id", "note_111"),
        (MindMapNotFoundError("mm_222"), "mind_map_id", "mm_222"),
        (LabelNotFoundError("label_333"), "label_id", "label_333"),
    ]

    @pytest.mark.parametrize(
        ("exc", "id_key", "id_value"),
        _CASES,
        ids=lambda v: type(v).__name__ if isinstance(v, Exception) else str(v),
    )
    def test_not_found_json_envelope(self, capsys, exc, id_key, id_value):
        """Each ``*NotFoundError`` produces NOT_FOUND + exit 1 + the resource id."""
        with pytest.raises(SystemExit) as exc_info, handle_errors(json_output=True):
            raise exc

        assert exc_info.value.code == 1
        data = json.loads(capsys.readouterr().out)
        assert data["error"] is True
        assert data["code"] == "NOT_FOUND"
        # Native attribute key plus the generic ``id`` alias are both present so
        # automation can read the id without knowing the exact subtype.
        assert data[id_key] == id_value
        assert data["id"] == id_value

    @pytest.mark.parametrize(
        ("exc", "id_key", "id_value"),
        _CASES,
        ids=lambda v: type(v).__name__ if isinstance(v, Exception) else str(v),
    )
    def test_not_found_exit_code_is_1(self, exc, id_key, id_value):
        """Not-found is a user error (exit 1), never the system-error exit 2."""
        with pytest.raises(SystemExit) as exc_info, handle_errors():
            raise exc
        assert exc_info.value.code == 1

    def test_not_found_text_mode(self, capsys):
        """Text mode prints the exception message (no traceback, exit 1)."""
        with pytest.raises(SystemExit) as exc_info, handle_errors(json_output=False):
            raise SourceNotFoundError("src_456")

        assert exc_info.value.code == 1
        output = capsys.readouterr().err
        assert "Source not found: src_456" in output

    def test_not_found_verbose_includes_method_id(self, capsys):
        """With ``verbose``, the RPC ``method_id`` is surfaced in the envelope."""
        with pytest.raises(SystemExit), handle_errors(json_output=True, verbose=True):
            raise SourceNotFoundError("src_456", method_id="abc123")

        data = json.loads(capsys.readouterr().out)
        assert data["code"] == "NOT_FOUND"
        assert data["method_id"] == "abc123"

    def test_not_found_non_verbose_excludes_method_id(self, capsys):
        """Without ``verbose``, ``method_id`` stays out of the envelope."""
        with pytest.raises(SystemExit), handle_errors(json_output=True, verbose=False):
            raise SourceNotFoundError("src_456", method_id="abc123")

        data = json.loads(capsys.readouterr().out)
        assert data["code"] == "NOT_FOUND"
        assert "method_id" not in data

    def test_not_found_branch_precedes_generic_rpc_error(self, capsys):
        """A bare ``NotFoundError`` umbrella instance still maps to NOT_FOUND.

        Confirms the dedicated branch sits before the generic
        ``except NotebookLMError`` (the catch-all that would otherwise emit
        ``NOTEBOOKLM_ERROR``). A future ``*NotFoundError`` subclass with no
        recognized id attribute drops the (empty) ``extra`` cleanly.
        """
        with pytest.raises(SystemExit), handle_errors(json_output=True):
            raise NotFoundError("nothing here")

        data = json.loads(capsys.readouterr().out)
        assert data["code"] == "NOT_FOUND"
        assert "id" not in data

    def test_generic_rpc_error_still_emits_notebooklm_error(self, capsys):
        """A non-not-found ``RPCError`` is unaffected — still NOTEBOOKLM_ERROR.

        Guards against the new branch widening too far and swallowing ordinary
        RPC failures.
        """
        with pytest.raises(SystemExit), handle_errors(json_output=True):
            raise RPCError("RPC failed")

        data = json.loads(capsys.readouterr().out)
        assert data["code"] == "NOTEBOOKLM_ERROR"


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

    def test_artifact_timeout_text_includes_poll_context(self, capsys):
        """ArtifactTimeoutError should remain a user error in text mode."""
        with pytest.raises(SystemExit) as exc_info, handle_errors(json_output=False):
            raise ArtifactPendingTimeoutError(
                "nb_123",
                "task_123",
                600.0,
                last_status="pending",
                status_history=("pending",),
            )

        assert exc_info.value.code == 1
        output = capsys.readouterr().err
        assert "Artifact timeout" in output
        assert "task_123" in output
        assert "last status: pending" in output

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


class TestEmitCancelledAndExit:
    """Tests for the SIGINT-with-resume-hint helper.

    ``emit_cancelled_and_exit`` is the canonical exit point for Ctrl-C during
    a long-running ``--wait`` poll. The helper enforces the required user-
    visible phrasing: ``Cancelled. Resume with: <resume_hint>`` and exit 130.
    """

    def test_emit_cancelled_with_hint_writes_to_stderr_and_exits_130(self, capsys):
        """Text mode: prints the canonical resume line on stderr, exits 130."""
        with pytest.raises(SystemExit) as exc_info:
            emit_cancelled_and_exit("notebooklm artifact poll task_abc")

        assert exc_info.value.code == 130
        captured = capsys.readouterr()
        # Specification: SIGINT under --wait emits exactly this resume line.
        # Used as a literal so a future cosmetic tweak can't silently drift
        # the user-facing string.
        assert "Cancelled. Resume with: notebooklm artifact poll task_abc" in captured.err
        assert captured.out == "", "resume hint must NOT leak onto stdout in text mode"

    def test_emit_cancelled_without_hint_falls_back_to_plain_cancelled(self, capsys):
        """No hint: emits the bare ``Cancelled.`` line (matches generic handler)."""
        with pytest.raises(SystemExit) as exc_info:
            emit_cancelled_and_exit(None)

        assert exc_info.value.code == 130
        captured = capsys.readouterr()
        assert "Cancelled." in captured.err
        assert "Resume with" not in captured.err

    def test_emit_cancelled_json_envelope_includes_resume_hint(self, capsys):
        """JSON mode: structured envelope with code=CANCELLED + resume_hint, exit 130."""
        with pytest.raises(SystemExit) as exc_info:
            emit_cancelled_and_exit(
                "notebooklm artifact poll task_xyz",
                json_output=True,
            )

        assert exc_info.value.code == 130
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["error"] is True
        assert data["code"] == "CANCELLED"
        assert data["resume_hint"] == "notebooklm artifact poll task_xyz"
        assert "message" in data

    def test_emit_cancelled_json_omits_resume_hint_when_none(self, capsys):
        """JSON mode without a hint: envelope still parses but no resume_hint key."""
        with pytest.raises(SystemExit) as exc_info:
            emit_cancelled_and_exit(None, json_output=True)

        assert exc_info.value.code == 130
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["error"] is True
        assert data["code"] == "CANCELLED"
        assert "resume_hint" not in data

    def test_emit_cancelled_json_extra_merged_into_envelope(self, capsys):
        """JSON mode: ``extra`` dict is merged so callers can attach a task_id."""
        with pytest.raises(SystemExit):
            emit_cancelled_and_exit(
                "notebooklm artifact poll task_abc",
                json_output=True,
                extra={"task_id": "task_abc"},
            )

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["task_id"] == "task_abc"
        assert data["resume_hint"] == "notebooklm artifact poll task_abc"
