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
from contextlib import asynccontextmanager

import pytest

from notebooklm import _core
from notebooklm._core_cache import ConversationCache
from notebooklm.auth import AuthTokens


def _assert_method_has_no_yield_points(method, label: str) -> None:
    src = inspect.getsource(method)
    tree = ast.parse(textwrap.dedent(src))
    awaits = [n for n in ast.walk(tree) if isinstance(n, ast.Await)]
    is_async = any(isinstance(n, ast.AsyncFunctionDef) for n in ast.walk(tree))
    assert not awaits, f"{label} must not contain `await` (breaks atomicity guarantee)"
    assert not is_async, f"{label} must not be `async def` (breaks atomicity guarantee)"


@asynccontextmanager
async def make_core():
    auth = AuthTokens(
        csrf_token="CSRF_OLD",
        session_id="SID_OLD",
        cookies={"SID": "old_sid_cookie"},
    )
    core = _core.ClientCore(auth=auth, refresh_retry_delay=0.0)
    await core.open()
    try:
        yield core
    finally:
        await core.close()


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
    _assert_method_has_no_yield_points(
        _core.ClientCore.cache_conversation_turn,
        "cache_conversation_turn",
    )


def test_conversation_cache_mutation_remains_synchronous():
    """The collaborator mutation owns the no-yield atomicity contract."""
    _assert_method_has_no_yield_points(
        ConversationCache.cache_conversation_turn,
        "ConversationCache.cache_conversation_turn",
    )


@pytest.mark.asyncio
async def test_cache_eviction_preserves_invariant_size(monkeypatch):
    monkeypatch.setattr(_core, "MAX_CONVERSATION_CACHE_SIZE", 3)
    async with make_core() as core:
        for i in range(10):
            core.cache_conversation_turn(f"conv-{i}", "q", "a", 0)
        assert len(core._conversation_cache) == 3
        assert list(core._conversation_cache.keys()) == ["conv-7", "conv-8", "conv-9"]
