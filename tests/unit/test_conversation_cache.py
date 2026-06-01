"""Unit tests for the core conversation-cache collaborator."""

from __future__ import annotations

from collections import OrderedDict

from notebooklm._conversation_cache import ConversationCache


def test_cache_conversation_turn_appends_turns() -> None:
    cache = ConversationCache()

    cache.cache_conversation_turn("conv-1", "q1", "a1", 1)
    cache.cache_conversation_turn("conv-1", "q2", "a2", 2)

    assert cache.get_cached_conversation("conv-1") == [
        {"query": "q1", "answer": "a1", "turn_number": 1},
        {"query": "q2", "answer": "a2", "turn_number": 2},
    ]


def test_cache_conversation_turn_lru_evicts_least_recently_used() -> None:
    cache = ConversationCache()

    cache.cache_conversation_turn("conv-1", "q1", "a1", 1, max_size=2)
    cache.cache_conversation_turn("conv-2", "q2", "a2", 1, max_size=2)
    # Re-touching conv-1 promotes it to most-recently-used, so the next new
    # conversation evicts conv-2 (the least-recently-used) rather than conv-1.
    cache.cache_conversation_turn("conv-1", "q3", "a3", 2, max_size=2)
    cache.cache_conversation_turn("conv-3", "q4", "a4", 1, max_size=2)

    assert list(cache.conversations) == ["conv-1", "conv-3"]
    assert cache.get_cached_conversation("conv-1") == [
        {"query": "q1", "answer": "a1", "turn_number": 1},
        {"query": "q3", "answer": "a3", "turn_number": 2},
    ]
    assert cache.get_cached_conversation("conv-3") == [
        {"query": "q4", "answer": "a4", "turn_number": 1}
    ]


def test_cache_conversation_turn_max_size_one_keeps_only_latest() -> None:
    cache = ConversationCache()

    cache.cache_conversation_turn("conv-1", "q1", "a1", 1, max_size=1)
    assert list(cache.conversations) == ["conv-1"]

    # Each new conversation evicts the sole prior entry; the cache always
    # holds exactly one conversation.
    cache.cache_conversation_turn("conv-2", "q2", "a2", 1, max_size=1)
    assert list(cache.conversations) == ["conv-2"]
    assert cache.get_cached_conversation("conv-1") == []
    assert cache.get_cached_conversation("conv-2") == [
        {"query": "q2", "answer": "a2", "turn_number": 1}
    ]


def test_get_cached_conversation_promotes_to_most_recently_used() -> None:
    cache = ConversationCache()

    cache.cache_conversation_turn("conv-1", "q1", "a1", 1, max_size=2)
    cache.cache_conversation_turn("conv-2", "q2", "a2", 1, max_size=2)
    # A pure read of conv-1 must mark it most-recently-used so the next new
    # conversation evicts the untouched conv-2 instead.
    assert cache.get_cached_conversation("conv-1")
    cache.cache_conversation_turn("conv-3", "q3", "a3", 1, max_size=2)

    assert list(cache.conversations) == ["conv-1", "conv-3"]


def test_per_conversation_turn_cap_evicts_oldest_turns() -> None:
    cache = ConversationCache()

    for i in range(1, 6):
        cache.cache_conversation_turn("conv-1", f"q{i}", f"a{i}", i, max_turns=3)

    turns = cache.get_cached_conversation("conv-1")
    # Only the newest three turns survive; the two oldest were trimmed.
    assert [turn["turn_number"] for turn in turns] == [3, 4, 5]
    assert [turn["query"] for turn in turns] == ["q3", "q4", "q5"]


def test_cache_conversation_turn_handles_non_positive_bounds() -> None:
    cache = ConversationCache()

    # A non-positive max_size must not loop forever / KeyError on popitem;
    # it retains nothing instead.
    cache.cache_conversation_turn("conv-1", "q1", "a1", 1, max_size=0)
    assert cache.get_cached_conversation("conv-1") == []
    assert list(cache.conversations) == []

    # A non-positive max_turns likewise caches nothing for that turn.
    cache.cache_conversation_turn("conv-2", "q1", "a1", 1, max_turns=0)
    assert cache.get_cached_conversation("conv-2") == []
    assert list(cache.conversations) == []


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
