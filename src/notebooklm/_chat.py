"""Backend-neutral chat semantics and orchestration."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import weakref
from abc import ABC, abstractmethod
from collections import OrderedDict
from dataclasses import dataclass, replace
from typing import Any, Literal

from ._conversation_cache import ConversationCache
from ._loop_bound import LoopBoundPrimitive
from ._notebook_metadata import CreatedChatSessionProvider, NotebookSourceIdProvider
from ._runtime.call_supervisor import OperationLease
from ._runtime.contracts import LoopGuard
from ._types.documents import StructuredDocument, utf16_len
from ._types.enums import ChatGoal, ChatResponseLength
from .exceptions import ChatError, NetworkError, ValidationError
from .types import (
    AskResult,
    ChatMode,
    ChatReference,
    ChatSessionStatus,
    ChatSettings,
    ConversationTurn,
    ConversationTurnKey,
    NextStepSuggestion,
    Note,
)

logger = logging.getLogger("notebooklm._chat.api")
notes_logger = logging.getLogger("notebooklm._chat.notes")

_TURN_COUNT_INITIAL_LIMIT = 100
_TURN_COUNT_MAX_LIMIT = 12_800
_CITATION_MARKER_RE = re.compile(r" ?\[(\d+)\]")
_DELETED_CONVERSATION_CAPACITY = 1024


class RecentlyDeletedConversations:
    """FIFO-bounded membership set of recently deleted conversation ids.

    A null-conversation ask resolves the current conversation before taking its
    lock. If that conversation is deleted while the ask waits, the marker tells
    the ask to recover the fresh conversation id returned by the server instead
    of pinning its result to the deleted id. Conversation ids are never reused,
    and only the brief lock-handoff window is relevant, so a bounded FIFO keeps
    memory flat without losing a live marker.
    """

    def __init__(self, capacity: int = _DELETED_CONVERSATION_CAPACITY) -> None:
        self._capacity = capacity
        self._ids: OrderedDict[str, None] = OrderedDict()

    def record(self, conversation_id: str) -> None:
        """Mark ``conversation_id`` as deleted, evicting the oldest if over cap."""
        self._ids[conversation_id] = None
        self._ids.move_to_end(conversation_id)
        while len(self._ids) > self._capacity:
            self._ids.popitem(last=False)

    def __contains__(self, conversation_id: str) -> bool:
        return conversation_id in self._ids

    def clear(self) -> None:
        self._ids.clear()


@dataclass(frozen=True)
class _PostedAsk:
    """One completed backend chat stream before semantic ID recovery."""

    answer: str
    references: list[ChatReference]
    conversation_id: str | None
    raw_response: str
    answer_document: StructuredDocument
    turn_key: ConversationTurnKey | None
    next_steps: list[NextStepSuggestion]


@dataclass(frozen=True)
class _ChatSettingsRead:
    """Backend-decoded chat settings before public model construction."""

    goal: ChatGoal
    response_length: ChatResponseLength
    custom_prompt: str | None


@dataclass(frozen=True)
class _TurnRoleSnapshot:
    """One bounded role snapshot plus backend-specific exhaustion evidence."""

    roles: tuple[object, ...]
    exhausted: bool


_ConfigureAttemptLogPolicy = Literal["before_validation", "silent"]


def _strip_citation_markers(answer_text: str) -> tuple[str, list[tuple[int, int]]]:
    """Strip ``[N]`` markers and return their UTF-16 offsets in clean text."""
    positions: list[tuple[int, int]] = []
    clean_parts: list[str] = []
    last_end = 0
    clean_offset = 0
    for match in _CITATION_MARKER_RE.finditer(answer_text):
        chunk = answer_text[last_end : match.start()]
        clean_parts.append(chunk)
        clean_offset += utf16_len(chunk)
        positions.append((int(match.group(1)), clean_offset))
        last_end = match.end()
    clean_parts.append(answer_text[last_end:])
    return "".join(clean_parts), positions


def _resolve_reference(
    references: list[ChatReference],
    citation_number: int,
) -> ChatReference | None:
    """Resolve a marker without mis-anchoring numbering holes."""
    for ref in references:
        if ref.citation_number == citation_number and ref.chunk_id:
            return ref
    index = citation_number - 1
    if 0 <= index < len(references):
        candidate = references[index]
        if candidate.citation_number is None and candidate.chunk_id:
            return candidate
    return None


def _prepare_note_citations(
    answer_text: str,
    references: list[ChatReference],
) -> tuple[str, list[tuple[ChatReference, int]]]:
    """Prepare clean note text and resolved citation anchors."""
    clean_answer, marker_positions = _strip_citation_markers(answer_text)
    if not any(ref.chunk_id for ref in references):
        raise ValueError(
            "save_chat_answer_as_note requires references with chunk_id set; "
            "got references without any usable chunk_id."
        )
    anchors: list[tuple[ChatReference, int]] = []
    for citation_number, position in marker_positions:
        reference = _resolve_reference(references, citation_number)
        if reference is None or reference.chunk_id is None:
            notes_logger.warning(
                "Citation marker [%d] in answer has no matching reference; "
                "skipping anchor for this marker",
                citation_number,
            )
            continue
        anchors.append((reference, position))
    return clean_answer, anchors


class ChatAPI(LoopBoundPrimitive, ABC):
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

    _configure_attempt_log_policy: _ConfigureAttemptLogPolicy = "silent"

    def _operation_scope(
        self, label: str
    ) -> contextlib.AbstractAsyncContextManager[OperationLease | None]:
        """Return the backend's scope for one multi-call workflow."""

        return contextlib.nullcontext(None)

    def __init__(
        self,
        *,
        loop_guard: LoopGuard,
        notebooks: NotebookSourceIdProvider,
        conversation_cache: ConversationCache | None = None,
        created_chat_sessions: CreatedChatSessionProvider | None = None,
    ) -> None:
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
        self._loop_guard = loop_guard
        self._notebooks = notebooks
        self._created_chat_sessions = created_chat_sessions
        self._cache = conversation_cache if conversation_cache is not None else ConversationCache()
        self._conversation_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )
        self._new_conversation_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )
        self._deleted_conversations = RecentlyDeletedConversations()

    def _on_loop_rebind(
        self,
        old: asyncio.AbstractEventLoop | None,
        new: asyncio.AbstractEventLoop | None,
    ) -> None:
        """Discard locks captured by a prior event loop."""
        self._conversation_locks.clear()
        self._new_conversation_locks.clear()

    def reset_after_open(self) -> None:
        """Discard the lazy conversation locks so a reopened client rebinds them.

        Called from :meth:`ClientLifecycle.open` so a client closed and reopened
        on a *different* event loop builds fresh ``asyncio.Lock`` instances on
        the new loop instead of reusing stale ones bound to the dead loop (which
        on 3.10/3.11 can raise "bound to a different event loop" or mispark
        waiters). Clearing the two ``WeakValueDictionary`` maps suffices — each
        per-key lock is rebuilt lazily on the next ``_get_*_lock`` call. Mirrors
        ``SourceUploadPipeline.reset_after_open``.
        """
        self._conversation_locks.clear()
        self._new_conversation_locks.clear()

    def _get_conversation_lock(self, conversation_id: str) -> asyncio.Lock:
        lock = self._conversation_locks.get(conversation_id)
        if lock is None:
            lock = asyncio.Lock()
            self._conversation_locks[conversation_id] = lock
        return lock

    def _get_new_conversation_lock(self, notebook_id: str) -> asyncio.Lock:
        lock = self._new_conversation_locks.get(notebook_id)
        if lock is None:
            lock = asyncio.Lock()
            self._new_conversation_locks[notebook_id] = lock
        return lock

    async def _count_prior_server_turns(
        self,
        notebook_id: str,
        conversation_id: str,
    ) -> int:
        """Count questions from a complete newest-first role snapshot."""
        limit = _TURN_COUNT_INITIAL_LIMIT
        while True:
            snapshot = await self._list_turn_roles(notebook_id, conversation_id, limit)
            question_count = sum(role == 1 for role in snapshot.roles)
            if snapshot.exhausted:
                return question_count
            if limit >= _TURN_COUNT_MAX_LIMIT:
                raise ChatError(
                    f"Conversation history filled the maximum "
                    f"{_TURN_COUNT_MAX_LIMIT:,}-row snapshot; cannot derive an "
                    "authoritative turn number."
                )
            limit *= 2

    async def ask(
        self,
        notebook_id: str,
        question: str,
        source_ids: list[str] | None = None,
        conversation_id: str | None = None,
    ) -> AskResult:
        """Ask the notebook a question.

        Args:
            notebook_id: The notebook ID.
            question: The question to ask.
            source_ids: Specific source IDs to query. If None, uses all sources.
            conversation_id: Existing conversation ID for follow-up questions.
                Omit (or pass ``None``) to continue the user's current
                conversation on this notebook (or create one if none
                exists) — matching the web UI's default behavior.

        Returns:
            AskResult with answer, server-recorded conversation_id, and
            turn info. For new conversations the conversation_id is
            fetched via ``hPTbtc`` post-ask (issue #659).

        Raises:
            ChatError: For a new conversation, if ``hPTbtc`` returns no
                conversation_id after the ask (the server failed to record
                the turn, or the API shape drifted). The full answer text
                is logged at ERROR level before the raise so it survives
                in the audit trail.
            NetworkError / ChatError: If the post-ask ``hPTbtc`` round-trip
                itself fails (transient network or auth issue). Same
                logging contract — answer is logged before the raise.

        Note:
            Repeated ``ask()`` calls without ``conversation_id`` all extend
            the same most-recent conversation. To force a fresh
            conversation, first call ``delete_conversation(notebook_id,
            last_conversation_id)`` — the server then has nothing to
            extend and the next ``ask()`` starts a new conversation.
        """
        self._loop_guard.assert_bound_loop()
        logger.debug(
            "Asking question in notebook %s (conversation=%s)",
            notebook_id,
            conversation_id or "new",
        )
        if source_ids is None:
            source_ids = await self._notebooks.get_source_ids(notebook_id)

        active_source_ids: list[str] = source_ids
        is_new_conversation = conversation_id is None
        is_follow_up = not is_new_conversation
        prior_turn_count = 0

        async def perform_request(
            *,
            cached_turns: list[ConversationTurn],
            active_conversation_id: str | None,
            resolved_id_override: str | None = None,
        ) -> _PostedAsk:
            posted = await self._stream_answer(
                notebook_id=notebook_id,
                question=question,
                source_ids=active_source_ids,
                cached_turns=cached_turns,
                conversation_id=active_conversation_id,
            )

            resolved_conversation_id = active_conversation_id
            if resolved_id_override is not None:
                resolved_conversation_id = resolved_id_override
            elif is_new_conversation:
                try:
                    resolved_conversation_id = await self.get_conversation_id(notebook_id)
                except (ChatError, NetworkError):
                    logger.error(
                        "Chat ask succeeded but post-ask get_conversation_id "
                        "failed. Answer (%d chars, may be truncated): %r",
                        len(posted.answer or ""),
                        (posted.answer or "")[:500],
                    )
                    raise
                if resolved_conversation_id is None:
                    if posted.answer:
                        logger.error(
                            "Server returned a non-empty answer but hPTbtc "
                            "returned no conversation_id (%d chars). Answer preview: %r",
                            len(posted.answer),
                            posted.answer[:500],
                        )
                    raise ChatError(
                        "Server did not register a conversation for this ask "
                        "(hPTbtc returned no id). The response may have been "
                        "empty, or the API shape may have changed. Please file "
                        "an issue at https://github.com/teng-lin/notebooklm-py/issues."
                    )

            assert resolved_conversation_id is not None
            return replace(posted, conversation_id=resolved_conversation_id)

        def cache_turn(posted: _PostedAsk, server_prior_turn_count: int) -> int:
            assert posted.conversation_id is not None
            if posted.answer:
                turn_number = server_prior_turn_count + 1
                self._cache.cache_conversation_turn(
                    posted.conversation_id,
                    question,
                    posted.answer,
                    turn_number,
                )
                return turn_number
            return server_prior_turn_count

        if is_new_conversation:
            async with self._get_new_conversation_lock(notebook_id):
                created_session_id = (
                    self._created_chat_sessions._take_created_chat_session_id(notebook_id)
                    if self._created_chat_sessions is not None
                    else None
                )
                current_id = created_session_id
                if current_id is None:
                    current_id = await self.get_conversation_id(notebook_id)
                if current_id is None:
                    posted = await perform_request(
                        cached_turns=[],
                        active_conversation_id=None,
                    )

            if current_id is not None:
                async with self._get_conversation_lock(current_id):
                    override = None if current_id in self._deleted_conversations else current_id
                    is_follow_up = False
                    if override is not None:
                        prior_turn_count = await self._count_prior_server_turns(
                            notebook_id,
                            override,
                        )
                        is_follow_up = prior_turn_count > 0
                    posted = await perform_request(
                        cached_turns=[],
                        active_conversation_id=(
                            override if created_session_id is not None else None
                        ),
                        resolved_id_override=override,
                    )
                    turn_number = cache_turn(posted, prior_turn_count)
            else:
                assert posted.conversation_id is not None
                async with self._get_conversation_lock(posted.conversation_id):
                    turn_number = cache_turn(posted, 0)
        else:
            assert conversation_id is not None
            async with self._get_conversation_lock(conversation_id):
                prior_turn_count = await self._count_prior_server_turns(
                    notebook_id,
                    conversation_id,
                )
                cached_turns = self.get_cached_turns(conversation_id)
                posted = await perform_request(
                    cached_turns=cached_turns,
                    active_conversation_id=conversation_id,
                )
                turn_number = cache_turn(posted, prior_turn_count)

        assert posted.conversation_id is not None
        return AskResult(
            answer=posted.answer,
            conversation_id=posted.conversation_id,
            turn_number=turn_number,
            is_follow_up=is_follow_up,
            references=posted.references,
            raw_response=posted.raw_response[:1000],
            answer_document=posted.answer_document,
            turn_key=posted.turn_key,
            next_steps=posted.next_steps,
        )

    def get_cached_turns(self, conversation_id: str) -> list[ConversationTurn]:
        """Get locally cached conversation turns.

        Args:
            conversation_id: The conversation ID.

        Returns:
            List of ConversationTurn objects.
        """
        cached = self._cache.get_cached_conversation(conversation_id)
        return [
            ConversationTurn(
                query=turn["query"],
                answer=turn["answer"],
                turn_number=turn["turn_number"],
            )
            for turn in cached
        ]

    async def session_status(
        self,
        notebook_id: str,
        conversation_id: str | None = None,
    ) -> ChatSessionStatus:
        """Return whether a chat session is currently generating.

        Args:
            notebook_id: The notebook that owns the session.
            conversation_id: Session to inspect. When omitted, resolves the
                notebook's most recent session just like :meth:`ask`.

        Returns:
            A :class:`ChatSessionStatus`. A notebook with no chat session is
            idle and returns ``ChatSessionStatus(False, None)`` without issuing
            a status request.

        Note:
            Google's status transition is slightly wider than the streamed
            response lifetime: it can become idle shortly before the HTTP/gRPC
            stream closes. Use it as a polling signal, not as proof that a
            caller already holding the stream has consumed its final frame.
        """
        self._loop_guard.assert_bound_loop()
        resolved_id = conversation_id or await self.get_conversation_id(notebook_id)
        if resolved_id is None:
            return ChatSessionStatus(generating=False)
        return await self._get_session_status(notebook_id, resolved_id)

    async def cancel(
        self,
        notebook_id: str,
        conversation_id: str | None = None,
    ) -> None:
        """Stop the active generation for a chat session, if any.

        Args:
            notebook_id: The notebook that owns the session.
            conversation_id: Session to cancel. When omitted, resolves the
                notebook's most recent session just like :meth:`ask`.

        Returns:
            ``None``. A notebook with no chat session is already idle and is a
            no-op; server and authorization failures still raise.

        Note:
            Google stops emitting answer frames but does not close an existing
            Web streaming response. A caller that owns that stream must abandon
            or cancel its local task after this method succeeds.
        """
        self._loop_guard.assert_bound_loop()
        resolved_id = conversation_id or await self.get_conversation_id(notebook_id)
        if resolved_id is None:
            return None
        await self._cancel_generation(notebook_id, resolved_id)
        return None

    async def delete_conversation(self, notebook_id: str, conversation_id: str) -> None:
        """Delete a conversation from the server.

        Mirrors the web UI's "Delete history" action. After deletion the next
        ``ask()`` with no ``conversation_id`` starts a fresh server-side
        conversation rather than extending the deleted one.

        Args:
            notebook_id: The notebook that owns the conversation.
            conversation_id: The conversation to delete.

        Returns:
            ``None`` on success; any failure raises first.

        .. versionchanged:: 0.8.0
            **Breaking change:** returns ``None`` instead of the uninformative
            always-``True`` value; the ``-> bool`` annotation is dropped (#1290).
        """
        self._loop_guard.assert_bound_loop()
        logger.debug("Deleting conversation %s in notebook %s", conversation_id, notebook_id)
        async with self._get_conversation_lock(conversation_id):
            await self._send_delete_conversation(notebook_id, conversation_id)
            self._cache.clear(conversation_id)
            self._deleted_conversations.record(conversation_id)
        return None

    def clear_cache(self, conversation_id: str | None = None) -> bool:
        """Clear conversation cache.

        Args:
            conversation_id: Clear specific conversation, or all if None.

        Returns:
            True if cache was cleared.
        """
        return self._cache.clear(conversation_id)

    def cache_size(self) -> int:
        """Return the number of conversations currently held in the cache.

        Surfaced for CLI ``history --clear --json`` so the emitted envelope
        can report how many conversations were dropped without reaching
        into ``_cache`` from the CLI layer.
        """
        return len(self._cache.conversations)

    async def set_mode(self, notebook_id: str, mode: ChatMode) -> None:
        """Set chat mode using predefined configurations.

        Args:
            notebook_id: The notebook ID.
            mode: Predefined ChatMode (DEFAULT, LEARNING_GUIDE, CONCISE, DETAILED).
        """
        mode_configs = {
            ChatMode.DEFAULT: (ChatGoal.DEFAULT, ChatResponseLength.DEFAULT, None),
            ChatMode.LEARNING_GUIDE: (
                ChatGoal.LEARNING_GUIDE,
                ChatResponseLength.LONGER,
                None,
            ),
            ChatMode.CONCISE: (ChatGoal.DEFAULT, ChatResponseLength.SHORTER, None),
            ChatMode.DETAILED: (ChatGoal.DEFAULT, ChatResponseLength.LONGER, None),
        }
        goal, length, prompt = mode_configs[mode]
        await self.configure(notebook_id, goal, length, prompt)

    async def save_answer_as_note(
        self,
        notebook_id: str,
        ask_result: AskResult,
        *,
        title: str | None = None,
    ) -> Note:
        """Save a chat answer as a citation-rich note (issue #660).

        Unlike :meth:`NotesAPI.create`, this preserves the ``[N]``
        citation markers in the answer as interactive hover-anchored
        references in the NotebookLM web UI. It mirrors the wire format
        the web UI's "Save to note" button uses.

        Args:
            notebook_id: The notebook ID.
            ask_result: Result from a prior ``client.chat.ask()`` call.
                Must have non-empty ``references`` — otherwise this
                method raises :class:`ValueError`.
            title: Note title. When ``None`` (default), a title is
                derived from the first 50 characters of the answer
                (``AskResult`` does not currently carry the original
                question, so the answer is used). An empty string
                (``""``) is passed through verbatim — i.e. treated as
                "use this exact (empty) title", NOT as "use default".
                The NotebookLM server may apply smart-title generation
                regardless; the returned ``Note.title`` reflects what
                the server actually stored.

        Returns:
            The created ``Note``. ``Note.content`` holds the answer text
            WITH ``[N]`` markers; the rich citation anchors live
            server-side and surface via the NotebookLM web UI.

        Raises:
            ValueError: If ``ask_result.references`` is empty. Callers
                without citations should fall back to
                :meth:`NotesAPI.create` for plain-text notes — this
                method raises rather than silently degrading so the
                caller can decide.
        """
        if not ask_result.references:
            raise ValueError(
                "save_answer_as_note requires AskResult.references to be "
                "non-empty; use notes.create() for plain-text notes."
            )
        resolved_title = (
            title
            if title is not None
            else f"Chat: {ask_result.answer[:50].strip().replace(chr(10), ' ')}"
        )
        notes_logger.debug(
            "Saving chat answer as note in notebook %s (%d refs)",
            notebook_id,
            len(ask_result.references),
        )
        clean_answer, citation_anchors = _prepare_note_citations(
            ask_result.answer,
            ask_result.references,
        )

        return await self._send_note(
            notebook_id=notebook_id,
            answer_text=ask_result.answer,
            references=ask_result.references,
            title=resolved_title,
            clean_answer=clean_answer,
            citation_anchors=citation_anchors,
        )

    @abstractmethod
    async def get_conversation_turns(
        self,
        notebook_id: str,
        conversation_id: str,
        limit: int = 2,
    ) -> Any:
        """Return backend-native conversation turns."""

    @abstractmethod
    async def get_conversation_id(self, notebook_id: str) -> str | None:
        """Return the notebook's current conversation ID."""

    @abstractmethod
    async def get_history(
        self,
        notebook_id: str,
        limit: int = 100,
        conversation_id: str | None = None,
    ) -> list[tuple[str, str]]:
        """Return decoded question/answer history."""

    async def configure(
        self,
        notebook_id: str,
        goal: ChatGoal | None = None,
        response_length: ChatResponseLength | None = None,
        custom_prompt: str | None = None,
    ) -> None:
        """Configure chat persona and response settings for a notebook.

        Writes the WHOLE chat-settings block with no server-side merge: an
        omitted ``goal`` / ``response_length`` resets that field to its default.
        This is the low-level primitive — for a partial, merge-preserving update
        (CLI ``configure`` / MCP ``chat_configure``) go through
        ``_app.chat.execute_configure``, which reads :meth:`get_settings` first.

        Args:
            notebook_id: The notebook ID.
            goal: Chat persona/goal (ChatGoal enum: DEFAULT, CUSTOM, LEARNING_GUIDE).
            response_length: Response verbosity (ChatResponseLength enum).
            custom_prompt: Custom instructions (required if goal is CUSTOM).

        Raises:
            ValidationError: If goal is CUSTOM but custom_prompt is not provided.
        """
        if self._configure_attempt_log_policy == "before_validation":
            logger.debug("Configuring chat for notebook %s", notebook_id)
        resolved_goal = ChatGoal.DEFAULT if goal is None else goal
        resolved_length = ChatResponseLength.DEFAULT if response_length is None else response_length
        if resolved_goal == ChatGoal.CUSTOM and not custom_prompt:
            raise ValidationError("custom_prompt is required when goal is CUSTOM")
        active_prompt = custom_prompt if resolved_goal == ChatGoal.CUSTOM else None
        await self._send_configure(
            notebook_id,
            resolved_goal,
            resolved_length,
            active_prompt,
        )

    async def get_settings(self, notebook_id: str) -> ChatSettings:
        """Read the notebook's current chat configuration.

        Decodes the chat-settings block from ``GET_NOTEBOOK`` so a *partial*
        ``configure`` can merge (read-modify-write) instead of clobbering the
        fields it doesn't touch — the server stores the whole block with no
        merge (see :meth:`configure`). A notebook that has never been configured
        reads back as ``DEFAULT``/``DEFAULT`` with no persona.

        Args:
            notebook_id: The notebook ID.

        Returns:
            The current :class:`ChatSettings` (goal, response length, persona).

        Raises:
            UnknownRPCMethodError: if the GET_NOTEBOOK chat-settings block has
                drifted from the expected shape — raised rather than silently
                defaulting, which on the merge path would clobber a field the
                caller meant to preserve (the #1751 footgun).
        """
        settings = await self._read_settings(notebook_id)
        return ChatSettings(
            goal=settings.goal,
            response_length=settings.response_length,
            custom_prompt=settings.custom_prompt,
        )

    @abstractmethod
    async def _send_configure(
        self,
        notebook_id: str,
        goal: ChatGoal,
        response_length: ChatResponseLength,
        custom_prompt: str | None,
    ) -> None:
        """Send one backend-specific whole-settings mutation."""

    @abstractmethod
    async def _read_settings(self, notebook_id: str) -> _ChatSettingsRead:
        """Read and validate backend chat settings into a neutral carrier."""

    @abstractmethod
    async def _list_turn_roles(
        self,
        notebook_id: str,
        conversation_id: str,
        limit: int,
    ) -> _TurnRoleSnapshot:
        """Return decoded roles plus backend-specific exhaustion evidence."""

    @abstractmethod
    async def _stream_answer(
        self,
        *,
        notebook_id: str,
        question: str,
        source_ids: list[str],
        cached_turns: list[ConversationTurn],
        conversation_id: str | None,
    ) -> _PostedAsk:
        """Send and decode one backend answer stream."""

    @abstractmethod
    async def _send_delete_conversation(
        self,
        notebook_id: str,
        conversation_id: str,
    ) -> None:
        """Send the backend delete request."""

    @abstractmethod
    async def _get_session_status(
        self,
        notebook_id: str,
        conversation_id: str,
    ) -> ChatSessionStatus:
        """Send and decode the backend chat-session status request."""

    @abstractmethod
    async def _cancel_generation(
        self,
        notebook_id: str,
        conversation_id: str,
    ) -> None:
        """Send the backend generation-cancel request."""

    @abstractmethod
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
        """Send the backend saved-from-chat note request."""


def __getattr__(name: str) -> Any:
    """Lazily preserve the historically importable private turn helper."""
    if name == "_extract_next_turn_content":
        from ._web.rows.chat_stream import _extract_next_turn_content

        return _extract_next_turn_content
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["ChatAPI", "_extract_next_turn_content"]  # noqa: F822 - resolved lazily above
