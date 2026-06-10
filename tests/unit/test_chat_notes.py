"""Unit tests for the ``_chat.notes`` module (Phase 6, ADR-0013).

The encoder is tested separately in
``tests/unit/test_save_chat_as_note_encoder.py``. The tests here pin
the behaviour of ``save_chat_answer_as_note`` itself — the thin RPC
wrapper that builds params, dispatches via the injected
:class:`SaveChatNoteRpc`, parses the response, and surfaces the
constructed :class:`Note`.

The Phase 6 split made this function reachable independently of any
``ChatAPI`` instance, which is why we test it here rather than
folding everything into ``test_chat_save_answer_as_note.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from notebooklm._chat.notes import save_chat_answer_as_note
from notebooklm.rpc import RPCMethod
from notebooklm.types import ChatReference
from tests._fixtures.fake_core import FakeSession, make_fake_core


def _make_refs(n: int = 1) -> list[ChatReference]:
    return [
        ChatReference(
            source_id=f"src-{i}",
            citation_number=i + 1,
            cited_text=f"passage {i}",
            start_char=0,
            end_char=9,
            chunk_id=f"chunk-{i}",
        )
        for i in range(n)
    ]


@pytest.fixture
def rpc() -> FakeSession:
    """ADR-0007 substrate: ``FakeSession`` with ``rpc_call`` injected.

    ``save_chat_answer_as_note`` only consumes the ``rpc_call`` method
    from its :class:`SaveChatNoteRpc` protocol surface, so the
    ``FakeSession`` from ``make_fake_core`` satisfies the seam
    structurally.
    """
    return make_fake_core(rpc_call=AsyncMock())


class TestSaveChatAnswerAsNote:
    @pytest.mark.asyncio
    async def test_dispatches_create_note_with_seven_element_params(self, rpc: FakeSession) -> None:
        rpc.rpc_call.return_value = [["note-id", "x", [2, "u", [1, 2]], [[]], "ServerTitle", []]]
        await save_chat_answer_as_note(rpc, "nb-1", "X [1].", _make_refs(), "Title")
        rpc.rpc_call.assert_awaited_once()
        method, params = rpc.rpc_call.call_args[0][0], rpc.rpc_call.call_args[0][1]
        assert method == RPCMethod.CREATE_NOTE
        assert len(params) == 7
        assert params[0] == "nb-1"
        assert params[1] == "X [1]."
        assert params[2] == [2]
        assert params[4] == "Title"
        assert params[6] == [2]
        assert rpc.rpc_call.call_args.kwargs["source_path"] == "/notebook/nb-1"
        assert rpc.rpc_call.call_args.kwargs["operation_variant"] == "saved_from_chat"

    @pytest.mark.asyncio
    async def test_parses_wrapped_response_shape(self, rpc: FakeSession) -> None:
        rpc.rpc_call.return_value = [["note-abc", "ignored", [2], [[]], "ServerTitle", []]]
        note = await save_chat_answer_as_note(rpc, "nb-1", "answer [1].", _make_refs(), "Requested")
        assert note.id == "note-abc"
        assert note.title == "ServerTitle"
        assert note.notebook_id == "nb-1"
        assert note.content == "answer [1]."  # content is the answer text passed in

    @pytest.mark.asyncio
    async def test_parses_flat_response_shape(self, rpc: FakeSession) -> None:
        # Flat shape: top-level list starts with a string note_id.
        rpc.rpc_call.return_value = [
            "note-flat",
            "ignored",
            [2],
            [[]],
            "FlatServerTitle",
            [],
        ]
        note = await save_chat_answer_as_note(rpc, "nb-1", "answer [1].", _make_refs(), "Requested")
        assert note.id == "note-flat"
        assert note.title == "FlatServerTitle"

    @pytest.mark.asyncio
    async def test_falls_back_to_requested_title_when_server_omits_one(
        self, rpc: FakeSession
    ) -> None:
        # Note row has fewer than 5 slots — server title slot is absent.
        rpc.rpc_call.return_value = [["note-id", "x", [2]]]
        note = await save_chat_answer_as_note(rpc, "nb-1", "answer [1].", _make_refs(), "Requested")
        assert note.title == "Requested"

    @pytest.mark.asyncio
    async def test_missing_note_id_raises_runtime_error(self, rpc: FakeSession) -> None:
        rpc.rpc_call.return_value = [None, "x", [], [], "T", []]
        with pytest.raises(RuntimeError, match="no note ID"):
            await save_chat_answer_as_note(rpc, "nb-1", "answer [1].", _make_refs(), "T")

    @pytest.mark.asyncio
    async def test_empty_references_raises_value_error(self, rpc: FakeSession) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            await save_chat_answer_as_note(rpc, "nb-1", "answer", [], "T")
        # The encoder rejects the call before any RPC dispatch happens.
        rpc.rpc_call.assert_not_called()
