"""Tests for notebook CLI commands (now top-level commands)."""

import json
import pytest
from datetime import datetime
from unittest.mock import AsyncMock, patch, MagicMock

from click.testing import CliRunner

from notebooklm.notebooklm_cli import cli
from notebooklm.types import Notebook, NotebookDescription, SuggestedTopic

from .conftest import create_mock_client, patch_main_cli_client


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
# NOTEBOOK LIST TESTS
# =============================================================================


class TestNotebookList:
    def test_notebook_list_empty(self, runner, mock_auth):
        with patch_main_cli_client() as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.notebooks.list = AsyncMock(return_value=[])
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["list"])

            assert result.exit_code == 0
            assert "Notebooks" in result.output

    def test_notebook_list_with_notebooks(self, runner, mock_auth):
        with patch_main_cli_client() as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.notebooks.list = AsyncMock(
                return_value=[
                    Notebook(id="nb_1", title="First Notebook", created_at=datetime(2024, 1, 1), is_owner=True),
                    Notebook(id="nb_2", title="Second Notebook", created_at=datetime(2024, 1, 2), is_owner=False),
                ]
            )
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["list"])

            assert result.exit_code == 0
            assert "First Notebook" in result.output
            assert "Second Notebook" in result.output

    def test_notebook_list_json_output(self, runner, mock_auth):
        with patch_main_cli_client() as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.notebooks.list = AsyncMock(
                return_value=[
                    Notebook(id="nb_1", title="Test Notebook", created_at=datetime(2024, 1, 1), is_owner=True),
                ]
            )
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["list", "--json"])

            assert result.exit_code == 0
            data = json.loads(result.output)
            assert "notebooks" in data
            assert data["count"] == 1
            assert data["notebooks"][0]["id"] == "nb_1"


# =============================================================================
# NOTEBOOK CREATE TESTS
# =============================================================================


class TestNotebookCreate:
    def test_notebook_create(self, runner, mock_auth):
        with patch_main_cli_client() as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.notebooks.create = AsyncMock(
                return_value=Notebook(id="new_nb_id", title="Test Notebook", created_at=datetime(2024, 1, 1))
            )
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["create", "Test Notebook"])

            assert result.exit_code == 0
            assert "Created notebook" in result.output

    def test_notebook_create_json_output(self, runner, mock_auth):
        with patch_main_cli_client() as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.notebooks.create = AsyncMock(
                return_value=Notebook(id="new_nb_id", title="Test Notebook", created_at=datetime(2024, 1, 1))
            )
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["create", "Test Notebook", "--json"])

            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["notebook"]["id"] == "new_nb_id"


# =============================================================================
# NOTEBOOK DELETE TESTS
# =============================================================================


class TestNotebookDelete:
    def test_notebook_delete(self, runner, mock_auth):
        with patch_main_cli_client() as mock_client_cls:
            mock_client = create_mock_client()
            # Mock list for partial ID resolution (returns the notebook to be deleted)
            mock_client.notebooks.list = AsyncMock(
                return_value=[
                    Notebook(id="nb_to_delete", title="Test Notebook", created_at=datetime(2024, 1, 1), is_owner=True),
                ]
            )
            mock_client.notebooks.delete = AsyncMock(return_value=True)
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["delete", "-n", "nb_to_delete", "-y"])

            assert result.exit_code == 0
            assert "Deleted notebook" in result.output
            mock_client.notebooks.delete.assert_called_once_with("nb_to_delete")

    def test_notebook_delete_clears_context_if_current(self, runner, mock_auth, tmp_path):
        context_file = tmp_path / "context.json"
        context_file.write_text('{"notebook_id": "nb_to_delete"}')

        with patch_main_cli_client() as mock_client_cls:
            mock_client = create_mock_client()
            # Mock list for partial ID resolution
            mock_client.notebooks.list = AsyncMock(
                return_value=[
                    Notebook(id="nb_to_delete", title="Test Notebook", created_at=datetime(2024, 1, 1), is_owner=True),
                ]
            )
            mock_client.notebooks.delete = AsyncMock(return_value=True)
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.cli.helpers.CONTEXT_FILE", context_file):
                with patch("notebooklm.cli.notebook.get_current_notebook", return_value="nb_to_delete"):
                    with patch("notebooklm.cli.notebook.clear_context") as mock_clear:
                        with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                            mock_fetch.return_value = ("csrf", "session")
                            result = runner.invoke(cli, ["delete", "-n", "nb_to_delete", "-y"])

            assert result.exit_code == 0
            assert "Cleared current notebook context" in result.output

    def test_notebook_delete_failure(self, runner, mock_auth):
        with patch_main_cli_client() as mock_client_cls:
            mock_client = create_mock_client()
            # Mock list for partial ID resolution
            mock_client.notebooks.list = AsyncMock(
                return_value=[
                    Notebook(id="nb_123", title="Test Notebook", created_at=datetime(2024, 1, 1), is_owner=True),
                ]
            )
            mock_client.notebooks.delete = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["delete", "-n", "nb_123", "-y"])

            assert result.exit_code == 0
            assert "Delete may have failed" in result.output


# =============================================================================
# NOTEBOOK RENAME TESTS
# =============================================================================


class TestNotebookRename:
    def test_notebook_rename(self, runner, mock_auth):
        with patch_main_cli_client() as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.notebooks.rename = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["rename", "New Title", "-n", "nb_123"])

            assert result.exit_code == 0
            assert "Renamed notebook" in result.output
            mock_client.notebooks.rename.assert_called_once_with("nb_123", "New Title")


# =============================================================================
# NOTEBOOK SHARE TESTS
# =============================================================================


class TestNotebookShare:
    def test_notebook_share(self, runner, mock_auth):
        with patch_main_cli_client() as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.notebooks.share = AsyncMock(
                return_value={"share_link": "https://notebooklm.google.com/notebook/abc123"}
            )
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["share", "-n", "nb_123"])

            assert result.exit_code == 0
            assert "Sharing configured" in result.output

    def test_notebook_share_no_result(self, runner, mock_auth):
        with patch_main_cli_client() as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.notebooks.share = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["share", "-n", "nb_123"])

            assert result.exit_code == 0
            assert "No sharing info returned" in result.output


# =============================================================================
# NOTEBOOK SUMMARY TESTS
# =============================================================================


class TestNotebookSummary:
    def test_notebook_summary(self, runner, mock_auth):
        with patch_main_cli_client() as mock_client_cls:
            mock_client = create_mock_client()
            mock_desc = MagicMock()
            mock_desc.summary = "This notebook contains research about AI."
            mock_desc.suggested_topics = []
            mock_client.notebooks.get_description = AsyncMock(return_value=mock_desc)
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["summary", "-n", "nb_123"])

            assert result.exit_code == 0
            assert "Summary" in result.output
            assert "research about AI" in result.output

    def test_notebook_summary_with_topics(self, runner, mock_auth):
        with patch_main_cli_client() as mock_client_cls:
            mock_client = create_mock_client()
            mock_desc = MagicMock()
            mock_desc.summary = "This is a summary."
            mock_topic = MagicMock()
            mock_topic.question = "What is machine learning?"
            mock_desc.suggested_topics = [mock_topic]
            mock_client.notebooks.get_description = AsyncMock(return_value=mock_desc)
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["summary", "-n", "nb_123", "--topics"])

            assert result.exit_code == 0
            assert "Suggested Topics" in result.output
            assert "machine learning" in result.output

    def test_notebook_summary_not_available(self, runner, mock_auth):
        with patch_main_cli_client() as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.notebooks.get_description = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["summary", "-n", "nb_123"])

            assert result.exit_code == 0
            assert "No summary available" in result.output


# =============================================================================
# NOTEBOOK ANALYTICS TESTS
# =============================================================================


class TestNotebookAnalytics:
    def test_notebook_analytics(self, runner, mock_auth):
        with patch_main_cli_client() as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.notebooks.get_analytics = AsyncMock(
                return_value={"views": 100, "queries": 50}
            )
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["analytics", "-n", "nb_123"])

            assert result.exit_code == 0
            assert "Analytics" in result.output

    def test_notebook_analytics_not_available(self, runner, mock_auth):
        with patch_main_cli_client() as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.notebooks.get_analytics = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["analytics", "-n", "nb_123"])

            assert result.exit_code == 0
            assert "No analytics available" in result.output


# =============================================================================
# NOTEBOOK HISTORY TESTS
# =============================================================================


class TestNotebookHistory:
    def test_notebook_history(self, runner, mock_auth):
        with patch_main_cli_client() as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.chat.get_history = AsyncMock(
                return_value=[[["conv_1"], ["conv_2"]]]
            )
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["history", "-n", "nb_123"])

            assert result.exit_code == 0
            assert "Conversation History" in result.output

    def test_notebook_history_empty(self, runner, mock_auth):
        with patch_main_cli_client() as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.chat.get_history = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["history", "-n", "nb_123"])

            assert result.exit_code == 0
            assert "No conversation history" in result.output

    def test_notebook_history_clear_cache(self, runner, mock_auth):
        with patch_main_cli_client() as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.chat.clear_cache = MagicMock(return_value=True)
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["history", "--clear"])

            assert result.exit_code == 0
            assert "cache cleared" in result.output


# =============================================================================
# NOTEBOOK ASK TESTS
# =============================================================================


class TestNotebookAsk:
    def test_notebook_ask(self, runner, mock_auth):
        with patch_main_cli_client() as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.chat.ask = AsyncMock(
                return_value={
                    "answer": "This is the answer to your question.",
                    "conversation_id": "conv_123",
                    "is_follow_up": False,
                }
            )
            mock_client.chat.get_history = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.cli.helpers.CONTEXT_FILE") as mock_ctx_file:
                mock_ctx_file.exists.return_value = False
                with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                    mock_fetch.return_value = ("csrf", "session")
                    result = runner.invoke(cli, ["ask", "-n", "nb_123", "What is this?"])

            assert result.exit_code == 0
            assert "This is the answer" in result.output

    def test_notebook_ask_new_conversation(self, runner, mock_auth):
        with patch_main_cli_client() as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.chat.ask = AsyncMock(
                return_value={
                    "answer": "Fresh answer",
                    "conversation_id": "new_conv",
                    "is_follow_up": False,
                }
            )
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["ask", "-n", "nb_123", "--new", "Fresh question"])

            assert result.exit_code == 0
            assert "Starting new conversation" in result.output or "New conversation" in result.output

    def test_notebook_ask_continue_conversation(self, runner, mock_auth):
        with patch_main_cli_client() as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.chat.ask = AsyncMock(
                return_value={
                    "answer": "Follow-up answer",
                    "conversation_id": "conv_123",
                    "is_follow_up": True,
                    "turn_number": 2,
                }
            )
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["ask", "-n", "nb_123", "-c", "conv_123", "Follow-up"])

            assert result.exit_code == 0
            assert "Follow-up answer" in result.output


# =============================================================================
# NOTEBOOK CONFIGURE TESTS
# =============================================================================


class TestNotebookConfigure:
    def test_notebook_configure_mode(self, runner, mock_auth):
        with patch_main_cli_client() as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.chat.set_mode = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["configure", "-n", "nb_123", "--mode", "learning-guide"])

            assert result.exit_code == 0
            assert "Chat mode set to: learning-guide" in result.output

    def test_notebook_configure_persona(self, runner, mock_auth):
        with patch_main_cli_client() as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.chat.configure = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["configure", "-n", "nb_123", "--persona", "Act as a tutor"])

            assert result.exit_code == 0
            assert "Chat configured" in result.output
            assert "persona" in result.output

    def test_notebook_configure_response_length(self, runner, mock_auth):
        with patch_main_cli_client() as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.chat.configure = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["configure", "-n", "nb_123", "--response-length", "longer"])

            assert result.exit_code == 0
            assert "response length: longer" in result.output


# =============================================================================
# NOTEBOOK RESEARCH TESTS
# =============================================================================


class TestNotebookResearch:
    def test_notebook_research_success(self, runner, mock_auth):
        with patch_main_cli_client() as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.research.start = AsyncMock(return_value={"task_id": "task_123"})
            mock_client.research.poll = AsyncMock(
                return_value={"status": "completed", "sources": [{"title": "Source 1"}]}
            )
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["research", "AI research", "-n", "nb_123"])

            assert result.exit_code == 0
            assert "Found 1 sources" in result.output

    def test_notebook_research_failed_to_start(self, runner, mock_auth):
        with patch_main_cli_client() as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.research.start = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["research", "AI research", "-n", "nb_123"])

            assert result.exit_code == 1
            assert "Research failed to start" in result.output

    def test_notebook_research_with_import(self, runner, mock_auth):
        with patch_main_cli_client() as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.research.start = AsyncMock(return_value={"task_id": "task_123"})
            mock_client.research.poll = AsyncMock(
                return_value={"status": "completed", "sources": [{"id": "src_1"}]}
            )
            mock_client.research.import_sources = AsyncMock(return_value=[{"id": "src_1"}])
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["research", "AI research", "-n", "nb_123", "--import-all"])

            assert result.exit_code == 0
            assert "Imported 1 sources" in result.output


# =============================================================================
# NOTEBOOK FEATURED TESTS
# =============================================================================


class TestNotebookFeatured:
    def test_notebook_featured(self, runner, mock_auth):
        with patch_main_cli_client() as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.notebooks.list_featured = AsyncMock(
                return_value=[
                    ["proj_1", "Featured Project 1"],
                    ["proj_2", "Featured Project 2"],
                ]
            )
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["featured"])

            assert result.exit_code == 0
            assert "Featured Notebooks" in result.output

    def test_notebook_featured_empty(self, runner, mock_auth):
        with patch_main_cli_client() as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.notebooks.list_featured = AsyncMock(return_value=[])
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["featured"])

            assert result.exit_code == 0
            assert "No featured notebooks found" in result.output


# =============================================================================
# COMMAND EXISTENCE TESTS
# =============================================================================


class TestNotebookCommandsExist:
    def test_list_command_exists(self, runner):
        result = runner.invoke(cli, ["list", "--help"])
        assert result.exit_code == 0
        assert "List all notebooks" in result.output

    def test_create_command_exists(self, runner):
        result = runner.invoke(cli, ["create", "--help"])
        assert result.exit_code == 0
        assert "TITLE" in result.output

    def test_delete_command_exists(self, runner):
        result = runner.invoke(cli, ["delete", "--help"])
        assert result.exit_code == 0
        assert "Delete a notebook" in result.output

    def test_rename_command_exists(self, runner):
        result = runner.invoke(cli, ["rename", "--help"])
        assert result.exit_code == 0
        assert "Rename a notebook" in result.output

    def test_ask_command_exists(self, runner):
        result = runner.invoke(cli, ["ask", "--help"])
        assert result.exit_code == 0
        assert "QUESTION" in result.output

    def test_top_level_help_shows_notebook_commands(self, runner):
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        # Verify notebook commands are at top level
        assert "list" in result.output
        assert "create" in result.output
        assert "delete" in result.output
        assert "ask" in result.output
        # Verify there's no "notebook" command in the Commands section
        # (it should only appear as part of "NotebookLM" in the description)
        commands_section = result.output.split("Commands:")[1] if "Commands:" in result.output else ""
        assert "  notebook " not in commands_section.lower()
