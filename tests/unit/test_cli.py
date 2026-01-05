"""Tests for CLI interface."""

import pytest
from click.testing import CliRunner
from unittest.mock import AsyncMock, patch, MagicMock

from notebooklm.notebooklm_cli import cli, main


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
            mock_client.list_notebooks = AsyncMock(
                return_value=[
                    ["nb_001", "First Notebook", None, None, 1704067200000],
                    ["nb_002", "Second Notebook", None, None, 1704153600000],
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
            mock_client.create_notebook = AsyncMock(
                return_value=["nb_new", "My Research", None, None, 1704067200000]
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
