"""Tests for CLI helper functions."""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

import notebooklm.cli._encoding as encoding_module
from notebooklm import Artifact
from notebooklm.cli.helpers import (
    clear_context,
    cli_name_to_artifact_type,
    display_report,
    display_research_sources,
    get_artifact_type_display,
    get_auth_tokens,
    get_client,
    get_current_conversation,
    get_current_notebook,
    get_source_type_display,
    handle_auth_error,
    handle_error,
    import_with_retry,
    json_error_response,
    json_output_response,
    require_notebook,
    run_async,
    set_current_conversation,
    set_current_notebook,
    with_client,
)
from notebooklm.exceptions import NetworkError, RPCError, RPCTimeoutError
from notebooklm.types import ArtifactType

# =============================================================================
# ARTIFACT TYPE DISPLAY TESTS
# =============================================================================


def _make_artifact(
    artifact_type: int,
    variant: int | None = None,
    title: str = "Test Artifact",
) -> Artifact:
    """Helper to create Artifact for testing get_artifact_type_display.

    For report subtypes, pass appropriate title:
    - "Briefing Doc: ..." for briefing_doc
    - "Study Guide: ..." for study_guide
    - "Blog Post: ..." for blog_post
    """
    return Artifact(
        id="test-id",
        title=title,
        _artifact_type=artifact_type,
        _variant=variant,
        status=3,  # Completed
    )


class TestGetArtifactTypeDisplay:
    def test_audio_type(self):
        art = _make_artifact(1)
        assert get_artifact_type_display(art) == "🎧 Audio"

    def test_report_type(self):
        art = _make_artifact(2)
        assert get_artifact_type_display(art) == "📄 Report"

    def test_video_type(self):
        art = _make_artifact(3)
        assert get_artifact_type_display(art) == "🎬 Video"

    def test_quiz_type_without_variant(self):
        art = _make_artifact(4, variant=2)
        assert get_artifact_type_display(art) == "📝 Quiz"

    def test_quiz_type_with_variant_2(self):
        art = _make_artifact(4, variant=2)
        assert get_artifact_type_display(art) == "📝 Quiz"

    def test_flashcards_type_with_variant_1(self):
        art = _make_artifact(4, variant=1)
        assert get_artifact_type_display(art) == "🃏 Flashcards"

    def test_mind_map_type(self):
        art = _make_artifact(5)
        assert get_artifact_type_display(art) == "🧠 Mind Map"

    def test_infographic_type(self):
        art = _make_artifact(7)
        assert get_artifact_type_display(art) == "🖼️ Infographic"

    def test_slide_deck_type(self):
        art = _make_artifact(8)
        assert get_artifact_type_display(art) == "📊 Slide Deck"

    def test_data_table_type(self):
        art = _make_artifact(9)
        assert get_artifact_type_display(art) == "📈 Data Table"

    @pytest.mark.filterwarnings("ignore::notebooklm.types.UnknownTypeWarning")
    def test_unknown_type(self):
        art = _make_artifact(999)
        # Unknown types return "Unknown (<kind>)" format
        display = get_artifact_type_display(art)
        assert "Unknown" in display

    def test_report_subtype_briefing_doc(self):
        # report_subtype is computed from title
        art = _make_artifact(2, title="Briefing Doc: Test Topic")
        assert get_artifact_type_display(art) == "📋 Briefing Doc"

    def test_report_subtype_study_guide(self):
        art = _make_artifact(2, title="Study Guide: Test Topic")
        assert get_artifact_type_display(art) == "📚 Study Guide"

    def test_report_subtype_blog_post(self):
        art = _make_artifact(2, title="Blog Post: Test Topic")
        assert get_artifact_type_display(art) == "✍️ Blog Post"

    def test_report_subtype_generic(self):
        art = _make_artifact(2, title="Report: Test Topic")
        assert get_artifact_type_display(art) == "📄 Report"

    def test_report_subtype_unknown(self):
        """Unknown report subtype should return default Report"""
        art = _make_artifact(2, title="Some Random Title")
        assert get_artifact_type_display(art) == "📄 Report"


class TestGetSourceTypeDisplay:
    def test_youtube(self):
        assert get_source_type_display("youtube") == "🎬 YouTube"

    def test_web_page(self):
        assert get_source_type_display("web_page") == "🌐 Web Page"

    def test_pdf(self):
        assert get_source_type_display("pdf") == "📄 PDF"

    def test_markdown(self):
        assert get_source_type_display("markdown") == "📝 Markdown"

    def test_google_spreadsheet(self):
        assert get_source_type_display("google_spreadsheet") == "📊 Google Sheets"

    def test_csv(self):
        assert get_source_type_display("csv") == "📊 CSV"

    def test_google_drive_audio(self):
        assert get_source_type_display("google_drive_audio") == "🎧 Drive Audio"

    def test_google_drive_video(self):
        assert get_source_type_display("google_drive_video") == "🎬 Drive Video"

    def test_docx(self):
        assert get_source_type_display("docx") == "📝 DOCX"

    def test_pasted_text(self):
        assert get_source_type_display("pasted_text") == "📝 Pasted Text"

    def test_epub(self):
        assert get_source_type_display("epub") == "📕 EPUB"

    def test_unknown_type(self):
        assert get_source_type_display("unknown") == "❓ Unknown"

    def test_unrecognized_type_shows_name(self):
        # Unrecognized types should show the type name
        assert get_source_type_display("future_type") == "❓ future_type"


class TestCliNameToArtifactType:
    def test_audio(self):
        assert cli_name_to_artifact_type("audio") == ArtifactType.AUDIO

    def test_video(self):
        assert cli_name_to_artifact_type("video") == ArtifactType.VIDEO

    def test_slide_deck(self):
        assert cli_name_to_artifact_type("slide-deck") == ArtifactType.SLIDE_DECK

    def test_quiz(self):
        assert cli_name_to_artifact_type("quiz") == ArtifactType.QUIZ

    def test_flashcard_alias(self):
        # CLI uses singular "flashcard", maps to ArtifactType.FLASHCARDS
        assert cli_name_to_artifact_type("flashcard") == ArtifactType.FLASHCARDS

    def test_mind_map(self):
        assert cli_name_to_artifact_type("mind-map") == ArtifactType.MIND_MAP

    def test_infographic(self):
        assert cli_name_to_artifact_type("infographic") == ArtifactType.INFOGRAPHIC

    def test_data_table(self):
        assert cli_name_to_artifact_type("data-table") == ArtifactType.DATA_TABLE

    def test_report(self):
        assert cli_name_to_artifact_type("report") == ArtifactType.REPORT

    def test_all_returns_none(self):
        assert cli_name_to_artifact_type("all") is None

    def test_invalid_type_raises_keyerror(self):
        with pytest.raises(KeyError):
            cli_name_to_artifact_type("invalid-type")


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

    def test_json_output_response_preserves_unicode(self, capsys):
        """CJK / emoji characters should be emitted as real UTF-8, not \\uXXXX."""
        json_output_response({"title": "中文笔记本", "emoji": "🚀"})

        captured = capsys.readouterr()
        # Round-trip must still parse.
        data = json.loads(captured.out)
        assert data["title"] == "中文笔记本"
        assert data["emoji"] == "🚀"
        # Raw output must contain real CJK chars, not escaped sequences.
        assert "中文笔记本" in captured.out
        assert "🚀" in captured.out
        assert "\\u" not in captured.out


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

    def test_json_error_response_preserves_unicode(self, capsys):
        """Error messages with CJK / emoji should be emitted as real UTF-8."""
        with pytest.raises(SystemExit):
            json_error_response("ERROR", "笔记本不存在 🚫", extra={"title": "中文"})

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["message"] == "笔记本不存在 🚫"
        assert data["title"] == "中文"
        assert "笔记本不存在" in captured.out
        assert "🚫" in captured.out
        assert "中文" in captured.out
        assert "\\u" not in captured.out

    def test_json_error_response_serializes_path_in_extra(self, capsys):
        """Non-primitive values like pathlib.Path must not crash the error reporter."""
        with pytest.raises(SystemExit):
            json_error_response("ERROR", "Bad path", extra={"path": Path("tmp_test_path")})

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["error"] is True
        assert data["code"] == "ERROR"
        assert data["message"] == "Bad path"
        assert data["path"] == str(Path("tmp_test_path"))


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

    def test_clear_context_preserves_account_metadata(self, tmp_path):
        context_file = tmp_path / "context.json"
        context_file.write_text(
            json.dumps(
                {
                    "notebook_id": "test",
                    "conversation_id": "conv",
                    "account": {"authuser": 1, "email": "bob@example.com"},
                }
            )
        )
        with patch("notebooklm.cli.helpers.get_context_path", return_value=context_file):
            assert clear_context() is True

        assert json.loads(context_file.read_text()) == {
            "account": {"authuser": 1, "email": "bob@example.com"}
        }

    def test_clear_context_can_remove_account_metadata(self, tmp_path):
        context_file = tmp_path / "context.json"
        context_file.write_text(
            json.dumps({"account": {"authuser": 1, "email": "bob@example.com"}})
        )
        with patch("notebooklm.cli.helpers.get_context_path", return_value=context_file):
            assert clear_context(clear_account=True) is True

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

    def test_set_current_notebook_clears_conversation_on_switch(self, tmp_path):
        context_file = tmp_path / "context.json"
        context_file.write_text('{"notebook_id": "nb_old", "conversation_id": "conv_1"}')
        with patch("notebooklm.cli.helpers.get_context_path", return_value=context_file):
            set_current_notebook("nb_new", title="New Notebook")
            data = json.loads(context_file.read_text())
            assert data["notebook_id"] == "nb_new"
            assert "conversation_id" not in data

    def test_set_current_notebook_preserves_account_metadata(self, tmp_path):
        context_file = tmp_path / "context.json"
        context_file.write_text(
            json.dumps({"account": {"authuser": 1, "email": "bob@example.com"}})
        )
        with patch("notebooklm.cli.helpers.get_context_path", return_value=context_file):
            set_current_notebook("nb_new", title="New Notebook")

        data = json.loads(context_file.read_text())
        assert data["notebook_id"] == "nb_new"
        assert data["account"] == {"authuser": 1, "email": "bob@example.com"}


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

    def test_falls_back_when_console_cannot_encode_error(self):
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

        with (
            patch("notebooklm.cli.helpers.console") as mock_console,
            patch("notebooklm.cli._encoding.click.echo", side_effect=flaky_echo),
            patch.object(encoding_module.sys, "stderr", DummyStderr()),
        ):
            mock_console.print.side_effect = UnicodeEncodeError(
                "cp950",
                "Error: broken 🌐",
                14,
                15,
                "illegal multibyte sequence",
            )

            with pytest.raises(SystemExit) as exc_info:
                handle_error(ValueError("broken 🌐"))

        assert exc_info.value.code == 1
        assert calls == [("Error: broken 🌐", True), ("Error: broken ?", True)]


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


class TestDisplayReport:
    def test_prints_markdown_as_literal_text(self):
        report = "See [NotebookLM](https://example.com) and [1]"

        with patch("notebooklm.cli.helpers.console") as mock_console:
            display_report(report, max_chars=1000)

        assert mock_console.print.call_count == 2
        assert mock_console.print.call_args_list[0].args[0] == "\n[bold]Report:[/bold]"
        assert mock_console.print.call_args_list[1].args[0] == report
        assert mock_console.print.call_args_list[1].kwargs["markup"] is False

    def test_truncates_report_and_shows_json_hint(self):
        report = "abcdef"

        with patch("notebooklm.cli.helpers.console") as mock_console:
            display_report(report, max_chars=3, json_hint=True)

        assert mock_console.print.call_count == 3
        assert mock_console.print.call_args_list[1].args[0] == "abc"
        assert mock_console.print.call_args_list[1].kwargs["markup"] is False
        assert mock_console.print.call_args_list[2].args[0] == (
            "[dim]... (truncated, use --json for full report)[/dim]"
        )

    def test_truncates_report_without_json_hint(self):
        report = "abcdef"

        with patch("notebooklm.cli.helpers.console") as mock_console:
            display_report(report, max_chars=3, json_hint=False)

        assert mock_console.print.call_args_list[2].args[0] == "[dim]... (truncated)[/dim]"


class TestDisplayResearchSources:
    def test_shows_string_result_type_labels(self):
        sources = [
            {"title": "Web Result", "url": "https://example.com", "result_type": "web"},
            {"title": "Drive Result", "url": "https://drive.example.com", "result_type": "drive"},
        ]

        with patch("notebooklm.cli.helpers.console") as mock_console:
            display_research_sources(sources)

        assert mock_console.print.call_count == 2
        table = mock_console.print.call_args_list[1].args[0]
        columns = [column.header for column in table.columns]
        assert columns == ["Title", "Type", "URL"]
        type_cells = table.columns[1]._cells
        assert type_cells == ["Web", "Drive"]


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
            mock_load.return_value = {"SID": "test", "__Secure-1PSIDTS": "test_1psidts"}
            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
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

    def test_decorator_file_not_found_in_command_not_treated_as_auth_error(self):
        """Test that FileNotFoundError from command logic is NOT treated as auth error.

        Regression test for GitHub issue #153: `source add --type file` with a
        missing file was incorrectly showing 'Not logged in' because the
        with_client decorator caught all FileNotFoundError as auth errors.
        """
        import click
        from click.testing import CliRunner

        @click.command()
        @with_client
        def test_cmd(ctx, client_auth):
            async def _run():
                raise FileNotFoundError("File not found: /tmp/nonexistent.pdf")

            return _run()

        runner = CliRunner()
        with patch("notebooklm.cli.helpers.load_auth_from_storage") as mock_load:
            mock_load.return_value = {"SID": "test", "__Secure-1PSIDTS": "test_1psidts"}
            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(test_cmd)

        assert result.exit_code == 1
        # Should show the actual file error, NOT an auth error
        assert "File not found" in result.output
        assert "login" not in result.output.lower()

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
            mock_load.return_value = {"SID": "test", "__Secure-1PSIDTS": "test_1psidts"}
            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
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
            mock_load.return_value = {"SID": "test", "__Secure-1PSIDTS": "test_1psidts"}
            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
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
            mock_load.return_value = {"SID": "test_sid", "__Secure-1PSIDTS": "test_1psidts"}
            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf_token", "session_id")

                cookies, csrf, session = get_client(ctx)

        assert cookies == {"SID": "test_sid", "__Secure-1PSIDTS": "test_1psidts"}
        assert csrf == "csrf_token"
        assert session == "session_id"

    def test_uses_storage_path_from_context(self):
        ctx = MagicMock()
        ctx.obj = {"storage_path": "/custom/path"}

        with patch("notebooklm.cli.helpers.load_auth_from_storage") as mock_load:
            mock_load.return_value = {"SID": "test", "__Secure-1PSIDTS": "test_1psidts"}
            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")

                get_client(ctx)

        mock_load.assert_called_once_with("/custom/path")


class TestGetAuthTokens:
    def test_returns_auth_tokens_object(self):
        ctx = MagicMock()
        ctx.obj = None

        with patch("notebooklm.cli.helpers.load_auth_from_storage") as mock_load:
            mock_load.return_value = {"SID": "test_sid", "__Secure-1PSIDTS": "test_1psidts"}
            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf_token", "session_id")

                auth = get_auth_tokens(ctx)

        assert auth.cookies == {
            ("SID", ".google.com", "/"): "test_sid",
            ("__Secure-1PSIDTS", ".google.com", "/"): "test_1psidts",
        }
        assert auth.flat_cookies == {"SID": "test_sid", "__Secure-1PSIDTS": "test_1psidts"}
        assert auth.csrf_token == "csrf_token"
        assert auth.session_id == "session_id"

    def test_explicit_storage_path_overrides_auth_json_cookie_jar(self, tmp_path, monkeypatch):
        storage_path = tmp_path / "storage_state.json"
        ctx = MagicMock()
        ctx.obj = {"storage_path": storage_path, "profile": None}
        monkeypatch.setenv(
            "NOTEBOOKLM_AUTH_JSON",
            json.dumps(
                {
                    "cookies": [
                        {"name": "SID", "value": "env", "domain": ".google.com"},
                        {
                            "name": "__Secure-1PSIDTS",
                            "value": "test_1psidts",
                            "domain": ".google.com",
                        },
                    ]
                }
            ),
        )

        with (
            patch("notebooklm.cli.helpers.load_auth_from_storage") as mock_load,
            patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch,
            patch("notebooklm.auth.build_httpx_cookies_from_storage") as mock_env_jar,
            patch("notebooklm.cli.helpers.build_cookie_jar") as mock_build_jar,
        ):
            mock_load.return_value = {"SID": "file", "__Secure-1PSIDTS": "test_1psidts"}
            mock_fetch.return_value = ("csrf", "session")
            mock_build_jar.return_value = httpx.Cookies()

            auth = get_auth_tokens(ctx)

        mock_env_jar.assert_not_called()
        mock_build_jar.assert_called_once_with(
            cookies={"SID": "file", "__Secure-1PSIDTS": "test_1psidts"}, storage_path=storage_path
        )
        assert auth.storage_path == storage_path


class TestRunAsync:
    def test_runs_coroutine_and_returns_result(self):
        async def sample_coro():
            return "result"

        result = run_async(sample_coro())
        assert result == "result"


class TestImportWithRetry:
    @pytest.mark.asyncio
    async def test_retries_rpc_timeout_then_succeeds(self):
        # Empty baseline + empty post-timeout probe → verification fails →
        # falls through to legacy retry. This exercises the retry path
        # explicitly rather than relying on a snapshot exception.
        client = MagicMock()
        client.sources.list = AsyncMock(return_value=[])
        client.research.import_sources = AsyncMock(
            side_effect=[
                RPCTimeoutError("Timed out", timeout_seconds=30.0),
                [{"id": "src_1", "title": "Source 1"}],
            ]
        )

        with (
            patch("notebooklm.cli.helpers.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
            patch("notebooklm.cli.helpers.console") as mock_console,
        ):
            imported = await import_with_retry(
                client,
                "nb_123",
                "task_123",
                [{"url": "https://example.com", "title": "Source 1"}],
                initial_delay=5,
                max_delay=60,
            )

        assert imported == [{"id": "src_1", "title": "Source 1"}]
        assert client.research.import_sources.await_count == 2
        mock_sleep.assert_awaited_once_with(5)
        mock_console.print.assert_called_once()

    @pytest.mark.asyncio
    async def test_retries_silently_for_json_output(self):
        client = MagicMock()
        client.sources.list = AsyncMock(return_value=[])
        client.research.import_sources = AsyncMock(
            side_effect=[
                RPCTimeoutError("Timed out", timeout_seconds=30.0),
                [],
            ]
        )

        with (
            patch("notebooklm.cli.helpers.asyncio.sleep", new_callable=AsyncMock),
            patch("notebooklm.cli.helpers.console") as mock_console,
        ):
            await import_with_retry(
                client,
                "nb_123",
                "task_123",
                [{"url": "https://example.com", "title": "Source 1"}],
                json_output=True,
            )

        mock_console.print.assert_not_called()

    @pytest.mark.asyncio
    async def test_raises_after_elapsed_budget(self):
        client = MagicMock()
        client.sources.list = AsyncMock(return_value=[])
        error = RPCTimeoutError("Timed out", timeout_seconds=30.0)
        client.research.import_sources = AsyncMock(side_effect=error)

        # time.monotonic is read once at start, then on each timeout. We need
        # enough values to cover the snapshot path plus the timeout-handling
        # path (elapsed check). Past-budget on second read forces the raise.
        with (
            patch(
                "notebooklm.cli.helpers.time.monotonic",
                side_effect=[0.0, 1801.0],
            ),
            patch("notebooklm.cli.helpers.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
            pytest.raises(RPCTimeoutError),
        ):
            await import_with_retry(
                client,
                "nb_123",
                "task_123",
                [{"url": "https://example.com", "title": "Source 1"}],
                max_elapsed=1800,
            )

        mock_sleep.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_does_not_retry_non_timeout_error(self):
        client = MagicMock()
        client.sources.list = AsyncMock(return_value=[])
        client.research.import_sources = AsyncMock(side_effect=ValueError("boom"))

        with (
            patch("notebooklm.cli.helpers.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
            pytest.raises(ValueError, match="boom"),
        ):
            await import_with_retry(
                client,
                "nb_123",
                "task_123",
                [{"url": "https://example.com", "title": "Source 1"}],
            )

        assert client.research.import_sources.await_count == 1
        mock_sleep.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_retry_when_server_state_shows_import_succeeded(self):
        """If the import RPC times out but sources.list shows our URLs were
        added server-side, treat it as success and skip retry. This avoids
        the duplicate-on-retry inflation that otherwise multiplies sources by
        the retry count.
        """
        # Two pre-existing sources, then after the timed-out import the same
        # two plus the URL we just tried to import.
        baseline_src = MagicMock(id="src_pre", title="Pre-existing", url="https://pre.example.com")
        new_src = MagicMock(id="src_new", title="Source 1", url="https://example.com")
        client = MagicMock()
        client.sources.list = AsyncMock(
            side_effect=[
                [baseline_src],  # snapshot before import
                [baseline_src, new_src],  # probe after timeout — URL is now there
            ]
        )
        client.research.import_sources = AsyncMock(
            side_effect=RPCTimeoutError("Timed out", timeout_seconds=30.0)
        )

        with (
            patch("notebooklm.cli.helpers.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
            patch("notebooklm.cli.helpers.console") as mock_console,
        ):
            imported = await import_with_retry(
                client,
                "nb_123",
                "task_123",
                [{"url": "https://example.com", "title": "Source 1"}],
            )

        assert imported == [{"id": "src_new", "title": "Source 1"}]
        # Single import attempt — no retry.
        assert client.research.import_sources.await_count == 1
        # Snapshot + post-timeout probe — exactly two sources.list calls.
        assert client.sources.list.await_count == 2
        # No sleep, no retry — straight to verified-success exit.
        mock_sleep.assert_not_awaited()
        # One console print: the verified-success notice.
        assert mock_console.print.call_count == 1

    @pytest.mark.asyncio
    async def test_skips_retry_when_url_normalization_matches(self):
        """Server-side URL normalization (case folding, trailing-slash strip)
        is handled by normalizing both sides before the subset check, so a
        cosmetic difference between request and stored URL doesn't force a
        duplicating retry.
        """
        # Server stored a normalized URL (no trailing slash, lowercased).
        new_src = MagicMock(id="src_new", title="Source 1", url="https://example.com")
        client = MagicMock()
        client.sources.list = AsyncMock(
            side_effect=[
                [],  # empty baseline
                [new_src],  # post-timeout — one new source visible
            ]
        )
        client.research.import_sources = AsyncMock(
            side_effect=RPCTimeoutError("Timed out", timeout_seconds=30.0)
        )

        with (
            patch("notebooklm.cli.helpers.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
            patch("notebooklm.cli.helpers.console"),
        ):
            imported = await import_with_retry(
                client,
                "nb_123",
                "task_123",
                # Trailing slash + uppercase host differ from server-normalized form.
                [{"url": "https://Example.com/", "title": "Source 1"}],
            )

        assert imported == [{"id": "src_new", "title": "Source 1"}]
        assert client.research.import_sources.await_count == 1
        mock_sleep.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_retries_when_server_state_shows_no_progress(self):
        """If sources.list shows the requested URLs were NOT imported, fall
        back to the original retry behavior.
        """
        client = MagicMock()
        client.sources.list = AsyncMock(return_value=[])  # always empty
        client.research.import_sources = AsyncMock(
            side_effect=[
                RPCTimeoutError("Timed out", timeout_seconds=30.0),
                [{"id": "src_1", "title": "Source 1"}],
            ]
        )

        with (
            patch("notebooklm.cli.helpers.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
            patch("notebooklm.cli.helpers.console"),
        ):
            imported = await import_with_retry(
                client,
                "nb_123",
                "task_123",
                [{"url": "https://example.com", "title": "Source 1"}],
                initial_delay=5,
            )

        assert imported == [{"id": "src_1", "title": "Source 1"}]
        assert client.research.import_sources.await_count == 2
        mock_sleep.assert_awaited_once_with(5)

    @pytest.mark.asyncio
    async def test_partial_timeout_retries_only_missing_urls(self):
        """If a timed-out import partially committed URLs, the retry payload
        must drop already-visible URLs to avoid duplicating them.
        """
        imported_src = MagicMock(id="src_1", title="Source 1", url="https://one.example.com")
        sources = [
            {"url": "https://one.example.com", "title": "Source 1"},
            {"url": "https://two.example.com", "title": "Source 2"},
            {"url": "https://three.example.com", "title": "Source 3"},
        ]
        client = MagicMock()
        client.sources.list = AsyncMock(
            side_effect=[
                [],  # baseline
                [imported_src],  # post-timeout probe — 1 of 3 is visible
            ]
        )
        client.research.import_sources = AsyncMock(
            side_effect=[
                RPCTimeoutError("Timed out", timeout_seconds=30.0),
                [{"id": "src_2", "title": "Source 2"}, {"id": "src_3", "title": "Source 3"}],
            ]
        )

        with (
            patch("notebooklm.cli.helpers.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
            patch("notebooklm.cli.helpers.console"),
        ):
            imported = await import_with_retry(
                client,
                "nb_123",
                "task_123",
                sources,
                initial_delay=5,
            )

        assert imported == [
            {"id": "src_1", "title": "Source 1"},
            {"id": "src_2", "title": "Source 2"},
            {"id": "src_3", "title": "Source 3"},
        ]
        assert client.research.import_sources.await_count == 2
        first_call_sources = client.research.import_sources.await_args_list[0].args[2]
        retry_call_sources = client.research.import_sources.await_args_list[1].args[2]
        assert first_call_sources == sources
        assert retry_call_sources == [
            {"url": "https://two.example.com", "title": "Source 2"},
            {"url": "https://three.example.com", "title": "Source 3"},
        ]
        mock_sleep.assert_awaited_once_with(5)

    @pytest.mark.asyncio
    async def test_partial_timeout_preserves_report_entries_for_retry(self):
        """Filtering URL entries that are already visible must leave no-URL
        report entries in the retry payload.
        """
        imported_src = MagicMock(id="src_1", title="Source 1", url="https://one.example.com")
        report_entry = {
            "title": "Research Report",
            "report_markdown": "# Findings\n...",
            "result_type": 5,
        }
        sources = [
            {"url": "https://one.example.com", "title": "Source 1"},
            {"url": "https://two.example.com", "title": "Source 2"},
            report_entry,
        ]
        client = MagicMock()
        client.sources.list = AsyncMock(
            side_effect=[
                [],  # baseline
                [imported_src],  # post-timeout probe — URL 1 is visible
            ]
        )
        client.research.import_sources = AsyncMock(
            side_effect=[
                RPCTimeoutError("Timed out", timeout_seconds=30.0),
                [{"id": "src_2", "title": "Source 2"}, {"id": "src_report", "title": "Report"}],
            ]
        )

        with (
            patch("notebooklm.cli.helpers.asyncio.sleep", new_callable=AsyncMock),
            patch("notebooklm.cli.helpers.console"),
        ):
            imported = await import_with_retry(
                client,
                "nb_123",
                "task_123",
                sources,
                initial_delay=5,
            )

        assert imported == [
            {"id": "src_1", "title": "Source 1"},
            {"id": "src_2", "title": "Source 2"},
            {"id": "src_report", "title": "Report"},
        ]
        retry_call_sources = client.research.import_sources.await_args_list[1].args[2]
        assert retry_call_sources == [
            {"url": "https://two.example.com", "title": "Source 2"},
            report_entry,
        ]

    @pytest.mark.asyncio
    async def test_partial_timeout_merges_prior_verified_sources_on_later_verified_success(self):
        """When multiple timeouts happen, later verified-success returns must
        include sources verified during earlier partial probes.
        """
        source_1 = MagicMock(id="src_1", title="Source 1", url="https://one.example.com")
        source_2 = MagicMock(id="src_2", title="Source 2", url="https://two.example.com")
        sources = [
            {"url": "https://one.example.com", "title": "Source 1"},
            {"url": "https://two.example.com", "title": "Source 2"},
        ]
        client = MagicMock()
        client.sources.list = AsyncMock(
            side_effect=[
                [],  # baseline
                [source_1],  # first timeout — only URL 1 is visible, so retry URL 2
                [source_1, source_2],  # second timeout — URL 2 is now visible
            ]
        )
        client.research.import_sources = AsyncMock(
            side_effect=[
                RPCTimeoutError("Timed out", timeout_seconds=30.0),
                RPCTimeoutError("Timed out", timeout_seconds=30.0),
            ]
        )

        with (
            patch("notebooklm.cli.helpers.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
            patch("notebooklm.cli.helpers.console"),
        ):
            imported = await import_with_retry(
                client,
                "nb_123",
                "task_123",
                sources,
                initial_delay=5,
            )

        assert imported == [
            {"id": "src_1", "title": "Source 1"},
            {"id": "src_2", "title": "Source 2"},
        ]
        assert client.research.import_sources.await_count == 2
        retry_call_sources = client.research.import_sources.await_args_list[1].args[2]
        assert retry_call_sources == [{"url": "https://two.example.com", "title": "Source 2"}]
        mock_sleep.assert_awaited_once_with(5)

    @pytest.mark.asyncio
    async def test_snapshot_failure_deduplicates_retries_without_verified_success(self):
        """A malformed pre-import snapshot must not masquerade as an empty
        notebook. Without a reliable baseline we can still drop URLs already
        visible after a timeout, but we must not classify all current rows as
        newly imported by this call.
        """
        source_1 = MagicMock(id="src_1", title="Source 1", url="https://one.example.com")
        source_2 = MagicMock(id="src_2", title="Source 2", url="https://two.example.com")
        source_3 = MagicMock(id="src_3", title="Source 3", url="https://three.example.com")
        sources = [
            {"url": "https://one.example.com", "title": "Source 1"},
            {"url": "https://two.example.com", "title": "Source 2"},
            {"url": "https://three.example.com", "title": "Source 3"},
        ]
        client = MagicMock()
        client.sources.list = AsyncMock(
            side_effect=[
                RPCError("snapshot unavailable"),
                [source_1],
                [source_1, source_2, source_3],
            ]
        )
        client.research.import_sources = AsyncMock(
            side_effect=[
                RPCTimeoutError("Timed out", timeout_seconds=30.0),
                RPCTimeoutError("Timed out", timeout_seconds=30.0),
            ]
        )

        with (
            patch("notebooklm.cli.helpers.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
            patch("notebooklm.cli.helpers.console"),
        ):
            imported = await import_with_retry(
                client,
                "nb_123",
                "task_123",
                sources,
                initial_delay=5,
            )

        assert imported == []
        assert client.research.import_sources.await_count == 2
        assert client.sources.list.await_count == 3
        assert all(
            awaited_call.kwargs.get("strict") is True
            for awaited_call in client.sources.list.await_args_list
        )
        assert client.research.import_sources.await_args_list[0].args[2] == sources
        assert client.research.import_sources.await_args_list[1].args[2] == [
            {"url": "https://two.example.com", "title": "Source 2"},
            {"url": "https://three.example.com", "title": "Source 3"},
        ]
        mock_sleep.assert_awaited_once_with(5)

    @pytest.mark.asyncio
    async def test_partial_timeout_skips_retry_when_filter_removes_all_sources(self):
        """If every requested URL is already visible after the timeout, there
        is nothing left to retry.
        """
        existing_src = MagicMock(id="src_existing", title="Old", url="https://example.com")
        client = MagicMock()
        client.sources.list = AsyncMock(
            side_effect=[
                [existing_src],  # baseline already has the URL
                [existing_src],  # post-timeout probe still shows it
            ]
        )
        client.research.import_sources = AsyncMock(
            side_effect=RPCTimeoutError("Timed out", timeout_seconds=30.0)
        )

        with (
            patch("notebooklm.cli.helpers.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
            patch("notebooklm.cli.helpers.console") as mock_console,
        ):
            imported = await import_with_retry(
                client,
                "nb_123",
                "task_123",
                [{"url": "https://example.com", "title": "Old (request)"}],
                initial_delay=5,
            )

        assert imported == []
        assert client.research.import_sources.await_count == 1
        mock_sleep.assert_not_awaited()
        assert mock_console.print.call_count == 1

    @pytest.mark.asyncio
    async def test_retries_when_pre_existing_url_meets_concurrent_unrelated_addition(
        self,
    ):
        """Combined edge case: the requested URL was already in the notebook
        before the import, AND a concurrent session added an unrelated source
        during the timeout window. The verified-success branch must NOT fire
        — neither the pre-existing URL nor the unrelated addition is proof
        our import wrote anything. The retry payload filter should still avoid
        re-adding the requested URL because it is already present.
        """
        existing_src = MagicMock(id="src_existing", title="Old", url="https://example.com")
        unrelated_src = MagicMock(
            id="src_unrelated",
            title="Unrelated (concurrent)",
            url="https://other.example.com",
        )
        client = MagicMock()
        client.sources.list = AsyncMock(
            side_effect=[
                [existing_src],  # baseline already has the requested URL
                # post-timeout: pre-existing + unrelated concurrent addition,
                # but no truly-new source matching the requested URL.
                [existing_src, unrelated_src],
            ]
        )
        client.research.import_sources = AsyncMock(
            side_effect=[
                RPCTimeoutError("Timed out", timeout_seconds=30.0),
                [{"id": "src_existing", "title": "Old"}],
            ]
        )

        with (
            patch("notebooklm.cli.helpers.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
            patch("notebooklm.cli.helpers.console"),
        ):
            imported = await import_with_retry(
                client,
                "nb_123",
                "task_123",
                [{"url": "https://example.com", "title": "Old (request)"}],
                initial_delay=5,
            )

        assert imported == []
        assert client.research.import_sources.await_count == 1
        mock_sleep.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_returned_list_includes_non_url_sources_like_research_reports(self):
        """When the request includes a research-report entry (no URL, only
        title + ``report_markdown``), the verified-success return value must
        surface the matching new no-URL source so callers can count it as
        imported.
        """
        # A new research-report entry with no URL.
        report_src = MagicMock(id="src_report", title="Research Report", url=None)
        # And a new URL-bearing source.
        new_src = MagicMock(id="src_new", title="Source 1", url="https://example.com")
        client = MagicMock()
        client.sources.list = AsyncMock(
            side_effect=[
                [],  # empty baseline
                [report_src, new_src],  # both new after the timeout
            ]
        )
        client.research.import_sources = AsyncMock(
            side_effect=RPCTimeoutError("Timed out", timeout_seconds=30.0)
        )

        with (
            patch("notebooklm.cli.helpers.asyncio.sleep", new_callable=AsyncMock),
            patch("notebooklm.cli.helpers.console"),
        ):
            imported = await import_with_retry(
                client,
                "nb_123",
                "task_123",
                [
                    # Mixed request: one URL + one report entry.
                    {"url": "https://example.com", "title": "Source 1"},
                    {
                        "title": "Research Report",
                        "report_markdown": "# Findings\n...",
                        "result_type": 5,
                    },
                ],
            )

        # Both sources are returned — the report (no URL) and the URL source.
        ids_returned = {entry["id"] for entry in imported}
        assert ids_returned == {"src_report", "src_new"}

    @pytest.mark.asyncio
    async def test_does_not_over_report_concurrent_no_url_source(self):
        """When the request has NO no-URL entries (URLs only), a concurrent
        no-URL source added during the timeout window must NOT be reported
        as imported — even if the requested URL itself was successfully
        written. Otherwise the caller's `len(imported)` overstates what this
        call actually added.
        """
        # The user's URL did import successfully.
        new_src = MagicMock(id="src_new", title="Source 1", url="https://example.com")
        # A concurrent session added a research report during the same window.
        concurrent_report = MagicMock(
            id="src_concurrent_report", title="Unrelated Report", url=None
        )
        client = MagicMock()
        client.sources.list = AsyncMock(
            side_effect=[
                [],  # empty baseline
                [new_src, concurrent_report],
            ]
        )
        client.research.import_sources = AsyncMock(
            side_effect=RPCTimeoutError("Timed out", timeout_seconds=30.0)
        )

        with (
            patch("notebooklm.cli.helpers.asyncio.sleep", new_callable=AsyncMock),
            patch("notebooklm.cli.helpers.console"),
        ):
            imported = await import_with_retry(
                client,
                "nb_123",
                "task_123",
                [{"url": "https://example.com", "title": "Source 1"}],
            )

        # Only the requested URL's source is returned; the concurrent report
        # is not part of this call's contribution.
        assert imported == [{"id": "src_new", "title": "Source 1"}]

    @pytest.mark.asyncio
    async def test_does_not_falsely_succeed_on_unrelated_concurrent_source(self):
        """Concurrent activity from another session (e.g. web UI, parallel CLI)
        can add unrelated sources during the import window. The verification
        condition must NOT fire on those — success must require the *requested*
        URLs to actually appear among the new sources, not just that the post-
        timeout source count grew.

        Without this guard, a real timeout coinciding with any concurrent
        addition would skip the retry and return the unrelated source as
        "imported" — silently losing the user's import.
        """
        unrelated_src = MagicMock(
            id="src_unrelated",
            title="Unrelated",
            url="https://other.example.com",
        )
        client = MagicMock()
        client.sources.list = AsyncMock(
            side_effect=[
                [],  # baseline: empty
                # Post-timeout: only the unrelated concurrent addition is
                # visible; our requested URL is NOT there.
                [unrelated_src],
            ]
        )
        client.research.import_sources = AsyncMock(
            side_effect=[
                RPCTimeoutError("Timed out", timeout_seconds=30.0),
                [{"id": "src_new", "title": "Source 1"}],
            ]
        )

        with (
            patch("notebooklm.cli.helpers.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
            patch("notebooklm.cli.helpers.console"),
        ):
            imported = await import_with_retry(
                client,
                "nb_123",
                "task_123",
                [{"url": "https://example.com", "title": "Source 1"}],
                initial_delay=5,
            )

        # Must retry, not falsely return the unrelated source.
        assert imported == [{"id": "src_new", "title": "Source 1"}]
        assert client.research.import_sources.await_count == 2
        mock_sleep.assert_awaited_once_with(5)

    @pytest.mark.asyncio
    async def test_does_not_falsely_succeed_on_pre_existing_requested_url(self):
        """If the requested URL was already in the notebook before the import
        and the post-timeout snapshot shows no truly-new source matching it,
        verification must NOT fire — even though `requested_urls.issubset(
        current_urls)` is trivially true. The retry filter then drops the
        already-present URL instead of re-adding a duplicate.
        """
        existing_src = MagicMock(id="src_existing", title="Old", url="https://example.com")
        client = MagicMock()
        client.sources.list = AsyncMock(
            side_effect=[
                [existing_src],  # baseline: already has the URL
                [existing_src],  # post-timeout: nothing changed
                [existing_src],  # post-retry probe (if reached)
            ]
        )
        client.research.import_sources = AsyncMock(
            side_effect=[
                RPCTimeoutError("Timed out", timeout_seconds=30.0),
                [{"id": "src_existing", "title": "Old"}],
            ]
        )

        with (
            patch("notebooklm.cli.helpers.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
            patch("notebooklm.cli.helpers.console"),
        ):
            imported = await import_with_retry(
                client,
                "nb_123",
                "task_123",
                [{"url": "https://example.com", "title": "Old (request)"}],
                initial_delay=5,
            )

        assert client.research.import_sources.await_count == 1
        mock_sleep.assert_not_awaited()
        assert imported == []

    @pytest.mark.asyncio
    async def test_report_only_import_bounded_retries_on_persistent_timeout(self):
        """Report-only deep-research imports (no URLs) can't use the URL-match
        verification path. To bound the worst-case duplicate inflation, the
        retry loop must give up after a small number of attempts rather than
        burning the full ``max_elapsed`` budget — otherwise a persistent
        timeout still produces 5-6x duplicate reports.

        Patches ``time.monotonic`` to never advance past budget, so the only
        thing that can bound the loop is an explicit retry cap on the
        no-URL path.
        """
        # All sources are report-only: no `url` field.
        client = MagicMock()
        client.sources.list = AsyncMock(return_value=[])
        client.research.import_sources = AsyncMock(
            side_effect=RPCTimeoutError("Timed out", timeout_seconds=30.0)
        )

        with (
            # Time budget never expires — only the retry cap can stop the loop.
            patch(
                "notebooklm.cli.helpers.time.monotonic",
                return_value=0.0,
            ),
            patch("notebooklm.cli.helpers.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
            patch("notebooklm.cli.helpers.console"),
            pytest.raises(RPCTimeoutError),
        ):
            await import_with_retry(
                client,
                "nb_123",
                "task_123",
                [
                    {
                        "title": "Research Report",
                        "report_markdown": "# Findings\n...",
                        "result_type": 5,
                    }
                ],
                initial_delay=1,
            )

        # Exactly 2 attempts (1 original + 1 retry) before raising. `<= 2`
        # would also pass if the retry disappeared entirely, which would mask
        # a regression — assert the cap and the single backoff sleep.
        assert client.research.import_sources.await_count == 2
        mock_sleep.assert_awaited_once_with(1)

    @pytest.mark.asyncio
    async def test_falls_back_to_retry_when_post_timeout_probe_raises(self):
        """If the post-timeout ``sources.list`` probe itself fails (transient
        network blip, server hiccup), the function must log and fall back to
        the legacy retry path rather than crashing or skipping verification
        silently.
        """
        new_src = MagicMock(id="src_new", title="Source 1", url="https://example.com")
        client = MagicMock()
        client.sources.list = AsyncMock(
            side_effect=[
                [],  # baseline
                NetworkError("probe down"),  # post-timeout probe fails
                [new_src],  # post-retry probe (would succeed if reached, unused)
            ]
        )
        client.research.import_sources = AsyncMock(
            side_effect=[
                RPCTimeoutError("Timed out", timeout_seconds=30.0),
                [{"id": "src_new", "title": "Source 1"}],
            ]
        )

        with (
            patch("notebooklm.cli.helpers.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
            patch("notebooklm.cli.helpers.console"),
        ):
            imported = await import_with_retry(
                client,
                "nb_123",
                "task_123",
                [{"url": "https://example.com", "title": "Source 1"}],
                initial_delay=5,
            )

        assert imported == [{"id": "src_new", "title": "Source 1"}]
        # Probe failure → legacy retry path → 2 import attempts.
        assert client.research.import_sources.await_count == 2
        mock_sleep.assert_awaited_once_with(5)

    @pytest.mark.asyncio
    async def test_verified_success_suppresses_console_output_when_json_output(self):
        """The verified-success branch's user-visible notice must respect the
        ``json_output`` flag — JSON consumers should not see human-readable
        text spliced into stdout.
        """
        new_src = MagicMock(id="src_new", title="Source 1", url="https://example.com")
        client = MagicMock()
        client.sources.list = AsyncMock(
            side_effect=[[], [new_src]],
        )
        client.research.import_sources = AsyncMock(
            side_effect=RPCTimeoutError("Timed out", timeout_seconds=30.0)
        )

        with (
            patch("notebooklm.cli.helpers.asyncio.sleep", new_callable=AsyncMock),
            patch("notebooklm.cli.helpers.console") as mock_console,
        ):
            imported = await import_with_retry(
                client,
                "nb_123",
                "task_123",
                [{"url": "https://example.com", "title": "Source 1"}],
                json_output=True,
            )

        assert imported == [{"id": "src_new", "title": "Source 1"}]
        mock_console.print.assert_not_called()

    @pytest.mark.asyncio
    async def test_snapshot_propagates_cancelled_error(self):
        """``asyncio.CancelledError`` from the pre-import snapshot must
        propagate so callers can cleanly cancel the operation. A bare
        ``except Exception`` would swallow it and continue running.
        """
        client = MagicMock()
        client.sources.list = AsyncMock(side_effect=asyncio.CancelledError())
        client.research.import_sources = AsyncMock()

        with pytest.raises(asyncio.CancelledError):
            await import_with_retry(
                client,
                "nb_123",
                "task_123",
                [{"url": "https://example.com", "title": "Source 1"}],
            )

        # The import should never run — cancellation aborted the snapshot.
        client.research.import_sources.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_probe_propagates_cancelled_error(self):
        """``asyncio.CancelledError`` from the post-timeout probe must
        propagate, not be swallowed and converted into a retry.
        """
        client = MagicMock()
        client.sources.list = AsyncMock(
            side_effect=[
                [],  # baseline OK
                asyncio.CancelledError(),  # probe cancelled
            ]
        )
        client.research.import_sources = AsyncMock(
            side_effect=RPCTimeoutError("Timed out", timeout_seconds=30.0)
        )

        with (
            patch("notebooklm.cli.helpers.asyncio.sleep", new_callable=AsyncMock),
            patch("notebooklm.cli.helpers.console"),
            pytest.raises(asyncio.CancelledError),
        ):
            await import_with_retry(
                client,
                "nb_123",
                "task_123",
                [{"url": "https://example.com", "title": "Source 1"}],
            )

        # Only the original attempt — no retry after cancellation.
        assert client.research.import_sources.await_count == 1


class TestGetAuthTokensAuthuser:
    """Regression for #359: get_auth_tokens must read authuser from context.json
    so RPC URLs route to the right Google account."""

    def test_authuser_from_context_json_propagates_to_authtokens(self, tmp_path):
        storage = tmp_path / "storage_state.json"
        storage.write_text(
            json.dumps(
                {
                    "cookies": [
                        {"name": "SID", "value": "x", "domain": ".google.com"},
                        {"name": "HSID", "value": "x", "domain": ".google.com"},
                        {"name": "SSID", "value": "x", "domain": ".google.com"},
                        {"name": "APISID", "value": "x", "domain": ".google.com"},
                        {"name": "SAPISID", "value": "x", "domain": ".google.com"},
                        {"name": "__Secure-1PSIDTS", "value": "x", "domain": ".google.com"},
                    ]
                }
            )
        )
        (tmp_path / "context.json").write_text(
            json.dumps({"account": {"authuser": 2, "email": "bob@example.com"}}),
            encoding="utf-8",
        )

        ctx = MagicMock()
        ctx.obj = {"storage_path": storage, "profile": None}

        token_fetch = object()
        with (
            patch("notebooklm.auth.fetch_tokens_with_domains", new=lambda *_, **__: token_fetch),
            patch(
                "notebooklm.cli.helpers.run_async",
                return_value=("csrf_v2", "sess_v2"),
            ),
        ):
            tokens = get_auth_tokens(ctx)

        assert tokens.authuser == 2
        assert tokens.csrf_token == "csrf_v2"

    def test_default_authuser_when_no_account_metadata(self, tmp_path):
        storage = tmp_path / "storage_state.json"
        storage.write_text(
            json.dumps(
                {
                    "cookies": [
                        {"name": "SID", "value": "x", "domain": ".google.com"},
                        {"name": "HSID", "value": "x", "domain": ".google.com"},
                        {"name": "SSID", "value": "x", "domain": ".google.com"},
                        {"name": "APISID", "value": "x", "domain": ".google.com"},
                        {"name": "SAPISID", "value": "x", "domain": ".google.com"},
                        {"name": "__Secure-1PSIDTS", "value": "x", "domain": ".google.com"},
                    ]
                }
            )
        )

        ctx = MagicMock()
        ctx.obj = {"storage_path": storage, "profile": None}

        token_fetch = object()
        with (
            patch("notebooklm.auth.fetch_tokens_with_domains", new=lambda *_, **__: token_fetch),
            patch(
                "notebooklm.cli.helpers.run_async",
                return_value=("csrf", "sess"),
            ),
        ):
            tokens = get_auth_tokens(ctx)

        assert tokens.authuser == 0
