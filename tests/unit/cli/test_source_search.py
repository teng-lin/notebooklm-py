"""CLI coverage for ranked passage search over ``SourcesAPI.search``."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest
from click.testing import CliRunner

from notebooklm.notebooklm_cli import cli
from notebooklm.types import RelevantChunk, Source

from .conftest import create_mock_client, inject_client

NB = "nb_123"
SRC_A = "5777b434-46fd-4f4a-bf28-577b26ead71b"
SRC_B = "f6e549bc-7593-4146-ab8f-ce01094d1387"


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def _cli_auth(mock_auth, mock_fetch_tokens):
    """Every command opens the injected client; reuse the shared auth patches."""
    yield


def _invoke(runner: CliRunner, client, args: list[str]):
    return runner.invoke(cli, args, obj=inject_client(client))


class TestSourceSearch:
    def test_json_returns_the_direct_relevant_chunk_array(self, runner: CliRunner) -> None:
        client = create_mock_client()
        client.sources.search = AsyncMock(
            return_value=[
                RelevantChunk(SRC_A, "First passage", 1, 10, 23),
                RelevantChunk(SRC_B, "Passage without a span", 2),
            ]
        )

        result = _invoke(
            runner,
            client,
            ["source", "search", "revenue growth", "-n", NB, "--json"],
        )

        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == [
            {
                "source_id": SRC_A,
                "text": "First passage",
                "rank": 1,
                "start": 10,
                "end": 23,
            },
            {
                "source_id": SRC_B,
                "text": "Passage without a span",
                "rank": 2,
                "start": None,
                "end": None,
            },
        ]
        client.sources.search.assert_awaited_once_with(
            NB,
            "revenue growth",
            source_ids=None,
            limit=None,
        )
        client.sources.list.assert_not_awaited()

    def test_repeatable_source_filters_resolve_prefixes_once(self, runner: CliRunner) -> None:
        client = create_mock_client()
        client.sources.list = AsyncMock(
            return_value=[
                Source(id=SRC_A, title="Revenue"),
                Source(id=SRC_B, title="Forecast"),
            ]
        )
        client.sources.search = AsyncMock(return_value=[])

        result = _invoke(
            runner,
            client,
            [
                "source",
                "search",
                "growth",
                "-n",
                NB,
                "-s",
                SRC_A[:12],
                "--source",
                SRC_B[:12],
                "--limit",
                "2",
                "--json",
            ],
        )

        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout) == []
        assert result.stderr.count("Matched:") == 2
        client.sources.list.assert_awaited_once_with(NB)
        client.sources.search.assert_awaited_once_with(
            NB,
            "growth",
            source_ids=[SRC_A, SRC_B],
            limit=2,
        )

    def test_text_mode_renders_rank_source_span_and_literal_text(self, runner: CliRunner) -> None:
        client = create_mock_client()
        client.sources.search = AsyncMock(
            return_value=[
                RelevantChunk(SRC_A, "Revenue [grew] quickly", 1, 4, 26),
                RelevantChunk(SRC_B, "No coordinates", 2),
            ]
        )

        result = _invoke(
            runner,
            client,
            ["source", "search", "revenue", "-n", NB],
        )

        assert result.exit_code == 0, result.output
        assert "2 relevant passage(s)" in result.output
        assert "Rank" in result.output
        assert "Source ID" in result.output
        assert "Span" in result.output
        assert SRC_A in result.output
        assert "4:26" in result.output
        assert "Revenue [grew] quickly" in result.output
        assert "No coordinates" in result.output

    @pytest.mark.parametrize("json_output", [False, True])
    def test_empty_results_have_a_stable_output(self, runner: CliRunner, json_output: bool) -> None:
        client = create_mock_client()
        client.sources.search = AsyncMock(return_value=[])
        args = ["source", "search", "missing", "-n", NB]
        if json_output:
            args.append("--json")

        result = _invoke(runner, client, args)

        assert result.exit_code == 0, result.output
        if json_output:
            assert json.loads(result.output) == []
        else:
            assert "No relevant passages found." in result.output

    def test_non_positive_limit_is_rejected_before_client_io(self, runner: CliRunner) -> None:
        client = create_mock_client()
        client.sources.search = AsyncMock(return_value=[])

        result = _invoke(
            runner,
            client,
            ["source", "search", "query", "-n", NB, "--limit", "0"],
        )

        assert result.exit_code == 2
        assert "Invalid value for '--limit'" in result.output
        client.sources.search.assert_not_awaited()

    def test_help_documents_filters_limit_and_json(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["source", "search", "--help"])

        assert result.exit_code == 0, result.output
        assert "Search indexed passages" in result.output
        assert "-s, --source TEXT" in result.output
        assert "--limit INTEGER RANGE" in result.output
        assert "--json" in result.output
