"""Unit tests for the core conversation-cache collaborator."""

from __future__ import annotations

from collections import OrderedDict

from notebooklm._core_cache import ConversationCache


def test_cache_conversation_turn_appends_turns() -> None:
    cache = ConversationCache()

    cache.cache_conversation_turn("conv-1", "q1", "a1", 1)
    cache.cache_conversation_turn("conv-1", "q2", "a2", 2)

    assert cache.get_cached_conversation("conv-1") == [
        {"query": "q1", "answer": "a1", "turn_number": 1},
        {"query": "q2", "answer": "a2", "turn_number": 2},
    ]


def test_cache_conversation_turn_fifo_evicts_only_for_new_conversations() -> None:
    cache = ConversationCache()

    cache.cache_conversation_turn("conv-1", "q1", "a1", 1, max_size=2)
    cache.cache_conversation_turn("conv-2", "q2", "a2", 1, max_size=2)
    cache.cache_conversation_turn("conv-1", "q3", "a3", 2, max_size=2)
    cache.cache_conversation_turn("conv-3", "q4", "a4", 1, max_size=2)

    assert list(cache.conversations) == ["conv-2", "conv-3"]
    assert cache.get_cached_conversation("conv-2") == [
        {"query": "q2", "answer": "a2", "turn_number": 1}
    ]
    assert cache.get_cached_conversation("conv-3") == [
        {"query": "q4", "answer": "a4", "turn_number": 1}
    ]


def test_get_cached_conversation_returns_empty_list_for_missing_conversation() -> None:
    cache = ConversationCache()

    assert cache.get_cached_conversation("missing") == []


def test_clear_specific_and_all_conversations() -> None:
    cache = ConversationCache()
    cache.cache_conversation_turn("conv-1", "q1", "a1", 1)
    cache.cache_conversation_turn("conv-2", "q2", "a2", 1)

    assert cache.clear("missing") is False
    assert cache.clear("conv-1") is True
    assert list(cache.conversations) == ["conv-2"]

    assert cache.clear() is True
    assert cache.conversations == {}


def test_constructor_preserves_seeded_order() -> None:
    cache = ConversationCache(
        OrderedDict(
            [
                ("conv-1", [{"query": "q1", "answer": "a1", "turn_number": 1}]),
                ("conv-2", [{"query": "q2", "answer": "a2", "turn_number": 1}]),
            ]
        )
    )

    assert list(cache.conversations) == ["conv-1", "conv-2"]


def test_constructor_keeps_ordered_dict_backing_mapping() -> None:
    conversations: OrderedDict[str, list[dict[str, object]]] = OrderedDict()
    cache = ConversationCache(conversations)

    cache.cache_conversation_turn("conv-1", "q1", "a1", 1)

    assert cache.conversations is conversations
    assert conversations["conv-1"] == [{"query": "q1", "answer": "a1", "turn_number": 1}]
