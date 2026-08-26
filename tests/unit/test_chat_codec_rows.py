"""Goldens for the P9.3 chat row-facing codecs.

Each row payload is the params array the retired handler built plus the exact
route and option flags it passed to ``_rpc_call``; the decoders delegate to the
recorded projections.  These pin that the move from handler body to codec row
changed no byte of any request.
"""

from __future__ import annotations

from typing import Any

import pytest

from notebooklm._binding import CodecPayload
from notebooklm._semantic.records import (
    ChatConfigureAction,
    ChatConfigureInput,
    ChatDeleteHistoryInput,
    ChatGetConversationInput,
    ChatGetHistoryInput,
    ChatReferenceRecord,
    ChatSaveNoteInput,
)
from notebooklm._web.codec.chat import (
    build_configure_params,
    build_delete_history_params,
    build_get_conversation_params,
    build_get_history_params,
    build_get_settings_params,
    build_save_note_params,
    decode_chat_configure,
    decode_chat_delete_history,
    decode_chat_get_conversation,
    decode_chat_get_history,
    decode_chat_save_note,
    decode_get_history_result,
    decode_get_settings_result,
    decode_save_note_result,
    encode_chat_configure,
    encode_chat_delete_history,
    encode_chat_get_conversation,
    encode_chat_get_history,
    encode_chat_save_note,
)

_NB = "nb-9"
_CONV = "conv-9"


def _reference() -> ChatReferenceRecord:
    return ChatReferenceRecord(
        source_id="src-1",
        citation_number=1,
        cited_text="cited",
        start_char=0,
        end_char=5,
        chunk_id="chunk-1",
    )


def test_row_payloads_match_the_handler_kwargs_golden() -> None:
    conversation = encode_chat_get_conversation(ChatGetConversationInput(_NB))
    history = encode_chat_get_history(ChatGetHistoryInput(_NB, _CONV, limit=7))
    delete = encode_chat_delete_history(ChatDeleteHistoryInput(_NB, _CONV))
    read = encode_chat_configure(ChatConfigureInput(_NB, ChatConfigureAction.GET))
    write = encode_chat_configure(
        ChatConfigureInput(_NB, ChatConfigureAction.SET, goal="default", response_length="longer")
    )
    save = encode_chat_save_note(ChatSaveNoteInput(_NB, "answer [1]", (_reference(),), "Title"))

    assert conversation == CodecPayload(params=[[], None, _NB, 1], source_path=f"/notebook/{_NB}")
    assert conversation.params == build_get_conversation_params(_NB)
    assert history == CodecPayload(
        params=[[], None, None, _CONV, 7], source_path=f"/notebook/{_NB}"
    )
    assert history.params == build_get_history_params(_CONV, 7)
    assert delete == CodecPayload(params=[[], _CONV, None, 1], source_path=f"/notebook/{_NB}")
    assert delete.params == build_delete_history_params(_CONV)
    assert read == CodecPayload(
        params=build_get_settings_params(_NB), source_path=f"/notebook/{_NB}"
    )
    assert read.allow_null is False
    assert write == CodecPayload(
        params=[_NB, [[None, None, None, None, None, None, None, [[1], [4]]]]],
        source_path=f"/notebook/{_NB}",
        allow_null=True,
    )
    assert write.params == build_configure_params(
        ChatConfigureInput(_NB, ChatConfigureAction.SET, goal="default", response_length="longer")
    )
    assert save == CodecPayload(
        params=build_save_note_params(_NB, "answer [1]", (_reference(),), "Title"),
        source_path=f"/notebook/{_NB}",
    )
    for payload in (conversation, history, delete, read, save):
        assert (payload.raise_on_null_status, payload.attempt_timeout) == (False, None)


def test_configure_write_payload_still_rejects_incomplete_settings_before_the_wire() -> None:
    with pytest.raises(ValueError):
        encode_chat_configure(ChatConfigureInput(_NB, ChatConfigureAction.SET, goal="default"))


def test_row_decoders_delegate_to_the_recorded_projections() -> None:
    history_raw: Any = [[["q-turn", None, 1]]]
    settings_raw: Any = [[None, None, None, None, None, None, None, [[3], [4]]]]
    note_raw: Any = [["note-1", ["Title", None, None, None, None, [1, 0]]]]
    save_input = ChatSaveNoteInput(_NB, "answer [1]", (_reference(),), "Title")

    assert (
        decode_chat_get_conversation(ChatGetConversationInput(_NB), [[[_CONV]]]).conversation_id
        == _CONV
    )
    assert decode_chat_get_conversation(ChatGetConversationInput(_NB), []).conversation_id is None
    assert decode_chat_get_history(
        ChatGetHistoryInput(_NB, _CONV), history_raw
    ) == decode_get_history_result(history_raw, source="ChatAPI.get_conversation_turns")
    assert decode_chat_delete_history(
        ChatDeleteHistoryInput(_NB, _CONV), None
    ).__class__.__name__ == ("ChatDeleteHistoryResult")
    read = decode_chat_configure(ChatConfigureInput(_NB, ChatConfigureAction.GET), settings_raw)
    assert read.settings == decode_get_settings_result(settings_raw)
    write = decode_chat_configure(
        ChatConfigureInput(_NB, ChatConfigureAction.SET, goal="default", response_length="longer"),
        None,
    )
    assert write.settings is None
    assert decode_chat_save_note(save_input, note_raw).note == decode_save_note_result(
        note_raw, notebook_id=_NB, answer="answer [1]", requested_title="Title"
    )
