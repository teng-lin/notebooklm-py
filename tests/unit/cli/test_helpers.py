"""Tests for CLI helper functions."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from notebooklm.cli.helpers import (
    ARTIFACT_TYPE_MAP,
    clear_context,
    detect_source_type,
    # Type display helpers
    get_artifact_type_display,
    get_auth_tokens,
    # Auth helpers
    get_client,
    get_current_conversation,
    # Context helpers
    get_current_notebook,
    get_source_type_display,
    handle_auth_error,
    # Error handling
    handle_error,
    json_error_response,
    # Output helpers
    json_output_response,
    require_notebook,
    run_async,
    set_current_conversation,
    set_current_notebook,
    # Decorator
    with_client,
)

# =============================================================================
# ARTIFACT TYPE DISPLAY TESTS
# =============================================================================


class TestGetArtifactTypeDisplay:
    def test_audio_type(self):
        assert get_artifact_type_display(1) == "🎵 Audio Overview"

    def test_report_type(self):
        assert get_artifact_type_display(2) == "📄 Report"

    def test_video_type(self):
        assert get_artifact_type_display(3) == "🎥 Video Overview"

    def test_quiz_type_without_variant(self):
        assert get_artifact_type_display(4) == "📝 Quiz"

    def test_quiz_type_with_variant_2(self):
        assert get_artifact_type_display(4, variant=2) == "📝 Quiz"

    def test_flashcards_type_with_variant_1(self):
        assert get_artifact_type_display(4, variant=1) == "🃏 Flashcards"

    def test_mind_map_type(self):
        assert get_artifact_type_display(5) == "🧠 Mind Map"

    def test_infographic_type(self):
        assert get_artifact_type_display(7) == "🖼️ Infographic"

    def test_slide_deck_type(self):
        assert get_artifact_type_display(8) == "🎞️ Slide Deck"

    def test_data_table_type(self):
        assert get_artifact_type_display(9) == "📋 Data Table"

    def test_unknown_type(self):
        assert get_artifact_type_display(999) == "Unknown (999)"

    def test_report_subtype_briefing_doc(self):
        assert get_artifact_type_display(2, report_subtype="briefing_doc") == "📋 Briefing Doc"

    def test_report_subtype_study_guide(self):
        assert get_artifact_type_display(2, report_subtype="study_guide") == "📚 Study Guide"

    def test_report_subtype_blog_post(self):
        assert get_artifact_type_display(2, report_subtype="blog_post") == "✍️ Blog Post"

    def test_report_subtype_generic(self):
        assert get_artifact_type_display(2, report_subtype="report") == "📄 Report"

    def test_report_subtype_unknown(self):
        """Unknown report subtype should return default Report"""
        assert get_artifact_type_display(2, report_subtype="unknown_type") == "📄 Report"


class TestDetectSourceType:
    def test_youtube_url(self):
        src = [
            "id",
            "Video Title",
            [None, None, None, None, None, None, None, ["https://youtube.com/watch?v=abc"]],
        ]
        assert detect_source_type(src) == "🎥 YouTube"

    def test_youtu_be_url(self):
        src = [
            "id",
            "Video Title",
            [None, None, None, None, None, None, None, ["https://youtu.be/abc"]],
        ]
        assert detect_source_type(src) == "🎥 YouTube"

    def test_web_url(self):
        src = [
            "id",
            "Web Page",
            [None, None, None, None, None, None, None, ["https://example.com/article"]],
        ]
        assert detect_source_type(src) == "🔗 Web URL"

    def test_pdf_file(self):
        src = ["id", "document.pdf", [None, 12345]]
        assert detect_source_type(src) == "📄 PDF"

    def test_text_file_txt(self):
        src = ["id", "notes.txt", [None, 1234]]
        assert detect_source_type(src) == "📝 Text File"

    def test_text_file_md(self):
        src = ["id", "readme.md", [None, 1234]]
        assert detect_source_type(src) == "📝 Text File"

    def test_text_file_doc(self):
        src = ["id", "readme.doc", [None, 1234]]
        assert detect_source_type(src) == "📝 Text File"

    def test_text_file_docx(self):
        src = ["id", "readme.docx", [None, 1234]]
        assert detect_source_type(src) == "📝 Text File"

    def test_spreadsheet_csv(self):
        src = ["id", "data.csv", [None, 1234]]
        assert detect_source_type(src) == "📊 Spreadsheet"

    def test_spreadsheet_xlsx(self):
        src = ["id", "data.xlsx", [None, 1234]]
        assert detect_source_type(src) == "📊 Spreadsheet"

    def test_spreadsheet_xls(self):
        src = ["id", "data.xls", [None, 1234]]
        assert detect_source_type(src) == "📊 Spreadsheet"

    def test_uploaded_file_with_size(self):
        src = ["id", "Unknown File", [None, 5000]]
        assert detect_source_type(src) == "📎 Upload"

    def test_pasted_text(self):
        src = ["id", "Pasted Text", [None, 0]]
        assert detect_source_type(src) == "📝 Pasted Text"

    def test_empty_source(self):
        src = []
        assert detect_source_type(src) == "📝 Pasted Text"


class TestGetSourceTypeDisplay:
    def test_youtube(self):
        assert get_source_type_display("youtube") == "🎥 YouTube"

    def test_web_page(self):
        assert get_source_type_display("web_page") == "🔗 Web URL"

    def test_pdf(self):
        assert get_source_type_display("pdf") == "📄 PDF"

    def test_markdown(self):
        assert get_source_type_display("markdown") == "📝 Markdown"

    def test_google_spreadsheet(self):
        assert get_source_type_display("google_spreadsheet") == "📊 Google Sheets"

    def test_csv(self):
        assert get_source_type_display("csv") == "📊 CSV"

    def test_upload(self):
        assert get_source_type_display("upload") == "📎 Upload"

    def test_docx(self):
        assert get_source_type_display("docx") == "📝 DOCX"

    def test_pasted_text(self):
        assert get_source_type_display("pasted_text") == "📝 Pasted Text"

    def test_unknown_type(self):
        assert get_source_type_display("unknown") == "❓ Unknown"

    def test_unrecognized_type_shows_name(self):
        # Unrecognized types should show the type name
        assert get_source_type_display("future_type") == "❓ future_type"


class TestArtifactTypeMappings:
    def test_artifact_type_map_video(self):
        assert ARTIFACT_TYPE_MAP["video"] == 3

    def test_artifact_type_map_slide_deck(self):
        assert ARTIFACT_TYPE_MAP["slide-deck"] == 8

    def test_artifact_type_map_quiz(self):
        assert ARTIFACT_TYPE_MAP["quiz"] == 4

    def test_artifact_type_map_flashcard(self):
        assert ARTIFACT_TYPE_MAP["flashcard"] == 4

    def test_artifact_type_map_mind_map(self):
        assert ARTIFACT_TYPE_MAP["mind-map"] == 5

    def test_artifact_type_map_infographic(self):
        assert ARTIFACT_TYPE_MAP["infographic"] == 7

    def test_artifact_type_map_data_table(self):
        assert ARTIFACT_TYPE_MAP["data-table"] == 9

    def test_artifact_type_map_report(self):
        assert ARTIFACT_TYPE_MAP["report"] == 2


# =============================================================================
# JSON OUTPUT TESTS
# =============================================================================


class TestJsonOutputResponse:
    def test_outputs_valid_json(self, capsys):
        json_output_response({"test": "value", "number": 42})

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["test"] == "value"
        assert data["number"] == 42

    def test_handles_nested_data(self, capsys):
        json_output_response({"nested": {"key": "value"}, "list": [1, 2, 3]})

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["nested"]["key"] == "value"
        assert data["list"] == [1, 2, 3]


class TestJsonErrorResponse:
    def test_outputs_error_json_and_exits(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            json_error_response("TEST_ERROR", "Test error message")

        assert exc_info.value.code == 1

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["error"] is True
        assert data["code"] == "TEST_ERROR"
        assert data["message"] == "Test error message"


# =============================================================================
# CONTEXT MANAGEMENT TESTS
# =============================================================================


class TestContextManagement:
    def test_get_current_notebook_no_file(self, tmp_path):
        with patch(
            "notebooklm.cli.helpers.get_context_path", return_value=tmp_path / "nonexistent.json"
        ):
            result = get_current_notebook()
            assert result is None

    def test_set_and_get_current_notebook(self, tmp_path):
        context_file = tmp_path / "context.json"
        with patch("notebooklm.cli.helpers.get_context_path", return_value=context_file):
            set_current_notebook("nb_test123", title="Test Notebook")
            result = get_current_notebook()
            assert result == "nb_test123"

    def test_set_notebook_with_all_fields(self, tmp_path):
        context_file = tmp_path / "context.json"
        with patch("notebooklm.cli.helpers.get_context_path", return_value=context_file):
            set_current_notebook(
                "nb_test123", title="Test Notebook", is_owner=True, created_at="2024-01-01T00:00:00"
            )
            data = json.loads(context_file.read_text())
            assert data["notebook_id"] == "nb_test123"
            assert data["title"] == "Test Notebook"
            assert data["is_owner"] is True
            assert data["created_at"] == "2024-01-01T00:00:00"

    def test_clear_context(self, tmp_path):
        context_file = tmp_path / "context.json"
        context_file.write_text('{"notebook_id": "test"}')
        with patch("notebooklm.cli.helpers.get_context_path", return_value=context_file):
            clear_context()
            assert not context_file.exists()

    def test_clear_context_no_file(self, tmp_path):
        """clear_context should not raise if file doesn't exist"""
        context_file = tmp_path / "nonexistent.json"
        with patch("notebooklm.cli.helpers.get_context_path", return_value=context_file):
            clear_context()  # Should not raise

    def test_get_current_conversation_no_file(self, tmp_path):
        with patch(
            "notebooklm.cli.helpers.get_context_path", return_value=tmp_path / "nonexistent.json"
        ):
            result = get_current_conversation()
            assert result is None

    def test_set_and_get_current_conversation(self, tmp_path):
        context_file = tmp_path / "context.json"
        context_file.parent.mkdir(parents=True, exist_ok=True)
        context_file.write_text('{"notebook_id": "nb_123"}')
        with patch("notebooklm.cli.helpers.get_context_path", return_value=context_file):
            set_current_conversation("conv_456")
            result = get_current_conversation()
            assert result == "conv_456"

    def test_clear_conversation(self, tmp_path):
        context_file = tmp_path / "context.json"
        context_file.write_text('{"notebook_id": "nb_123", "conversation_id": "conv_456"}')
        with patch("notebooklm.cli.helpers.get_context_path", return_value=context_file):
            set_current_conversation(None)
            result = get_current_conversation()
            assert result is None

    def test_get_notebook_invalid_json(self, tmp_path):
        context_file = tmp_path / "context.json"
        context_file.write_text("invalid json")
        with patch("notebooklm.cli.helpers.get_context_path", return_value=context_file):
            result = get_current_notebook()
            assert result is None


class TestRequireNotebook:
    def test_returns_provided_notebook_id(self, tmp_path):
        with patch(
            "notebooklm.cli.helpers.get_context_path", return_value=tmp_path / "context.json"
        ):
            result = require_notebook("nb_provided")
            assert result == "nb_provided"

    def test_returns_context_notebook_when_none_provided(self, tmp_path):
        context_file = tmp_path / "context.json"
        context_file.write_text('{"notebook_id": "nb_context"}')
        with patch("notebooklm.cli.helpers.get_context_path", return_value=context_file):
            result = require_notebook(None)
            assert result == "nb_context"

    def test_raises_system_exit_when_no_notebook(self, tmp_path):
        with (
            patch(
                "notebooklm.cli.helpers.get_context_path",
                return_value=tmp_path / "nonexistent.json",
            ),
            patch("notebooklm.cli.helpers.console"),
        ):
            with pytest.raises(SystemExit) as exc_info:
                require_notebook(None)
            assert exc_info.value.code == 1


# =============================================================================
# ERROR HANDLING TESTS
# =============================================================================


class TestHandleError:
    def test_prints_error_and_exits(self):
        with patch("notebooklm.cli.helpers.console") as mock_console:
            with pytest.raises(SystemExit) as exc_info:
                handle_error(ValueError("Test error"))
            assert exc_info.value.code == 1
            mock_console.print.assert_called_once()
            call_args = mock_console.print.call_args[0][0]
            assert "Test error" in call_args


class TestHandleAuthError:
    def test_non_json_prints_message_and_exits(self):
        with patch("notebooklm.cli.helpers.console") as mock_console:
            with pytest.raises(SystemExit) as exc_info:
                handle_auth_error(json_output=False)
            assert exc_info.value.code == 1
            # Enhanced error message makes multiple print calls
            assert mock_console.print.call_count >= 1
            # Verify key messages are present across all calls
            all_output = " ".join(str(call[0][0]) for call in mock_console.print.call_args_list)
            assert "not logged in" in all_output.lower()
            assert "login" in all_output.lower()

    def test_json_outputs_json_error_and_exits(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            handle_auth_error(json_output=True)

        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["error"] is True
        assert data["code"] == "AUTH_REQUIRED"


# =============================================================================
# WITH_CLIENT DECORATOR TESTS
# =============================================================================


class TestWithClientDecorator:
    def test_decorator_passes_auth_to_function(self):
        """Test that @with_client properly injects client_auth"""
        import click
        from click.testing import CliRunner

        @click.command()
        @with_client
        def test_cmd(ctx, client_auth):
            async def _run():
                click.echo(f"Got auth: {client_auth is not None}")

            return _run()

        runner = CliRunner()
        with patch("notebooklm.cli.helpers.load_auth_from_storage") as mock_load:
            mock_load.return_value = {"SID": "test"}
            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(test_cmd)

        assert result.exit_code == 0
        assert "Got auth: True" in result.output

    def test_decorator_handles_no_auth(self):
        """Test that @with_client handles missing auth gracefully"""
        import click
        from click.testing import CliRunner

        @click.command()
        @with_client
        def test_cmd(ctx, client_auth):
            async def _run():
                pass

            return _run()

        runner = CliRunner()
        with patch("notebooklm.cli.helpers.load_auth_from_storage") as mock_load:
            mock_load.side_effect = FileNotFoundError("No auth")
            result = runner.invoke(test_cmd)

        assert result.exit_code == 1
        assert "login" in result.output.lower()

    def test_decorator_handles_exception_non_json(self):
        """Test error handling in non-JSON mode"""
        import click
        from click.testing import CliRunner

        @click.command()
        @with_client
        def test_cmd(ctx, client_auth):
            async def _run():
                raise ValueError("Test error")

            return _run()

        runner = CliRunner()
        with patch("notebooklm.cli.helpers.load_auth_from_storage") as mock_load:
            mock_load.return_value = {"SID": "test"}
            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(test_cmd)

        assert result.exit_code == 1
        assert "Test error" in result.output

    def test_decorator_handles_exception_json_mode(self):
        """Test error handling in JSON mode"""
        import click
        from click.testing import CliRunner

        @click.command()
        @click.option("--json", "json_output", is_flag=True)
        @with_client
        def test_cmd(ctx, json_output, client_auth):
            async def _run():
                raise ValueError("Test error")

            return _run()

        runner = CliRunner()
        with patch("notebooklm.cli.helpers.load_auth_from_storage") as mock_load:
            mock_load.return_value = {"SID": "test"}
            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(test_cmd, ["--json"])

        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["error"] is True
        assert "Test error" in data["message"]


# =============================================================================
# GET_CLIENT AND GET_AUTH_TOKENS TESTS
# =============================================================================


class TestGetClient:
    def test_returns_tuple_of_auth_components(self):
        ctx = MagicMock()
        ctx.obj = None

        with patch("notebooklm.cli.helpers.load_auth_from_storage") as mock_load:
            mock_load.return_value = {"SID": "test_sid"}
            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf_token", "session_id")

                cookies, csrf, session = get_client(ctx)

        assert cookies == {"SID": "test_sid"}
        assert csrf == "csrf_token"
        assert session == "session_id"

    def test_uses_storage_path_from_context(self):
        ctx = MagicMock()
        ctx.obj = {"storage_path": "/custom/path"}

        with patch("notebooklm.cli.helpers.load_auth_from_storage") as mock_load:
            mock_load.return_value = {"SID": "test"}
            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")

                get_client(ctx)

        mock_load.assert_called_once_with("/custom/path")


class TestGetAuthTokens:
    def test_returns_auth_tokens_object(self):
        ctx = MagicMock()
        ctx.obj = None

        with patch("notebooklm.cli.helpers.load_auth_from_storage") as mock_load:
            mock_load.return_value = {"SID": "test_sid"}
            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf_token", "session_id")

                auth = get_auth_tokens(ctx)

        assert auth.cookies == {"SID": "test_sid"}
        assert auth.csrf_token == "csrf_token"
        assert auth.session_id == "session_id"


class TestRunAsync:
    def test_runs_coroutine_and_returns_result(self):
        async def sample_coro():
            return "result"

        result = run_async(sample_coro())
        assert result == "result"
