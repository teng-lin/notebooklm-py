"""Unit tests for the transport-neutral ``notebooklm._app.chat`` core.

``_app/chat.py`` had **zero** direct coverage before this file — it was
exercised only through ``CliRunner`` in ``tests/unit/cli/test_chat.py``. These
tests pin the Click-free chat business logic at the ``_app`` boundary with a
``MagicMock`` client and a tiny in-memory :class:`ProgressSink`, independent of
the Click adapter / exit-code policy:

* the conversation-id selection ladder (:func:`determine_conversation_id`) and
  its lazy short-circuit reads,
* :func:`get_latest_conversation_from_server` (resumed vs. unavailable),
* :func:`validate_ask_flags` (the ``--new`` / ``--conversation-id`` conflict),
* :func:`execute_configure` / :class:`ConfigureResult` mode-vs-persona
  projection,
* the ``ask --save-as-note`` workflow (:func:`save_answer_as_note`),
* the history fetch + note-content formatters + clear-cache count capture.

The CLI tests keep ownership of the ``--json`` envelope shapes, exit codes, and
status-prose-under-``--quiet`` contracts.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from notebooklm._app.chat import (
    ClearCacheResult,
    ConfigureResult,
    HistoryFetch,
    SaveNoteOutcome,
    determine_conversation_id,
    execute_clear_cache,
    execute_configure,
    fetch_history,
    format_history,
    format_single_qa,
    get_latest_conversation_from_server,
    save_answer_as_note,
    validate_ask_flags,
)
from notebooklm._app.events import ProgressEvent, ProgressSink
from notebooklm.exceptions import ValidationError
from notebooklm.rpc.types import ChatGoal, ChatResponseLength
from notebooklm.types import AskResult, ChatMode, ChatSettings, Note


class _RecordingSink:
    """In-memory :class:`ProgressSink` that records emitted messages."""

    def __init__(self) -> None:
        self.events: list[ProgressEvent] = []

    def emit(self, event: ProgressEvent) -> None:
        self.events.append(event)

    @property
    def messages(self) -> list[str]:
        return [e.message for e in self.events]


def test_recording_sink_conforms_to_progress_sink_protocol() -> None:
    """The test double honors the real (runtime-checkable) ProgressSink seam.

    Guards against drift: if ``ProgressSink.emit`` ever changes shape, the
    status-emission assertions below would silently exercise a non-conforming
    double otherwise.
    """
    assert isinstance(_RecordingSink(), ProgressSink)


def _client() -> MagicMock:
    client = MagicMock()
    client.chat = MagicMock()
    client.notes = MagicMock()
    return client


# ---------------------------------------------------------------------------
# validate_ask_flags — the --new / --conversation-id mutual exclusion
# ---------------------------------------------------------------------------


def test_validate_ask_flags_allows_each_alone() -> None:
    # Neither, only --new, only --conversation-id: all permitted (no raise).
    validate_ask_flags(new_conversation=False, conversation_id=None)
    validate_ask_flags(new_conversation=True, conversation_id=None)
    validate_ask_flags(new_conversation=False, conversation_id="conv_1")


def test_validate_ask_flags_rejects_new_with_conversation_id() -> None:
    with pytest.raises(ValidationError) as exc:
        validate_ask_flags(new_conversation=True, conversation_id="conv_1")
    assert "--new" in str(exc.value)
    assert "--conversation-id" in str(exc.value)


# ---------------------------------------------------------------------------
# determine_conversation_id — the selection ladder + lazy short-circuits
# ---------------------------------------------------------------------------


def test_determine_conversation_explicit_id_wins_without_reading_cache() -> None:
    """An explicit --conversation-id passes through and reads NEITHER cache helper."""
    cached_nb = MagicMock(return_value="nb_other")
    cached_conv = MagicMock(return_value="conv_cached")
    result = determine_conversation_id(
        explicit_conversation_id="conv_explicit",
        explicit_notebook_id="nb_1",
        resolved_notebook_id="nb_1",
        cached_notebook_id=cached_nb,
        cached_conversation_id=cached_conv,
    )
    assert result == "conv_explicit"
    cached_nb.assert_not_called()
    cached_conv.assert_not_called()


def test_determine_conversation_notebook_switch_starts_fresh() -> None:
    """An explicit --notebook differing from the cached one starts a fresh conversation."""
    sink = _RecordingSink()
    result = determine_conversation_id(
        explicit_conversation_id=None,
        explicit_notebook_id="nb_new",
        resolved_notebook_id="nb_new",
        cached_notebook_id=lambda: "nb_old",
        cached_conversation_id=lambda: "conv_cached",
        progress=sink,
    )
    assert result is None
    assert any("new conversation" in m for m in sink.messages)


def test_determine_conversation_falls_back_to_cached() -> None:
    """No explicit id and no notebook switch → the cached conversation id."""
    result = determine_conversation_id(
        explicit_conversation_id=None,
        explicit_notebook_id=None,
        resolved_notebook_id="nb_1",
        cached_notebook_id=lambda: "nb_1",
        cached_conversation_id=lambda: "conv_cached",
    )
    assert result == "conv_cached"


def test_determine_conversation_same_notebook_does_not_start_fresh() -> None:
    """An explicit notebook equal to the cached one continues the cached conversation."""
    result = determine_conversation_id(
        explicit_conversation_id=None,
        explicit_notebook_id="nb_1",
        resolved_notebook_id="nb_1",
        cached_notebook_id=lambda: "nb_1",
        cached_conversation_id=lambda: "conv_cached",
    )
    assert result == "conv_cached"


def test_determine_conversation_explicit_notebook_but_no_cached_falls_through() -> None:
    """An explicit --notebook on first run (no cached notebook) does NOT start fresh.

    The ``and`` short-circuit on a falsy cached notebook falls straight through to
    the cached conversation id, so no "new conversation" status is emitted.
    """
    sink = _RecordingSink()
    result = determine_conversation_id(
        explicit_conversation_id=None,
        explicit_notebook_id="nb_new",
        resolved_notebook_id="nb_new",
        cached_notebook_id=lambda: None,
        cached_conversation_id=lambda: "conv_cached",
        progress=sink,
    )
    assert result == "conv_cached"
    assert sink.messages == []


def test_determine_conversation_no_explicit_notebook_skips_notebook_read() -> None:
    """Without an explicit --notebook the cached-notebook helper is never read."""
    cached_nb = MagicMock(return_value="nb_old")
    result = determine_conversation_id(
        explicit_conversation_id=None,
        explicit_notebook_id=None,
        resolved_notebook_id="nb_1",
        cached_notebook_id=cached_nb,
        cached_conversation_id=lambda: "conv_cached",
    )
    assert result == "conv_cached"
    cached_nb.assert_not_called()


# ---------------------------------------------------------------------------
# get_latest_conversation_from_server — resumed vs. unavailable
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_latest_conversation_returns_server_id() -> None:
    client = _client()
    client.chat.get_conversation_id = AsyncMock(return_value="conv-server-abc")
    sink = _RecordingSink()
    result = await get_latest_conversation_from_server(client, "nb_1", progress=sink)
    assert result == "conv-server-abc"
    assert any("Continuing conversation" in m for m in sink.messages)


@pytest.mark.asyncio
async def test_get_latest_conversation_none_when_server_empty() -> None:
    client = _client()
    client.chat.get_conversation_id = AsyncMock(return_value=None)
    result = await get_latest_conversation_from_server(client, "nb_1")
    assert result is None


@pytest.mark.asyncio
async def test_get_latest_conversation_swallows_fetch_error() -> None:
    """A server-fetch failure is folded into a neutral status line, not re-raised."""
    client = _client()
    client.chat.get_conversation_id = AsyncMock(side_effect=RuntimeError("boom"))
    sink = _RecordingSink()
    result = await get_latest_conversation_from_server(client, "nb_1", progress=sink)
    assert result is None
    assert any("history unavailable" in m for m in sink.messages)


# ---------------------------------------------------------------------------
# execute_configure — mode vs. persona projection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_configure_mode_short_circuits_to_set_mode() -> None:
    client = _client()
    client.chat.set_mode = AsyncMock(return_value=None)
    client.chat.configure = AsyncMock(return_value=None)

    result = await execute_configure(
        client,
        "nb_123",
        chat_mode="learning-guide",
        persona=None,
        response_length=None,
    )

    assert isinstance(result, ConfigureResult)
    assert result.notebook_id == "nb_123"
    assert result.mode == "learning-guide"
    assert result.goal_name is None
    assert result.persona is None
    assert result.response_length is None
    client.chat.set_mode.assert_awaited_once_with("nb_123", ChatMode.LEARNING_GUIDE)
    client.chat.configure.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(("persona", "response_length"), [("tutor", None), (None, "longer")])
async def test_execute_configure_mode_with_custom_field_rejected(persona, response_length) -> None:
    """A preset cannot be combined with a custom persona/response_length (no RPC)."""
    client = _client()
    client.chat.set_mode = AsyncMock(return_value=None)
    client.chat.configure = AsyncMock(return_value=None)

    with pytest.raises(ValidationError, match="chat_mode preset"):
        await execute_configure(
            client,
            "nb_123",
            chat_mode="detailed",
            persona=persona,
            response_length=response_length,
        )
    client.chat.set_mode.assert_not_called()
    client.chat.configure.assert_not_called()


@pytest.mark.asyncio
async def test_execute_configure_mode_with_empty_persona_ok() -> None:
    """An empty persona ("") is a no-op, so it does not block a preset."""
    client = _client()
    client.chat.set_mode = AsyncMock(return_value=None)
    client.chat.configure = AsyncMock(return_value=None)

    result = await execute_configure(
        client, "nb_123", chat_mode="concise", persona="", response_length=None
    )
    assert result.mode == "concise"
    client.chat.set_mode.assert_awaited_once_with("nb_123", ChatMode.CONCISE)
    # The preset short-circuits: the custom configure block must not be written.
    client.chat.configure.assert_not_called()


@pytest.mark.asyncio
async def test_execute_configure_persona_selects_custom_goal() -> None:
    """A persona selects the CUSTOM goal, exposed as the lowercase enum name."""
    client = _client()
    client.chat.configure = AsyncMock(return_value=None)

    result = await execute_configure(
        client,
        "nb_123",
        chat_mode=None,
        persona="Act as a chemistry tutor",
        response_length="longer",
    )

    assert result.mode is None
    assert result.goal_name == "custom"
    assert result.persona == "Act as a chemistry tutor"
    assert result.response_length == "longer"
    client.chat.configure.assert_awaited_once_with(
        "nb_123",
        goal=ChatGoal.CUSTOM,
        response_length=ChatResponseLength.LONGER,
        custom_prompt="Act as a chemistry tutor",
    )


@pytest.mark.asyncio
async def test_execute_configure_no_flags_leaves_goal_and_length_none() -> None:
    client = _client()
    client.chat.configure = AsyncMock(return_value=None)

    result = await execute_configure(
        client,
        "nb_123",
        chat_mode=None,
        persona=None,
        response_length=None,
    )

    assert result.mode is None
    assert result.goal_name is None
    assert result.persona is None
    assert result.response_length is None
    client.chat.configure.assert_awaited_once_with(
        "nb_123", goal=None, response_length=None, custom_prompt=None
    )


@pytest.mark.asyncio
async def test_execute_configure_no_flags_skips_getter() -> None:
    """The bare reset path must NOT read current settings (no round-trip, no merge)."""
    client = _client()
    client.chat.configure = AsyncMock(return_value=None)
    client.chat.get_settings = AsyncMock()

    await execute_configure(client, "nb_123", chat_mode=None, persona=None, response_length=None)

    client.chat.get_settings.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_configure_both_supplied_skips_getter() -> None:
    """When both fields are given the block is complete — a single write, no getter read."""
    client = _client()
    client.chat.configure = AsyncMock(return_value=None)
    client.chat.get_settings = AsyncMock()

    result = await execute_configure(
        client, "nb_123", chat_mode=None, persona="tutor", response_length="longer"
    )

    client.chat.get_settings.assert_not_awaited()
    assert result.goal_name == "custom"
    assert result.persona == "tutor"
    assert result.response_length == "longer"
    client.chat.configure.assert_awaited_once_with(
        "nb_123",
        goal=ChatGoal.CUSTOM,
        response_length=ChatResponseLength.LONGER,
        custom_prompt="tutor",
    )


@pytest.mark.asyncio
async def test_execute_configure_persona_only_preserves_length() -> None:
    """persona-only merges: the omitted response_length is preserved from current (#1751)."""
    client = _client()
    client.chat.configure = AsyncMock(return_value=None)
    client.chat.get_settings = AsyncMock(
        return_value=ChatSettings(
            goal=ChatGoal.DEFAULT,
            response_length=ChatResponseLength.SHORTER,
            custom_prompt=None,
        )
    )

    result = await execute_configure(
        client, "nb_123", chat_mode=None, persona="chemistry tutor", response_length=None
    )

    client.chat.get_settings.assert_awaited_once_with("nb_123")
    # Effective write preserves SHORTER; result reports only the delta (persona).
    client.chat.configure.assert_awaited_once_with(
        "nb_123",
        goal=ChatGoal.CUSTOM,
        response_length=ChatResponseLength.SHORTER,
        custom_prompt="chemistry tutor",
    )
    assert result.goal_name == "custom"
    assert result.persona == "chemistry tutor"
    assert result.response_length is None


@pytest.mark.asyncio
async def test_execute_configure_length_only_preserves_goal_and_prompt() -> None:
    """length-only merges: the omitted CUSTOM goal + persona are preserved from current (#1751)."""
    client = _client()
    client.chat.configure = AsyncMock(return_value=None)
    client.chat.get_settings = AsyncMock(
        return_value=ChatSettings(
            goal=ChatGoal.CUSTOM,
            response_length=ChatResponseLength.DEFAULT,
            custom_prompt="keep this persona",
        )
    )

    result = await execute_configure(
        client, "nb_123", chat_mode=None, persona=None, response_length="longer"
    )

    client.chat.get_settings.assert_awaited_once_with("nb_123")
    client.chat.configure.assert_awaited_once_with(
        "nb_123",
        goal=ChatGoal.CUSTOM,
        response_length=ChatResponseLength.LONGER,
        custom_prompt="keep this persona",
    )
    # Delta reporting: persona/goal_name stay None because THIS call didn't set them.
    assert result.goal_name is None
    assert result.persona is None
    assert result.response_length == "longer"


@pytest.mark.asyncio
async def test_execute_configure_empty_persona_no_length_is_bare_reset() -> None:
    """persona="" with no length is a bare reset (empty persona is a no-op), no getter."""
    client = _client()
    client.chat.configure = AsyncMock(return_value=None)
    client.chat.get_settings = AsyncMock()

    result = await execute_configure(
        client, "nb_123", chat_mode=None, persona="", response_length=None
    )

    client.chat.get_settings.assert_not_awaited()
    assert result.goal_name is None
    client.chat.configure.assert_awaited_once_with(
        "nb_123", goal=None, response_length=None, custom_prompt=None
    )


@pytest.mark.asyncio
async def test_execute_configure_partial_propagates_get_settings_drift() -> None:
    """A get_settings decode-drift on a partial update fails loud — never falls back to clobbering."""
    from notebooklm.exceptions import UnknownRPCMethodError

    client = _client()
    client.chat.configure = AsyncMock(return_value=None)
    client.chat.get_settings = AsyncMock(side_effect=UnknownRPCMethodError("drift"))

    with pytest.raises(UnknownRPCMethodError):
        await execute_configure(
            client, "nb_123", chat_mode=None, persona="tutor", response_length=None
        )

    # The write must NOT happen when the read fails (no silent clobber).
    client.chat.configure.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_configure_whitespace_persona_no_length_is_bare_reset() -> None:
    """A whitespace-only persona is a no-op (repo convention), so alone it's a bare reset."""
    client = _client()
    client.chat.configure = AsyncMock(return_value=None)
    client.chat.get_settings = AsyncMock()

    result = await execute_configure(
        client, "nb_123", chat_mode=None, persona="   ", response_length=None
    )

    client.chat.get_settings.assert_not_awaited()
    assert result.goal_name is None
    client.chat.configure.assert_awaited_once_with(
        "nb_123", goal=None, response_length=None, custom_prompt=None
    )


@pytest.mark.asyncio
async def test_execute_configure_whitespace_persona_with_length_merges() -> None:
    """A whitespace-only persona + a length is a length-only partial write (persona not-supplied)."""
    client = _client()
    client.chat.configure = AsyncMock(return_value=None)
    client.chat.get_settings = AsyncMock(
        return_value=ChatSettings(
            goal=ChatGoal.CUSTOM,
            response_length=ChatResponseLength.DEFAULT,
            custom_prompt="keep me",
        )
    )

    await execute_configure(
        client, "nb_123", chat_mode=None, persona="   ", response_length="longer"
    )

    client.chat.get_settings.assert_awaited_once_with("nb_123")
    client.chat.configure.assert_awaited_once_with(
        "nb_123",
        goal=ChatGoal.CUSTOM,
        response_length=ChatResponseLength.LONGER,
        custom_prompt="keep me",
    )


@pytest.mark.asyncio
async def test_execute_configure_empty_persona_with_length_merges() -> None:
    """persona="" + a length is a length-only partial write (empty persona not-supplied)."""
    client = _client()
    client.chat.configure = AsyncMock(return_value=None)
    client.chat.get_settings = AsyncMock(
        return_value=ChatSettings(
            goal=ChatGoal.LEARNING_GUIDE,
            response_length=ChatResponseLength.DEFAULT,
            custom_prompt=None,
        )
    )

    await execute_configure(client, "nb_123", chat_mode=None, persona="", response_length="shorter")

    client.chat.get_settings.assert_awaited_once_with("nb_123")
    client.chat.configure.assert_awaited_once_with(
        "nb_123",
        goal=ChatGoal.LEARNING_GUIDE,
        response_length=ChatResponseLength.SHORTER,
        custom_prompt=None,
    )


# ---------------------------------------------------------------------------
# save_answer_as_note — the ask --save-as-note secondary action
# ---------------------------------------------------------------------------


def _ask_result(*, answer: str, references: list | None = None) -> AskResult:
    return AskResult(
        answer=answer,
        conversation_id="conv_1",
        turn_number=1,
        is_follow_up=False,
        references=references or [],
        raw_response="",
    )


@pytest.mark.asyncio
async def test_save_answer_no_answer_returns_error_outcome() -> None:
    client = _client()
    outcome = await save_answer_as_note(
        client,
        "nb_1",
        _ask_result(answer=""),
        note_title=None,
        question="Q?",
    )
    assert isinstance(outcome, SaveNoteOutcome)
    assert outcome.note is None
    assert outcome.error == "No answer to save as note"


@pytest.mark.asyncio
async def test_save_answer_citation_rich_uses_chat_save_path() -> None:
    client = _client()
    note = Note(id="note_abc123", notebook_id="nb_1", title="My title", content="A")
    client.chat.save_answer_as_note = AsyncMock(return_value=note)
    result = _ask_result(answer="The answer.", references=[object()])

    outcome = await save_answer_as_note(
        client,
        "nb_1",
        result,
        note_title="My title",
        question="Q?",
    )

    assert outcome.error is None
    assert outcome.plain_text_fallback is False
    assert outcome.note == {"id": "note_abc123", "title": "My title"}
    client.chat.save_answer_as_note.assert_awaited_once_with("nb_1", result, title="My title")
    client.notes.create.assert_not_called()


@pytest.mark.asyncio
async def test_save_answer_without_citations_falls_back_to_plain_text() -> None:
    client = _client()
    note = Note(id="note_def456", notebook_id="nb_1", title="Chat: Q?", content="The answer.")
    client.notes.create = AsyncMock(return_value=note)
    sink = _RecordingSink()

    outcome = await save_answer_as_note(
        client,
        "nb_1",
        _ask_result(answer="The answer.", references=[]),
        note_title=None,
        question="Q?",
        progress=sink,
    )

    assert outcome.plain_text_fallback is True
    assert outcome.note == {"id": "note_def456", "title": "Chat: Q?"}
    client.notes.create.assert_awaited_once()
    assert any("plain-text note" in m for m in sink.messages)


@pytest.mark.asyncio
async def test_save_answer_folds_exception_into_error_outcome() -> None:
    """A save failure is non-fatal: the error is returned, not raised."""
    client = _client()
    client.chat.save_answer_as_note = AsyncMock(side_effect=RuntimeError("save boom"))

    outcome = await save_answer_as_note(
        client,
        "nb_1",
        _ask_result(answer="The answer.", references=[object()]),
        note_title="T",
        question="Q?",
    )

    assert outcome.note is None
    assert outcome.error == "save boom"


# ---------------------------------------------------------------------------
# history: fetch order + content formatters + clear-cache count
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_history_preserves_rpc_order() -> None:
    client = _client()
    client.chat.get_conversation_id = AsyncMock(return_value="conv_1")
    client.chat.get_history = AsyncMock(return_value=[("Q1", "A1"), ("Q2", "A2")])

    fetch = await fetch_history(client, "nb_1", limit=10)

    assert isinstance(fetch, HistoryFetch)
    assert fetch.conversation_id == "conv_1"
    assert fetch.qa_pairs == [("Q1", "A1"), ("Q2", "A2")]
    client.chat.get_conversation_id.assert_awaited_once_with("nb_1")
    client.chat.get_history.assert_awaited_once_with("nb_1", limit=10, conversation_id="conv_1")


def test_format_single_qa_renders_both_parts() -> None:
    out = format_single_qa("What is X?", "X is Y.")
    assert "**Q:** What is X?" in out
    assert "**A:** X is Y." in out


def test_format_single_qa_omits_missing_parts() -> None:
    assert format_single_qa("", "Only an answer.") == "**A:** Only an answer."
    assert format_single_qa("Only a question.", "") == "**Q:** Only a question."


def test_format_history_numbers_turns_and_separates() -> None:
    out = format_history([("Q1", "A1"), ("Q2", "A2")])
    assert "### Turn 1" in out
    assert "### Turn 2" in out
    assert "\n\n---\n\n" in out


def test_execute_clear_cache_captures_pre_clear_count() -> None:
    client = _client()
    client.chat.cache_size = MagicMock(return_value=3)
    client.chat.clear_cache = MagicMock(return_value=True)

    result = execute_clear_cache(client)

    assert isinstance(result, ClearCacheResult)
    assert result.cleared is True
    assert result.count == 3


def test_execute_clear_cache_reports_zero_when_nothing_cleared() -> None:
    client = _client()
    client.chat.cache_size = MagicMock(return_value=0)
    client.chat.clear_cache = MagicMock(return_value=False)

    result = execute_clear_cache(client)

    assert result.cleared is False
    assert result.count == 0
