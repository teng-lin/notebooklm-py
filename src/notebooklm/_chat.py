"""Chat API for NotebookLM notebook conversations.

Provides operations for asking questions, managing conversations, and
retrieving conversation history.
"""

import asyncio
import logging
import uuid
import weakref
from typing import Any

from ._chat_protocol import (
    build_streaming_chat_request,
    collect_texts_from_nested,
    extract_answer_and_refs_from_chunk,
    extract_text_passages,
    extract_uuid_from_nested,
    parse_citations,
    parse_single_citation,
    parse_streaming_chat_response,
    raise_if_rate_limited,
)
from ._core import ClientCore, _AuthSnapshot
from ._logging import get_request_id, reset_request_id, set_request_id
from .exceptions import ChatError, NetworkError, ValidationError
from .rpc import (
    ChatGoal,
    ChatResponseLength,
    RPCMethod,
    safe_index,
)
from .types import AskResult, ChatMode, ChatReference, ConversationTurn

logger = logging.getLogger(__name__)


def _extract_next_turn_content(next_turn: Any) -> str | None:
    """Extract the response content from a streaming-chat next_turn frame.

    The ``khqZz`` (``GET_CONVERSATION_TURNS``) response packs each AI answer
    as ``turn[4][0][0]`` — three nested wrappers around the answer text. This
    helper delegates the inner-most descent to :func:`safe_index` so soft-mode
    shape drift (``NOTEBOOKLM_STRICT_DECODE`` unset) logs a structured warning
    and returns ``None`` instead of raising; strict-decode mode still lets
    :func:`safe_index` raise ``UnknownRPCMethodError`` so callers fail fast.

    Args:
        next_turn: The candidate answer turn (a ``turn[2] == 2`` row from the
            ``khqZz`` payload). Caller has already validated this is a list
            with ``len(next_turn) > 4`` and ``next_turn[2] == 2``.

    Returns:
        The answer-text string on success. ``None`` when the inner
        ``[4][0][0]`` chain drifts or the leaf is not a string — callers
        should fall back to an empty-answer pair so chat history rendering
        degrades gracefully rather than crashing.
    """
    content = safe_index(
        next_turn,
        4,
        0,
        0,
        method_id=RPCMethod.GET_CONVERSATION_TURNS.value,
        source="_chat._extract_next_turn_content",
    )
    if content is None:
        return None
    if not isinstance(content, str):
        # The schema-drift contract returns ``None`` for non-string leaves so
        # the caller's empty-answer fallback fires uniformly. A non-string
        # leaf is rare enough to warrant a debug breadcrumb but not a warning
        # (safe_index already emitted one for genuine descent failures).
        logger.debug(
            "next_turn content is not a string (type=%s); treating as drift",
            type(content).__name__,
        )
        return None
    return content


class ChatAPI:
    """Operations for notebook chat/conversations.

    Provides methods for asking questions to notebooks and managing
    conversation history with follow-up support.

    Usage:
        async with await NotebookLMClient.from_storage() as client:
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

    def __init__(self, core: ClientCore):
        """Initialize the chat API.

        Args:
            core: The core client infrastructure.
        """
        self._core = core
        # Per-``conversation_id`` lock that serializes follow-up asks on the
        # same conversation (audit §10 / T7.F1). Without this, two
        # ``asyncio.gather``'d ``ask`` calls on the same conversation read
        # identical pre-update history at the top, both POST that history,
        # then race to append to ``_core._conversation_cache`` — the server
        # sees two follow-ups both claiming to be turn N+1 and the local
        # cache loses one turn's lineage.
        #
        # ``WeakValueDictionary`` keeps the map bounded automatically:
        # callers hold a strong reference to the lock while inside
        # ``async with lock:``; once every waiter releases, the entry GCs
        # itself and the key is removed. The cost is per-key churn for
        # one-shot conversations, which is negligible compared to the
        # round-trip we're protecting.
        self._conversation_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )

    def _get_conversation_lock(self, conversation_id: str) -> asyncio.Lock:
        """Return the (lazily created) lock for ``conversation_id``.

        Single-threaded asyncio means ``WeakValueDictionary.get`` /
        ``__setitem__`` are atomic w.r.t. coroutine interleaving — no
        ``await`` between the lookup and the insert, so two concurrent
        callers on the same conversation either both see the existing lock
        or one creates it and the other reads it. Either way they share a
        single lock instance.

        Returning the bare lock (vs. an async-context-manager wrapper) so
        callers use ``async with self._get_conversation_lock(cid):`` and
        the strong reference to ``lock`` keeps the WeakValueDictionary
        entry alive for the duration of the critical section.
        """
        lock = self._conversation_locks.get(conversation_id)
        if lock is None:
            lock = asyncio.Lock()
            self._conversation_locks[conversation_id] = lock
        return lock

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

        Returns:
            AskResult with answer, conversation_id, and turn info.

        Example:
            # New conversation
            result = await client.chat.ask(notebook_id, "What is machine learning?")

            # Follow-up
            result = await client.chat.ask(
                notebook_id,
                "How does it differ from deep learning?",
                conversation_id=result.conversation_id
            )
        """
        logger.debug(
            "Asking question in notebook %s (conversation=%s)",
            notebook_id,
            conversation_id or "new",
        )
        if source_ids is None:
            source_ids = await self._core.get_source_ids(notebook_id)

        is_new_conversation = conversation_id is None
        if is_new_conversation:
            conversation_id = str(uuid.uuid4())
        assert conversation_id is not None  # Type narrowing for mypy

        # Hold the per-``conversation_id`` lock across history-build, the
        # network round-trip, and the cache append (audit §10 / T7.F1).
        # Without this, two ``asyncio.gather``'d follow-ups on the same
        # conversation read identical pre-update history at the top, both
        # POST that history, and the server sees two follow-ups both
        # claiming to be turn N+1. New conversations use a fresh UUID so
        # the lookup is uncontested — the overhead is a no-op
        # ``WeakValueDictionary`` insert + a single uncontested
        # ``asyncio.Lock`` acquire.
        async with self._get_conversation_lock(conversation_id):
            if is_new_conversation:
                conversation_history = None
            else:
                conversation_history = self._build_conversation_history(conversation_id)
            # Rebind to non-Optional locals so the build_request closure below
            # carries the narrowed types — mypy doesn't propagate flow-narrowing
            # of ``conversation_id`` / ``source_ids`` through a nested-function
            # capture. ``_build_chat_request`` declares both as non-Optional, so
            # the closure capture would otherwise be a latent type error.
            active_conversation_id: str = conversation_id
            active_source_ids: list[str] = source_ids

            # Mint the request-id under the asyncio-safe counter helper so two
            # concurrent ``ask`` calls on the same client never collide (audit C3,
            # synthesis §6 Tier-2 item 2). The previous direct mutation
            # ``self._core._reqid_counter += 100000`` raced under
            # ``asyncio.gather`` and produced duplicate ``_reqid`` URL params.
            reqid = await self._core.next_reqid()

            def build_request(snapshot: _AuthSnapshot) -> tuple[str, str, dict[str, str]]:
                return self._build_chat_request(
                    snapshot=snapshot,
                    notebook_id=notebook_id,
                    question=question,
                    source_ids=active_source_ids,
                    conversation_history=conversation_history,
                    conversation_id=active_conversation_id,
                    reqid=reqid,
                )

            # ``query_post`` owns the retry/refresh/429 transport pipeline plus
            # the transport→ChatError/NetworkError mapping that ``ask`` used to
            # duplicate inline. The request-id context lives here so retries
            # inside the helper share the same ``[req=<id>]`` log prefix as the
            # initial attempt.
            reqid_token = None if get_request_id() is not None else set_request_id()
            try:
                response = await self._core.query_post(
                    build_request=build_request,
                    parse_label="chat.ask",
                )
            finally:
                if reqid_token is not None:
                    reset_request_id(reqid_token)

            answer_text, references, server_conv_id = self._parse_ask_response_with_references(
                response.text
            )
            # Prefer the conversation ID returned by the server over our locally
            # generated UUID, so that get_conversation_id() and
            # get_conversation_turns() stay in sync.
            if server_conv_id:
                conversation_id = server_conv_id

            turns = self._core.get_cached_conversation(conversation_id)
            if answer_text:
                turn_number = len(turns) + 1
                self._core.cache_conversation_turn(
                    conversation_id, question, answer_text, turn_number
                )
            else:
                turn_number = len(turns)

        return AskResult(
            answer=answer_text,
            conversation_id=conversation_id,
            turn_number=turn_number,
            is_follow_up=not is_new_conversation,
            references=references,
            raw_response=response.text[:1000],
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
            Raw turn data from API. Each turn has:
              turn[2] == 1: user question, text at turn[3]
              turn[2] == 2: AI answer, text at turn[4][0][0]
        """
        logger.debug(
            "Getting conversation turns for %s (conversation=%s, limit=%d)",
            notebook_id,
            conversation_id,
            limit,
        )
        params: list[Any] = [[], None, None, conversation_id, limit]
        return await self._core.rpc_call(
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
        raw = await self._core.rpc_call(
            RPCMethod.GET_LAST_CONVERSATION_ID,
            params,
            source_path=f"/notebook/{notebook_id}",
        )
        # Response structure: [[[conv_id]]]
        if raw and isinstance(raw, list):
            for group in raw:
                if isinstance(group, list):
                    for conv in group:
                        if isinstance(conv, list) and conv and isinstance(conv[0], str):
                            return conv[0]
            logger.debug(
                "No conversation ID found in response (API structure may have changed): %s",
                raw,
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
        except (ChatError, NetworkError) as e:
            logger.warning("Failed to fetch conversation turns for %s: %s", notebook_id, e)
            return []
        # API returns individual turns newest-first: [A2, Q2, A1, Q1, ...]
        # Reverse to chronological order [Q1, A1, Q2, A2, ...] so the
        # Q→A forward-pairing parser works correctly.
        if (
            turns_data
            and isinstance(turns_data, list)
            and turns_data[0]
            and isinstance(turns_data[0], list)
        ):
            turns_data = [list(reversed(turns_data[0]))]
        return self._parse_turns_to_qa_pairs(turns_data)

    @staticmethod
    def _parse_turns_to_qa_pairs(turns_data: Any) -> list[tuple[str, str]]:
        """Parse raw turn data into (question, answer) pairs in array order.

        Pairs are returned in the same order as the input data (newest-first
        from the API). Callers should reverse if oldest-first is needed.
        Each user question (turn[2]==1) is followed by its AI answer (turn[2]==2).
        """
        if not turns_data or not isinstance(turns_data, list):
            return []
        first = turns_data[0]
        if not isinstance(first, list):
            return []

        turns = first

        pairs: list[tuple[str, str]] = []
        i = 0
        while i < len(turns):
            turn = turns[i]
            if not isinstance(turn, list) or len(turn) < 3:
                i += 1
                continue
            if turn[2] == 1 and len(turn) > 3:
                q = str(turn[3] or "")
                a = ""
                # Look for the answer immediately following
                if i + 1 < len(turns):
                    next_turn = turns[i + 1]
                    if isinstance(next_turn, list) and len(next_turn) > 4 and next_turn[2] == 2:
                        # Named extractor folds the previous ``try/except`` —
                        # ``safe_index`` handles schema-drift logging and
                        # returns ``None`` so the empty-answer fallback fires
                        # uniformly. Strict-decode mode still raises through.
                        content = _extract_next_turn_content(next_turn)
                        a = str(content or "")
                        i += 1  # skip the answer turn
                pairs.append((q, a))
            i += 1
        return pairs

    def get_cached_turns(self, conversation_id: str) -> list[ConversationTurn]:
        """Get locally cached conversation turns.

        Args:
            conversation_id: The conversation ID.

        Returns:
            List of ConversationTurn objects.
        """
        cached = self._core.get_cached_conversation(conversation_id)
        return [
            ConversationTurn(
                query=turn["query"],
                answer=turn["answer"],
                turn_number=turn["turn_number"],
            )
            for turn in cached
        ]

    def clear_cache(self, conversation_id: str | None = None) -> bool:
        """Clear conversation cache.

        Args:
            conversation_id: Clear specific conversation, or all if None.

        Returns:
            True if cache was cleared.
        """
        return self._core.clear_conversation_cache(conversation_id)

    async def configure(
        self,
        notebook_id: str,
        goal: ChatGoal | None = None,
        response_length: ChatResponseLength | None = None,
        custom_prompt: str | None = None,
    ) -> None:
        """Configure chat persona and response settings for a notebook.

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

        goal_array = [goal.value, custom_prompt] if goal == ChatGoal.CUSTOM else [goal.value]

        chat_settings = [goal_array, [response_length.value]]
        params = [
            notebook_id,
            [[None, None, None, None, None, None, None, chat_settings]],
        ]

        await self._core.rpc_call(
            RPCMethod.RENAME_NOTEBOOK,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
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

    # =========================================================================
    # Private Helpers
    # =========================================================================

    def _build_conversation_history(self, conversation_id: str) -> list | None:
        """Build conversation history for follow-up requests."""
        turns = self._core.get_cached_conversation(conversation_id)
        if not turns:
            return None

        history = []
        for turn in turns:
            history.append([turn["answer"], None, 2])
            history.append([turn["query"], None, 1])
        return history

    def _build_chat_request(
        self,
        *,
        snapshot: _AuthSnapshot,
        notebook_id: str,
        question: str,
        source_ids: list[str],
        conversation_history: list | None,
        conversation_id: str,
        reqid: int,
    ) -> tuple[str, str, dict[str, str]]:
        """Compatibility wrapper for streamed-chat request construction."""
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
        self, response_text: str
    ) -> tuple[str, list[ChatReference], str | None]:
        """Compatibility wrapper preserving the old tuple return shape."""
        result = parse_streaming_chat_response(response_text)
        return result.answer, result.references, result.conversation_id

    def _extract_answer_and_refs_from_chunk(
        self, json_str: str
    ) -> tuple[str | None, bool, list[ChatReference], str | None]:
        """Compatibility wrapper for streamed-chat chunk parsing."""
        return extract_answer_and_refs_from_chunk(json_str)

    def _raise_if_rate_limited(self, error_payload: list) -> None:
        """Compatibility wrapper for streamed-chat error payload parsing."""
        raise_if_rate_limited(error_payload)

    def _parse_citations(self, first: list) -> list[ChatReference]:
        """Compatibility wrapper for streamed-chat citation parsing."""
        return parse_citations(first)

    def _parse_single_citation(self, cite: Any) -> ChatReference | None:
        """Compatibility wrapper for single streamed-chat citation parsing."""
        return parse_single_citation(cite)

    def _extract_text_passages(self, cite_inner: list) -> tuple[str | None, int | None, int | None]:
        """Compatibility wrapper for streamed-chat citation text extraction."""
        return extract_text_passages(cite_inner)

    def _collect_texts_from_nested(self, nested: Any, texts: list[str]) -> None:
        """Compatibility wrapper for streamed-chat nested text collection."""
        collect_texts_from_nested(nested, texts)

    def _extract_uuid_from_nested(self, data: Any, max_depth: int = 10) -> str | None:
        """Compatibility wrapper for streamed-chat source UUID extraction."""
        return extract_uuid_from_nested(data, max_depth)
