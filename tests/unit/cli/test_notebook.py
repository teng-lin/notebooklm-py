"""Tests for notebook CLI commands (now top-level commands)."""

import json
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from notebooklm.exceptions import NotebookLimitError, RPCError
from notebooklm.notebooklm_cli import cli
from notebooklm.rpc import RPCMethod
from notebooklm.types import AskResult, Notebook

from .conftest import create_mock_client, patch_client_for_module, patch_main_cli_client


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

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["list"])

            assert result.exit_code == 0
            assert "Notebooks" in result.output

    def test_notebook_list_with_notebooks(self, runner, mock_auth):
        with patch_main_cli_client() as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.notebooks.list = AsyncMock(
                return_value=[
                    Notebook(
                        id="nb_1",
                        title="First Notebook",
                        created_at=datetime(2024, 1, 1),
                        is_owner=True,
                    ),
                    Notebook(
                        id="nb_2",
                        title="Second Notebook",
                        created_at=datetime(2024, 1, 2),
                        is_owner=False,
                    ),
                ]
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
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
                    Notebook(
                        id="nb_1",
                        title="Test Notebook",
                        created_at=datetime(2024, 1, 1),
                        is_owner=True,
                    ),
                ]
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
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
    def test_notebook_create(self, runner, mock_auth, mock_context_file):
        with patch_main_cli_client() as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.notebooks.create = AsyncMock(
                return_value=Notebook(
                    id="new_nb_id", title="Test Notebook", created_at=datetime(2024, 1, 1)
                )
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["create", "Test Notebook"])

            assert result.exit_code == 0
            assert "Created notebook" in result.output
            assert "Context set to new notebook" in result.output
            context = json.loads(mock_context_file.read_text())
            assert context["notebook_id"] == "new_nb_id"
            assert context["title"] == "Test Notebook"

    def test_notebook_create_json_output(self, runner, mock_auth, mock_context_file):
        with patch_main_cli_client() as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.notebooks.create = AsyncMock(
                return_value=Notebook(
                    id="new_nb_id", title="Test Notebook", created_at=datetime(2024, 1, 1)
                )
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["create", "Test Notebook", "--json"])

            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["notebook"]["id"] == "new_nb_id"
            context = json.loads(mock_context_file.read_text())
            assert context["notebook_id"] == "new_nb_id"

    def test_notebook_create_replaces_existing_context(self, runner, mock_auth, mock_context_file):
        """Reproduces #220: create must overwrite a previously active context."""
        mock_context_file.write_text(
            json.dumps({"notebook_id": "old_nb", "title": "Previously Active"})
        )

        with patch_main_cli_client() as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.notebooks.create = AsyncMock(
                return_value=Notebook(
                    id="brand_new", title="Fresh", created_at=datetime(2024, 1, 1)
                )
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["create", "Fresh"])

            assert result.exit_code == 0
            context = json.loads(mock_context_file.read_text())
            assert context["notebook_id"] == "brand_new"
            assert context["title"] == "Fresh"

    def test_notebook_create_without_created_at(self, runner, mock_auth, mock_context_file):
        """Guard the `if nb.created_at else None` branch so a future refactor can't regress it."""
        with patch_main_cli_client() as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.notebooks.create = AsyncMock(
                return_value=Notebook(id="nb_no_date", title="Undated", created_at=None)
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["create", "Undated"])

            assert result.exit_code == 0
            context = json.loads(mock_context_file.read_text())
            assert context["notebook_id"] == "nb_no_date"
            assert "created_at" not in context

    def test_notebook_create_json_quota_error(self, runner, mock_auth):
        """Create emits structured JSON when notebook quota is detected."""
        with patch_main_cli_client() as mock_client_cls:
            mock_client = create_mock_client()
            original = RPCError(
                "RPC CCqFvf returned null result with status code 3 (Invalid argument).",
                method_id=RPCMethod.CREATE_NOTEBOOK.value,
                rpc_code=3,
            )
            mock_client.notebooks.create = AsyncMock(
                side_effect=NotebookLimitError(499, limit=500, original_error=original)
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["create", "Test Notebook", "--json"])

            assert result.exit_code == 1
            data = json.loads(result.output)
            assert data["code"] == "NOTEBOOK_LIMIT"
            assert data["current_count"] == 499
            assert data["limit"] == 500
            assert "known_limits" not in data
            assert data["method_id"] == RPCMethod.CREATE_NOTEBOOK.value
            assert data["rpc_code"] == 3

    def test_notebook_create_text_quota_error(self, runner, mock_auth):
        """Create emits an actionable text error when notebook quota is detected."""
        with patch_main_cli_client() as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.notebooks.create = AsyncMock(side_effect=NotebookLimitError(499, limit=500))
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["create", "Test Notebook"])

            assert result.exit_code == 1
            assert "notebook limit" in result.output.lower()
            assert "499/500" in result.output


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
                    Notebook(
                        id="nb_to_delete",
                        title="Test Notebook",
                        created_at=datetime(2024, 1, 1),
                        is_owner=True,
                    ),
                ]
            )
            mock_client.notebooks.delete = AsyncMock(return_value=True)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
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
                    Notebook(
                        id="nb_to_delete",
                        title="Test Notebook",
                        created_at=datetime(2024, 1, 1),
                        is_owner=True,
                    ),
                ]
            )
            mock_client.notebooks.delete = AsyncMock(return_value=True)
            mock_client_cls.return_value = mock_client

            with (
                patch("notebooklm.cli.helpers.get_context_path", return_value=context_file),
                patch("notebooklm.cli.notebook.get_current_notebook", return_value="nb_to_delete"),
                patch("notebooklm.cli.notebook.clear_context"),
                patch(
                    "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
                ) as mock_fetch,
            ):
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
                    Notebook(
                        id="nb_123",
                        title="Test Notebook",
                        created_at=datetime(2024, 1, 1),
                        is_owner=True,
                    ),
                ]
            )
            mock_client.notebooks.delete = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
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
            # Mock list for partial ID resolution
            mock_client.notebooks.list = AsyncMock(
                return_value=[
                    Notebook(
                        id="nb_123",
                        title="Test Notebook",
                        created_at=datetime(2024, 1, 1),
                        is_owner=True,
                    ),
                ]
            )
            mock_client.notebooks.rename = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["rename", "New Title", "-n", "nb_123"])

            assert result.exit_code == 0
            assert "Renamed notebook" in result.output
            mock_client.notebooks.rename.assert_called_once_with("nb_123", "New Title")


# =============================================================================
# NOTEBOOK SHARE TESTS (moved to share command group)
# =============================================================================

# Note: Share functionality has moved to 'share' command group.
# Tests are now in tests/unit/cli/test_share.py


# =============================================================================
# NOTEBOOK SUMMARY TESTS
# =============================================================================


class TestNotebookSummary:
    def test_notebook_summary(self, runner, mock_auth):
        with patch_main_cli_client() as mock_client_cls:
            mock_client = create_mock_client()
            # Mock list for partial ID resolution
            mock_client.notebooks.list = AsyncMock(
                return_value=[
                    Notebook(
                        id="nb_123",
                        title="Test Notebook",
                        created_at=datetime(2024, 1, 1),
                        is_owner=True,
                    ),
                ]
            )
            mock_desc = MagicMock()
            mock_desc.summary = "This notebook contains research about AI."
            mock_desc.suggested_topics = []
            mock_client.notebooks.get_description = AsyncMock(return_value=mock_desc)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["summary", "-n", "nb_123"])

            assert result.exit_code == 0
            assert "Summary" in result.output
            assert "research about AI" in result.output

    def test_notebook_summary_with_topics(self, runner, mock_auth):
        with patch_main_cli_client() as mock_client_cls:
            mock_client = create_mock_client()
            # Mock list for partial ID resolution
            mock_client.notebooks.list = AsyncMock(
                return_value=[
                    Notebook(
                        id="nb_123",
                        title="Test Notebook",
                        created_at=datetime(2024, 1, 1),
                        is_owner=True,
                    ),
                ]
            )
            mock_desc = MagicMock()
            mock_desc.summary = "This is a summary."
            mock_topic = MagicMock()
            mock_topic.question = "What is machine learning?"
            mock_desc.suggested_topics = [mock_topic]
            mock_client.notebooks.get_description = AsyncMock(return_value=mock_desc)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["summary", "-n", "nb_123", "--topics"])

            assert result.exit_code == 0
            assert "Suggested Topics" in result.output
            assert "machine learning" in result.output

    def test_notebook_summary_not_available(self, runner, mock_auth):
        with patch_main_cli_client() as mock_client_cls:
            mock_client = create_mock_client()
            # Mock list for partial ID resolution
            mock_client.notebooks.list = AsyncMock(
                return_value=[
                    Notebook(
                        id="nb_123",
                        title="Test Notebook",
                        created_at=datetime(2024, 1, 1),
                        is_owner=True,
                    ),
                ]
            )
            mock_client.notebooks.get_description = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["summary", "-n", "nb_123"])

            assert result.exit_code == 0
            assert "No summary available" in result.output


# =============================================================================
# NOTEBOOK HISTORY TESTS
# =============================================================================


class TestNotebookHistory:
    def test_notebook_history(self, runner, mock_auth):
        with patch_main_cli_client() as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.chat.get_history = AsyncMock(return_value=[("Q1?", "A1"), ("Q2?", "A2")])
            mock_client.chat.get_conversation_id = AsyncMock(return_value="conv_001")
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["history", "-n", "nb_123"])

            assert result.exit_code == 0
            assert "Conversation History" in result.output

    def test_notebook_history_empty(self, runner, mock_auth):
        with patch_main_cli_client() as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.chat.get_conversation_id = AsyncMock(return_value=None)
            mock_client.chat.get_history = AsyncMock(return_value=[])
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["history", "-n", "nb_123"])

            assert result.exit_code == 0
            assert "No conversation history" in result.output

    def test_notebook_history_clear_cache(self, runner, mock_auth):
        with patch_main_cli_client() as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.chat.clear_cache = MagicMock(return_value=True)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
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
                return_value=AskResult(
                    answer="This is the answer to your question.",
                    conversation_id="conv_123",
                    is_follow_up=False,
                    turn_number=1,
                )
            )
            mock_client.chat.get_conversation_id = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with (
                patch(
                    "notebooklm.cli.helpers.get_context_path",
                    return_value=Path("/nonexistent/context.json"),
                ),
                patch(
                    "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
                ) as mock_fetch,
            ):
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["ask", "-n", "nb_123", "What is this?"])

            assert result.exit_code == 0
            assert "This is the answer" in result.output

    def test_notebook_ask_continue_conversation(self, runner, mock_auth):
        with patch_main_cli_client() as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.chat.ask = AsyncMock(
                return_value=AskResult(
                    answer="Follow-up answer",
                    conversation_id="conv_123",
                    is_follow_up=True,
                    turn_number=2,
                )
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
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

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["configure", "-n", "nb_123", "--mode", "learning-guide"]
                )

            assert result.exit_code == 0
            assert "Chat mode set to: learning-guide" in result.output

    def test_notebook_configure_persona(self, runner, mock_auth):
        with patch_main_cli_client() as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.chat.configure = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["configure", "-n", "nb_123", "--persona", "Act as a tutor"]
                )

            assert result.exit_code == 0
            assert "Chat configured" in result.output
            assert "persona" in result.output

    def test_notebook_configure_response_length(self, runner, mock_auth):
        with patch_main_cli_client() as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.chat.configure = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["configure", "-n", "nb_123", "--response-length", "longer"]
                )

            assert result.exit_code == 0
            assert "response length: longer" in result.output


# =============================================================================
# SOURCE ADD-RESEARCH TESTS (moved from insights to source)
# =============================================================================


class TestSourceAddResearch:
    def test_source_add_research_success(self, runner, mock_auth):
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.research.start = AsyncMock(return_value={"task_id": "task_123"})
            mock_client.research.poll = AsyncMock(
                return_value={"status": "completed", "sources": [{"title": "Source 1"}]}
            )
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["source", "add-research", "AI research", "-n", "nb_123"]
                )

            assert result.exit_code == 0
            assert "Found 1 sources" in result.output

    def test_source_add_research_failed_to_start(self, runner, mock_auth):
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.research.start = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["source", "add-research", "AI research", "-n", "nb_123"]
                )

            assert result.exit_code == 1
            assert "Research failed to start" in result.output

    def test_source_add_research_with_import(self, runner, mock_auth):
        with patch_client_for_module("source") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.research.start = AsyncMock(return_value={"task_id": "task_123"})
            mock_client.research.poll = AsyncMock(
                return_value={"status": "completed", "sources": [{"id": "src_1"}]}
            )
            mock_client.research.import_sources = AsyncMock(return_value=[{"id": "src_1"}])
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["source", "add-research", "AI research", "-n", "nb_123", "--import-all"]
                )

            assert result.exit_code == 0
            assert "Imported 1 sources" in result.output


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
        commands_section = (
            result.output.split("Commands:")[1] if "Commands:" in result.output else ""
        )
        assert "  notebook " not in commands_section.lower()


# =============================================================================
# METADATA COMMAND TESTS
# =============================================================================


class TestNotebookMetadata:
    """Tests for the metadata command."""

    def test_metadata_human_readable(self, runner, mock_auth):
        """Test human-readable output (default)."""
        from datetime import datetime

        from notebooklm.types import Notebook, NotebookMetadata, SourceSummary, SourceType

        with patch_main_cli_client() as mock_client_cls:
            mock_client = create_mock_client()
            notebook = Notebook(
                id="nb_1",
                title="Test Notebook",
                created_at=datetime(2024, 1, 1),
            )
            # Override notebooks.list to return only our test notebook (avoid partial ID conflicts)
            mock_client.notebooks.list = AsyncMock(return_value=[notebook])

            metadata = NotebookMetadata(
                notebook=notebook,
                sources=[
                    SourceSummary(kind=SourceType.PDF, title="test.pdf"),
                ],
            )

            # Use side_effect to avoid potential pickling issues with enums
            async def return_metadata(nb_id):
                return metadata

            mock_client.notebooks.get_metadata = AsyncMock(side_effect=return_metadata)
            mock_client.notebooks.get = AsyncMock(return_value=notebook)
            mock_client_cls.return_value = mock_client

            with (
                patch("notebooklm.cli.helpers.get_current_notebook", return_value="nb_1"),
                patch(
                    "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
                ) as mock_fetch,
            ):
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["metadata"])

            assert result.exit_code == 0
            assert "Test Notebook" in result.output
            assert "[pdf]" in result.output
            assert "nb_1" in result.output

    def test_metadata_json_output(self, runner, mock_auth):
        """Test JSON output with --json flag."""

        from notebooklm.types import Notebook, NotebookMetadata, SourceSummary, SourceType

        with patch_main_cli_client() as mock_client_cls:
            mock_client = create_mock_client()
            notebook = Notebook(id="nb_1", title="Test Notebook")
            # Override notebooks.list to return only our test notebook
            mock_client.notebooks.list = AsyncMock(return_value=[notebook])

            metadata = NotebookMetadata(
                notebook=notebook,
                sources=[SourceSummary(kind=SourceType.PDF, title="test.pdf")],
            )

            # Use side_effect to avoid potential pickling issues with enums
            async def return_metadata(nb_id):
                return metadata

            mock_client.notebooks.get_metadata = AsyncMock(side_effect=return_metadata)
            mock_client.notebooks.get = AsyncMock(return_value=notebook)
            mock_client_cls.return_value = mock_client

            with (
                patch("notebooklm.cli.helpers.get_current_notebook", return_value="nb_1"),
                patch(
                    "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
                ) as mock_fetch,
            ):
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["metadata", "--json"])

            assert result.exit_code == 0
            # JSON output should be valid JSON (without Rich markup in JSON mode)
            data = json.loads(result.output)
            assert data["id"] == "nb_1"
            assert data["title"] == "Test Notebook"
            assert data["sources"][0]["type"] == "pdf"

    def test_metadata_empty_sources(self, runner, mock_auth):
        """Test metadata with no sources."""
        from notebooklm.types import Notebook, NotebookMetadata

        with patch_main_cli_client() as mock_client_cls:
            mock_client = create_mock_client()
            notebook = Notebook(id="nb_empty", title="Empty Notebook")
            # Override notebooks.list to return only our test notebook
            mock_client.notebooks.list = AsyncMock(return_value=[notebook])

            metadata = NotebookMetadata(notebook=notebook, sources=[])
            mock_client.notebooks.get_metadata = AsyncMock(return_value=metadata)
            mock_client.notebooks.get = AsyncMock(return_value=notebook)
            mock_client_cls.return_value = mock_client

            with (
                patch("notebooklm.cli.helpers.get_current_notebook", return_value="nb_empty"),
                patch(
                    "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
                ) as mock_fetch,
            ):
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["metadata"])

            assert result.exit_code == 0
            assert "No sources" in result.output

    def test_metadata_with_url_source(self, runner, mock_auth):
        """Test metadata with URL source displays URL."""
        from notebooklm.types import Notebook, NotebookMetadata, SourceSummary, SourceType

        with patch_main_cli_client() as mock_client_cls:
            mock_client = create_mock_client()
            notebook = Notebook(id="nb_url", title="URL Notebook")
            # Override notebooks.list to return only our test notebook
            mock_client.notebooks.list = AsyncMock(return_value=[notebook])

            metadata = NotebookMetadata(
                notebook=notebook,
                sources=[
                    SourceSummary(
                        kind=SourceType.WEB_PAGE,
                        title="Example Site",
                        url="https://example.com/article",
                    )
                ],
            )
            mock_client.notebooks.get_metadata = AsyncMock(return_value=metadata)
            mock_client.notebooks.get = AsyncMock(return_value=notebook)
            mock_client_cls.return_value = mock_client

            with (
                patch("notebooklm.cli.helpers.get_current_notebook", return_value="nb_url"),
                patch(
                    "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
                ) as mock_fetch,
            ):
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["metadata"])

            assert result.exit_code == 0
            output_lines = result.output.splitlines()
            assert "     https://example.com/article" in output_lines
