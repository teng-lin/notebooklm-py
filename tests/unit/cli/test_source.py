"""Tests for source CLI commands."""

import importlib
import json
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from notebooklm.notebooklm_cli import cli
from notebooklm.types import (
    Source,
    SourceFulltext,
    SourceNotFoundError,
    SourceProcessingError,
    SourceTimeoutError,
)

from .conftest import create_mock_client, patch_client_for_module

source_module = importlib.import_module("notebooklm.cli.source")


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

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
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

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
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

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
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

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
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

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
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

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
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

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
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

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
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

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
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

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
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
            # Mock sources.list for source delete resolution
            mock_client.sources.list = AsyncMock(
                return_value=[Source(id="src_123", title="Test Source")]
            )
            mock_client.sources.delete = AsyncMock(return_value=True)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["source", "delete", "src_123", "-n", "nb_123", "-y"])

            assert result.exit_code == 0
            assert "Deleted source" in result.output
            mock_client.sources.delete.assert_called_once_with("nb_123", "src_123")

    def test_source_delete_failure(self, runner, mock_auth):
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            # Mock sources.list for source delete resolution
            mock_client.sources.list = AsyncMock(
                return_value=[Source(id="src_123", title="Test Source")]
            )
            mock_client.sources.delete = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["source", "delete", "src_123", "-n", "nb_123", "-y"])

            assert result.exit_code == 0
            assert "Delete may have failed" in result.output

    def test_source_delete_full_uuid_skips_source_list(self, runner, mock_auth):
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.list = AsyncMock()
            mock_client.sources.delete = AsyncMock(return_value=True)
            mock_client_cls.return_value = mock_client

            source_id = "03abe51c-d8df-43ba-ae2d-0efe02c71c4a"
            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["source", "delete", source_id, "-n", "nb_123", "-y"])

            assert result.exit_code == 0
            mock_client.sources.list.assert_not_called()
            mock_client.sources.delete.assert_called_once_with("nb_123", source_id)

    def test_source_delete_long_hex_string_does_not_skip_source_list(self, runner, mock_auth):
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.list = AsyncMock(return_value=[])
            mock_client.sources.delete = AsyncMock(return_value=True)
            mock_client_cls.return_value = mock_client

            source_id = "03abe51cd8df43baae2d0efe02c71c4a"
            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["source", "delete", source_id, "-n", "nb_123", "-y"])

            assert result.exit_code == 1
            mock_client.sources.list.assert_called_once_with("nb_123")
            mock_client.sources.delete.assert_not_called()

    def test_source_delete_title_suggests_delete_by_title(self, runner, mock_auth):
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.list = AsyncMock(
                return_value=[
                    Source(
                        id="03abe51c-d8df-43ba-ae2d-0efe02c71c4a",
                        title="Emails_Verzonden_2026_02",
                    )
                ]
            )
            mock_client.sources.delete = AsyncMock(return_value=True)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["source", "delete", "Emails_Verzonden_2026_02", "-n", "nb_123", "-y"]
                )

            assert result.exit_code == 1
            assert "delete-by-title" in result.output
            mock_client.sources.delete.assert_not_called()

    def test_source_delete_unknown_long_string_fails_locally(self, runner, mock_auth):
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.list = AsyncMock(
                return_value=[Source(id="src_123", title="Test Source")]
            )
            mock_client.sources.delete = AsyncMock(return_value=True)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli,
                    ["source", "delete", "Emails_Verzonden_2025-Q1.txt", "-n", "nb_123", "-y"],
                )

            assert result.exit_code == 1
            assert "No source found starting with" in result.output
            mock_client.sources.delete.assert_not_called()

    def test_source_delete_ambiguous_partial_id(self, runner, mock_auth):
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.list = AsyncMock(
                return_value=[
                    Source(id="src_123", title="First Source"),
                    Source(id="src_456", title="Second Source"),
                ]
            )
            mock_client.sources.delete = AsyncMock(return_value=True)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["source", "delete", "src", "-n", "nb_123", "-y"])

            assert result.exit_code == 1
            assert "Ambiguous ID 'src' matches 2 sources" in result.output
            mock_client.sources.delete.assert_not_called()


class TestSourceDeleteByTitle:
    def test_source_delete_by_title(self, runner, mock_auth):
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.list = AsyncMock(
                return_value=[Source(id="src_123", title="Test Source")]
            )
            mock_client.sources.delete = AsyncMock(return_value=True)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["source", "delete-by-title", "Test Source", "-n", "nb_123", "-y"]
                )

            assert result.exit_code == 0
            assert "Deleted source" in result.output
            mock_client.sources.delete.assert_called_once_with("nb_123", "src_123")

    def test_source_delete_by_title_duplicate_titles(self, runner, mock_auth):
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.list = AsyncMock(
                return_value=[
                    Source(id="src_123", title="Duplicate"),
                    Source(id="src_456", title="Duplicate"),
                ]
            )
            mock_client.sources.delete = AsyncMock(return_value=True)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["source", "delete-by-title", "Duplicate", "-n", "nb_123", "-y"]
                )

            assert result.exit_code == 1
            assert "Delete by ID instead" in result.output
            mock_client.sources.delete.assert_not_called()

    def test_source_delete_by_title_not_found(self, runner, mock_auth):
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.list = AsyncMock(return_value=[])
            mock_client.sources.delete = AsyncMock(return_value=True)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["source", "delete-by-title", "Missing", "-n", "nb_123", "-y"]
                )

            assert result.exit_code == 1
            assert "No source found with title" in result.output
            mock_client.sources.delete.assert_not_called()

    def test_source_delete_by_title_confirmation_shows_title_and_id(self, runner, mock_auth):
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.list = AsyncMock(
                return_value=[Source(id="src_123", title="Test Source")]
            )
            mock_client.sources.delete = AsyncMock(return_value=True)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli,
                    ["source", "delete-by-title", "Test Source", "-n", "nb_123"],
                    input="y\n",
                )

            assert result.exit_code == 0
            assert "Delete source 'Test Source' (src_123)?" in result.output
            mock_client.sources.delete.assert_called_once_with("nb_123", "src_123")


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

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
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

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
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

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
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

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
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

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
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
# SOURCE ADD-RESEARCH TESTS
# =============================================================================


class TestSourceAddResearch:
    def test_add_research_with_import_all_uses_retry_helper(self, runner, mock_auth):
        with (
            patch_client_for_module("source") as mock_client_cls,
            patch.object(source_module, "import_with_retry", new_callable=AsyncMock) as mock_import,
        ):
            mock_client = create_mock_client()
            mock_client.research.start = AsyncMock(return_value={"task_id": "task_123"})
            mock_client.research.poll = AsyncMock(
                return_value={
                    "status": "completed",
                    "task_id": "task_123",
                    "sources": [{"title": "Source 1", "url": "http://example.com"}],
                    "report": "# Report",
                }
            )
            mock_import.return_value = [{"id": "src_1", "title": "Source 1"}]
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli,
                    [
                        "source",
                        "add-research",
                        "AI papers",
                        "--mode",
                        "deep",
                        "--import-all",
                        "-n",
                        "nb_123",
                    ],
                )

        assert result.exit_code == 0
        assert "Imported 1 sources" in result.output
        mock_import.assert_awaited_once_with(
            mock_client,
            "nb_123",
            "task_123",
            [{"title": "Source 1", "url": "http://example.com"}],
        )


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

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
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

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
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

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
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

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
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

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
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

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
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

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
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


# =============================================================================
# SOURCE ADD AUTO-DETECT TESTS
# =============================================================================


class TestSourceAddAutoDetect:
    def test_source_add_autodetect_file(self, runner, mock_auth, tmp_path):
        """Pass a real file path without --type; should auto-detect as 'file'."""
        test_file = tmp_path / "notes.txt"
        test_file.write_text("Some file content")

        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.add_file = AsyncMock(
                return_value=Source(id="src_file", title="notes.txt")
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli,
                    ["source", "add", str(test_file), "-n", "nb_123"],
                )

            assert result.exit_code == 0
            mock_client.sources.add_file.assert_called_once()

    def test_source_add_autodetect_plain_text(self, runner, mock_auth):
        """Pass plain text (not URL, not existing path) without --type.

        Should auto-detect as 'text' with default title 'Pasted Text'.
        """
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.add_text = AsyncMock(
                return_value=Source(id="src_text", title="Pasted Text")
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli,
                    ["source", "add", "This is just some plain text content", "-n", "nb_123"],
                )

            assert result.exit_code == 0
            # Verify add_text was called with the default "Pasted Text" title
            mock_client.sources.add_text.assert_called_once()
            call_args = mock_client.sources.add_text.call_args
            assert call_args[0][1] == "Pasted Text"  # title arg

    def test_source_add_autodetect_text_with_custom_title(self, runner, mock_auth):
        """Pass plain text without --type but with --title.

        Title should be the custom title, not 'Pasted Text'.
        """
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.add_text = AsyncMock(
                return_value=Source(id="src_text", title="Custom Title")
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli,
                    [
                        "source",
                        "add",
                        "This is just some plain text content",
                        "--title",
                        "Custom Title",
                        "-n",
                        "nb_123",
                    ],
                )

            assert result.exit_code == 0
            mock_client.sources.add_text.assert_called_once()
            call_args = mock_client.sources.add_text.call_args
            assert call_args[0][1] == "Custom Title"  # title arg


# =============================================================================
# SOURCE FULLTEXT TESTS
# =============================================================================


class TestSourceFulltext:
    def test_source_fulltext_console_output(self, runner, mock_auth):
        """Short content (<= 2000 chars) is displayed in full."""
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.list = AsyncMock(
                return_value=[Source(id="src_123", title="Test Source")]
            )
            mock_client.sources.get_fulltext = AsyncMock(
                return_value=SourceFulltext(
                    source_id="src_123",
                    title="Test Source",
                    content="This is the full text content.",
                    char_count=30,
                    url=None,
                )
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["source", "fulltext", "src_123", "-n", "nb_123"])

            assert result.exit_code == 0
            assert "src_123" in result.output
            assert "Test Source" in result.output
            assert "This is the full text content." in result.output
            # Should NOT show truncation message for short content
            assert "more chars" not in result.output

    def test_source_fulltext_truncated_output(self, runner, mock_auth):
        """Long content (> 2000 chars) is truncated with a 'more chars' message."""
        long_content = "A" * 3000
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.list = AsyncMock(
                return_value=[Source(id="src_123", title="Test Source")]
            )
            mock_client.sources.get_fulltext = AsyncMock(
                return_value=SourceFulltext(
                    source_id="src_123",
                    title="Long Source",
                    content=long_content,
                    char_count=3000,
                    url=None,
                )
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["source", "fulltext", "src_123", "-n", "nb_123"])

            assert result.exit_code == 0
            assert "more chars" in result.output

    def test_source_fulltext_save_to_file(self, runner, mock_auth, tmp_path):
        """-o flag saves content to file."""
        output_file = tmp_path / "output.txt"
        content = "Full text content to save."

        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.list = AsyncMock(
                return_value=[Source(id="src_123", title="Test Source")]
            )
            mock_client.sources.get_fulltext = AsyncMock(
                return_value=SourceFulltext(
                    source_id="src_123",
                    title="Test Source",
                    content=content,
                    char_count=len(content),
                    url=None,
                )
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli,
                    ["source", "fulltext", "src_123", "-n", "nb_123", "-o", str(output_file)],
                )

            assert result.exit_code == 0
            assert "Saved" in result.output
            assert output_file.read_text(encoding="utf-8") == content

    def test_source_fulltext_json_output(self, runner, mock_auth):
        """--json outputs JSON with fulltext fields."""
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.list = AsyncMock(
                return_value=[Source(id="src_123", title="Test Source")]
            )
            mock_client.sources.get_fulltext = AsyncMock(
                return_value=SourceFulltext(
                    source_id="src_123",
                    title="Test Source",
                    content="Some content",
                    char_count=12,
                    url=None,
                )
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["source", "fulltext", "src_123", "-n", "nb_123", "--json"]
                )

            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["source_id"] == "src_123"
            assert data["title"] == "Test Source"
            assert data["content"] == "Some content"
            assert data["char_count"] == 12

    def test_source_fulltext_with_url(self, runner, mock_auth):
        """Shows URL field when present in fulltext."""
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.list = AsyncMock(
                return_value=[Source(id="src_123", title="Web Source")]
            )
            mock_client.sources.get_fulltext = AsyncMock(
                return_value=SourceFulltext(
                    source_id="src_123",
                    title="Web Source",
                    content="Web page content.",
                    char_count=17,
                    url="https://example.com/page",
                )
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["source", "fulltext", "src_123", "-n", "nb_123"])

            assert result.exit_code == 0
            assert "https://example.com/page" in result.output


# =============================================================================
# SOURCE WAIT TESTS
# =============================================================================


class TestSourceWait:
    def test_source_wait_success(self, runner, mock_auth):
        """wait_until_ready returns a Source → prints 'ready'."""
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.list = AsyncMock(
                return_value=[Source(id="src_123", title="Test Source")]
            )
            mock_client.sources.wait_until_ready = AsyncMock(
                return_value=Source(id="src_123", title="Test Source", status=2)
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["source", "wait", "src_123", "-n", "nb_123"])

            assert result.exit_code == 0
            assert "ready" in result.output.lower()

    def test_source_wait_success_with_title(self, runner, mock_auth):
        """Source has a title → prints the title after 'ready' message."""
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.list = AsyncMock(
                return_value=[Source(id="src_123", title="My Source Title")]
            )
            mock_client.sources.wait_until_ready = AsyncMock(
                return_value=Source(id="src_123", title="My Source Title", status=2)
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["source", "wait", "src_123", "-n", "nb_123"])

            assert result.exit_code == 0
            assert "My Source Title" in result.output

    def test_source_wait_success_json(self, runner, mock_auth):
        """--json output on successful wait."""
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.list = AsyncMock(
                return_value=[Source(id="src_123", title="Test Source")]
            )
            mock_client.sources.wait_until_ready = AsyncMock(
                return_value=Source(id="src_123", title="Test Source", status=2)
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["source", "wait", "src_123", "-n", "nb_123", "--json"])

            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["source_id"] == "src_123"
            assert data["status"] == "ready"

    def test_source_wait_not_found(self, runner, mock_auth):
        """Raises SourceNotFoundError → exit code 1."""
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.list = AsyncMock(
                return_value=[Source(id="src_123", title="Test Source")]
            )
            mock_client.sources.wait_until_ready = AsyncMock(
                side_effect=SourceNotFoundError("src_123")
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["source", "wait", "src_123", "-n", "nb_123"])

            assert result.exit_code == 1
            assert "not found" in result.output.lower()

    def test_source_wait_not_found_json(self, runner, mock_auth):
        """--json on SourceNotFoundError → JSON with status 'not_found', exit 1."""
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.list = AsyncMock(
                return_value=[Source(id="src_123", title="Test Source")]
            )
            mock_client.sources.wait_until_ready = AsyncMock(
                side_effect=SourceNotFoundError("src_123")
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["source", "wait", "src_123", "-n", "nb_123", "--json"])

            assert result.exit_code == 1
            data = json.loads(result.output)
            assert data["status"] == "not_found"
            assert data["source_id"] == "src_123"

    def test_source_wait_processing_error(self, runner, mock_auth):
        """Raises SourceProcessingError → exit code 1."""
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.list = AsyncMock(
                return_value=[Source(id="src_123", title="Test Source")]
            )
            mock_client.sources.wait_until_ready = AsyncMock(
                side_effect=SourceProcessingError("src_123", status=3)
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["source", "wait", "src_123", "-n", "nb_123"])

            assert result.exit_code == 1
            assert "processing failed" in result.output.lower()

    def test_source_wait_processing_error_json(self, runner, mock_auth):
        """--json on SourceProcessingError → JSON with status 'error', exit 1."""
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.list = AsyncMock(
                return_value=[Source(id="src_123", title="Test Source")]
            )
            mock_client.sources.wait_until_ready = AsyncMock(
                side_effect=SourceProcessingError("src_123", status=3)
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["source", "wait", "src_123", "-n", "nb_123", "--json"])

            assert result.exit_code == 1
            data = json.loads(result.output)
            assert data["status"] == "error"
            assert data["source_id"] == "src_123"
            assert data["status_code"] == 3

    def test_source_wait_timeout(self, runner, mock_auth):
        """Raises SourceTimeoutError → exit code 2."""
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.list = AsyncMock(
                return_value=[Source(id="src_123", title="Test Source")]
            )
            mock_client.sources.wait_until_ready = AsyncMock(
                side_effect=SourceTimeoutError("src_123", timeout=30.0, last_status=1)
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["source", "wait", "src_123", "-n", "nb_123"])

            assert result.exit_code == 2
            assert "timeout" in result.output.lower()

    def test_source_wait_timeout_json(self, runner, mock_auth):
        """--json on SourceTimeoutError → JSON with status 'timeout', exit 2."""
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.sources.list = AsyncMock(
                return_value=[Source(id="src_123", title="Test Source")]
            )
            mock_client.sources.wait_until_ready = AsyncMock(
                side_effect=SourceTimeoutError("src_123", timeout=30.0, last_status=1)
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["source", "wait", "src_123", "-n", "nb_123", "--json"])

            assert result.exit_code == 2
            data = json.loads(result.output)
            assert data["status"] == "timeout"
            assert data["source_id"] == "src_123"
            assert data["timeout_seconds"] == 30
            assert data["last_status_code"] == 1
