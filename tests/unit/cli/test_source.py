"""Tests for source CLI commands."""

import json
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from notebooklm.notebooklm_cli import cli
from notebooklm.types import Source

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
# SOURCE LIST TESTS
# =============================================================================


class TestSourceList:
    def test_source_list(self, runner, mock_auth):
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.list = AsyncMock(
                return_value=[
                    Source(id="src_1", title="Source One"),
                ]
            )
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["source", "list", "-n", "nb_123"])

            assert result.exit_code == 0
            assert "Source One" in result.output or "src_1" in result.output

    def test_source_list_json_output(self, runner, mock_auth):
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.list = AsyncMock(
                return_value=[
                    Source(
                        id="src_1",
                        title="Test Source",
                        url="https://example.com",
                    ),
                ]
            )
            mock_client.notebooks.get = AsyncMock(return_value=MagicMock(title="Test Notebook"))
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["source", "list", "-n", "nb_123", "--json"])

            assert result.exit_code == 0
            data = json.loads(result.output)
            assert "sources" in data
            assert data["count"] == 1
            assert data["sources"][0]["id"] == "src_1"


# =============================================================================
# SOURCE ADD TESTS
# =============================================================================


class TestSourceAdd:
    def test_source_add_url(self, runner, mock_auth):
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.add_url = AsyncMock(
                return_value=Source(
                    id="src_new",
                    title="Example",
                    url="https://example.com",
                )
            )
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["source", "add", "https://example.com", "-n", "nb_123"]
                )

            assert result.exit_code == 0

    def test_source_add_youtube_url(self, runner, mock_auth):
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.add_url = AsyncMock(
                return_value=Source(
                    id="src_yt",
                    title="YouTube Video",
                    url="https://youtube.com/watch?v=abc",
                )
            )
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["source", "add", "https://youtube.com/watch?v=abc123", "-n", "nb_123"]
                )

            assert result.exit_code == 0
            mock_client.sources.add_url.assert_called()

    def test_source_add_text(self, runner, mock_auth):
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.add_text = AsyncMock(
                return_value=Source(id="src_text", title="My Text Source")
            )
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli,
                    ["source", "add", "Some text content", "--type", "text", "-n", "nb_123"],
                )

            assert result.exit_code == 0

    def test_source_add_text_with_title(self, runner, mock_auth):
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.add_text = AsyncMock(
                return_value=Source(id="src_text", title="Custom Title")
            )
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli,
                    [
                        "source",
                        "add",
                        "My notes",
                        "--type",
                        "text",
                        "--title",
                        "Custom Title",
                        "-n",
                        "nb_123",
                    ],
                )

            assert result.exit_code == 0

    def test_source_add_file(self, runner, mock_auth, tmp_path):
        # Create a temp file
        test_file = tmp_path / "test.pdf"
        test_file.write_bytes(b"fake pdf content")

        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.add_file = AsyncMock(
                return_value=Source(id="src_file", title="test.pdf")
            )
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli,
                    ["source", "add", str(test_file), "--type", "file", "-n", "nb_123"],
                )

            assert result.exit_code == 0

    def test_source_add_json_output(self, runner, mock_auth):
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.add_url = AsyncMock(
                return_value=Source(
                    id="src_new",
                    title="Example",
                    url="https://example.com",
                )
            )
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["source", "add", "https://example.com", "-n", "nb_123", "--json"]
                )

            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["source"]["id"] == "src_new"


# =============================================================================
# SOURCE GET TESTS
# =============================================================================


class TestSourceGet:
    def test_source_get(self, runner, mock_auth):
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            # Mock sources.list for resolve_source_id
            mock_client.sources.list = AsyncMock(
                return_value=[Source(id="src_123", title="Test Source")]
            )
            mock_client.sources.get = AsyncMock(
                return_value=Source(
                    id="src_123",
                    title="Test Source",
                    url="https://example.com",
                    created_at=datetime(2024, 1, 1),
                )
            )
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["source", "get", "src_123", "-n", "nb_123"])

            assert result.exit_code == 0
            assert "Test Source" in result.output
            assert "src_123" in result.output

    def test_source_get_not_found(self, runner, mock_auth):
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            # Mock sources.list to return empty (no match for resolve_source_id)
            mock_client.sources.list = AsyncMock(return_value=[])
            mock_client.sources.get = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["source", "get", "nonexistent", "-n", "nb_123"])

            # Now exits with error from resolve_source_id (no match)
            assert result.exit_code == 1
            assert "No source found" in result.output


# =============================================================================
# SOURCE DELETE TESTS
# =============================================================================


class TestSourceDelete:
    def test_source_delete(self, runner, mock_auth):
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            # Mock sources.list for resolve_source_id
            mock_client.sources.list = AsyncMock(
                return_value=[Source(id="src_123", title="Test Source")]
            )
            mock_client.sources.delete = AsyncMock(return_value=True)
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["source", "delete", "src_123", "-n", "nb_123", "-y"])

            assert result.exit_code == 0
            assert "Deleted source" in result.output
            mock_client.sources.delete.assert_called_once_with("nb_123", "src_123")

    def test_source_delete_failure(self, runner, mock_auth):
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            # Mock sources.list for resolve_source_id
            mock_client.sources.list = AsyncMock(
                return_value=[Source(id="src_123", title="Test Source")]
            )
            mock_client.sources.delete = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["source", "delete", "src_123", "-n", "nb_123", "-y"])

            assert result.exit_code == 0
            assert "Delete may have failed" in result.output


# =============================================================================
# SOURCE RENAME TESTS
# =============================================================================


class TestSourceRename:
    def test_source_rename(self, runner, mock_auth):
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            # Mock sources.list for resolve_source_id
            mock_client.sources.list = AsyncMock(
                return_value=[Source(id="src_123", title="Old Title")]
            )
            mock_client.sources.rename = AsyncMock(
                return_value=Source(id="src_123", title="New Title")
            )
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["source", "rename", "src_123", "New Title", "-n", "nb_123"]
                )

            assert result.exit_code == 0
            assert "Renamed source" in result.output
            assert "New Title" in result.output


# =============================================================================
# SOURCE REFRESH TESTS
# =============================================================================


class TestSourceRefresh:
    def test_source_refresh(self, runner, mock_auth):
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            # Mock sources.list for resolve_source_id
            mock_client.sources.list = AsyncMock(
                return_value=[Source(id="src_123", title="Original Source")]
            )
            mock_client.sources.refresh = AsyncMock(
                return_value=Source(id="src_123", title="Refreshed Source")
            )
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["source", "refresh", "src_123", "-n", "nb_123"])

            assert result.exit_code == 0
            assert "Source refreshed" in result.output

    def test_source_refresh_no_result(self, runner, mock_auth):
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            # Mock sources.list for resolve_source_id
            mock_client.sources.list = AsyncMock(
                return_value=[Source(id="src_123", title="Original Source")]
            )
            mock_client.sources.refresh = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["source", "refresh", "src_123", "-n", "nb_123"])

            assert result.exit_code == 0
            assert "Refresh returned no result" in result.output


# =============================================================================
# SOURCE ADD-DRIVE TESTS
# =============================================================================


class TestSourceAddDrive:
    def test_source_add_drive_google_doc(self, runner, mock_auth):
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.add_drive = AsyncMock(
                return_value=Source(
                    id="src_drive",
                    title="My Google Doc",
                )
            )
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["source", "add-drive", "drive_file_id", "My Google Doc", "-n", "nb_123"]
                )

            assert result.exit_code == 0
            assert "Added Drive source" in result.output

    def test_source_add_drive_pdf(self, runner, mock_auth):
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.add_drive = AsyncMock(
                return_value=Source(
                    id="src_drive",
                    title="PDF from Drive",
                )
            )
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli,
                    [
                        "source",
                        "add-drive",
                        "file_id",
                        "PDF Title",
                        "--mime-type",
                        "pdf",
                        "-n",
                        "nb_123",
                    ],
                )

            assert result.exit_code == 0


# =============================================================================
# COMMAND EXISTENCE TESTS
# =============================================================================


# =============================================================================
# SOURCE GUIDE TESTS
# =============================================================================


class TestSourceGuide:
    def test_source_guide_with_summary_and_keywords(self, runner, mock_auth):
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.list = AsyncMock(
                return_value=[Source(id="src_123", title="Test Source")]
            )
            mock_client.sources.get_guide = AsyncMock(
                return_value={
                    "summary": "This is a **test** summary about AI.",
                    "keywords": ["AI", "machine learning", "data science"],
                }
            )
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["source", "guide", "src_123", "-n", "nb_123"])

            assert result.exit_code == 0
            assert "Summary" in result.output
            assert "test" in result.output
            assert "Keywords" in result.output
            assert "AI" in result.output

    def test_source_guide_no_guide_available(self, runner, mock_auth):
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.list = AsyncMock(
                return_value=[Source(id="src_123", title="Test Source")]
            )
            mock_client.sources.get_guide = AsyncMock(return_value={"summary": "", "keywords": []})
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["source", "guide", "src_123", "-n", "nb_123"])

            assert result.exit_code == 0
            assert "No guide available" in result.output

    def test_source_guide_json_output(self, runner, mock_auth):
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.list = AsyncMock(
                return_value=[Source(id="src_123", title="Test Source")]
            )
            mock_client.sources.get_guide = AsyncMock(
                return_value={"summary": "Test summary", "keywords": ["keyword1", "keyword2"]}
            )
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["source", "guide", "src_123", "-n", "nb_123", "--json"]
                )

            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["source_id"] == "src_123"
            assert data["summary"] == "Test summary"
            assert data["keywords"] == ["keyword1", "keyword2"]

    def test_source_guide_summary_only(self, runner, mock_auth):
        """Test that summary is displayed even when keywords are empty."""
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.list = AsyncMock(
                return_value=[Source(id="src_123", title="Test Source")]
            )
            mock_client.sources.get_guide = AsyncMock(
                return_value={"summary": "Summary without keywords", "keywords": []}
            )
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["source", "guide", "src_123", "-n", "nb_123"])

            assert result.exit_code == 0
            assert "Summary" in result.output
            assert "Summary without keywords" in result.output
            assert "No guide available" not in result.output

    def test_source_guide_keywords_only(self, runner, mock_auth):
        """Test that keywords are displayed even when summary is empty."""
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.list = AsyncMock(
                return_value=[Source(id="src_123", title="Test Source")]
            )
            mock_client.sources.get_guide = AsyncMock(
                return_value={"summary": "", "keywords": ["AI", "ML", "Data"]}
            )
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["source", "guide", "src_123", "-n", "nb_123"])

            assert result.exit_code == 0
            assert "Keywords" in result.output
            assert "AI" in result.output
            assert "No guide available" not in result.output


# =============================================================================
# SOURCE STALE TESTS
# =============================================================================


class TestSourceStale:
    def test_source_stale_is_stale(self, runner, mock_auth):
        """Test exit code 0 when source is stale (needs refresh)."""
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.list = AsyncMock(
                return_value=[Source(id="src_123", title="Test Source")]
            )
            mock_client.sources.check_freshness = AsyncMock(return_value=False)  # Not fresh = stale
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["source", "stale", "src_123", "-n", "nb_123"])

            assert result.exit_code == 0  # 0 = stale (condition is true)
            assert "stale" in result.output.lower()
            assert "refresh" in result.output.lower()

    def test_source_stale_is_fresh(self, runner, mock_auth):
        """Test exit code 1 when source is fresh (no refresh needed)."""
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.list = AsyncMock(
                return_value=[Source(id="src_123", title="Test Source")]
            )
            mock_client.sources.check_freshness = AsyncMock(return_value=True)  # Fresh
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["source", "stale", "src_123", "-n", "nb_123"])

            assert result.exit_code == 1  # 1 = not stale (condition is false)
            assert "fresh" in result.output.lower()


# =============================================================================
# COMMAND EXISTENCE TESTS
# =============================================================================


class TestSourceCommandsExist:
    def test_source_group_exists(self, runner):
        result = runner.invoke(cli, ["source", "--help"])
        assert result.exit_code == 0
        assert "list" in result.output
        assert "add" in result.output
        assert "delete" in result.output
        assert "guide" in result.output
        assert "stale" in result.output

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

    def test_source_guide_command_exists(self, runner):
        result = runner.invoke(cli, ["source", "guide", "--help"])
        assert result.exit_code == 0
        assert "SOURCE_ID" in result.output
        assert "--json" in result.output

    def test_source_stale_command_exists(self, runner):
        result = runner.invoke(cli, ["source", "stale", "--help"])
        assert result.exit_code == 0
        assert "SOURCE_ID" in result.output
        assert "exit code" in result.output.lower()
