"""Tests for research CLI commands."""

import importlib
import json
from unittest.mock import AsyncMock, patch

from notebooklm.notebooklm_cli import cli

from .conftest import create_mock_client, inject_client, research_task

research_module = importlib.import_module("notebooklm.cli.research_cmd")
research_import_module = importlib.import_module("notebooklm.cli.research_import")

# =============================================================================
# RESEARCH STATUS TESTS
# =============================================================================


class TestResearchStatus:
    def test_status_no_research(self, runner, mock_auth, mock_fetch_tokens):
        mock_client = create_mock_client()
        mock_client.research.poll = AsyncMock(return_value=research_task({"status": "no_research"}))

        result = runner.invoke(
            cli, ["research", "status", "-n", "nb_123"], obj=inject_client(mock_client)
        )

        assert result.exit_code == 0
        assert "No research running" in result.output

    def test_status_in_progress(self, runner, mock_auth, mock_fetch_tokens):
        mock_client = create_mock_client()
        mock_client.research.poll = AsyncMock(
            return_value=research_task({"status": "in_progress", "query": "AI research"})
        )

        result = runner.invoke(
            cli, ["research", "status", "-n", "nb_123"], obj=inject_client(mock_client)
        )

        assert result.exit_code == 0
        assert "Research in progress" in result.output
        assert "AI research" in result.output

    def test_status_completed(self, runner, mock_auth, mock_fetch_tokens):
        mock_client = create_mock_client()
        mock_client.research.poll = AsyncMock(
            return_value=research_task(
                {
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
        )

        result = runner.invoke(
            cli, ["research", "status", "-n", "nb_123"], obj=inject_client(mock_client)
        )

        assert result.exit_code == 0
        assert "Research completed" in result.output
        assert "Found 2 sources" in result.output
        assert "Source 1" in result.output
        assert "Research Report" in result.output

    def test_status_completed_with_many_sources(self, runner, mock_auth, mock_fetch_tokens):
        """Test that more than 10 sources shows truncation message."""
        mock_client = create_mock_client()
        sources = [{"title": f"Source {i}", "url": f"http://example.com/{i}"} for i in range(15)]
        mock_client.research.poll = AsyncMock(
            return_value=research_task(
                {
                    "status": "completed",
                    "query": "AI research",
                    "sources": sources,
                    "summary": "",
                }
            )
        )

        result = runner.invoke(
            cli, ["research", "status", "-n", "nb_123"], obj=inject_client(mock_client)
        )

        assert result.exit_code == 0
        assert "Found 15 sources" in result.output
        assert "and 5 more" in result.output

    def test_status_unknown(self, runner, mock_auth, mock_fetch_tokens):
        # ``research status`` renders any status it does not special-case
        # (no_research / in_progress / completed) via the generic
        # ``Status: <value>`` line. ``failed`` is a valid terminal status that
        # hits that fallback branch. (The typed return guarantees the status is
        # one of the ResearchStatus values, so a truly "unknown" string can no
        # longer reach the CLI.)
        mock_client = create_mock_client()
        mock_client.research.poll = AsyncMock(return_value=research_task({"status": "failed"}))

        result = runner.invoke(
            cli, ["research", "status", "-n", "nb_123"], obj=inject_client(mock_client)
        )

        assert result.exit_code == 0
        assert "Status: failed" in result.output

    def test_status_json_output(self, runner, mock_auth, mock_fetch_tokens):
        mock_client = create_mock_client()
        mock_client.research.poll = AsyncMock(
            return_value=research_task(
                {
                    "status": "completed",
                    "query": "AI research",
                    "sources": [{"title": "Source 1", "url": "http://example.com"}],
                    "summary": "Summary",
                }
            )
        )

        result = runner.invoke(
            cli, ["research", "status", "-n", "nb_123", "--json"], obj=inject_client(mock_client)
        )

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "completed"
        assert len(data["sources"]) == 1


# =============================================================================
# RESEARCH WAIT TESTS
# =============================================================================


class TestResearchWait:
    def test_wait_completes(self, runner, mock_auth, mock_fetch_tokens):
        mock_client = create_mock_client()
        mock_client.research.poll = AsyncMock(
            return_value=research_task(
                {
                    "status": "completed",
                    "task_id": "task_123",
                    "query": "AI research",
                    "sources": [{"title": "Source 1", "url": "http://example.com"}],
                    "report": "# Test Report",
                }
            )
        )

        result = runner.invoke(
            cli, ["research", "wait", "-n", "nb_123"], obj=inject_client(mock_client)
        )

        assert result.exit_code == 0
        assert "Research completed" in result.output
        assert "Found 1 sources" in result.output
        assert "Test Report" in result.output

    def test_wait_no_research(self, runner, mock_auth, mock_fetch_tokens):
        mock_client = create_mock_client()
        mock_client.research.poll = AsyncMock(return_value=research_task({"status": "no_research"}))

        result = runner.invoke(
            cli, ["research", "wait", "-n", "nb_123"], obj=inject_client(mock_client)
        )

        assert result.exit_code == 1
        assert "No research running" in result.output

    def test_wait_failed(self, runner, mock_auth, mock_fetch_tokens):
        mock_client = create_mock_client()
        mock_client.research.poll = AsyncMock(
            return_value=research_task(
                {
                    "status": "failed",
                    "task_id": "task_123",
                    "query": "AI research",
                    "sources": [{"title": "Source 1", "url": "http://example.com"}],
                    "report": "# Partial",
                }
            )
        )

        result = runner.invoke(
            cli, ["research", "wait", "-n", "nb_123"], obj=inject_client(mock_client)
        )

        assert result.exit_code == 1
        assert "Research failed" in result.output
        assert "AI research" in result.output

    def test_wait_timeout(self, runner, mock_auth, mock_fetch_tokens):
        mock_client = create_mock_client()
        mock_client.research.poll = AsyncMock(
            return_value=research_task({"status": "in_progress", "query": "AI research"})
        )

        result = runner.invoke(
            cli,
            ["research", "wait", "-n", "nb_123", "--timeout", "1", "--interval", "1"],
            obj=inject_client(mock_client),
        )

        assert result.exit_code == 1
        assert "Timed out" in result.output

    def test_wait_with_import_all(self, runner, mock_auth, mock_fetch_tokens):
        with patch.object(
            research_import_module, "import_with_retry", new_callable=AsyncMock
        ) as mock_import:
            mock_client = create_mock_client()
            mock_client.research.poll = AsyncMock(
                return_value=research_task(
                    {
                        "status": "completed",
                        "task_id": "task_123",
                        "query": "AI research",
                        "sources": [{"title": "Source 1", "url": "http://example.com"}],
                    }
                )
            )
            mock_import.return_value = [{"id": "src_1", "title": "Source 1"}]

            result = runner.invoke(
                cli,
                ["research", "wait", "-n", "nb_123", "--import-all"],
                obj=inject_client(mock_client),
            )

        assert result.exit_code == 0
        assert "Imported 1 sources" in result.output
        mock_import.assert_awaited_once_with(
            mock_client,
            "nb_123",
            "task_123",
            [{"url": "http://example.com", "title": "Source 1", "result_type": 1}],
            max_elapsed=1800,
        )

    def test_wait_with_import_all_cited_only(self, runner, mock_auth, mock_fetch_tokens):
        with patch.object(
            research_import_module, "import_with_retry", new_callable=AsyncMock
        ) as mock_import:
            mock_client = create_mock_client()
            mock_client.research.poll = AsyncMock(
                return_value=research_task(
                    {
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
            )
            mock_import.return_value = [{"id": "src_1", "title": "Cited"}]

            result = runner.invoke(
                cli,
                ["research", "wait", "-n", "nb_123", "--import-all", "--cited-only"],
                obj=inject_client(mock_client),
            )

        assert result.exit_code == 0
        assert "Imported 1 sources" in result.output
        mock_import.assert_awaited_once_with(
            mock_client,
            "nb_123",
            "task_123",
            [{"url": "https://example.com/cited", "title": "Cited", "result_type": 1}],
            max_elapsed=1800,
        )

    def test_wait_cited_only_requires_import_all(self, runner, mock_auth, mock_fetch_tokens):
        result = runner.invoke(cli, ["research", "wait", "-n", "nb_123", "--cited-only"])

        # ``click.UsageError`` exits 2 — Click's standard convention.
        assert result.exit_code == 2
        assert "--cited-only requires --import-all" in result.output

    def test_wait_json_output_completed(self, runner, mock_auth, mock_fetch_tokens):
        mock_client = create_mock_client()
        mock_client.research.poll = AsyncMock(
            return_value=research_task(
                {
                    "status": "completed",
                    "task_id": "task_123",
                    "query": "AI research",
                    "sources": [{"title": "Source 1", "url": "http://example.com"}],
                    "report": "# JSON Report",
                }
            )
        )

        result = runner.invoke(
            cli, ["research", "wait", "-n", "nb_123", "--json"], obj=inject_client(mock_client)
        )

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "completed"
        assert data["sources_found"] == 1
        assert data["report"] == "# JSON Report"

    def test_wait_json_output_with_import(self, runner, mock_auth, mock_fetch_tokens):
        with patch.object(
            research_import_module, "import_with_retry", new_callable=AsyncMock
        ) as mock_import:
            mock_client = create_mock_client()
            mock_client.research.poll = AsyncMock(
                return_value=research_task(
                    {
                        "status": "completed",
                        "task_id": "task_123",
                        "query": "AI research",
                        "sources": [{"title": "Source 1", "url": "http://example.com"}],
                    }
                )
            )
            mock_import.return_value = [{"id": "src_1", "title": "Source 1"}]

            result = runner.invoke(
                cli,
                ["research", "wait", "-n", "nb_123", "--json", "--import-all"],
                obj=inject_client(mock_client),
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
            [{"url": "http://example.com", "title": "Source 1", "result_type": 1}],
            max_elapsed=1800,
            json_output=True,
        )

    def test_wait_json_output_with_import_cited_only(self, runner, mock_auth, mock_fetch_tokens):
        with patch.object(
            research_import_module, "import_with_retry", new_callable=AsyncMock
        ) as mock_import:
            mock_client = create_mock_client()
            mock_client.research.poll = AsyncMock(
                return_value=research_task(
                    {
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
            )
            mock_import.return_value = [{"id": "src_1", "title": "Cited"}]

            result = runner.invoke(
                cli,
                ["research", "wait", "-n", "nb_123", "--json", "--import-all", "--cited-only"],
                obj=inject_client(mock_client),
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
            [{"url": "https://example.com/cited", "title": "Cited", "result_type": 1}],
            max_elapsed=1800,
            json_output=True,
        )

    def test_wait_json_no_research(self, runner, mock_auth, mock_fetch_tokens):
        mock_client = create_mock_client()
        mock_client.research.poll = AsyncMock(return_value=research_task({"status": "no_research"}))

        result = runner.invoke(
            cli, ["research", "wait", "-n", "nb_123", "--json"], obj=inject_client(mock_client)
        )

        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["status"] == "no_research"
        assert "error" in data

    def test_wait_json_failed(self, runner, mock_auth, mock_fetch_tokens):
        mock_client = create_mock_client()
        mock_client.research.poll = AsyncMock(
            return_value=research_task(
                {
                    "status": "failed",
                    "task_id": "task_123",
                    "query": "AI research",
                    "sources": [{"title": "Source 1", "url": "http://example.com"}],
                    "report": "# Partial",
                }
            )
        )

        result = runner.invoke(
            cli, ["research", "wait", "-n", "nb_123", "--json"], obj=inject_client(mock_client)
        )

        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["status"] == "failed"
        assert data["error"] == "Research failed"
        assert data["query"] == "AI research"
        assert data["sources_found"] == 1
        assert data["report"] == "# Partial"

    def test_wait_json_timeout(self, runner, mock_auth, mock_fetch_tokens):
        mock_client = create_mock_client()
        mock_client.research.poll = AsyncMock(
            return_value=research_task({"status": "in_progress", "query": "AI research"})
        )

        result = runner.invoke(
            cli,
            ["research", "wait", "-n", "nb_123", "--json", "--timeout", "1", "--interval", "1"],
            obj=inject_client(mock_client),
        )

        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["status"] == "timeout"


# =============================================================================
# RESEARCH CANCEL TESTS
# =============================================================================


class TestResearchCancel:
    def test_cancel_requests_and_confirms(self, runner, mock_auth, mock_fetch_tokens):
        mock_client = create_mock_client()
        mock_client.research.cancel = AsyncMock(return_value=None)

        result = runner.invoke(
            cli,
            ["research", "cancel", "run_456", "-n", "nb_123"],
            obj=inject_client(mock_client),
        )

        assert result.exit_code == 0
        assert "Cancel requested for run:" in result.output
        assert "run_456" in result.output
        # The notebook is resolved and passed through with the run id; fire-and-
        # forget so the command never asserts success from the (empty) response.
        mock_client.research.cancel.assert_awaited_once()
        args = mock_client.research.cancel.await_args.args
        assert args[0] == "nb_123"
        assert args[1] == "run_456"

    def test_cancel_json_output(self, runner, mock_auth, mock_fetch_tokens):
        mock_client = create_mock_client()
        mock_client.research.cancel = AsyncMock(return_value=None)

        result = runner.invoke(
            cli,
            ["research", "cancel", "run_456", "-n", "nb_123", "--json"],
            obj=inject_client(mock_client),
        )

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data == {"run_id": "run_456", "cancel_requested": True}

    def test_cancel_does_not_raise_on_unknown_id(self, runner, mock_auth, mock_fetch_tokens):
        # The server returns [] for an unknown id; ``cancel`` returns None and
        # the command still reports success (fire-and-forget — confirm by poll).
        mock_client = create_mock_client()
        mock_client.research.cancel = AsyncMock(return_value=None)

        result = runner.invoke(
            cli,
            ["research", "cancel", "00000000-0000-0000-0000-000000000000", "-n", "nb_123"],
            obj=inject_client(mock_client),
        )

        assert result.exit_code == 0
        assert "Cancel requested for run:" in result.output

    def test_cancel_requires_run_id(self, runner):
        # RUN_ID is a required positional; omitting it is a Click usage error.
        result = runner.invoke(cli, ["research", "cancel", "-n", "nb_123"])
        assert result.exit_code == 2
        assert "Missing argument" in result.output or "Usage:" in result.output


# =============================================================================
# COMMAND EXISTENCE TESTS
# =============================================================================


class TestResearchDiscover:
    def _client(self, task):
        mock_client = create_mock_client()
        mock_client.research.discover = AsyncMock(return_value=task)
        return mock_client

    def test_discover_renders_sources_overview_and_run_id(
        self, runner, mock_auth, mock_fetch_tokens
    ):
        mock_client = self._client(
            research_task(
                {
                    "status": "completed",
                    "query": "history of the transistor",
                    "sources": [{"title": "Source 1", "url": "http://example.com/1"}],
                    "summary": "An overview.",
                },
                task_id="job_001",
            )
        )
        result = runner.invoke(
            cli,
            ["research", "discover", "history of the transistor", "-n", "nb_123"],
            obj=inject_client(mock_client),
        )
        assert result.exit_code == 0, result.output
        mock_client.research.discover.assert_awaited_once_with(
            "nb_123", "history of the transistor", mode="default"
        )
        assert "Research completed" in result.output
        assert "Source 1" in result.output
        assert "An overview." in result.output
        assert "Run id: job_001" in result.output

    def test_discover_json_emits_the_public_task_dict(self, runner, mock_auth, mock_fetch_tokens):
        task = research_task(
            {"status": "completed", "query": "q", "sources": [], "summary": "o"}, task_id="job_002"
        )
        mock_client = self._client(task)
        result = runner.invoke(
            cli,
            ["research", "discover", "q", "-n", "nb_123", "--json"],
            obj=inject_client(mock_client),
        )
        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == task.to_public_dict()

    def test_discover_forwards_the_mode_and_allows_an_empty_curious_query(
        self, runner, mock_auth, mock_fetch_tokens
    ):
        mock_client = self._client(
            research_task({"status": "completed", "query": "", "sources": []}, task_id="job_003")
        )
        result = runner.invoke(
            cli,
            ["research", "discover", "-n", "nb_123", "--mode", "curious"],
            obj=inject_client(mock_client),
        )
        assert result.exit_code == 0, result.output
        mock_client.research.discover.assert_awaited_once_with("nb_123", "", mode="curious")

    def test_discover_rejects_an_unknown_mode_at_the_option(
        self, runner, mock_auth, mock_fetch_tokens
    ):
        result = runner.invoke(cli, ["research", "discover", "q", "-n", "nb_123", "--mode", "deep"])
        assert result.exit_code != 0
        assert "Invalid value for '--mode'" in result.output


class TestResearchCommandsExist:
    def test_research_discover_command_exists(self, runner):
        result = runner.invoke(cli, ["research", "discover", "--help"])
        assert result.exit_code == 0
        assert "Discover web sources for QUERY" in result.output

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

    def test_research_cancel_command_exists(self, runner):
        result = runner.invoke(cli, ["research", "cancel", "--help"])
        assert result.exit_code == 0
        assert "Cancel an in-flight research run" in result.output

    def test_research_import_command_exists(self, runner):
        result = runner.invoke(cli, ["research", "import", "--help"])
        assert result.exit_code == 0
        assert "Import a completed research run's sources" in result.output


# =============================================================================
# RESEARCH IMPORT TESTS (#2206)
# =============================================================================


class _ImportedList(list):
    """A ``list`` carrying the ``already_present`` side channel, as the client does."""

    def __init__(self, items, already_present=()):
        super().__init__(items)
        self.already_present = list(already_present)


def _completed_poll(*, task_id="run_789", sources=None, report=""):
    return research_task(
        {
            "task_id": task_id,
            "status": "completed",
            "query": "AI research",
            "sources": sources
            if sources is not None
            else [
                {"title": "Source 1", "url": "http://example.com/1"},
                {"title": "Source 2", "url": "http://example.com/2"},
            ],
            "report": report,
        }
    )


class TestResearchImport:
    def test_import_defaults_to_the_notebooks_current_run(
        self, runner, mock_auth, mock_fetch_tokens
    ):
        """No --run-id: the run is taken from the poll, which is also the only poll."""
        mock_client = create_mock_client()
        mock_client.research.poll = AsyncMock(return_value=_completed_poll())
        mock_client.research.import_sources_with_verification = AsyncMock(
            return_value=_ImportedList([{"id": "s1", "title": "Source 1"}])
        )

        result = runner.invoke(
            cli, ["research", "import", "-n", "nb_123"], obj=inject_client(mock_client)
        )

        assert result.exit_code == 0
        assert "Imported 1 sources" in result.output
        # ONE poll — the resolve and the importable-state check share it.
        assert mock_client.research.poll.await_count == 1
        # The resolved run id is what the import is pinned to.
        assert mock_client.research.import_sources_with_verification.await_args.args[1] == "run_789"

    def test_import_pins_an_explicit_run_id(self, runner, mock_auth, mock_fetch_tokens):
        mock_client = create_mock_client()
        mock_client.research.poll = AsyncMock(return_value=_completed_poll(task_id="other"))
        mock_client.research.import_sources_with_verification = AsyncMock(
            return_value=_ImportedList([{"id": "s1"}])
        )

        result = runner.invoke(
            cli,
            ["research", "import", "-n", "nb_123", "--run-id", "run_explicit"],
            obj=inject_client(mock_client),
        )

        assert result.exit_code == 0
        # The poll is filtered by the requested id, and the import uses it too —
        # never the notebook's current run.
        assert mock_client.research.poll.await_args.args[1] == "run_explicit"
        assert (
            mock_client.research.import_sources_with_verification.await_args.args[1]
            == "run_explicit"
        )

    def test_import_never_waits_on_an_in_progress_run(self, runner, mock_auth, mock_fetch_tokens):
        """The whole point of the command: refuse, don't block."""
        mock_client = create_mock_client()
        mock_client.research.poll = AsyncMock(
            return_value=research_task({"task_id": "run_789", "status": "in_progress"})
        )
        mock_client.research.import_sources_with_verification = AsyncMock()

        result = runner.invoke(
            cli, ["research", "import", "-n", "nb_123"], obj=inject_client(mock_client)
        )

        assert result.exit_code == 1
        assert "not complete" in result.output
        # Polled exactly once and imported nothing — no retry loop, no wait.
        assert mock_client.research.poll.await_count == 1
        mock_client.research.import_sources_with_verification.assert_not_awaited()

    def test_import_refuses_when_no_research_run_exists(self, runner, mock_auth, mock_fetch_tokens):
        mock_client = create_mock_client()
        mock_client.research.poll = AsyncMock(return_value=research_task({"status": "no_research"}))
        mock_client.research.import_sources_with_verification = AsyncMock()

        result = runner.invoke(
            cli, ["research", "import", "-n", "nb_123", "--json"], obj=inject_client(mock_client)
        )

        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["code"] == "VALIDATION_ERROR"
        assert "No research run found" in data["message"]
        mock_client.research.import_sources_with_verification.assert_not_awaited()

    def test_import_refuses_a_completed_run_with_no_sources(
        self, runner, mock_auth, mock_fetch_tokens
    ):
        mock_client = create_mock_client()
        mock_client.research.poll = AsyncMock(return_value=_completed_poll(sources=[]))
        mock_client.research.import_sources_with_verification = AsyncMock()

        result = runner.invoke(
            cli, ["research", "import", "-n", "nb_123"], obj=inject_client(mock_client)
        )

        assert result.exit_code == 1
        assert "no sources to import" in result.output
        mock_client.research.import_sources_with_verification.assert_not_awaited()

    def test_import_reports_the_idempotency_split(self, runner, mock_auth, mock_fetch_tokens):
        """A repeat import reads as "0 new, N already present", not a silent no-op (#2206)."""
        mock_client = create_mock_client()
        mock_client.research.poll = AsyncMock(return_value=_completed_poll())
        mock_client.research.import_sources_with_verification = AsyncMock(
            return_value=_ImportedList(
                [],
                already_present=[
                    {"id": "s1", "title": "Source 1", "url": "http://example.com/1"},
                    {"id": "s2", "title": "Source 2", "url": "http://example.com/2"},
                ],
            )
        )

        result = runner.invoke(
            cli, ["research", "import", "-n", "nb_123", "--json"], obj=inject_client(mock_client)
        )

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "already_imported"
        assert data["imported"] == 0
        assert data["already_present"] == 2
        assert [row["id"] for row in data["already_present_sources"]] == ["s1", "s2"]
        assert data["run_id"] == "run_789"
        assert data["sources_found"] == 2
        assert data["sources_selected"] == 2

    def test_import_text_mode_mentions_the_skipped_sources(
        self, runner, mock_auth, mock_fetch_tokens
    ):
        mock_client = create_mock_client()
        mock_client.research.poll = AsyncMock(return_value=_completed_poll())
        mock_client.research.import_sources_with_verification = AsyncMock(
            return_value=_ImportedList([], already_present=[{"id": "s1"}])
        )

        result = runner.invoke(
            cli, ["research", "import", "-n", "nb_123"], obj=inject_client(mock_client)
        )

        assert result.exit_code == 0
        assert "Imported 0 sources" in result.output
        assert "1 already present" in result.output

    def test_import_max_sources_caps_what_is_imported(self, runner, mock_auth, mock_fetch_tokens):
        mock_client = create_mock_client()
        mock_client.research.poll = AsyncMock(return_value=_completed_poll())
        mock_client.research.import_sources_with_verification = AsyncMock(
            return_value=_ImportedList([{"id": "s1"}])
        )

        result = runner.invoke(
            cli,
            ["research", "import", "-n", "nb_123", "--max-sources", "1", "--json"],
            obj=inject_client(mock_client),
        )

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["sources_found"] == 2
        assert data["sources_selected"] == 1
        # The cap reaches the client, not just the report.
        assert len(mock_client.research.import_sources_with_verification.await_args.args[2]) == 1

    def test_import_rejects_a_zero_max_sources_at_parse_time(self, runner):
        result = runner.invoke(cli, ["research", "import", "-n", "nb_123", "--max-sources", "0"])
        assert result.exit_code == 2

    def test_import_cited_only_narrows_the_selection(self, runner, mock_auth, mock_fetch_tokens):
        mock_client = create_mock_client()
        mock_client.research.poll = AsyncMock(
            return_value=_completed_poll(report="see http://example.com/1 for details")
        )
        mock_client.research.import_sources_with_verification = AsyncMock(
            return_value=_ImportedList([{"id": "s1"}])
        )

        result = runner.invoke(
            cli,
            ["research", "import", "-n", "nb_123", "--cited-only", "--json"],
            obj=inject_client(mock_client),
        )

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["cited_only"] is True
        assert data["cited_only_fallback"] is False
        assert data["sources_found"] == 2
        assert data["sources_selected"] == 1
        sent = mock_client.research.import_sources_with_verification.await_args.args[2]
        assert [s["url"] for s in sent] == ["http://example.com/1"]

    def test_import_forwards_allow_duplicate(self, runner, mock_auth, mock_fetch_tokens):
        mock_client = create_mock_client()
        mock_client.research.poll = AsyncMock(return_value=_completed_poll())
        mock_client.research.import_sources_with_verification = AsyncMock(
            return_value=_ImportedList([{"id": "s1"}])
        )

        result = runner.invoke(
            cli,
            ["research", "import", "-n", "nb_123", "--allow-duplicate"],
            obj=inject_client(mock_client),
        )

        assert result.exit_code == 0
        assert (
            mock_client.research.import_sources_with_verification.await_args.kwargs[
                "allow_duplicate"
            ]
            is True
        )

    def test_import_bounds_the_import_retry_loop_with_timeout(
        self, runner, mock_auth, mock_fetch_tokens
    ):
        """--timeout reaches ``max_elapsed``, so the import retry loop is bounded.

        The command never waits for the RUN, but IMPORT_RESEARCH is retried with
        reconciliation and would otherwise inherit the library's 1800s default
        with no way to shorten it.
        """
        mock_client = create_mock_client()
        mock_client.research.poll = AsyncMock(return_value=_completed_poll())
        mock_client.research.import_sources_with_verification = AsyncMock(
            return_value=_ImportedList([{"id": "s1"}])
        )

        result = runner.invoke(
            cli,
            ["research", "import", "-n", "nb_123", "--timeout", "42"],
            obj=inject_client(mock_client),
        )

        assert result.exit_code == 0
        kwargs = mock_client.research.import_sources_with_verification.await_args.kwargs
        assert kwargs["max_elapsed"] == 42

    def test_import_timeout_defaults_to_the_research_wait_budget(
        self, runner, mock_auth, mock_fetch_tokens
    ):
        mock_client = create_mock_client()
        mock_client.research.poll = AsyncMock(return_value=_completed_poll())
        mock_client.research.import_sources_with_verification = AsyncMock(
            return_value=_ImportedList([{"id": "s1"}])
        )

        result = runner.invoke(
            cli, ["research", "import", "-n", "nb_123"], obj=inject_client(mock_client)
        )

        assert result.exit_code == 0
        kwargs = mock_client.research.import_sources_with_verification.await_args.kwargs
        assert kwargs["max_elapsed"] == 1800

    def test_import_reports_the_capped_count_not_the_uncapped_selection(
        self, runner, mock_auth, mock_fetch_tokens
    ):
        """Text mode must not announce more cited sources than it imports.

        The cited display used to render the pre-cap selection, so three
        citations with --max-sources 1 printed "Importing 3 cited source(s)" and
        imported one.
        """
        sources = [{"title": f"S{i}", "url": f"http://example.com/{i}"} for i in range(1, 4)]
        report = " ".join(f"http://example.com/{i}" for i in range(1, 4))
        mock_client = create_mock_client()
        mock_client.research.poll = AsyncMock(
            return_value=_completed_poll(sources=sources, report=report)
        )
        mock_client.research.import_sources_with_verification = AsyncMock(
            return_value=_ImportedList([{"id": "s1"}])
        )

        result = runner.invoke(
            cli,
            ["research", "import", "-n", "nb_123", "--cited-only", "--max-sources", "1"],
            obj=inject_client(mock_client),
        )

        assert result.exit_code == 0
        assert "1 of 3" in result.output
        assert "Importing 3 cited" not in result.output
        # "selected", not "cited": the count includes any preserved report row,
        # which ``matched_url_source_count`` excludes.
        assert "selected source(s)" in result.output
        sent = mock_client.research.import_sources_with_verification.await_args.args[2]
        assert len(sent) == 1

    def test_import_narrows_to_cited_sources_before_applying_the_cap(
        self, runner, mock_auth, mock_fetch_tokens
    ):
        """Order matters: cap-first would import the UNCITED source (#2206).

        The report cites only the second source, so `--cited-only --max-sources 1`
        must import that one. Applying the cap first would truncate to the first
        source and then "narrow" a set that no longer contains a cited entry.
        """
        mock_client = create_mock_client()
        mock_client.research.poll = AsyncMock(
            return_value=_completed_poll(report="only http://example.com/2 is cited")
        )
        mock_client.research.import_sources_with_verification = AsyncMock(
            return_value=_ImportedList([{"id": "s2"}])
        )

        result = runner.invoke(
            cli,
            ["research", "import", "-n", "nb_123", "--cited-only", "--max-sources", "1", "--json"],
            obj=inject_client(mock_client),
        )

        assert result.exit_code == 0
        sent = mock_client.research.import_sources_with_verification.await_args.args[2]
        assert [s["url"] for s in sent] == ["http://example.com/2"]

    def test_import_resume_hint_repins_the_notebook_run_and_filters(
        self, runner, mock_auth, mock_fetch_tokens
    ):
        """A SIGINT resume hint must re-run THIS import, not a wider one.

        A bare 'notebooklm research import' would retarget the active notebook,
        go ambiguous instead of re-pinning this run, and drop --cited-only /
        --max-sources — so following it could import sources the user excluded.
        """
        hint = research_module._import_resume_hint(
            notebook_id="nb_resolved",
            run_id="run_789",
            cited_only=True,
            max_sources=2,
            allow_duplicate=True,
            timeout=1800,
            json_output=False,
        )
        assert hint == (
            "notebooklm research import -n nb_resolved --run-id run_789 "
            "--cited-only --max-sources 2 --allow-duplicate"
        )

    def test_import_resume_hint_preserves_json_output(self):
        """The cancellation envelope is machine-readable; the resumed run must be too.

        Automation that parses the envelope and executes its ``resume_hint``
        would otherwise get human-readable output back and fail to parse it.
        """
        hint = research_module._import_resume_hint(
            notebook_id="nb",
            run_id="r",
            cited_only=False,
            max_sources=None,
            allow_duplicate=False,
            timeout=1800,
            json_output=True,
        )
        assert hint == "notebooklm research import -n nb --run-id r --json"

    def test_import_resume_hint_omits_defaults_and_unset_flags(self):
        hint = research_module._import_resume_hint(
            notebook_id="nb_resolved",
            run_id="run_789",
            cited_only=False,
            max_sources=None,
            allow_duplicate=False,
            timeout=1800,
            json_output=False,
        )
        assert hint == "notebooklm research import -n nb_resolved --run-id run_789"

    def test_import_resume_hint_carries_a_non_default_timeout(self):
        hint = research_module._import_resume_hint(
            notebook_id="nb",
            run_id="r",
            cited_only=False,
            max_sources=None,
            allow_duplicate=False,
            timeout=60,
            json_output=False,
        )
        assert hint.endswith("--timeout 60")

    def test_import_flags_the_cited_only_fallback(self, runner, mock_auth, mock_fetch_tokens):
        """When no citation resolves, --cited-only silently imports EVERYTHING.

        ``cited_only_fallback`` is the caller's only signal that they did not get
        the narrowing they asked for, so it has to be true here.
        """
        mock_client = create_mock_client()
        mock_client.research.poll = AsyncMock(
            return_value=_completed_poll(report="a report citing nothing at all")
        )
        mock_client.research.import_sources_with_verification = AsyncMock(
            return_value=_ImportedList([{"id": "s1"}, {"id": "s2"}])
        )

        result = runner.invoke(
            cli,
            ["research", "import", "-n", "nb_123", "--cited-only", "--json"],
            obj=inject_client(mock_client),
        )

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["cited_only_fallback"] is True
        assert data["sources_selected"] == 2

    def test_import_text_mode_reports_the_cited_selection(
        self, runner, mock_auth, mock_fetch_tokens
    ):
        """The text-mode cited display runs (it is suppressed only under --json)."""
        mock_client = create_mock_client()
        mock_client.research.poll = AsyncMock(
            return_value=_completed_poll(report="see http://example.com/1 for details")
        )
        mock_client.research.import_sources_with_verification = AsyncMock(
            return_value=_ImportedList([{"id": "s1"}])
        )

        result = runner.invoke(
            cli,
            ["research", "import", "-n", "nb_123", "--cited-only"],
            obj=inject_client(mock_client),
        )

        assert result.exit_code == 0
        assert "cited" in result.output.lower()

    def test_import_text_mode_cited_display_is_suppressed_under_json(
        self, runner, mock_auth, mock_fetch_tokens
    ):
        """Stdout purity: the cited chatter must never land in the JSON document."""
        mock_client = create_mock_client()
        mock_client.research.poll = AsyncMock(
            return_value=_completed_poll(report="see http://example.com/1 for details")
        )
        mock_client.research.import_sources_with_verification = AsyncMock(
            return_value=_ImportedList([{"id": "s1"}])
        )

        result = runner.invoke(
            cli,
            ["research", "import", "-n", "nb_123", "--cited-only", "--json"],
            obj=inject_client(mock_client),
        )

        assert result.exit_code == 0
        json.loads(result.output)  # parses => nothing extra was printed

    def test_import_partial_overlap_is_reported_as_imported(
        self, runner, mock_auth, mock_fetch_tokens
    ):
        """Some new + some already present is an IMPORT, not an "already_imported"."""
        mock_client = create_mock_client()
        mock_client.research.poll = AsyncMock(return_value=_completed_poll())
        mock_client.research.import_sources_with_verification = AsyncMock(
            return_value=_ImportedList([{"id": "s1"}], already_present=[{"id": "s2"}])
        )

        result = runner.invoke(
            cli, ["research", "import", "-n", "nb_123", "--json"], obj=inject_client(mock_client)
        )

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "imported"
        assert data["imported"] == 1
        assert data["already_present"] == 1

    def test_import_of_nothing_new_and_nothing_present_is_not_already_imported(
        self, runner, mock_auth, mock_fetch_tokens
    ):
        """0 new / 0 present (reachable on a report-only import) is not a repeat."""
        mock_client = create_mock_client()
        mock_client.research.poll = AsyncMock(return_value=_completed_poll())
        mock_client.research.import_sources_with_verification = AsyncMock(
            return_value=_ImportedList([])
        )

        result = runner.invoke(
            cli, ["research", "import", "-n", "nb_123", "--json"], obj=inject_client(mock_client)
        )

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "imported"
        assert data["imported"] == 0
        assert data["already_present"] == 0


# =============================================================================
# _display_cited_import_selection — capped-count arithmetic (#2206 review)
# =============================================================================


class TestCitedImportSelectionDisplay:
    """The capped message must count the SAME rows in numerator and denominator.

    ``matched_url_source_count`` counts cited URL rows only, while the selection
    can also carry a preserved deep-research report row — so mixing the two
    produced a message that could not be read literally.
    """

    @staticmethod
    def _selection(*, rows: int, matched: int, fallback: bool = False):
        from notebooklm.types import CitedSourceSelection

        return CitedSourceSelection(
            sources=[{"url": f"http://example.com/{i}"} for i in range(rows)],
            cited_url_count=matched,
            matched_url_source_count=matched,
            used_fallback=fallback,
        )

    def test_capped_message_counts_selected_rows_not_matched_urls(self, capsys):
        from rich.console import Console

        console = Console(force_terminal=False, width=200)
        # 3 cited URLs + 1 preserved report row = 4 selected rows; cap to 2.
        research_import_module._display_cited_import_selection(
            self._selection(rows=4, matched=3),
            output_console=console,
            selected_count=2,
        )
        out = capsys.readouterr().out
        assert "2 of 4 selected source(s)" in out
        # The old arithmetic mixed the two counters and said "2 of 3 cited".
        assert "of 3" not in out

    def test_uncapped_message_is_unchanged_for_the_research_wait_caller(self, capsys):
        from rich.console import Console

        console = Console(force_terminal=False, width=200)
        research_import_module._display_cited_import_selection(
            self._selection(rows=3, matched=3), output_console=console
        )
        assert "Importing 3 cited source(s)" in capsys.readouterr().out

    def test_capped_fallback_message_reports_the_cap(self, capsys):
        from rich.console import Console

        console = Console(force_terminal=False, width=200)
        research_import_module._display_cited_import_selection(
            self._selection(rows=5, matched=0, fallback=True),
            output_console=console,
            selected_count=2,
        )
        out = capsys.readouterr().out
        assert "Could not resolve cited sources" in out
        assert "2 of 5 selected source(s)" in out
        # It must not claim it is importing everything while capping.
        assert "importing all sources" not in out

    def test_uncapped_fallback_message_is_unchanged(self, capsys):
        from rich.console import Console

        console = Console(force_terminal=False, width=200)
        research_import_module._display_cited_import_selection(
            self._selection(rows=5, matched=0, fallback=True), output_console=console
        )
        assert "importing all sources" in capsys.readouterr().out
