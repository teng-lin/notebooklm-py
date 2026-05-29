"""Conversation cache collaborator owned by :mod:`notebooklm._chat`.

A per-instance, true-LRU cache of conversation turns. Two caps bound growth:

* ``MAX_CONVERSATION_CACHE_SIZE`` caps the number of distinct conversations.
  Every access (read or write) marks the touched conversation as most-recently
  used; when a *new* conversation would exceed the cap the *least*-recently-used
  conversation is evicted.
* ``MAX_TURNS_PER_CONVERSATION`` caps the turns retained per conversation. When
  a conversation exceeds the cap its oldest turns are dropped so the most recent
  exchanges (the ones follow-up history is built from) always survive.

All mutations are synchronous and contain no ``await`` points, so under
cooperative asyncio scheduling a mutation runs to completion before any other
coroutine resumes (see ``tests/unit/test_concurrency_cache_race.py``).
"""

from __future__ import annotations

__all__ = [
    "MAX_CONVERSATION_CACHE_SIZE",
    "MAX_TURNS_PER_CONVERSATION",
    "ConversationCache",
]

from collections import OrderedDict
from collections.abc import Mapping
from typing import Any

# Maximum number of distinct conversations to cache (LRU eviction).
MAX_CONVERSATION_CACHE_SIZE = 100

# Maximum number of turns retained per conversation. Generous enough to cover
# normal multi-turn chats while bounding unbounded growth on a single, very long
# conversation. When exceeded the oldest turns are dropped first so follow-up
# history (built from the most recent turns) stays intact.
MAX_TURNS_PER_CONVERSATION = 1000


class ConversationCache:
    """Synchronous true-LRU cache for conversation turns."""

    def __init__(
        self,
        conversations: Mapping[str, list[dict[str, Any]]] | None = None,
    ) -> None:
        self.conversations: OrderedDict[str, list[dict[str, Any]]]
        if isinstance(conversations, OrderedDict):
            self.conversations = conversations
        else:
            self.conversations = OrderedDict(conversations or {})

    def cache_conversation_turn(
        self,
        conversation_id: str,
        query: str,
        answer: str,
        turn_number: int,
        *,
        max_size: int = MAX_CONVERSATION_CACHE_SIZE,
        max_turns: int = MAX_TURNS_PER_CONVERSATION,
    ) -> None:
        """Cache a conversation turn, applying LRU + per-conversation bounds.

        Touching a conversation (new or existing) marks it most-recently-used.
        A *new* conversation evicts the least-recently-used entries until the
        cache is back under ``max_size``. Each conversation's turn list is
        trimmed to its newest ``max_turns`` entries.

        A non-positive ``max_size`` or ``max_turns`` means "retain nothing":
        the turn is dropped without mutating the cache. Guarding here also
        avoids the eviction loop draining the cache and raising ``KeyError``
        on a ``popitem`` against an empty mapping.
        """
        if max_size <= 0 or max_turns <= 0:
            return

        is_new_conversation = conversation_id not in self.conversations

        if is_new_conversation:
            while len(self.conversations) >= max_size:
                self.conversations.popitem(last=False)
            self.conversations[conversation_id] = []
        else:
            # Existing conversation accessed: promote to most-recently-used.
            self.conversations.move_to_end(conversation_id)

        turns = self.conversations[conversation_id]
        turns.append(
            {
                "query": query,
                "answer": answer,
                "turn_number": turn_number,
            }
        )
        # Trim oldest turns once the per-conversation cap is exceeded.
        if len(turns) > max_turns:
            del turns[: len(turns) - max_turns]

    def get_cached_conversation(self, conversation_id: str) -> list[dict[str, Any]]:
        """Return cached turns for ``conversation_id`` or an empty list.

        Reading a present conversation marks it most-recently-used so it
        survives eviction ahead of conversations that have not been touched.
        """
        if conversation_id in self.conversations:
            self.conversations.move_to_end(conversation_id)
            return self.conversations[conversation_id]
        return []

    def clear(self, conversation_id: str | None = None) -> bool:
        """Clear one cached conversation or the whole cache."""
        if conversation_id:
            if conversation_id in self.conversations:
                del self.conversations[conversation_id]
                return True
            return False

        self.conversations.clear()
        return True
