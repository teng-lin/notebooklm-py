"""Chat API for NotebookLM notebook conversations.

Provides operations for asking questions, managing conversations, and
retrieving conversation history.
"""

import asyncio
import json
import logging
import re
import uuid
import weakref
from typing import Any
from urllib.parse import quote, urlencode

from ._core import ClientCore, _AuthSnapshot
from ._env import get_default_bl, get_default_language
from ._logging import get_request_id, reset_request_id, set_request_id
from .auth import format_authuser_value
from .exceptions import ChatError, NetworkError, ValidationError
from .rpc import (
    ChatGoal,
    ChatResponseLength,
    RPCMethod,
    get_query_url,
    nest_source_ids,
    safe_index,
)
from .types import AskResult, ChatMode, ChatReference, ConversationTurn

logger = logging.getLogger(__name__)

# UUID pattern for validating source IDs (compiled once at module level)
_UUID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


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
        """Assemble ``(url, body, extra_headers)`` for one chat HTTP attempt.

        Called by ``core.query_post`` once per attempt with a fresh
        ``_AuthSnapshot``; on retry, a new snapshot picks up the refreshed
        CSRF / session id / account routing so the rebuilt request matches
        the post-refresh credentials byte-for-byte.

        Args:
            snapshot: Point-in-time auth state for this attempt. The
                ``csrf_token`` is embedded in the body's ``at=`` field; the
                ``session_id`` and account routing fields go into URL params.
            notebook_id: Owning notebook id (echoed at params[7] for
                server-side conversation persistence).
            question: User-facing question text.
            source_ids: Sources to scope this turn against. Encoded
                triple-nested via :func:`nest_source_ids`.
            conversation_history: Prior turns for follow-up Q&A, or ``None``
                when starting a new conversation.
            conversation_id: Stable conversation identifier. Locally minted
                UUID for new conversations, server-assigned for follow-ups.
            reqid: Monotonic request id from :meth:`ClientCore.next_reqid`.

        Returns:
            ``(url, body, extra_headers)``. ``extra_headers`` is empty
            because the chat endpoint inherits Content-Type from the shared
            httpx client.
        """
        sources_array = nest_source_ids(source_ids, 2)

        params: list[Any] = [
            sources_array,
            question,
            conversation_history,
            [2, None, [1], [1]],
            conversation_id,
            None,  # [5] - always null
            None,  # [6] - always null
            notebook_id,  # [7] - required for server-side conversation persistence
            1,  # [8] - always 1
        ]

        params_json = json.dumps(params, separators=(",", ":"))
        f_req_json = json.dumps([None, params_json], separators=(",", ":"))
        encoded_req = quote(f_req_json, safe="")

        body_parts = [f"f.req={encoded_req}"]
        if snapshot.csrf_token:
            encoded_at = quote(snapshot.csrf_token, safe="")
            body_parts.append(f"at={encoded_at}")
        body = "&".join(body_parts) + "&"

        url_params: dict[str, str] = {
            "bl": get_default_bl(),
            "hl": get_default_language(),
            "_reqid": str(reqid),
            "rt": "c",
        }
        if snapshot.session_id:
            url_params["f.sid"] = snapshot.session_id
        # Multi-account routing: previously omitted entirely on the chat
        # endpoint (audit C2 / M1), which silently routed requests to the
        # default browser account when the auth tokens were minted for a
        # non-default profile. Mirrors the batchexecute path in
        # ``ClientCore._build_url``.
        if snapshot.account_email or snapshot.authuser:
            url_params["authuser"] = format_authuser_value(
                snapshot.authuser,
                snapshot.account_email,
            )

        url = f"{get_query_url()}?{urlencode(url_params)}"
        return url, body, {}

    def _parse_ask_response_with_references(
        self, response_text: str
    ) -> tuple[str, list[ChatReference], str | None]:
        """Parse the streaming response to extract answer, references, and conversation ID.

        Returns:
            Tuple of (answer_text, list of ChatReference objects, server_conversation_id).
            server_conversation_id is None if not present in the response.
        """

        if response_text.startswith(")]}'"):
            response_text = response_text[4:]

        lines = response_text.strip().split("\n")
        best_marked_answer = ""
        best_marked_refs: list[ChatReference] = []
        best_unmarked_answer = ""
        best_unmarked_refs: list[ChatReference] = []
        server_conv_id: str | None = None

        def process_chunk(json_str: str) -> None:
            """Process a JSON chunk, updating best answer candidates and their refs."""
            nonlocal best_marked_answer, best_marked_refs
            nonlocal best_unmarked_answer, best_unmarked_refs
            nonlocal server_conv_id
            text, is_answer, refs, conv_id = self._extract_answer_and_refs_from_chunk(json_str)
            if text:
                if is_answer and len(text) > len(best_marked_answer):
                    best_marked_answer = text
                    best_marked_refs = refs
                elif not is_answer and len(text) > len(best_unmarked_answer):
                    best_unmarked_answer = text
                    best_unmarked_refs = refs
            if conv_id:
                server_conv_id = conv_id

        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if not line:
                i += 1
                continue

            try:
                int(line)
                i += 1
                if i < len(lines):
                    process_chunk(lines[i])
                i += 1
            except ValueError:
                process_chunk(line)
                i += 1

        # Prefer marked answers; fall back to longest unmarked text
        if best_marked_answer:
            longest_answer = best_marked_answer
            final_refs = best_marked_refs
        elif best_unmarked_answer:
            logger.warning(
                "No marked answer found; falling back to longest unmarked "
                "text (%d chars). The API response format may have changed.",
                len(best_unmarked_answer),
            )
            longest_answer = best_unmarked_answer
            final_refs = best_unmarked_refs
        else:
            longest_answer = ""
            final_refs = []

        if not longest_answer:
            logger.warning(
                "No answer extracted from response (%d lines parsed)",
                len(lines),
            )

        # Assign citation numbers based on order of appearance
        for idx, ref in enumerate(final_refs, start=1):
            if ref.citation_number is None:
                ref.citation_number = idx

        return longest_answer, final_refs, server_conv_id

    def _extract_answer_and_refs_from_chunk(
        self, json_str: str
    ) -> tuple[str | None, bool, list[ChatReference], str | None]:
        """Extract answer text, references, and conversation ID from a response chunk.

        Response structure (discovered via reverse engineering):
        - first[0]: answer text
        - first[1]: None
        - first[2]: [conversation_id, numeric_hash]
        - first[3]: None
        - first[4]: Citation metadata
          - first[4][0]: Per-source citation positions with text spans
          - first[4][3]: Detailed citation array with structure:
            - cite[0][0]: chunk ID
            - cite[1][2]: relevance score
            - cite[1][4]: array of [text_passage, char_positions] items
            - cite[1][5][0][0][0]: parent SOURCE ID (this is the real source UUID)

        When item[2] is null and item[5] contains a UserDisplayableError, raises
        ChatError with a rate-limit message.

        Returns:
            Tuple of (text, is_answer, references, server_conversation_id).
        """
        refs: list[ChatReference] = []

        try:
            data = json.loads(json_str)
        except json.JSONDecodeError:
            return None, False, refs, None

        if not isinstance(data, list):
            return None, False, refs, None

        for item in data:
            if not isinstance(item, list) or len(item) < 3:
                continue
            if item[0] != "wrb.fr":
                continue

            inner_json = item[2]
            if not isinstance(inner_json, str):
                # item[2] is null — check item[5] for a server-side error payload
                if len(item) > 5 and isinstance(item[5], list):
                    self._raise_if_rate_limited(item[5])
                continue

            try:
                inner_data = json.loads(inner_json)
                if isinstance(inner_data, list) and len(inner_data) > 0:
                    first = inner_data[0]
                    if isinstance(first, list) and len(first) > 0:
                        text = first[0]
                        if not isinstance(text, str) or not text:
                            continue

                        is_answer = (
                            len(first) > 4
                            and isinstance(first[4], list)
                            and len(first[4]) > 0
                            and first[4][-1] == 1
                        )

                        # Extract the server-assigned conversation ID from first[2]
                        server_conv_id: str | None = None
                        if (
                            len(first) > 2
                            and isinstance(first[2], list)
                            and first[2]
                            and isinstance(first[2][0], str)
                        ):
                            server_conv_id = first[2][0]

                        refs = self._parse_citations(first)
                        return text, is_answer, refs, server_conv_id
            except json.JSONDecodeError:
                # Hot-path stream parser: skip non-JSON chunks. Guard the
                # debug log with isEnabledFor so the redaction regex doesn't
                # run on every chunk when DEBUG is off.
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug("Stream parser: non-JSON chunk skipped")
                continue

        return None, False, refs, None

    def _raise_if_rate_limited(self, error_payload: list) -> None:
        """Raise ChatError if the payload contains a UserDisplayableError.

        Args:
            error_payload: The item[5] list from a wrb.fr response chunk.

        Raises:
            ChatError: When a UserDisplayableError is detected.
        """
        try:
            # Structure: [8, None, [["type.googleapis.com/.../UserDisplayableError", ...]]]
            if len(error_payload) > 2 and isinstance(error_payload[2], list):
                for entry in error_payload[2]:
                    if isinstance(entry, list) and entry and isinstance(entry[0], str):
                        if "UserDisplayableError" in entry[0]:
                            raise ChatError(
                                "Chat request was rate limited or rejected by the API. "
                                "Wait a few seconds and try again."
                            )
        except ChatError:
            raise
        except Exception:
            logger.debug(
                "Could not parse chat error payload; continuing with empty-answer handling",
                exc_info=True,
            )

    def _parse_citations(self, first: list) -> list[ChatReference]:
        """Parse citation details from response structure.

        The citation data is in first[4][3], which contains an array of citations.
        Each citation has:
          - cite[0][0]: chunk ID (internal reference)
          - cite[1][4]: array of text passages with character positions
          - cite[1][5]: nested structure containing the parent SOURCE ID (UUID)

        Note:
            This parsing relies on reverse-engineered response structures that
            Google can change at any time. Parsing failures are logged and
            result in graceful degradation (empty references list).

        Args:
            first: The first element of the parsed response.

        Returns:
            List of ChatReference objects with source IDs and cited text.
        """
        try:
            # Validate path to citations array: first[4][3]
            if len(first) <= 4 or not isinstance(first[4], list):
                return []
            type_info = first[4]
            if len(type_info) <= 3 or not isinstance(type_info[3], list):
                return []

            refs: list[ChatReference] = []
            for cite in type_info[3]:
                ref = self._parse_single_citation(cite)
                if ref is not None:
                    refs.append(ref)
            return refs
        except (IndexError, TypeError, AttributeError) as e:
            logger.debug(
                "Citation parsing failed (API structure may have changed): %s",
                e,
                exc_info=True,
            )
            return []

    def _parse_single_citation(self, cite: Any) -> ChatReference | None:
        """Parse a single citation entry into a ChatReference.

        Args:
            cite: A citation entry from the citations array.

        Returns:
            ChatReference if valid source ID found, None otherwise.
        """
        if not isinstance(cite, list) or len(cite) < 2:
            return None

        cite_inner = cite[1]
        if not isinstance(cite_inner, list):
            return None

        # Extract source ID from cite[1][5] - required for valid reference
        source_id_data = cite_inner[5] if len(cite_inner) > 5 else None
        source_id = self._extract_uuid_from_nested(source_id_data)
        if source_id is None:
            return None

        # Extract chunk ID from cite[0][0]
        chunk_id = None
        if isinstance(cite[0], list) and cite[0]:
            first_item = cite[0][0]
            if isinstance(first_item, str):
                chunk_id = first_item

        # Extract text passages and char positions from cite[1][4]
        cited_text, start_char, end_char = self._extract_text_passages(cite_inner)

        return ChatReference(
            source_id=source_id,
            cited_text=cited_text,
            start_char=start_char,
            end_char=end_char,
            chunk_id=chunk_id,
        )

    def _extract_text_passages(self, cite_inner: list) -> tuple[str | None, int | None, int | None]:
        """Extract cited text and character positions from citation data.

        Structure (discovered via analysis):
          cite_inner[4] = [[passage_data, ...], ...]
          passage_data = [start_char, end_char, nested_passages]
          nested_passages contains text at varying depths

        Args:
            cite_inner: The inner citation data (cite[1]).

        Returns:
            Tuple of (cited_text, start_char, end_char).
        """
        if len(cite_inner) <= 4 or not isinstance(cite_inner[4], list):
            return None, None, None

        texts: list[str] = []
        start_char: int | None = None
        end_char: int | None = None

        for passage_wrapper in cite_inner[4]:
            if not isinstance(passage_wrapper, list) or not passage_wrapper:
                continue
            passage_data = passage_wrapper[0]
            if not isinstance(passage_data, list) or len(passage_data) < 3:
                continue

            # Extract char positions from first valid passage
            if start_char is None and isinstance(passage_data[0], int):
                start_char = passage_data[0]
            if isinstance(passage_data[1], int):
                end_char = passage_data[1]

            # Extract text from nested structure
            self._collect_texts_from_nested(passage_data[2], texts)

        cited_text = " ".join(texts) if texts else None
        return cited_text, start_char, end_char

    def _collect_texts_from_nested(self, nested: Any, texts: list[str]) -> None:
        """Collect text strings from deeply nested passage structure.

        The text can appear at various levels of nesting. This walks through
        the structure looking for [start, end, text_value] triplets.

        Args:
            nested: Nested list structure to search.
            texts: List to append found text strings to.
        """
        if not isinstance(nested, list):
            return

        for nested_group in nested:
            if not isinstance(nested_group, list):
                continue
            for inner in nested_group:
                if not isinstance(inner, list) or len(inner) < 3:
                    continue
                text_val = inner[2]
                if isinstance(text_val, str) and text_val.strip():
                    texts.append(text_val.strip())
                elif isinstance(text_val, list):
                    for item in text_val:
                        if isinstance(item, str) and item.strip():
                            texts.append(item.strip())

    def _extract_uuid_from_nested(self, data: Any, max_depth: int = 10) -> str | None:
        """Recursively extract a UUID from nested list structures.

        The API returns source IDs in deeply nested list structures that can vary.
        This walks through the nesting to find the first valid UUID string.

        Args:
            data: Nested list data to search.
            max_depth: Maximum recursion depth to prevent stack overflow.

        Returns:
            UUID string if found, None otherwise.
        """
        if max_depth <= 0:
            logger.warning("Max recursion depth reached in UUID extraction")
            return None

        if data is None:
            return None

        if isinstance(data, str):
            return data if _UUID_PATTERN.match(data) else None

        if isinstance(data, list):
            for item in data:
                result = self._extract_uuid_from_nested(item, max_depth - 1)
                if result is not None:
                    return result

        return None
