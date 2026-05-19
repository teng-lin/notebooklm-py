"""Conversation cache atomicity guarantee.

``cache_conversation_turn`` is synchronous: under cooperative asyncio
scheduling it runs to completion before any other coroutine resumes. This
file pins that guarantee with a concurrent-appends test, an AST guard
against future ``await`` additions, and an eviction-correctness test.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import textwrap

import pytest

from notebooklm import _conversation_cache
from notebooklm._conversation_cache import ConversationCache


def _assert_method_has_no_yield_points(method, label: str) -> None:
    src = inspect.getsource(method)
    tree = ast.parse(textwrap.dedent(src))
    awaits = [n for n in ast.walk(tree) if isinstance(n, ast.Await)]
    is_async = any(isinstance(n, ast.AsyncFunctionDef) for n in ast.walk(tree))
    assert not awaits, f"{label} must not contain `await` (breaks atomicity guarantee)"
    assert not is_async, f"{label} must not be `async def` (breaks atomicity guarantee)"


@pytest.mark.asyncio
async def test_concurrent_cache_appends_to_same_conversation_preserve_all_turns():
    cache = ConversationCache()
    n = 100

    async def append(i):
        cache.cache_conversation_turn("conv-1", f"q{i}", f"a{i}", i)

    await asyncio.gather(*(append(i) for i in range(n)))

    turns = cache.get_cached_conversation("conv-1")
    assert len(turns) == n, f"Lost appends under gather: got {len(turns)}/{n}"
    seen = {(t["query"], t["answer"], t["turn_number"]) for t in turns}
    assert seen == {(f"q{i}", f"a{i}", i) for i in range(n)}


def test_conversation_cache_mutation_remains_synchronous():
    """The collaborator mutation owns the no-yield atomicity contract."""
    _assert_method_has_no_yield_points(
        ConversationCache.cache_conversation_turn,
        "ConversationCache.cache_conversation_turn",
    )


def test_cache_eviction_preserves_invariant_size(monkeypatch):
    monkeypatch.setattr(_conversation_cache, "MAX_CONVERSATION_CACHE_SIZE", 3)
    cache = ConversationCache()
    for i in range(10):
        cache.cache_conversation_turn(
            f"conv-{i}",
            "q",
            "a",
            0,
            max_size=_conversation_cache.MAX_CONVERSATION_CACHE_SIZE,
        )
    assert len(cache.conversations) == 3
    assert list(cache.conversations.keys()) == ["conv-7", "conv-8", "conv-9"]
