"""Tests for artifact CLI commands."""

import json
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from notebooklm.notebooklm_cli import cli
from notebooklm.types import Artifact

from .conftest import create_mock_client, patch_client_for_module


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

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
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

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
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

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["artifact", "list", "-n", "nb_123", "--json"])

            assert result.exit_code == 0
            data = json.loads(result.output)
            assert "artifacts" in data
            assert data["count"] == 1


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

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
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

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["artifact", "get", "nonexistent", "-n", "nb_123"])

            # Now exits with error from resolve_artifact_id (no match)
            assert result.exit_code == 1
            assert "No artifact found" in result.output


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

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["artifact", "rename", "art_123", "New Title", "-n", "nb_123"]
                )

            assert result.exit_code == 0
            assert "Renamed artifact" in result.output

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

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
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

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["artifact", "delete", "art_123", "-n", "nb_123", "-y"])

            assert result.exit_code == 0
            assert "Deleted artifact" in result.output

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

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
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

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
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

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
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

    def test_artifact_export_failure(self, runner, mock_auth):
        with patch_client_for_module("artifact") as mock_client_cls:
            mock_client = create_mock_client()
            # Mock list for partial ID resolution
            mock_client.artifacts.list = AsyncMock(
                return_value=[Artifact(id="art_123", title="Doc", _artifact_type=2, status=3)]
            )
            mock_client.artifacts.export = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
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

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["artifact", "poll", "task_123", "-n", "nb_123"])

            assert result.exit_code == 0
            assert "Task Status" in result.output


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

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
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

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
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

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
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

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["artifact", "wait", "art_123", "-n", "nb_123", "--json"]
                )

            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["status"] == "completed"
            assert data["artifact_id"] == "art_123"

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

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["artifact", "wait", "art_123", "-n", "nb_123", "--json", "--timeout", "5"]
                )

            assert result.exit_code == 1
            data = json.loads(result.output)
            assert data["status"] == "timeout"


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

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["artifact", "suggestions", "-n", "nb_123"])

            assert result.exit_code == 0
            assert "Suggested Reports" in result.output

    def test_artifact_suggestions_empty(self, runner, mock_auth):
        with patch_client_for_module("artifact") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.artifacts.suggest_reports = AsyncMock(return_value=[])
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
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

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["artifact", "suggestions", "-n", "nb_123", "--json"])

            assert result.exit_code == 0
            data = json.loads(result.output)
            assert len(data) == 1
            assert data[0]["title"] == "Topic 1"


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
