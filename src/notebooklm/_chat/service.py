"""Transport-neutral semantic service for the six chat operations.

``ask`` is the one workflow here: P10 R2.2 made ``chat.ask`` service-owned, so
this module sequences the streamed answer leaf and — only when the caller could
not resolve a conversation id itself — the conversation-id readback leaf.  The
backend refuses ``CHAT_ASK_DEF`` directly; the definition survives as the
operation this service's failures are attributed to.
"""

from __future__ import annotations

import logging
from types import MappingProxyType

from .._backend import (
    BackendAdapter,
    BackendDeadlineExceededError,
    BackendError,
    BackendErrorReason,
    mark_backend_outcome_unknown,
    rebind_operation,
    require_leaves,
)
from .._deadline import RuntimeDeadline
from .._records import (
    CHAT_ASK_DEF,
    CHAT_CONFIGURE_DEF,
    CHAT_DELETE_HISTORY_DEF,
    CHAT_GET_CONVERSATION_DEF,
    CHAT_GET_HISTORY_DEF,
    CHAT_SAVE_NOTE_DEF,
    CHAT_STREAM_ANSWER_DEF,
    ChatAskInput,
    ChatAskResultRecord,
    ChatConfigureInput,
    ChatConfigureResult,
    ChatDeleteHistoryInput,
    ChatGetConversationInput,
    ChatGetHistoryInput,
    ChatGetHistoryResult,
    ChatSaveNoteInput,
    ChatSaveNoteResult,
    ChatStreamAnswerInput,
    ChatStreamAnswerRecord,
)

# The conversation-id diagnostics predate the service and stay pinned under the
# chat facade's logger name.
chat_logger = logging.getLogger("notebooklm._chat.api")

_NO_CONVERSATION_REGISTERED = (
    "Server did not register a conversation for this ask (hPTbtc returned no "
    "id). The response may have been empty, or the API shape may have changed. "
    "Please file an issue at https://github.com/teng-lin/notebooklm-py/issues."
)


class ChatService:
    """Invoke typed chat operations without naming web RPC or transport vocabulary."""

    __slots__ = ("_backend",)

    def __init__(self, backend: BackendAdapter) -> None:
        self._backend = backend

    async def ask(
        self,
        value: ChatAskInput,
        *,
        deadline: RuntimeDeadline | None = None,
    ) -> ChatAskResultRecord:
        """Stream one answer, then resolve the conversation it was recorded in.

        The stream never returns a usable conversation id, so a caller that did
        not already know one pays for a readback.  Every leaf failure is
        re-raised as ``chat.ask`` — the workflow, not the leaf, is the operation
        a caller asked for — and a readback that expires after an accepted POST
        additionally reports an unknown outcome, since the turn is then recorded
        but undiscoverable.
        """
        require_leaves(self._backend, CHAT_STREAM_ANSWER_DEF.key, CHAT_GET_CONVERSATION_DEF.key)
        try:
            streamed = await self._backend.invoke(
                CHAT_STREAM_ANSWER_DEF,
                ChatStreamAnswerInput(
                    notebook_id=value.notebook_id,
                    question=value.question,
                    source_ids=value.source_ids,
                    conversation_history=value.conversation_history,
                    post_conversation_id=value.post_conversation_id,
                ),
                deadline=deadline,
            )
        except BackendError as error:
            raise self._as_ask_failure(error) from error.__cause__
        answer = streamed.answer
        conversation_id = value.resolved_conversation_id
        if conversation_id is None:
            conversation_id = await self._read_back_conversation_id(
                value.notebook_id, answer, deadline=deadline
            )
        return ChatAskResultRecord(
            answer=answer.answer,
            conversation_id=conversation_id,
            references=answer.references,
            raw_response=streamed.raw_response,
            answer_document=answer.answer_document,
            turn_key=answer.turn_key,
            next_steps=answer.next_steps,
        )

    async def _read_back_conversation_id(
        self,
        notebook_id: str,
        answer: ChatStreamAnswerRecord,
        *,
        deadline: RuntimeDeadline | None,
    ) -> str:
        """Resolve the id the accepted stream was recorded under, or fail closed."""
        try:
            result = await self._backend.invoke(
                CHAT_GET_CONVERSATION_DEF,
                ChatGetConversationInput(notebook_id),
                deadline=deadline,
            )
        except BackendError as error:
            chat_logger.error(
                "Chat ask succeeded but post-ask get_conversation_id failed. "
                "Answer (%d chars, may be truncated): %r",
                len(answer.answer or ""),
                (answer.answer or "")[:500],
            )
            # The stream was accepted before this read; a *pre-dispatch* expiry
            # on the readback therefore still leaves a turn the caller cannot
            # discover.  Only the deadline family gains that marking — every
            # other readback failure keeps the uncertainty the leaf reported.
            if isinstance(error, BackendDeadlineExceededError):
                error = mark_backend_outcome_unknown(error)
            raise self._as_ask_failure(error) from error.__cause__
        if result.conversation_id is None:
            if answer.answer:
                chat_logger.error(
                    "Server returned a non-empty answer but hPTbtc returned no "
                    "conversation_id (%d chars). Answer preview: %r",
                    len(answer.answer),
                    answer.answer[:500],
                )
            raise BackendError(
                message=_NO_CONVERSATION_REGISTERED,
                operation=CHAT_ASK_DEF.key,
                # The message is the whole evidence: the compatibility projector
                # rebuilds the ``ChatError`` the retired row raised from it, and
                # there is no wire failure to describe — the read succeeded and
                # simply carried no id.
                diagnostics=MappingProxyType({}),
                reason=BackendErrorReason.CHAT,
            )
        return result.conversation_id

    @staticmethod
    def _as_ask_failure(error: BackendError) -> BackendError:
        """Attribute one leaf failure to the workflow that sequenced it."""
        if error.operation is CHAT_ASK_DEF.key:
            return error
        return rebind_operation(error, CHAT_ASK_DEF.key)

    async def get_conversation_id(
        self,
        notebook_id: str,
        *,
        deadline: RuntimeDeadline | None = None,
    ) -> str | None:
        result = await self._backend.invoke(
            CHAT_GET_CONVERSATION_DEF,
            ChatGetConversationInput(notebook_id),
            deadline=deadline,
        )
        return result.conversation_id

    async def get_history(
        self,
        notebook_id: str,
        conversation_id: str,
        *,
        limit: int = 2,
        deadline: RuntimeDeadline | None = None,
    ) -> ChatGetHistoryResult:
        return await self._backend.invoke(
            CHAT_GET_HISTORY_DEF,
            ChatGetHistoryInput(notebook_id, conversation_id, limit),
            deadline=deadline,
        )

    async def delete_history(
        self,
        notebook_id: str,
        conversation_id: str,
        *,
        deadline: RuntimeDeadline | None = None,
    ) -> None:
        await self._backend.invoke(
            CHAT_DELETE_HISTORY_DEF,
            ChatDeleteHistoryInput(notebook_id, conversation_id),
            deadline=deadline,
        )

    async def configure(
        self,
        value: ChatConfigureInput,
        *,
        deadline: RuntimeDeadline | None = None,
    ) -> ChatConfigureResult:
        return await self._backend.invoke(CHAT_CONFIGURE_DEF, value, deadline=deadline)

    async def save_note(
        self,
        value: ChatSaveNoteInput,
        *,
        deadline: RuntimeDeadline | None = None,
    ) -> ChatSaveNoteResult:
        return await self._backend.invoke(CHAT_SAVE_NOTE_DEF, value, deadline=deadline)


__all__ = ["ChatService"]
