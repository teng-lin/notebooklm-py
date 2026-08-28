"""Backend-neutral chat semantics and orchestration."""

from __future__ import annotations

import asyncio
import logging
import re
import weakref
from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from typing import Any

from .._conversation_cache import ConversationCache
from .._loop_bound import LoopBoundPrimitive
from .._notebook_metadata import CreatedChatSessionProvider, NotebookSourceIdProvider
from .._runtime.contracts import LoopGuard
from .._types.documents import StructuredDocument, utf16_len
from .._types.enums import ChatGoal, ChatResponseLength
from ..exceptions import ChatError, NetworkError
from ..types import (
    AskResult,
    ChatMode,
    ChatReference,
    ChatSettings,
    ConversationTurn,
    ConversationTurnKey,
    NextStepSuggestion,
    Note,
)
from .deleted_tracker import RecentlyDeletedConversations

logger = logging.getLogger("notebooklm._chat.api")
notes_logger = logging.getLogger("notebooklm._chat.notes")

_TURN_COUNT_INITIAL_LIMIT = 100
_TURN_COUNT_MAX_LIMIT = 12_800
_CITATION_MARKER_RE = re.compile(r" ?\[(\d+)\]")


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
    """Backend-neutral operations for notebook chat and conversations.

    Provides shared workflows for asking questions, managing conversation
    history, and saving answers as notes. Concrete backends implement the
    transport reads and the three protected send hooks.

    Usage:
        async with NotebookLMClient.from_storage() as client:
            result = await client.chat.ask(notebook_id, "What is X?")
            result = await client.chat.ask(
                notebook_id,
                "Can you elaborate?",
                conversation_id=result.conversation_id,
            )
    """

    def __init__(
        self,
        *,
        loop_guard: LoopGuard,
        notebooks: NotebookSourceIdProvider,
        conversation_cache: ConversationCache | None = None,
        created_chat_sessions: CreatedChatSessionProvider | None = None,
    ) -> None:
        """Initialize backend-neutral chat state.

        Args:
            loop_guard: Collaborator whose ``assert_bound_loop`` check runs
                before lock acquisition in shared ask/delete workflows.
            notebooks: Required source-ID resolver. The composition root passes
                the client's shared notebooks API.
            conversation_cache: Optional injected cache; defaults to a fresh
                per-instance ``ConversationCache``.
            created_chat_sessions: Optional one-shot provider for an initial
                session ID returned by notebook creation.
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
        """Rebuild lazy conversation locks after a client reopen."""
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
            roles = await self._list_turn_roles(notebook_id, conversation_id, limit)
            row_count = len(roles)
            question_count = sum(role == 1 for role in roles)
            if row_count < limit:
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
        """Ask a question while preserving conversation and cache semantics."""
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
        """Return typed turns from the local conversation cache."""
        cached = self._cache.get_cached_conversation(conversation_id)
        return [
            ConversationTurn(
                query=turn["query"],
                answer=turn["answer"],
                turn_number=turn["turn_number"],
            )
            for turn in cached
        ]

    async def delete_conversation(self, notebook_id: str, conversation_id: str) -> None:
        """Delete server turns, then clear local state atomically."""
        self._loop_guard.assert_bound_loop()
        logger.debug("Deleting conversation %s in notebook %s", conversation_id, notebook_id)
        async with self._get_conversation_lock(conversation_id):
            await self._send_delete_conversation(notebook_id, conversation_id)
            self._cache.clear(conversation_id)
            self._deleted_conversations.record(conversation_id)
        return None

    def clear_cache(self, conversation_id: str | None = None) -> bool:
        """Clear one cached conversation, or all cached conversations."""
        return self._cache.clear(conversation_id)

    def cache_size(self) -> int:
        """Return the number of cached conversations."""
        return len(self._cache.conversations)

    async def set_mode(self, notebook_id: str, mode: ChatMode) -> None:
        """Apply a predefined chat configuration."""
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
        """Save a chat answer as a citation-rich note."""
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

    @abstractmethod
    async def configure(
        self,
        notebook_id: str,
        goal: ChatGoal | None = None,
        response_length: ChatResponseLength | None = None,
        custom_prompt: str | None = None,
    ) -> None:
        """Persist chat configuration."""

    @abstractmethod
    async def get_settings(self, notebook_id: str) -> ChatSettings:
        """Return decoded chat settings."""

    @abstractmethod
    async def _list_turn_roles(
        self,
        notebook_id: str,
        conversation_id: str,
        limit: int,
    ) -> list[object]:
        """Return one decoded role value per backend turn row."""

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


__all__ = ["ChatAPI"]
