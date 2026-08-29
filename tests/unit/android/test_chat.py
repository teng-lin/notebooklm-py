"""Direct B5 Android chat adapter and neutral orchestration tests."""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator
from typing import Any, cast

import pytest
from google.protobuf.empty_pb2 import Empty

from notebooklm._android.chat import (
    DELETE_CHAT_TURNS_METHOD,
    GENERATE_FREE_FORM_STREAMED_METHOD,
    GET_PROJECT_METHOD,
    LIST_CHAT_SESSIONS_METHOD,
    LIST_CHAT_TURNS_METHOD,
    MUTATE_PROJECT_METHOD,
    AndroidChatAPI,
)
from notebooklm._android.notes import CREATE_NOTE_METHOD, SAVED_RESPONSE_NOTE_TYPE
from notebooklm._android.proto.google.internal.labs.tailwind.orchestration.v1 import (
    chat_pb2,
    notes_pb2,
    read_pb2,
    sources_pb2,
)
from notebooklm._android.proto.labs.language.tailwind.common.protos import common_pb2
from notebooklm._android.proto.notebooklm.internal.android.wire.v1 import (
    notebooks_pb2 as wire_notebooks_pb2,
)
from notebooklm._android.session import AndroidSession
from notebooklm._chat import ChatAPI
from notebooklm._types.documents import StructuredDocument
from notebooklm._types.enums import ChatGoal, ChatResponseLength
from notebooklm.exceptions import ChatResponseParseError, UnknownRPCMethodError, ValidationError
from notebooklm.types import AskResult, ChatMode, ChatReference, ChatSettings, ConversationTurn


class FakeSession:
    """Recording B5 fake server at the AndroidSession seam."""

    def __init__(self) -> None:
        self.unary_responses: dict[str, list[Any]] = {}
        self.stream_responses: list[list[Any]] = []
        self.unary_calls: list[tuple[str, Any, dict[str, Any]]] = []
        self.stream_calls: list[tuple[str, Any, dict[str, Any]]] = []

    async def unary(self, method: str, request: Any, **kwargs: Any) -> Any:
        self.unary_calls.append((method, request, kwargs))
        return self.unary_responses[method].pop(0)

    async def stream(self, method: str, request: Any, **kwargs: Any) -> AsyncIterator[Any]:
        self.stream_calls.append((method, request, kwargs))
        for response in self.stream_responses.pop(0):
            yield response


class FakeLoopGuard:
    def __init__(self) -> None:
        self.calls = 0

    def assert_bound_loop(self) -> None:
        self.calls += 1


class FakeNotebooks:
    def __init__(self, source_ids: list[str] | None = None) -> None:
        self.source_ids = source_ids or ["source-1"]
        self.calls: list[str] = []

    async def get_source_ids(self, notebook_id: str) -> list[str]:
        self.calls.append(notebook_id)
        return list(self.source_ids)


def _android_session(fake: FakeSession) -> AndroidSession:
    return cast(AndroidSession, fake)


def _api(
    fake: FakeSession,
    *,
    turn_id: str = "00000000-0000-4000-8000-000000000099",
    source_ids: list[str] | None = None,
) -> tuple[AndroidChatAPI, FakeLoopGuard, FakeNotebooks]:
    guard = FakeLoopGuard()
    notebooks = FakeNotebooks(source_ids)
    api = AndroidChatAPI(
        session=_android_session(fake),
        loop_guard=guard,
        notebooks=notebooks,
        turn_id_factory=lambda: turn_id,
    )
    return api, guard, notebooks


def _chat_turn(question: str, answer: str, *, message_id: str, role: int) -> Any:
    return chat_pb2.ChatHistoryMessage(
        message_id=message_id,
        observed_event_type=role,
        user_query_text=question,
        act_on_sources_response=chat_pb2.ActOnSourcesResponse(
            response=chat_pb2.AnswerResponse(response=answer)
        ),
    )


def _document() -> Any:
    return chat_pb2.TailwindDoc(
        body=chat_pb2.Body(
            content=[
                chat_pb2.StructuralElement(
                    start_index=0,
                    end_index=12,
                    paragraph=chat_pb2.Paragraph(
                        elements=[
                            chat_pb2.ParagraphElement(
                                start_index=0,
                                end_index=12,
                                text_run=chat_pb2.TextRun(content="Final answer"),
                            )
                        ]
                    ),
                )
            ],
            inline_object_locations=[
                chat_pb2.AnnotationMapEntry(
                    object_id=chat_pb2.ObjectId(id="chunk-1"),
                    content_range=chat_pb2.Range(start_index=12, end_index=12),
                )
            ],
        ),
        objects=[
            chat_pb2.DocumentObject(object_id=chat_pb2.ObjectId(id="non-citation-object")),
            chat_pb2.DocumentObject(
                object_id=chat_pb2.ObjectId(id="chunk-1"),
                citation=chat_pb2.Citation(
                    fragment=chat_pb2.TailwindDocFragment(
                        elements=[
                            chat_pb2.StructuralElement(
                                start_index=20,
                                end_index=33,
                                paragraph=chat_pb2.Paragraph(
                                    elements=[
                                        chat_pb2.ParagraphElement(
                                            start_index=20,
                                            end_index=33,
                                            text_run=chat_pb2.TextRun(content="Source passage"),
                                        )
                                    ]
                                ),
                            )
                        ]
                    ),
                    source_attribution=chat_pb2.CitationSource(
                        ingested_source=chat_pb2.SourceRevision(
                            source=read_pb2.SourceId(id="source-1")
                        )
                    ),
                    object_id=chat_pb2.ObjectId(id="citation-inner-id"),
                ),
            ),
        ],
    )


def _frame(text: str, *, final: bool) -> Any:
    return chat_pb2.GenerateFreeFormStreamedResponse(
        answer=chat_pb2.AnswerResponse(
            response=text,
            conversation_turn_key=chat_pb2.ConversationTurnKey(
                session_id="conversation-1",
                conversation_id="turn-server-1",
                observed_field_3=17,
            ),
            response_doc=_document() if final else None,
        ),
        is_final_response=final,
    )


def test_android_chat_is_private_concrete_and_inherits_ask_orchestration() -> None:
    assert AndroidChatAPI.__abstractmethods__ == frozenset()
    assert "ask" not in AndroidChatAPI.__dict__
    assert AndroidChatAPI.ask is ChatAPI.ask
    assert (
        inspect.signature(AndroidChatAPI).parameters["session"].default is inspect.Parameter.empty
    )


@pytest.mark.asyncio
async def test_list_sessions_raw_turns_and_history_decode_exact_requests() -> None:
    fake = FakeSession()
    fake.unary_responses = {
        LIST_CHAT_SESSIONS_METHOD: [
            chat_pb2.ListChatSessionsResponse(
                sessions=[common_pb2.ChatSession(chat_session_id="conversation-1")]
            ),
            chat_pb2.ListChatSessionsResponse(
                sessions=[common_pb2.ChatSession(chat_session_id="conversation-1")]
            ),
        ],
        LIST_CHAT_TURNS_METHOD: [
            chat_pb2.ListChatTurnsResponse(
                chat_turns=[
                    _chat_turn("Newest?", "Newest.", message_id="message-2", role=2),
                    _chat_turn("Oldest?", "Oldest.", message_id="message-1", role=1),
                ],
                next_page_token="captured-next-page",
            ),
            chat_pb2.ListChatTurnsResponse(
                chat_turns=[
                    _chat_turn("Newest?", "Newest.", message_id="message-2", role=2),
                    _chat_turn("Oldest?", "Oldest.", message_id="message-1", role=1),
                ]
            ),
        ],
    }
    api, _, _ = _api(fake)

    assert await api.get_conversation_id("notebook-1") == "conversation-1"
    raw = await api.get_conversation_turns("notebook-1", "conversation-1", limit=1)
    assert isinstance(raw, chat_pb2.ListChatTurnsResponse)
    assert raw.next_page_token == "captured-next-page"
    assert len(raw.chat_turns) == 2
    assert [turn.observed_event_type for turn in raw.chat_turns] == [2, 1]
    assert await api.get_history("notebook-1", limit=2) == [
        ("Oldest?", "Oldest."),
        ("Newest?", "Newest."),
    ]

    assert fake.unary_calls == [
        (
            LIST_CHAT_SESSIONS_METHOD,
            chat_pb2.ListChatSessionsRequest(project_id="notebook-1"),
            {
                "replay_safe": True,
                "response_type": chat_pb2.ListChatSessionsResponse,
            },
        ),
        (
            LIST_CHAT_TURNS_METHOD,
            chat_pb2.ListChatTurnsRequest(chat_session_id="conversation-1"),
            {
                "replay_safe": True,
                "response_type": chat_pb2.ListChatTurnsResponse,
            },
        ),
        (
            LIST_CHAT_SESSIONS_METHOD,
            chat_pb2.ListChatSessionsRequest(project_id="notebook-1"),
            {
                "replay_safe": True,
                "response_type": chat_pb2.ListChatSessionsResponse,
            },
        ),
        (
            LIST_CHAT_TURNS_METHOD,
            chat_pb2.ListChatTurnsRequest(chat_session_id="conversation-1"),
            {
                "replay_safe": True,
                "response_type": chat_pb2.ListChatTurnsResponse,
            },
        ),
    ]


@pytest.mark.asyncio
async def test_base_ask_uses_latest_cumulative_final_without_concatenating_frames() -> None:
    fake = FakeSession()
    fake.unary_responses[LIST_CHAT_SESSIONS_METHOD] = [
        chat_pb2.ListChatSessionsResponse(),
        chat_pb2.ListChatSessionsResponse(
            sessions=[common_pb2.ChatSession(chat_session_id="conversation-1")]
        ),
    ]
    fake.stream_responses = [
        [
            _frame("Part", final=False),
            _frame("Superseded final", final=True),
            _frame("Final answer [2]", final=True),
        ]
    ]
    api, guard, notebooks = _api(fake, source_ids=["source-1", "source-2"])

    result = await api.ask("notebook-1", "Question?")

    assert result.answer == "Final answer [2]"
    assert result.conversation_id == "conversation-1"
    assert result.turn_number == 1
    assert result.is_follow_up is False
    assert result.raw_response == ""
    assert result.answer_document.text == "Final answer"
    assert result.turn_key is not None
    assert result.turn_key.session_id == "conversation-1"
    assert result.turn_key.turn_id == "turn-server-1"
    assert result.turn_key.turn_code == 17
    assert result.references == [
        ChatReference(
            source_id="source-1",
            citation_number=2,
            cited_text="Source passage",
            start_char=20,
            end_char=33,
            chunk_id="chunk-1",
            answer_anchor_start=12,
            answer_anchor_end=12,
        )
    ]
    assert api.get_cached_turns("conversation-1") == [
        ConversationTurn(query="Question?", answer="Final answer [2]", turn_number=1)
    ]
    assert guard.calls == 1
    assert notebooks.calls == ["notebook-1"]

    method, request, kwargs = fake.stream_calls[0]
    assert method == GENERATE_FREE_FORM_STREAMED_METHOD
    assert request == chat_pb2.GenerateFreeFormStreamedRequest(
        sources=[
            sources_pb2.InputSource(source_id=read_pb2.SourceId(id="source-1")),
            sources_pb2.InputSource(source_id=read_pb2.SourceId(id="source-2")),
        ],
        user_query="Question?",
        user_message_id="00000000-0000-4000-8000-000000000099",
        project_id="notebook-1",
        origin=chat_pb2.QUERY_ORIGIN_CHAT_TEXT_BOX,
    )
    assert kwargs == {
        "timeout": 180.0,
        "response_type": chat_pb2.GenerateFreeFormStreamedResponse,
        "telemetry_method": None,
    }


@pytest.mark.asyncio
async def test_follow_up_maps_cached_turns_to_captured_conversation_events() -> None:
    fake = FakeSession()
    turns = chat_pb2.ListChatTurnsResponse(
        chat_turns=[
            _chat_turn("Server newest?", "Yes.", message_id="server-2", role=2),
            _chat_turn("Server oldest?", "Yes.", message_id="server-1", role=1),
        ]
    )
    fake.unary_responses[LIST_CHAT_TURNS_METHOD] = [turns, turns]
    fake.stream_responses = [[_frame("Final answer", final=True)]]
    api, _, _ = _api(fake)
    api._cache.cache_conversation_turn("conversation-1", "Cached question?", "Cached answer.", 1)

    assert await api._list_turn_roles("notebook-1", "conversation-1", 2) == [2, 1]
    result = await api.ask(
        "notebook-1",
        "Follow-up?",
        source_ids=["source-1"],
        conversation_id="conversation-1",
    )

    # The base counts only role 1 as a prior user question; role 2 is not
    # silently rewritten into another question by the Android adapter.
    assert result.turn_number == 2
    assert result.is_follow_up is True
    request = fake.stream_calls[0][1]
    assert list(request.conversation_history) == [
        chat_pb2.ConversationEvent(
            text="Cached answer.",
            type=chat_pb2.ConversationEvent.GENERATED_RESPONSE,
        ),
        chat_pb2.ConversationEvent(
            text="Cached question?",
            type=chat_pb2.ConversationEvent.USER_QUERY,
        ),
    ]
    assert request.chat_session_id == "conversation-1"


@pytest.mark.asyncio
async def test_stream_eof_without_field_5_finality_fails_and_does_not_cache() -> None:
    fake = FakeSession()
    fake.stream_responses = [[_frame("Partial only", final=False)]]
    api, _, _ = _api(fake)

    with pytest.raises(ChatResponseParseError, match="field 5"):
        await api._stream_answer(
            notebook_id="notebook-1",
            question="Question?",
            source_ids=["source-1"],
            cached_turns=[],
            conversation_id=None,
        )
    assert api.cache_size() == 0
    assert len(fake.stream_calls) == 1


@pytest.mark.asyncio
async def test_delete_uses_base_lock_cache_workflow_and_exact_non_replay_request() -> None:
    fake = FakeSession()
    fake.unary_responses[DELETE_CHAT_TURNS_METHOD] = [Empty()]
    api, guard, _ = _api(fake)
    api._cache.cache_conversation_turn("conversation-1", "Question?", "Answer.", 1)

    assert await api.delete_conversation("notebook-1", "conversation-1") is None

    assert guard.calls == 1
    assert api.get_cached_turns("conversation-1") == []
    assert fake.unary_calls == [
        (
            DELETE_CHAT_TURNS_METHOD,
            chat_pb2.DeleteChatTurnsRequest(
                chat_session_id="conversation-1",
                delete_all_history=True,
            ),
            {"replay_safe": False, "response_type": Empty},
        )
    ]


@pytest.mark.asyncio
async def test_configure_sends_whole_advanced_settings_block() -> None:
    fake = FakeSession()
    fake.unary_responses[MUTATE_PROJECT_METHOD] = [read_pb2.Project(id="notebook-1")]
    api, _, _ = _api(fake)

    await api.configure(
        "notebook-1",
        goal=ChatGoal.CUSTOM,
        response_length=ChatResponseLength.LONGER,
        custom_prompt="Be exact.",
    )

    assert len(fake.unary_calls) == 1
    method, request, kwargs = fake.unary_calls[0]
    assert method == MUTATE_PROJECT_METHOD
    assert request.project_id == "notebook-1"
    assert request.HasField("request_context")
    assert len(request.mutations) == 1
    settings = request.mutations[0].advanced_settings
    assert (settings.goal_settings.goal, settings.goal_settings.custom_prompt) == (
        ChatGoal.CUSTOM.value,
        "Be exact.",
    )
    assert settings.response_style_settings.response_length == ChatResponseLength.LONGER.value
    assert kwargs == {"replay_safe": False, "response_type": read_pb2.Project}
    assert fake.stream_calls == []


@pytest.mark.asyncio
async def test_configure_defaults_and_validates_custom_prompt() -> None:
    fake = FakeSession()
    fake.unary_responses[MUTATE_PROJECT_METHOD] = [read_pb2.Project(id="notebook-1")]
    api, _, _ = _api(fake)

    await api.configure("notebook-1")
    settings = fake.unary_calls[0][1].mutations[0].advanced_settings
    assert settings.goal_settings.goal == ChatGoal.DEFAULT.value
    assert settings.response_style_settings.response_length == ChatResponseLength.DEFAULT.value

    with pytest.raises(ValidationError, match="custom_prompt is required"):
        await api.configure("notebook-1", goal=ChatGoal.CUSTOM)
    assert len(fake.unary_calls) == 1

    fake.unary_responses[MUTATE_PROJECT_METHOD] = [read_pb2.Project(id="notebook-1")]
    await api.configure(
        "notebook-1",
        goal=ChatGoal.LEARNING_GUIDE,
        custom_prompt="inactive draft",
    )
    assert fake.unary_calls[-1][1].mutations[0].advanced_settings.goal_settings.custom_prompt == ""


@pytest.mark.asyncio
async def test_get_settings_decodes_advanced_project_block() -> None:
    fake = FakeSession()
    fake.unary_responses[GET_PROJECT_METHOD] = [
        wire_notebooks_pb2.WireGetProjectResponse(
            project=wire_notebooks_pb2.WireProjectWithAdvancedSettings(
                advanced_settings=wire_notebooks_pb2.WireProjectAdvancedSettings(
                    goal_settings=wire_notebooks_pb2.WireProjectGoalSettings(
                        goal=ChatGoal.CUSTOM.value,
                        custom_prompt="Use terse proofs.",
                    ),
                    response_style_settings=wire_notebooks_pb2.WireProjectResponseStyleSettings(
                        response_length=ChatResponseLength.SHORTER.value,
                    ),
                )
            )
        )
    ]
    api, _, _ = _api(fake)

    assert await api.get_settings("notebook-1") == ChatSettings(
        goal=ChatGoal.CUSTOM,
        response_length=ChatResponseLength.SHORTER,
        custom_prompt="Use terse proofs.",
    )
    assert fake.unary_calls == [
        (
            GET_PROJECT_METHOD,
            read_pb2.GetProjectRequest(
                project_id="notebook-1",
                include_audio_overview_ids=True,
            ),
            {"replay_safe": True, "response_type": wire_notebooks_pb2.WireGetProjectResponse},
        )
    ]


@pytest.mark.asyncio
async def test_get_settings_defaults_when_block_is_absent() -> None:
    fake = FakeSession()
    fake.unary_responses[GET_PROJECT_METHOD] = [
        wire_notebooks_pb2.WireGetProjectResponse(
            project=wire_notebooks_pb2.WireProjectWithAdvancedSettings()
        )
    ]
    api, _, _ = _api(fake)
    assert await api.get_settings("notebook-1") == ChatSettings(
        goal=ChatGoal.DEFAULT,
        response_length=ChatResponseLength.DEFAULT,
    )


@pytest.mark.asyncio
async def test_get_settings_rejects_partial_or_unknown_settings() -> None:
    fake = FakeSession()
    fake.unary_responses[GET_PROJECT_METHOD] = [
        wire_notebooks_pb2.WireGetProjectResponse(
            project=wire_notebooks_pb2.WireProjectWithAdvancedSettings(
                advanced_settings=wire_notebooks_pb2.WireProjectAdvancedSettings(
                    goal_settings=wire_notebooks_pb2.WireProjectGoalSettings(goal=99),
                    response_style_settings=wire_notebooks_pb2.WireProjectResponseStyleSettings(
                        response_length=ChatResponseLength.DEFAULT.value
                    ),
                )
            )
        ),
        wire_notebooks_pb2.WireGetProjectResponse(
            project=wire_notebooks_pb2.WireProjectWithAdvancedSettings(
                advanced_settings=wire_notebooks_pb2.WireProjectAdvancedSettings(
                    goal_settings=wire_notebooks_pb2.WireProjectGoalSettings(
                        goal=ChatGoal.DEFAULT.value
                    )
                )
            )
        ),
        wire_notebooks_pb2.WireGetProjectResponse(
            project=wire_notebooks_pb2.WireProjectWithAdvancedSettings(
                advanced_settings=wire_notebooks_pb2.WireProjectAdvancedSettings(
                    goal_settings=wire_notebooks_pb2.WireProjectGoalSettings(
                        goal=ChatGoal.CUSTOM.value
                    ),
                    response_style_settings=wire_notebooks_pb2.WireProjectResponseStyleSettings(
                        response_length=ChatResponseLength.DEFAULT.value
                    ),
                )
            )
        ),
    ]
    api, _, _ = _api(fake)
    with pytest.raises(UnknownRPCMethodError, match="unknown Android chat-settings enum"):
        await api.get_settings("notebook-1")
    with pytest.raises(UnknownRPCMethodError, match="partial chat-settings block"):
        await api.get_settings("notebook-1")
    with pytest.raises(UnknownRPCMethodError, match="CUSTOM chat settings without a prompt"):
        await api.get_settings("notebook-1")


@pytest.mark.asyncio
async def test_get_settings_rejects_missing_project_envelope() -> None:
    fake = FakeSession()
    fake.unary_responses[GET_PROJECT_METHOD] = [wire_notebooks_pb2.WireGetProjectResponse()]
    api, _, _ = _api(fake)
    with pytest.raises(UnknownRPCMethodError, match="omitted its project"):
        await api.get_settings("notebook-1")


@pytest.mark.asyncio
async def test_save_answer_as_note_uses_b6_saved_response_seam() -> None:
    fake = FakeSession()
    fake.unary_responses[CREATE_NOTE_METHOD] = [
        notes_pb2.CreateNoteResponse(
            note=notes_pb2.ProjectNote(
                id="note-1",
                content="Answer [1]",
                metadata=notes_pb2.NoteMetadata(type=SAVED_RESPONSE_NOTE_TYPE),
                name="Saved answer",
            )
        )
    ]
    api, _, _ = _api(fake)
    result = AskResult(
        answer="Answer [1]",
        conversation_id="conversation-1",
        turn_number=1,
        is_follow_up=False,
        references=[
            ChatReference(
                source_id="source-1",
                citation_number=1,
                chunk_id="chunk-1",
            )
        ],
        answer_document=StructuredDocument(),
    )

    note = await api.save_answer_as_note("notebook-1", result, title="Saved answer")

    assert (note.id, note.title, note.content) == ("note-1", "Saved answer", "Answer [1]")
    assert fake.unary_calls == [
        (
            CREATE_NOTE_METHOD,
            notes_pb2.CreateNoteRequest(
                project_id="notebook-1",
                content="Answer [1]",
                metadata=notes_pb2.NoteMetadata(type=SAVED_RESPONSE_NOTE_TYPE),
                name="Saved answer",
            ),
            {"replay_safe": False, "response_type": notes_pb2.CreateNoteResponse},
        )
    ]
    assert fake.stream_calls == []


@pytest.mark.asyncio
async def test_set_mode_reaches_configure_hook() -> None:
    class RecordingAndroidChatAPI(AndroidChatAPI):
        def __init__(self) -> None:
            fake = FakeSession()
            super().__init__(
                session=_android_session(fake),
                loop_guard=FakeLoopGuard(),
                notebooks=FakeNotebooks(),
            )
            self.configured: tuple[Any, ...] | None = None

        async def configure(
            self, notebook_id: str, goal=None, response_length=None, custom_prompt=None
        ):
            self.configured = (notebook_id, goal, response_length, custom_prompt)

    api = RecordingAndroidChatAPI()
    await api.set_mode("notebook-1", ChatMode.LEARNING_GUIDE)
    assert api.configured == (
        "notebook-1",
        ChatGoal.LEARNING_GUIDE,
        ChatResponseLength.LONGER,
        None,
    )


def test_android_chat_module_does_not_select_public_factory() -> None:
    from notebooklm import _client_assembly

    assert "AndroidChatAPI" not in vars(_client_assembly)
    assert "AndroidChatAPI" not in vars(__import__("notebooklm", fromlist=["*"]))
