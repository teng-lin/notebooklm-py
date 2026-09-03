"""Transport-neutral contract tests for the chat base class."""

from __future__ import annotations

import hashlib
import inspect
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest

from notebooklm._chat import ChatAPI, _ChatSettingsRead, _PostedAsk, _TurnRoleSnapshot
from notebooklm._types.documents import StructuredDocument
from notebooklm._types.enums import ChatGoal, ChatResponseLength
from notebooklm._web.chat import WebChatAPI
from notebooklm.exceptions import ValidationError
from notebooklm.types import (
    ChatReference,
    ChatSessionStatus,
    ChatSettings,
    ConversationTurn,
    Note,
)

# Normalized docstring fingerprints from the pre-split ChatAPI at de56890b.
# ``inspect.getdoc`` keeps these stable across CPython's 3.13 docstring
# indentation change while still detecting documentation drift. These cover the
# effective runtime owner of every public method: shared workflows on the
# neutral base and Web overrides for the remaining abstract reads.
_CHAT_DOCSTRING_SHA256 = {
    ("ChatAPI", "<class>"): "3cb0c1f0686423392457277486d386eeef8f99865e8b45ba754bc96ec75cb804",
    ("ChatAPI", "__init__"): "a48df90f2e18ac8d5591f96686b829897edc6b9ecfdb3901ef56905de4966e23",
    (
        "ChatAPI",
        "reset_after_open",
    ): "642dc585ace0fe8dfead8faedc91298016b11807b8c88fa702c99042791ca5d7",
    ("ChatAPI", "ask"): "13f46f7053e70d68a386a8910cc5323d06c7f027b42b9fc6daa4c28696cb39d7",
    (
        "ChatAPI",
        "get_cached_turns",
    ): "db1dc0d85a854422c9840010023190134778f837ac00580f0cf0d9b2259b3a56",
    (
        "ChatAPI",
        "delete_conversation",
    ): "01a50a49944455d889f446d82810e50c733fea9964d144792484ce3c6f32e12c",
    (
        "ChatAPI",
        "clear_cache",
    ): "d4237ff0c442c2acc5deac4ab18c6fa778bdf60a4d0ec754b20f88ae95b5c6f2",
    (
        "ChatAPI",
        "cache_size",
    ): "793175e56b14d374b9177878f327ae201f86661c8f6f534e7fa528bea0d332bf",
    (
        "ChatAPI",
        "set_mode",
    ): "822abb5683d83ab0b02df825156f243b8ecb2ce4ab54a41167ab5526ede72fdf",
    (
        "ChatAPI",
        "save_answer_as_note",
    ): "a637cc7f1e583d027397d3db062e2e91c7607bc3bc6b06b9c1a86686d423326a",
    (
        "WebChatAPI",
        "<class>",
    ): "3cb0c1f0686423392457277486d386eeef8f99865e8b45ba754bc96ec75cb804",
    (
        "WebChatAPI",
        "__init__",
    ): "a48df90f2e18ac8d5591f96686b829897edc6b9ecfdb3901ef56905de4966e23",
    (
        "WebChatAPI",
        "get_conversation_turns",
    ): "318935a9bdd9dc412e63536cf7d4119e41d09aa49ab5f379e5c4aa8420c64feb",
    (
        "WebChatAPI",
        "get_conversation_id",
    ): "2b6a77deded362f6fa4198b4b4a39b76ae3dd835ff7d97fec8055c6f89b11dc8",
    (
        "WebChatAPI",
        "get_history",
    ): "05fc7448600335aa9c7f548b2e12723c3836f1f75ad8ca04c86f25008bdb7526",
    (
        "ChatAPI",
        "configure",
    ): "18fee62a4f952caf863098bbef023655be560d9d2a69c8307ef83fa9fb6e0e01",
    (
        "ChatAPI",
        "get_settings",
    ): "ab537f9191b92e48129cbc1431f32b85ba613bc871175e4fbdcfb89babce3fcc",
}


class _FakeChatAPI(ChatAPI):
    """Minimal backend proving shared workflows need only their declared seams."""

    def __init__(
        self,
        *,
        conversation_ids: list[str | None] | None = None,
        role_snapshots: list[list[object]] | None = None,
        settings: _ChatSettingsRead | None = None,
    ) -> None:
        self.events: list[tuple[str, object]] = []
        self._conversation_ids = list(conversation_ids or [])
        self._role_snapshots = list(role_snapshots or [])
        self._settings = settings or _ChatSettingsRead(
            goal=ChatGoal.DEFAULT,
            response_length=ChatResponseLength.DEFAULT,
            custom_prompt=None,
        )
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
    ) -> _TurnRoleSnapshot:
        self.events.append(("roles", (notebook_id, conversation_id, limit)))
        roles = tuple(self._role_snapshots.pop(0))
        return _TurnRoleSnapshot(roles=roles, exhausted=len(roles) < limit)

    async def _send_delete_conversation(
        self,
        notebook_id: str,
        conversation_id: str,
    ) -> None:
        self.events.append(("delete", (notebook_id, conversation_id)))

    async def _get_session_status(
        self,
        notebook_id: str,
        conversation_id: str,
    ) -> ChatSessionStatus:
        self.events.append(("session_status", (notebook_id, conversation_id)))
        return ChatSessionStatus(generating=False)

    async def _cancel_generation(
        self,
        notebook_id: str,
        conversation_id: str,
    ) -> None:
        self.events.append(("cancel", (notebook_id, conversation_id)))

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

    async def _send_configure(
        self,
        notebook_id: str,
        goal: ChatGoal,
        response_length: ChatResponseLength,
        custom_prompt: str | None,
    ) -> None:
        self.events.append(("configure", (notebook_id, goal, response_length, custom_prompt)))

    async def _read_settings(self, notebook_id: str) -> _ChatSettingsRead:
        self.events.append(("settings", notebook_id))
        return self._settings


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


@pytest.mark.asyncio
async def test_configure_normalizes_once_before_the_typed_send_hook() -> None:
    api = _FakeChatAPI()

    await api.configure("notebook-1")
    await api.configure(
        "notebook-1",
        goal=ChatGoal.LEARNING_GUIDE,
        response_length=ChatResponseLength.LONGER,
        custom_prompt="inactive draft",
    )
    await api.configure(
        "notebook-1",
        goal=ChatGoal.CUSTOM,
        custom_prompt="Be exact.",
    )

    assert api.events == [
        (
            "configure",
            (
                "notebook-1",
                ChatGoal.DEFAULT,
                ChatResponseLength.DEFAULT,
                None,
            ),
        ),
        (
            "configure",
            (
                "notebook-1",
                ChatGoal.LEARNING_GUIDE,
                ChatResponseLength.LONGER,
                None,
            ),
        ),
        (
            "configure",
            (
                "notebook-1",
                ChatGoal.CUSTOM,
                ChatResponseLength.DEFAULT,
                "Be exact.",
            ),
        ),
    ]


@pytest.mark.asyncio
async def test_configure_rejects_missing_custom_prompt_before_send_hook() -> None:
    api = _FakeChatAPI()

    with pytest.raises(
        ValidationError,
        match="custom_prompt is required when goal is CUSTOM",
    ):
        await api.configure("notebook-1", goal=ChatGoal.CUSTOM)

    assert api.events == []


@pytest.mark.asyncio
async def test_get_settings_constructs_public_model_after_one_typed_read() -> None:
    api = _FakeChatAPI(
        settings=_ChatSettingsRead(
            goal=ChatGoal.CUSTOM,
            response_length=ChatResponseLength.SHORTER,
            custom_prompt="Use proofs.",
        )
    )

    assert await api.get_settings("notebook-1") == ChatSettings(
        goal=ChatGoal.CUSTOM,
        response_length=ChatResponseLength.SHORTER,
        custom_prompt="Use proofs.",
    )
    assert api.events == [("settings", "notebook-1")]


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
    """Normalized public runtime documentation stays identical across the split."""
    owner = {"ChatAPI": ChatAPI, "WebChatAPI": WebChatAPI}[owner_name]
    documented = owner if member_name == "<class>" else getattr(owner, member_name)
    doc = inspect.getdoc(documented)
    assert doc is not None
    assert hashlib.sha256(doc.encode()).hexdigest() == expected_sha256
