"""Unit tests for ``ChatAPI.save_answer_as_note`` (Phase 6, issue #660).

Saved-chat ownership moved from the former ``NotesAPI.create_from_chat``
forwarder to ``ChatAPI.save_answer_as_note`` in refactor-history.md Step 8 /
ADR-0013; the forwarder was removed in v0.7.0 and the encoder +
title-derivation semantics now live here.

These tests pin:

* the empty-references guard (raises ``ValueError``, mirroring the
  original saved-from-chat contract),
* the default-title derivation (derived from ``ask_result.answer`` —
  ``AskResult`` has no ``question`` field today),
* the explicit-title override path,
* the 7-element CREATE_NOTE params payload + ``[2]`` mode flag,
* the malformed-response failure mode.

Wave 8 of the session-decoupling plan (ADR-0014 Rule 2 Corollary): the
chat-local ``ChatRuntime`` Protocol was deleted; ``ChatAPI`` takes its
four direct collaborators (RpcCaller, RuntimeTransport, ReqidCounter,
LoopGuard) by keyword argument. ``save_answer_as_note`` only touches
the ``rpc`` collaborator, so the other three are mocked without specs.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from notebooklm._chat import ChatAPI
from notebooklm._runtime.contracts import RpcCaller
from notebooklm.rpc import RPCMethod
from notebooklm.types import AskResult, ChatReference


@pytest.fixture
def mock_rpc() -> MagicMock:
    """Narrow ``RpcCaller`` fake — the only collaborator this surface uses.

    ``save_answer_as_note`` only calls ``rpc.rpc_call`` — no transport /
    reqid / loop-guard surface is exercised. The ``AsyncMock`` is wired
    into the ``MagicMock(spec=...)`` via its constructor so the ADR-0007
    meta-lint stays clean (no post-hoc attribute assignment).
    """
    return MagicMock(spec=RpcCaller, rpc_call=AsyncMock())


@pytest.fixture
def chat_api(mock_rpc: MagicMock) -> ChatAPI:
    """A ``ChatAPI`` instance wired with narrow collaborator fakes.

    A ``MagicMock(get_source_ids=AsyncMock(...))`` notebooks resolver
    is injected so the ``NotebooksAPI`` fallback in
    ``ChatAPI.__init__`` does not try to wrap ``rpc``; the
    ``NotebookSourceIdProvider`` protocol surface is small enough that
    a ``MagicMock`` with a single async stub satisfies it without
    falling into ADR-0007's forbidden-attribute-assignment lint
    (the stub is passed via constructor injection).
    """
    notebooks = MagicMock(get_source_ids=AsyncMock(return_value=[]))
    return ChatAPI(
        rpc=mock_rpc,
        transport=MagicMock(),
        reqid=MagicMock(),
        loop_guard=MagicMock(),
        notebooks=notebooks,
    )


def _make_ask_result(
    answer: str = "One fruit mentioned is apples [1].",
    n_refs: int = 1,
) -> AskResult:
    refs = [
        ChatReference(
            source_id=f"src-{i}",
            citation_number=i + 1,
            cited_text=f"passage {i}",
            start_char=0,
            end_char=9,
            chunk_id=f"chunk-{i}",
        )
        for i in range(n_refs)
    ]
    return AskResult(
        answer=answer,
        conversation_id="conv-1",
        turn_number=1,
        is_follow_up=False,
        references=refs,
        raw_response="",
    )


class TestSaveAnswerAsNote:
    """Behavioural tests for ``ChatAPI.save_answer_as_note``."""

    @pytest.mark.asyncio
    async def test_empty_references_raises_value_error(self, chat_api: ChatAPI) -> None:
        ask_result = _make_ask_result(n_refs=0)
        with pytest.raises(ValueError, match="non-empty"):
            await chat_api.save_answer_as_note("nb-1", ask_result)

    @pytest.mark.asyncio
    async def test_default_title_derives_from_answer_not_question(
        self, chat_api: ChatAPI, mock_rpc: MagicMock
    ) -> None:
        # Wrapped response shape — slot [0] is a list whose first
        # element is the note_id.
        mock_rpc.rpc_call.return_value = [
            [
                "note-new-id",
                "One fruit mentioned is apples [1].",
                [2, "user", [123, 456]],
                [[]],
                "ServerTitle",
                [],
            ]
        ]
        ask_result = _make_ask_result()
        note = await chat_api.save_answer_as_note("nb-1", ask_result)
        # Server-returned title is what surfaces in the Note.
        assert note.title == "ServerTitle"
        # The RPC call received our derived default title (from .answer).
        sent_title = mock_rpc.rpc_call.call_args[0][1][4]
        assert sent_title.startswith("Chat: ")
        # Derivation truncates the answer to 50 chars — confirm the
        # source field is .answer, not anything question-derived.
        assert "fruit" in sent_title or "apples" in sent_title

    @pytest.mark.asyncio
    async def test_explicit_title_overrides_default(
        self, chat_api: ChatAPI, mock_rpc: MagicMock
    ) -> None:
        mock_rpc.rpc_call.return_value = [
            "note-new-id",
            "answer",
            [2, "u", [1, 2]],
            [[]],
            "My Title",  # server echoes the title back
            [],
        ]
        ask_result = _make_ask_result()
        note = await chat_api.save_answer_as_note("nb-1", ask_result, title="My Title")
        assert mock_rpc.rpc_call.call_args[0][1][4] == "My Title"
        assert note.title == "My Title"

    @pytest.mark.asyncio
    async def test_uses_create_note_rpc_with_mode_flag_2(
        self, chat_api: ChatAPI, mock_rpc: MagicMock
    ) -> None:
        mock_rpc.rpc_call.return_value = [
            "note-id",
            "x",
            [2, "u", [1, 2]],
            [[]],
            "T",
            [],
        ]
        ask_result = _make_ask_result()
        await chat_api.save_answer_as_note("nb-1", ask_result, title="T")
        call_args = mock_rpc.rpc_call.call_args
        assert call_args[0][0] == RPCMethod.CREATE_NOTE
        params = call_args[0][1]
        # 7-element params with [2] mode flag at slot 2 (vs [1] for blank-note variant)
        assert len(params) == 7
        assert params[2] == [2]
        assert params[6] == [2]
        # Only ONE RPC call — no follow-up UPDATE_NOTE.
        assert mock_rpc.rpc_call.call_count == 1

    @pytest.mark.asyncio
    async def test_missing_note_id_in_response_raises(
        self, chat_api: ChatAPI, mock_rpc: MagicMock
    ) -> None:
        # Malformed response (note_id slot is None / not a str) must
        # surface a clear ``RuntimeError`` rather than returning a Note
        # with id="".
        mock_rpc.rpc_call.return_value = [None, "x", [], [], "T", []]
        ask_result = _make_ask_result()
        with pytest.raises(RuntimeError, match="no note ID"):
            await chat_api.save_answer_as_note("nb-1", ask_result, title="T")

    @pytest.mark.asyncio
    async def test_returned_note_carries_answer_with_markers(
        self, chat_api: ChatAPI, mock_rpc: MagicMock
    ) -> None:
        """``Note.content`` is the answer text WITH ``[N]`` markers.

        Rich citation anchors live server-side and surface via the
        NotebookLM UI; the dataclass only carries the raw answer.
        """
        mock_rpc.rpc_call.return_value = [
            "note-id",
            "ignored-server-content",
            [2, "u", [1, 2]],
            [[]],
            "ServerTitle",
            [],
        ]
        ask_result = _make_ask_result(answer="The answer is X [1].")
        note = await chat_api.save_answer_as_note("nb-1", ask_result)
        assert note.content == "The answer is X [1]."
        assert note.notebook_id == "nb-1"
