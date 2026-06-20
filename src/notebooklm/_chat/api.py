"""Chat API for NotebookLM notebook conversations.

Provides operations for asking questions, managing conversations, and
retrieving conversation history.
"""

from __future__ import annotations

import asyncio
import logging
import reprlib
import weakref
from typing import TYPE_CHECKING, Any

from .._conversation_cache import ConversationCache
from .._logging import get_request_id, reset_request_id, set_request_id
from .._loop_bound import LoopBoundPrimitive
from .._notebook_metadata import NotebookSourceIdProvider
from .._request_types import AuthSnapshot
from .._row_adapters.chat import (
    ConversationTurnRow,
    unwrap_conversation_turns,
    unwrap_last_conversation_id,
)
from .._runtime.config import DEFAULT_CHAT_TIMEOUT
from .._runtime.contracts import LoopGuard, RpcCaller
from ..exceptions import ChatError, NetworkError, ValidationError
from .notes import save_chat_answer_as_note
from .transport import chat_aware_authed_post
from .wire import (
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

if TYPE_CHECKING:
    from .._reqid_counter import ReqidCounter
    from .._runtime.transport import RuntimeTransport
from ..rpc import (
    ChatGoal,
    ChatResponseLength,
    RPCMethod,
    safe_index,
)
from ..types import (
    AskResult,
    ChatMode,
    ChatReference,
    ConversationTurn,
    Note,
)

logger = logging.getLogger(__name__)


def _extract_next_turn_content(next_turn: Any) -> str | None:
    """Extract the response content from a streaming-chat next_turn frame.

    The ``khqZz`` (``GET_CONVERSATION_TURNS``) response packs each AI answer
    as ``turn[4][0][0]`` — three nested wrappers around the answer text. The
    descent goes through :func:`safe_index` under strict decoding (the only
    mode since the ``NOTEBOOKLM_STRICT_DECODE=0`` opt-out was retired in
    v0.7.0; rationale in ADR-0011): a genuine descent failure raises
    :class:`~notebooklm.exceptions.UnknownRPCMethodError` so callers fail
    fast on Google-side shape drift.

    ``next_turn`` is a validated answer row (a list with ``len > 4`` and the
    answer role code — see ``ConversationTurnRow.is_answer``). Returns the
    answer-text string, or ``None`` when the leaf descends successfully to a
    non-string value (the caller's empty-answer fallback).
    """
    content = safe_index(
        next_turn,
        4,
        0,
        0,
        method_id=RPCMethod.GET_CONVERSATION_TURNS.value,
        source="_chat._extract_next_turn_content",
    )
    if not isinstance(content, str):
        # A non-string leaf at a structurally-valid path is normalised to
        # ``None`` so the caller's empty-answer fallback fires uniformly. This
        # is distinct from shape drift, which safe_index raises on.
        logger.debug(
            "next_turn content is not a string (type=%s); treating as drift",
            type(content).__name__,
        )
        return None
    return content


class ChatAPI(LoopBoundPrimitive):
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
        rpc: RpcCaller,
        transport: RuntimeTransport,
        reqid: ReqidCounter,
        loop_guard: LoopGuard,
        chat_timeout: float | None = DEFAULT_CHAT_TIMEOUT,
        conversation_cache: ConversationCache | None = None,
        notebooks: NotebookSourceIdProvider | None = None,
    ):
        """Initialize the chat API.

        Per ADR-0014 Rule 2 Corollary, ``ChatAPI`` depends on the **direct**
        collaborators it exercises (``rpc``, ``transport``, ``reqid``,
        ``loop_guard``) rather than a chat-local Runtime Protocol bundling them.

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
            chat_timeout: Per-read HTTP timeout (seconds) for the streamed chat
                endpoint. ``None`` inherits the underlying transport timeout.
            conversation_cache: Optional injected cache; defaults to a fresh
                per-instance ``ConversationCache``.
            notebooks: Optional source-id resolver; defaults to a
                ``NotebooksAPI`` around ``rpc`` so a bare ``ChatAPI(...)`` still
                resolves source ids without callers wiring the full graph.
        """
        self._rpc = rpc
        self._transport = transport
        self._reqid = reqid
        self._loop_guard = loop_guard
        self._chat_timeout = chat_timeout
        if notebooks is None:
            from .._notebooks import NotebooksAPI

            notebooks = NotebooksAPI(rpc)
        self._notebooks = notebooks
        self._cache = conversation_cache if conversation_cache is not None else ConversationCache()
        # Per-``conversation_id`` lock serializing follow-up asks on the same
        # conversation. Without it, two ``asyncio.gather``'d asks read identical
        # pre-update history, both POST it, then race to append to ``self._cache``
        # — the server sees two turn N+1 follow-ups and the cache loses lineage.
        #
        # ``WeakValueDictionary`` keeps the map bounded: a caller holds a strong
        # ref while inside ``async with lock:``; once all waiters release, the
        # entry GCs itself. Per-key churn for one-shot conversations is negligible.
        self._conversation_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )
        # Per-``notebook_id`` lock for asks that enter without a
        # ``conversation_id``. The server treats ``params[4] = null`` as
        # "append to the current conversation for this notebook, creating it
        # if needed"; until ``hPTbtc`` returns the real id, the only stable
        # key we can serialize on locally is the notebook id.
        self._new_conversation_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )
        # Event-loop binding for the two lazy lock maps. ``set_bound_loop`` comes
        # from :class:`~notebooklm._loop_bound.LoopBoundPrimitive`; this API
        # overrides :meth:`_on_loop_rebind` to clear the maps on a loop change so
        # a lock bound to a closed loop is never reused after a reopen — see
        # :meth:`_on_loop_rebind` / :meth:`reset_after_open`.

    def _on_loop_rebind(
        self,
        old: asyncio.AbstractEventLoop | None,
        new: asyncio.AbstractEventLoop | None,
    ) -> None:
        """Clear the lazy conversation lock maps when the bound loop changes.

        Fires from ``LoopBoundPrimitive.set_bound_loop`` only on a real loop
        change (before ``_bound_loop`` updates), so a stale ``asyncio.Lock``
        bound to the old loop is never reused after a rebind even when called
        independently of :meth:`reset_after_open`. The cross-loop guard for
        :meth:`ask` is the injected ``loop_guard.assert_bound_loop``; this hook
        only governs when the lazy locks are rebuilt.
        """
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
        """Return the (lazily created) lock for ``conversation_id``.

        Single-threaded asyncio makes the ``WeakValueDictionary`` get/set atomic
        (no ``await`` between lookup and insert), so concurrent callers on the
        same conversation share one lock instance. The bare lock is returned (not
        a context-manager wrapper) so the caller's strong ref keeps the entry
        alive for the critical section.
        """
        lock = self._conversation_locks.get(conversation_id)
        if lock is None:
            lock = asyncio.Lock()
            self._conversation_locks[conversation_id] = lock
        return lock

    def _get_new_conversation_lock(self, notebook_id: str) -> asyncio.Lock:
        """Return the lock for null-conversation asks in ``notebook_id``.

        Uses the same weak-cache pattern as per-conversation locks: the
        caller's local variable keeps the lock alive while it is held, and
        the registry entry is reclaimed when there are no active holders or
        waiters.
        """
        lock = self._new_conversation_locks.get(notebook_id)
        if lock is None:
            lock = asyncio.Lock()
            self._new_conversation_locks[notebook_id] = lock
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
        # Catch cross-loop ``ask`` before any work — particularly
        # before acquiring the per-conversation lock below, which would
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
        if source_ids is None:
            source_ids = await self._notebooks.get_source_ids(notebook_id)

        is_new_conversation = conversation_id is None

        async def perform_request(
            *,
            conversation_history: list[Any] | None,
            active_conversation_id: str | None,
        ) -> tuple[str, list[ChatReference], str, str]:
            # Capture into closure-local variables so the nested ``build_request``
            # closure carries explicit types — mypy doesn't propagate flow
            # narrowing through nested-function captures, and the wire
            # builder accepts ``conversation_id: str | None``.
            active_source_ids: list[str] = source_ids

            # Mint the request-id under the asyncio-safe counter helper so two
            # concurrent ``ask`` calls on the same client never collide.
            # A direct counter mutation would race under ``asyncio.gather``
            # and produce duplicate ``_reqid`` URL params. ``ChatAPI`` holds
            # the :class:`ReqidCounter` directly (constructor injection).
            reqid = await self._reqid.next_reqid()

            def build_request(snapshot: AuthSnapshot) -> tuple[str, str, dict[str, str]]:
                return self._build_chat_request(
                    snapshot=snapshot,
                    notebook_id=notebook_id,
                    question=question,
                    source_ids=active_source_ids,
                    conversation_history=conversation_history,
                    conversation_id=active_conversation_id,
                    reqid=reqid,
                )

            # ``chat_aware_authed_post`` owns the chat-flavored exception
            # mapping (transport→ChatError/NetworkError) and drain
            # bookkeeping that ``ask`` used to duplicate inline. The
            # request-id context lives here so retries inside the helper
            # share the same ``[req=<id>]`` log prefix as the initial
            # attempt.
            reqid_token = None if get_request_id() is not None else set_request_id()
            try:
                response = await chat_aware_authed_post(
                    self._transport,
                    build_request=build_request,
                    parse_label="chat.ask",
                    read_timeout=self._chat_timeout,
                    disable_read_timeout_retries=True,
                )
            finally:
                if reqid_token is not None:
                    reset_request_id(reqid_token)

            # ``_parse_ask_response_with_references`` returns a third tuple
            # element historically called ``server_conv_id``. Live API tests
            # (issue #659) proved that field is a per-stream/per-query id,
            # not a real conversation_id: querying ``khqZz`` with it returns
            # 0 turns, and passing it back as ``params[4]`` for a follow-up
            # produces a ghost turn the server does not register. We discard
            # it here and fetch the real id via ``hPTbtc`` below.
            answer_text, references, _ignored_stream_id = self._parse_ask_response_with_references(
                response.text
            )

            resolved_conversation_id = active_conversation_id
            if is_new_conversation:
                # The real conversation_id is not present anywhere in the
                # streamed chat response. The only way to recover it is to
                # query ``hPTbtc`` (GET_LAST_CONVERSATION_ID), which returns
                # the user's current conversation for this notebook — i.e.
                # the one our null-at-params[4] ask just attached to.
                #
                # Wrap the call in try/except so that if hPTbtc itself fails
                # (network, auth, etc.), we log the answer text before
                # surfacing the exception — otherwise the caller loses an
                # answer they already paid for.
                try:
                    real_conversation_id = await self.get_conversation_id(notebook_id)
                except (ChatError, NetworkError):
                    logger.error(
                        "Chat ask succeeded but post-ask get_conversation_id "
                        "failed. Answer (%d chars, may be truncated): %r",
                        len(answer_text or ""),
                        (answer_text or "")[:500],
                    )
                    raise
                if real_conversation_id is None:
                    if answer_text:
                        # Server returned an answer but hPTbtc has no id.
                        # The conversation may have been recorded but is
                        # invisible to hPTbtc, OR the API shape drifted.
                        # Log the answer so it survives the raise.
                        logger.error(
                            "Server returned a non-empty answer but hPTbtc "
                            "returned no conversation_id (%d chars). Answer "
                            "preview: %r",
                            len(answer_text),
                            answer_text[:500],
                        )
                    raise ChatError(
                        "Server did not register a conversation for this ask "
                        "(hPTbtc returned no id). The response may have been "
                        "empty, or the API shape may have changed. Please file "
                        "an issue at https://github.com/teng-lin/notebooklm-py/issues."
                    )
                resolved_conversation_id = real_conversation_id
            # Follow-up: keep the caller-supplied id. (We used to rebind to
            # ``server_conv_id`` here, but that field is a stream id not a
            # conv_id — see comment above.)

            assert resolved_conversation_id is not None

            return answer_text, references, resolved_conversation_id, response.text

        def cache_turn(resolved_conversation_id: str, answer_text: str) -> int:
            turns = self._cache.get_cached_conversation(resolved_conversation_id)
            if answer_text:
                turn_number = len(turns) + 1
                self._cache.cache_conversation_turn(
                    resolved_conversation_id, question, answer_text, turn_number
                )
            else:
                turn_number = len(turns)
            return turn_number

        # Follow-ups use the per-conversation lock from history build through
        # cache update. Null-conversation asks have no id to lock on yet, but
        # the server still appends them to the notebook's current conversation.
        # Serialize those by notebook until hPTbtc returns the real id; then
        # release the notebook path and use the existing conversation-id lock
        # for the local cache update.
        #
        # A null ask cannot serialize its streamed POST against an explicit
        # follow-up that already knows the same eventual conversation id; the
        # null path does not know that key until hPTbtc returns. The handoff
        # below still serializes the local cache update with that follow-up.
        if is_new_conversation:
            async with self._get_new_conversation_lock(notebook_id):
                (
                    answer_text,
                    references,
                    resolved_conversation_id,
                    raw_response,
                ) = await perform_request(
                    conversation_history=None,
                    active_conversation_id=None,
                )
            async with self._get_conversation_lock(resolved_conversation_id):
                turn_number = cache_turn(resolved_conversation_id, answer_text)
        else:
            assert conversation_id is not None  # narrowed by is_new_conversation
            async with self._get_conversation_lock(conversation_id):
                conversation_history = self._build_conversation_history(conversation_id)
                (
                    answer_text,
                    references,
                    resolved_conversation_id,
                    raw_response,
                ) = await perform_request(
                    conversation_history=conversation_history,
                    active_conversation_id=conversation_id,
                )
                turn_number = cache_turn(resolved_conversation_id, answer_text)

        return AskResult(
            answer=answer_text,
            conversation_id=resolved_conversation_id,
            turn_number=turn_number,
            is_follow_up=not is_new_conversation,
            references=references,
            raw_response=raw_response[:1000],
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
        # Response [[[conv_id]]]: SOFT walk in
        # ``_row_adapters.chat.unwrap_last_conversation_id`` (None if no row).
        if raw and isinstance(raw, list):
            conversation_id = unwrap_last_conversation_id(raw)
            if conversation_id is not None:
                return conversation_id
            # WARNING (not DEBUG): the shape is the actionable diagnostic when
            # ``ChatAPI.ask`` raises ChatError on a ``None`` return (issue #659).
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
        except (ChatError, NetworkError) as e:
            logger.warning("Failed to fetch conversation turns for %s: %s", notebook_id, e)
            return []
        # API returns turns newest-first: [A2, Q2, ...]; reverse to [Q1, A1, ...]
        # for the Q→A pairer. Unwrap keeps an empty history soft, raises on drift.
        turns = unwrap_conversation_turns(turns_data, source="_chat.get_history")
        if turns:
            turns_data = [list(reversed(turns))]
        return self._parse_turns_to_qa_pairs(turns_data)

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
        turns = unwrap_conversation_turns(turns_data, source="_chat._parse_turns_to_qa_pairs")

        pairs: list[tuple[str, str]] = []
        i = 0
        while i < len(turns):
            turn = ConversationTurnRow(turns[i])
            if not turn.is_well_formed:
                logger.debug(
                    "_parse_turns_to_qa_pairs: skipping malformed turn at index %d: %s",
                    i,
                    reprlib.repr(turns[i]),
                )
                i += 1
                continue
            if turn.has_unrecognized_role:
                logger.debug(
                    "_parse_turns_to_qa_pairs: unrecognized role code %r at turn %d — skipping; "
                    "possible role-slot drift: %s",
                    turn.role,
                    i,
                    reprlib.repr(turns[i]),
                )
                i += 1
                continue
            if turn.is_question:
                q = turn.question_text
                a = ""
                # Pair with the immediately-following answer turn, if any; a
                # non-string content leaf yields "" (drift raises in the leaf).
                if i + 1 < len(turns):
                    next_turn = ConversationTurnRow(turns[i + 1])
                    if next_turn.is_answer:
                        content = _extract_next_turn_content(next_turn.raw)
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
        # Catch cross-loop misuse before acquiring the per-conversation lock
        # (like ``ask``), so a client reused from another loop fails fast rather
        # than hang on a dead-loop lock. ``set_bound_loop`` / ``reset_after_open``
        # (#1225) only reset locks on *reopen*; an open cross-loop client raises.
        self._loop_guard.assert_bound_loop()
        logger.debug("Deleting conversation %s in notebook %s", conversation_id, notebook_id)
        # Hold the per-``conversation_id`` lock like ``ask`` does for follow-ups,
        # so a concurrent follow-up can't read pre-delete history then POST it
        # after the delete cleared both server-side state and the local cache.
        async with self._get_conversation_lock(conversation_id):
            # DELETE_CONVERSATION is the live ``DeleteChatTurns``: it deletes the
            # conversation's chat turns (the "Delete history" action), not a
            # standalone conversation entity.
            # Param shape from web-UI traffic; trailing 1 is a fixed flag.
            params: list[Any] = [[], conversation_id, None, 1]
            await self._rpc.rpc_call(
                RPCMethod.DELETE_CONVERSATION,
                params,
                source_path=f"/notebook/{notebook_id}",
            )
            # Clear the cache only after a successful RPC (failure raises above).
            self._cache.clear(conversation_id)
        # v0.8.0 (#1290): the uninformative always-``True`` return becomes ``None``.
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

        await self._rpc.rpc_call(
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
        return await save_chat_answer_as_note(
            self._rpc,
            notebook_id,
            ask_result.answer,
            ask_result.references,
            resolved_title,
        )

    # =========================================================================
    # Private Helpers
    # =========================================================================

    def _build_conversation_history(self, conversation_id: str) -> list | None:
        """Build conversation history for follow-up requests."""
        turns = self._cache.get_cached_conversation(conversation_id)
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
        snapshot: AuthSnapshot,
        notebook_id: str,
        question: str,
        source_ids: list[str],
        conversation_history: list | None,
        conversation_id: str | None,
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
