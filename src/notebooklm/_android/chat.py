"""Android implementation of the public notebook chat contract."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, cast
from uuid import uuid4

from .._chat import ChatAPI, _PostedAsk
from .._conversation_cache import ConversationCache
from .._notebook_metadata import CreatedChatSessionProvider, NotebookSourceIdProvider
from .._runtime.config import DEFAULT_CHAT_TIMEOUT, validate_read_timeout_kwarg
from .._runtime.contracts import LoopGuard
from .._types.enums import ChatGoal, ChatResponseLength
from ..exceptions import ChatResponseParseError, UnknownRPCMethodError, ValidationError
from ..types import ChatReference, ChatSettings, ConversationTurn, Note
from .codecs.chat import decode_document, decode_history, decode_references, decode_turn_key
from .notes import SAVED_RESPONSE_NOTE_TYPE, create_note
from .session import AndroidSession

logger = logging.getLogger("notebooklm._chat.api")
_SERVICE = "google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService"
LIST_CHAT_SESSIONS_METHOD = f"/{_SERVICE}/ListChatSessions"
LIST_CHAT_TURNS_METHOD = f"/{_SERVICE}/ListChatTurns"
DELETE_CHAT_TURNS_METHOD = f"/{_SERVICE}/DeleteChatTurns"
GENERATE_FREE_FORM_STREAMED_METHOD = f"/{_SERVICE}/GenerateFreeFormStreamed"
GET_PROJECT_METHOD = f"/{_SERVICE}/GetProject"
MUTATE_PROJECT_METHOD = f"/{_SERVICE}/MutateProject"


def _proto() -> Any:
    from .proto.google.internal.labs.tailwind.orchestration.v1 import chat_pb2

    return cast(Any, chat_pb2)


def _read_proto() -> Any:
    from .proto.google.internal.labs.tailwind.orchestration.v1 import read_pb2

    return cast(Any, read_pb2)


def _sources_proto() -> Any:
    from .proto.google.internal.labs.tailwind.orchestration.v1 import sources_pb2

    return cast(Any, sources_pb2)


def _wire_proto() -> Any:
    from .proto.notebooklm.internal.android.wire.v1 import notebooks_pb2

    return cast(Any, notebooks_pb2)


def _empty_type() -> Any:
    from google.protobuf.empty_pb2 import Empty

    return Empty


def _new_turn_id() -> str:
    return str(uuid4())


class AndroidChatAPI(ChatAPI):
    """Android chat adapter installed by public Android backend selection."""

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
        proto = _proto()
        response = await self._transport.unary(
            LIST_CHAT_SESSIONS_METHOD,
            proto.ListChatSessionsRequest(project_id=notebook_id),
            replay_safe=True,
            response_type=proto.ListChatSessionsResponse,
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
        proto = _proto()
        return await self._transport.unary(
            LIST_CHAT_TURNS_METHOD,
            proto.ListChatTurnsRequest(chat_session_id=conversation_id),
            replay_safe=True,
            response_type=proto.ListChatTurnsResponse,
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
        return [turn.observed_event_type for turn in response.chat_turns[: max(0, limit)]]

    @staticmethod
    def _conversation_history(cached_turns: list[ConversationTurn]) -> list[Any]:
        proto = _proto()
        events: list[Any] = []
        for turn in cached_turns:
            events.extend(
                (
                    proto.ConversationEvent(
                        text=turn.answer,
                        type=proto.ConversationEvent.GENERATED_RESPONSE,
                    ),
                    proto.ConversationEvent(
                        text=turn.query,
                        type=proto.ConversationEvent.USER_QUERY,
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

        proto = _proto()
        read_proto = _read_proto()
        sources_proto = _sources_proto()
        request = proto.GenerateFreeFormStreamedRequest(
            sources=[
                sources_proto.InputSource(source_id=read_proto.SourceId(id=source_id))
                for source_id in source_ids
            ],
            user_query=question,
            conversation_history=self._conversation_history(cached_turns),
            chat_session_id=conversation_id or "",
            user_message_id=turn_id,
            project_id=notebook_id,
            origin=proto.QUERY_ORIGIN_CHAT_TEXT_BOX,
        )

        final_response = None
        async for response in self._transport.stream(
            GENERATE_FREE_FORM_STREAMED_METHOD,
            request,
            timeout=self._chat_timeout,
            response_type=proto.GenerateFreeFormStreamedResponse,
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
        proto = _proto()
        await self._transport.unary(
            DELETE_CHAT_TURNS_METHOD,
            proto.DeleteChatTurnsRequest(
                chat_session_id=conversation_id,
                delete_all_history=True,
            ),
            replay_safe=False,
            response_type=_empty_type(),
        )

    async def configure(
        self,
        notebook_id: str,
        goal: ChatGoal | None = None,
        response_length: ChatResponseLength | None = None,
        custom_prompt: str | None = None,
    ) -> None:
        if goal is None:
            goal = ChatGoal.DEFAULT
        if response_length is None:
            response_length = ChatResponseLength.DEFAULT
        if goal == ChatGoal.CUSTOM and not custom_prompt:
            raise ValidationError("custom_prompt is required when goal is CUSTOM")
        active_prompt = custom_prompt if goal == ChatGoal.CUSTOM else ""

        from .upload import android_request_context

        wire = _wire_proto()
        read_proto = _read_proto()
        await self._transport.unary(
            MUTATE_PROJECT_METHOD,
            wire.WireMutateProjectRequest(
                project_id=notebook_id,
                mutations=[
                    wire.WireProjectMutation(
                        advanced_settings=wire.WireProjectAdvancedSettings(
                            goal_settings=wire.WireProjectGoalSettings(
                                goal=goal.value,
                                custom_prompt=active_prompt,
                            ),
                            response_style_settings=wire.WireProjectResponseStyleSettings(
                                response_length=response_length.value,
                            ),
                        )
                    )
                ],
                request_context=android_request_context(),
            ),
            replay_safe=False,
            response_type=read_proto.Project,
        )

    async def get_settings(self, notebook_id: str) -> ChatSettings:
        read_proto = _read_proto()
        wire = _wire_proto()
        response = await self._transport.unary(
            GET_PROJECT_METHOD,
            read_proto.GetProjectRequest(
                project_id=notebook_id,
                include_audio_overview_ids=True,
            ),
            replay_safe=True,
            response_type=wire.WireGetProjectResponse,
        )
        if not response.HasField("project"):
            raise UnknownRPCMethodError(
                "Android GetProject response omitted its project",
                method_id=GET_PROJECT_METHOD,
                path=(0,),
                source="AndroidChatAPI.get_settings",
            )
        project = response.project
        if not project.HasField("advanced_settings"):
            return ChatSettings(
                goal=ChatGoal.DEFAULT,
                response_length=ChatResponseLength.DEFAULT,
            )

        settings = project.advanced_settings
        if not settings.HasField("goal_settings") or not settings.HasField(
            "response_style_settings"
        ):
            raise UnknownRPCMethodError(
                "Android GetProject returned a partial chat-settings block",
                method_id=GET_PROJECT_METHOD,
                path=(0, 7),
                source="AndroidChatAPI.get_settings",
            )
        goal_code = settings.goal_settings.goal
        response_length_code = settings.response_style_settings.response_length
        try:
            goal = ChatGoal(goal_code)
            response_length = ChatResponseLength(response_length_code)
        except ValueError as exc:
            raise UnknownRPCMethodError(
                "unknown Android chat-settings enum code "
                f"(goal={goal_code!r}, response_length={response_length_code!r})",
                method_id=GET_PROJECT_METHOD,
                path=(0, 7),
                source="AndroidChatAPI.get_settings",
                data_at_failure=(goal_code, response_length_code),
            ) from exc
        custom_prompt = settings.goal_settings.custom_prompt or None
        if goal == ChatGoal.CUSTOM and custom_prompt is None:
            raise UnknownRPCMethodError(
                "Android GetProject returned CUSTOM chat settings without a prompt",
                method_id=GET_PROJECT_METHOD,
                path=(0, 7, 0, 1),
                source="AndroidChatAPI.get_settings",
            )
        if goal != ChatGoal.CUSTOM:
            custom_prompt = None
        return ChatSettings(
            goal=goal,
            response_length=response_length,
            custom_prompt=custom_prompt,
        )

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
        del references, clean_answer, citation_anchors
        return await create_note(
            self._transport,
            notebook_id,
            title=title,
            content=answer_text,
            note_type=SAVED_RESPONSE_NOTE_TYPE,
        )


__all__ = [
    "AndroidChatAPI",
    "DELETE_CHAT_TURNS_METHOD",
    "GENERATE_FREE_FORM_STREAMED_METHOD",
    "GET_PROJECT_METHOD",
    "LIST_CHAT_SESSIONS_METHOD",
    "LIST_CHAT_TURNS_METHOD",
    "MUTATE_PROJECT_METHOD",
]
