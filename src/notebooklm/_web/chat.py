"""Web implementation of notebook chat operations."""

from __future__ import annotations

import logging
import reprlib
from typing import TYPE_CHECKING, Any

from .._chat import (
    ChatAPI,
    _ChatSettingsRead,
    _ConfigureAttemptLogPolicy,
    _PostedAsk,
    _prepare_note_citations,
    _TurnRoleSnapshot,
)
from .._conversation_cache import ConversationCache
from .._logging import get_request_id, reset_request_id, set_request_id
from .._notebook_metadata import CreatedChatSessionProvider, NotebookSourceIdProvider
from .._runtime.config import (
    DEFAULT_CHAT_RESPONSE_MAX_BYTES,
    DEFAULT_CHAT_TIMEOUT,
    assert_resolved_read_timeout,
)
from .._runtime.contracts import LoopGuard
from .._types.enums import ChatGoal, ChatResponseLength
from ..exceptions import ChatError, NetworkError, UnknownRPCMethodError
from ..rpc import RPCMethod, safe_index
from ..types import ChatReference, ChatSessionStatus, ConversationTurn, Note
from .contracts import RpcCaller
from .params.chat_note import build_save_chat_as_note_params
from .params.chat_session import (
    build_cancel_generation_params,
    build_chat_session_status_params,
)
from .params.chat_stream import build_streaming_chat_request
from .params.notebooks import build_get_notebook_params
from .rows.chat import (
    ConversationTurnRow,
    SavedChatNoteRow,
    unwrap_chat_session_status,
    unwrap_chat_settings,
    unwrap_conversation_turns,
    unwrap_last_conversation_id,
)
from .rows.chat_stream import (
    _extract_next_turn_content,
    extract_answer_and_refs_from_chunk,
    extract_text_passages,
    extract_uuid_from_nested,
    parse_citations,
    parse_single_citation,
    parse_streaming_chat_response,
    raise_if_rate_limited,
)
from .rows.notes import NoteRow
from .transport.chat import chat_aware_authed_post

if TYPE_CHECKING:
    from .transport.reqid_counter import ReqidCounter
    from .transport.request_types import AuthSnapshot
    from .transport.runtime import RuntimeTransport

logger = logging.getLogger("notebooklm._chat.api")
notes_logger = logging.getLogger("notebooklm._chat.notes")


async def save_chat_answer_as_note(
    rpc: RpcCaller,
    notebook_id: str,
    answer_text: str,
    references: list[ChatReference],
    title: str,
    *,
    clean_answer: str | None = None,
    citation_anchors: list[tuple[ChatReference, int]] | None = None,
) -> Note:
    """Persist one Web saved-from-chat note.

    ``clean_answer`` and ``citation_anchors`` are supplied by the neutral base
    on the production path. Their optional form preserves direct internal test
    coverage of this Web adapter without reviving the deleted module path.
    """
    if not references:
        raise ValueError(
            "save_chat_answer_as_note requires non-empty references; "
            "use notes.create() for plain-text notes."
        )
    if clean_answer is None or citation_anchors is None:
        notes_logger.debug(
            "Saving chat answer as note in notebook %s (%d refs)",
            notebook_id,
            len(references),
        )
        clean_answer, citation_anchors = _prepare_note_citations(answer_text, references)
    params = build_save_chat_as_note_params(
        notebook_id,
        answer_text,
        references,
        title,
        clean_answer=clean_answer,
        citation_anchors=citation_anchors,
    )
    result = await rpc.rpc_call(
        RPCMethod.CREATE_NOTE,
        params,
        source_path=f"/notebook/{notebook_id}",
        operation_variant="saved_from_chat",
    )
    create_row = SavedChatNoteRow(result)
    note_data = create_row.note_data
    note_id = create_row.note_id
    server_title = create_row.server_title if create_row.server_title is not None else title
    if not note_id:
        raise RuntimeError("CREATE_NOTE returned no note ID for saved-from-chat request")
    created_at = NoteRow([note_id, note_data]).created_at
    return Note(
        id=note_id,
        notebook_id=notebook_id,
        title=server_title,
        content=answer_text,
        created_at=created_at,
    )


class WebChatAPI(ChatAPI):
    """Operations for notebook chat/conversations.

    Provides methods for asking questions to notebooks and managing
    conversation history with follow-up support.

    Usage:
        async with NotebookLMClient.from_storage() as client:
            # Ask a question
            result = await client.chat.ask(notebook_id, "What is X?")
            print(result.answer)

            # Follow-up question
            result = await client.chat.ask(
                notebook_id,
                "Can you elaborate?",
                conversation_id=result.conversation_id
            )
    """

    _configure_attempt_log_policy: _ConfigureAttemptLogPolicy = "before_validation"

    def __init__(
        self,
        *,
        rpc: RpcCaller,
        transport: RuntimeTransport,
        reqid: ReqidCounter,
        loop_guard: LoopGuard,
        notebooks: NotebookSourceIdProvider,
        chat_timeout: float | None = DEFAULT_CHAT_TIMEOUT,
        chat_response_max_bytes: int | None = DEFAULT_CHAT_RESPONSE_MAX_BYTES,
        conversation_cache: ConversationCache | None = None,
        created_chat_sessions: CreatedChatSessionProvider | None = None,
    ):
        """Initialize the chat API.

        Per ADR-0014 Rule 2 Corollary, ``ChatAPI`` depends on the **direct**
        collaborators it exercises (``rpc``, ``transport``, ``reqid``,
        ``loop_guard``, ``notebooks``) rather than a chat-local Runtime Protocol
        bundling them.

        Args:
            rpc: RPC dispatch collaborator for the ``get_conversation_*``,
                ``configure``, ``delete_conversation``, and
                ``save_answer_as_note`` round-trips.
            transport: :class:`RuntimeTransport` owning the authed-POST entry
                point used by :meth:`ask` via :func:`chat_aware_authed_post`.
            reqid: :class:`ReqidCounter` minting the per-attempt ``_reqid``
                query parameter for the streamed chat request.
            loop_guard: :class:`LoopGuard` whose :meth:`assert_bound_loop` fires
                before :meth:`ask` acquires the per-conversation lock, so a
                cross-loop follow-up doesn't hang on a lock bound to a dead loop.
            notebooks: Required source-id resolver. The composition root passes
                the client's shared notebooks API so chat never constructs a
                transport-specific notebook implementation implicitly.
            chat_timeout: Per-read HTTP timeout (seconds) for the streamed chat
                endpoint. ``None`` inherits the underlying transport timeout.
            chat_response_max_bytes: Maximum buffered streamed-chat response
                size in bytes. ``None`` inherits the shared RPC response cap.
            conversation_cache: Optional injected cache; defaults to a fresh
                per-instance ``ConversationCache``.
            created_chat_sessions: Optional one-shot provider for the initial
                session id returned by ``CREATE_NOTEBOOK``. The assembled
                client passes its shared ``NotebooksAPI`` instance.
        """
        self._rpc = rpc
        self._transport = transport
        self._reqid = reqid
        assert_resolved_read_timeout(chat_timeout, name="chat_timeout")
        self._chat_timeout = chat_timeout
        self._chat_response_max_bytes = chat_response_max_bytes
        super().__init__(
            loop_guard=loop_guard,
            notebooks=notebooks,
            conversation_cache=conversation_cache,
            created_chat_sessions=created_chat_sessions,
        )

    async def _stream_answer(
        self,
        *,
        notebook_id: str,
        question: str,
        source_ids: list[str],
        cached_turns: list[ConversationTurn],
        conversation_id: str | None,
    ) -> _PostedAsk:
        conversation_history = self._build_history_from_turns(cached_turns)
        reqid = await self._reqid.next_reqid()

        def build_request(snapshot: AuthSnapshot) -> tuple[str, str, dict[str, str]]:
            return build_streaming_chat_request(
                snapshot=snapshot,
                notebook_id=notebook_id,
                question=question,
                source_ids=source_ids,
                conversation_history=conversation_history,
                conversation_id=conversation_id,
                reqid=reqid,
            )

        reqid_token = None if get_request_id() is not None else set_request_id()
        try:
            response = await chat_aware_authed_post(
                self._transport,
                build_request=build_request,
                parse_label="chat.ask",
                read_timeout=self._chat_timeout,
                max_response_bytes=self._chat_response_max_bytes,
                disable_read_timeout_retries=True,
            )
        finally:
            if reqid_token is not None:
                reset_request_id(reqid_token)

        parsed = parse_streaming_chat_response(response.text)
        return _PostedAsk(
            answer=parsed.answer,
            references=parsed.references,
            conversation_id=conversation_id,
            raw_response=response.text,
            answer_document=parsed.answer_document,
            turn_key=parsed.turn_key,
            next_steps=parsed.next_steps,
        )

    async def _list_turn_roles(
        self,
        notebook_id: str,
        conversation_id: str,
        limit: int,
    ) -> _TurnRoleSnapshot:
        turns_data = await self.get_conversation_turns(
            notebook_id,
            conversation_id,
            limit=limit,
        )
        turns = unwrap_conversation_turns(turns_data, source="_chat.ask.turn_count")
        return _TurnRoleSnapshot(
            roles=tuple(ConversationTurnRow(turn).role for turn in turns),
            exhausted=len(turns) < limit,
        )

    async def _send_delete_conversation(
        self,
        notebook_id: str,
        conversation_id: str,
    ) -> None:
        params: list[Any] = [[], conversation_id, None, 1]
        await self._rpc.rpc_call(
            RPCMethod.DELETE_CONVERSATION,
            params,
            source_path=f"/notebook/{notebook_id}",
        )

    async def _get_session_status(
        self,
        notebook_id: str,
        conversation_id: str,
    ) -> ChatSessionStatus:
        raw = await self._rpc.rpc_call(
            RPCMethod.GET_CHAT_SESSION_STATUS,
            build_chat_session_status_params(conversation_id),
            source_path=f"/notebook/{notebook_id}",
        )
        row = unwrap_chat_session_status(raw)
        return ChatSessionStatus(generating=row.generating, token=row.token)

    async def _cancel_generation(
        self,
        notebook_id: str,
        conversation_id: str,
    ) -> None:
        await self._rpc.rpc_call(
            RPCMethod.CANCEL_GENERATION,
            build_cancel_generation_params(conversation_id),
            source_path=f"/notebook/{notebook_id}",
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
        return await save_chat_answer_as_note(
            self._rpc,
            notebook_id,
            answer_text,
            references,
            title,
            clean_answer=clean_answer,
            citation_anchors=citation_anchors,
        )

    async def get_conversation_turns(
        self,
        notebook_id: str,
        conversation_id: str,
        limit: int = 2,
    ) -> Any:
        """Get turns (individual messages) for a specific conversation.

        Args:
            notebook_id: The notebook ID.
            conversation_id: The conversation ID to fetch turns for.
            limit: Maximum number of turns to retrieve. Turns are returned
                newest-first, so limit=2 gives the latest Q&A pair.

        Returns:
            Raw turn data from API; the per-turn position contract lives in
            :class:`~notebooklm._web.rows.chat.ConversationTurnRow`.
        """
        logger.debug(
            "Getting conversation turns for %s (conversation=%s, limit=%d)",
            notebook_id,
            conversation_id,
            limit,
        )
        params: list[Any] = [[], None, None, conversation_id, limit]
        return await self._rpc.rpc_call(
            RPCMethod.GET_CONVERSATION_TURNS,
            params,
            source_path=f"/notebook/{notebook_id}",
        )

    async def get_conversation_id(self, notebook_id: str) -> str | None:
        """Get the most recent conversation ID from the API.

        The underlying RPC (hPTbtc) returns the last conversation ID for a notebook.

        Args:
            notebook_id: The notebook ID.

        Returns:
            The most recent conversation ID, or None if no conversations exist.
        """
        logger.debug("Getting conversation ID for notebook %s", notebook_id)
        params: list[Any] = [[], None, notebook_id, 1]
        raw = await self._rpc.rpc_call(
            RPCMethod.GET_LAST_CONVERSATION_ID,
            params,
            source_path=f"/notebook/{notebook_id}",
        )
        if raw and isinstance(raw, list):
            conversation_id = unwrap_last_conversation_id(raw)
            if conversation_id is not None:
                return conversation_id
            logger.warning(
                "hPTbtc returned an unexpected response shape; no "
                "conversation_id extracted (notebook=%s, raw=%r)",
                notebook_id,
                repr(raw)[:500],
            )
        elif raw is not None:
            logger.warning(
                "hPTbtc returned a non-list, non-empty response (notebook=%s, type=%s, raw=%r)",
                notebook_id,
                type(raw).__name__,
                repr(raw)[:500],
            )
        return None

    async def get_history(
        self,
        notebook_id: str,
        limit: int = 100,
        conversation_id: str | None = None,
    ) -> list[tuple[str, str]]:
        """Get Q&A history for the most recent conversation.

        Args:
            notebook_id: The notebook ID.
            limit: Maximum number of Q&A turns to retrieve.
            conversation_id: Use this conversation ID instead of fetching it.
                Defaults to the most recent conversation if not provided.

        Returns:
            List of (question, answer) pairs, oldest-first.
            Returns an empty list if no conversations exist.
        """
        logger.debug("Getting conversation history for notebook %s (limit=%d)", notebook_id, limit)
        conv_id = conversation_id or await self.get_conversation_id(notebook_id)
        if not conv_id:
            return []
        try:
            turns_data = await self.get_conversation_turns(notebook_id, conv_id, limit=limit)
        except (ChatError, NetworkError) as exc:
            logger.warning("Failed to fetch conversation turns for %s: %s", notebook_id, exc)
            return []
        turns = unwrap_conversation_turns(turns_data, source="_chat.get_history")
        if turns:
            turns_data = [list(reversed(turns))]
        return self._parse_turns_to_qa_pairs(turns_data)

    async def _send_configure(
        self,
        notebook_id: str,
        goal: ChatGoal,
        response_length: ChatResponseLength,
        custom_prompt: str | None,
    ) -> None:
        """Send one Web whole-settings mutation."""
        goal_array = [goal.value, custom_prompt] if goal == ChatGoal.CUSTOM else [goal.value]
        chat_settings = [goal_array, [response_length.value]]
        params = [notebook_id, [[None, None, None, None, None, None, None, chat_settings]]]
        await self._rpc.rpc_call(
            RPCMethod.RENAME_NOTEBOOK,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
            # #2290: a status-tagged null is a server rejection, not an empty success.
            raise_on_null_status=True,
        )

    async def _read_settings(self, notebook_id: str) -> _ChatSettingsRead:
        """Read and validate Web chat settings."""
        params = build_get_notebook_params(notebook_id)
        result = await self._rpc.rpc_call(
            RPCMethod.GET_NOTEBOOK,
            params,
            source_path=f"/notebook/{notebook_id}",
        )
        nb_info = safe_index(
            result,
            0,
            method_id=RPCMethod.GET_NOTEBOOK.value,
            source="ChatAPI.get_settings",
        )
        row = unwrap_chat_settings(nb_info, source="ChatAPI.get_settings")
        try:
            goal = ChatGoal(row.goal_code)
            length = ChatResponseLength(row.response_length_code)
        except ValueError as exc:
            raise UnknownRPCMethodError(
                f"unknown chat-settings enum code "
                f"(goal={row.goal_code!r}, response_length={row.response_length_code!r})",
                method_id=RPCMethod.GET_NOTEBOOK.value,
                path=(7,),
                source="ChatAPI.get_settings",
                data_at_failure=reprlib.repr((row.goal_code, row.response_length_code)),
            ) from exc
        return _ChatSettingsRead(
            goal=goal,
            response_length=length,
            custom_prompt=row.custom_prompt,
        )

    @staticmethod
    def _parse_turns_to_qa_pairs(turns_data: Any) -> list[tuple[str, str]]:
        turns = unwrap_conversation_turns(turns_data, source="_chat._parse_turns_to_qa_pairs")
        pairs: list[tuple[str, str]] = []
        index = 0
        while index < len(turns):
            turn = ConversationTurnRow(turns[index])
            if not turn.is_well_formed:
                logger.debug(
                    "_parse_turns_to_qa_pairs: skipping malformed turn at index %d: %s",
                    index,
                    reprlib.repr(turns[index]),
                )
                index += 1
                continue
            if turn.has_unrecognized_role:
                logger.debug(
                    "_parse_turns_to_qa_pairs: unrecognized role code %r at turn %d — skipping; "
                    "possible role-slot drift: %s",
                    turn.role,
                    index,
                    reprlib.repr(turns[index]),
                )
                index += 1
                continue
            if turn.is_question:
                question = turn.question_text
                answer = ""
                if index + 1 < len(turns):
                    next_turn = ConversationTurnRow(turns[index + 1])
                    if next_turn.is_answer:
                        content = _extract_next_turn_content(next_turn.raw)
                        answer = str(content or "")
                        index += 1
                pairs.append((question, answer))
            index += 1
        return pairs

    @staticmethod
    def _build_history_from_turns(turns: list[ConversationTurn]) -> list[Any] | None:
        if not turns:
            return None
        history: list[Any] = []
        for turn in turns:
            history.append([turn.answer, None, 2])
            history.append([turn.query, None, 1])
        return history

    def _build_conversation_history(self, conversation_id: str) -> list[Any] | None:
        """Compatibility helper for tests and advanced internal callers."""
        return self._build_history_from_turns(self.get_cached_turns(conversation_id))

    def _build_chat_request(
        self,
        *,
        snapshot: AuthSnapshot,
        notebook_id: str,
        question: str,
        source_ids: list[str],
        conversation_history: list | None,
        conversation_id: str | None,
        reqid: int,
    ) -> tuple[str, str, dict[str, str]]:
        return build_streaming_chat_request(
            snapshot=snapshot,
            notebook_id=notebook_id,
            question=question,
            source_ids=source_ids,
            conversation_history=conversation_history,
            conversation_id=conversation_id,
            reqid=reqid,
        )

    def _parse_ask_response_with_references(
        self,
        response_text: str,
    ) -> tuple[str, list[ChatReference], str | None]:
        result = parse_streaming_chat_response(response_text)
        return result.answer, result.references, result.conversation_id

    def _extract_answer_and_refs_from_chunk(
        self,
        json_str: str,
    ) -> tuple[str | None, bool, list[ChatReference], str | None]:
        return extract_answer_and_refs_from_chunk(json_str)

    def _raise_if_rate_limited(self, error_payload: list) -> None:
        raise_if_rate_limited(error_payload)

    def _parse_citations(self, first: list) -> list[ChatReference]:
        return parse_citations(first)

    def _parse_single_citation(self, cite: Any) -> ChatReference | None:
        return parse_single_citation(cite)

    def _extract_text_passages(
        self,
        cite_inner: list,
    ) -> tuple[str | None, int | None, int | None]:
        return extract_text_passages(cite_inner)

    def _extract_uuid_from_nested(self, data: Any, max_depth: int = 10) -> str | None:
        return extract_uuid_from_nested(data, max_depth)


__all__ = ["WebChatAPI", "save_chat_answer_as_note"]
