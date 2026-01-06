"""Tests for CLI interface."""

import pytest
from click.testing import CliRunner
from unittest.mock import AsyncMock, patch, MagicMock
from datetime import datetime
from pathlib import Path

from notebooklm.notebooklm_cli import (
    cli,
    main,
    get_artifact_type_display,
    detect_source_type,
    ARTIFACT_TYPE_DISPLAY,
    ARTIFACT_TYPE_MAP,
)
from notebooklm.types import (
    Notebook,
    Source,
    Artifact,
    GenerationStatus,
    Note,
)


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def mock_auth():
    with patch("notebooklm.notebooklm_cli.load_auth_from_storage") as mock:
        mock.return_value = {
            "SID": "test",
            "HSID": "test",
            "SSID": "test",
            "APISID": "test",
            "SAPISID": "test",
        }
        yield mock


@pytest.fixture
def mock_client():
    """Create a fully mocked NotebookLMClient."""
    with patch("notebooklm.notebooklm_cli.NotebookLMClient") as mock_cls:
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)
        mock_cls.return_value = client
        yield client


@pytest.fixture
def mock_fetch_tokens():
    with patch("notebooklm.notebooklm_cli.fetch_tokens") as mock:
        mock.return_value = ("csrf_token", "session_id")
        yield mock


# =============================================================================
# Helper Function Tests
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


class TestDetectSourceType:
    def test_youtube_url(self):
        src = ["id", "Video Title", [None, None, None, None, None, None, None, ["https://youtube.com/watch?v=abc"]]]
        assert detect_source_type(src) == "🎥 YouTube"

    def test_youtu_be_url(self):
        src = ["id", "Video Title", [None, None, None, None, None, None, None, ["https://youtu.be/abc"]]]
        assert detect_source_type(src) == "🎥 YouTube"

    def test_web_url(self):
        src = ["id", "Web Page", [None, None, None, None, None, None, None, ["https://example.com/article"]]]
        assert detect_source_type(src) == "🔗 Web URL"

    def test_pdf_file(self):
        src = ["id", "document.pdf", [None, 12345]]
        assert detect_source_type(src) == "📄 PDF"

    def test_text_file(self):
        src = ["id", "notes.txt", [None, 1234]]
        assert detect_source_type(src) == "📝 Text File"

    def test_markdown_file(self):
        src = ["id", "readme.md", [None, 1234]]
        assert detect_source_type(src) == "📝 Text File"

    def test_spreadsheet_csv(self):
        src = ["id", "data.csv", [None, 1234]]
        assert detect_source_type(src) == "📊 Spreadsheet"

    def test_spreadsheet_xlsx(self):
        src = ["id", "data.xlsx", [None, 1234]]
        assert detect_source_type(src) == "📊 Spreadsheet"

    def test_uploaded_file_with_size(self):
        src = ["id", "Unknown File", [None, 5000]]
        assert detect_source_type(src) == "📎 Upload"

    def test_pasted_text(self):
        src = ["id", "Pasted Text", [None, 0]]
        assert detect_source_type(src) == "📝 Pasted Text"

    def test_empty_source(self):
        # Empty source defaults to pasted text behavior
        src = []
        assert detect_source_type(src) == "📝 Pasted Text"


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


class TestCLIBasics:
    def test_cli_exists(self, runner):
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "NotebookLM" in result.output

    def test_version_flag(self, runner):
        result = runner.invoke(cli, ["--version"])
        assert result.exit_code == 0
        assert "0.1.0" in result.output

    def test_command_groups_shown(self, runner):
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "notebook" in result.output
        assert "source" in result.output
        assert "artifact" in result.output
        assert "generate" in result.output
        assert "download" in result.output
        assert "note" in result.output


class TestListNotebooks:
    def test_list_command_exists(self, runner):
        result = runner.invoke(cli, ["list", "--help"])
        assert result.exit_code == 0

    def test_notebook_list_command_exists(self, runner):
        result = runner.invoke(cli, ["notebook", "list", "--help"])
        assert result.exit_code == 0

    def test_list_notebooks(self, runner, mock_auth):
        with patch("notebooklm.notebooklm_cli.NotebookLMClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.notebooks.list = AsyncMock(
                return_value=[
                    Notebook(id="nb_001", title="First Notebook", created_at=datetime(2024, 1, 1)),
                    Notebook(id="nb_002", title="Second Notebook", created_at=datetime(2024, 1, 2)),
                ]
            )
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.notebooklm_cli.fetch_tokens") as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["list"])

            assert result.exit_code == 0
            assert "First Notebook" in result.output or "nb_001" in result.output


class TestCreateNotebook:
    def test_create_command_exists(self, runner):
        result = runner.invoke(cli, ["create", "--help"])
        assert result.exit_code == 0
        assert "TITLE" in result.output

    def test_notebook_create_command_exists(self, runner):
        result = runner.invoke(cli, ["notebook", "create", "--help"])
        assert result.exit_code == 0
        assert "TITLE" in result.output

    def test_create_notebook(self, runner, mock_auth):
        with patch("notebooklm.notebooklm_cli.NotebookLMClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.notebooks.create = AsyncMock(
                return_value=Notebook(id="nb_new", title="My Research", created_at=datetime(2024, 1, 1))
            )
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.notebooklm_cli.fetch_tokens") as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["create", "My Research"])

            assert result.exit_code == 0


class TestSourceGroup:
    def test_source_group_exists(self, runner):
        result = runner.invoke(cli, ["source", "--help"])
        assert result.exit_code == 0
        assert "list" in result.output
        assert "add" in result.output
        assert "delete" in result.output

    def test_source_add_command_exists(self, runner):
        result = runner.invoke(cli, ["source", "add", "--help"])
        assert result.exit_code == 0
        assert "CONTENT" in result.output
        assert "--type" in result.output
        assert "--notebook" in result.output or "-n" in result.output

    def test_source_list_command_exists(self, runner):
        result = runner.invoke(cli, ["source", "list", "--help"])
        assert result.exit_code == 0
        assert "--notebook" in result.output or "-n" in result.output


class TestGenerateGroup:
    def test_generate_group_exists(self, runner):
        result = runner.invoke(cli, ["generate", "--help"])
        assert result.exit_code == 0
        assert "audio" in result.output
        assert "video" in result.output
        assert "quiz" in result.output

    def test_generate_audio_command_exists(self, runner):
        result = runner.invoke(cli, ["generate", "audio", "--help"])
        assert result.exit_code == 0
        # Description is now the primary positional argument (optional)
        assert "DESCRIPTION" in result.output
        assert "--notebook" in result.output or "-n" in result.output

    def test_generate_audio_with_description_arg(self, runner):
        # Instructions are now passed via the description positional argument
        result = runner.invoke(cli, ["generate", "audio", "--help"])
        assert "DESCRIPTION" in result.output

    def test_generate_video_command_exists(self, runner):
        result = runner.invoke(cli, ["generate", "video", "--help"])
        assert result.exit_code == 0
        assert "DESCRIPTION" in result.output

    def test_generate_quiz_command_exists(self, runner):
        result = runner.invoke(cli, ["generate", "quiz", "--help"])
        assert result.exit_code == 0

    def test_generate_slide_deck_command_exists(self, runner):
        result = runner.invoke(cli, ["generate", "slide-deck", "--help"])
        assert result.exit_code == 0


class TestDownloadGroup:
    def test_download_group_exists(self, runner):
        result = runner.invoke(cli, ["download", "--help"])
        assert result.exit_code == 0
        assert "audio" in result.output
        assert "video" in result.output

    def test_download_audio_command_exists(self, runner):
        result = runner.invoke(cli, ["download", "audio", "--help"])
        assert result.exit_code == 0
        assert "OUTPUT_PATH" in result.output
        assert "--notebook" in result.output or "-n" in result.output


class TestArtifactGroup:
    def test_artifact_group_exists(self, runner):
        result = runner.invoke(cli, ["artifact", "--help"])
        assert result.exit_code == 0
        assert "list" in result.output
        assert "get" in result.output
        assert "delete" in result.output

    def test_artifact_list_command_exists(self, runner):
        result = runner.invoke(cli, ["artifact", "list", "--help"])
        assert result.exit_code == 0
        assert "--type" in result.output


class TestNoteGroup:
    def test_note_group_exists(self, runner):
        result = runner.invoke(cli, ["note", "--help"])
        assert result.exit_code == 0
        assert "list" in result.output
        assert "create" in result.output
        assert "rename" in result.output
        assert "delete" in result.output

    def test_note_create_command_exists(self, runner):
        result = runner.invoke(cli, ["note", "create", "--help"])
        assert result.exit_code == 0
        assert "--title" in result.output
        assert "[CONTENT]" in result.output  # Positional argument


class TestNotebookGroup:
    def test_notebook_group_exists(self, runner):
        result = runner.invoke(cli, ["notebook", "--help"])
        assert result.exit_code == 0
        assert "list" in result.output
        assert "create" in result.output
        assert "delete" in result.output
        assert "rename" in result.output

    def test_notebook_ask_command_exists(self, runner):
        result = runner.invoke(cli, ["notebook", "ask", "--help"])
        assert result.exit_code == 0
        assert "QUESTION" in result.output


class TestAskShortcut:
    def test_ask_command_exists(self, runner):
        result = runner.invoke(cli, ["ask", "--help"])
        assert result.exit_code == 0
        assert "QUESTION" in result.output
        assert "--notebook" in result.output or "-n" in result.output


class TestContextCommands:
    def test_use_command_exists(self, runner):
        result = runner.invoke(cli, ["use", "--help"])
        assert result.exit_code == 0
        assert "NOTEBOOK_ID" in result.output

    def test_status_command_exists(self, runner):
        result = runner.invoke(cli, ["status", "--help"])
        assert result.exit_code == 0

    def test_clear_command_exists(self, runner):
        result = runner.invoke(cli, ["clear", "--help"])
        assert result.exit_code == 0

    def test_use_sets_context(self, runner, tmp_path):
        # Patch CONTEXT_FILE to use tmp_path
        with patch("notebooklm.cli.helpers.CONTEXT_FILE", tmp_path / "context.json"):
            result = runner.invoke(cli, ["use", "nb_test123"])
            assert result.exit_code == 0
            assert "nb_test123" in result.output

    def test_status_shows_no_context(self, runner, tmp_path):
        # Patch CONTEXT_FILE to use tmp_path (empty)
        with patch("notebooklm.cli.helpers.CONTEXT_FILE", tmp_path / "context.json"):
            result = runner.invoke(cli, ["status"])
            assert result.exit_code == 0
            assert "No notebook selected" in result.output

    def test_status_shows_context_after_use(self, runner, tmp_path):
        with patch("notebooklm.cli.helpers.CONTEXT_FILE", tmp_path / "context.json"):
            runner.invoke(cli, ["use", "nb_test456"])
            result = runner.invoke(cli, ["status"])
            assert result.exit_code == 0
            assert "nb_test456" in result.output

    def test_clear_removes_context(self, runner, tmp_path):
        with patch("notebooklm.cli.helpers.CONTEXT_FILE", tmp_path / "context.json"):
            runner.invoke(cli, ["use", "nb_test789"])
            result = runner.invoke(cli, ["clear"])
            assert result.exit_code == 0
            # After clear, status should show no context
            status_result = runner.invoke(cli, ["status"])
            assert "No notebook selected" in status_result.output


# =============================================================================
# Command Execution Tests with Mocked Client
# =============================================================================


class TestNotebookCommandsWithMock:
    def test_notebook_delete(self, runner, mock_auth):
        with patch("notebooklm.notebooklm_cli.NotebookLMClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.notebooks.delete = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.notebooklm_cli.fetch_tokens") as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                # notebook delete uses -n option for notebook ID
                result = runner.invoke(cli, ["notebook", "delete", "-n", "nb_to_delete", "-y"])

            assert result.exit_code == 0
            mock_client.notebooks.delete.assert_called_once_with("nb_to_delete")

    def test_notebook_rename(self, runner, mock_auth):
        with patch("notebooklm.notebooklm_cli.NotebookLMClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.notebooks.rename = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.notebooklm_cli.fetch_tokens") as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                # notebook rename: NEW_TITLE is positional, notebook ID via -n
                result = runner.invoke(cli, ["notebook", "rename", "New Title", "-n", "nb_123"])

            assert result.exit_code == 0
            mock_client.notebooks.rename.assert_called_once_with("nb_123", "New Title")


class TestSourceCommandsWithMock:
    def test_source_list(self, runner, mock_auth):
        with patch("notebooklm.notebooklm_cli.NotebookLMClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.sources.list = AsyncMock(
                return_value=[
                    Source(id="src_1", title="Source One"),
                ]
            )
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.notebooklm_cli.fetch_tokens") as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["source", "list", "-n", "nb_123"])

            assert result.exit_code == 0
            assert "Source One" in result.output or "src_1" in result.output

    def test_source_add_url(self, runner, mock_auth):
        with patch("notebooklm.notebooklm_cli.NotebookLMClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.sources.add_url = AsyncMock(
                return_value=Source(id="src_new", title="https://example.com", url="https://example.com")
            )
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.notebooklm_cli.fetch_tokens") as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["source", "add", "https://example.com", "-n", "nb_123"]
                )

            assert result.exit_code == 0

    def test_source_add_text(self, runner, mock_auth):
        with patch("notebooklm.notebooklm_cli.NotebookLMClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.sources.add_text = AsyncMock(
                return_value=Source(id="src_text", title="My Text Source")
            )
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.notebooklm_cli.fetch_tokens") as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli,
                    ["source", "add", "Some text content", "--type", "text", "-n", "nb_123"],
                )

            assert result.exit_code == 0

    def test_source_delete(self, runner, mock_auth):
        with patch("notebooklm.notebooklm_cli.NotebookLMClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.sources.delete = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.notebooklm_cli.fetch_tokens") as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["source", "delete", "src_123", "-n", "nb_123", "-y"]
                )

            assert result.exit_code == 0
            mock_client.sources.delete.assert_called_once_with("nb_123", "src_123")


class TestArtifactCommandsWithMock:
    def test_artifact_list(self, runner, mock_auth):
        with patch("notebooklm.notebooklm_cli.NotebookLMClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.artifacts.list = AsyncMock(
                return_value=[
                    Artifact(id="art_1", title="Quiz One", artifact_type=4, status=3),  # 3=completed
                    Artifact(id="art_2", title="Briefing Doc", artifact_type=2, status=3),
                ]
            )
            mock_client.notes.list_mind_maps = AsyncMock(return_value=[])
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.notebooklm_cli.fetch_tokens") as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["artifact", "list", "-n", "nb_123"])

            assert result.exit_code == 0
            assert "Quiz One" in result.output or "art_1" in result.output

    def test_artifact_list_includes_mind_maps(self, runner, mock_auth):
        with patch("notebooklm.notebooklm_cli.NotebookLMClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.artifacts.list = AsyncMock(return_value=[])
            mock_client.notes.list_mind_maps = AsyncMock(
                return_value=[
                    ["mm_1", ["mm_1", "{}", None, None, "My Mind Map"]],
                ]
            )
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.notebooklm_cli.fetch_tokens") as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["artifact", "list", "-n", "nb_123"])

            assert result.exit_code == 0
            assert "Mind Map" in result.output

    def test_artifact_rename_rejects_mind_map(self, runner, mock_auth):
        with patch("notebooklm.notebooklm_cli.NotebookLMClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.notes.list_mind_maps = AsyncMock(
                return_value=[
                    ["mm_123", ["mm_123", "{}", None, None, "Old Title"]],
                ]
            )
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.notebooklm_cli.fetch_tokens") as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["artifact", "rename", "mm_123", "New Title", "-n", "nb_123"]
                )

            assert result.exit_code != 0
            assert "Mind maps cannot be renamed" in result.output

    def test_artifact_delete_mind_map_clears(self, runner, mock_auth):
        with patch("notebooklm.notebooklm_cli.NotebookLMClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.notes.list_mind_maps = AsyncMock(
                return_value=[
                    ["mm_456", ["mm_456", "{}", None, None, "Mind Map Title"]],
                ]
            )
            mock_client.notes.delete = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.notebooklm_cli.fetch_tokens") as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["artifact", "delete", "mm_456", "-n", "nb_123", "-y"]
                )

            assert result.exit_code == 0
            assert "Cleared mind map" in result.output
            mock_client.notes.delete.assert_called_once_with("nb_123", "mm_456")


class TestNoteCommandsWithMock:
    def test_note_list(self, runner, mock_auth):
        with patch("notebooklm.notebooklm_cli.NotebookLMClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.notes.list = AsyncMock(
                return_value=[
                    ["note_1", ["Note Title", "<p>Content</p>"]],
                    ["note_2", ["Another Note", "<p>More content</p>"]],
                ]
            )
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.notebooklm_cli.fetch_tokens") as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["note", "list", "-n", "nb_123"])

            assert result.exit_code == 0

    def test_note_create(self, runner, mock_auth):
        with patch("notebooklm.notebooklm_cli.NotebookLMClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.notes.create = AsyncMock(
                return_value=["note_new", ["My Note", "<p>Hello</p>"]]
            )
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.notebooklm_cli.fetch_tokens") as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli,
                    ["note", "create", "Hello world", "--title", "My Note", "-n", "nb_123"],
                )

            assert result.exit_code == 0

    def test_note_delete(self, runner, mock_auth):
        with patch("notebooklm.notebooklm_cli.NotebookLMClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.notes.delete = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.notebooklm_cli.fetch_tokens") as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["note", "delete", "note_123", "-n", "nb_123", "-y"]
                )

            assert result.exit_code == 0


class TestGenerateCommandsWithMock:
    def test_generate_audio(self, runner, mock_auth):
        with patch("notebooklm.notebooklm_cli.NotebookLMClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.artifacts.generate_audio = AsyncMock(
                return_value={"artifact_id": "audio_123", "status": "processing"}
            )
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.notebooklm_cli.fetch_tokens") as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["generate", "audio", "-n", "nb_123"])

            assert result.exit_code == 0
            assert "audio_123" in result.output or "Started" in result.output

    def test_generate_video(self, runner, mock_auth):
        with patch("notebooklm.notebooklm_cli.NotebookLMClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.artifacts.generate_video = AsyncMock(
                return_value={"artifact_id": "video_123", "status": "processing"}
            )
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.notebooklm_cli.fetch_tokens") as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["generate", "video", "-n", "nb_123"])

            assert result.exit_code == 0

    def test_generate_quiz(self, runner, mock_auth):
        with patch("notebooklm.notebooklm_cli.NotebookLMClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.artifacts.generate_quiz = AsyncMock(
                return_value={"artifact_id": "quiz_123", "status": "processing"}
            )
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.notebooklm_cli.fetch_tokens") as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["generate", "quiz", "-n", "nb_123"])

            assert result.exit_code == 0

    def test_generate_flashcards(self, runner, mock_auth):
        with patch("notebooklm.notebooklm_cli.NotebookLMClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.artifacts.generate_flashcards = AsyncMock(
                return_value={"artifact_id": "flash_123", "status": "processing"}
            )
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.notebooklm_cli.fetch_tokens") as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["generate", "flashcards", "-n", "nb_123"])

            assert result.exit_code == 0

    def test_generate_slide_deck(self, runner, mock_auth):
        with patch("notebooklm.notebooklm_cli.NotebookLMClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.artifacts.generate_slide_deck = AsyncMock(
                return_value={"artifact_id": "slides_123", "status": "processing"}
            )
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.notebooklm_cli.fetch_tokens") as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["generate", "slide-deck", "-n", "nb_123"])

            assert result.exit_code == 0

    def test_generate_infographic(self, runner, mock_auth):
        with patch("notebooklm.notebooklm_cli.NotebookLMClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.artifacts.generate_infographic = AsyncMock(
                return_value={"artifact_id": "info_123", "status": "processing"}
            )
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.notebooklm_cli.fetch_tokens") as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["generate", "infographic", "-n", "nb_123"])

            assert result.exit_code == 0

    def test_generate_mind_map(self, runner, mock_auth):
        with patch("notebooklm.notebooklm_cli.NotebookLMClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.artifacts.generate_mind_map = AsyncMock(
                return_value={"mind_map": {"name": "Root", "children": []}, "note_ids": ["n1"]}
            )
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.notebooklm_cli.fetch_tokens") as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["generate", "mind-map", "-n", "nb_123"])

            assert result.exit_code == 0

    def test_generate_report(self, runner, mock_auth):
        with patch("notebooklm.notebooklm_cli.NotebookLMClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.artifacts.generate_report = AsyncMock(
                return_value={"artifact_id": "report_123", "status": "processing"}
            )
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.notebooklm_cli.fetch_tokens") as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["generate", "report", "-n", "nb_123"])

            assert result.exit_code == 0


class TestDownloadCommandsWithMock:
    def test_download_audio(self, runner, mock_auth, tmp_path):
        with patch("notebooklm.notebooklm_cli.NotebookLMClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            # list_artifacts returns artifacts in format: [id, title, type, created_at, status, ...]
            # type 1 = AUDIO, status 3 = completed
            mock_client.artifacts.list = AsyncMock(
                return_value=[["audio_123", "My Audio", 1, 1234567890, 3]]
            )

            output_file = tmp_path / "audio.mp3"

            # Mock download_audio to write file and return path (like real function)
            async def mock_download_audio(notebook_id, output_path, artifact_id=None):
                Path(output_path).write_bytes(b"fake audio content")
                return output_path

            mock_client.download_audio = mock_download_audio
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.notebooklm_cli.fetch_tokens") as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["download", "audio", str(output_file), "-n", "nb_123"]
                )

            assert result.exit_code == 0
            assert output_file.exists()

    def test_download_video(self, runner, mock_auth, tmp_path):
        with patch("notebooklm.notebooklm_cli.NotebookLMClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            # type 3 = VIDEO, status 3 = completed
            mock_client.artifacts.list = AsyncMock(
                return_value=[["vid_1", "My Video", 3, 1234567890, 3]]
            )

            output_file = tmp_path / "video.mp4"

            # Mock download_video to write file and return path
            async def mock_download_video(notebook_id, output_path, artifact_id=None):
                Path(output_path).write_bytes(b"fake video content")
                return output_path

            mock_client.download_video = mock_download_video
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.notebooklm_cli.fetch_tokens") as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["download", "video", str(output_file), "-n", "nb_123"]
                )

            assert result.exit_code == 0
            assert output_file.exists()


class TestDownloadCommandsAdvanced:
    """Tests for download command advanced features."""

    def test_download_audio_dry_run(self, runner, mock_auth, tmp_path):
        with patch("notebooklm.notebooklm_cli.NotebookLMClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.artifacts.list = AsyncMock(
                return_value=[["audio_123", "My Audio", 1, 1234567890, 3]]
            )
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.notebooklm_cli.fetch_tokens") as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["download", "audio", "--dry-run", "-n", "nb_123"]
                )

            assert result.exit_code == 0
            assert "DRY RUN" in result.output

    def test_download_audio_no_artifacts(self, runner, mock_auth):
        with patch("notebooklm.notebooklm_cli.NotebookLMClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.artifacts.list = AsyncMock(return_value=[])
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.notebooklm_cli.fetch_tokens") as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["download", "audio", "-n", "nb_123"])

            # Should report no artifacts found
            assert "No completed audio artifacts found" in result.output or result.exit_code != 0

    def test_download_infographic(self, runner, mock_auth, tmp_path):
        with patch("notebooklm.notebooklm_cli.NotebookLMClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            # type 7 = INFOGRAPHIC, status 3 = completed
            mock_client.artifacts.list = AsyncMock(
                return_value=[["info_1", "My Infographic", 7, 1234567890, 3]]
            )

            output_file = tmp_path / "infographic.png"

            async def mock_download_infographic(notebook_id, output_path, artifact_id=None):
                Path(output_path).write_bytes(b"fake image content")
                return output_path

            mock_client.download_infographic = mock_download_infographic
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.notebooklm_cli.fetch_tokens") as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["download", "infographic", str(output_file), "-n", "nb_123"]
                )

            assert result.exit_code == 0
            assert output_file.exists()

    def test_download_slide_deck(self, runner, mock_auth, tmp_path):
        with patch("notebooklm.notebooklm_cli.NotebookLMClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            # type 8 = SLIDE_DECK, status 3 = completed
            mock_client.artifacts.list = AsyncMock(
                return_value=[["slide_1", "My Slides", 8, 1234567890, 3]]
            )

            output_dir = tmp_path / "slides"

            async def mock_download_slide_deck(notebook_id, output_path, artifact_id=None):
                # Slide deck downloads create a directory with files
                Path(output_path).mkdir(parents=True, exist_ok=True)
                (Path(output_path) / "slide_1.png").write_bytes(b"fake slide")
                return output_path

            mock_client.download_slide_deck = mock_download_slide_deck
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.notebooklm_cli.fetch_tokens") as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["download", "slide-deck", str(output_dir), "-n", "nb_123"]
                )

            assert result.exit_code == 0


class TestNotebookListWithMock:
    """Tests for notebook list command."""

    def test_notebook_list_empty(self, runner, mock_auth):
        with patch("notebooklm.notebooklm_cli.NotebookLMClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.notebooks.list = AsyncMock(return_value=[])
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.notebooklm_cli.fetch_tokens") as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["list"])

            assert result.exit_code == 0
            # With no notebooks, the table will be empty (no data rows)
            assert "Notebooks" in result.output

    def test_notebook_list_with_notebooks(self, runner, mock_auth):
        with patch("notebooklm.notebooklm_cli.NotebookLMClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.notebooks.list = AsyncMock(
                return_value=[
                    Notebook(id="nb_1", title="First Notebook", created_at=datetime(2024, 1, 1)),
                    Notebook(id="nb_2", title="Second Notebook", created_at=datetime(2024, 1, 2)),
                ]
            )
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.notebooklm_cli.fetch_tokens") as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["list"])

            assert result.exit_code == 0
            assert "First Notebook" in result.output
            assert "Second Notebook" in result.output

    def test_notebook_create(self, runner, mock_auth):
        with patch("notebooklm.notebooklm_cli.NotebookLMClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.notebooks.create = AsyncMock(
                return_value=Notebook(id="new_nb_id", title="Test Notebook", created_at=datetime(2024, 1, 1))
            )
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.notebooklm_cli.fetch_tokens") as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["notebook", "create", "Test Notebook"])

            assert result.exit_code == 0


class TestQueryCommand:
    """Tests for query/ask commands."""

    def test_notebook_ask(self, runner, mock_auth):
        with patch("notebooklm.notebooklm_cli.NotebookLMClient") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)

            # Mock ask method which returns a dict
            mock_client.chat.ask = AsyncMock(
                return_value={
                    "answer": "This is a response",
                    "conversation_id": "conv_123",
                    "is_follow_up": False,
                }
            )
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.notebooklm_cli.fetch_tokens") as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["notebook", "ask", "-n", "nb_123", "What is this?"]
                )

            assert result.exit_code == 0
            assert "This is a response" in result.output


class TestErrorHandling:
    def test_no_auth_error(self, runner):
        # Patch where get_client() uses load_auth_from_storage (in cli.helpers)
        with patch("notebooklm.cli.helpers.load_auth_from_storage") as mock:
            mock.return_value = None
            result = runner.invoke(cli, ["list"])
            # Should fail when no auth
            assert result.exit_code != 0

    def test_missing_notebook_context(self, runner, mock_auth):
        with runner.isolated_filesystem():
            # Try to run a command that requires notebook context without setting it
            with patch("notebooklm.notebooklm_cli.fetch_tokens") as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["source", "list"])
            # Should fail because no notebook is set
            assert result.exit_code != 0 or "notebook" in result.output.lower()
