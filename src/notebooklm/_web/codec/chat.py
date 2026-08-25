"""Web wire codecs for the six semantic chat operations."""

from __future__ import annotations

import logging
import reprlib
from typing import Any

from ..._binding import CodecPayload
from ..._notebook_payloads import build_get_notebook_params
from ..._records import (
    ChatAskInput,
    ChatConfigureAction,
    ChatConfigureInput,
    ChatConfigureResult,
    ChatConversationTurnRecord,
    ChatDeleteHistoryInput,
    ChatDeleteHistoryResult,
    ChatGetConversationInput,
    ChatGetConversationResult,
    ChatGetHistoryInput,
    ChatGetHistoryResult,
    ChatLegacyMappingRecord,
    ChatLegacySequenceRecord,
    ChatLegacyValue,
    ChatReferenceRecord,
    ChatSavedNoteRecord,
    ChatSaveNoteInput,
    ChatSaveNoteResult,
    ChatSettingsRecord,
    ChatStreamAnswerRecord,
    ChatTurnDecodeErrorRecord,
)
from ..._request_types import AuthSnapshot
from ..._row_adapters.chat import (
    ConversationTurnRow,
    SavedChatNoteRow,
    unwrap_chat_settings,
    unwrap_conversation_turns,
    unwrap_last_conversation_id,
)
from ..._row_adapters.notes import NoteRow
from ...exceptions import UnknownRPCMethodError
from ...rpc import RPCMethod, safe_index
from .chat_saved_note import build_save_note_params as _encode_save_note_params
from .chat_stream import (
    _extract_next_turn_content,
    encode_ask_stream,
    parse_streaming_chat_response,
)

# The conversation-id shape warnings predate the codec rows and stay pinned
# under the chat facade's logger name.
chat_logger = logging.getLogger("notebooklm._chat.api")

_GOAL_CODES = {
    "default": 1,
    "custom": 2,
    "learning_guide": 3,
}
_GOAL_LABELS = {value: key for key, value in _GOAL_CODES.items()}
_RESPONSE_LENGTH_CODES = {
    "default": 1,
    "longer": 4,
    "shorter": 5,
}
_RESPONSE_LENGTH_LABELS = {value: key for key, value in _RESPONSE_LENGTH_CODES.items()}


def build_get_conversation_params(notebook_id: str) -> list[Any]:
    """Encode ``GET_LAST_CONVERSATION_ID`` for one notebook."""
    return [[], None, notebook_id, 1]


def decode_get_conversation_result(raw: Any) -> str | None:
    """Decode the soft ``hPTbtc`` conversation-id envelope."""
    return unwrap_last_conversation_id(raw)


def decode_conversation_id_or_warn(raw: Any, *, notebook_id: str) -> str | None:
    """Decode the conversation id, warning on unexpected non-empty shapes.

    Shared by the ``chat.get_conversation`` codec row and the ``chat.ask``
    post-stream resolution so both surface identical diagnostics.
    """
    conversation_id = decode_get_conversation_result(raw)
    if conversation_id is not None:
        return conversation_id
    if raw and isinstance(raw, list):
        chat_logger.warning(
            "hPTbtc returned an unexpected response shape; no "
            "conversation_id extracted (notebook=%s, raw=%r)",
            notebook_id,
            repr(raw)[:500],
        )
    elif raw is not None:
        chat_logger.warning(
            "hPTbtc returned a non-list, non-empty response (notebook=%s, type=%s, raw=%r)",
            notebook_id,
            type(raw).__name__,
            repr(raw)[:500],
        )
    return None


def build_get_history_params(conversation_id: str, limit: int) -> list[Any]:
    """Encode ``GET_CONVERSATION_TURNS`` for one exact conversation."""
    return [[], None, None, conversation_id, limit]


def _freeze_legacy(value: Any) -> ChatLegacyValue:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return ChatLegacySequenceRecord(tuple(_freeze_legacy(item) for item in value))
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return ChatLegacyMappingRecord(
            tuple((key, _freeze_legacy(item)) for key, item in value.items())
        )
    return reprlib.repr(value)


def _capture_turn_error(exc: UnknownRPCMethodError) -> ChatTurnDecodeErrorRecord:
    return ChatTurnDecodeErrorRecord(
        message=str(exc.args[0]) if exc.args else "",
        method_id=exc.method_id,
        path=exc.path,
        source=exc.source,
        found_ids=tuple(exc.found_ids or ()),
        raw_response=(exc.raw_response if isinstance(exc.raw_response, str) else None),
        data_at_failure=(exc.data_at_failure if isinstance(exc.data_at_failure, str) else None),
        rpc_code=exc.rpc_code,
    )


def decode_get_history_result(raw: Any, *, source: str) -> ChatGetHistoryResult:
    """Decode history rows once while retaining their exact legacy projection."""
    turns = unwrap_conversation_turns(raw, source=source)
    records: list[ChatConversationTurnRecord] = []
    for turn in turns:
        row = ConversationTurnRow(turn)
        answer_text: str | None = None
        answer_error: ChatTurnDecodeErrorRecord | None = None
        if row.is_answer:
            try:
                answer_text = _extract_next_turn_content(row.raw)
            except UnknownRPCMethodError as exc:
                answer_error = _capture_turn_error(exc)
        records.append(
            ChatConversationTurnRecord(
                legacy_row=_freeze_legacy(turn),
                is_well_formed=row.is_well_formed,
                is_question_role=row.role == ConversationTurnRow.ROLE_QUESTION,
                is_question=row.is_question,
                is_answer=row.is_answer,
                has_unrecognized_role=row.has_unrecognized_role,
                role=_freeze_legacy(row.role),
                question_text=row.question_text,
                answer_text=answer_text,
                answer_error=answer_error,
            )
        )
    envelope_present = isinstance(raw, list) and bool(raw)
    return ChatGetHistoryResult(
        turns=tuple(records),
        envelope_present=envelope_present,
        turns_container_present=not (envelope_present and raw[0] is None),
    )


def build_delete_history_params(conversation_id: str) -> list[Any]:
    """Encode the live DeleteChatTurns request."""
    return [[], conversation_id, None, 1]


def build_configure_params(value: ChatConfigureInput) -> list[Any]:
    """Encode a complete chat-settings replacement."""
    if value.goal is None or value.response_length is None:
        raise ValueError("chat.configure SET requires goal and response_length")
    goal_code = _GOAL_CODES[value.goal]
    length_code = _RESPONSE_LENGTH_CODES[value.response_length]
    goal_array = [goal_code, value.custom_prompt] if value.goal == "custom" else [goal_code]
    chat_settings = [goal_array, [length_code]]
    return [
        value.notebook_id,
        [[None, None, None, None, None, None, None, chat_settings]],
    ]


def build_get_settings_params(notebook_id: str) -> list[Any]:
    """Encode chat's dedicated recency-bumping ``GET_NOTEBOOK`` read."""
    return build_get_notebook_params(notebook_id)


def decode_get_settings_result(raw: Any) -> ChatSettingsRecord:
    """Decode and validate chat settings without exposing native enum codes."""
    nb_info = safe_index(
        raw,
        0,
        method_id=RPCMethod.GET_NOTEBOOK.value,
        source="ChatAPI.get_settings",
    )
    row = unwrap_chat_settings(nb_info, source="ChatAPI.get_settings")
    goal = _GOAL_LABELS.get(row.goal_code)
    length = _RESPONSE_LENGTH_LABELS.get(row.response_length_code)
    if goal is None or length is None:
        raise UnknownRPCMethodError(
            "unknown chat-settings enum code "
            f"(goal={row.goal_code!r}, response_length={row.response_length_code!r})",
            method_id=RPCMethod.GET_NOTEBOOK.value,
            path=(7,),
            source="ChatAPI.get_settings",
            data_at_failure=reprlib.repr((row.goal_code, row.response_length_code)),
        )
    return ChatSettingsRecord(goal=goal, response_length=length, custom_prompt=row.custom_prompt)


def build_save_note_params(
    notebook_id: str,
    answer: str,
    references: tuple[ChatReferenceRecord, ...],
    title: str,
) -> list[Any]:
    """Encode the seven-element ``saved_from_chat`` CREATE_NOTE variant."""
    return _encode_save_note_params(notebook_id, answer, references, title)


def decode_save_note_result(
    raw: Any,
    *,
    notebook_id: str,
    answer: str,
    requested_title: str,
) -> ChatSavedNoteRecord:
    """Decode the saved-note response into a neutral record."""
    create_row = SavedChatNoteRow(raw)
    note_data = create_row.note_data
    note_id = create_row.note_id
    server_title = (
        create_row.server_title if create_row.server_title is not None else requested_title
    )
    if not note_id:
        raise RuntimeError("CREATE_NOTE returned no note ID for saved-from-chat request")
    created_at = NoteRow([note_id, note_data]).created_at
    return ChatSavedNoteRecord(
        id=note_id,
        notebook_id=notebook_id,
        title=server_title,
        content=answer,
        created_at=created_at,
    )


def build_ask_request(
    snapshot: AuthSnapshot,
    value: ChatAskInput,
    *,
    reqid: int,
) -> tuple[str, str, dict[str, str]]:
    """Encode phase one of the streamed ask workflow."""
    conversation_history: list[Any] | None = None
    if value.conversation_history:
        conversation_history = []
        for turn in value.conversation_history:
            conversation_history.append([turn.answer, None, 2])
            conversation_history.append([turn.question, None, 1])
    return encode_ask_stream(
        snapshot=snapshot,
        notebook_id=value.notebook_id,
        question=value.question,
        source_ids=list(value.source_ids),
        conversation_history=conversation_history,
        conversation_id=value.post_conversation_id,
        reqid=reqid,
    )


def decode_ask_response(response_text: str) -> ChatStreamAnswerRecord:
    """Decode phase-one bytes into the neutral streamed-answer record.

    The parser already emits records (P10 R2.1), so this only drops the
    per-stream id ``StreamingChatParseResult.conversation_id`` carries — an
    unreliable value the real conversation-id readback replaces — and freezes
    the parser's lists into tuples.
    """
    parsed = parse_streaming_chat_response(response_text)
    return ChatStreamAnswerRecord(
        answer=parsed.answer,
        references=tuple(parsed.references),
        answer_document=parsed.answer_document,
        turn_key=parsed.turn_key,
        next_steps=tuple(parsed.next_steps),
    )


# Row-facing codecs (P9.3). Each encoder returns the full request payload one
# codec row dispatches — params plus the notebook route and option flags exactly
# as the retired handler passed them — and never names a method; the row's
# ``NativeCallSpec`` is the sole method authority.
def encode_chat_get_conversation(value: ChatGetConversationInput) -> CodecPayload:
    """Payload for the ``chat.get_conversation`` codec row."""
    return CodecPayload(
        params=build_get_conversation_params(value.notebook_id),
        source_path=f"/notebook/{value.notebook_id}",
    )


def encode_ask_conversation_readback(notebook_id: str) -> CodecPayload:
    """Payload for ``chat.ask``'s post-stream conversation-id read (phase two)."""
    return CodecPayload(
        params=build_get_conversation_params(notebook_id),
        source_path=f"/notebook/{notebook_id}",
    )


def decode_chat_get_conversation(
    value: ChatGetConversationInput, raw: Any
) -> ChatGetConversationResult:
    """Row decoder for ``chat.get_conversation`` with the shape warnings retained."""
    return ChatGetConversationResult(
        decode_conversation_id_or_warn(raw, notebook_id=value.notebook_id)
    )


def encode_chat_get_history(value: ChatGetHistoryInput) -> CodecPayload:
    """Payload for the ``chat.get_history`` codec row."""
    return CodecPayload(
        params=build_get_history_params(value.conversation_id, value.limit),
        source_path=f"/notebook/{value.notebook_id}",
    )


def decode_chat_get_history(value: ChatGetHistoryInput, raw: Any) -> ChatGetHistoryResult:
    """Row decoder for ``chat.get_history``."""
    del value
    return decode_get_history_result(raw, source="ChatAPI.get_conversation_turns")


def encode_chat_delete_history(value: ChatDeleteHistoryInput) -> CodecPayload:
    """Payload for the ``chat.delete_history`` codec row."""
    return CodecPayload(
        params=build_delete_history_params(value.conversation_id),
        source_path=f"/notebook/{value.notebook_id}",
    )


def decode_chat_delete_history(value: ChatDeleteHistoryInput, raw: Any) -> ChatDeleteHistoryResult:
    """Row decoder for ``chat.delete_history``; the delete echo carries nothing."""
    del value, raw
    return ChatDeleteHistoryResult()


def encode_chat_configure(value: ChatConfigureInput) -> CodecPayload:
    """Payload for the input-keyed ``chat.configure`` codec row.

    ``action is GET`` reads the settings through the recency-bumping notebook
    read; any other action replaces the settings and tolerates a null echo.
    """
    if value.action is ChatConfigureAction.GET:
        return CodecPayload(
            params=build_get_settings_params(value.notebook_id),
            source_path=f"/notebook/{value.notebook_id}",
        )
    return CodecPayload(
        params=build_configure_params(value),
        source_path=f"/notebook/{value.notebook_id}",
        allow_null=True,
    )


def decode_chat_configure(value: ChatConfigureInput, raw: Any) -> ChatConfigureResult:
    """Row decoder for ``chat.configure``; only the read branch decodes settings."""
    if value.action is ChatConfigureAction.GET:
        return ChatConfigureResult(settings=decode_get_settings_result(raw))
    return ChatConfigureResult()


def encode_chat_save_note(value: ChatSaveNoteInput) -> CodecPayload:
    """Payload for the ``chat.save_note`` codec row (``saved_from_chat`` variant)."""
    return CodecPayload(
        params=build_save_note_params(
            value.notebook_id,
            value.answer,
            value.references,
            value.title,
        ),
        source_path=f"/notebook/{value.notebook_id}",
    )


def decode_chat_save_note(value: ChatSaveNoteInput, raw: Any) -> ChatSaveNoteResult:
    """Row decoder for ``chat.save_note``."""
    return ChatSaveNoteResult(
        note=decode_save_note_result(
            raw,
            notebook_id=value.notebook_id,
            answer=value.answer,
            requested_title=value.title,
        )
    )


__all__ = [
    "build_ask_request",
    "build_configure_params",
    "build_delete_history_params",
    "build_get_conversation_params",
    "build_get_history_params",
    "build_get_settings_params",
    "build_save_note_params",
    "decode_ask_response",
    "decode_chat_configure",
    "decode_chat_delete_history",
    "decode_chat_get_conversation",
    "decode_chat_get_history",
    "decode_chat_save_note",
    "decode_conversation_id_or_warn",
    "decode_get_conversation_result",
    "decode_get_history_result",
    "decode_get_settings_result",
    "decode_save_note_result",
    "encode_chat_configure",
    "encode_ask_conversation_readback",
    "encode_chat_delete_history",
    "encode_chat_get_conversation",
    "encode_chat_get_history",
    "encode_chat_save_note",
]
