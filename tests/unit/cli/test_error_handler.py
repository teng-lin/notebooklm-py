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
    SourceAddError,
    SourceNotFoundError,
    ValidationError,
)
from notebooklm.types import GenerationStatus


def _partial_upload(cause: Exception, *, source_id: str, stage: str) -> Exception:
    """Mirror ``raise_partial_upload_failure()``: attach recovery context directly
    to the real cause rather than wrapping it in a new exception type.
    """
    cause.source_id = source_id  # type: ignore[attr-defined]
    cause.stage = stage  # type: ignore[attr-defined]
    return cause


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

    @pytest.mark.parametrize(
        ("cause", "expected_code"),
        [
            (RateLimitError("Too many requests", retry_after=30), "RATE_LIMITED"),
            (AuthError("Session expired"), "AUTH_ERROR"),
            (NetworkError("Connection reset"), "NETWORK_ERROR"),
            (ValidationError("File type rejected"), "VALIDATION_ERROR"),
        ],
    )
    def test_partial_upload_error_keeps_its_cause_category(self, capsys, cause, expected_code):
        """A partial upload must not flatten to NOTEBOOKLM_ERROR.

        The recovery context rides on the real cause as plain attributes rather
        than a wrapper type, so the exception lands in whichever typed branch
        already matched it — keeping its category and, with it, the re-auth hint /
        retry hint / ``retry_after`` field.
        """
        with pytest.raises(SystemExit), handle_errors(json_output=True):
            raise _partial_upload(cause, source_id="src_123", stage="upload_finalize")

        data = json.loads(capsys.readouterr().out)
        assert data["code"] == expected_code
        assert data["code"] != "NOTEBOOKLM_ERROR"

    def test_partial_upload_error_preserves_retry_after(self, capsys):
        """The cause-specific extras are untouched, not just the code."""
        with pytest.raises(SystemExit), handle_errors(json_output=True):
            raise _partial_upload(
                RateLimitError("Too many requests", retry_after=30),
                source_id="src_123",
                stage="start_session",
            )

        data = json.loads(capsys.readouterr().out)
        assert data["code"] == "RATE_LIMITED"
        assert data["retry_after"] == 30

    @pytest.mark.parametrize(
        ("cause", "expected_code"),
        [
            (ValidationError("File type rejected"), "VALIDATION_ERROR"),
            (NetworkError("Connection reset"), "NETWORK_ERROR"),
            (RateLimitError("Too many requests", retry_after=30), "RATE_LIMITED"),
        ],
    )
    def test_partial_upload_json_keeps_category_and_recovery_together(
        self, capsys, cause, expected_code
    ):
        """The typed branch's projection and the recovery context must coexist.

        The category / ``retry_after`` projection comes from the typed branch; the
        retained ``source_id`` / ``stage`` come from the attached attributes. Both
        have to survive in the same envelope, or ``source add`` reports *why* it
        failed without saying *what it left behind*.
        """
        with pytest.raises(SystemExit), handle_errors(json_output=True):
            raise _partial_upload(cause, source_id="src_123", stage="upload_finalize")

        data = json.loads(capsys.readouterr().out)
        assert data["code"] == expected_code
        assert data["source_id"] == "src_123"
        assert data["stage"] == "upload_finalize"
        if isinstance(cause, RateLimitError):
            assert data["retry_after"] == 30

    def test_partial_upload_text_names_the_retained_source(self, capsys):
        """Text mode must be actionable: which row was left, and how to drop it."""
        with pytest.raises(SystemExit), handle_errors(json_output=False):
            raise _partial_upload(
                AuthError("Session expired"), source_id="src_123", stage="upload_finalize"
            )

        err = capsys.readouterr().err
        assert "src_123" in err
        assert "upload_finalize" in err
        assert "notebooklm source delete src_123" in err
        # The cause's own projection (here: the re-auth hint) still has to show.
        assert "notebooklm login" in err
        # ``stage`` is a location, not proof of transfer — never claim bytes moved.
        assert "uploaded" not in err

    def test_ordinary_error_gets_no_retained_source_context(self, capsys):
        """The recovery block is opt-in: an error with no attached ``source_id``
        must emit exactly the envelope it always did.
        """
        with pytest.raises(SystemExit), handle_errors(json_output=True):
            raise AuthError("Session expired")

        data = json.loads(capsys.readouterr().out)
        assert data["code"] == "AUTH_ERROR"
        assert "source_id" not in data
        assert "stage" not in data

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

    def test_not_found_candidates_in_json_and_text_hint(self, capsys):
        """Near-miss candidates (issue #1787) surface in JSON and as a text hint."""
        err = NotebookNotFoundError("Scientific")
        err.candidates = [{"id": "37fe5c1d", "title": "Scientific PDF Parsing"}]
        with pytest.raises(SystemExit), handle_errors(json_output=True):
            raise err
        data = json.loads(capsys.readouterr().out)
        assert data["code"] == "NOT_FOUND"
        assert data["candidates"] == [{"id": "37fe5c1d", "title": "Scientific PDF Parsing"}]

        err2 = NotebookNotFoundError("Scientific")
        err2.candidates = [{"id": "37fe5c1d", "title": "Scientific PDF Parsing"}]
        with pytest.raises(SystemExit), handle_errors(json_output=False):
            raise err2
        text = capsys.readouterr().err
        assert "Did you mean:" in text
        assert "Scientific PDF Parsing" in text

    def test_not_found_without_candidates_omits_candidates_key(self, capsys):
        with pytest.raises(SystemExit), handle_errors(json_output=True):
            raise NotebookNotFoundError("Scientific")
        data = json.loads(capsys.readouterr().out)
        assert "candidates" not in data

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


def test_unconfirmed_create_overrides_the_typed_branch_retry_advice(capsys):
    """The CLI must not tell a user to retry an unresolved create (#2220).

    ``handle_errors`` dispatches on exception TYPE, and a probe re-raises its
    transport/auth failure unchanged — so a marked ``RateLimitError`` lands in
    the rate-limit branch and is told "Retry after Ns", a marked ``AuthError``
    is told to re-login and retry, a marked ``NetworkError`` to try again. Each
    is the duplicate the marker exists to prevent. The CLI has its own
    ``except`` ladder rather than routing through ``_app.errors.classify``, so
    the classifier fix does not reach it.

    The machine-readable ``code`` is overridden too, not merely annotated: a
    script keying on ``RATE_LIMITED`` would otherwise back off and retry
    exactly as the message tells it not to.
    """
    from notebooklm._idempotency import mark_unconfirmed

    for error in (
        RateLimitError("Too many requests", retry_after=30),
        AuthError("Token expired"),
        NetworkError("Connection failed"),
    ):
        with pytest.raises(SystemExit), handle_errors(json_output=True):
            raise mark_unconfirmed(error)

        data = json.loads(capsys.readouterr().out)
        assert data["code"] == "UNCONFIRMED_WRITE", f"{type(error).__name__} kept its own code"
        assert data["unconfirmed"] is True
        # The branch's own text carries the advice ("Retry after 30s"), so it
        # must be REPLACED, not merely annotated — and ``retry_after`` is an
        # instruction in machine-readable form, so it must not survive either.
        # ``_output_error`` serializes ``extra`` but NOT ``hint``, which is why
        # the hint is carried in ``extra`` for JSON.
        assert "Retry after" not in data["message"]
        assert "retry_after" not in data
        assert "retrying blind can create a duplicate" in data["hint"]


def test_unconfirmed_create_note_reaches_human_output(capsys):
    """Text mode gets the same correction, appended rather than replacing."""
    from notebooklm._idempotency import mark_unconfirmed

    with pytest.raises(SystemExit), handle_errors():
        raise mark_unconfirmed(NetworkError("Connection failed"))

    out = capsys.readouterr()
    combined = (out.out + out.err).lower()
    assert "retrying blind can create a duplicate" in combined
    # Once, not twice: text mode prints the hint after the message, so setting
    # both would repeat the whole paragraph.
    assert combined.count("retrying blind can create a duplicate") == 1
    assert "could not be confirmed" in combined


@pytest.mark.parametrize("operation", ["sources.add_url", "sources.add_drive", "sources.add_text"])
def test_source_add_unknown_operations_project_reconciliation_guidance(capsys, operation: str):
    from notebooklm._idempotency import mark_unconfirmed

    error = mark_unconfirmed(SourceAddError("input"), operation=operation)
    with pytest.raises(SystemExit), handle_errors(json_output=True):
        raise error

    data = json.loads(capsys.readouterr().out)
    assert data["code"] == "UNCONFIRMED_WRITE"
    assert data["unconfirmed"] is True
    assert "source list" in data["hint"]


def test_chat_unknown_operation_projects_conversation_history_guidance(capsys):
    from notebooklm._idempotency import mark_unconfirmed

    with pytest.raises(SystemExit), handle_errors(json_output=True):
        raise mark_unconfirmed(NetworkError("stream lost"), operation="chat")

    data = json.loads(capsys.readouterr().out)
    assert data["unconfirmed"] is True
    assert "conversation history" in data["hint"]


def test_ordinary_errors_keep_their_typed_branch_code(capsys):
    """The override is opt-in — unmarked failures keep their own guidance."""
    with pytest.raises(SystemExit), handle_errors(json_output=True):
        raise RateLimitError("Too many requests", retry_after=30)

    data = json.loads(capsys.readouterr().out)
    assert data["code"] == "RATE_LIMITED"
    assert "unconfirmed" not in data


def _four_state_batch_error() -> NetworkError:
    from notebooklm._idempotency import attach_batch_outcome
    from notebooklm.outcomes import (
        BatchItemOutcome,
        BatchOutcome,
        CommitState,
        ReconciliationReport,
    )

    return attach_batch_outcome(
        NetworkError("lost after dispatch at /home/alice/private.log"),
        BatchOutcome(
            items=(
                BatchItemOutcome(
                    member=0,
                    input="https://ok.example",
                    commit_state=CommitState.CONFIRMED,
                    resource_id="source-confirmed",
                ),
                BatchItemOutcome(
                    member=1,
                    input="https://bad.example",
                    commit_state=CommitState.REJECTED,
                ),
                BatchItemOutcome(
                    member=2,
                    input="https://unknown.example?access_token=top-secret",
                    commit_state=CommitState.UNKNOWN,
                    reconciliation=ReconciliationReport(
                        unresolved_inputs=("/home/alice/private.log",),
                        reason="readback inconclusive",
                    ),
                ),
                BatchItemOutcome(
                    member=3,
                    input="ftp://not-sent.example",
                    commit_state=CommitState.NOT_SENT,
                ),
            )
        ),
    )


def test_cli_json_error_projects_public_four_state_batch_progress(capsys) -> None:
    with pytest.raises(SystemExit) as raised, handle_errors(json_output=True):
        raise _four_state_batch_error()

    assert raised.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    items = payload["batch_outcome"]["items"]
    assert [item["commit_state"] for item in items] == [
        "confirmed",
        "rejected",
        "unknown",
        "not_sent",
    ]
    assert items[0]["resource_id"] == "source-confirmed"
    serialized = json.dumps(payload)
    assert "top-secret" not in serialized
    assert "/home/alice" not in serialized
    assert payload["batch_outcome"]["whole_request_retriable"] is False


def test_cli_text_error_projects_batch_progress_and_exit_policy(capsys) -> None:
    with pytest.raises(SystemExit) as raised, handle_errors(json_output=False):
        raise _four_state_batch_error()

    assert raised.value.code == 1
    rendered = capsys.readouterr().err
    assert "source-confirmed" in rendered
    assert '"commit_state":"confirmed"' in rendered
    assert '"commit_state":"unknown"' in rendered
    assert "top-secret" not in rendered
    assert "/home/alice" not in rendered
