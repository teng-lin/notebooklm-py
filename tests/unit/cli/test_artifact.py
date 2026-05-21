"""Tests for artifact CLI commands."""

import importlib
import json
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from notebooklm.notebooklm_cli import cli
from notebooklm.types import Artifact

from .conftest import create_mock_client, patch_client_for_module

# ``notebooklm.cli.artifact`` (the module) is shadowed by ``cli.__init__``'s
# re-export of the ``artifact`` Click Group (same name). Use ``importlib`` so
# tests target the module's attribute set (``console``, helpers) rather than
# the Click Group sitting at the same dotted path.
artifact_module = importlib.import_module("notebooklm.cli.artifact")


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
# ARTIFACT LIST TESTS
# =============================================================================


class TestArtifactList:
    @pytest.mark.filterwarnings("ignore::notebooklm.types.UnknownTypeWarning")
    def test_artifact_list(self, runner, mock_auth):
        with patch_client_for_module("artifact") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.artifacts.list = AsyncMock(
                return_value=[
                    Artifact(id="art_1", title="Quiz One", _artifact_type=4, status=3),
                    Artifact(id="art_2", title="Briefing Doc", _artifact_type=2, status=3),
                ]
            )
            mock_client.notes.list_mind_maps = AsyncMock(return_value=[])
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["artifact", "list", "-n", "nb_123"])

            assert result.exit_code == 0
            assert "Quiz One" in result.output or "art_1" in result.output

    def test_artifact_list_includes_mind_maps(self, runner, mock_auth):
        """Test that artifacts.list() includes mind maps (they come from the API now)."""
        with patch_client_for_module("artifact") as mock_client_cls:
            mock_client = create_mock_client()
            # Mind maps are now included via artifacts.list() from the notes system
            mock_client.artifacts.list = AsyncMock(
                return_value=[
                    Artifact(id="mm_1", title="My Mind Map", _artifact_type=5, status=3),
                ]
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["artifact", "list", "-n", "nb_123"])

            assert result.exit_code == 0
            assert "Mind Map" in result.output

    def test_artifact_list_json_output(self, runner, mock_auth):
        with patch_client_for_module("artifact") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.artifacts.list = AsyncMock(
                return_value=[
                    Artifact(id="art_1", title="Test Artifact", _artifact_type=4, status=3),
                ]
            )
            mock_client.notes.list_mind_maps = AsyncMock(return_value=[])
            mock_client.notebooks.get = AsyncMock(return_value=MagicMock(title="Test Notebook"))
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["artifact", "list", "-n", "nb_123", "--json"])

            assert result.exit_code == 0
            data = json.loads(result.output)
            assert "artifacts" in data
            assert data["count"] == 1

    @pytest.mark.filterwarnings("ignore::notebooklm.types.UnknownTypeWarning")
    def test_artifact_list_limit_caps_rows(self, runner, mock_auth):
        """`artifact list --limit N` returns at most N data rows."""
        many = [
            Artifact(id=f"art_{i:02d}", title=f"Artifact {i:02d}", _artifact_type=4, status=3)
            for i in range(15)
        ]
        with patch_client_for_module("artifact") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.artifacts.list = AsyncMock(return_value=many)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["artifact", "list", "-n", "nb_123", "--limit", "3"])

            assert result.exit_code == 0, result.output
            for i in range(3):
                assert f"art_{i:02d}" in result.output
            for i in range(3, 15):
                assert f"art_{i:02d}" not in result.output

    @pytest.mark.filterwarnings("ignore::notebooklm.types.UnknownTypeWarning")
    def test_artifact_list_limit_json_caps_rows(self, runner, mock_auth):
        """`artifact list --limit N --json` caps the JSON `artifacts` array."""
        many = [
            Artifact(id=f"art_{i:02d}", title=f"Artifact {i:02d}", _artifact_type=4, status=3)
            for i in range(15)
        ]
        with patch_client_for_module("artifact") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.artifacts.list = AsyncMock(return_value=many)
            mock_client.notebooks.get = AsyncMock(return_value=MagicMock(title="Test"))
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["artifact", "list", "-n", "nb_123", "--limit", "2", "--json"]
                )

            assert result.exit_code == 0, result.output
            data = json.loads(result.output)
            assert data["count"] == 2
            assert len(data["artifacts"]) == 2
            assert [a["id"] for a in data["artifacts"]] == ["art_00", "art_01"]

    def test_artifact_list_no_truncate_disables_ellipsis(self, runner, mock_auth):
        """`artifact list --no-truncate` shows full title without ellipsis.

        The default Title column uses Rich's ``overflow="ellipsis"`` so a
        title that exceeds the auto-detected terminal width is truncated
        with ``…``. ``--no-truncate`` flips the column to ``overflow="fold"``
        so the title wraps instead, preserving every character.
        """
        long_title = "X" * 200
        with patch_client_for_module("artifact") as mock_client_cls:
            mock_client = create_mock_client()
            # _artifact_type=1 (AUDIO) avoids the QUIZ/FLASHCARDS variant
            # disambiguation so the Type column stays narrow ("🎧 Audio")
            # and never injects an ellipsis on its own.
            mock_client.artifacts.list = AsyncMock(
                return_value=[
                    Artifact(id="art_long", title=long_title, _artifact_type=1, status=3),
                ]
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["artifact", "list", "-n", "nb_123", "--no-truncate"])

            assert result.exit_code == 0, result.output
            assert result.output.count("X") >= 200
            assert "…" not in result.output

    def test_artifact_list_default_truncates_long_title(self, runner, mock_auth):
        """Default rendering inserts an ellipsis for over-wide titles."""
        long_title = "X" * 200
        with patch_client_for_module("artifact") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.artifacts.list = AsyncMock(
                return_value=[
                    Artifact(id="art_long", title=long_title, _artifact_type=1, status=3),
                ]
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["artifact", "list", "-n", "nb_123"])

            assert result.exit_code == 0, result.output
            assert result.output.count("X") < 200
            assert "…" in result.output


# =============================================================================
# ARTIFACT GET TESTS
# =============================================================================


class TestArtifactGet:
    def test_artifact_get(self, runner, mock_auth):
        with patch_client_for_module("artifact") as mock_client_cls:
            mock_client = create_mock_client()
            # Mock list for partial ID resolution
            mock_client.artifacts.list = AsyncMock(
                return_value=[
                    Artifact(id="art_123", title="Test Artifact", _artifact_type=4, status=3)
                ]
            )
            mock_client.artifacts.get = AsyncMock(
                return_value=Artifact(
                    id="art_123",
                    title="Test Artifact",
                    _artifact_type=4,
                    status=3,
                    created_at=datetime(2024, 1, 1),
                )
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["artifact", "get", "art_123", "-n", "nb_123"])

            assert result.exit_code == 0
            assert "Test Artifact" in result.output
            assert "art_123" in result.output

    def test_artifact_get_not_found(self, runner, mock_auth):
        with patch_client_for_module("artifact") as mock_client_cls:
            mock_client = create_mock_client()
            # Mock list to return empty (no match for resolve_artifact_id)
            mock_client.artifacts.list = AsyncMock(return_value=[])
            mock_client.artifacts.get = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["artifact", "get", "nonexistent", "-n", "nb_123"])

            # Now exits with error from resolve_artifact_id (no match)
            assert result.exit_code == 1
            assert "No artifact found" in result.output

    def test_artifact_get_json_output(self, runner, mock_auth):
        """`artifact get --json` emits structured JSON mirroring the Artifact."""
        with patch_client_for_module("artifact") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.artifacts.list = AsyncMock(
                return_value=[
                    Artifact(id="art_123", title="Test Artifact", _artifact_type=4, status=3)
                ]
            )
            mock_client.artifacts.get = AsyncMock(
                return_value=Artifact(
                    id="art_123",
                    title="Test Artifact",
                    _artifact_type=4,
                    status=3,
                    created_at=datetime(2024, 1, 1),
                )
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["artifact", "get", "art_123", "-n", "nb_123", "--json"]
                )

            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["id"] == "art_123"
            assert data["title"] == "Test Artifact"
            assert data["found"] is True
            # notebook_id mirrors the wrapper used by `artifact list --json`,
            # so cached responses share one schema across the two commands.
            assert data["notebook_id"] == "nb_123"
            # type / status / created_at keys must be present for automation
            assert "type" in data
            assert "status" in data
            assert "created_at" in data

    def test_artifact_get_json_not_found(self, runner, mock_auth):
        """`artifact get --json` emits typed JSON error + exits 1.

        ``client.artifacts.get`` may return ``None`` after a successful partial-ID
        resolve when the server reports the artifact has been deleted between
        the list call and the get call. The current contract emits the standard
        typed JSON error envelope (``{error, code, message}``) + exit 1
        (changed from the previous exit-0 ``{found: false}`` placeholder).
        See ``docs/cli-exit-codes.md`` and the BREAKING entry in ``CHANGELOG.md``.
        """
        with patch_client_for_module("artifact") as mock_client_cls:
            mock_client = create_mock_client()
            # Resolve succeeds (list contains the ID) but get() returns None
            # (e.g., concurrent delete from another session).
            mock_client.artifacts.list = AsyncMock(
                return_value=[Artifact(id="art_123", title="Doomed", _artifact_type=4, status=3)]
            )
            mock_client.artifacts.get = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["artifact", "get", "art_123", "-n", "nb_123", "--json"]
                )

            assert result.exit_code == 1, result.output
            data = json.loads(result.output)
            assert data["error"] is True
            assert data["code"] == "NOT_FOUND"
            assert "Artifact not found" in data["message"]
            assert data["id"] == "art_123"
            assert data["notebook_id"] == "nb_123"

    # -------------------------------------------------------------------------
    # get-on-not-found exits 1 (changed from 0). Mirrors the
    # ``test_source.py`` Path A / Path B coverage so the contract on the two
    # ``get`` commands matches.
    # -------------------------------------------------------------------------

    def test_artifact_get_not_found_pathA_long_id_text_exits_1(self, runner, mock_auth):
        """Path A: UUID-shaped ID skips partial-resolve; backend None → exit 1."""
        # Canonical 36-char UUID — qualifies for the resolver's full-ID fast-path
        # so artifacts.list is bypassed and the backend ``get`` is hit directly.
        long_id = "abc12345-6789-4abc-def0-1234567890ab"
        with patch_client_for_module("artifact") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.artifacts.list = AsyncMock(return_value=[])
            mock_client.artifacts.get = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["artifact", "get", long_id, "-n", "nb_123"])

            assert result.exit_code == 1, result.output
            assert "Artifact not found" in result.output
            mock_client.artifacts.list.assert_not_called()

    def test_artifact_get_not_found_pathA_long_id_json_exits_1(self, runner, mock_auth):
        """Path A under ``--json``: typed JSON error doc + exit 1."""
        long_id = "abc12345-6789-4abc-def0-1234567890ab"
        with patch_client_for_module("artifact") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.artifacts.list = AsyncMock(return_value=[])
            mock_client.artifacts.get = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["artifact", "get", long_id, "-n", "nb_123", "--json"])

            assert result.exit_code == 1, result.output
            data = json.loads(result.output)
            assert data["error"] is True
            assert data["code"] == "NOT_FOUND"
            assert "Artifact not found" in data["message"]
            assert data["id"] == long_id
            assert data["notebook_id"] == "nb_123"
            mock_client.artifacts.list.assert_not_called()

    def test_artifact_get_not_found_pathB_resolved_then_none_text_exits_1(self, runner, mock_auth):
        """Path B: partial-resolve succeeds, backend get() returns None → exit 1."""
        with patch_client_for_module("artifact") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.artifacts.list = AsyncMock(
                return_value=[Artifact(id="art_xyz", title="Doomed", _artifact_type=4, status=3)]
            )
            mock_client.artifacts.get = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["artifact", "get", "art_xyz", "-n", "nb_123"])

            assert result.exit_code == 1, result.output
            assert "Artifact not found" in result.output


# =============================================================================
# ARTIFACT RENAME TESTS
# =============================================================================


class TestArtifactRename:
    def test_artifact_rename(self, runner, mock_auth):
        with patch_client_for_module("artifact") as mock_client_cls:
            mock_client = create_mock_client()
            # Mock list for partial ID resolution
            mock_client.artifacts.list = AsyncMock(
                return_value=[Artifact(id="art_123", title="Old Title", _artifact_type=4, status=3)]
            )
            mock_client.notes.list_mind_maps = AsyncMock(return_value=[])
            mock_client.artifacts.rename = AsyncMock(
                return_value=Artifact(id="art_123", title="New Title", _artifact_type=4, status=3)
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["artifact", "rename", "art_123", "New Title", "-n", "nb_123"]
                )

            assert result.exit_code == 0
            assert "Renamed artifact" in result.output

    def test_artifact_rename_json_output(self, runner, mock_auth):
        """`artifact rename --json` emits a structured success payload."""
        with patch_client_for_module("artifact") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.artifacts.list = AsyncMock(
                return_value=[Artifact(id="art_123", title="Old Title", _artifact_type=4, status=3)]
            )
            mock_client.notes.list_mind_maps = AsyncMock(return_value=[])
            mock_client.artifacts.rename = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli,
                    ["artifact", "rename", "art_123", "New Title", "-n", "nb_123", "--json"],
                )

            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data == {"id": "art_123", "renamed": True, "new_title": "New Title"}

    def test_artifact_rename_rejects_mind_map(self, runner, mock_auth):
        with patch_client_for_module("artifact") as mock_client_cls:
            mock_client = create_mock_client()
            # Mock list for partial ID resolution (include the mind map)
            mock_client.artifacts.list = AsyncMock(
                return_value=[Artifact(id="mm_123", title="Old Title", _artifact_type=5, status=3)]
            )
            mock_client.notes.list_mind_maps = AsyncMock(
                return_value=[
                    ["mm_123", ["mm_123", "{}", None, None, "Old Title"]],
                ]
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["artifact", "rename", "mm_123", "New Title", "-n", "nb_123"]
                )

            assert result.exit_code != 0
            assert "Mind maps cannot be renamed" in result.output


# =============================================================================
# ARTIFACT DELETE TESTS
# =============================================================================


class TestArtifactDelete:
    def test_artifact_delete(self, runner, mock_auth):
        with patch_client_for_module("artifact") as mock_client_cls:
            mock_client = create_mock_client()
            # Mock list for partial ID resolution
            mock_client.artifacts.list = AsyncMock(
                return_value=[
                    Artifact(id="art_123", title="Test Artifact", _artifact_type=4, status=3)
                ]
            )
            mock_client.notes.list_mind_maps = AsyncMock(return_value=[])
            mock_client.artifacts.delete = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["artifact", "delete", "art_123", "-n", "nb_123", "-y"])

            assert result.exit_code == 0
            assert "Deleted artifact" in result.output

    def test_artifact_delete_json_output(self, runner, mock_auth):
        """`artifact delete --json` emits a structured success payload."""
        with patch_client_for_module("artifact") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.artifacts.list = AsyncMock(
                return_value=[
                    Artifact(id="art_123", title="Test Artifact", _artifact_type=4, status=3)
                ]
            )
            mock_client.notes.list_mind_maps = AsyncMock(return_value=[])
            mock_client.artifacts.delete = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["artifact", "delete", "art_123", "-n", "nb_123", "-y", "--json"]
                )

            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data == {"id": "art_123", "deleted": True}

    def test_artifact_delete_mind_map_json_output(self, runner, mock_auth):
        """`artifact delete --json` flags mind-map carve-out in the payload."""
        with patch_client_for_module("artifact") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.artifacts.list = AsyncMock(
                return_value=[
                    Artifact(id="mm_456", title="Mind Map Title", _artifact_type=5, status=3)
                ]
            )
            mock_client.notes.list_mind_maps = AsyncMock(
                return_value=[
                    ["mm_456", ["mm_456", "{}", None, None, "Mind Map Title"]],
                ]
            )
            mock_client.notes.delete = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["artifact", "delete", "mm_456", "-n", "nb_123", "-y", "--json"]
                )

            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["id"] == "mm_456"
            assert data["deleted"] is True
            assert data["kind"] == "mind_map"

    def test_artifact_delete_mind_map_clears(self, runner, mock_auth):
        with patch_client_for_module("artifact") as mock_client_cls:
            mock_client = create_mock_client()
            # Mock list for partial ID resolution (include the mind map)
            mock_client.artifacts.list = AsyncMock(
                return_value=[
                    Artifact(id="mm_456", title="Mind Map Title", _artifact_type=5, status=3)
                ]
            )
            mock_client.notes.list_mind_maps = AsyncMock(
                return_value=[
                    ["mm_456", ["mm_456", "{}", None, None, "Mind Map Title"]],
                ]
            )
            mock_client.notes.delete = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["artifact", "delete", "mm_456", "-n", "nb_123", "-y"])

            assert result.exit_code == 0
            assert "Cleared mind map" in result.output
            mock_client.notes.delete.assert_called_once_with("nb_123", "mm_456")


# =============================================================================
# ARTIFACT EXPORT TESTS
# =============================================================================


class TestArtifactExport:
    def test_artifact_export_docs(self, runner, mock_auth):
        with patch_client_for_module("artifact") as mock_client_cls:
            mock_client = create_mock_client()
            # Mock list for partial ID resolution
            mock_client.artifacts.list = AsyncMock(
                return_value=[Artifact(id="art_123", title="Doc", _artifact_type=2, status=3)]
            )
            mock_client.artifacts.export = AsyncMock(
                return_value={"url": "https://docs.google.com/document/d/123"}
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["artifact", "export", "art_123", "--title", "My Export", "-n", "nb_123"]
                )

            assert result.exit_code == 0
            assert "Exported to Google Docs" in result.output
            # Verify export was called with correct arguments
            mock_client.artifacts.export.assert_called_once()
            call_args = mock_client.artifacts.export.call_args
            from notebooklm.rpc import ExportType

            # call_args[0] = (notebook_id, artifact_id, content, title, export_type)
            assert call_args[0][2] is None, "content should be None (backend retrieves it)"
            assert call_args[0][4] == ExportType.DOCS, "export_type should be ExportType.DOCS"

    def test_artifact_export_sheets(self, runner, mock_auth):
        with patch_client_for_module("artifact") as mock_client_cls:
            mock_client = create_mock_client()
            # Mock list for partial ID resolution
            mock_client.artifacts.list = AsyncMock(
                return_value=[Artifact(id="art_123", title="Table", _artifact_type=9, status=3)]
            )
            mock_client.artifacts.export = AsyncMock(
                return_value={"url": "https://sheets.google.com/spreadsheets/d/123"}
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli,
                    [
                        "artifact",
                        "export",
                        "art_123",
                        "--title",
                        "My Sheet",
                        "--type",
                        "sheets",
                        "-n",
                        "nb_123",
                    ],
                )

            assert result.exit_code == 0
            assert "Exported to Google Sheets" in result.output
            # Verify export was called with correct arguments
            mock_client.artifacts.export.assert_called_once()
            call_args = mock_client.artifacts.export.call_args
            from notebooklm.rpc import ExportType

            # call_args[0] = (notebook_id, artifact_id, content, title, export_type)
            assert call_args[0][2] is None, "content should be None (backend retrieves it)"
            assert call_args[0][4] == ExportType.SHEETS, "export_type should be ExportType.SHEETS"

    def test_artifact_export_json_output(self, runner, mock_auth):
        """`artifact export --json` emits structured payload with the export result."""
        with patch_client_for_module("artifact") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.artifacts.list = AsyncMock(
                return_value=[Artifact(id="art_123", title="Doc", _artifact_type=2, status=3)]
            )
            mock_client.artifacts.export = AsyncMock(
                return_value={"url": "https://docs.google.com/document/d/123"}
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli,
                    [
                        "artifact",
                        "export",
                        "art_123",
                        "--title",
                        "My Export",
                        "-n",
                        "nb_123",
                        "--json",
                    ],
                )

            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["id"] == "art_123"
            assert data["exported"] is True
            assert data["export_type"] == "docs"
            assert data["title"] == "My Export"
            assert data["result"] == {"url": "https://docs.google.com/document/d/123"}

    def test_artifact_export_failure(self, runner, mock_auth):
        with patch_client_for_module("artifact") as mock_client_cls:
            mock_client = create_mock_client()
            # Mock list for partial ID resolution
            mock_client.artifacts.list = AsyncMock(
                return_value=[Artifact(id="art_123", title="Doc", _artifact_type=2, status=3)]
            )
            mock_client.artifacts.export = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["artifact", "export", "art_123", "--title", "Fail", "-n", "nb_123"]
                )

            assert result.exit_code == 0
            assert "Export may have failed" in result.output


# =============================================================================
# ARTIFACT POLL TESTS
# =============================================================================


class TestArtifactPoll:
    def test_artifact_poll(self, runner, mock_auth):
        with patch_client_for_module("artifact") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.artifacts.poll_status = AsyncMock(
                return_value={"status": "completed", "artifact_id": "art_123"}
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["artifact", "poll", "task_123", "-n", "nb_123"])

            assert result.exit_code == 0
            assert "Task Status" in result.output

    def test_artifact_poll_json_output(self, runner, mock_auth):
        """`artifact poll --json` mirrors the GenerationStatus dataclass fields."""
        from notebooklm.types import GenerationStatus

        with patch_client_for_module("artifact") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.artifacts.poll_status = AsyncMock(
                return_value=GenerationStatus(
                    task_id="task_123",
                    status="completed",
                    url="https://example.com/audio.mp3",
                    error=None,
                )
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["artifact", "poll", "task_123", "-n", "nb_123", "--json"]
                )

            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["task_id"] == "task_123"
            assert data["status"] == "completed"
            assert data["url"] == "https://example.com/audio.mp3"
            assert data["error"] is None


# =============================================================================
# ARTIFACT WAIT TESTS
# =============================================================================


class TestArtifactWait:
    def test_artifact_wait_completed(self, runner, mock_auth):
        """Test waiting for artifact that completes successfully."""
        with patch_client_for_module("artifact") as mock_client_cls:
            mock_client = create_mock_client()
            # Mock list for partial ID resolution
            mock_client.artifacts.list = AsyncMock(
                return_value=[Artifact(id="art_123", title="Test", _artifact_type=1, status=3)]
            )
            mock_client.artifacts.wait_for_completion = AsyncMock(
                return_value=MagicMock(
                    status="completed", url="https://example.com/audio.mp3", error=None
                )
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["artifact", "wait", "art_123", "-n", "nb_123"])

            assert result.exit_code == 0
            assert "Artifact completed" in result.output

    def test_artifact_wait_failed(self, runner, mock_auth):
        """Test waiting for artifact that fails generation."""
        with patch_client_for_module("artifact") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.artifacts.list = AsyncMock(
                return_value=[Artifact(id="art_123", title="Test", _artifact_type=1, status=1)]
            )
            mock_client.artifacts.wait_for_completion = AsyncMock(
                return_value=MagicMock(
                    status="failed", url=None, error="Generation failed due to content policy"
                )
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["artifact", "wait", "art_123", "-n", "nb_123"])

            assert result.exit_code == 1
            assert "Generation failed" in result.output

    def test_artifact_wait_timeout(self, runner, mock_auth):
        """Test waiting for artifact that times out."""
        with patch_client_for_module("artifact") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.artifacts.list = AsyncMock(
                return_value=[Artifact(id="art_123", title="Test", _artifact_type=1, status=1)]
            )
            mock_client.artifacts.wait_for_completion = AsyncMock(
                side_effect=TimeoutError("Timed out")
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["artifact", "wait", "art_123", "-n", "nb_123", "--timeout", "5"]
                )

            assert result.exit_code == 1
            assert "Timeout" in result.output

    def test_artifact_wait_json_output(self, runner, mock_auth):
        """Test waiting with JSON output."""
        with patch_client_for_module("artifact") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.artifacts.list = AsyncMock(
                return_value=[Artifact(id="art_123", title="Test", _artifact_type=1, status=3)]
            )
            mock_client.artifacts.wait_for_completion = AsyncMock(
                return_value=MagicMock(
                    status="completed", url="https://example.com/audio.mp3", error=None
                )
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["artifact", "wait", "art_123", "-n", "nb_123", "--json"]
                )

            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["status"] == "completed"
            assert data["artifact_id"] == "art_123"

    def test_artifact_wait_timeout_interval_forwarded(self, runner, mock_auth):
        """`artifact wait --timeout 60 --interval 5` plumbs both into
        wait_for_completion.

        Pre-existing behavior — already had `--timeout` and `--interval` —
        but pin the wiring so the shared `wait_polling_options` decorator
        cannot silently drop one of the values during the refactor.
        """
        with patch_client_for_module("artifact") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.artifacts.list = AsyncMock(
                return_value=[Artifact(id="art_123", title="Test", _artifact_type=1, status=3)]
            )
            mock_client.artifacts.wait_for_completion = AsyncMock(
                return_value=MagicMock(
                    status="completed", url="https://example.com/audio.mp3", error=None
                )
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli,
                    [
                        "artifact",
                        "wait",
                        "art_123",
                        "-n",
                        "nb_123",
                        "--timeout",
                        "60",
                        "--interval",
                        "5",
                    ],
                )

            assert result.exit_code == 0, result.output
            mock_client.artifacts.wait_for_completion.assert_awaited_once()
            kwargs = mock_client.artifacts.wait_for_completion.await_args.kwargs
            assert kwargs.get("timeout") == 60.0
            assert kwargs.get("initial_interval") == 5.0, (
                f"expected --interval=5 to plumb into wait_for_completion, got kwargs={kwargs}"
            )
            assert "poll_interval" not in kwargs

    def test_artifact_wait_timeout_json_output(self, runner, mock_auth):
        """Test timeout with JSON output."""
        with patch_client_for_module("artifact") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.artifacts.list = AsyncMock(
                return_value=[Artifact(id="art_123", title="Test", _artifact_type=1, status=1)]
            )
            mock_client.artifacts.wait_for_completion = AsyncMock(
                side_effect=TimeoutError("Timed out")
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["artifact", "wait", "art_123", "-n", "nb_123", "--json", "--timeout", "5"]
                )

            assert result.exit_code == 1
            data = json.loads(result.output)
            assert data["status"] == "timeout"

    def test_artifact_wait_invokes_console_status(self, runner, mock_auth):
        """`artifact wait` wraps the polling call in `console.status`.

        The spinner gives interactive users feedback during the blocking wait.
        Asserts the wrap by patching `notebooklm.cli.artifact.console.status`
        and confirming it is invoked exactly once with a message that mentions
        the wait. Does not assert under `--json` because the JSON path
        intentionally suppresses the spinner to keep stdout pure JSON.
        """
        with patch_client_for_module("artifact") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.artifacts.list = AsyncMock(
                return_value=[Artifact(id="art_123", title="Test", _artifact_type=1, status=3)]
            )
            mock_client.artifacts.wait_for_completion = AsyncMock(
                return_value=MagicMock(
                    status="completed", url="https://example.com/audio.mp3", error=None
                )
            )
            mock_client_cls.return_value = mock_client

            with (
                patch(
                    "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
                ) as mock_fetch,
                patch.object(artifact_module.console, "status") as mock_status,
            ):
                mock_fetch.return_value = ("csrf", "session")
                mock_status.return_value.__enter__ = MagicMock(return_value=MagicMock())
                mock_status.return_value.__exit__ = MagicMock(return_value=False)
                result = runner.invoke(cli, ["artifact", "wait", "art_123", "-n", "nb_123"])

            assert result.exit_code == 0, result.output
            assert mock_status.called, "expected console.status to wrap the wait call"
            status_msg = mock_status.call_args.args[0]
            assert "artifact" in status_msg.lower() or "wait" in status_msg.lower(), (
                f"expected status message to describe the wait, got: {status_msg!r}"
            )

    def test_artifact_wait_json_skips_console_status(self, runner, mock_auth):
        """`artifact wait --json` must NOT invoke console.status (stdout stays JSON).

        The spinner is suppressed under JSON mode so automation parsing stdout
        does not see Rich escape sequences leak in.
        """
        with patch_client_for_module("artifact") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.artifacts.list = AsyncMock(
                return_value=[Artifact(id="art_123", title="Test", _artifact_type=1, status=3)]
            )
            mock_client.artifacts.wait_for_completion = AsyncMock(
                return_value=MagicMock(
                    status="completed", url="https://example.com/audio.mp3", error=None
                )
            )
            mock_client_cls.return_value = mock_client

            with (
                patch(
                    "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
                ) as mock_fetch,
                patch.object(artifact_module.console, "status") as mock_status,
            ):
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["artifact", "wait", "art_123", "-n", "nb_123", "--json"]
                )

            assert result.exit_code == 0, result.output
            assert not mock_status.called, (
                "console.status must NOT be invoked under --json (would leak ANSI into stdout)"
            )

    def test_artifact_wait_sigint_prints_resume_hint_and_exits_130(self, runner, mock_auth):
        """Ctrl-C during ``artifact wait`` exits 130 with the canonical resume hint
        naming the resolved artifact id.

        Same hint shape as ``generate <kind> --wait`` because both polling
        loops resume via ``artifact poll``. Simulates the Ctrl-C by raising
        ``KeyboardInterrupt`` from the awaitable that the wait loop is
        currently suspended on.
        """
        with patch_client_for_module("artifact") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.artifacts.list = AsyncMock(
                return_value=[Artifact(id="art_sigint", title="Test", _artifact_type=1, status=3)]
            )
            mock_client.artifacts.wait_for_completion = AsyncMock(side_effect=KeyboardInterrupt)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["artifact", "wait", "art_sigint", "-n", "nb_123"])

        assert result.exit_code == 130, (
            f"expected SIGINT exit 130, got {result.exit_code}; output={result.output!r}"
        )
        combined = result.output + (result.stderr if result.stderr_bytes else "")
        assert "Cancelled. Resume with: notebooklm artifact poll art_sigint" in combined, (
            f"expected canonical resume hint with artifact id; got: {combined!r}"
        )

    def test_artifact_wait_sigint_json_emits_cancelled_envelope(self, runner, mock_auth):
        """Ctrl-C under ``artifact wait --json`` emits a CANCELLED envelope with
        the resume hint, exits 130.

        Keeps stdout-as-JSON automation from breaking on a Python traceback.
        """
        with patch_client_for_module("artifact") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.artifacts.list = AsyncMock(
                return_value=[Artifact(id="art_json_sigint", title="T", _artifact_type=1, status=3)]
            )
            mock_client.artifacts.wait_for_completion = AsyncMock(side_effect=KeyboardInterrupt)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["artifact", "wait", "art_json_sigint", "-n", "nb_123", "--json"]
                )

        assert result.exit_code == 130
        assert '"code": "CANCELLED"' in result.output
        assert "notebooklm artifact poll art_json_sigint" in result.output


# =============================================================================
# ARTIFACT SUGGESTIONS TESTS
# =============================================================================


class TestArtifactSuggestions:
    def test_artifact_suggestions(self, runner, mock_auth):
        with patch_client_for_module("artifact") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.artifacts.suggest_reports = AsyncMock(
                return_value=[
                    MagicMock(title="Topic 1", description="Desc 1", prompt="Prompt 1"),
                    MagicMock(title="Topic 2", description="Desc 2", prompt="Prompt 2"),
                ]
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["artifact", "suggestions", "-n", "nb_123"])

            assert result.exit_code == 0
            assert "Suggested Reports" in result.output

    def test_artifact_suggestions_empty(self, runner, mock_auth):
        with patch_client_for_module("artifact") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.artifacts.suggest_reports = AsyncMock(return_value=[])
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["artifact", "suggestions", "-n", "nb_123"])

            assert result.exit_code == 0
            assert "No suggestions available" in result.output

    def test_artifact_suggestions_json(self, runner, mock_auth):
        with patch_client_for_module("artifact") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.artifacts.suggest_reports = AsyncMock(
                return_value=[
                    MagicMock(title="Topic 1", description="Desc 1", prompt="Prompt 1"),
                ]
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["artifact", "suggestions", "-n", "nb_123", "--json"])

            assert result.exit_code == 0
            data = json.loads(result.output)
            assert len(data) == 1
            assert data[0]["title"] == "Topic 1"

    def test_artifact_suggestions_json_preserves_unicode(self, runner, mock_auth):
        """CJK / emoji in suggestion titles should be emitted as real UTF-8."""
        with patch_client_for_module("artifact") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.artifacts.suggest_reports = AsyncMock(
                return_value=[
                    MagicMock(title="中文主题 🚀", description="说明", prompt="问题"),
                ]
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["artifact", "suggestions", "-n", "nb_123", "--json"])

            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data[0]["title"] == "中文主题 🚀"
            assert data[0]["description"] == "说明"
            assert data[0]["prompt"] == "问题"
            # Raw output must contain real CJK/emoji, not escaped sequences.
            assert "中文主题" in result.output
            assert "🚀" in result.output
            assert "\\u" not in result.output


# =============================================================================
# COMMAND EXISTENCE TESTS
# =============================================================================


class TestArtifactCommandsExist:
    def test_artifact_group_exists(self, runner):
        result = runner.invoke(cli, ["artifact", "--help"])
        assert result.exit_code == 0
        assert "list" in result.output
        assert "get" in result.output
        assert "delete" in result.output
        assert "wait" in result.output

    def test_artifact_list_command_exists(self, runner):
        result = runner.invoke(cli, ["artifact", "list", "--help"])
        assert result.exit_code == 0
        assert "--type" in result.output

    def test_artifact_wait_command_exists(self, runner):
        result = runner.invoke(cli, ["artifact", "wait", "--help"])
        assert result.exit_code == 0
        assert "--timeout" in result.output
        assert "--interval" in result.output
        assert "--json" in result.output


# =============================================================================
# POLL vs WAIT ID-KIND DOCUMENTATION SNAPSHOT
# =============================================================================


class TestPollWaitIdKindHelp:
    """Snapshot test that pins the canonical ID-source phrasing in help text.

    `artifact poll` and `artifact wait` both accept the same underlying
    identifier (the API returns one ID that serves as both the generation
    task_id and the eventual artifact_id; see ``_artifacts.py``
    ``_parse_generation_result`` docstring). The two commands diverge in:

    1. **Resolution**: ``poll`` passes the raw ID through (so it works
       *immediately* after ``generate <type>`` returns, before the artifact
       appears in any list); ``wait`` partial-matches against
       ``artifact list`` output via ``resolve_artifact_id``.
    2. **Blocking**: ``poll`` is a single non-blocking check; ``wait`` blocks
       with ``--timeout`` / ``--interval`` exponential backoff.

    These tests assert the help docstrings explicitly cite where each ID
    typically comes from so users stop confusing them. If a future docstring
    refactor removes the canonical phrasing, these snapshots fail loudly.
    """

    # Canonical phrases — pinned here as the source of truth.
    POLL_PHRASE_TASK_ID_FROM_GENERATE = "task_id"
    POLL_PHRASE_GENERATE_REFERENCE = "generate"
    WAIT_PHRASE_ARTIFACT_LIST_REFERENCE = "artifact list"
    SHARED_PHRASE_SAME_ID = "same identifier"

    def test_poll_help_cites_task_id_source(self, runner):
        """`artifact poll --help` must explain the task_id comes from `generate`."""
        result = runner.invoke(cli, ["artifact", "poll", "--help"])
        assert result.exit_code == 0
        # Must reference the task_id concept and where it comes from.
        assert self.POLL_PHRASE_TASK_ID_FROM_GENERATE in result.output, (
            "poll --help must reference 'task_id' as the ID kind"
        )
        assert self.POLL_PHRASE_GENERATE_REFERENCE in result.output, (
            "poll --help must reference `generate` as the source of the ID"
        )

    def test_wait_help_cites_artifact_list_source(self, runner):
        """`artifact wait --help` must explain the artifact_id comes from `artifact list`."""
        result = runner.invoke(cli, ["artifact", "wait", "--help"])
        assert result.exit_code == 0
        # Must reference the artifact_id discovery path.
        assert self.WAIT_PHRASE_ARTIFACT_LIST_REFERENCE in result.output, (
            "wait --help must reference `artifact list` as the source of the ID"
        )

    def test_both_help_acknowledge_same_underlying_id(self, runner):
        """Both `--help` outputs should acknowledge that the underlying ID is the same.

        This prevents users from believing they need to convert between two
        distinct ID kinds when copy-pasting between `poll` and `wait`.
        """
        poll_result = runner.invoke(cli, ["artifact", "poll", "--help"])
        wait_result = runner.invoke(cli, ["artifact", "wait", "--help"])
        assert poll_result.exit_code == 0
        assert wait_result.exit_code == 0
        assert self.SHARED_PHRASE_SAME_ID in poll_result.output, (
            "poll --help must acknowledge poll/wait share the same identifier"
        )
        assert self.SHARED_PHRASE_SAME_ID in wait_result.output, (
            "wait --help must acknowledge poll/wait share the same identifier"
        )

    def test_poll_help_includes_concrete_example(self, runner):
        """poll --help should include a runnable example."""
        result = runner.invoke(cli, ["artifact", "poll", "--help"])
        assert result.exit_code == 0
        assert "notebooklm artifact poll" in result.output, (
            "poll --help must include at least one concrete example invocation"
        )

    def test_wait_help_includes_concrete_example(self, runner):
        """wait --help should retain the existing concrete examples."""
        result = runner.invoke(cli, ["artifact", "wait", "--help"])
        assert result.exit_code == 0
        assert "notebooklm artifact wait" in result.output, (
            "wait --help must include at least one concrete example invocation"
        )
