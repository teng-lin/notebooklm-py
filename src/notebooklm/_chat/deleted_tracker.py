"""Bounded record of recently deleted conversation ids.

``ChatAPI.ask``'s null-conversation path (issue #1875) resolves the notebook's
current conversation, then serializes its POST on that conversation's lock. If a
``delete_conversation`` for that same id completes while the null ask is blocked
on the lock, the server starts a *fresh* conversation for the null POST — so the
ask must NOT pin its result to the now-deleted id. ``delete_conversation``
records each deleted id here (under the conversation lock) and the null ask
re-checks after acquiring that lock; a hit means "drop the override and recover
the id the server actually used post-POST".

Conversation ids are unique and never reused, so a recorded id is deleted
forever — membership never yields a false positive. Only ids deleted in the
brief lock-handoff window are ever consulted, so a small bounded FIFO keeps
memory flat without losing a still-relevant marker.
"""

from __future__ import annotations

from collections import OrderedDict

_DEFAULT_CAPACITY = 1024


class RecentlyDeletedConversations:
    """FIFO-bounded membership set of recently deleted conversation ids."""

    def __init__(self, capacity: int = _DEFAULT_CAPACITY) -> None:
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
