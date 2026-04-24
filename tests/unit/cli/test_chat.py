"""Tests for chat CLI commands (save-as-note, enhanced history)."""

from unittest.mock import AsyncMock, patch

import pytest
from click.testing import CliRunner

from notebooklm.notebooklm_cli import cli
from notebooklm.types import AskResult, Note

from .conftest import create_mock_client, patch_client_for_module


def make_note(id="note_abc", title="Chat Note", content="The answer") -> Note:
    return Note(id=id, notebook_id="nb_123", title=title, content=content)


def make_ask_result(answer="The answer is 42.") -> AskResult:
    return AskResult(
        answer=answer,
        conversation_id="a1b2c3d4-0000-0000-0000-000000000001",
        turn_number=1,
        is_follow_up=False,
        references=[],
        raw_response="",
    )


# get_history returns flat list of (question, answer) pairs
MOCK_CONV_ID = "conv-abc123"
MOCK_QA_PAIRS = [
    ("What is ML?", "ML is a type of AI."),
    ("Explain AI", "AI stands for Artificial Intelligence."),
]
MOCK_HISTORY = MOCK_QA_PAIRS


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


class TestAskSaveAsNote:
    def test_ask_save_as_note_creates_note(self, runner, mock_auth):
        with patch_client_for_module("chat") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.chat.ask = AsyncMock(return_value=make_ask_result())
            mock_client.chat.get_conversation_id = AsyncMock(return_value=None)
            mock_client.notes.create = AsyncMock(return_value=make_note())
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["ask", "What is 42?", "--save-as-note", "-n", "nb_123"]
                )

            assert result.exit_code == 0, result.output
            mock_client.notes.create.assert_awaited_once()
            call = mock_client.notes.create.call_args
            all_args = list(call.args) + list(call.kwargs.values())
            assert any("The answer is 42." in str(a) for a in all_args)

    def test_ask_save_as_note_uses_custom_title(self, runner, mock_auth):
        with patch_client_for_module("chat") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.chat.ask = AsyncMock(return_value=make_ask_result())
            mock_client.chat.get_conversation_id = AsyncMock(return_value=None)
            mock_client.notes.create = AsyncMock(return_value=make_note(title="My Title"))
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli,
                    [
                        "ask",
                        "What is 42?",
                        "--save-as-note",
                        "--note-title",
                        "My Title",
                        "-n",
                        "nb_123",
                    ],
                )

            assert result.exit_code == 0, result.output
            call = mock_client.notes.create.call_args
            all_args = list(call.args) + list(call.kwargs.values())
            assert any("My Title" in str(a) for a in all_args)

    def test_ask_without_flag_does_not_create_note(self, runner, mock_auth):
        with patch_client_for_module("chat") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.chat.ask = AsyncMock(return_value=make_ask_result())
            mock_client.chat.get_conversation_id = AsyncMock(return_value=None)
            mock_client.notes.create = AsyncMock(return_value=make_note())
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["ask", "What is 42?", "-n", "nb_123"])

            assert result.exit_code == 0, result.output
            mock_client.notes.create.assert_not_awaited()


class TestHistoryCommand:
    def test_history_shows_qa_pairs(self, runner, mock_auth):
        with patch_client_for_module("chat") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.chat.get_history = AsyncMock(return_value=MOCK_HISTORY)
            mock_client.chat.get_conversation_id = AsyncMock(return_value=MOCK_CONV_ID)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["history", "-n", "nb_123"])

            assert result.exit_code == 0, result.output
            assert "What is ML?" in result.output
            assert "Explain AI" in result.output

    def test_history_save_creates_note(self, runner, mock_auth):
        with patch_client_for_module("chat") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.chat.get_conversation_id = AsyncMock(return_value=MOCK_CONV_ID)
            mock_client.chat.get_history = AsyncMock(return_value=MOCK_HISTORY)
            mock_client.notes.create = AsyncMock(return_value=make_note())
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["history", "--save", "-n", "nb_123"])

            assert result.exit_code == 0, result.output
            mock_client.notes.create.assert_awaited_once()

    def test_history_empty_shows_message(self, runner, mock_auth):
        with patch_client_for_module("chat") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.chat.get_conversation_id = AsyncMock(return_value=None)
            mock_client.chat.get_history = AsyncMock(return_value=[])
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["history", "-n", "nb_123"])

            assert result.exit_code == 0, result.output
            assert "No conversation history" in result.output

    def test_history_json_outputs_valid_json(self, runner, mock_auth):
        with patch_client_for_module("chat") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.chat.get_history = AsyncMock(return_value=MOCK_HISTORY)
            mock_client.chat.get_conversation_id = AsyncMock(return_value=MOCK_CONV_ID)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["history", "--json", "-n", "nb_123"])

            assert result.exit_code == 0, result.output
            import json

            data = json.loads(result.output)
            assert data["notebook_id"] == "nb_123"
            assert data["conversation_id"] == MOCK_CONV_ID
            assert data["count"] == 2
            assert data["qa_pairs"][0]["turn"] == 1
            assert data["qa_pairs"][0]["question"] == "What is ML?"
            assert data["qa_pairs"][0]["answer"] == "ML is a type of AI."
            assert data["qa_pairs"][1]["turn"] == 2

    def test_history_json_empty(self, runner, mock_auth):
        with patch_client_for_module("chat") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.chat.get_history = AsyncMock(return_value=[])
            mock_client.chat.get_conversation_id = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["history", "--json", "-n", "nb_123"])

            assert result.exit_code == 0, result.output
            import json

            data = json.loads(result.output)
            assert data["qa_pairs"] == []
            assert data["count"] == 0

    def test_history_show_all_outputs_full_text(self, runner, mock_auth):
        long_q = "Q" * 100
        long_a = "A" * 100
        pairs = [(long_q, long_a)]

        with patch_client_for_module("chat") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.chat.get_history = AsyncMock(return_value=pairs)
            mock_client.chat.get_conversation_id = AsyncMock(return_value=MOCK_CONV_ID)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["history", "--show-all", "-n", "nb_123"])

            assert result.exit_code == 0, result.output
            # Rich may wrap long lines, so strip newlines and check full content
            flat = result.output.replace("\n", "")
            assert long_q in flat
            assert long_a in flat


class TestAskServerResumed:
    def test_ask_shows_resumed_when_no_local_conv_but_server_has_one(
        self, runner, mock_auth, tmp_path
    ):
        """When context has no conv ID but server returns one, output should say 'Resumed'."""
        context_file = tmp_path / "context.json"
        context_file.write_text('{"notebook_id": "nb_123"}')

        # is_follow_up=True because ask() was called with a conversation_id from server
        ask_result = AskResult(
            answer="The answer.",
            conversation_id="conv-server-abc",
            turn_number=1,
            is_follow_up=True,
            references=[],
            raw_response="",
        )

        with patch_client_for_module("chat") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.chat.ask = AsyncMock(return_value=ask_result)
            mock_client.chat.get_conversation_id = AsyncMock(return_value="conv-server-abc")
            mock_client_cls.return_value = mock_client

            with (
                patch(
                    "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
                ) as mock_fetch,
                patch("notebooklm.cli.helpers.get_context_path", return_value=context_file),
            ):
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["ask", "-n", "nb_123", "question"])

        assert result.exit_code == 0, result.output
        assert "Resumed conversation:" in result.output
        assert "(turn 1)" not in result.output

    def test_ask_shows_turn_number_for_local_follow_up(self, runner, mock_auth, tmp_path):
        """When context has a local conv ID, follow-up should show turn number."""
        context_file = tmp_path / "context.json"
        context_file.write_text('{"notebook_id": "nb_123", "conversation_id": "conv-local-abc"}')

        ask_result = AskResult(
            answer="The answer.",
            conversation_id="conv-local-abc",
            turn_number=2,
            is_follow_up=True,
            references=[],
            raw_response="",
        )

        with patch_client_for_module("chat") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.chat.ask = AsyncMock(return_value=ask_result)
            mock_client_cls.return_value = mock_client

            with (
                patch(
                    "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
                ) as mock_fetch,
                patch("notebooklm.cli.helpers.get_context_path", return_value=context_file),
            ):
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["ask", "-n", "nb_123", "follow-up question"])

        assert result.exit_code == 0, result.output
        assert "Conversation: conv-local-abc (turn 2)" in result.output
        assert "Resumed" not in result.output
