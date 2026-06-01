"""CLI integration tests for the chat commands (``ask`` + ``history``).

These tests exercise the full CLI -> Client -> RPC path using VCR cassettes,
covering the chat happy paths flagged by issue #1316 (the sibling notebook +
share groups landed in #1322; this file is the chat counterpart tracked by
#1324).

RPC fan-out per command
-----------------------
``ask`` uses the **streamed** chat backend (``_chat_wire`` / a ``_reqid``-bearing
streaming POST), preceded by a ``GET_LAST_CONVERSATION_ID`` lookup:

* ``ask``     -> ``hPTbtc`` (GET_LAST_CONVERSATION_ID) then ``rLM`` (streamed ask).
* ``history`` -> ``hPTbtc`` (GET_LAST_CONVERSATION_ID) then ``khqZz``
  (GET_CONVERSATION_TURNS).

The cassettes were recorded against a read-only notebook
(``mock_context`` supplies its full UUID, so ``resolve_notebook_id`` skips the
``LIST_NOTEBOOKS`` preflight and each cassette holds only the chat RPC chain).

Recording (maintainer, with a valid profile)::

    NOTEBOOKLM_VCR_RECORD=1 uv run pytest \\
        tests/integration/cli_vcr/test_chat.py -m vcr
"""

import json

import pytest

from notebooklm.notebooklm_cli import cli

from .conftest import notebooklm_vcr, skip_no_cassettes

pytestmark = [pytest.mark.vcr, skip_no_cassettes]


class TestAskCommand:
    """Test ``notebooklm ask`` (streamed chat backend)."""

    @notebooklm_vcr.use_cassette("chat_ask.yaml")
    def test_ask_question(self, runner, mock_auth_for_vcr, mock_context):
        """``ask`` streams an answer and prints it under the Answer header."""
        result = runner.invoke(cli, ["ask", "What is this notebook about?"])
        assert result.exit_code == 0, result.output
        assert "Answer:" in result.output

    @notebooklm_vcr.use_cassette("chat_ask.yaml")
    def test_ask_question_json(self, runner, mock_auth_for_vcr, mock_context):
        """``ask --json`` emits the chat-response envelope (answer + references)."""
        result = runner.invoke(cli, ["ask", "--json", "What is this notebook about?"])
        assert result.exit_code == 0, result.output

        # Parse strictly: a ``--json`` command's whole stdout must be valid
        # JSON with no stray prefix.
        data = json.loads(result.output)
        assert isinstance(data, dict), f"Expected JSON object, got: {result.output!r}"
        assert "answer" in data, f"Expected an 'answer' key: {data!r}"
        assert "references" in data, f"Expected a 'references' key: {data!r}"
        # ``raw_response`` is deliberately stripped from CLI output for brevity.
        assert "raw_response" not in data


class TestHistoryCommand:
    """Test ``notebooklm history`` (GET_LAST_CONVERSATION_ID + GET_CONVERSATION_TURNS)."""

    @notebooklm_vcr.use_cassette("chat_get_history.yaml")
    def test_history(self, runner, mock_auth_for_vcr, mock_context):
        """``history`` renders the Q&A turns for the last conversation."""
        result = runner.invoke(cli, ["history"])
        assert result.exit_code == 0, result.output
        assert "Conversation History" in result.output

    @notebooklm_vcr.use_cassette("chat_get_history.yaml")
    def test_history_json(self, runner, mock_auth_for_vcr, mock_context):
        """``history --json`` emits a parseable envelope carrying the turns.

        Reuses ``chat_get_history.yaml`` because ``--json`` only changes
        rendering, not the underlying ``hPTbtc`` + ``khqZz`` RPC chain, so no
        orphan cassette is recorded for an identical request.
        """
        result = runner.invoke(cli, ["history", "--json"])
        assert result.exit_code == 0, result.output

        # Parse strictly: the whole stdout must be valid JSON (no stray prefix).
        data = json.loads(result.output)
        assert isinstance(data, dict | list), f"Expected JSON, got: {result.output!r}"


class TestGetConversationTurnsCommand:
    """Test conversation turns fetching via GET_CONVERSATION_TURNS (khqZz) RPC.

    Cassette: chat_get_conversation_turns.yaml
    Notebook: f59447f4-2a13-4d64-9df8-bc89c615c7bd
    Conversation: b1556695-010e-4fe3-a841-a6efa7fe0697

    The cassette captures two sequential batchexecute calls:
      1. hPTbtc (GET_LAST_CONVERSATION_ID) -> returns one conversation ID
      2. khqZz (GET_CONVERSATION_TURNS) -> returns Q&A turns for that conversation
    """

    @notebooklm_vcr.use_cassette("chat_get_conversation_turns.yaml")
    def test_history_shows_qa_previews(self, runner, mock_auth_for_vcr, mock_context):
        """history command shows Q&A preview columns populated from khqZz turns API."""
        # Use the full UUID directly so resolve_notebook_id skips LIST_NOTEBOOKS
        result = runner.invoke(cli, ["history", "-n", "f59447f4-2a13-4d64-9df8-bc89c615c7bd"])
        assert result.exit_code == 0, result.output
        assert "What question should I" in result.output
        assert "Based on the sources" in result.output
