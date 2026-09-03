"""Direct chat Android chat adapter and neutral orchestration tests."""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, cast

import pytest
from google.protobuf.empty_pb2 import Empty

from notebooklm._android.chat import (
    CANCEL_GENERATION_METHOD,
    DELETE_CHAT_TURNS_METHOD,
    GENERATE_FREE_FORM_STREAMED_METHOD,
    GET_CHAT_SESSION_STATUS_METHOD,
    GET_PROJECT_METHOD,
    LIST_CHAT_SESSIONS_METHOD,
    LIST_CHAT_TURNS_METHOD,
    MUTATE_PROJECT_METHOD,
    AndroidChatAPI,
)
from notebooklm._android.codecs.chat import decode_references
from notebooklm._android.codecs.documents import decode_document
from notebooklm._android.notes import CREATE_NOTE_METHOD, SAVED_RESPONSE_NOTE_TYPE
from notebooklm._android.proto.google.internal.labs.tailwind.orchestration.v1 import (
    chat_pb2,
    notebooks_pb2,
    notes_pb2,
    read_pb2,
    sources_pb2,
)
from notebooklm._android.proto.labs.language.tailwind.common.protos import common_pb2
from notebooklm._android.proto.notebooklm.internal.android.wire.v1 import (
    notebooks_pb2 as wire_notebooks_pb2,
)
from notebooklm._android.session import AndroidSession
from notebooklm._chat import ChatAPI, _TurnRoleSnapshot
from notebooklm._types.documents import BlockKind, BlockStyle, ListStyle, StructuredDocument
from notebooklm._types.enums import ChatGoal, ChatResponseLength
from notebooklm.exceptions import (
    AuthError,
    ChatError,
    ChatResponseParseError,
    DecodingError,
    UnknownRPCMethodError,
    ValidationError,
)
from notebooklm.types import (
    AskResult,
    ChatMode,
    ChatReference,
    ChatSettings,
    ConversationTurn,
    NextStepSuggestion,
)


class FakeSession:
    """Recording chat fake server at the AndroidSession seam."""

    def __init__(self) -> None:
        self.unary_responses: dict[str, list[Any]] = {}
        self.stream_responses: list[list[Any]] = []
        self.unary_calls: list[tuple[str, Any, dict[str, Any]]] = []
        self.stream_calls: list[tuple[str, Any, dict[str, Any]]] = []

    @asynccontextmanager
    async def operation_scope(self, label: str, **kwargs: Any) -> AsyncIterator[_Lease]:
        assert not kwargs
        yield _Lease()

    async def unary(self, method: str, request: Any, **kwargs: Any) -> Any:
        self.unary_calls.append((method, request, kwargs))
        if method == LIST_CHAT_SESSIONS_METHOD and not self.unary_responses.get(method):
            return chat_pb2.ListChatSessionsResponse(
                sessions=[common_pb2.ChatSession(chat_session_id="conversation-1")]
            )
        response = self.unary_responses[method].pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    async def stream(self, method: str, request: Any, **kwargs: Any) -> AsyncIterator[Any]:
        self.stream_calls.append((method, request, kwargs))
        for response in self.stream_responses.pop(0):
            yield response


@dataclass(frozen=True)
class _Lease:
    epoch: int = 7


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
    chat_response_max_bytes: int | None = None,
) -> tuple[AndroidChatAPI, FakeLoopGuard, FakeNotebooks]:
    guard = FakeLoopGuard()
    notebooks = FakeNotebooks(source_ids)
    api = AndroidChatAPI(
        session=_android_session(fake),
        loop_guard=guard,
        notebooks=notebooks,
        chat_response_max_bytes=chat_response_max_bytes,
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "generating", "token"),
    [
        (chat_pb2.GetChatSessionStatusResponse(status=1), False, None),
        (
            chat_pb2.GetChatSessionStatusResponse(
                generation_token="generation-token",
                status=2,
            ),
            True,
            "generation-token",
        ),
    ],
)
async def test_session_status_decodes_native_state_and_exact_request(
    response: Any,
    generating: bool,
    token: str | None,
) -> None:
    fake = FakeSession()
    fake.unary_responses[GET_CHAT_SESSION_STATUS_METHOD] = [response]
    api, guard, _ = _api(fake)

    status = await api.session_status("notebook-1", "conversation-1")

    assert (status.generating, status.token) == (generating, token)
    assert guard.calls == 1
    method, request, kwargs = fake.unary_calls[0]
    assert method == GET_CHAT_SESSION_STATUS_METHOD
    assert request.chat_session_id == "conversation-1"
    assert request.request_context.client_type == 2
    assert kwargs == {
        "replay_safe": True,
        "response_type": chat_pb2.GetChatSessionStatusResponse,
    }


@pytest.mark.asyncio
async def test_session_status_resolves_latest_conversation() -> None:
    fake = FakeSession()
    fake.unary_responses[GET_CHAT_SESSION_STATUS_METHOD] = [
        chat_pb2.GetChatSessionStatusResponse(status=1)
    ]
    api, _, _ = _api(fake)

    status = await api.session_status("notebook-1")

    assert status.generating is False
    assert [method for method, _request, _kwargs in fake.unary_calls] == [
        LIST_CHAT_SESSIONS_METHOD,
        GET_CHAT_SESSION_STATUS_METHOD,
    ]


@pytest.mark.asyncio
async def test_cancel_generation_sends_cancellable_context_and_preserves_auth_error() -> None:
    fake = FakeSession()
    fake.unary_responses[CANCEL_GENERATION_METHOD] = [
        chat_pb2.CancelGenerationResponse(),
        AuthError("permission denied", method_id=CANCEL_GENERATION_METHOD, rpc_code=7),
    ]
    api, guard, _ = _api(fake)

    assert await api.cancel("notebook-1", "conversation-1") is None
    method, request, kwargs = fake.unary_calls[0]
    assert method == CANCEL_GENERATION_METHOD
    assert request.chat_session_id == "conversation-1"
    assert request.request_context.client_type == 2
    assert kwargs == {
        "replay_safe": True,
        "response_type": chat_pb2.CancelGenerationResponse,
    }

    with pytest.raises(AuthError) as raised:
        await api.cancel("notebook-1", "foreign-conversation")
    assert raised.value.rpc_code == 7
    assert guard.calls == 2


@pytest.mark.asyncio
async def test_session_control_is_noop_without_a_conversation() -> None:
    fake = FakeSession()
    fake.unary_responses[LIST_CHAT_SESSIONS_METHOD] = [
        chat_pb2.ListChatSessionsResponse(),
        chat_pb2.ListChatSessionsResponse(),
    ]
    api, _, _ = _api(fake)

    status = await api.session_status("notebook-1")
    assert (status.generating, status.token) == (False, None)
    assert await api.cancel("notebook-1") is None
    assert [method for method, _request, _kwargs in fake.unary_calls] == [
        LIST_CHAT_SESSIONS_METHOD,
        LIST_CHAT_SESSIONS_METHOD,
    ]


def test_shared_document_decoder_preserves_style_lists_tables_and_offset_kinds() -> None:
    document = chat_pb2.TailwindDoc(
        body=chat_pb2.Body(
            content=[
                chat_pb2.StructuralElement(
                    start_index=0,
                    end_index=6,
                    paragraph=chat_pb2.Paragraph(
                        elements=[
                            chat_pb2.ParagraphElement(
                                start_index=0,
                                end_index=6,
                                text_run=chat_pb2.TextRun(
                                    content="Styled",
                                    text_style=chat_pb2.TextStyle(
                                        bold=True,
                                        italic=True,
                                        underline=True,
                                        url="https://example.invalid/style",
                                    ),
                                ),
                            )
                        ],
                        paragraph_style=chat_pb2.ParagraphStyle(
                            named_style_type=chat_pb2.HEADING_1
                        ),
                        bullet_info=chat_pb2.BulletInfo(
                            nesting_level=2,
                            glyph="1.",
                            list_type=chat_pb2.LIST_TYPE_ORDERED,
                            ordinal=1,
                        ),
                    ),
                ),
                chat_pb2.StructuralElement(
                    start_index=6,
                    end_index=16,
                    table=chat_pb2.Table(
                        rows=1,
                        columns=2,
                        table_rows=[
                            chat_pb2.TableRow(
                                start_index=6,
                                end_index=16,
                                table_cells=[
                                    chat_pb2.TableCell(
                                        start_index=6,
                                        end_index=10,
                                        content=[
                                            chat_pb2.StructuralElement(
                                                start_index=5,
                                                end_index=11,
                                                paragraph=chat_pb2.Paragraph(
                                                    elements=[
                                                        chat_pb2.ParagraphElement(
                                                            start_index=5,
                                                            end_index=11,
                                                            text_run=chat_pb2.TextRun(
                                                                content="XHeadY"
                                                            ),
                                                        )
                                                    ]
                                                ),
                                            )
                                        ],
                                    ),
                                    chat_pb2.TableCell(
                                        start_index=10,
                                        end_index=16,
                                        content=[
                                            chat_pb2.StructuralElement(
                                                start_index=10,
                                                end_index=16,
                                                paragraph=chat_pb2.Paragraph(
                                                    elements=[
                                                        chat_pb2.ParagraphElement(
                                                            start_index=10,
                                                            end_index=16,
                                                            text_run=chat_pb2.TextRun(
                                                                content="Value!"
                                                            ),
                                                        )
                                                    ]
                                                ),
                                            )
                                        ],
                                    ),
                                ],
                            )
                        ],
                    ),
                ),
                chat_pb2.StructuralElement(
                    start_index=16,
                    end_index=20,
                    code_block=chat_pb2.CodeBlock(content="code"),
                ),
                chat_pb2.StructuralElement(
                    start_index=20,
                    end_index=24,
                    thought=chat_pb2.Thought(),
                ),
                chat_pb2.StructuralElement(
                    start_index=24,
                    end_index=25,
                    image=chat_pb2.Image(url="https://example.invalid/image"),
                ),
                chat_pb2.StructuralElement(
                    start_index=25,
                    end_index=26,
                    a2ui_block=chat_pb2.A2uiBlock(json="{}"),
                ),
                chat_pb2.StructuralElement(
                    start_index=26,
                    end_index=27,
                    horizontal_rule=chat_pb2.HorizontalRule(),
                ),
                chat_pb2.StructuralElement(start_index=27, end_index=28),
            ]
        )
    )

    decoded = decode_document(document)

    paragraph, table, code, thought, image, a2ui, rule, unknown = decoded.blocks
    assert paragraph.style is BlockStyle.HEADING_1
    assert paragraph.list_info is not None
    assert paragraph.list_info.style is ListStyle.ORDERED
    assert (paragraph.list_info.nesting_level, paragraph.list_info.glyph) == (2, "1.")
    assert paragraph.list_info.ordinal == 1
    [span] = paragraph.spans
    assert (span.bold, span.italic, span.underline, span.url) == (
        True,
        True,
        True,
        "https://example.invalid/style",
    )
    assert table.kind is BlockKind.TABLE
    assert [span.text for span in table.spans] == ["Head", "Value!"]
    assert [(cell.start_index, cell.end_index) for cell in table.table_rows[0]] == [
        (6, 10),
        (10, 16),
    ]
    assert decoded.render(6, 16) == "Head\tValue!"
    assert [block.kind for block in (code, thought, image, a2ui, rule, unknown)] == [
        BlockKind.CODE_BLOCK,
        BlockKind.THOUGHT,
        BlockKind.IMAGE,
        BlockKind.A2UI_BLOCK,
        BlockKind.HORIZONTAL_RULE,
        BlockKind.UNKNOWN,
    ]
    assert all(not block.spans for block in (code, thought, image, a2ui, rule, unknown))


def test_citation_fragment_uses_structural_text_when_blocks_have_no_spans() -> None:
    document = chat_pb2.TailwindDoc(
        objects=[
            chat_pb2.DocumentObject(
                citation=chat_pb2.Citation(
                    fragment=chat_pb2.TailwindDocFragment(
                        elements=[
                            chat_pb2.StructuralElement(
                                start_index=5,
                                end_index=9,
                                code_block=chat_pb2.CodeBlock(content="code"),
                            ),
                            chat_pb2.StructuralElement(
                                start_index=9,
                                end_index=16,
                                thought=chat_pb2.Thought(
                                    elements=[
                                        chat_pb2.StructuralElement(
                                            start_index=9,
                                            end_index=16,
                                            paragraph=chat_pb2.Paragraph(
                                                elements=[
                                                    chat_pb2.ParagraphElement(
                                                        text_run=chat_pb2.TextRun(content="thought")
                                                    )
                                                ]
                                            ),
                                        )
                                    ]
                                ),
                            ),
                        ]
                    ),
                    source_attribution=chat_pb2.CitationSource(
                        ingested_source=chat_pb2.SourceRevision(
                            source=read_pb2.SourceId(id="source-structural")
                        )
                    ),
                )
            )
        ]
    )

    [reference] = decode_references(document, StructuredDocument())

    assert reference.source_id == "source-structural"
    assert reference.cited_text == "code\nthought"
    assert (reference.start_char, reference.end_char) == (5, 16)


def test_citation_declared_ranges_union_without_overwriting_fragment_offsets() -> None:
    document = chat_pb2.TailwindDoc(
        objects=[
            chat_pb2.DocumentObject(
                citation=chat_pb2.Citation(
                    ranges=[
                        chat_pb2.Range(start_index=100, end_index=110),
                        chat_pb2.Range(start_index=120, end_index=140),
                    ],
                    fragment=chat_pb2.TailwindDocFragment(
                        elements=[
                            chat_pb2.StructuralElement(
                                start_index=5,
                                end_index=9,
                                paragraph=chat_pb2.Paragraph(
                                    elements=[
                                        chat_pb2.ParagraphElement(
                                            start_index=5,
                                            end_index=9,
                                            text_run=chat_pb2.TextRun(content="text"),
                                        )
                                    ]
                                ),
                            )
                        ]
                    ),
                    source_attribution=chat_pb2.CitationSource(
                        ingested_source=chat_pb2.SourceRevision(
                            source=read_pb2.SourceId(id="source-ranged")
                        )
                    ),
                )
            )
        ]
    )

    [reference] = decode_references(document, StructuredDocument())

    assert (reference.start_char, reference.end_char) == (5, 9)
    assert (reference.fragment_start_char, reference.fragment_end_char) == (100, 140)
    assert (reference.answer_start_char, reference.answer_end_char) == (100, 140)


def test_citation_declared_ranges_reject_an_invalid_pair_as_one_value() -> None:
    document = chat_pb2.TailwindDoc(
        objects=[
            chat_pb2.DocumentObject(
                citation=chat_pb2.Citation(
                    ranges=[
                        chat_pb2.Range(start_index=10, end_index=20),
                        chat_pb2.Range(start_index=30, end_index=25),
                    ],
                    source_attribution=chat_pb2.CitationSource(
                        ingested_source=chat_pb2.SourceRevision(
                            source=read_pb2.SourceId(id="source-invalid-range")
                        )
                    ),
                )
            )
        ]
    )

    [reference] = decode_references(document, StructuredDocument())

    assert reference.fragment_start_char is None
    assert reference.fragment_end_char is None


def _frame(
    text: str,
    *,
    final: bool,
    suggestions: list[tuple[str, int]] | None = None,
) -> Any:
    response = chat_pb2.GenerateFreeFormStreamedResponse(
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
    if suggestions is not None:
        response.next_step_suggestions.CopyFrom(
            notebooks_pb2.NextStepSuggestions(
                next_steps=[
                    notebooks_pb2.NextStep(suggestion=question, suggestion_type=type_code)
                    for question, type_code in suggestions
                ]
            )
        )
    return response


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
                    _chat_turn("", "Newest.", message_id="answer-2", role=2),
                ],
                next_page_token="raw-next-page",
            ),
            chat_pb2.ListChatTurnsResponse(
                chat_turns=[
                    _chat_turn("", "Newest.", message_id="answer-2", role=2),
                    _chat_turn("Newest?", "", message_id="question-2", role=1),
                    _chat_turn("", "Middle.", message_id="answer-1", role=2),
                    _chat_turn("Middle?", "", message_id="question-1", role=1),
                ],
                next_page_token="history-next-page",
            ),
            chat_pb2.ListChatTurnsResponse(
                chat_turns=[
                    _chat_turn("", "Oldest.", message_id="answer-0", role=2),
                    _chat_turn("Oldest?", "", message_id="question-0", role=1),
                ]
            ),
        ],
    }
    api, _, _ = _api(fake)

    assert await api.get_conversation_id("notebook-1") == "conversation-1"
    raw = await api.get_conversation_turns("notebook-1", "conversation-1", limit=1)
    assert isinstance(raw, chat_pb2.ListChatTurnsResponse)
    assert raw.next_page_token == "raw-next-page"
    assert len(raw.chat_turns) == 1
    assert [turn.observed_event_type for turn in raw.chat_turns] == [2]
    assert await api.get_history("notebook-1", limit=3) == [
        ("Oldest?", "Oldest."),
        ("Middle?", "Middle."),
        ("Newest?", "Newest."),
    ]

    assert [call[0] for call in fake.unary_calls] == [
        LIST_CHAT_SESSIONS_METHOD,
        LIST_CHAT_SESSIONS_METHOD,
        LIST_CHAT_TURNS_METHOD,
        LIST_CHAT_SESSIONS_METHOD,
        LIST_CHAT_SESSIONS_METHOD,
        LIST_CHAT_TURNS_METHOD,
        LIST_CHAT_TURNS_METHOD,
    ]
    scoped_calls = fake.unary_calls[1:3] + fake.unary_calls[4:]
    assert all(call[2]["expected_epoch"] == 7 for call in scoped_calls)


@pytest.mark.asyncio
async def test_history_uses_response_document_when_legacy_answer_text_is_empty() -> None:
    answer_turn = _chat_turn("", "", message_id="answer-1", role=2)
    answer_turn.act_on_sources_response.response.response_doc.CopyFrom(_document())
    question_turn = _chat_turn("Document answer?", "", message_id="question-1", role=1)
    fake = FakeSession()
    fake.unary_responses = {
        LIST_CHAT_SESSIONS_METHOD: [
            chat_pb2.ListChatSessionsResponse(
                sessions=[common_pb2.ChatSession(chat_session_id="conversation-1")]
            )
        ],
        LIST_CHAT_TURNS_METHOD: [
            chat_pb2.ListChatTurnsResponse(chat_turns=[answer_turn, question_turn])
        ],
    }
    api, _, _ = _api(fake)

    assert await api.get_history("notebook-1", limit=1) == [("Document answer?", "Final answer")]


@pytest.mark.asyncio
async def test_list_turns_zero_limit_skips_transport_and_token_cycle_fails() -> None:
    fake = FakeSession()
    fake.unary_responses[LIST_CHAT_TURNS_METHOD] = [
        chat_pb2.ListChatTurnsResponse(
            chat_turns=[_chat_turn("One?", "One.", message_id="message-1", role=1)],
            next_page_token="cycle-token",
        ),
        chat_pb2.ListChatTurnsResponse(next_page_token="cycle-token"),
    ]
    api, _, _ = _api(fake)

    empty = await api.get_conversation_turns("notebook-1", "conversation-1", limit=-1)
    assert list(empty.chat_turns) == []
    assert fake.unary_calls == []

    with pytest.raises(UnknownRPCMethodError, match="pagination token"):
        await api.get_conversation_turns("notebook-1", "conversation-1", limit=2)

    turn_calls = [call for call in fake.unary_calls if call[0] == LIST_CHAT_TURNS_METHOD]
    assert [call[1].page_token for call in turn_calls] == ["", "cycle-token"]


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
    assert 0 < len(result.raw_response) <= 1000
    assert '"response": "Final answer [2]"' in result.raw_response
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
        request_context=request.request_context,
        user_message_id="00000000-0000-4000-8000-000000000099",
        project_id="notebook-1",
        origin=chat_pb2.QUERY_ORIGIN_CHAT_TEXT_BOX,
    )
    assert request.request_context.client_type == 2
    assert kwargs == {
        "replay_safe": False,
        "timeout": 180.0,
        "response_type": chat_pb2.GenerateFreeFormStreamedResponse,
        "telemetry_method": "chat.ask",
        "max_response_bytes": None,
    }


@pytest.mark.asyncio
async def test_stream_next_step_suggestions_are_nonempty_last_wins_across_frames() -> None:
    fake = FakeSession()
    fake.stream_responses = [
        [
            _frame(
                "Partial",
                final=False,
                suggestions=[("First suggestion", 9), ("", 7)],
            ),
            _frame(
                "Later partial",
                final=False,
                suggestions=[("Winning suggestion", 99)],
            ),
            _frame("Final answer", final=True, suggestions=[]),
        ]
    ]
    api, _, _ = _api(fake)

    posted = await api._stream_answer(
        notebook_id="notebook-1",
        question="Question?",
        source_ids=["source-1"],
        cached_turns=[],
        conversation_id="conversation-1",
    )

    assert posted.next_steps == [NextStepSuggestion(question="Winning suggestion", type_code=99)]
    assert '"response": "Final answer"' in posted.raw_response


@pytest.mark.asyncio
async def test_stream_preserves_exact_empty_answer_reason_in_raw_response() -> None:
    fake = FakeSession()
    fake.stream_responses = [
        [
            chat_pb2.GenerateFreeFormStreamedResponse(
                answer=chat_pb2.AnswerResponse(
                    empty_answer_reason=chat_pb2.FILTERED,
                ),
                is_final_response=True,
            )
        ]
    ]
    api, _, _ = _api(fake)

    posted = await api._stream_answer(
        notebook_id="notebook-1",
        question="Question?",
        source_ids=["source-1"],
        cached_turns=[],
        conversation_id="conversation-1",
    )

    assert posted.answer == ""
    assert '"empty_answer_reason": "FILTERED"' in posted.raw_response


@pytest.mark.asyncio
async def test_follow_up_maps_cached_turns_to_captured_conversation_events() -> None:
    fake = FakeSession()
    first_page = chat_pb2.ListChatTurnsResponse(
        chat_turns=[_chat_turn("Server newest?", "Yes.", message_id="server-2", role=2)],
        next_page_token="older-page",
    )
    second_page = chat_pb2.ListChatTurnsResponse(
        chat_turns=[_chat_turn("Server oldest?", "Yes.", message_id="server-1", role=1)]
    )
    fake.unary_responses[LIST_CHAT_TURNS_METHOD] = [
        first_page,
        second_page,
        first_page,
        second_page,
    ]
    fake.stream_responses = [[_frame("Final answer", final=True)]]
    api, _, _ = _api(fake)
    api._cache.cache_conversation_turn("conversation-1", "Cached question?", "Cached answer.", 1)

    assert await api._list_turn_roles("notebook-1", "conversation-1", 2) == _TurnRoleSnapshot(
        roles=(2, 1), exhausted=True
    )
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
    turn_calls = [call for call in fake.unary_calls if call[0] == LIST_CHAT_TURNS_METHOD]
    assert [call[1].page_token for call in turn_calls] == [
        "",
        "older-page",
        "",
        "older-page",
    ]
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
@pytest.mark.parametrize("exhausted", [True, False])
async def test_turn_count_uses_android_exhaustion_token_at_the_safety_ceiling(
    exhausted: bool,
) -> None:
    fake = FakeSession()
    api, _, _ = _api(fake)

    async def get_conversation_turns(
        notebook_id: str,
        conversation_id: str,
        limit: int = 2,
    ) -> chat_pb2.ListChatTurnsResponse:
        del notebook_id, conversation_id
        return chat_pb2.ListChatTurnsResponse(
            chat_turns=[chat_pb2.ChatHistoryMessage(observed_event_type=1)] * limit,
            next_page_token="" if exhausted and limit == 12_800 else "more",
        )

    api.get_conversation_turns = get_conversation_turns  # type: ignore[method-assign]
    if exhausted:
        assert await api._count_prior_server_turns("notebook-1", "conversation-1") == 12_800
    else:
        with pytest.raises(ChatError, match="maximum 12,800-row snapshot"):
            await api._count_prior_server_turns("notebook-1", "conversation-1")


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
    assert [call[0] for call in fake.unary_calls] == [
        LIST_CHAT_SESSIONS_METHOD,
        DELETE_CHAT_TURNS_METHOD,
    ]
    _method, request, kwargs = fake.unary_calls[1]
    assert request == chat_pb2.DeleteChatTurnsRequest(
        chat_session_id="conversation-1",
        delete_all_history=True,
    )
    assert kwargs == {
        "replay_safe": False,
        "response_type": Empty,
        "expected_epoch": 7,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["turns", "delete"])
async def test_global_conversation_operations_reject_cross_notebook_ids(
    operation: str,
) -> None:
    fake = FakeSession()
    fake.unary_responses[LIST_CHAT_SESSIONS_METHOD] = [
        chat_pb2.ListChatSessionsResponse(
            sessions=[common_pb2.ChatSession(chat_session_id="owned-conversation")]
        )
    ]
    api, _, _ = _api(fake)

    with pytest.raises(ChatError, match="was not found in notebook"):
        if operation == "turns":
            await api.get_conversation_turns("notebook-1", "foreign-conversation")
        else:
            await api.delete_conversation("notebook-1", "foreign-conversation")

    assert [method for method, _request, _kwargs in fake.unary_calls] == [LIST_CHAT_SESSIONS_METHOD]


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
async def test_configure_rejects_an_unexpected_project_identity() -> None:
    fake = FakeSession()
    fake.unary_responses[MUTATE_PROJECT_METHOD] = [read_pb2.Project(id="other-notebook")]
    api, _, _ = _api(fake)

    with pytest.raises(DecodingError, match="unexpected notebook id"):
        await api.configure("notebook-1")


@pytest.mark.asyncio
async def test_get_settings_decodes_advanced_project_block() -> None:
    fake = FakeSession()
    fake.unary_responses[GET_PROJECT_METHOD] = [
        wire_notebooks_pb2.WireGetProjectResponse(
            project=wire_notebooks_pb2.WireProjectWithAdvancedSettings(
                id="notebook-1",
                advanced_settings=wire_notebooks_pb2.WireProjectAdvancedSettings(
                    goal_settings=wire_notebooks_pb2.WireProjectGoalSettings(
                        goal=ChatGoal.CUSTOM.value,
                        custom_prompt="Use terse proofs.",
                    ),
                    response_style_settings=wire_notebooks_pb2.WireProjectResponseStyleSettings(
                        response_length=ChatResponseLength.SHORTER.value,
                    ),
                ),
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
            project=wire_notebooks_pb2.WireProjectWithAdvancedSettings(id="notebook-1")
        )
    ]
    api, _, _ = _api(fake)
    assert await api.get_settings("notebook-1") == ChatSettings(
        goal=ChatGoal.DEFAULT,
        response_length=ChatResponseLength.DEFAULT,
    )


@pytest.mark.asyncio
async def test_get_settings_rejects_a_different_echoed_notebook_id() -> None:
    fake = FakeSession()
    fake.unary_responses[GET_PROJECT_METHOD] = [
        wire_notebooks_pb2.WireGetProjectResponse(
            project=wire_notebooks_pb2.WireProjectWithAdvancedSettings(id="other-notebook")
        )
    ]
    api, _, _ = _api(fake)

    with pytest.raises(DecodingError, match="unexpected notebook id"):
        await api.get_settings("requested-notebook")


@pytest.mark.asyncio
async def test_get_settings_rejects_a_missing_echoed_notebook_id() -> None:
    fake = FakeSession()
    fake.unary_responses[GET_PROJECT_METHOD] = [
        wire_notebooks_pb2.WireGetProjectResponse(
            project=wire_notebooks_pb2.WireProjectWithAdvancedSettings()
        )
    ]
    api, _, _ = _api(fake)

    with pytest.raises(DecodingError, match="did not contain a notebook id"):
        await api.get_settings("requested-notebook")


@pytest.mark.asyncio
async def test_get_settings_rejects_partial_or_unknown_settings() -> None:
    fake = FakeSession()
    fake.unary_responses[GET_PROJECT_METHOD] = [
        wire_notebooks_pb2.WireGetProjectResponse(
            project=wire_notebooks_pb2.WireProjectWithAdvancedSettings(
                id="notebook-1",
                advanced_settings=wire_notebooks_pb2.WireProjectAdvancedSettings(
                    goal_settings=wire_notebooks_pb2.WireProjectGoalSettings(goal=99),
                    response_style_settings=wire_notebooks_pb2.WireProjectResponseStyleSettings(
                        response_length=ChatResponseLength.DEFAULT.value
                    ),
                ),
            )
        ),
        wire_notebooks_pb2.WireGetProjectResponse(
            project=wire_notebooks_pb2.WireProjectWithAdvancedSettings(
                id="notebook-1",
                advanced_settings=wire_notebooks_pb2.WireProjectAdvancedSettings(
                    goal_settings=wire_notebooks_pb2.WireProjectGoalSettings(
                        goal=ChatGoal.DEFAULT.value
                    )
                ),
            )
        ),
        wire_notebooks_pb2.WireGetProjectResponse(
            project=wire_notebooks_pb2.WireProjectWithAdvancedSettings(
                id="notebook-1",
                advanced_settings=wire_notebooks_pb2.WireProjectAdvancedSettings(
                    goal_settings=wire_notebooks_pb2.WireProjectGoalSettings(
                        goal=ChatGoal.CUSTOM.value
                    ),
                    response_style_settings=wire_notebooks_pb2.WireProjectResponseStyleSettings(
                        response_length=ChatResponseLength.DEFAULT.value
                    ),
                ),
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
async def test_save_answer_as_note_preserves_rich_citation_contract_natively() -> None:
    fake = FakeSession()
    fake.unary_responses[CREATE_NOTE_METHOD] = [
        notes_pb2.CreateNoteResponse(
            note=notes_pb2.ProjectNote(
                id="note-1",
                content="Answer 😀 [1]",
                metadata=notes_pb2.NoteMetadata(type=SAVED_RESPONSE_NOTE_TYPE),
                name="Saved answer",
            )
        )
    ]
    api, _, _ = _api(fake)
    result = AskResult(
        answer="Answer 😀 [1]",
        conversation_id="conversation-1",
        turn_number=1,
        is_follow_up=False,
        references=[
            ChatReference(
                source_id="source-1",
                citation_number=1,
                cited_text="Source 😀 passage",
                start_char=20,
                end_char=37,
                chunk_id="chunk-1",
            )
        ],
        answer_document=StructuredDocument(),
    )

    note = await api.save_answer_as_note("notebook-1", result, title="Saved answer")

    assert (note.id, note.title, note.content) == (
        "note-1",
        "Saved answer",
        "Answer 😀 [1]",
    )
    assert len(fake.unary_calls) == 1
    method, request, kwargs = fake.unary_calls[0]
    assert method == CREATE_NOTE_METHOD
    assert kwargs == {"replay_safe": False, "response_type": notes_pb2.CreateNoteResponse}
    assert (request.project_id, request.content, request.metadata.type, request.name) == (
        "notebook-1",
        "Answer 😀 [1]",
        SAVED_RESPONSE_NOTE_TYPE,
        "Saved answer",
    )
    assert request.request_context.client_type == 3
    document = request.tailwind_doc_content
    assert document.type == 0
    assert document.body.content[0].end_index == 9
    assert document.body.content[0].paragraph.elements[0].text_run.content == "Answer 😀"
    anchor = document.body.inline_object_locations[0]
    assert (
        anchor.object_id.id,
        anchor.content_range.start_index,
        anchor.content_range.end_index,
    ) == (
        "chunk-1",
        0,
        9,
    )
    citation = document.objects[0].citation
    assert document.objects[0].object_id.id == "chunk-1"
    assert (citation.ranges[0].start_index, citation.ranges[0].end_index) == (20, 37)
    assert citation.fragment.elements[0].end_index == 17
    assert citation.fragment.elements[0].paragraph.elements[0].text_run.content == (
        "Source 😀 passage"
    )
    assert citation.source_attribution.ingested_source.source.id == "source-1"
    assert citation.object_id.id == "chunk-1"
    assert list(request.source_passages) == [citation]
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
