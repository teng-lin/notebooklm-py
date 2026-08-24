"""Web workflow bindings for semantic Chat operations."""

from __future__ import annotations

import logging
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

import httpx

from .._backend import BackendContractError, BackendDeadlineExceededError
from .._deadline import RuntimeDeadline
from .._logging import get_request_id, reset_request_id, set_request_id
from .._operations import Operation
from .._records import (
    ChatAskInput,
    ChatAskResultRecord,
    ChatConfigureAction,
    ChatConfigureInput,
    ChatConfigureResult,
    ChatDeleteHistoryInput,
    ChatDeleteHistoryResult,
    ChatGetConversationInput,
    ChatGetConversationResult,
    ChatGetHistoryInput,
    ChatGetHistoryResult,
    ChatSaveNoteInput,
    ChatSaveNoteResult,
)
from ..exceptions import ChatError, NetworkError, NotebookLMError
from ..rpc import RPCMethod
from .codec.chat import (
    build_ask_request,
    build_configure_params,
    build_delete_history_params,
    build_get_conversation_params,
    build_get_history_params,
    build_get_settings_params,
    build_save_note_params,
    decode_ask_response,
    decode_get_conversation_result,
    decode_get_history_result,
    decode_get_settings_result,
    decode_save_note_result,
)
from .source_variants import SourceVariantWebHandlers
from .transport import WebStreamRequest

if TYPE_CHECKING:
    from .._reqid_counter import ReqidCounter
    from .._runtime.transport import RuntimeTransport
    from .transport import WebTransport

chat_logger = logging.getLogger("notebooklm._chat.api")


class ChatWebHandlers(SourceVariantWebHandlers):
    """Reusable Chat handlers mixed into the web backend."""

    _chat_transport: RuntimeTransport | None
    _chat_reqid: ReqidCounter | None
    _chat_timeout: float | None
    _chat_response_max_bytes: int | None
    _transport: WebTransport

    async def _chat_conversation_id(
        self,
        notebook_id: str,
        *,
        operation: Operation,
        deadline: RuntimeDeadline | None,
        outcome_unknown_on_expiry: bool = False,
    ) -> str | None:
        raw = await self._rpc_call(
            RPCMethod.GET_LAST_CONVERSATION_ID,
            build_get_conversation_params(notebook_id),
            operation=operation,
            deadline=deadline,
            source_path=f"/notebook/{notebook_id}",
            outcome_unknown_on_expiry=outcome_unknown_on_expiry,
        )
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

    async def _chat_get_conversation(
        self,
        value: ChatGetConversationInput,
        *,
        deadline: RuntimeDeadline | None,
    ) -> ChatGetConversationResult:
        conversation_id = await self._chat_conversation_id(
            value.notebook_id,
            operation=Operation.CHAT_GET_CONVERSATION,
            deadline=deadline,
        )
        return ChatGetConversationResult(conversation_id)

    async def _chat_get_history(
        self,
        value: ChatGetHistoryInput,
        *,
        deadline: RuntimeDeadline | None,
    ) -> ChatGetHistoryResult:
        raw = await self._rpc_call(
            RPCMethod.GET_CONVERSATION_TURNS,
            build_get_history_params(value.conversation_id, value.limit),
            operation=Operation.CHAT_GET_HISTORY,
            deadline=deadline,
            source_path=f"/notebook/{value.notebook_id}",
        )
        return decode_get_history_result(raw, source="ChatAPI.get_conversation_turns")

    async def _chat_delete_history(
        self,
        value: ChatDeleteHistoryInput,
        *,
        deadline: RuntimeDeadline | None,
    ) -> ChatDeleteHistoryResult:
        await self._rpc_call(
            RPCMethod.DELETE_CONVERSATION,
            build_delete_history_params(value.conversation_id),
            operation=Operation.CHAT_DELETE_HISTORY,
            deadline=deadline,
            source_path=f"/notebook/{value.notebook_id}",
        )
        return ChatDeleteHistoryResult()

    async def _chat_configure(
        self,
        value: ChatConfigureInput,
        *,
        deadline: RuntimeDeadline | None,
    ) -> ChatConfigureResult:
        if value.action is ChatConfigureAction.GET:
            raw = await self._rpc_call(
                RPCMethod.GET_NOTEBOOK,
                build_get_settings_params(value.notebook_id),
                operation=Operation.CHAT_CONFIGURE,
                deadline=deadline,
                source_path=f"/notebook/{value.notebook_id}",
            )
            return ChatConfigureResult(settings=decode_get_settings_result(raw))
        await self._rpc_call(
            RPCMethod.RENAME_NOTEBOOK,
            build_configure_params(value),
            operation=Operation.CHAT_CONFIGURE,
            deadline=deadline,
            source_path=f"/notebook/{value.notebook_id}",
            allow_null=True,
        )
        return ChatConfigureResult()

    async def _chat_save_note(
        self,
        value: ChatSaveNoteInput,
        *,
        deadline: RuntimeDeadline | None,
    ) -> ChatSaveNoteResult:
        raw = await self._rpc_call(
            RPCMethod.CREATE_NOTE,
            build_save_note_params(
                value.notebook_id,
                value.answer,
                value.references,
                value.title,
            ),
            operation=Operation.CHAT_SAVE_NOTE,
            deadline=deadline,
            source_path=f"/notebook/{value.notebook_id}",
            operation_variant="saved_from_chat",
        )
        return ChatSaveNoteResult(
            note=decode_save_note_result(
                raw,
                notebook_id=value.notebook_id,
                answer=value.answer,
                requested_title=value.title,
            )
        )

    async def _chat_ask(
        self,
        value: ChatAskInput,
        *,
        deadline: RuntimeDeadline | None,
    ) -> ChatAskResultRecord:
        """Own streamed phase one and conditional conversation-id phase two."""
        if self._chat_transport is None or self._chat_reqid is None:
            raise BackendContractError(
                "chat.ask requires the composed chat transport and request-id counter",
                operation=Operation.CHAT_ASK,
            )
        reqid = await self._chat_reqid.next_reqid()

        def build_request(snapshot: Any) -> tuple[str, str, dict[str, str]]:
            return build_ask_request(snapshot, value, reqid=reqid)

        attempt_timeout = self._chat_timeout
        if deadline is not None:
            remaining = deadline.remaining()
            if remaining <= 0.0:
                raise BackendDeadlineExceededError(
                    Operation.CHAT_ASK,
                    diagnostics=MappingProxyType(
                        {
                            "timeout": deadline.timeout,
                            "remaining": remaining,
                            "timeout_seconds": deadline.timeout,
                        }
                    ),
                )
            attempt_timeout = (
                remaining if attempt_timeout is None else min(attempt_timeout, remaining)
            )
        reqid_token = None if get_request_id() is not None else set_request_id()
        try:
            try:
                response = await self._transport.stream(
                    WebStreamRequest(
                        operation=Operation.CHAT_ASK,
                        build_request=build_request,
                        parse_label="chat.ask",
                        read_timeout=attempt_timeout,
                    ),
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
                        diagnostics=MappingProxyType(
                            {
                                "timeout": deadline.timeout,
                                "remaining": deadline.remaining(),
                                "timeout_seconds": deadline.timeout,
                            }
                        ),
                    ) from exc
                raise
        finally:
            if reqid_token is not None:
                reset_request_id(reqid_token)

        if deadline is not None and deadline.expired():
            raise BackendDeadlineExceededError(
                Operation.CHAT_ASK,
                outcome_unknown=True,
                diagnostics=MappingProxyType(
                    {
                        "timeout": deadline.timeout,
                        "remaining": deadline.remaining(),
                        "timeout_seconds": deadline.timeout,
                    }
                ),
            )

        streamed = decode_ask_response(response.text)
        resolved_conversation_id = value.resolved_conversation_id
        if resolved_conversation_id is None:
            try:
                resolved_conversation_id = await self._chat_conversation_id(
                    value.notebook_id,
                    operation=Operation.CHAT_ASK,
                    deadline=deadline,
                    outcome_unknown_on_expiry=True,
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


__all__ = ["ChatWebHandlers"]
