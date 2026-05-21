"""Tests for note CLI commands."""

import json
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
    """Provide a Click test runner."""
    return CliRunner()


@pytest.fixture
def mock_auth():
    """Patch auth storage to return test credentials."""
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
    """Tests for the note list command."""

    def test_note_list(self, runner, mock_auth):
        """Renders a table when notes exist."""
        with patch_client_for_module("note") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.notes.list = AsyncMock(
                return_value=[
                    make_note("note_1", "Note Title", "Content 1"),
                    make_note("note_2", "Another Note", "Content 2"),
                ]
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["note", "list", "-n", "nb_123"])

            assert result.exit_code == 0

    def test_note_list_empty(self, runner, mock_auth):
        """Shows 'No notes found' when the notebook has no notes."""
        with patch_client_for_module("note") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.notes.list = AsyncMock(return_value=[])
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["note", "list", "-n", "nb_123"])

            assert result.exit_code == 0
            assert "No notes found" in result.output

    def test_note_list_json(self, runner, mock_auth):
        """Outputs valid JSON with notebook_id, notes array, and count."""
        with patch_client_for_module("note") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.notes.list = AsyncMock(
                return_value=[
                    make_note("note_1", "Note Title", "Content 1"),
                    make_note("note_2", "Another Note", "Content 2"),
                ]
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["note", "list", "-n", "nb_123", "--json"])

            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["notebook_id"] == "nb_123"
            assert len(data["notes"]) == 2
            assert data["count"] == 2
            assert data["notes"][0]["id"] == "note_1"

    def test_note_list_json_empty(self, runner, mock_auth):
        """JSON output has empty notes array and count of zero when no notes exist."""
        with patch_client_for_module("note") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.notes.list = AsyncMock(return_value=[])
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["note", "list", "-n", "nb_123", "--json"])

            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["notes"] == []
            assert data["count"] == 0

    def test_note_list_json_count_matches_serialized_notes(self, runner, mock_auth):
        """count reflects only Note instances, not total items in the raw list."""
        with patch_client_for_module("note") as mock_client_cls:
            mock_client = create_mock_client()
            # Include a non-Note item to verify count only counts Note instances
            mock_client.notes.list = AsyncMock(
                return_value=[
                    make_note("note_1", "Title", "Content"),
                    "unexpected_string_item",  # non-Note item
                ]
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["note", "list", "-n", "nb_123", "--json"])

            assert result.exit_code == 0
            data = json.loads(result.output)
            assert len(data["notes"]) == 1
            assert data["count"] == 1  # must match notes array length, not raw list length


# =============================================================================
# NOTE CREATE TESTS
# =============================================================================


class TestNoteCreate:
    """Tests for the note create command."""

    def test_note_create(self, runner, mock_auth):
        """Creates a note and confirms success message."""
        with patch_client_for_module("note") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.notes.create = AsyncMock(
                return_value=["note_new", ["note_new", "Hello world", None, None, "My Note"]]
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli,
                    ["note", "create", "Hello world", "--title", "My Note", "-n", "nb_123"],
                )

            assert result.exit_code == 0
            assert "Note created" in result.output

    def test_note_create_empty(self, runner, mock_auth):
        """Creates an empty note with the default title."""
        with patch_client_for_module("note") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.notes.create = AsyncMock(
                return_value=["note_new", ["note_new", "", None, None, "New Note"]]
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["note", "create", "-n", "nb_123"])

            assert result.exit_code == 0

    def test_note_create_failure(self, runner, mock_auth):
        """Shows a warning when the API returns None."""
        with patch_client_for_module("note") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.notes.create = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["note", "create", "Test", "-n", "nb_123"])

            assert result.exit_code == 0
            assert "Creation may have failed" in result.output


# =============================================================================
# NOTE GET TESTS
# =============================================================================


class TestNoteGet:
    """Tests for the note get command."""

    def test_note_get(self, runner, mock_auth):
        """Displays note ID, title, and content."""
        with patch_client_for_module("note") as mock_client_cls:
            mock_client = create_mock_client()
            # Mock notes.list for resolve_note_id
            mock_client.notes.list = AsyncMock(
                return_value=[make_note("note_123", "My Note", "This is the content")]
            )
            mock_client.notes.get = AsyncMock(
                return_value=make_note("note_123", "My Note", "This is the content")
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["note", "get", "note_123", "-n", "nb_123"])

            assert result.exit_code == 0
            assert "note_123" in result.output
            assert "This is the content" in result.output

    def test_note_get_not_found(self, runner, mock_auth):
        """Exits with error code 1 when no matching note exists."""
        with patch_client_for_module("note") as mock_client_cls:
            mock_client = create_mock_client()
            # Mock notes.list to return empty (no match for resolve_note_id)
            mock_client.notes.list = AsyncMock(return_value=[])
            mock_client.notes.get = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["note", "get", "nonexistent", "-n", "nb_123"])

            # resolve_note_id will raise ClickException for no match
            assert result.exit_code == 1
            assert "No note found" in result.output

    # -------------------------------------------------------------------------
    # get-on-not-found now exits 1 (was 0). Mirrors the
    # ``test_source.py`` / ``test_artifact.py`` Path A / Path B coverage so the
    # contract is uniform across all three ``get`` commands.
    # -------------------------------------------------------------------------

    def test_note_get_not_found_pathA_long_id_text_exits_1(self, runner, mock_auth):
        """Path A: ID ≥20 chars skips partial-resolve; backend None → exit 1."""
        long_id = "a" * 24
        with patch_client_for_module("note") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.notes.list = AsyncMock(return_value=[])
            mock_client.notes.get = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["note", "get", long_id, "-n", "nb_123"])

            assert result.exit_code == 1, result.output
            assert "Note not found" in result.output
            mock_client.notes.list.assert_not_called()

    def test_note_get_not_found_pathA_long_id_json_exits_1(self, runner, mock_auth):
        """Path A under ``--json``: typed JSON error doc + exit 1."""
        long_id = "a" * 24
        with patch_client_for_module("note") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.notes.list = AsyncMock(return_value=[])
            mock_client.notes.get = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["note", "get", long_id, "-n", "nb_123", "--json"])

            assert result.exit_code == 1, result.output
            data = json.loads(result.output)
            assert data["error"] is True
            assert data["code"] == "NOT_FOUND"
            assert "Note not found" in data["message"]
            assert data["id"] == long_id
            assert data["notebook_id"] == "nb_123"
            mock_client.notes.list.assert_not_called()

    def test_note_get_not_found_pathB_resolved_then_none_text_exits_1(self, runner, mock_auth):
        """Path B: partial-resolve succeeds, backend get() returns None → exit 1."""
        with patch_client_for_module("note") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.notes.list = AsyncMock(return_value=[make_note("note_xyz", "Doomed", "")])
            mock_client.notes.get = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["note", "get", "note_xyz", "-n", "nb_123"])

            assert result.exit_code == 1, result.output
            assert "Note not found" in result.output


# =============================================================================
# NOTE SAVE TESTS
# =============================================================================


class TestNoteSave:
    """Tests for the note save command."""

    def test_note_save_content(self, runner, mock_auth):
        """Updates note content and prints confirmation."""
        with patch_client_for_module("note") as mock_client_cls:
            mock_client = create_mock_client()
            # Mock notes.list for resolve_note_id
            mock_client.notes.list = AsyncMock(
                return_value=[make_note("note_123", "Test Note", "Original content")]
            )
            mock_client.notes.update = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["note", "save", "note_123", "--content", "New content", "-n", "nb_123"]
                )

            assert result.exit_code == 0
            assert "Note updated" in result.output

    def test_note_save_title(self, runner, mock_auth):
        """Updates note title and prints confirmation."""
        with patch_client_for_module("note") as mock_client_cls:
            mock_client = create_mock_client()
            # Mock notes.list for resolve_note_id
            mock_client.notes.list = AsyncMock(
                return_value=[make_note("note_123", "Old Title", "Content")]
            )
            mock_client.notes.update = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
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

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["note", "save", "note_123", "-n", "nb_123"])

        assert "Provide --title and/or --content" in result.output


# =============================================================================
# NOTE RENAME TESTS
# =============================================================================


class TestNoteRename:
    """Tests for the note rename command."""

    def test_note_rename(self, runner, mock_auth):
        """Renames a note and prints the new title."""
        with patch_client_for_module("note") as mock_client_cls:
            mock_client = create_mock_client()
            # Mock notes.list for resolve_note_id
            mock_client.notes.list = AsyncMock(
                return_value=[make_note("note_123", "Old Title", "Original content")]
            )
            mock_client.notes.get = AsyncMock(
                return_value=make_note("note_123", "Old Title", "Original content")
            )
            mock_client.notes.update = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["note", "rename", "note_123", "New Title", "-n", "nb_123"]
                )

            assert result.exit_code == 0
            assert "Note renamed" in result.output

    def test_note_rename_not_found(self, runner, mock_auth):
        """Exits with error code 1 when the note cannot be resolved."""
        with patch_client_for_module("note") as mock_client_cls:
            mock_client = create_mock_client()
            # Mock notes.list to return empty (no match for resolve_note_id)
            mock_client.notes.list = AsyncMock(return_value=[])
            mock_client.notes.get = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["note", "rename", "nonexistent", "New Title", "-n", "nb_123"]
                )

            # resolve_note_id will raise ClickException for no match
            assert result.exit_code == 1
            assert "No note found" in result.output


# =============================================================================
# NOTE DELETE TESTS
# =============================================================================


class TestNoteDelete:
    """Tests for the note delete command."""

    def test_note_delete(self, runner, mock_auth):
        """Deletes a note and prints the deleted note ID."""
        with patch_client_for_module("note") as mock_client_cls:
            mock_client = create_mock_client()
            # Mock notes.list for resolve_note_id
            mock_client.notes.list = AsyncMock(
                return_value=[make_note("note_123", "Test Note", "Content")]
            )
            mock_client.notes.delete = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["note", "delete", "note_123", "-n", "nb_123", "-y"])

            assert result.exit_code == 0
            assert "Deleted note" in result.output


# =============================================================================
# COMMAND EXISTENCE TESTS
# =============================================================================


class TestNoteCommandsExist:
    """Smoke tests that verify all note subcommands are registered."""

    def test_note_group_exists(self, runner):
        """Note group help lists all expected subcommands."""
        result = runner.invoke(cli, ["note", "--help"])
        assert result.exit_code == 0
        assert "list" in result.output
        assert "create" in result.output
        assert "rename" in result.output
        assert "delete" in result.output

    def test_note_create_command_exists(self, runner):
        """note create --help exposes --title and CONTENT argument."""
        result = runner.invoke(cli, ["note", "create", "--help"])
        assert result.exit_code == 0
        assert "--title" in result.output
        assert "[CONTENT]" in result.output

    def test_note_list_json_flag_exists(self, runner):
        """note list --help exposes the --json flag."""
        result = runner.invoke(cli, ["note", "list", "--help"])
        assert result.exit_code == 0
        assert "--json" in result.output


# =============================================================================
# JSON OUTPUT TESTS
# =============================================================================
#
# Each mutating subcommand (create/save/rename/delete) emits a structured
# ``{"id": ..., "<verb>ed": true}`` shape on success and ``{..., "error": ...}``
# on failure. ``note get`` mirrors the underlying ``Note`` dataclass via
# ``dataclasses.asdict`` so callers can round-trip the value through automation
# without needing to introspect Rich-formatted text.
#
# Smoke level only: each test asserts ``json.loads(stdout)`` is parseable and
# carries the contract keys. Behavioural coverage of the underlying flows lives
# in the existing per-command test classes above.


class TestNoteJsonFlagsRegistered:
    """Verify --json appears in --help for every note subcommand."""

    @pytest.mark.parametrize("subcommand", ["get", "save", "create", "delete", "rename"])
    def test_json_flag_in_help(self, runner, subcommand):
        result = runner.invoke(cli, ["note", subcommand, "--help"])
        assert result.exit_code == 0, result.output
        assert "--json" in result.output


class TestNoteCreateJson:
    """JSON shape for ``note create``."""

    def test_create_success(self, runner, mock_auth):
        with patch_client_for_module("note") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.notes.create = AsyncMock(
                return_value=["note_new", ["note_new", "Hello", None, None, "My Note"]]
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli,
                    [
                        "note",
                        "create",
                        "Hello",
                        "--title",
                        "My Note",
                        "-n",
                        "nb_123",
                        "--json",
                    ],
                )

            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            assert data["id"] == "note_new"
            assert data["created"] is True
            assert data["title"] == "My Note"
            assert data["notebook_id"] == "nb_123"

    def test_create_failure(self, runner, mock_auth):
        with patch_client_for_module("note") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.notes.create = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["note", "create", "X", "-n", "nb_123", "--json"])

            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            assert data["created"] is False
            assert "error" in data


class TestNoteGetJson:
    """JSON shape for ``note get`` (mirrors Note dataclass)."""

    def test_get_success(self, runner, mock_auth):
        with patch_client_for_module("note") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.notes.list = AsyncMock(return_value=[make_note("note_123", "T", "Body")])
            mock_client.notes.get = AsyncMock(return_value=make_note("note_123", "T", "Body"))
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["note", "get", "note_123", "-n", "nb_123", "--json"])

            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            # Note dataclass mirror + injected ``found`` discriminator so
            # callers can distinguish success from the failure shape with a
            # single ``data["found"]`` check.
            assert data["found"] is True
            assert data["id"] == "note_123"
            assert data["notebook_id"] == "nb_123"
            assert data["title"] == "T"
            assert data["content"] == "Body"
            assert "created_at" in data

    def test_get_resolves_but_returns_none(self, runner, mock_auth):
        """When resolve succeeds but the GET returns None, exit 1 with typed JSON.

        The contract was flipped from the previous exit-0 ``{found: false}``
        placeholder to the standard typed JSON error envelope (``{error, code,
        message}``) + exit 1. See ``docs/cli-exit-codes.md`` and the BREAKING
        entry in ``CHANGELOG.md`` (Unreleased → Changed).
        """
        with patch_client_for_module("note") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.notes.list = AsyncMock(return_value=[make_note("note_123", "T", "B")])
            mock_client.notes.get = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["note", "get", "note_123", "-n", "nb_123", "--json"])

            assert result.exit_code == 1, result.output
            data = json.loads(result.output)
            assert data["error"] is True
            assert data["code"] == "NOT_FOUND"
            assert "Note not found" in data["message"]
            assert data["id"] == "note_123"
            assert data["notebook_id"] == "nb_123"


class TestNoteSaveJson:
    """JSON shape for ``note save``."""

    def test_save_content(self, runner, mock_auth):
        with patch_client_for_module("note") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.notes.list = AsyncMock(return_value=[make_note("note_123", "T", "Old")])
            mock_client.notes.update = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli,
                    [
                        "note",
                        "save",
                        "note_123",
                        "--content",
                        "New body",
                        "-n",
                        "nb_123",
                        "--json",
                    ],
                )

            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            assert data["id"] == "note_123"
            assert data["saved"] is True
            assert data["content"] == "New body"

    def test_save_no_changes(self, runner, mock_auth):
        """No --title/--content with --json still emits parseable JSON."""
        with patch_client_for_module("note") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["note", "save", "note_123", "-n", "nb_123", "--json"])

            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            assert data["saved"] is False
            assert "error" in data
            # ``notebook_id`` is part of the contract on every JSON response
            # in this module — assert presence so the no-op shape stays in
            # sync with the rest. Value is the raw CLI argument here, so it
            # is the literal string the caller passed.
            assert data["notebook_id"] == "nb_123"

    """JSON shape for ``note rename``."""

    def test_rename_success(self, runner, mock_auth):
        with patch_client_for_module("note") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.notes.list = AsyncMock(return_value=[make_note("note_123", "Old", "Body")])
            mock_client.notes.get = AsyncMock(return_value=make_note("note_123", "Old", "Body"))
            mock_client.notes.update = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli,
                    [
                        "note",
                        "rename",
                        "note_123",
                        "New Title",
                        "-n",
                        "nb_123",
                        "--json",
                    ],
                )

            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            assert data["id"] == "note_123"
            assert data["title"] == "New Title"
            assert data["renamed"] is True

    def test_rename_target_missing_after_resolve_json_exits_1(self, runner, mock_auth):
        """``rename`` race: resolve succeeds, then GET returns ``None`` (note
        deleted between the two calls) → typed JSON ``NOT_FOUND`` envelope +
        exit ``1``.

        Mirrors ``note get``'s Path B contract (see
        ``test_get_resolves_but_returns_none``). The prior shape was a
        ``{renamed: false, error: "Note not found"}`` payload on exit ``0``,
        which silently passed in ``set -e`` scripts that branch on the exit
        code. See ``docs/cli-exit-codes.md`` and the BREAKING entry in
        ``CHANGELOG.md`` (Unreleased → Changed).
        """
        with patch_client_for_module("note") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.notes.list = AsyncMock(return_value=[make_note("note_123", "Old", "Body")])
            mock_client.notes.get = AsyncMock(return_value=None)
            mock_client.notes.update = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli,
                    [
                        "note",
                        "rename",
                        "note_123",
                        "New",
                        "-n",
                        "nb_123",
                        "--json",
                    ],
                )

            assert result.exit_code == 1, result.output
            data = json.loads(result.output)
            assert data["error"] is True
            assert data["code"] == "NOT_FOUND"
            assert "Note not found" in data["message"]
            assert data["id"] == "note_123"
            assert data["notebook_id"] == "nb_123"
            # The race-loser path must NOT issue an update RPC.
            mock_client.notes.update.assert_not_called()

    def test_rename_target_missing_after_resolve_text_exits_1(self, runner, mock_auth):
        """Same race in text mode: ``Note not found`` on stderr + exit 1."""
        with patch_client_for_module("note") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.notes.list = AsyncMock(return_value=[make_note("note_123", "Old", "Body")])
            mock_client.notes.get = AsyncMock(return_value=None)
            mock_client.notes.update = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli,
                    ["note", "rename", "note_123", "New", "-n", "nb_123"],
                )

            assert result.exit_code == 1, result.output
            assert "Note not found" in result.output
            mock_client.notes.update.assert_not_called()


class TestNoteDeleteJson:
    """JSON shape for ``note delete``."""

    def test_delete_success(self, runner, mock_auth):
        with patch_client_for_module("note") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.notes.list = AsyncMock(return_value=[make_note("note_123", "T", "Body")])
            mock_client.notes.delete = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli,
                    [
                        "note",
                        "delete",
                        "note_123",
                        "-n",
                        "nb_123",
                        "-y",
                        "--json",
                    ],
                )

            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            assert data["id"] == "note_123"
            assert data["deleted"] is True

    def test_delete_json_without_yes_emits_typed_error(self, runner, mock_auth):
        """``--json`` mode without ``--yes`` must NOT prompt — it would corrupt
        stdout and break ``subprocess.check_output(...) -> json.loads(...)``
        callers. Instead, surface a typed JSON error and exit non-zero so the
        ``set -e`` / ``check_call`` family of script idioms catches the
        misconfiguration immediately (audit P1.T5).

        Migration from the prior shape: the response previously emitted
        ``{deleted: false, error: "Pass --yes ..."}`` on exit ``0``, which
        passed silently in scripts that branched on the exit code. The new
        contract uses the standard typed envelope (``{error, code:
        "VALIDATION_ERROR", message, ...}``) + exit ``1``. See
        ``docs/cli-exit-codes.md`` and the BREAKING entry in
        ``CHANGELOG.md`` (Unreleased → Changed).
        """
        with patch_client_for_module("note") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.notes.list = AsyncMock(return_value=[make_note("note_123", "T", "Body")])
            # ``delete`` must NOT be invoked — assert that below.
            mock_client.notes.delete = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                # No --yes, no input piped: the command must NOT block on
                # stdin. If it did, ``runner.invoke`` would hang.
                result = runner.invoke(
                    cli,
                    ["note", "delete", "note_123", "-n", "nb_123", "--json"],
                )

            assert result.exit_code == 1, result.output
            # Stdout must be ONLY parseable JSON — no prompt residue.
            data = json.loads(result.output)
            assert data["error"] is True
            assert data["code"] == "VALIDATION_ERROR"
            assert "--yes" in data["message"]
            assert data["id"] == "note_123"
            assert data["notebook_id"] == "nb_123"
            mock_client.notes.delete.assert_not_called()

    def test_delete_non_json_cancelled(self, runner, mock_auth):
        """Non-JSON mode preserves the interactive prompt; declining is a no-op."""
        with patch_client_for_module("note") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.notes.list = AsyncMock(return_value=[make_note("note_123", "T", "Body")])
            mock_client.notes.delete = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli,
                    ["note", "delete", "note_123", "-n", "nb_123"],
                    input="n\n",
                )

            assert result.exit_code == 0, result.output
            # Prompt was printed; deletion was NOT performed.
            assert "Delete note" in result.output
            mock_client.notes.delete.assert_not_called()


# =============================================================================
# Stdin (`-`) convention for ``note create``
# =============================================================================
#
# Unix tradition: ``-`` as a value means "read from stdin". A ``--content``
# flag on ``note create`` makes ``cat notes.md | notebooklm
# note create --content -`` is the canonical pipeline. The positional
# ``CONTENT`` argument also accepts ``-`` for the same reason. Both must be
# mutually exclusive (passing both is a UsageError).


class TestNoteCreateStdinDash:
    """``note create --content -`` and ``note create -`` accept piped stdin."""

    def test_note_create_content_flag_dash_reads_stdin(self, runner, mock_auth):
        with patch_client_for_module("note") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.notes.create = AsyncMock(
                return_value=["note_new", ["note_new", "from stdin", None, None, "New Note"]]
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli,
                    ["note", "create", "--content", "-", "-n", "nb_123"],
                    input="from stdin\n",
                )

            assert result.exit_code == 0, result.output
            # client.notes.create(notebook_id, title, content)
            call = mock_client.notes.create.call_args
            assert call.args[2] == "from stdin"

    def test_note_create_positional_dash_reads_stdin(self, runner, mock_auth):
        with patch_client_for_module("note") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.notes.create = AsyncMock(
                return_value=["note_new", ["note_new", "piped body", None, None, "New Note"]]
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli,
                    ["note", "create", "-", "-n", "nb_123"],
                    input="piped body\n",
                )

            assert result.exit_code == 0, result.output
            call = mock_client.notes.create.call_args
            assert call.args[2] == "piped body"

    def test_note_create_content_flag_literal_value_unchanged(self, runner, mock_auth):
        """Regression: ``--content "literal"`` is not interpreted as stdin."""
        with patch_client_for_module("note") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.notes.create = AsyncMock(
                return_value=["note_new", ["note_new", "literal", None, None, "New Note"]]
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli,
                    ["note", "create", "--content", "literal", "-n", "nb_123"],
                    input="ignored\n",
                )

            assert result.exit_code == 0, result.output
            call = mock_client.notes.create.call_args
            assert call.args[2] == "literal"

    def test_note_create_positional_and_content_flag_conflict(self, runner, mock_auth):
        """Passing both positional CONTENT and --content is a UsageError."""
        with patch_client_for_module("note") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.notes.create = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli,
                    ["note", "create", "positional body", "--content", "flag body", "-n", "nb_123"],
                )

            assert result.exit_code != 0
            assert "Cannot use both" in result.output
