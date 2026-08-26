"""Chat API for NotebookLM notebook conversations.

Provides operations for asking questions, managing conversations, and
retrieving conversation history.

This module is the public-vocabulary facade only: it asserts loop affinity,
projects the private backend failure vocabulary to the public exceptions, and
projects the workflow's records to the public models. The conversation state
and the sequencing of the chat operations live in
:class:`~notebooklm._chat.workflow.ChatWorkflowService` below it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable
from typing import Any, TypeVar

from .._conversation_cache import ConversationCache
from .._notebook_metadata import CreatedChatSessionProvider, NotebookSourceIdProvider
from .._runtime.contracts import LoopGuard
from .._semantic.backend import BackendAdapter, BackendError
from .._semantic.compat import project_backend_error
from .._semantic.projectors import (
    chat_reference_record,
    project_chat_ask_result,
    project_chat_saved_note,
    project_chat_settings,
    project_chat_turns_legacy,
)
from .._semantic.records import (
    ChatConfigureAction,
    ChatConfigureInput,
    ChatSaveNoteInput,
)
from ..exceptions import ChatError, NetworkError, ValidationError
from ..types import (
    AskResult,
    ChatGoal,
    ChatMode,
    ChatResponseLength,
    ChatSettings,
    ConversationTurn,
    Note,
)
from .history import parse_recorded_turns_to_qa_pairs
from .history_legacy import parse_legacy_turns_to_qa_pairs
from .workflow import ChatWorkflowService

logger = logging.getLogger(__name__)

ResultT = TypeVar("ResultT")


class ChatAPI:
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

    def __init__(
        self,
        *,
        backend: BackendAdapter,
        loop_guard: LoopGuard,
        conversation_cache: ConversationCache | None = None,
        notebooks: NotebookSourceIdProvider,
        created_chat_sessions: CreatedChatSessionProvider | None = None,
    ):
        """Initialize the chat API.

        ``ChatAPI`` keeps only semantic/backend-neutral collaborators. Web
        transport, request ids, stream parsing, and native RPC dispatch belong
        to the client-owned backend binding.

        Args:
            backend: Typed semantic backend implementing all six chat operations.
            loop_guard: :class:`LoopGuard` whose :meth:`assert_bound_loop` fires
                before :meth:`ask` reaches the per-conversation lock, so a
                cross-loop follow-up doesn't hang on a lock bound to a dead loop.
            conversation_cache: Optional injected cache; defaults to a fresh
                per-instance ``ConversationCache``.
            notebooks: Source-id resolver used only when ``ask`` omits source ids.
            created_chat_sessions: Optional one-shot provider for the initial
                session id returned by ``CREATE_NOTEBOOK``. The assembled
                client passes its shared ``NotebooksAPI`` instance.
        """
        self._loop_guard = loop_guard
        self._workflow = ChatWorkflowService(
            backend,
            notebooks=notebooks,
            created_chat_sessions=created_chat_sessions,
            conversation_cache=conversation_cache,
        )

    async def _invoke(self, operation: Awaitable[ResultT]) -> ResultT:
        """Project the private backend failure vocabulary at the compatibility facade."""
        public_error: Exception | None = None
        try:
            return await operation
        except BackendError as error:
            public_error = project_backend_error(error)
        # Raise outside the private BackendError frame. ``raise ... from None``
        # would overwrite the reviewed public cause/context graph restored by
        # ``project_backend_error``.
        assert public_error is not None
        raise public_error

    def set_bound_loop(self, loop: asyncio.AbstractEventLoop | None) -> None:
        """Forward the captured event loop to the workflow that owns the locks.

        ``ClientLifecycle.open`` drives this (and :meth:`reset_after_open`) on
        whichever object the composition root handed it as ``chat=`` — the
        facade. The lazy per-conversation / per-notebook ``asyncio.Lock`` maps
        the binding governs belong to the workflow service, so both hooks
        forward; the pair is the ``ChatLifecycleHooks`` protocol the open path
        is typed against.
        """
        self._workflow.set_bound_loop(loop)

    def reset_after_open(self) -> None:
        """Discard the lazy conversation locks so a reopened client rebinds them."""
        self._workflow.reset_after_open()

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
                the turn, or the API shape drifted). The answer length and
                first 500 characters are logged at ERROR level before the
                raise so bounded recovery evidence survives in the audit trail.
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
        # Catch cross-loop ``ask`` before any work — particularly
        # before the workflow acquires the per-conversation lock, which would
        # otherwise hang on a lock bound to a dead loop. The POST-path
        # guard in ``RuntimeTransport.perform_authed_post`` only catches misuse on
        # the POST itself, which is *after* the conversation lock is
        # already held — too late.
        self._loop_guard.assert_bound_loop()
        logger.debug(
            "Asking question in notebook %s (conversation=%s)",
            notebook_id,
            conversation_id or "new",
        )
        outcome = await self._invoke(
            self._workflow.ask_turn(notebook_id, question, source_ids, conversation_id)
        )
        return project_chat_ask_result(
            outcome.result,
            turn_number=outcome.turn_number,
            is_follow_up=outcome.is_follow_up,
        )

    async def get_conversation_turns(
        self, notebook_id: str, conversation_id: str, limit: int = 2
    ) -> Any:
        """Get turns (individual messages) for a specific conversation.

        Args:
            notebook_id: The notebook ID.
            conversation_id: The conversation ID to fetch turns for.
            limit: Maximum number of turns to retrieve. Turns are returned
                newest-first, so limit=2 gives the latest Q&A pair.

        Returns:
            Raw turn data from API; the per-turn position contract lives in
            :class:`~notebooklm._row_adapters.chat.ConversationTurnRow`.
        """
        logger.debug(
            "Getting conversation turns for %s (conversation=%s, limit=%d)",
            notebook_id,
            conversation_id,
            limit,
        )
        result = await self._invoke(
            self._workflow.get_history(notebook_id, conversation_id, limit=limit)
        )
        return project_chat_turns_legacy(result)

    async def get_conversation_id(self, notebook_id: str) -> str | None:
        """Get the most recent conversation ID from the API.

        The underlying RPC (hPTbtc) returns the last conversation ID for a notebook.

        Args:
            notebook_id: The notebook ID.

        Returns:
            The most recent conversation ID, or None if no conversations exist.
        """
        logger.debug("Getting conversation ID for notebook %s", notebook_id)
        return await self._invoke(self._workflow.get_conversation_id(notebook_id))

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
            result = await self._invoke(
                self._workflow.get_history(notebook_id, conv_id, limit=limit)
            )
        except (ChatError, NetworkError) as e:
            logger.warning("Failed to fetch conversation turns for %s: %s", notebook_id, e)
            return []
        return parse_recorded_turns_to_qa_pairs(result, oldest_first=True)

    @staticmethod
    def _parse_turns_to_qa_pairs(turns_data: Any) -> list[tuple[str, str]]:
        """Parse raw turn data into (question, answer) pairs in array order.

        Pairs are returned in the same order as the input data (newest-first
        from the API); callers reverse if oldest-first is needed. Each user
        question (role 1) is followed by its AI answer (role 2); per-turn
        positions live in :class:`~notebooklm._row_adapters.chat.ConversationTurnRow`.

        Drift handling (#1485): an empty/absent history parses to ``[]``; a
        truthy-but-malformed payload/container raises ``UnknownRPCMethodError``
        via ``unwrap_conversation_turns``; a malformed turn row or an
        unrecognized role code is skipped with a DEBUG diagnostic (ordinary
        unpaired answer rows are consumed by pairing and never logged).
        """
        return parse_legacy_turns_to_qa_pairs(turns_data)

    def get_cached_turns(self, conversation_id: str) -> list[ConversationTurn]:
        """Get locally cached conversation turns.

        Args:
            conversation_id: The conversation ID.

        Returns:
            List of ConversationTurn objects.
        """
        return [
            ConversationTurn(
                query=turn.query,
                answer=turn.answer,
                turn_number=turn.turn_number,
            )
            for turn in self._workflow.cached_turns(conversation_id)
        ]

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
        # Catch cross-loop misuse before the workflow acquires the
        # per-conversation lock (like ``ask``), so a client reused from another
        # loop fails fast rather than hang on a dead-loop lock.
        # ``set_bound_loop`` / ``reset_after_open`` (#1225) only reset locks on
        # *reopen*; an open cross-loop client raises.
        self._loop_guard.assert_bound_loop()
        logger.debug("Deleting conversation %s in notebook %s", conversation_id, notebook_id)
        await self._invoke(self._workflow.delete_conversation(notebook_id, conversation_id))
        # v0.8.0 (#1290): the uninformative always-``True`` return becomes ``None``.
        return None

    def clear_cache(self, conversation_id: str | None = None) -> bool:
        """Clear conversation cache.

        Args:
            conversation_id: Clear specific conversation, or all if None.

        Returns:
            True if cache was cleared.
        """
        return self._workflow.clear_cache(conversation_id)

    def cache_size(self) -> int:
        """Return the number of conversations currently held in the cache.

        Surfaced for CLI ``history --clear --json`` so the emitted envelope
        can report how many conversations were dropped without reaching
        into the workflow's cache from the CLI layer.
        """
        return self._workflow.cache_size()

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
        logger.debug("Configuring chat for notebook %s", notebook_id)

        if goal is None:
            goal = ChatGoal.DEFAULT
        if response_length is None:
            response_length = ChatResponseLength.DEFAULT

        if goal == ChatGoal.CUSTOM and not custom_prompt:
            raise ValidationError("custom_prompt is required when goal is CUSTOM")

        await self._invoke(
            self._workflow.configure(
                ChatConfigureInput(
                    notebook_id=notebook_id,
                    action=ChatConfigureAction.SET,
                    goal=goal.name.lower(),
                    response_length=response_length.name.lower(),
                    custom_prompt=custom_prompt,
                )
            )
        )

    async def set_mode(self, notebook_id: str, mode: ChatMode) -> None:
        """Set chat mode using predefined configurations.

        Args:
            notebook_id: The notebook ID.
            mode: Predefined ChatMode (DEFAULT, LEARNING_GUIDE, CONCISE, DETAILED).
        """

        mode_configs = {
            ChatMode.DEFAULT: (ChatGoal.DEFAULT, ChatResponseLength.DEFAULT, None),
            ChatMode.LEARNING_GUIDE: (ChatGoal.LEARNING_GUIDE, ChatResponseLength.LONGER, None),
            ChatMode.CONCISE: (ChatGoal.DEFAULT, ChatResponseLength.SHORTER, None),
            ChatMode.DETAILED: (ChatGoal.DEFAULT, ChatResponseLength.LONGER, None),
        }

        goal, length, prompt = mode_configs[mode]
        await self.configure(notebook_id, goal, length, prompt)

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
        result = await self._invoke(
            self._workflow.configure(
                ChatConfigureInput(
                    notebook_id=notebook_id,
                    action=ChatConfigureAction.GET,
                )
            )
        )
        if result.settings is None:
            raise RuntimeError("chat.configure GET returned no settings")
        return project_chat_settings(result.settings)

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
        result = await self._invoke(
            self._workflow.save_note(
                ChatSaveNoteInput(
                    notebook_id=notebook_id,
                    answer=ask_result.answer,
                    references=tuple(
                        chat_reference_record(reference) for reference in ask_result.references
                    ),
                    title=resolved_title,
                )
            )
        )
        return project_chat_saved_note(result.note)
