"""Direct B5 Android chat adapter and neutral orchestration tests."""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, cast

import pytest
from google.protobuf.empty_pb2 import Empty

from notebooklm._android.chat import (
    DELETE_CHAT_TURNS_METHOD,
    GENERATE_FREE_FORM_STREAMED_METHOD,
    LIST_CHAT_SESSIONS_METHOD,
    LIST_CHAT_TURNS_METHOD,
    AndroidChatAPI,
)
from notebooklm._android.proto.google.internal.labs.tailwind.orchestration.v1 import (
    b1_read_pb2,
    b3_sources_pb2,
    b5_chat_pb2,
)
from notebooklm._android.proto.labs.language.tailwind.common.protos import chat_history_pb2
from notebooklm._android.session import AndroidSession
from notebooklm._chat import ChatAPI
from notebooklm._types.documents import StructuredDocument
from notebooklm._types.enums import ChatGoal, ChatResponseLength
from notebooklm.exceptions import ChatResponseParseError, UnsupportedOperationError
from notebooklm.types import AskResult, ChatMode, ChatReference, ConversationTurn


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
    return b5_chat_pb2.ChatHistoryMessage(
        message_id=message_id,
        observed_event_type=role,
        user_query_text=question,
        act_on_sources_response=b5_chat_pb2.ActOnSourcesResponse(
            response=b5_chat_pb2.AnswerResponse(response=answer)
        ),
    )


def _document() -> Any:
    return b5_chat_pb2.TailwindDoc(
        body=b5_chat_pb2.Body(
            content=[
                b5_chat_pb2.StructuralElement(
                    start_index=0,
                    end_index=12,
                    paragraph=b5_chat_pb2.Paragraph(
                        elements=[
                            b5_chat_pb2.ParagraphElement(
                                start_index=0,
                                end_index=12,
                                text_run=b5_chat_pb2.TextRun(content="Final answer"),
                            )
                        ]
                    ),
                )
            ],
            inline_object_locations=[
                b5_chat_pb2.AnnotationMapEntry(
                    object_id=b5_chat_pb2.ObjectId(id="chunk-1"),
                    content_range=b5_chat_pb2.Range(start_index=12, end_index=12),
                )
            ],
        ),
        objects=[
            b5_chat_pb2.DocumentObject(object_id=b5_chat_pb2.ObjectId(id="non-citation-object")),
            b5_chat_pb2.DocumentObject(
                object_id=b5_chat_pb2.ObjectId(id="chunk-1"),
                citation=b5_chat_pb2.Citation(
                    fragment=b5_chat_pb2.TailwindDocFragment(
                        elements=[
                            b5_chat_pb2.StructuralElement(
                                start_index=20,
                                end_index=33,
                                paragraph=b5_chat_pb2.Paragraph(
                                    elements=[
                                        b5_chat_pb2.ParagraphElement(
                                            start_index=20,
                                            end_index=33,
                                            text_run=b5_chat_pb2.TextRun(content="Source passage"),
                                        )
                                    ]
                                ),
                            )
                        ]
                    ),
                    source_attribution=b5_chat_pb2.CitationSource(
                        ingested_source=b5_chat_pb2.SourceRevision(
                            source=b1_read_pb2.SourceId(id="source-1")
                        )
                    ),
                    object_id=b5_chat_pb2.ObjectId(id="citation-inner-id"),
                ),
            ),
        ],
    )


def _frame(text: str, *, final: bool) -> Any:
    return b5_chat_pb2.GenerateFreeFormStreamedResponse(
        answer=b5_chat_pb2.AnswerResponse(
            response=text,
            conversation_turn_key=b5_chat_pb2.ConversationTurnKey(
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
            b5_chat_pb2.ListChatSessionsResponse(
                sessions=[chat_history_pb2.ChatSession(chat_session_id="conversation-1")]
            ),
            b5_chat_pb2.ListChatSessionsResponse(
                sessions=[chat_history_pb2.ChatSession(chat_session_id="conversation-1")]
            ),
        ],
        LIST_CHAT_TURNS_METHOD: [
            b5_chat_pb2.ListChatTurnsResponse(
                chat_turns=[
                    _chat_turn("Newest?", "Newest.", message_id="message-2", role=2),
                    _chat_turn("Oldest?", "Oldest.", message_id="message-1", role=1),
                ],
                next_page_token="captured-next-page",
            ),
            b5_chat_pb2.ListChatTurnsResponse(
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
    assert isinstance(raw, b5_chat_pb2.ListChatTurnsResponse)
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
            b5_chat_pb2.ListChatSessionsRequest(project_id="notebook-1"),
            {
                "replay_safe": True,
                "response_type": b5_chat_pb2.ListChatSessionsResponse,
            },
        ),
        (
            LIST_CHAT_TURNS_METHOD,
            b5_chat_pb2.ListChatTurnsRequest(chat_session_id="conversation-1"),
            {
                "replay_safe": True,
                "response_type": b5_chat_pb2.ListChatTurnsResponse,
            },
        ),
        (
            LIST_CHAT_SESSIONS_METHOD,
            b5_chat_pb2.ListChatSessionsRequest(project_id="notebook-1"),
            {
                "replay_safe": True,
                "response_type": b5_chat_pb2.ListChatSessionsResponse,
            },
        ),
        (
            LIST_CHAT_TURNS_METHOD,
            b5_chat_pb2.ListChatTurnsRequest(chat_session_id="conversation-1"),
            {
                "replay_safe": True,
                "response_type": b5_chat_pb2.ListChatTurnsResponse,
            },
        ),
    ]


@pytest.mark.asyncio
async def test_base_ask_uses_latest_cumulative_final_without_concatenating_frames() -> None:
    fake = FakeSession()
    fake.unary_responses[LIST_CHAT_SESSIONS_METHOD] = [
        b5_chat_pb2.ListChatSessionsResponse(),
        b5_chat_pb2.ListChatSessionsResponse(
            sessions=[chat_history_pb2.ChatSession(chat_session_id="conversation-1")]
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
    assert request == b5_chat_pb2.GenerateFreeFormStreamedRequest(
        sources=[
            b3_sources_pb2.InputSource(source_id=b1_read_pb2.SourceId(id="source-1")),
            b3_sources_pb2.InputSource(source_id=b1_read_pb2.SourceId(id="source-2")),
        ],
        user_query="Question?",
        user_message_id="00000000-0000-4000-8000-000000000099",
        project_id="notebook-1",
        origin=b5_chat_pb2.QUERY_ORIGIN_CHAT_TEXT_BOX,
    )
    assert kwargs == {
        "timeout": 180.0,
        "response_type": b5_chat_pb2.GenerateFreeFormStreamedResponse,
        "telemetry_method": None,
    }


@pytest.mark.asyncio
async def test_follow_up_maps_cached_turns_to_captured_conversation_events() -> None:
    fake = FakeSession()
    turns = b5_chat_pb2.ListChatTurnsResponse(
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
        b5_chat_pb2.ConversationEvent(
            text="Cached answer.",
            type=b5_chat_pb2.ConversationEvent.GENERATED_RESPONSE,
        ),
        b5_chat_pb2.ConversationEvent(
            text="Cached question?",
            type=b5_chat_pb2.ConversationEvent.USER_QUERY,
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
            b5_chat_pb2.DeleteChatTurnsRequest(
                chat_session_id="conversation-1",
                delete_all_history=True,
            ),
            {"replay_safe": False, "response_type": Empty},
        )
    ]


UnsupportedCall = Callable[[AndroidChatAPI], Awaitable[object]]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invoke",
    [
        pytest.param(lambda api: api.configure("notebook-1"), id="configure"),
        pytest.param(lambda api: api.get_settings("notebook-1"), id="get-settings"),
        pytest.param(lambda api: api.set_mode("notebook-1", ChatMode.DEFAULT), id="set-mode"),
        pytest.param(
            lambda api: api.save_answer_as_note(
                "notebook-1",
                AskResult(
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
                ),
            ),
            id="save-answer-as-note",
        ),
    ],
)
async def test_b5_unsupported_operations_fail_before_transport_io(invoke: UnsupportedCall) -> None:
    fake = FakeSession()
    api, _, _ = _api(fake)
    with pytest.raises(UnsupportedOperationError, match="web backend"):
        await invoke(api)
    assert fake.unary_calls == []
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
