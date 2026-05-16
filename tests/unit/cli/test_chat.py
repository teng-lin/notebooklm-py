"""Tests for chat CLI commands (save-as-note, enhanced history)."""

from unittest.mock import AsyncMock, patch

import pytest
from click.testing import CliRunner

from notebooklm.notebooklm_cli import cli
from notebooklm.types import AskResult, ChatReference, Note

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

    def test_ask_save_as_note_with_citations_uses_rich_path(self, runner, mock_auth):
        """When AskResult.references is non-empty, --save-as-note should
        route through create_from_chat (the citation-rich path) rather
        than the plain-text notes.create() path (issue #660)."""
        with patch_client_for_module("chat") as mock_client_cls:
            mock_client = create_mock_client()
            ask_result = AskResult(
                answer="Apples are mentioned [1].",
                conversation_id="a1b2c3d4-0000-0000-0000-000000000001",
                turn_number=1,
                is_follow_up=False,
                references=[
                    ChatReference(
                        source_id="src-1",
                        citation_number=1,
                        cited_text="...apples...",
                        start_char=0,
                        end_char=10,
                        chunk_id="chunk-1",
                    )
                ],
                raw_response="",
            )
            mock_client.chat.ask = AsyncMock(return_value=ask_result)
            mock_client.chat.get_conversation_id = AsyncMock(return_value=None)
            mock_client.notes.create_from_chat = AsyncMock(return_value=make_note(title="Saved"))
            mock_client.notes.create = AsyncMock(return_value=make_note())
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["ask", "What fruit?", "--save-as-note", "-n", "nb_123"]
                )

            assert result.exit_code == 0, result.output
            # Citation-rich path was used.
            mock_client.notes.create_from_chat.assert_awaited_once()
            # Plain-text path was NOT used.
            mock_client.notes.create.assert_not_awaited()

    def test_ask_save_as_note_without_citations_falls_back_to_plain(self, runner, mock_auth):
        """When AskResult.references is empty (no citations in the
        answer), --save-as-note falls back to plain-text notes.create()
        rather than failing."""
        with patch_client_for_module("chat") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.chat.ask = AsyncMock(return_value=make_ask_result())  # empty refs
            mock_client.chat.get_conversation_id = AsyncMock(return_value=None)
            mock_client.notes.create_from_chat = AsyncMock()
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
            assert "No citations in answer" in result.output
            mock_client.notes.create.assert_awaited_once()
            mock_client.notes.create_from_chat.assert_not_awaited()


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

    def test_history_no_truncate_outputs_full_text(self, runner, mock_auth):
        """`history --no-truncate` lifts the ``max_width=50`` table cap (P6.T1 / I16).

        The default table preview slices each Q/A to 50 chars for the table
        cell *and* sets ``max_width=50`` on the column. ``--no-truncate``
        drops both, so a long Q/A pair renders in full. We verify by
        counting character occurrences (Rich may wrap inside the table cell
        depending on the auto-detected terminal width, but the character
        budget is preserved).
        """
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
                result = runner.invoke(
                    cli,
                    ["history", "--no-truncate", "-n", "nb_123"],
                )

            assert result.exit_code == 0, result.output
            # Default behavior slices to 50 chars per cell; --no-truncate
            # MUST emit all 100 instances of each character (Rich may
            # soft-wrap, but cannot drop characters).
            assert result.output.count("Q") >= 100
            assert result.output.count("A") >= 100

    def test_history_default_truncates_to_50_chars(self, runner, mock_auth):
        """Default (no flag) preserves the legacy 50-char preview cap (P6.T1 / I16).

        This regression test pins the existing behavior so the new
        --no-truncate flag does not silently change the default rendering.
        """
        long_q = "Q" * 200
        long_a = "A" * 200
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
                result = runner.invoke(cli, ["history", "-n", "nb_123"])

            assert result.exit_code == 0, result.output
            # The default branch slices each cell to 50 chars before adding
            # to the table, so the rendered output must contain at most ~50
            # of each character (giving generous slack for the
            # "Question"/"Answer preview" header letters).
            assert result.output.count("Q") <= 60
            assert result.output.count("A") <= 60


class TestAskTimeout:
    def test_ask_passes_timeout_to_client(self, runner, mock_auth):
        with patch_client_for_module("chat") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.chat.ask = AsyncMock(return_value=make_ask_result())
            mock_client.chat.get_conversation_id = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["ask", "What is 42?", "-n", "nb_123", "--timeout", "300"]
                )

            assert result.exit_code == 0, result.output
            mock_client_cls.assert_called_once()
            assert mock_client_cls.call_args.kwargs.get("timeout") == 300.0

    def test_ask_omits_timeout_kwarg_when_flag_not_set(self, runner, mock_auth):
        """When --timeout is not passed, the CLI must not override the library default."""
        with patch_client_for_module("chat") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.chat.ask = AsyncMock(return_value=make_ask_result())
            mock_client.chat.get_conversation_id = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["ask", "What is 42?", "-n", "nb_123"])

            assert result.exit_code == 0, result.output
            assert "timeout" not in mock_client_cls.call_args.kwargs

    def test_ask_rejects_non_positive_timeout(self, runner, mock_auth):
        result = runner.invoke(cli, ["ask", "What is 42?", "-n", "nb_123", "--timeout", "0"])
        assert result.exit_code == 2, result.output


class TestConfigureJsonOutput:
    """Smoke tests for `configure --json` (P2.T5 / I3)."""

    def test_configure_mode_json(self, runner, mock_auth):
        with patch_client_for_module("chat") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.chat.set_mode = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli,
                    ["configure", "-n", "nb_123", "--mode", "learning-guide", "--json"],
                )

            assert result.exit_code == 0, result.output
            import json

            data = json.loads(result.output)
            assert data["notebook_id"] == "nb_123"
            assert data["mode"] == "learning-guide"
            assert data["configured"] is True
            mock_client.chat.set_mode.assert_awaited_once()

    def test_configure_persona_json(self, runner, mock_auth):
        with patch_client_for_module("chat") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.chat.configure = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli,
                    [
                        "configure",
                        "-n",
                        "nb_123",
                        "--persona",
                        "Act as a chemistry tutor",
                        "--response-length",
                        "longer",
                        "--json",
                    ],
                )

            assert result.exit_code == 0, result.output
            import json

            data = json.loads(result.output)
            assert data["notebook_id"] == "nb_123"
            assert data["mode"] is None
            # ChatGoal.CUSTOM exposed as the lowercase enum name "custom"
            # because persona was provided.
            assert data["goal"] == "custom"
            assert data["persona"] == "Act as a chemistry tutor"
            assert data["response_length"] == "longer"
            assert data["configured"] is True
            mock_client.chat.configure.assert_awaited_once()

    def test_configure_no_flags_json(self, runner, mock_auth):
        """`configure --json` with no other flags should still emit valid JSON.

        Mirrors the non-JSON "Chat configured (no changes)" path so callers
        running the command in a script can still parse a result.
        """
        with patch_client_for_module("chat") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.chat.configure = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["configure", "-n", "nb_123", "--json"])

            assert result.exit_code == 0, result.output
            import json

            data = json.loads(result.output)
            assert data["notebook_id"] == "nb_123"
            assert data["mode"] is None
            assert data["goal"] is None
            assert data["persona"] is None
            assert data["response_length"] is None
            assert data["configured"] is True
            mock_client.chat.configure.assert_awaited_once()


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


class TestAskNewFlag:
    """Tests for `ask --new` flag (P4.T1 / I1).

    The --new flag was promised in the docstring but missing from the decorator.
    --new must skip both the local-cache and server-side conversation lookup so
    a fresh conversation is started, and it must conflict with --conversation-id.
    """

    def test_ask_new_starts_fresh_conversation(self, runner, mock_auth, tmp_path):
        """`ask --new` should NOT pass conversation_id to client.chat.ask."""
        # Pre-populate context with a cached conversation that would normally resume.
        context_file = tmp_path / "context.json"
        context_file.write_text('{"notebook_id": "nb_123", "conversation_id": "conv-cached-abc"}')

        fresh_result = AskResult(
            answer="Fresh answer.",
            conversation_id="conv-fresh-xyz",
            turn_number=1,
            is_follow_up=False,
            references=[],
            raw_response="",
        )

        with patch_client_for_module("chat") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.chat.ask = AsyncMock(return_value=fresh_result)
            # Server also has a conversation, but --new should skip both lookups.
            mock_client.chat.get_conversation_id = AsyncMock(return_value="conv-server-abc")
            mock_client_cls.return_value = mock_client

            with (
                patch(
                    "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
                ) as mock_fetch,
                patch("notebooklm.cli.helpers.get_context_path", return_value=context_file),
            ):
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["ask", "-n", "nb_123", "--new", "question"])

        assert result.exit_code == 0, result.output
        # Server lookup must be skipped when --new is set.
        mock_client.chat.get_conversation_id.assert_not_awaited()
        # client.chat.ask must be called with conversation_id=None to start fresh.
        mock_client.chat.ask.assert_awaited_once()
        call = mock_client.chat.ask.call_args
        assert call.kwargs.get("conversation_id") is None, (
            f"expected conversation_id=None, got {call.kwargs.get('conversation_id')!r}"
        )
        assert "New conversation: conv-fresh-xyz" in result.output

    def test_ask_new_conflicts_with_conversation_id(self, runner, mock_auth):
        """`ask --new --conversation-id <id>` should raise UsageError (exit 2)."""
        with patch_client_for_module("chat") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.chat.ask = AsyncMock(return_value=make_ask_result())
            mock_client.chat.get_conversation_id = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli,
                    [
                        "ask",
                        "-n",
                        "nb_123",
                        "--new",
                        "--conversation-id",
                        "conv-explicit-abc",
                        "question",
                    ],
                )

            # Click UsageError exits with code 2.
            assert result.exit_code == 2, result.output
            assert "--new" in result.output and "--conversation-id" in result.output
            # client.chat.ask must not have been awaited — error came before dispatch.
            mock_client.chat.ask.assert_not_awaited()

    def test_ask_new_skips_server_resume_when_no_local_cache(self, runner, mock_auth, tmp_path):
        """`ask --new` with no cached conversation must still skip the server fetch."""
        # Empty context (no cached conversation_id).
        context_file = tmp_path / "context.json"
        context_file.write_text('{"notebook_id": "nb_123"}')

        fresh_result = AskResult(
            answer="Fresh answer.",
            conversation_id="conv-fresh-xyz",
            turn_number=1,
            is_follow_up=False,
            references=[],
            raw_response="",
        )

        with patch_client_for_module("chat") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.chat.ask = AsyncMock(return_value=fresh_result)
            # Server has a conversation, but --new must NOT consult it.
            mock_client.chat.get_conversation_id = AsyncMock(return_value="conv-server-abc")
            mock_client_cls.return_value = mock_client

            with (
                patch(
                    "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
                ) as mock_fetch,
                patch("notebooklm.cli.helpers.get_context_path", return_value=context_file),
            ):
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["ask", "-n", "nb_123", "--new", "question"])

        assert result.exit_code == 0, result.output
        mock_client.chat.get_conversation_id.assert_not_awaited()
        call = mock_client.chat.ask.call_args
        assert call.kwargs.get("conversation_id") is None


# =============================================================================
# P7.T2 / M3 — Stdin (`-`) convention
# =============================================================================
#
# Unix tradition: a positional argument of ``-`` means "read from stdin".
# These tests pin that ``ask -`` and ``ask --prompt-file -`` both pull the
# question text from stdin via ``CliRunner.invoke(input=...)``. The non-``-``
# happy path is covered above (and via existing prompt-file tests), so these
# tests only need to assert the new dash semantics are wired correctly.


class TestAskStdinDash:
    """``notebooklm ask -`` and ``--prompt-file -`` accept piped stdin."""

    def test_ask_positional_dash_reads_stdin(self, runner, mock_auth):
        with patch_client_for_module("chat") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.chat.ask = AsyncMock(return_value=make_ask_result())
            mock_client.chat.get_conversation_id = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["ask", "-", "-n", "nb_123"], input="what is X?\n")

            assert result.exit_code == 0, result.output
            call = mock_client.chat.ask.call_args
            # Question is the second positional arg (notebook_id, question, ...)
            assert call.args[1] == "what is X?"

    def test_ask_prompt_file_dash_reads_stdin(self, runner, mock_auth):
        with patch_client_for_module("chat") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.chat.ask = AsyncMock(return_value=make_ask_result())
            mock_client.chat.get_conversation_id = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli,
                    ["ask", "--prompt-file", "-", "-n", "nb_123"],
                    input="prompt from stdin\n",
                )

            assert result.exit_code == 0, result.output
            call = mock_client.chat.ask.call_args
            assert call.args[1] == "prompt from stdin"

    def test_ask_positional_non_dash_unchanged(self, runner, mock_auth):
        """Regression: literal questions are not interpreted as stdin."""
        with patch_client_for_module("chat") as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.chat.ask = AsyncMock(return_value=make_ask_result())
            mock_client.chat.get_conversation_id = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            with patch(
                "notebooklm.auth.fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                # Pass input that should be IGNORED — positional question wins.
                result = runner.invoke(
                    cli, ["ask", "literal question", "-n", "nb_123"], input="ignored\n"
                )

            assert result.exit_code == 0, result.output
            call = mock_client.chat.ask.call_args
            assert call.args[1] == "literal question"
