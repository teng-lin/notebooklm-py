"""Tests for research CLI commands."""

import importlib
import json
from unittest.mock import AsyncMock, patch

from notebooklm.notebooklm_cli import cli

from .conftest import create_mock_client, patch_client_for_module

research_module = importlib.import_module("notebooklm.cli.research")
helpers_module = importlib.import_module("notebooklm.cli.helpers")

# =============================================================================
# RESEARCH STATUS TESTS
# =============================================================================


class TestResearchStatus:
    def test_status_no_research(self, runner, mock_auth, mock_fetch_tokens):
        with patch_client_for_module("research") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.research.poll = AsyncMock(return_value={"status": "no_research"})
            mock_client_cls.return_value = mock_client

            result = runner.invoke(cli, ["research", "status", "-n", "nb_123"])

        assert result.exit_code == 0
        assert "No research running" in result.output

    def test_status_in_progress(self, runner, mock_auth, mock_fetch_tokens):
        with patch_client_for_module("research") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.research.poll = AsyncMock(
                return_value={"status": "in_progress", "query": "AI research"}
            )
            mock_client_cls.return_value = mock_client

            result = runner.invoke(cli, ["research", "status", "-n", "nb_123"])

        assert result.exit_code == 0
        assert "Research in progress" in result.output
        assert "AI research" in result.output

    def test_status_completed(self, runner, mock_auth, mock_fetch_tokens):
        with patch_client_for_module("research") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.research.poll = AsyncMock(
                return_value={
                    "status": "completed",
                    "query": "AI research",
                    "sources": [
                        {"title": "Source 1", "url": "http://example.com/1"},
                        {"title": "Source 2", "url": "http://example.com/2"},
                    ],
                    "summary": "This is a summary of the research results.",
                    "report": "# Research Report\nDetailed findings here.",
                }
            )
            mock_client_cls.return_value = mock_client

            result = runner.invoke(cli, ["research", "status", "-n", "nb_123"])

        assert result.exit_code == 0
        assert "Research completed" in result.output
        assert "Found 2 sources" in result.output
        assert "Source 1" in result.output
        assert "Research Report" in result.output

    def test_status_completed_with_many_sources(self, runner, mock_auth, mock_fetch_tokens):
        """Test that more than 10 sources shows truncation message."""
        with patch_client_for_module("research") as mock_client_cls:
            mock_client = create_mock_client()
            sources = [
                {"title": f"Source {i}", "url": f"http://example.com/{i}"} for i in range(15)
            ]
            mock_client.research.poll = AsyncMock(
                return_value={
                    "status": "completed",
                    "query": "AI research",
                    "sources": sources,
                    "summary": "",
                }
            )
            mock_client_cls.return_value = mock_client

            result = runner.invoke(cli, ["research", "status", "-n", "nb_123"])

        assert result.exit_code == 0
        assert "Found 15 sources" in result.output
        assert "and 5 more" in result.output

    def test_status_unknown(self, runner, mock_auth, mock_fetch_tokens):
        with patch_client_for_module("research") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.research.poll = AsyncMock(return_value={"status": "unknown_status"})
            mock_client_cls.return_value = mock_client

            result = runner.invoke(cli, ["research", "status", "-n", "nb_123"])

        assert result.exit_code == 0
        assert "Status: unknown_status" in result.output

    def test_status_json_output(self, runner, mock_auth, mock_fetch_tokens):
        with patch_client_for_module("research") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.research.poll = AsyncMock(
                return_value={
                    "status": "completed",
                    "query": "AI research",
                    "sources": [{"title": "Source 1", "url": "http://example.com"}],
                    "summary": "Summary",
                }
            )
            mock_client_cls.return_value = mock_client

            result = runner.invoke(cli, ["research", "status", "-n", "nb_123", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "completed"
        assert len(data["sources"]) == 1


# =============================================================================
# RESEARCH WAIT TESTS
# =============================================================================


class TestResearchWait:
    def test_wait_completes(self, runner, mock_auth, mock_fetch_tokens):
        with patch_client_for_module("research") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.research.poll = AsyncMock(
                return_value={
                    "status": "completed",
                    "task_id": "task_123",
                    "query": "AI research",
                    "sources": [{"title": "Source 1", "url": "http://example.com"}],
                    "report": "# Test Report",
                }
            )
            mock_client_cls.return_value = mock_client

            result = runner.invoke(cli, ["research", "wait", "-n", "nb_123"])

        assert result.exit_code == 0
        assert "Research completed" in result.output
        assert "Found 1 sources" in result.output
        assert "Test Report" in result.output

    def test_wait_no_research(self, runner, mock_auth, mock_fetch_tokens):
        with patch_client_for_module("research") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.research.poll = AsyncMock(return_value={"status": "no_research"})
            mock_client_cls.return_value = mock_client

            result = runner.invoke(cli, ["research", "wait", "-n", "nb_123"])

        assert result.exit_code == 1
        assert "No research running" in result.output

    def test_wait_timeout(self, runner, mock_auth, mock_fetch_tokens):
        with patch_client_for_module("research") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.research.poll = AsyncMock(
                return_value={"status": "in_progress", "query": "AI research"}
            )
            mock_client_cls.return_value = mock_client

            result = runner.invoke(
                cli, ["research", "wait", "-n", "nb_123", "--timeout", "1", "--interval", "1"]
            )

        assert result.exit_code == 1
        assert "Timed out" in result.output

    def test_wait_with_import_all(self, runner, mock_auth, mock_fetch_tokens):
        with (
            patch_client_for_module("research") as mock_client_cls,
            patch.object(
                helpers_module, "import_with_retry", new_callable=AsyncMock
            ) as mock_import,
        ):
            mock_client = create_mock_client()
            mock_client.research.poll = AsyncMock(
                return_value={
                    "status": "completed",
                    "task_id": "task_123",
                    "query": "AI research",
                    "sources": [{"title": "Source 1", "url": "http://example.com"}],
                }
            )
            mock_import.return_value = [{"id": "src_1", "title": "Source 1"}]
            mock_client_cls.return_value = mock_client

            result = runner.invoke(cli, ["research", "wait", "-n", "nb_123", "--import-all"])

        assert result.exit_code == 0
        assert "Imported 1 sources" in result.output
        mock_import.assert_awaited_once_with(
            mock_client,
            "nb_123",
            "task_123",
            [{"title": "Source 1", "url": "http://example.com"}],
            max_elapsed=300,
        )

    def test_wait_with_import_all_cited_only(self, runner, mock_auth, mock_fetch_tokens):
        with (
            patch_client_for_module("research") as mock_client_cls,
            patch.object(
                helpers_module, "import_with_retry", new_callable=AsyncMock
            ) as mock_import,
        ):
            mock_client = create_mock_client()
            mock_client.research.poll = AsyncMock(
                return_value={
                    "status": "completed",
                    "task_id": "task_123",
                    "query": "AI research",
                    "sources": [
                        {"title": "Cited", "url": "https://example.com/cited"},
                        {"title": "Uncited", "url": "https://example.com/uncited"},
                    ],
                    "report": "Report cites https://example.com/cited",
                }
            )
            mock_import.return_value = [{"id": "src_1", "title": "Cited"}]
            mock_client_cls.return_value = mock_client

            result = runner.invoke(
                cli,
                ["research", "wait", "-n", "nb_123", "--import-all", "--cited-only"],
            )

        assert result.exit_code == 0
        assert "Imported 1 sources" in result.output
        mock_import.assert_awaited_once_with(
            mock_client,
            "nb_123",
            "task_123",
            [{"title": "Cited", "url": "https://example.com/cited"}],
            max_elapsed=300,
        )

    def test_wait_cited_only_requires_import_all(self, runner, mock_auth, mock_fetch_tokens):
        result = runner.invoke(cli, ["research", "wait", "-n", "nb_123", "--cited-only"])

        assert result.exit_code == 1
        assert "--cited-only requires --import-all" in result.output

    def test_wait_json_output_completed(self, runner, mock_auth, mock_fetch_tokens):
        with patch_client_for_module("research") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.research.poll = AsyncMock(
                return_value={
                    "status": "completed",
                    "task_id": "task_123",
                    "query": "AI research",
                    "sources": [{"title": "Source 1", "url": "http://example.com"}],
                    "report": "# JSON Report",
                }
            )
            mock_client_cls.return_value = mock_client

            result = runner.invoke(cli, ["research", "wait", "-n", "nb_123", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "completed"
        assert data["sources_found"] == 1
        assert data["report"] == "# JSON Report"

    def test_wait_json_output_with_import(self, runner, mock_auth, mock_fetch_tokens):
        with (
            patch_client_for_module("research") as mock_client_cls,
            patch.object(
                helpers_module, "import_with_retry", new_callable=AsyncMock
            ) as mock_import,
        ):
            mock_client = create_mock_client()
            mock_client.research.poll = AsyncMock(
                return_value={
                    "status": "completed",
                    "task_id": "task_123",
                    "query": "AI research",
                    "sources": [{"title": "Source 1", "url": "http://example.com"}],
                }
            )
            mock_import.return_value = [{"id": "src_1", "title": "Source 1"}]
            mock_client_cls.return_value = mock_client

            result = runner.invoke(
                cli, ["research", "wait", "-n", "nb_123", "--json", "--import-all"]
            )

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "completed"
        assert data["imported"] == 1
        assert len(data["imported_sources"]) == 1
        mock_import.assert_awaited_once_with(
            mock_client,
            "nb_123",
            "task_123",
            [{"title": "Source 1", "url": "http://example.com"}],
            max_elapsed=300,
            json_output=True,
        )

    def test_wait_json_output_with_import_cited_only(self, runner, mock_auth, mock_fetch_tokens):
        with (
            patch_client_for_module("research") as mock_client_cls,
            patch.object(
                helpers_module, "import_with_retry", new_callable=AsyncMock
            ) as mock_import,
        ):
            mock_client = create_mock_client()
            mock_client.research.poll = AsyncMock(
                return_value={
                    "status": "completed",
                    "task_id": "task_123",
                    "query": "AI research",
                    "sources": [
                        {"title": "Cited", "url": "https://example.com/cited"},
                        {"title": "Uncited", "url": "https://example.com/uncited"},
                    ],
                    "report": "Report cites https://example.com/cited",
                }
            )
            mock_import.return_value = [{"id": "src_1", "title": "Cited"}]
            mock_client_cls.return_value = mock_client

            result = runner.invoke(
                cli,
                ["research", "wait", "-n", "nb_123", "--json", "--import-all", "--cited-only"],
            )

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["cited_only"] is True
        assert data["cited_sources_selected"] == 1
        assert data["cited_only_fallback"] is False
        mock_import.assert_awaited_once_with(
            mock_client,
            "nb_123",
            "task_123",
            [{"title": "Cited", "url": "https://example.com/cited"}],
            max_elapsed=300,
            json_output=True,
        )

    def test_wait_json_no_research(self, runner, mock_auth, mock_fetch_tokens):
        with patch_client_for_module("research") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.research.poll = AsyncMock(return_value={"status": "no_research"})
            mock_client_cls.return_value = mock_client

            result = runner.invoke(cli, ["research", "wait", "-n", "nb_123", "--json"])

        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["status"] == "no_research"
        assert "error" in data

    def test_wait_json_timeout(self, runner, mock_auth, mock_fetch_tokens):
        with patch_client_for_module("research") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.research.poll = AsyncMock(
                return_value={"status": "in_progress", "query": "AI research"}
            )
            mock_client_cls.return_value = mock_client

            result = runner.invoke(
                cli,
                ["research", "wait", "-n", "nb_123", "--json", "--timeout", "1", "--interval", "1"],
            )

        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["status"] == "timeout"


# =============================================================================
# COMMAND EXISTENCE TESTS
# =============================================================================


class TestResearchCommandsExist:
    def test_research_group_exists(self, runner):
        result = runner.invoke(cli, ["research", "--help"])
        assert result.exit_code == 0
        assert "Research management commands" in result.output

    def test_research_status_command_exists(self, runner):
        result = runner.invoke(cli, ["research", "status", "--help"])
        assert result.exit_code == 0
        assert "Check research status" in result.output

    def test_research_wait_command_exists(self, runner):
        result = runner.invoke(cli, ["research", "wait", "--help"])
        assert result.exit_code == 0
        assert "Wait for research to complete" in result.output
