"""Tests for the ``notebooklm suggest-prompts`` CLI command.

Mirrors the sibling chat-command tests (``test_chat.py``): drives the command
through the same ``inject_client`` seam used by the top-level ``ask`` command,
patching ``client.notebooks.suggest_prompts`` rather than constructing a real
client. Covers the default mode, an explicit ``--mode``, ``--json`` envelope
shape, and an out-of-range ``--mode`` (the method's ``ValidationError`` must
surface as a clean exit-1 / VALIDATION_ERROR envelope, not a traceback).
"""

import json
from unittest.mock import AsyncMock, patch

import pytest
from click.testing import CliRunner

import notebooklm.auth as auth_module
from notebooklm.cli import helpers as helpers_module
from notebooklm.notebooklm_cli import cli
from notebooklm.types import PromptSuggestion

from .conftest import create_mock_client, inject_client

SUGGESTIONS = [
    PromptSuggestion(title="Professional Briefing", prompt="Give me a briefing on the sources."),
    PromptSuggestion(title="Key Risks", prompt="What are the key risks raised?"),
    PromptSuggestion(title="Counterarguments", prompt="What are the strongest counterarguments?"),
]


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def mock_auth():
    with patch.object(helpers_module, "load_auth_from_storage") as mock:
        mock.return_value = {
            "SID": "test",
            "HSID": "test",
            "SSID": "test",
            "APISID": "test",
            "SAPISID": "test",
        }
        yield mock


def _invoke(runner, mock_client, argv):
    with patch.object(
        auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
    ) as mock_fetch:
        mock_fetch.return_value = ("csrf", "session")
        return runner.invoke(cli, argv, obj=inject_client(mock_client))


def test_default_mode_lists_suggestions(runner, mock_auth):
    mock_client = create_mock_client()
    mock_client.notebooks.suggest_prompts = AsyncMock(return_value=SUGGESTIONS)

    result = _invoke(runner, mock_client, ["suggest-prompts", "-n", "nb_123"])

    assert result.exit_code == 0, result.output
    # Default mode is 4 and source_ids defaults to None (all sources).
    mock_client.notebooks.suggest_prompts.assert_awaited_once()
    call = mock_client.notebooks.suggest_prompts.call_args
    assert call.kwargs["mode"] == 4
    assert call.kwargs["source_ids"] is None
    assert call.kwargs["query"] is None
    # Text output is a numbered list of title + prompt.
    assert "Professional Briefing" in result.output
    assert "Give me a briefing on the sources." in result.output
    assert "1." in result.output


def test_explicit_mode_and_query_forwarded(runner, mock_auth):
    mock_client = create_mock_client()
    mock_client.notebooks.suggest_prompts = AsyncMock(return_value=SUGGESTIONS)

    result = _invoke(
        runner,
        mock_client,
        ["suggest-prompts", "-n", "nb_123", "--mode", "8", "--query", "exam topics"],
    )

    assert result.exit_code == 0, result.output
    call = mock_client.notebooks.suggest_prompts.call_args
    assert call.kwargs["mode"] == 8
    assert call.kwargs["query"] == "exam topics"


def test_source_ids_forwarded(runner, mock_auth):
    mock_client = create_mock_client()
    mock_client.notebooks.suggest_prompts = AsyncMock(return_value=SUGGESTIONS)

    result = _invoke(
        runner,
        mock_client,
        ["suggest-prompts", "-n", "nb_123", "-s", "src_001", "-s", "src_002"],
    )

    assert result.exit_code == 0, result.output
    call = mock_client.notebooks.suggest_prompts.call_args
    assert call.kwargs["source_ids"] == ["src_001", "src_002"]


def test_json_envelope_shape(runner, mock_auth):
    mock_client = create_mock_client()
    mock_client.notebooks.suggest_prompts = AsyncMock(return_value=SUGGESTIONS)

    result = _invoke(runner, mock_client, ["suggest-prompts", "-n", "nb_123", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["notebook_id"] == "nb_123"
    assert payload["count"] == 3
    assert payload["suggestions"][0] == {
        "title": "Professional Briefing",
        "prompt": "Give me a briefing on the sources.",
    }


def test_empty_suggestions_text(runner, mock_auth):
    mock_client = create_mock_client()
    mock_client.notebooks.suggest_prompts = AsyncMock(return_value=[])

    result = _invoke(runner, mock_client, ["suggest-prompts", "-n", "nb_123"])

    assert result.exit_code == 0, result.output
    assert "No prompt suggestions" in result.output


def test_bad_mode_exits_one(runner, mock_auth):
    mock_client = create_mock_client()
    mock_client.notebooks.suggest_prompts = AsyncMock(return_value=[])

    result = _invoke(
        runner,
        mock_client,
        ["suggest-prompts", "-n", "nb_123", "--mode", "99", "--json"],
    )

    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert payload["error"] is True
    assert payload["code"] == "VALIDATION_ERROR"
    assert "1..9" in payload["message"]


def test_bad_mode_validated_before_source_resolution(runner, mock_auth):
    # The mode check fires BEFORE notebook/source resolution, so an out-of-range
    # mode with an unresolvable partial ``-s`` id still surfaces the mode error
    # (not a "no source found" error) and never incurs a ``sources.list`` RPC.
    mock_client = create_mock_client()
    mock_client.sources.list = AsyncMock(return_value=[])
    mock_client.notebooks.suggest_prompts = AsyncMock(return_value=[])

    result = _invoke(
        runner,
        mock_client,
        ["suggest-prompts", "-n", "nb_123", "-s", "does-not-exist", "--mode", "99", "--json"],
    )

    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert payload["code"] == "VALIDATION_ERROR"
    assert "1..9" in payload["message"]
    mock_client.sources.list.assert_not_awaited()
    mock_client.notebooks.suggest_prompts.assert_not_awaited()
