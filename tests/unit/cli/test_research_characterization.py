"""Golden-snapshot characterization tests for ``research wait``.

These tests freeze the current ``research wait`` behavior (text + JSON modes,
across the happy / timeout / cancelled / --no-import / --import-all paths)
before the P3.T6a extraction lands. They MUST pass on main HEAD (commit 1 of
the PR) and continue to pass byte-for-byte after the service extraction
(commit 2).

Covered paths (text + JSON):

* happy           — research completes immediately, no import.
* timeout         — research stays ``in_progress`` until ``--timeout`` elapses.
* cancelled       — SIGINT during the polling spinner triggers the structured
                    cancellation envelope from ``emit_cancelled_and_exit``.
* --no-import     — completed; ``--import-all`` NOT passed; importer is never
                    invoked (this is the default "wait without importing" shape
                    a script would use).
* --import-all    — completed; ``--import-all`` calls ``import_with_retry``.

The JSON-mode snapshots compare parsed dicts (key set + values) so output is
stable across Click/Rich versions. The text-mode snapshots compare a small
normalized line slice (color codes stripped, fixed substrings only) so they
don't fight Rich rendering differences.
"""

from __future__ import annotations

import importlib
import json
import re
from unittest.mock import AsyncMock, patch

import pytest

from notebooklm.notebooklm_cli import cli

from .conftest import create_mock_client, research_task

research_import_module = importlib.import_module("notebooklm.cli.research_import")

# Strip ANSI/Rich SGR sequences so text-mode snapshots survive Rich version
# bumps. We don't care that "[green]" or "\x1b[32m" is on/off — we care that
# the literal phrase appears.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


@pytest.fixture
def runner_and_mocks(runner, mock_auth, mock_fetch_tokens):
    """Convenience fixture: returns ``(runner, mock_client_factory)``."""

    def factory(poll_return):
        mock_client = create_mock_client()
        # ``research.poll`` now returns a typed ``ResearchTask``; adapt legacy
        # dict specs declared by these characterization fixtures.
        adapted = research_task(poll_return) if isinstance(poll_return, dict) else poll_return
        mock_client.research.poll = AsyncMock(return_value=adapted)
        return mock_client

    return runner, factory


# ---------------------------------------------------------------------------
# Happy path — completed, no import
# ---------------------------------------------------------------------------


HAPPY_POLL = {
    "status": "completed",
    "task_id": "task_abc",
    "query": "AI research",
    "sources": [{"title": "Source 1", "url": "http://example.com"}],
    "report": "# Report\nBody",
}


class TestWaitHappy:
    def test_happy_text(self, runner_and_mocks):
        runner, factory = runner_and_mocks
        with patch("notebooklm.cli.research_cmd.NotebookLMClient") as mock_cls:
            mock_cls.return_value = factory(HAPPY_POLL)
            result = runner.invoke(cli, ["research", "wait", "-n", "nb_123"])

        assert result.exit_code == 0
        out = _strip_ansi(result.output)
        # Golden lines (order-sensitive substrings).
        assert "Research completed" in out
        assert "AI research" in out
        assert "Found 1 sources" in out
        assert "Source 1" in out
        assert "Report" in out
        # Import was NOT invoked.
        assert "Imported" not in out

    def test_happy_json(self, runner_and_mocks):
        runner, factory = runner_and_mocks
        with patch("notebooklm.cli.research_cmd.NotebookLMClient") as mock_cls:
            mock_cls.return_value = factory(HAPPY_POLL)
            result = runner.invoke(cli, ["research", "wait", "-n", "nb_123", "--json"])

        assert result.exit_code == 0
        payload = json.loads(result.output)
        # Frozen JSON contract (P1.T2 baseline).
        assert payload == {
            "status": "completed",
            "query": "AI research",
            "sources_found": 1,
            # Typed sources serialize back to the canonical dict shape, which
            # always carries ``result_type``.
            "sources": [{"url": "http://example.com", "title": "Source 1", "result_type": 1}],
            "report": "# Report\nBody",
        }
        # No import-related keys when --import-all was not passed.
        assert "imported" not in payload
        assert "cited_only" not in payload


# ---------------------------------------------------------------------------
# Timeout path — research never completes
# ---------------------------------------------------------------------------


TIMEOUT_POLL = {"status": "in_progress", "query": "AI research"}


class TestWaitTimeout:
    def test_timeout_text(self, runner_and_mocks):
        runner, factory = runner_and_mocks
        with patch("notebooklm.cli.research_cmd.NotebookLMClient") as mock_cls:
            mock_cls.return_value = factory(TIMEOUT_POLL)
            result = runner.invoke(
                cli,
                ["research", "wait", "-n", "nb_123", "--timeout", "1", "--interval", "1"],
            )

        assert result.exit_code == 1
        out = _strip_ansi(result.output)
        assert "Timed out after 1 seconds" in out

    def test_timeout_json(self, runner_and_mocks):
        runner, factory = runner_and_mocks
        with patch("notebooklm.cli.research_cmd.NotebookLMClient") as mock_cls:
            mock_cls.return_value = factory(TIMEOUT_POLL)
            result = runner.invoke(
                cli,
                [
                    "research",
                    "wait",
                    "-n",
                    "nb_123",
                    "--json",
                    "--timeout",
                    "1",
                    "--interval",
                    "1",
                ],
            )

        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload == {"status": "timeout", "error": "Timed out after 1s"}


# ---------------------------------------------------------------------------
# Cancelled path — SIGINT during polling triggers structured cancellation
# ---------------------------------------------------------------------------


class TestWaitCancelled:
    """Ctrl-C inside ``status_with_elapsed`` must surface the canonical
    ``Cancelled. Resume with: notebooklm research status`` shape.

    Verifies the resume hint plumbed into ``status_with_elapsed`` matches the
    one wired in ``research_cmd.research_wait`` today.
    """

    def test_cancelled_text(self, runner_and_mocks):
        runner, _ = runner_and_mocks
        mock_client = create_mock_client()
        mock_client.research.poll = AsyncMock(side_effect=KeyboardInterrupt)
        with patch("notebooklm.cli.research_cmd.NotebookLMClient") as mock_cls:
            mock_cls.return_value = mock_client
            result = runner.invoke(cli, ["research", "wait", "-n", "nb_123"])

        # SIGINT exit code is 130 (128 + signal 2).
        assert result.exit_code == 130
        out = _strip_ansi(result.output)
        # ``Cancelled.`` line + canonical resume hint.
        assert "Cancelled" in out
        assert "notebooklm research status" in out

    def test_cancelled_json(self, runner_and_mocks):
        runner, _ = runner_and_mocks
        mock_client = create_mock_client()
        mock_client.research.poll = AsyncMock(side_effect=KeyboardInterrupt)
        with patch("notebooklm.cli.research_cmd.NotebookLMClient") as mock_cls:
            mock_cls.return_value = mock_client
            result = runner.invoke(cli, ["research", "wait", "-n", "nb_123", "--json"])

        assert result.exit_code == 130
        payload = json.loads(result.output)
        # Structured cancellation envelope from emit_cancelled_and_exit.
        assert payload["error"] is True
        assert payload["code"] == "CANCELLED"
        # Resume hint surfaces the research-specific command.
        assert "notebooklm research status" in payload.get("resume_hint", "")


# ---------------------------------------------------------------------------
# --no-import path — explicit "completed without importing" goldens
# ---------------------------------------------------------------------------


class TestWaitNoImport:
    """Default ``research wait`` (no ``--import-all``) returns the completed
    payload but never calls the importer. This is the canonical script shape.
    """

    def test_no_import_text_does_not_invoke_importer(self, runner_and_mocks):
        runner, factory = runner_and_mocks
        with (
            patch("notebooklm.cli.research_cmd.NotebookLMClient") as mock_cls,
            patch.object(
                research_import_module, "import_with_retry", new_callable=AsyncMock
            ) as mock_import,
        ):
            mock_cls.return_value = factory(HAPPY_POLL)
            result = runner.invoke(cli, ["research", "wait", "-n", "nb_123"])

        assert result.exit_code == 0
        mock_import.assert_not_awaited()
        out = _strip_ansi(result.output)
        assert "Research completed" in out
        assert "Imported" not in out

    def test_no_import_json_omits_import_keys(self, runner_and_mocks):
        runner, factory = runner_and_mocks
        with (
            patch("notebooklm.cli.research_cmd.NotebookLMClient") as mock_cls,
            patch.object(
                research_import_module, "import_with_retry", new_callable=AsyncMock
            ) as mock_import,
        ):
            mock_cls.return_value = factory(HAPPY_POLL)
            result = runner.invoke(cli, ["research", "wait", "-n", "nb_123", "--json"])

        assert result.exit_code == 0
        mock_import.assert_not_awaited()
        payload = json.loads(result.output)
        assert "imported" not in payload
        assert "imported_sources" not in payload
        assert "cited_only" not in payload
        assert "cited_sources_selected" not in payload
        assert "cited_only_fallback" not in payload


# ---------------------------------------------------------------------------
# --import-all path — completed + import + (optional) --cited-only
# ---------------------------------------------------------------------------


class TestWaitImportAll:
    def test_import_all_text(self, runner_and_mocks):
        runner, factory = runner_and_mocks
        mock_client = factory(HAPPY_POLL)
        with (
            patch("notebooklm.cli.research_cmd.NotebookLMClient") as mock_cls,
            patch.object(
                research_import_module, "import_with_retry", new_callable=AsyncMock
            ) as mock_import,
        ):
            mock_cls.return_value = mock_client
            mock_import.return_value = [{"id": "src_1", "title": "Source 1"}]
            result = runner.invoke(
                cli,
                ["research", "wait", "-n", "nb_123", "--import-all"],
            )

        assert result.exit_code == 0
        out = _strip_ansi(result.output)
        assert "Research completed" in out
        assert "Imported 1 sources" in out
        # P1.T2 task-id pinning: the importer must receive the pinned task_id
        # discovered on the first poll, not None.
        mock_import.assert_awaited_once_with(
            mock_client,
            "nb_123",
            "task_abc",
            [{"url": "http://example.com", "title": "Source 1", "result_type": 1}],
            max_elapsed=300,
        )

    def test_import_all_json(self, runner_and_mocks):
        runner, factory = runner_and_mocks
        mock_client = factory(HAPPY_POLL)
        with (
            patch("notebooklm.cli.research_cmd.NotebookLMClient") as mock_cls,
            patch.object(
                research_import_module, "import_with_retry", new_callable=AsyncMock
            ) as mock_import,
        ):
            mock_cls.return_value = mock_client
            mock_import.return_value = [{"id": "src_1", "title": "Source 1"}]
            result = runner.invoke(
                cli,
                ["research", "wait", "-n", "nb_123", "--json", "--import-all"],
            )

        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["status"] == "completed"
        assert payload["sources_found"] == 1
        assert payload["imported"] == 1
        assert payload["imported_sources"] == [{"id": "src_1", "title": "Source 1"}]
        # cited_only branch not active.
        assert "cited_only" not in payload
        mock_import.assert_awaited_once_with(
            mock_client,
            "nb_123",
            "task_abc",
            [{"url": "http://example.com", "title": "Source 1", "result_type": 1}],
            max_elapsed=300,
            json_output=True,
        )

    def test_import_all_cited_only_text(self, runner_and_mocks):
        runner, _ = runner_and_mocks
        mock_client = create_mock_client()
        mock_client.research.poll = AsyncMock(
            return_value=research_task(
                {
                    "status": "completed",
                    "task_id": "task_abc",
                    "query": "AI research",
                    "sources": [
                        {"title": "Cited", "url": "https://example.com/cited"},
                        {"title": "Uncited", "url": "https://example.com/uncited"},
                    ],
                    "report": "Report cites https://example.com/cited",
                }
            )
        )
        with (
            patch("notebooklm.cli.research_cmd.NotebookLMClient") as mock_cls,
            patch.object(
                research_import_module, "import_with_retry", new_callable=AsyncMock
            ) as mock_import,
        ):
            mock_cls.return_value = mock_client
            mock_import.return_value = [{"id": "src_1", "title": "Cited"}]
            result = runner.invoke(
                cli,
                ["research", "wait", "-n", "nb_123", "--import-all", "--cited-only"],
            )

        assert result.exit_code == 0
        out = _strip_ansi(result.output)
        assert "Imported 1 sources" in out
        # Only the cited source is forwarded to the importer.
        mock_import.assert_awaited_once_with(
            mock_client,
            "nb_123",
            "task_abc",
            [{"url": "https://example.com/cited", "title": "Cited", "result_type": 1}],
            max_elapsed=300,
        )

    def test_import_all_cited_only_json(self, runner_and_mocks):
        runner, _ = runner_and_mocks
        mock_client = create_mock_client()
        mock_client.research.poll = AsyncMock(
            return_value=research_task(
                {
                    "status": "completed",
                    "task_id": "task_abc",
                    "query": "AI research",
                    "sources": [
                        {"title": "Cited", "url": "https://example.com/cited"},
                        {"title": "Uncited", "url": "https://example.com/uncited"},
                    ],
                    "report": "Report cites https://example.com/cited",
                }
            )
        )
        with (
            patch("notebooklm.cli.research_cmd.NotebookLMClient") as mock_cls,
            patch.object(
                research_import_module, "import_with_retry", new_callable=AsyncMock
            ) as mock_import,
        ):
            mock_cls.return_value = mock_client
            mock_import.return_value = [{"id": "src_1", "title": "Cited"}]
            result = runner.invoke(
                cli,
                [
                    "research",
                    "wait",
                    "-n",
                    "nb_123",
                    "--json",
                    "--import-all",
                    "--cited-only",
                ],
            )

        assert result.exit_code == 0
        payload = json.loads(result.output)
        # P1.T2 fields preserved.
        assert payload["cited_only"] is True
        assert payload["cited_sources_selected"] == 1
        assert payload["cited_only_fallback"] is False
        assert payload["imported"] == 1
        mock_import.assert_awaited_once_with(
            mock_client,
            "nb_123",
            "task_abc",
            [{"url": "https://example.com/cited", "title": "Cited", "result_type": 1}],
            max_elapsed=300,
            json_output=True,
        )


# ---------------------------------------------------------------------------
# P1.T2 task-id pinning regression guard (must survive extraction unmodified)
# ---------------------------------------------------------------------------


class TestTaskIdPinning:
    """First poll discovers a task_id; subsequent polls MUST pin to it.

    Repro of the P1.T2 fix: the wait loop pins the task_id discriminator after
    the first non-empty discovery so a second concurrent research task cannot
    substitute its sources/report into the in-flight wait.
    """

    def test_task_id_pinned_after_first_discovery(self, runner_and_mocks):
        runner, _ = runner_and_mocks
        mock_client = create_mock_client()
        # First poll: in_progress, with a task_id to pin. Second poll: completed.
        poll_mock = AsyncMock()
        poll_mock.side_effect = [
            research_task({"status": "in_progress", "task_id": "task_pinned", "query": "AI"}),
            research_task(
                {
                    "status": "completed",
                    "task_id": "task_pinned",
                    "query": "AI",
                    "sources": [{"title": "S", "url": "http://example.com"}],
                    "report": "R",
                }
            ),
        ]
        mock_client.research.poll = poll_mock
        with patch("notebooklm.cli.research_cmd.NotebookLMClient") as mock_cls:
            mock_cls.return_value = mock_client
            result = runner.invoke(
                cli,
                ["research", "wait", "-n", "nb_123", "--interval", "1", "--timeout", "10"],
            )

        assert result.exit_code == 0
        # First call: task_id=None (nothing pinned yet).
        first_call = poll_mock.await_args_list[0]
        assert first_call.kwargs.get("task_id") is None
        # Second call: task_id pinned to the value discovered on the first poll.
        second_call = poll_mock.await_args_list[1]
        assert second_call.kwargs.get("task_id") == "task_pinned"
