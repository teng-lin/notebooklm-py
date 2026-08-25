"""Chat codec rows (P9.3 chat domain).

Each row is ``encode → one native call → decode``; the :class:`NativeCallSpec`
is the sole authority for the native it dispatches, so the method the policy
ledger audits is the method that runs.  The rows are module-level assignments
because the operation-catalog walker derives execution authorities from them.
``CHAT_CONFIGURE`` is input-keyed: the read branch performs the recency-bumping
notebook read, the mutation branch the settings replacement.

``CHAT_ASK`` (P9.4) is the *protocol* :class:`CustomBinding` row: the wire forces
the sequence — a streamed ``GenerateFreeFormStreamed`` phase, then a conditional
``GET_LAST_CONVERSATION_ID`` read when the server issued no conversation id.
The streamed verb is declared as the row's ``StreamSpec`` (it is not a native
method and never enters the policy ledger's native set); the row reaches the
request-id counter and the configured chat read timeout only through its
declared collaborators, never the transport.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

import httpx

from ..._backend import BackendContractError, BackendDeadlineExceededError
from ..._binding import (
    Binding,
    CodecBinding,
    CustomBinding,
    ErrorMode,
    NativeCallSpec,
    NativeChoice,
    RowInvoker,
    StreamPayload,
    StreamSpec,
)
from ..._deadline import RuntimeDeadline
from ..._logging import get_request_id, reset_request_id, set_request_id
from ..._operations import Operation
from ..._records import (
    CHAT_ASK_DEF,
    CHAT_CONFIGURE_DEF,
    CHAT_DELETE_HISTORY_DEF,
    CHAT_GET_CONVERSATION_DEF,
    CHAT_GET_HISTORY_DEF,
    CHAT_SAVE_NOTE_DEF,
    ChatAskInput,
    ChatAskResultRecord,
    ChatConfigureAction,
    ChatConfigureInput,
)
from ...exceptions import ChatError, NetworkError, NotebookLMError
from ...rpc import RPCMethod
from ..codec import chat as chat_codec

chat_logger = logging.getLogger("notebooklm._chat.api")

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


def _select_configure_native(value: ChatConfigureInput) -> NativeChoice[RPCMethod]:
    """``action is GET`` reads through GET_NOTEBOOK; every other action mutates."""
    if value.action is ChatConfigureAction.GET:
        return NativeChoice(RPCMethod.GET_NOTEBOOK)
    return NativeChoice(RPCMethod.RENAME_NOTEBOOK)


CHAT_CONFIGURE = CodecBinding(
    definition=CHAT_CONFIGURE_DEF,
    encode=chat_codec.encode_chat_configure,
    decode=chat_codec.decode_chat_configure,
    native=NativeCallSpec.keyed(
        _select_configure_native,
        NativeChoice(RPCMethod.GET_NOTEBOOK),
        NativeChoice(RPCMethod.RENAME_NOTEBOOK),
    ),
)

CHAT_SAVE_NOTE = CodecBinding(
    definition=CHAT_SAVE_NOTE_DEF,
    encode=chat_codec.encode_chat_save_note,
    decode=chat_codec.decode_chat_save_note,
    native=NativeCallSpec.constant(RPCMethod.CREATE_NOTE, "saved_from_chat"),
)

# --- chat.ask: the protocol custom row --------------------------------------

_ASK = "ask"
_CONVERSATION = "conversation"


def _deadline_diagnostics(
    deadline: RuntimeDeadline, remaining: float
) -> MappingProxyType[str, Any]:
    return MappingProxyType(
        {
            "timeout": deadline.timeout,
            "remaining": remaining,
            "timeout_seconds": deadline.timeout,
        }
    )


async def _ask(
    value: ChatAskInput,
    deadline: RuntimeDeadline | None,
    invoke: RowInvoker,
) -> ChatAskResultRecord:
    """Own streamed phase one and the conditional conversation-id phase two."""
    reqid_counter = invoke.collaborator("chat_reqid")
    if not invoke.collaborator("chat_transport_composed") or reqid_counter is None:
        raise BackendContractError(
            "chat.ask requires the composed chat transport and request-id counter",
            operation=Operation.CHAT_ASK,
        )
    reqid = await reqid_counter.next_reqid()

    def build_request(snapshot: Any) -> tuple[str, str, dict[str, str]]:
        return chat_codec.build_ask_request(snapshot, value, reqid=reqid)

    attempt_timeout: float | None = invoke.collaborator("chat_timeout")
    if deadline is not None:
        remaining = deadline.remaining()
        if remaining <= 0.0:
            raise BackendDeadlineExceededError(
                Operation.CHAT_ASK,
                diagnostics=_deadline_diagnostics(deadline, remaining),
            )
        attempt_timeout = remaining if attempt_timeout is None else min(attempt_timeout, remaining)
    reqid_token = None if get_request_id() is not None else set_request_id()
    try:
        try:
            response = await invoke.stream(
                _ASK,
                StreamPayload(build_request=build_request, attempt_timeout=attempt_timeout),
                value=value,
                deadline=deadline,
            )
        except NetworkError as exc:
            if (
                deadline is not None
                and deadline.expired()
                and isinstance(exc.original_error, httpx.TimeoutException)
            ):
                raise BackendDeadlineExceededError(
                    Operation.CHAT_ASK,
                    outcome_unknown=True,
                    diagnostics=_deadline_diagnostics(deadline, deadline.remaining()),
                ) from exc
            raise
    finally:
        if reqid_token is not None:
            reset_request_id(reqid_token)

    if deadline is not None and deadline.expired():
        raise BackendDeadlineExceededError(
            Operation.CHAT_ASK,
            outcome_unknown=True,
            diagnostics=_deadline_diagnostics(deadline, deadline.remaining()),
        )

    streamed = chat_codec.decode_ask_response(response.text)
    resolved_conversation_id = value.resolved_conversation_id
    if resolved_conversation_id is None:
        try:
            raw = await invoke.call(
                _CONVERSATION,
                chat_codec.encode_ask_conversation_readback(value.notebook_id),
                value=value,
                deadline=deadline,
                outcome_unknown_on_expiry=True,
            )
            resolved_conversation_id = chat_codec.decode_conversation_id_or_warn(
                raw, notebook_id=value.notebook_id
            )
        except NotebookLMError:
            chat_logger.error(
                "Chat ask succeeded but post-ask get_conversation_id failed. "
                "Answer (%d chars, may be truncated): %r",
                len(streamed.answer or ""),
                (streamed.answer or "")[:500],
            )
            raise
        if resolved_conversation_id is None:
            if streamed.answer:
                chat_logger.error(
                    "Server returned a non-empty answer but hPTbtc returned no "
                    "conversation_id (%d chars). Answer preview: %r",
                    len(streamed.answer),
                    streamed.answer[:500],
                )
            raise ChatError(
                "Server did not register a conversation for this ask (hPTbtc returned no "
                "id). The response may have been empty, or the API shape may have changed. "
                "Please file an issue at https://github.com/teng-lin/notebooklm-py/issues."
            )

    return ChatAskResultRecord(
        answer=streamed.answer,
        conversation_id=resolved_conversation_id,
        references=streamed.references,
        raw_response=response.text[:1000],
        answer_document=streamed.answer_document,
        turn_key=streamed.turn_key,
        next_steps=streamed.next_steps,
    )


CHAT_ASK = CustomBinding(
    definition=CHAT_ASK_DEF,
    handler=_ask,
    native=(NativeCallSpec.constant(RPCMethod.GET_LAST_CONVERSATION_ID, key=_CONVERSATION),),
    streams=(StreamSpec(key=_ASK, label="chat.ask"),),
    justification=(
        "Protocol: the wire forces the sequence — the conversation-id fetch follows the "
        "streamed answer and only when the server issued no id."
    ),
    category="protocol",
    error_mode=ErrorMode.TRANSLATE_SCRUBBED,
    collaborators=("chat_reqid", "chat_timeout", "chat_transport_composed"),
)


CHAT_ROWS: Mapping[Operation, Binding] = MappingProxyType(
    {
        CHAT_ASK.definition.key: CHAT_ASK,
        CHAT_GET_CONVERSATION.definition.key: CHAT_GET_CONVERSATION,
        CHAT_GET_HISTORY.definition.key: CHAT_GET_HISTORY,
        CHAT_DELETE_HISTORY.definition.key: CHAT_DELETE_HISTORY,
        CHAT_CONFIGURE.definition.key: CHAT_CONFIGURE,
        CHAT_SAVE_NOTE.definition.key: CHAT_SAVE_NOTE,
    }
)

__all__ = [
    "CHAT_ASK",
    "CHAT_CONFIGURE",
    "CHAT_DELETE_HISTORY",
    "CHAT_GET_CONVERSATION",
    "CHAT_GET_HISTORY",
    "CHAT_ROWS",
    "CHAT_SAVE_NOTE",
]
