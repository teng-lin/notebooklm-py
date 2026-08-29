"""Private Android implementation of the B5 notebook chat contract."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, NoReturn, cast
from uuid import uuid4

from google.protobuf.empty_pb2 import Empty

from .._chat import ChatAPI, _PostedAsk
from .._conversation_cache import ConversationCache
from .._notebook_metadata import CreatedChatSessionProvider, NotebookSourceIdProvider
from .._runtime.config import DEFAULT_CHAT_TIMEOUT, validate_read_timeout_kwarg
from .._runtime.contracts import LoopGuard
from .._types.enums import ChatGoal, ChatResponseLength
from ..exceptions import ChatResponseParseError
from ..types import ChatReference, ChatSettings, ConversationTurn, Note
from .codecs.chat import decode_document, decode_history, decode_references, decode_turn_key
from .errors import unsupported_operation
from .proto.google.internal.labs.tailwind.orchestration.v1 import (
    b1_read_pb2,
    b3_sources_pb2,
    b5_chat_pb2,
)
from .session import AndroidSession

logger = logging.getLogger("notebooklm._chat.api")
_PROTO = cast(Any, b5_chat_pb2)
_B1_PROTO = cast(Any, b1_read_pb2)
_SOURCES_PROTO = cast(Any, b3_sources_pb2)

_SERVICE = "google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService"
LIST_CHAT_SESSIONS_METHOD = f"/{_SERVICE}/ListChatSessions"
LIST_CHAT_TURNS_METHOD = f"/{_SERVICE}/ListChatTurns"
DELETE_CHAT_TURNS_METHOD = f"/{_SERVICE}/DeleteChatTurns"
GENERATE_FREE_FORM_STREAMED_METHOD = f"/{_SERVICE}/GenerateFreeFormStreamed"


def _new_turn_id() -> str:
    return str(uuid4())


def _reject(operation: str) -> NoReturn:
    unsupported_operation(operation)
    raise AssertionError("unsupported_operation returned")  # pragma: no cover


class AndroidChatAPI(ChatAPI):
    """Direct-test Android chat adapter; intentionally absent from public assembly."""

    def __init__(
        self,
        *,
        session: AndroidSession,
        loop_guard: LoopGuard,
        notebooks: NotebookSourceIdProvider,
        chat_timeout: float | None = DEFAULT_CHAT_TIMEOUT,
        turn_id_factory: Callable[[], str] = _new_turn_id,
        conversation_cache: ConversationCache | None = None,
        created_chat_sessions: CreatedChatSessionProvider | None = None,
    ) -> None:
        self._transport = session
        self._chat_timeout = validate_read_timeout_kwarg(chat_timeout, name="chat_timeout")
        self._turn_id_factory = turn_id_factory
        super().__init__(
            loop_guard=loop_guard,
            notebooks=notebooks,
            conversation_cache=conversation_cache,
            created_chat_sessions=created_chat_sessions,
        )

    async def get_conversation_id(self, notebook_id: str) -> str | None:
        """Return the first (current) chat session volunteered for a project."""
        response = await self._transport.unary(
            LIST_CHAT_SESSIONS_METHOD,
            _PROTO.ListChatSessionsRequest(project_id=notebook_id),
            replay_safe=True,
            response_type=_PROTO.ListChatSessionsResponse,
        )
        if not response.sessions:
            return None
        return response.sessions[0].chat_session_id or None

    async def get_conversation_turns(
        self,
        notebook_id: str,
        conversation_id: str,
        limit: int = 2,
    ) -> Any:
        """Return the raw first ListChatTurns protobuf page.

        The Android request has no limit field. ``limit`` remains in the
        backend-neutral signature and is applied only by decoded consumers.
        """
        del notebook_id, limit
        return await self._transport.unary(
            LIST_CHAT_TURNS_METHOD,
            _PROTO.ListChatTurnsRequest(chat_session_id=conversation_id),
            replay_safe=True,
            response_type=_PROTO.ListChatTurnsResponse,
        )

    async def get_history(
        self,
        notebook_id: str,
        limit: int = 100,
        conversation_id: str | None = None,
    ) -> list[tuple[str, str]]:
        """Return captured newest-first Android history as oldest-first pairs."""
        resolved_id = conversation_id or await self.get_conversation_id(notebook_id)
        if not resolved_id:
            return []
        response = await self.get_conversation_turns(
            notebook_id,
            resolved_id,
            limit=limit,
        )
        return decode_history(response, limit=limit)

    async def _list_turn_roles(
        self,
        notebook_id: str,
        conversation_id: str,
        limit: int,
    ) -> list[object]:
        response = await self.get_conversation_turns(
            notebook_id,
            conversation_id,
            limit=limit,
        )
        return [1] * min(len(response.chat_turns), max(0, limit))

    @staticmethod
    def _conversation_history(cached_turns: list[ConversationTurn]) -> list[Any]:
        events: list[Any] = []
        for turn in cached_turns:
            events.extend(
                (
                    _PROTO.ConversationEvent(
                        text=turn.answer,
                        type=_PROTO.ConversationEvent.GENERATED_RESPONSE,
                    ),
                    _PROTO.ConversationEvent(
                        text=turn.query,
                        type=_PROTO.ConversationEvent.USER_QUERY,
                    ),
                )
            )
        return events

    async def _stream_answer(
        self,
        *,
        notebook_id: str,
        question: str,
        source_ids: list[str],
        cached_turns: list[ConversationTurn],
        conversation_id: str | None,
    ) -> _PostedAsk:
        turn_id = self._turn_id_factory()
        if not isinstance(turn_id, str) or not turn_id:
            raise ValueError("turn_id_factory must return a non-empty string")

        request = _PROTO.GenerateFreeFormStreamedRequest(
            sources=[
                _SOURCES_PROTO.InputSource(source_id=_B1_PROTO.SourceId(id=source_id))
                for source_id in source_ids
            ],
            user_query=question,
            conversation_history=self._conversation_history(cached_turns),
            chat_session_id=conversation_id or "",
            user_message_id=turn_id,
            project_id=notebook_id,
            origin=_PROTO.QUERY_ORIGIN_CHAT_TEXT_BOX,
        )

        final_response = None
        async for response in self._transport.stream(
            GENERATE_FREE_FORM_STREAMED_METHOD,
            request,
            timeout=self._chat_timeout,
            response_type=_PROTO.GenerateFreeFormStreamedResponse,
            telemetry_method=None,
        ):
            if response.is_final_response:
                final_response = response

        if final_response is None:
            raise ChatResponseParseError(
                "Android GenerateFreeFormStreamed ended before response field 5 "
                "declared a final snapshot."
            )

        answer = final_response.answer
        answer_document = decode_document(answer.response_doc)
        references = decode_references(answer.response_doc, answer_document)
        return _PostedAsk(
            answer=answer.response,
            references=references,
            conversation_id=conversation_id,
            raw_response="",
            answer_document=answer_document,
            turn_key=decode_turn_key(answer),
            next_steps=[],
        )

    async def _send_delete_conversation(
        self,
        notebook_id: str,
        conversation_id: str,
    ) -> None:
        del notebook_id
        await self._transport.unary(
            DELETE_CHAT_TURNS_METHOD,
            _PROTO.DeleteChatTurnsRequest(
                chat_session_id=conversation_id,
                delete_all_history=True,
            ),
            replay_safe=False,
            response_type=Empty,
        )

    async def configure(
        self,
        notebook_id: str,
        goal: ChatGoal | None = None,
        response_length: ChatResponseLength | None = None,
        custom_prompt: str | None = None,
    ) -> None:
        _reject("chat.configure")

    async def get_settings(self, notebook_id: str) -> ChatSettings:
        _reject("chat.get_settings")

    async def _send_note(
        self,
        *,
        notebook_id: str,
        answer_text: str,
        references: list[ChatReference],
        title: str,
        clean_answer: str,
        citation_anchors: list[tuple[ChatReference, int]],
    ) -> Note:
        _reject("chat.save_answer_as_note")


__all__ = [
    "AndroidChatAPI",
    "DELETE_CHAT_TURNS_METHOD",
    "GENERATE_FREE_FORM_STREAMED_METHOD",
    "LIST_CHAT_SESSIONS_METHOD",
    "LIST_CHAT_TURNS_METHOD",
]
