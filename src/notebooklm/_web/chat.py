"""Web workflow binding for the two-phase ``chat.ask`` composite.

Since P9.3 the five unary chat leaves are codec rows in ``_web/bindings/chat.py``;
this mixin keeps only ``chat.ask`` (streamed phase plus the conditional
conversation-id read) and the conversation-id helper that phase shares with
the ``chat.get_conversation`` row's decoder.
"""

from __future__ import annotations

import logging
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

import httpx

from .._backend import BackendContractError, BackendDeadlineExceededError
from .._deadline import RuntimeDeadline
from .._logging import get_request_id, reset_request_id, set_request_id
from .._operations import Operation
from .._records import ChatAskInput, ChatAskResultRecord
from ..exceptions import ChatError, NetworkError, NotebookLMError
from ..rpc import RPCMethod
from .codec.chat import (
    build_ask_request,
    build_get_conversation_params,
    decode_ask_response,
    decode_conversation_id_or_warn,
)
from .source_variants import SourceVariantWebHandlers
from .transport import WebStreamRequest

if TYPE_CHECKING:
    from .._reqid_counter import ReqidCounter
    from .._runtime.transport import RuntimeTransport
    from .transport import WebTransport

chat_logger = logging.getLogger("notebooklm._chat.api")


class ChatWebHandlers(SourceVariantWebHandlers):
    """The ``chat.ask`` composite handler mixed into the web backend."""

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
        return decode_conversation_id_or_warn(raw, notebook_id=notebook_id)

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
