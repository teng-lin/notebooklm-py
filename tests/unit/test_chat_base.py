"""Transport-neutral contract tests for the chat base class."""

from __future__ import annotations

import inspect
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest

from notebooklm._chat.api import ChatAPI, _PostedAsk
from notebooklm._types.documents import StructuredDocument
from notebooklm._types.enums import ChatGoal, ChatResponseLength
from notebooklm._web.chat import WebChatAPI
from notebooklm.types import (
    ChatReference,
    ChatSettings,
    ConversationTurn,
    Note,
)


class _FakeChatAPI(ChatAPI):
    """Minimal backend proving shared workflows need only their declared seams."""

    def __init__(
        self,
        *,
        conversation_ids: list[str | None] | None = None,
        role_snapshots: list[list[object]] | None = None,
    ) -> None:
        self.events: list[tuple[str, object]] = []
        self._conversation_ids = list(conversation_ids or [])
        self._role_snapshots = list(role_snapshots or [])
        self._source_ids = ["source-1"]
        loop_guard = SimpleNamespace(assert_bound_loop=self._assert_loop)
        notebooks = SimpleNamespace(get_source_ids=self._get_source_ids)
        super().__init__(loop_guard=loop_guard, notebooks=notebooks)

    def _assert_loop(self) -> None:
        self.events.append(("loop", None))

    async def _get_source_ids(self, notebook_id: str) -> list[str]:
        self.events.append(("sources", notebook_id))
        return self._source_ids

    async def _stream_answer(
        self,
        *,
        notebook_id: str,
        question: str,
        source_ids: list[str],
        cached_turns: list[ConversationTurn],
        conversation_id: str | None,
    ) -> _PostedAsk:
        self.events.append(
            (
                "stream",
                (notebook_id, question, source_ids, cached_turns, conversation_id),
            )
        )
        return _PostedAsk(
            answer="Neutral answer [1]",
            references=[ChatReference(source_id="source-1", citation_number=1, chunk_id="chunk-1")],
            conversation_id=conversation_id,
            raw_response="raw stream",
            answer_document=StructuredDocument(),
            turn_key=None,
            next_steps=[],
        )

    async def _list_turn_roles(
        self,
        notebook_id: str,
        conversation_id: str,
        limit: int,
    ) -> list[object]:
        self.events.append(("roles", (notebook_id, conversation_id, limit)))
        return self._role_snapshots.pop(0)

    async def _send_delete_conversation(
        self,
        notebook_id: str,
        conversation_id: str,
    ) -> None:
        self.events.append(("delete", (notebook_id, conversation_id)))

    async def _send_note(
        self,
        *,
        notebook_id: str,
        answer_text: str,
        references: list[ChatReference],
        title: str,
        clean_answer: str,
        citation_anchors: list[tuple[ChatReference, int]],
    ) -> Note:
        self.events.append(
            (
                "note",
                (
                    notebook_id,
                    answer_text,
                    references,
                    title,
                    clean_answer,
                    citation_anchors,
                ),
            )
        )
        return Note(
            id="note-1",
            notebook_id=notebook_id,
            title=title,
            content=answer_text,
            created_at=datetime(2026, 1, 1),
        )

    async def get_conversation_turns(
        self,
        notebook_id: str,
        conversation_id: str,
        limit: int = 2,
    ) -> Any:
        raise NotImplementedError

    async def get_conversation_id(self, notebook_id: str) -> str | None:
        self.events.append(("conversation_id", notebook_id))
        return self._conversation_ids.pop(0)

    async def get_history(
        self,
        notebook_id: str,
        limit: int = 100,
        conversation_id: str | None = None,
    ) -> list[tuple[str, str]]:
        raise NotImplementedError

    async def configure(
        self,
        notebook_id: str,
        goal: ChatGoal | None = None,
        response_length: ChatResponseLength | None = None,
        custom_prompt: str | None = None,
    ) -> None:
        raise NotImplementedError

    async def get_settings(self, notebook_id: str) -> ChatSettings:
        raise NotImplementedError


@pytest.mark.asyncio
async def test_ask_orchestration_recovers_id_and_caches_without_web_types() -> None:
    api = _FakeChatAPI(conversation_ids=[None, "conversation-1"])

    result = await api.ask("notebook-1", "Question?")

    assert result.conversation_id == "conversation-1"
    assert result.turn_number == 1
    assert result.is_follow_up is False
    assert api.get_cached_turns("conversation-1") == [
        ConversationTurn(query="Question?", answer="Neutral answer [1]", turn_number=1)
    ]
    assert api.events == [
        ("loop", None),
        ("sources", "notebook-1"),
        ("conversation_id", "notebook-1"),
        (
            "stream",
            ("notebook-1", "Question?", ["source-1"], [], None),
        ),
        ("conversation_id", "notebook-1"),
    ]


@pytest.mark.asyncio
async def test_ask_counts_complete_role_snapshot_before_single_stream_hook() -> None:
    api = _FakeChatAPI(role_snapshots=[[1] * 100, [2, 1, 2, 1]])
    api._cache.cache_conversation_turn("conversation-1", "Cached?", "Cached.", 1)

    result = await api.ask(
        "notebook-1",
        "Follow-up?",
        source_ids=["source-explicit"],
        conversation_id="conversation-1",
    )

    assert result.turn_number == 3
    assert result.is_follow_up is True
    assert [event[0] for event in api.events] == ["loop", "roles", "roles", "stream"]
    assert api.events[1:] == [
        ("roles", ("notebook-1", "conversation-1", 100)),
        ("roles", ("notebook-1", "conversation-1", 200)),
        (
            "stream",
            (
                "notebook-1",
                "Follow-up?",
                ["source-explicit"],
                [ConversationTurn(query="Cached?", answer="Cached.", turn_number=1)],
                "conversation-1",
            ),
        ),
    ]


@pytest.mark.asyncio
async def test_delete_and_note_workflows_prepare_state_around_one_hook_each() -> None:
    api = _FakeChatAPI()
    api._cache.cache_conversation_turn("conversation-1", "Q?", "A.", 1)
    reference = ChatReference(source_id="source-1", citation_number=1, chunk_id="chunk-1")

    await api.delete_conversation("notebook-1", "conversation-1")
    note = await api.save_answer_as_note(
        "notebook-1",
        SimpleNamespace(answer="Answer [1]", references=[reference]),
    )

    assert api.get_cached_turns("conversation-1") == []
    assert note.id == "note-1"
    assert api.events == [
        ("loop", None),
        ("delete", ("notebook-1", "conversation-1")),
        (
            "note",
            (
                "notebook-1",
                "Answer [1]",
                [reference],
                "Chat: Answer [1]",
                "Answer",
                [(reference, 6)],
            ),
        ),
    ]


@pytest.mark.parametrize(
    "method_name",
    [
        "get_conversation_turns",
        "get_conversation_id",
        "get_history",
        "configure",
        "get_settings",
    ],
)
def test_web_public_override_signatures_match_base(method_name: str) -> None:
    assert inspect.signature(getattr(WebChatAPI, method_name)) == inspect.signature(
        getattr(ChatAPI, method_name)
    )
