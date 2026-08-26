"""Stateful, transport-neutral chat workflow service.

Everything about a conversation that is *not* one backend call lives here: the
two lazy lock maps that serialize asks, the local turn cache, the bounded
record of recently deleted conversations, the one-shot created-session hint,
and the follow-up / turn-number reasoning built on top of them. The six
semantic chat operations fold in beside that state, so the sequencing of
``chat.ask``'s two leaves sits next to the locks that decide *when* it runs.

The service is neutral in both directions (P10 invariant I1): it names no
projector, public model, row adapter, RPC or web module, and every public
method returns a record, a built-in scalar or ``None``.
:class:`~notebooklm._chat.api.ChatAPI` above it owns the public vocabulary —
the loop-guard assertion, the ``BackendError`` projection, and the projection
of these records to :class:`~notebooklm.types.AskResult` and friends.

It is a :class:`~notebooklm._loop_bound.LoopBoundPrimitive` because it, not the
facade, now owns the ``asyncio.Lock`` instances that bind to a running loop;
``ClientLifecycle.open`` reaches it through ``ChatAPI``'s forwarding
``set_bound_loop`` / ``reset_after_open`` (both ends satisfy the
``ChatLifecycleHooks`` protocol the open path is typed against).
"""

from __future__ import annotations

import asyncio
import logging
import weakref
from collections.abc import Sequence
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
from .._conversation_cache import ConversationCache
from .._deadline import RuntimeDeadline
from .._loop_bound import LoopBoundPrimitive
from .._notebook_metadata import CreatedChatSessionProvider, NotebookSourceIdProvider
from .._records import (
    CHAT_ASK_DEF,
    CHAT_CONFIGURE_DEF,
    CHAT_DELETE_HISTORY_DEF,
    CHAT_GET_CONVERSATION_DEF,
    CHAT_GET_HISTORY_DEF,
    CHAT_SAVE_NOTE_DEF,
    CHAT_STREAM_ANSWER_DEF,
    ChatAskInput,
    ChatAskOutcomeRecord,
    ChatAskResultRecord,
    ChatCachedTurnRecord,
    ChatConfigureInput,
    ChatConfigureResult,
    ChatDeleteHistoryInput,
    ChatGetConversationInput,
    ChatGetHistoryInput,
    ChatGetHistoryResult,
    ChatHistoryPairRecord,
    ChatSaveNoteInput,
    ChatSaveNoteResult,
    ChatStreamAnswerInput,
    ChatStreamAnswerRecord,
)
from .deleted_tracker import RecentlyDeletedConversations
from .history import count_prior_recorded_turns

# The conversation-id diagnostics predate the service and stay pinned under the
# chat facade's logger name.
logger = logging.getLogger("notebooklm._chat.api")

_NO_CONVERSATION_REGISTERED = (
    "Server did not register a conversation for this ask (hPTbtc returned no "
    "id). The response may have been empty, or the API shape may have changed. "
    "Please file an issue at https://github.com/teng-lin/notebooklm-py/issues."
)


class ChatWorkflowService(LoopBoundPrimitive):
    """Own the conversation state and sequence the typed chat operations."""

    def __init__(
        self,
        backend: BackendAdapter,
        *,
        notebooks: NotebookSourceIdProvider,
        created_chat_sessions: CreatedChatSessionProvider | None = None,
        conversation_cache: ConversationCache | None = None,
    ) -> None:
        """Bind the workflow to one backend and its client-local conversation state.

        Args:
            backend: Typed semantic backend implementing the chat operations.
            notebooks: Source-id resolver used only when ``ask_turn`` omits
                source ids.
            created_chat_sessions: Optional one-shot provider for the initial
                session id returned by ``CREATE_NOTEBOOK``. The assembled
                client passes its shared ``NotebooksAPI`` instance.
            conversation_cache: Optional injected cache; defaults to a fresh
                per-instance ``ConversationCache``.
        """
        self._backend = backend
        self._notebooks = notebooks
        self._created_chat_sessions = created_chat_sessions
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
        # Recently deleted conversation ids (see ``deleted_tracker``); a null ask
        # that resolved a since-deleted id re-checks here after taking its lock.
        self._deleted_conversations = RecentlyDeletedConversations()
        # Event-loop binding for the two lazy lock maps. ``set_bound_loop`` comes
        # from :class:`~notebooklm._loop_bound.LoopBoundPrimitive`; this service
        # overrides :meth:`_on_loop_rebind` to clear the maps on a loop change so
        # a lock bound to a closed loop is never reused after a reopen — see
        # :meth:`_on_loop_rebind` / :meth:`reset_after_open`.

    # -- loop affinity --------------------------------------------------------

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
        ``ask`` is the facade's injected ``loop_guard.assert_bound_loop``; this
        hook only governs when the lazy locks are rebuilt.
        """
        self._conversation_locks.clear()
        self._new_conversation_locks.clear()

    def reset_after_open(self) -> None:
        """Discard the lazy conversation locks so a reopened client rebinds them.

        Called from :meth:`ClientLifecycle.open` (through ``ChatAPI``) so a
        client closed and reopened on a *different* event loop builds fresh
        ``asyncio.Lock`` instances on the new loop instead of reusing stale
        ones bound to the dead loop (which on 3.10/3.11 can raise "bound to a
        different event loop" or mispark waiters). Clearing the two
        ``WeakValueDictionary`` maps suffices — each per-key lock is rebuilt
        lazily on the next ``_get_*_lock`` call. Mirrors
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

    # -- the ask workflow -----------------------------------------------------

    async def ask_turn(
        self,
        notebook_id: str,
        question: str,
        source_ids: Sequence[str] | None = None,
        conversation_id: str | None = None,
    ) -> ChatAskOutcomeRecord:
        """Place one ask in the right conversation and record the turn locally.

        Resolves the conversation the ask belongs to, serializes the POST on
        that conversation's lock, derives the authoritative turn ordinal from
        the server's prior-turn count, and caches the exchange. The returned
        record carries the completed ask plus the two conversation facts the
        backend never reports: the turn number and whether the ask continued an
        existing conversation.
        """
        if source_ids is None:
            source_ids = await self._notebooks.get_source_ids(notebook_id)
        posted_source_ids = tuple(source_ids)

        is_new_conversation = conversation_id is None
        # ``is_follow_up`` records whether this ask CONTINUED an existing
        # conversation. An explicit ``conversation_id`` is always a follow-up.
        # A null ask is a follow-up only when the notebook already had a current
        # conversation that we resumed — refined on the null-ask path below,
        # where a first-ever (or just-deleted) conversation starts fresh (#1965).
        is_follow_up = not is_new_conversation
        prior_turn_count = 0

        async def perform_request(
            *,
            conversation_history: tuple[ChatHistoryPairRecord, ...],
            post_conversation_id: str | None,
            resolved_id_override: str | None = None,
        ) -> ChatAskResultRecord:
            return await self.ask(
                ChatAskInput(
                    notebook_id=notebook_id,
                    question=question,
                    source_ids=posted_source_ids,
                    conversation_history=conversation_history,
                    post_conversation_id=post_conversation_id,
                    resolved_conversation_id=resolved_id_override,
                )
            )

        def cache_turn(
            resolved_conversation_id: str,
            answer_text: str,
            server_prior_turn_count: int,
        ) -> int:
            if answer_text:
                turn_number = server_prior_turn_count + 1
                self._cache.cache_conversation_turn(
                    resolved_conversation_id, question, answer_text, turn_number
                )
            else:
                turn_number = server_prior_turn_count
            return turn_number

        # Null-conversation asks carry no caller id; the server appends them to
        # the notebook's *current* conversation (params[4]=null). Resolve that id
        # under the notebook lock, then serialize the POST on it like an explicit
        # follow-up (#1875). Residual assumption: a follow-up to a *different*
        # conversation won't move the server's current pointer between the
        # resolve and this POST.
        if is_new_conversation:
            async with self._get_new_conversation_lock(notebook_id):
                # CREATE_NOTEBOOK already returned this notebook's initial
                # ChatSession. Consume that one-shot hint before falling back
                # to hPTbtc, avoiding an immediate re-fetch of data the server
                # just volunteered (#2133). Keep its provenance: unlike an id
                # resolved from hPTbtc, the create hint must be bound to the
                # POST below. Otherwise another client changing the notebook's
                # current session between create() and ask() would make the
                # null-session POST land elsewhere while we still reported the
                # stale create id.
                created_session_id = (
                    self._created_chat_sessions._take_created_chat_session_id(notebook_id)
                    if self._created_chat_sessions is not None
                    else None
                )
                current_id = created_session_id
                if current_id is None:
                    current_id = await self.get_conversation_id(notebook_id)
                if current_id is None:
                    # First-ever conversation: no id to lock on, so serialize the
                    # create under the notebook lock; recover the id post-POST.
                    posted = await perform_request(
                        conversation_history=(), post_conversation_id=None
                    )
            # Existing conversation: release the notebook lock and serialize on the
            # conversation lock alone, so other null asks on this notebook resolve
            # in parallel yet still serialize here on that shared lock.
            if current_id is not None:
                async with self._get_conversation_lock(current_id):
                    # A delete_conversation for current_id may have finished while
                    # we blocked on this lock; the server then starts a fresh
                    # conversation for the null POST, so drop the override and
                    # recover the real id post-POST, not the deleted one (#1875).
                    override = None if current_id in self._deleted_conversations else current_id
                    # A current id can refer to an auto-created, zero-turn
                    # conversation. Query the server even when local turns are
                    # cached because another client may have deleted them (#1973).
                    is_follow_up = False
                    if override is not None:
                        prior_turn_count = await count_prior_recorded_turns(
                            self.get_history, notebook_id, override
                        )
                        is_follow_up = prior_turn_count > 0
                    posted = await perform_request(
                        conversation_history=(),
                        # A CREATE hint is a genuine server-issued session id,
                        # not the client-minted UUID forbidden by #659. Bind
                        # the first ask to it so the returned id and the POST
                        # target cannot diverge if the server's current pointer
                        # changed in another client meanwhile.
                        post_conversation_id=(override if created_session_id is not None else None),
                        resolved_id_override=override,
                    )
                    turn_number = cache_turn(
                        posted.conversation_id, posted.answer, prior_turn_count
                    )
            else:
                async with self._get_conversation_lock(posted.conversation_id):
                    turn_number = cache_turn(posted.conversation_id, posted.answer, 0)
        else:
            assert conversation_id is not None  # narrowed by is_new_conversation
            async with self._get_conversation_lock(conversation_id):
                prior_turn_count = await count_prior_recorded_turns(
                    self.get_history, notebook_id, conversation_id
                )
                conversation_history = self._conversation_history_records(conversation_id)
                posted = await perform_request(
                    conversation_history=conversation_history,
                    post_conversation_id=conversation_id,
                    resolved_id_override=conversation_id,
                )
                turn_number = cache_turn(posted.conversation_id, posted.answer, prior_turn_count)

        return ChatAskOutcomeRecord(
            result=posted,
            turn_number=turn_number,
            is_follow_up=is_follow_up,
        )

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
            logger.error(
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
                logger.error(
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

    # -- the remaining chat operations ----------------------------------------

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

    async def delete_conversation(
        self,
        notebook_id: str,
        conversation_id: str,
        *,
        deadline: RuntimeDeadline | None = None,
    ) -> None:
        """Delete one conversation server-side and drop every local trace of it."""
        # Hold the per-``conversation_id`` lock like ``ask_turn`` does for
        # follow-ups, so a concurrent follow-up can't read pre-delete history
        # then POST it after the delete cleared both server-side state and the
        # local cache.
        async with self._get_conversation_lock(conversation_id):
            await self._backend.invoke(
                CHAT_DELETE_HISTORY_DEF,
                ChatDeleteHistoryInput(notebook_id, conversation_id),
                deadline=deadline,
            )
            # Clear the cache only after a successful RPC (failure raises above).
            self._cache.clear(conversation_id)
            # Record under the conversation lock so a null ask blocked on this
            # same lock learns, on wake, not to pin its POST to the deleted id
            # (see ``ask_turn``'s null-conversation path, #1875).
            self._deleted_conversations.record(conversation_id)

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

    # -- cache inspection -----------------------------------------------------

    def cached_turns(self, conversation_id: str) -> tuple[ChatCachedTurnRecord, ...]:
        """Return the locally cached exchanges for ``conversation_id``."""
        return tuple(
            ChatCachedTurnRecord(
                query=turn["query"],
                answer=turn["answer"],
                turn_number=turn["turn_number"],
            )
            for turn in self._cache.get_cached_conversation(conversation_id)
        )

    def clear_cache(self, conversation_id: str | None = None) -> bool:
        """Clear one cached conversation, or the whole cache when ``None``."""
        return self._cache.clear(conversation_id)

    def cache_size(self) -> int:
        """Return the number of conversations currently held in the cache."""
        return len(self._cache.conversations)

    def _conversation_history_records(
        self,
        conversation_id: str,
    ) -> tuple[ChatHistoryPairRecord, ...]:
        """Return cached follow-up context without constructing web wire rows."""
        return tuple(
            ChatHistoryPairRecord(question=turn["query"], answer=turn["answer"])
            for turn in self._cache.get_cached_conversation(conversation_id)
        )


__all__ = ["ChatWorkflowService"]
