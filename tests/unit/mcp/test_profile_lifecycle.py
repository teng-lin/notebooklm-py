"""Exception and loop ownership contracts for multi-profile MCP clients."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import TracebackType
from unittest.mock import MagicMock

import pytest

pytest.importorskip("fastmcp")

from notebooklm.client import NotebookLMClient  # noqa: E402
from notebooklm.mcp._profiles import ProfileClientProvider, profile_lifespan  # noqa: E402


async def test_lifespan_forwards_original_failure_to_every_client() -> None:
    received: list[
        tuple[str, type[BaseException] | None, BaseException | None, TracebackType | None]
    ] = []
    failure = ValueError("original lifespan failure")

    class Factory:
        def __init__(self, name: str) -> None:
            self.name = name

        async def __aenter__(self) -> NotebookLMClient:
            return MagicMock(spec=NotebookLMClient)

        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            tb: TracebackType | None,
        ) -> bool:
            received.append((self.name, exc_type, exc, tb))
            if exc is None:
                # Like NotebookLMClient, cleanup only raises when there is no
                # existing failure whose traceback needs to be preserved.
                raise OSError("close failed")
            return False

    with pytest.raises(ValueError) as caught:
        async with profile_lifespan(
            {"work": Path("work"), "personal": Path("personal")}, Factory, 1, None
        ) as registry:
            await asyncio.gather(
                *(state.client_provider.get() for state in registry.profiles.values())
            )
            try:
                raise failure
            except ValueError:
                body_tb = failure.__traceback__
                raise

    assert caught.value is failure
    assert [entry[0] for entry in received] == ["personal", "work"]
    assert all(entry[1:3] == (ValueError, failure) for entry in received)
    assert received[0][3] is received[1][3]
    traceback = received[0][3]
    while traceback is not None and traceback is not body_tb:
        traceback = traceback.tb_next
    assert traceback is body_tb


async def test_lifespan_honors_client_exception_suppression() -> None:
    received: list[tuple[str, BaseException | None]] = []
    failure = ValueError("suppress this lifespan failure")

    @asynccontextmanager
    async def factory(name: str) -> AsyncIterator[NotebookLMClient]:
        try:
            yield MagicMock(spec=NotebookLMClient)
        except ValueError as exc:
            received.append((name, exc))
            # Deliberately suppress the lifespan exception.
        else:
            received.append((name, None))

    async with profile_lifespan(
        {"work": Path("work"), "personal": Path("personal")}, factory, 1, None
    ) as registry:
        await asyncio.gather(*(state.client_provider.get() for state in registry.profiles.values()))
        raise failure

    assert received == [("personal", failure), ("work", None)]


async def test_normal_shutdown_propagates_cleanup_failure_and_closes_siblings() -> None:
    closed: list[str] = []

    @asynccontextmanager
    async def factory(name: str) -> AsyncIterator[NotebookLMClient]:
        try:
            yield MagicMock(spec=NotebookLMClient)
        finally:
            closed.append(name)
            if name == "personal":
                raise OSError("close failed")

    with pytest.raises(OSError, match="close failed"):
        async with profile_lifespan(
            {"work": Path("work"), "personal": Path("personal")}, factory, 1, None
        ) as registry:
            await asyncio.gather(
                *(state.client_provider.get() for state in registry.profiles.values())
            )

    assert closed == ["personal", "work"]


@pytest.mark.parametrize("method", ["get", "start", "aclose"])
async def test_cached_provider_rejects_cross_loop_access_without_mutation(method: str) -> None:
    closed = False
    client = MagicMock(spec=NotebookLMClient)

    @asynccontextmanager
    async def factory() -> AsyncIterator[NotebookLMClient]:
        nonlocal closed
        try:
            yield client
        finally:
            closed = True

    provider = ProfileClientProvider(factory, 1)
    try:
        assert await provider.get() is client

        async def foreign_loop() -> None:
            if method == "start":
                provider.start()
            else:
                await getattr(provider, method)()

        with pytest.raises(RuntimeError, match="owning event loop"):
            await asyncio.to_thread(asyncio.run, foreign_loop())
        assert await provider.get() is client
        assert not closed
    finally:
        await provider.aclose()
    assert closed
