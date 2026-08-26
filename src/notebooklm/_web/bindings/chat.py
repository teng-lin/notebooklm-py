"""Chat codec rows (P9.3 chat domain).

Each row is ``encode → one native call → decode``; the :class:`NativeCallSpec`
is the sole authority for the native it dispatches, so the method the policy
ledger audits is the method that runs.  The rows are module-level assignments
because the operation-catalog walker derives execution authorities from them.
``CHAT_CONFIGURE`` is input-keyed: the read branch performs the recency-bumping
notebook read, the mutation branch the settings replacement.

``chat.ask`` is not here.  P10 R2.2 made it a service-owned workflow that
``ChatWorkflowService`` sequences from two leaves — the streamed ``CHAT_STREAM_ANSWER``
row in :mod:`.primitives` and, only when the caller resolved no conversation
id, the ``CHAT_GET_CONVERSATION`` row below.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from ..._binding import Binding, CodecBinding, NativeCallSpec, RpcNative
from ..._operations import Operation
from ..._semantic.records import (
    CHAT_CONFIGURE_DEF,
    CHAT_DELETE_HISTORY_DEF,
    CHAT_GET_CONVERSATION_DEF,
    CHAT_GET_HISTORY_DEF,
    CHAT_SAVE_NOTE_DEF,
    ChatConfigureAction,
    ChatConfigureInput,
)
from ...rpc import RPCMethod
from ..codec import chat as chat_codec

CHAT_GET_CONVERSATION = CodecBinding(
    definition=CHAT_GET_CONVERSATION_DEF,
    encode=chat_codec.encode_chat_get_conversation,
    decode=chat_codec.decode_chat_get_conversation,
    native=NativeCallSpec.constant(RPCMethod.GET_LAST_CONVERSATION_ID),
)

CHAT_GET_HISTORY = CodecBinding(
    definition=CHAT_GET_HISTORY_DEF,
    encode=chat_codec.encode_chat_get_history,
    decode=chat_codec.decode_chat_get_history,
    native=NativeCallSpec.constant(RPCMethod.GET_CONVERSATION_TURNS),
)

CHAT_DELETE_HISTORY = CodecBinding(
    definition=CHAT_DELETE_HISTORY_DEF,
    encode=chat_codec.encode_chat_delete_history,
    decode=chat_codec.decode_chat_delete_history,
    native=NativeCallSpec.constant(RPCMethod.DELETE_CONVERSATION),
)


def _select_configure_native(value: ChatConfigureInput) -> RpcNative[RPCMethod]:
    """``action is GET`` reads through GET_NOTEBOOK; every other action mutates."""
    if value.action is ChatConfigureAction.GET:
        return RpcNative(RPCMethod.GET_NOTEBOOK)
    return RpcNative(RPCMethod.RENAME_NOTEBOOK)


CHAT_CONFIGURE = CodecBinding(
    definition=CHAT_CONFIGURE_DEF,
    encode=chat_codec.encode_chat_configure,
    decode=chat_codec.decode_chat_configure,
    native=NativeCallSpec.keyed(
        _select_configure_native,
        RpcNative(RPCMethod.GET_NOTEBOOK),
        RpcNative(RPCMethod.RENAME_NOTEBOOK),
    ),
)

CHAT_SAVE_NOTE = CodecBinding(
    definition=CHAT_SAVE_NOTE_DEF,
    encode=chat_codec.encode_chat_save_note,
    decode=chat_codec.decode_chat_save_note,
    native=NativeCallSpec.constant(RPCMethod.CREATE_NOTE, "saved_from_chat"),
)

CHAT_ROWS: Mapping[Operation, Binding] = MappingProxyType(
    {
        CHAT_GET_CONVERSATION.definition.key: CHAT_GET_CONVERSATION,
        CHAT_GET_HISTORY.definition.key: CHAT_GET_HISTORY,
        CHAT_DELETE_HISTORY.definition.key: CHAT_DELETE_HISTORY,
        CHAT_CONFIGURE.definition.key: CHAT_CONFIGURE,
        CHAT_SAVE_NOTE.definition.key: CHAT_SAVE_NOTE,
    }
)

__all__ = [
    "CHAT_CONFIGURE",
    "CHAT_DELETE_HISTORY",
    "CHAT_GET_CONVERSATION",
    "CHAT_GET_HISTORY",
    "CHAT_ROWS",
    "CHAT_SAVE_NOTE",
]
