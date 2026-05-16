"""Conversation cache collaborator for :mod:`notebooklm._core`."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping
from typing import Any

# Maximum number of conversations to cache (FIFO eviction)
MAX_CONVERSATION_CACHE_SIZE = 100


class ConversationCache:
    """Synchronous FIFO cache for conversation turns."""

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
    ) -> None:
        """Cache a conversation turn, evicting FIFO only for new conversations."""
        is_new_conversation = conversation_id not in self.conversations

        if is_new_conversation:
            while len(self.conversations) >= max_size:
                self.conversations.popitem(last=False)
            self.conversations[conversation_id] = []

        self.conversations[conversation_id].append(
            {
                "query": query,
                "answer": answer,
                "turn_number": turn_number,
            }
        )

    def get_cached_conversation(self, conversation_id: str) -> list[dict[str, Any]]:
        """Return cached turns for ``conversation_id`` or an empty list."""
        return self.conversations.get(conversation_id, [])

    def clear(self, conversation_id: str | None = None) -> bool:
        """Clear one cached conversation or the whole cache."""
        if conversation_id:
            if conversation_id in self.conversations:
                del self.conversations[conversation_id]
                return True
            return False

        self.conversations.clear()
        return True
