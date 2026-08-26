"""Transport-neutral records for semantic chat operations."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, unique
from typing import TypeAlias

from ..._types.documents import StructuredDocument
from ..operations import CallPolicy, Operation, OperationDef, OperationTier

ChatLegacyScalar: TypeAlias = str | int | float | bool | None


@dataclass(frozen=True, slots=True)
class ChatLegacySequenceRecord:
    """Immutable compatibility carrier for an opaque sequence in a raw turn row."""

    items: tuple[ChatLegacyValue, ...]


@dataclass(frozen=True, slots=True)
class ChatLegacyMappingRecord:
    """Immutable compatibility carrier for an opaque mapping in a raw turn row."""

    items: tuple[tuple[str, ChatLegacyValue], ...]


ChatLegacyValue: TypeAlias = ChatLegacyScalar | ChatLegacySequenceRecord | ChatLegacyMappingRecord


@dataclass(frozen=True, slots=True)
class ChatReferenceRecord:
    """Backend-neutral citation with source and answer-document coordinate ranges."""

    source_id: str
    citation_number: int | None = None
    cited_text: str | None = None
    start_char: int | None = None
    end_char: int | None = None
    chunk_id: str | None = None
    passage_id: str | None = None
    score: float | None = None
    fragment_start_char: int | None = None
    fragment_end_char: int | None = None
    answer_anchor_start: int | None = None
    answer_anchor_end: int | None = None


@dataclass(frozen=True, slots=True)
class ChatTurnKeyRecord:
    """Neutral three-part identifier volunteered by one streamed answer turn."""

    session_id: str
    turn_id: str | None = None
    turn_code: int | None = None


@dataclass(frozen=True, slots=True)
class ChatNextStepRecord:
    """Neutral follow-up suggestion preserving an unknown backend type code."""

    question: str
    type_code: int


@dataclass(frozen=True, slots=True)
class ChatTurnDecodeErrorRecord:
    """Bounded evidence for a deferred strict answer-content decode failure."""

    message: str
    method_id: str | int | None = None
    path: tuple[int, ...] | None = None
    source: str | None = None
    found_ids: tuple[str | int, ...] = ()
    raw_response: str | None = None
    data_at_failure: str | None = None
    rpc_code: str | int | None = None


@dataclass(frozen=True, slots=True)
class ChatConversationTurnRecord:
    """One decoded history row plus its exact immutable compatibility projection."""

    legacy_row: ChatLegacyValue = field(repr=False)
    is_well_formed: bool = False
    is_question_role: bool = False
    is_question: bool = False
    is_answer: bool = False
    has_unrecognized_role: bool = False
    role: ChatLegacyValue = None
    question_text: str = ""
    answer_text: str | None = None
    answer_error: ChatTurnDecodeErrorRecord | None = None


@dataclass(frozen=True, slots=True)
class ChatGetConversationInput:
    """Notebook whose most recent conversation identity is requested."""

    notebook_id: str


@dataclass(frozen=True, slots=True)
class ChatGetConversationResult:
    """Most recent conversation identity, or ``None`` when none is visible."""

    conversation_id: str | None


@dataclass(frozen=True, slots=True)
class ChatGetHistoryInput:
    """Bounded history request for one exact conversation."""

    notebook_id: str
    conversation_id: str
    limit: int = 2


@dataclass(frozen=True, slots=True)
class ChatGetHistoryResult:
    """Newest-first decoded turns with the legacy envelope-presence distinction."""

    turns: tuple[ChatConversationTurnRecord, ...]
    envelope_present: bool = True
    turns_container_present: bool = True


@dataclass(frozen=True, slots=True)
class ChatDeleteHistoryInput:
    """Conversation-turn collection to delete from one notebook."""

    notebook_id: str
    conversation_id: str


@dataclass(frozen=True, slots=True)
class ChatDeleteHistoryResult:
    """Successful chat-history deletion."""


@unique
class ChatConfigureAction(str, Enum):
    """Closed read/write variants carried by ``chat.configure``."""

    GET = "get"
    SET = "set"


@dataclass(frozen=True, slots=True)
class ChatConfigureInput:
    """Read or replace a notebook's complete chat-settings block."""

    notebook_id: str
    action: ChatConfigureAction
    goal: str | None = None
    response_length: str | None = None
    custom_prompt: str | None = None


@dataclass(frozen=True, slots=True)
class ChatSettingsRecord:
    """Neutral chat settings using semantic enum labels rather than wire codes."""

    goal: str
    response_length: str
    custom_prompt: str | None = None


@dataclass(frozen=True, slots=True)
class ChatConfigureResult:
    """Settings for a read, or ``None`` after a successful mutation."""

    settings: ChatSettingsRecord | None = None


@dataclass(frozen=True, slots=True)
class ChatSaveNoteInput:
    """Citation-rich answer to persist through the saved-from-chat variant."""

    notebook_id: str
    answer: str
    references: tuple[ChatReferenceRecord, ...]
    title: str


@dataclass(frozen=True, slots=True)
class ChatSavedNoteRecord:
    """Created saved-answer note independent of the public mutable model."""

    id: str
    notebook_id: str
    title: str
    content: str
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ChatSaveNoteResult:
    """Completed saved-answer note creation."""

    note: ChatSavedNoteRecord


@dataclass(frozen=True, slots=True)
class ChatHistoryPairRecord:
    """One cached Q/A pair supplied to a follow-up streamed request."""

    question: str
    answer: str


@dataclass(frozen=True, slots=True)
class ChatCachedTurnRecord:
    """One locally cached exchange with the ordinal the workflow assigned it.

    The cache keeps its own turn numbering because the server does not echo
    one back; this record is what the chat workflow hands its facade, which
    projects it to the public conversation-turn model.
    """

    query: str
    answer: str
    turn_number: int


@dataclass(frozen=True, slots=True)
class ChatAskInput:
    """One adapter-owned streamed ask workflow request."""

    notebook_id: str
    question: str
    source_ids: tuple[str, ...]
    conversation_history: tuple[ChatHistoryPairRecord, ...] = ()
    post_conversation_id: str | None = None
    resolved_conversation_id: str | None = None


@dataclass(frozen=True, slots=True)
class ChatStreamAnswerRecord:
    """Decoded phase-one stream value before the real conversation id is resolved."""

    answer: str
    references: tuple[ChatReferenceRecord, ...]
    answer_document: StructuredDocument
    turn_key: ChatTurnKeyRecord | None = None
    next_steps: tuple[ChatNextStepRecord, ...] = ()


@dataclass(frozen=True, slots=True)
class ChatStreamAnswerInput:
    """One streamed answer request: chat.ask's first phase without its readback.

    The conversation id here is the one *posted* to the server.  The id the
    workflow finally reports is resolved above this leaf — the stream never
    returns one — so ``ChatAskInput.resolved_conversation_id`` has no place in
    the leaf's input.
    """

    notebook_id: str
    question: str
    source_ids: tuple[str, ...]
    conversation_history: tuple[ChatHistoryPairRecord, ...] = ()
    post_conversation_id: str | None = None


@dataclass(frozen=True, slots=True)
class ChatStreamResult:
    """One decoded streamed answer plus the bounded raw response it came from.

    ``raw_response`` is the truncated diagnostic slice ``ChatAskResultRecord``
    requires; the streamed answer record itself carries no wire text, so the
    leaf's result pairs the two rather than widening the answer record.
    """

    answer: ChatStreamAnswerRecord
    raw_response: str


@dataclass(frozen=True, slots=True)
class ChatAskResultRecord:
    """One fully completed two-phase chat result; partial results are unrepresentable."""

    answer: str
    conversation_id: str
    references: tuple[ChatReferenceRecord, ...]
    raw_response: str
    answer_document: StructuredDocument
    turn_key: ChatTurnKeyRecord | None = None
    next_steps: tuple[ChatNextStepRecord, ...] = ()


@dataclass(frozen=True, slots=True)
class ChatAskOutcomeRecord:
    """One completed ask plus the two conversation facts only the workflow knows.

    ``turn_number`` and ``is_follow_up`` are not server-reported: the first is
    derived from the authoritative prior-turn count the workflow reads before
    posting, the second from whether the ask continued an existing
    conversation. Neither belongs in ``ChatAskResultRecord`` — that record is
    the backend's answer to one operation, while these two describe the
    client-side conversation the workflow placed it in.
    """

    result: ChatAskResultRecord
    turn_number: int
    is_follow_up: bool


CHAT_GET_CONVERSATION_DEF = OperationDef(
    Operation.CHAT_GET_CONVERSATION,
    CallPolicy.READ,
    ChatGetConversationInput,
    ChatGetConversationResult,
)
CHAT_GET_HISTORY_DEF = OperationDef(
    Operation.CHAT_GET_HISTORY,
    CallPolicy.READ,
    ChatGetHistoryInput,
    ChatGetHistoryResult,
)
CHAT_DELETE_HISTORY_DEF = OperationDef(
    Operation.CHAT_DELETE_HISTORY,
    CallPolicy.MUTATION,
    ChatDeleteHistoryInput,
    ChatDeleteHistoryResult,
)
CHAT_CONFIGURE_DEF = OperationDef(
    Operation.CHAT_CONFIGURE,
    CallPolicy.MUTATION,
    ChatConfigureInput,
    ChatConfigureResult,
)
CHAT_SAVE_NOTE_DEF = OperationDef(
    Operation.CHAT_SAVE_NOTE,
    CallPolicy.MUTATION,
    ChatSaveNoteInput,
    ChatSaveNoteResult,
)
CHAT_ASK_DEF = OperationDef(
    Operation.CHAT_ASK,
    CallPolicy.STREAM,
    ChatAskInput,
    ChatAskResultRecord,
)
CHAT_STREAM_ANSWER_DEF = OperationDef(
    Operation.CHAT_STREAM_ANSWER,
    CallPolicy.STREAM,
    ChatStreamAnswerInput,
    ChatStreamResult,
    tier=OperationTier.PRIMITIVE,
)


__all__ = [name for name in globals() if name.startswith("Chat") or name.startswith("CHAT_")]
