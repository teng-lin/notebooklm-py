"""Tests for note CLI commands."""

from unittest.mock import AsyncMock, patch

import pytest
from click.testing import CliRunner

from notebooklm.notebooklm_cli import cli
from notebooklm.types import Note

from .conftest import create_mock_client, patch_client_for_module


def make_note(id: str, title: str, content: str, notebook_id: str = "nb_123") -> Note:
    """Create a Note for testing."""
    return Note(
        id=id,
        notebook_id=notebook_id,
        title=title,
        content=content,
    )


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def mock_auth():
    with patch("notebooklm.cli.helpers.load_auth_from_storage") as mock:
        mock.return_value = {
            "SID": "test",
            "HSID": "test",
            "SSID": "test",
            "APISID": "test",
            "SAPISID": "test",
        }
        yield mock


# =============================================================================
# NOTE LIST TESTS
# =============================================================================


class TestNoteList:
    def test_note_list(self, runner, mock_auth):
        with patch_client_for_module("note") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.notes.list = AsyncMock(
                return_value=[
                    make_note("note_1", "Note Title", "Content 1"),
                    make_note("note_2", "Another Note", "Content 2"),
                ]
            )
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["note", "list", "-n", "nb_123"])

            assert result.exit_code == 0

    def test_note_list_empty(self, runner, mock_auth):
        with patch_client_for_module("note") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.notes.list = AsyncMock(return_value=[])
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["note", "list", "-n", "nb_123"])

            assert result.exit_code == 0
            assert "No notes found" in result.output


# =============================================================================
# NOTE CREATE TESTS
# =============================================================================


class TestNoteCreate:
    def test_note_create(self, runner, mock_auth):
        with patch_client_for_module("note") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.notes.create = AsyncMock(
                return_value=["note_new", ["note_new", "Hello world", None, None, "My Note"]]
            )
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli,
                    ["note", "create", "Hello world", "--title", "My Note", "-n", "nb_123"],
                )

            assert result.exit_code == 0
            assert "Note created" in result.output

    def test_note_create_empty(self, runner, mock_auth):
        with patch_client_for_module("note") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.notes.create = AsyncMock(
                return_value=["note_new", ["note_new", "", None, None, "New Note"]]
            )
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["note", "create", "-n", "nb_123"])

            assert result.exit_code == 0

    def test_note_create_failure(self, runner, mock_auth):
        with patch_client_for_module("note") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.notes.create = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["note", "create", "Test", "-n", "nb_123"])

            assert result.exit_code == 0
            assert "Creation may have failed" in result.output


# =============================================================================
# NOTE GET TESTS
# =============================================================================


class TestNoteGet:
    def test_note_get(self, runner, mock_auth):
        with patch_client_for_module("note") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.notes.get = AsyncMock(
                return_value=make_note("note_123", "My Note", "This is the content")
            )
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["note", "get", "note_123", "-n", "nb_123"])

            assert result.exit_code == 0
            assert "note_123" in result.output
            assert "This is the content" in result.output

    def test_note_get_not_found(self, runner, mock_auth):
        with patch_client_for_module("note") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.notes.get = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["note", "get", "nonexistent", "-n", "nb_123"])

            assert result.exit_code == 0
            assert "Note not found" in result.output


# =============================================================================
# NOTE SAVE TESTS
# =============================================================================


class TestNoteSave:
    def test_note_save_content(self, runner, mock_auth):
        with patch_client_for_module("note") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.notes.update = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["note", "save", "note_123", "--content", "New content", "-n", "nb_123"]
                )

            assert result.exit_code == 0
            assert "Note updated" in result.output

    def test_note_save_title(self, runner, mock_auth):
        with patch_client_for_module("note") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.notes.update = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["note", "save", "note_123", "--title", "New Title", "-n", "nb_123"]
                )

            assert result.exit_code == 0
            assert "Note updated" in result.output

    def test_note_save_no_changes(self, runner, mock_auth):
        """Should show message when neither title nor content provided"""
        with patch_client_for_module("note") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["note", "save", "note_123", "-n", "nb_123"])

        assert "Provide --title and/or --content" in result.output


# =============================================================================
# NOTE RENAME TESTS
# =============================================================================


class TestNoteRename:
    def test_note_rename(self, runner, mock_auth):
        with patch_client_for_module("note") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.notes.get = AsyncMock(
                return_value=make_note("note_123", "Old Title", "Original content")
            )
            mock_client.notes.update = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["note", "rename", "note_123", "New Title", "-n", "nb_123"]
                )

            assert result.exit_code == 0
            assert "Note renamed" in result.output

    def test_note_rename_not_found(self, runner, mock_auth):
        with patch_client_for_module("note") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.notes.get = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["note", "rename", "nonexistent", "New Title", "-n", "nb_123"]
                )

            assert result.exit_code == 0
            assert "Note not found" in result.output


# =============================================================================
# NOTE DELETE TESTS
# =============================================================================


class TestNoteDelete:
    def test_note_delete(self, runner, mock_auth):
        with patch_client_for_module("note") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.notes.delete = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["note", "delete", "note_123", "-n", "nb_123", "-y"])

            assert result.exit_code == 0
            assert "Deleted note" in result.output


# =============================================================================
# COMMAND EXISTENCE TESTS
# =============================================================================


class TestNoteCommandsExist:
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
        assert "[CONTENT]" in result.output
