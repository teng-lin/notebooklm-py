"""Phase 1.5 P1.5.2 — conversation cache atomicity guarantee.

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

from conftest import make_core  # type: ignore[import-not-found]
from notebooklm import _core


@pytest.mark.asyncio
async def test_concurrent_cache_appends_to_same_conversation_preserve_all_turns():
    async with make_core() as core:
        n = 100

        async def append(i):
            core.cache_conversation_turn("conv-1", f"q{i}", f"a{i}", i)

        await asyncio.gather(*(append(i) for i in range(n)))

        cache = core.get_cached_conversation("conv-1")
        assert len(cache) == n, f"Lost appends under gather: got {len(cache)}/{n}"
        seen = {(t["query"], t["answer"], t["turn_number"]) for t in cache}
        assert seen == {(f"q{i}", f"a{i}", i) for i in range(n)}


def test_cache_conversation_turn_remains_synchronous():
    """If anyone adds ``await`` to ``cache_conversation_turn``, this fails.

    The cache's atomicity guarantee depends on the function having no yield
    points.
    """
    src = inspect.getsource(_core.ClientCore.cache_conversation_turn)
    tree = ast.parse(textwrap.dedent(src))
    awaits = [n for n in ast.walk(tree) if isinstance(n, ast.Await)]
    is_async = any(isinstance(n, ast.AsyncFunctionDef) for n in ast.walk(tree))
    assert not awaits, (
        "cache_conversation_turn must not contain `await` (breaks atomicity guarantee)"
    )
    assert not is_async, (
        "cache_conversation_turn must not be `async def` (breaks atomicity guarantee)"
    )


@pytest.mark.asyncio
async def test_cache_eviction_preserves_invariant_size(monkeypatch):
    monkeypatch.setattr(_core, "MAX_CONVERSATION_CACHE_SIZE", 3)
    async with make_core() as core:
        for i in range(10):
            core.cache_conversation_turn(f"conv-{i}", "q", "a", 0)
        assert len(core._conversation_cache) == 3
        assert list(core._conversation_cache.keys()) == ["conv-7", "conv-8", "conv-9"]
