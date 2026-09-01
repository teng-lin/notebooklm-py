"""CLI tests for the #2283 commands.

``source add-async`` / ``source append`` / ``source copy``, ``artifact copy`` /
``artifact choices`` and the top-level ``suggest-next-steps``. Each command is
driven through the real Click tree against the standard mock client
(``create_mock_client`` + ``inject_client``); the assertions pin the client
call the command makes and the ``--json`` envelope it prints.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from click.testing import CliRunner

from notebooklm.exceptions import SourceNotFoundError
from notebooklm.notebooklm_cli import cli
from notebooklm.types import (
    ArtifactCustomizationChoices,
    CopiedArtifact,
    CopiedSource,
    CustomizationChoice,
    NextStepSuggestion,
    ReportPreset,
)

from .conftest import create_mock_client, inject_client

NB = "nb_123"
SRC = "src_001"
ART = "art_1"


def _source(source_id: str = "src_new", title: str = "Copy") -> MagicMock:
    return MagicMock(
        id=source_id,
        title=title,
        kind=MagicMock(value="url"),
        status=MagicMock(value="ready"),
    )


def _artifact(artifact_id: str = "art_new", title: str = "ML Quiz") -> MagicMock:
    return MagicMock(
        id=artifact_id, title=title, kind=MagicMock(value="quiz"), status_str="completed"
    )


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def _cli_auth(mock_auth, mock_fetch_tokens):
    """Every command here opens the (mocked) client; reuse the shared auth patches."""
    yield


def _invoke(runner: CliRunner, client: MagicMock, args: list[str], **kwargs):
    return runner.invoke(cli, args, obj=inject_client(client), **kwargs)


# ---------------------------------------------------------------------------
# source add-async
# ---------------------------------------------------------------------------


class TestSourceAddAsync:
    def test_queues_urls_and_prints_ids(self, runner):
        client = create_mock_client()
        client.sources.add_urls_async = AsyncMock(return_value=[_source("src_a", "Example")])
        result = _invoke(runner, client, ["source", "add-async", "https://example.com/", "-n", NB])
        assert result.exit_code == 0, result.output
        assert "Queued 1 of 1 source(s)" in result.output
        assert "src_a" in result.output
        client.sources.add_urls_async.assert_awaited_once_with(NB, ["https://example.com/"])

    def test_json_envelope(self, runner):
        client = create_mock_client()
        client.sources.add_urls_async = AsyncMock(
            return_value=[_source("src_a", "A"), _source("src_b", "B")]
        )
        result = _invoke(
            runner,
            client,
            ["source", "add-async", "https://a.example/", "https://b.example/", "-n", NB, "--json"],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["notebook_id"] == NB
        assert data["count"] == 2
        assert data["requested"] == 2
        assert [s["id"] for s in data["sources"]] == ["src_a", "src_b"]
        # The shared ``source list`` / ``source get`` row shape, not a hand-rolled dict.
        assert data["sources"][0]["title"] == "A"
        assert {"id", "title", "type", "status"} <= set(data["sources"][0])

    def test_requires_at_least_one_url(self, runner):
        result = _invoke(runner, create_mock_client(), ["source", "add-async", "-n", NB])
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# source append
# ---------------------------------------------------------------------------


class TestSourceAppend:
    def test_appends_and_reports_characters(self, runner):
        client = create_mock_client()
        client.sources.append_text = AsyncMock(return_value=None)
        result = _invoke(runner, client, ["source", "append", SRC, "more text", "-n", NB])
        assert result.exit_code == 0, result.output
        assert "Appended 9 characters" in result.output
        client.sources.append_text.assert_awaited_once_with(NB, SRC, "more text", header="")

    def test_header_and_json(self, runner):
        client = create_mock_client()
        client.sources.append_text = AsyncMock(return_value=None)
        result = _invoke(
            runner, client, ["source", "append", SRC, "abc", "--header", "H", "-n", NB, "--json"]
        )
        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == {
            "notebook_id": NB,
            "source_id": SRC,
            "appended": True,
            "characters": 3,
        }
        client.sources.append_text.assert_awaited_once_with(NB, SRC, "abc", header="H")

    def test_reads_text_from_stdin(self, runner):
        client = create_mock_client()
        client.sources.append_text = AsyncMock(return_value=None)
        result = _invoke(
            runner, client, ["source", "append", SRC, "-", "-n", NB], input="piped text\n"
        )
        assert result.exit_code == 0, result.output
        client.sources.append_text.assert_awaited_once_with(NB, SRC, "piped text", header="")

    def test_empty_text_is_a_validation_error(self, runner):
        client = create_mock_client()
        client.sources.append_text = AsyncMock(return_value=None)
        result = _invoke(runner, client, ["source", "append", SRC, "", "-n", NB, "--json"])
        assert result.exit_code == 1
        assert json.loads(result.output)["code"] == "VALIDATION_ERROR"
        client.sources.append_text.assert_not_awaited()


# ---------------------------------------------------------------------------
# source copy
# ---------------------------------------------------------------------------


class TestSourceCopy:
    def test_copies_into_target_and_prints_pairs(self, runner):
        client = create_mock_client()
        client.sources.copy = AsyncMock(
            return_value=[CopiedSource(original_id=SRC, source=_source("src_new", "Copy"))]
        )
        result = _invoke(runner, client, ["source", "copy", SRC, "--to", NB, "-n", NB])
        assert result.exit_code == 0, result.output
        assert "Copied 1 of 1 source(s)" in result.output
        assert f"{SRC} -> src_new" in result.output
        client.sources.copy.assert_awaited_once_with(NB, [SRC], NB)

    def test_json_envelope_reports_requested_and_copied(self, runner):
        client = create_mock_client()
        client.sources.copy = AsyncMock(
            return_value=[CopiedSource(original_id=SRC, source=_source("src_new", "Copy"))]
        )
        result = _invoke(runner, client, ["source", "copy", SRC, "--to", NB, "-n", NB, "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["target_notebook_id"] == NB
        assert data["count"] == 1 and data["requested"] == 1
        assert data["copied"][0]["original_id"] == SRC
        assert data["copied"][0]["source"]["id"] == "src_new"

    def test_not_found_maps_to_error_envelope(self, runner):
        client = create_mock_client()
        client.sources.copy = AsyncMock(side_effect=SourceNotFoundError(SRC))
        result = _invoke(runner, client, ["source", "copy", SRC, "--to", NB, "-n", NB, "--json"])
        assert result.exit_code != 0
        assert json.loads(result.output)["error"] is True

    def test_target_is_required(self, runner):
        result = _invoke(runner, create_mock_client(), ["source", "copy", SRC, "-n", NB])
        assert result.exit_code != 0
        assert "--to" in result.output


# ---------------------------------------------------------------------------
# artifact copy
# ---------------------------------------------------------------------------


class TestArtifactCopy:
    def test_copies_and_prints_pairs(self, runner):
        client = create_mock_client()
        client.artifacts.copy = AsyncMock(
            return_value=[CopiedArtifact(original_id=ART, artifact=_artifact())]
        )
        result = _invoke(runner, client, ["artifact", "copy", ART, "--to", NB, "-n", NB])
        assert result.exit_code == 0, result.output
        assert "Copied 1 of 1 artifact(s)" in result.output
        assert f"{ART} -> art_new" in result.output
        client.artifacts.copy.assert_awaited_once_with(NB, [ART], NB)

    def test_json_envelope(self, runner):
        client = create_mock_client()
        client.artifacts.copy = AsyncMock(
            return_value=[CopiedArtifact(original_id=ART, artifact=_artifact())]
        )
        result = _invoke(runner, client, ["artifact", "copy", ART, "--to", NB, "-n", NB, "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["copied"] == [
            {
                "original_id": ART,
                "artifact": {
                    "id": "art_new",
                    "title": "ML Quiz",
                    "type": "quiz",
                    "status": "completed",
                },
            }
        ]
        assert data["count"] == 1 and data["requested"] == 1


# ---------------------------------------------------------------------------
# artifact choices
# ---------------------------------------------------------------------------


_CHOICES = ArtifactCustomizationChoices(
    audio=[CustomizationChoice(1, "Deep Dive", "Two hosts")],
    video=[CustomizationChoice(3, "Cinematic", "Rich")],
    slide_deck=[CustomizationChoice(2, "Presenter Slides", "Clean")],
    reports=[ReportPreset("Briefing Doc", "Key insights", "Create a briefing.")],
)


class TestArtifactChoices:
    def test_renders_tables(self, runner):
        client = create_mock_client()
        client.artifacts.get_customization_choices = AsyncMock(return_value=_CHOICES)
        result = _invoke(runner, client, ["artifact", "choices", "-n", NB])
        assert result.exit_code == 0, result.output
        for label in ("Audio formats", "Video formats", "Slide-deck formats", "Report presets"):
            assert label in result.output
        assert "Deep Dive" in result.output and "Briefing Doc" in result.output
        client.artifacts.get_customization_choices.assert_awaited_once_with(NB)

    def test_json_envelope_carries_directives(self, runner):
        client = create_mock_client()
        client.artifacts.get_customization_choices = AsyncMock(return_value=_CHOICES)
        result = _invoke(runner, client, ["artifact", "choices", "-n", NB, "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["audio"] == [{"code": 1, "title": "Deep Dive", "description": "Two hosts"}]
        assert data["slide_deck"][0]["code"] == 2
        assert data["reports"][0]["directive"] == "Create a briefing."

    def test_notebook_is_optional(self, runner, monkeypatch):
        from notebooklm.cli import artifact_cmd

        monkeypatch.setattr(artifact_cmd, "get_current_notebook", lambda: None)
        client = create_mock_client()
        client.artifacts.get_customization_choices = AsyncMock(return_value=_CHOICES)
        result = _invoke(runner, client, ["artifact", "choices", "--json"])
        assert result.exit_code == 0, result.output
        client.artifacts.get_customization_choices.assert_awaited_once_with(None)


# ---------------------------------------------------------------------------
# suggest-next-steps
# ---------------------------------------------------------------------------


class TestSuggestNextSteps:
    def test_lists_questions(self, runner):
        client = create_mock_client()
        client.notebooks.suggest_next_steps = AsyncMock(
            return_value=[
                NextStepSuggestion("Why does X?", 9),
                NextStepSuggestion("What about Y?", 9),
            ]
        )
        result = _invoke(runner, client, ["suggest-next-steps", "-n", NB])
        assert result.exit_code == 0, result.output
        assert "1. Why does X?" in result.output and "2. What about Y?" in result.output
        client.notebooks.suggest_next_steps.assert_awaited_once_with(NB, source_ids=None)

    def test_json_and_source_scoping(self, runner):
        client = create_mock_client()
        client.notebooks.suggest_next_steps = AsyncMock(
            return_value=[NextStepSuggestion("Why does X?", 9)]
        )
        result = _invoke(
            runner,
            client,
            ["suggest-next-steps", "-n", NB, "-s", "src_1", "--json"],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data == {
            "notebook_id": NB,
            "suggestions": [{"question": "Why does X?", "type_code": 9}],
            "count": 1,
        }
        kwargs = client.notebooks.suggest_next_steps.await_args.kwargs
        assert kwargs["source_ids"] == ["src_1"]

    def test_empty_result_message(self, runner):
        client = create_mock_client()
        client.notebooks.suggest_next_steps = AsyncMock(return_value=[])
        result = _invoke(runner, client, ["suggest-next-steps", "-n", NB])
        assert result.exit_code == 0, result.output
        assert "No follow-up suggestions returned" in result.output


class TestPartialCopiesAndValidation:
    def test_partial_source_copy_lists_missing_ids_and_exits_one(self, runner):
        client = create_mock_client()
        client.sources.copy = AsyncMock(
            return_value=[CopiedSource(original_id="src_1", source=_source("src_new", "Copy"))]
        )
        result = _invoke(
            runner, client, ["source", "copy", "src_1", "src_2", "--to", NB, "-n", NB, "--json"]
        )
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["count"] == 1 and data["requested"] == 2
        assert data["not_copied"] == ["src_2"]
        client.sources.copy.assert_awaited_once_with(NB, ["src_1", "src_2"], NB)
        text = _invoke(runner, client, ["source", "copy", "src_1", "src_2", "--to", NB, "-n", NB])
        assert text.exit_code == 1
        assert "Copied 1 of 2" in text.output and "Not copied (1): src_2" in text.output

    def test_partial_artifact_copy_exits_one(self, runner):
        client = create_mock_client()
        client.artifacts.copy = AsyncMock(
            return_value=[CopiedArtifact(original_id="art_1", artifact=_artifact())]
        )
        result = _invoke(
            runner, client, ["artifact", "copy", "art_1", "art_2", "--to", NB, "-n", NB, "--json"]
        )
        assert result.exit_code == 1
        assert json.loads(result.output)["not_copied"] == ["art_2"]

    def test_add_async_rejects_non_http_and_internal_urls(self, runner):
        client = create_mock_client()
        client.sources.add_urls_async = AsyncMock(return_value=[])
        for bad in ("file:///etc/passwd", "http://localhost:9000/admin", "hello world"):
            result = _invoke(runner, client, ["source", "add-async", bad, "-n", NB, "--json"])
            assert result.exit_code != 0, bad
            assert json.loads(result.output)["error"] is True
        client.sources.add_urls_async.assert_not_awaited()
        ok = _invoke(
            runner,
            client,
            [
                "source",
                "add-async",
                "http://localhost:9000/x",
                "--allow-internal",
                "-n",
                NB,
                "--json",
            ],
        )
        assert ok.exit_code == 0, ok.output
        client.sources.add_urls_async.assert_awaited_once()
