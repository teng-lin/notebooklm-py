"""Transport-neutral contract tests for the chat base class."""

from __future__ import annotations

import hashlib
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

# Raw ``__doc__`` fingerprints from the pre-split ChatAPI at de56890b. These
# cover the effective runtime owner of every public method: shared workflows on
# the neutral base and Web overrides for the abstract reads/configuration.
_CHAT_DOCSTRING_SHA256 = {
    ("ChatAPI", "<class>"): "125617c3294301ee5987a2e926fbc90f6ad6a4bbee91b0944c41a0c8fe46536d",
    ("ChatAPI", "__init__"): "ae8476b2aa8120b15ea62eafd7efe67ea84e9a21175d212db6fb709b0176a2ea",
    (
        "ChatAPI",
        "reset_after_open",
    ): "561cee4e0504ee3cbcb413a4f34879d1a34fd5d096e9a08b0b5208d3943001b4",
    ("ChatAPI", "ask"): "704583ce2f9eaf1b303ec68e196fbb7f07073636f5d25538de65e58240426907",
    (
        "ChatAPI",
        "get_cached_turns",
    ): "3cc05a18496555e0974363f13a275b638cb791aa31f2cd0ebde2751299d88993",
    (
        "ChatAPI",
        "delete_conversation",
    ): "a8d79dc920ff192411b105d70d3e2a27ec32fed94d793aba859535a7233fe9fb",
    (
        "ChatAPI",
        "clear_cache",
    ): "841e1bc228c61c0d08585966ccdcfe6c14fba1d7bcdfb4783c71fa5b35995776",
    (
        "ChatAPI",
        "cache_size",
    ): "d59415c6b3b9a129248d8dadf1f2eac518d775d6c0d1ae333460dc1d407b9e0b",
    (
        "ChatAPI",
        "set_mode",
    ): "7df6a33d66d207e57d9961f3fd733b079d3b88701bbe9bead3552b8495404e01",
    (
        "ChatAPI",
        "save_answer_as_note",
    ): "f78942e68ea2346928926a470ad1938e3a3f80fd36992cfccfb5e98d97ca2bc7",
    (
        "WebChatAPI",
        "<class>",
    ): "125617c3294301ee5987a2e926fbc90f6ad6a4bbee91b0944c41a0c8fe46536d",
    (
        "WebChatAPI",
        "__init__",
    ): "ae8476b2aa8120b15ea62eafd7efe67ea84e9a21175d212db6fb709b0176a2ea",
    (
        "WebChatAPI",
        "get_conversation_turns",
    ): "9ec5273e41fd0e0ea089eb78484011d268995601ce56779b9bd94a387f586a29",
    (
        "WebChatAPI",
        "get_conversation_id",
    ): "c5feb719c00d9c81edd1347a742de4c0b62d9e8ec2fb8e5557558b5d778712bd",
    (
        "WebChatAPI",
        "get_history",
    ): "4a683b206539a327bf2295e6413452115d9b74b972f8df02f97dc6e0409a7ead",
    (
        "WebChatAPI",
        "configure",
    ): "7a3547e5a56a08283b0572b8290b0d67e4a8d79a6b4de5eac9f560849eb319ab",
    (
        "WebChatAPI",
        "get_settings",
    ): "62723894d239d33ddf0384c496909da70c0e1740fcb32706127aab5443bfbf99",
}


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


@pytest.mark.parametrize(
    ("owner_name", "member_name", "expected_sha256"),
    [(*key, digest) for key, digest in _CHAT_DOCSTRING_SHA256.items()],
)
def test_chat_split_preserves_effective_runtime_docstrings(
    owner_name: str,
    member_name: str,
    expected_sha256: str,
) -> None:
    """Public runtime documentation stays byte-identical across the split."""
    owner = {"ChatAPI": ChatAPI, "WebChatAPI": WebChatAPI}[owner_name]
    documented = owner if member_name == "<class>" else getattr(owner, member_name)
    assert documented.__doc__ is not None
    assert hashlib.sha256(documented.__doc__.encode()).hexdigest() == expected_sha256
