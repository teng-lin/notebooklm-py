"""Backpressure tests for expensive REST route groups."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast

import pytest

pytest.importorskip("fastapi")
httpx = pytest.importorskip("httpx")

from notebooklm._types.chat import AskResult  # noqa: E402
from notebooklm.server._limits import LimitGroup, ServerLimiters  # noqa: E402
from notebooklm.server.app import create_app  # noqa: E402

from ._concurrency import ActiveHold  # noqa: E402
from .conftest import TEST_TOKEN  # noqa: E402
from .fakes import FakeClient  # noqa: E402


def _factory_for(fake_client: FakeClient) -> Any:
    @asynccontextmanager
    async def factory() -> AsyncIterator[FakeClient]:
        yield fake_client

    return factory


@asynccontextmanager
async def _async_client(fake_client: FakeClient) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(client_factory=_factory_for(fake_client))
    headers = {"Authorization": f"Bearer {TEST_TOKEN}", "Host": "127.0.0.1"}
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 5555), raise_app_exceptions=False)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=transport, base_url="http://127.0.0.1", headers=headers
        ) as client,
    ):
        yield client


async def test_chat_backpressure_bounds_concurrency_and_leaves_healthz_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NOTEBOOKLM_SERVER_CHAT_CONCURRENCY", "1")
    fake_client = FakeClient()
    hold = ActiveHold()

    async def slow_ask(
        notebook_id: str, question: str, *, conversation_id: str | None = None
    ) -> AskResult:
        await hold.enter()
        try:
            await hold.release.wait()
            return AskResult(
                answer=f"answer to: {question}",
                conversation_id=conversation_id or "conv-1",
                turn_number=1,
                is_follow_up=conversation_id is not None,
            )
        finally:
            await hold.leave()

    monkeypatch.setattr(fake_client.chat, "ask", slow_ask)

    async with _async_client(fake_client) as client:
        first = asyncio.create_task(
            client.post("/v1/notebooks/nb-1/chat", json={"question": "one"})
        )
        await asyncio.wait_for(hold.entered.wait(), timeout=1)

        second = asyncio.create_task(
            client.post("/v1/notebooks/nb-1/chat", json={"question": "two"})
        )
        try:
            await asyncio.sleep(0.05)
            health = await asyncio.wait_for(client.get("/healthz"), timeout=1)
            assert health.status_code == 200
            assert health.json() == {"ok": True}
        finally:
            hold.release.set()

        first_response, second_response = await asyncio.gather(first, second)

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert hold.max_active == 1


async def test_source_mutation_backpressure_bounds_concurrency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NOTEBOOKLM_SERVER_SOURCE_MUTATION_CONCURRENCY", "1")
    fake_client = FakeClient()
    hold = ActiveHold()
    original_add_text = fake_client.sources.add_text

    async def slow_add_text(notebook_id: str, title: str, content: str) -> Any:
        await hold.enter()
        try:
            await hold.release.wait()
            return await original_add_text(notebook_id, title, content)
        finally:
            await hold.leave()

    monkeypatch.setattr(fake_client.sources, "add_text", slow_add_text)

    async with _async_client(fake_client) as client:
        first = asyncio.create_task(
            client.post(
                "/v1/notebooks/nb-1/sources/text",
                json={"title": "one", "text": "one"},
            )
        )
        await asyncio.wait_for(hold.entered.wait(), timeout=1)

        second = asyncio.create_task(
            client.post(
                "/v1/notebooks/nb-1/sources/text",
                json={"title": "two", "text": "two"},
            )
        )
        try:
            await asyncio.sleep(0.05)
        finally:
            hold.release.set()

        first_response, second_response = await asyncio.gather(first, second)

    assert first_response.status_code == 201
    assert second_response.status_code == 201
    assert hold.max_active == 1


async def test_file_upload_backpressure_bounds_concurrency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NOTEBOOKLM_SERVER_SOURCE_MUTATION_CONCURRENCY", "1")
    fake_client = FakeClient()
    hold = ActiveHold()
    original_add_file = fake_client.sources.add_file

    async def slow_add_file(
        notebook_id: str,
        path: str,
        mime_type: str | None = None,
        *,
        title: str | None = None,
    ) -> Any:
        await hold.enter()
        try:
            await hold.release.wait()
            return await original_add_file(notebook_id, path, mime_type, title=title)
        finally:
            await hold.leave()

    monkeypatch.setattr(fake_client.sources, "add_file", slow_add_file)

    async with _async_client(fake_client) as client:
        first = asyncio.create_task(
            client.post(
                "/v1/notebooks/nb-1/sources/file",
                files={"file": ("one.txt", b"one", "text/plain")},
            )
        )
        await asyncio.wait_for(hold.entered.wait(), timeout=1)

        second = asyncio.create_task(
            client.post(
                "/v1/notebooks/nb-1/sources/file",
                files={"file": ("two.txt", b"two", "text/plain")},
            )
        )
        try:
            await asyncio.sleep(0.05)
        finally:
            hold.release.set()

        first_response, second_response = await asyncio.gather(first, second)

    assert first_response.status_code == 201
    assert second_response.status_code == 201
    assert hold.max_active == 1


async def test_invalid_backpressure_env_fails_lifespan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NOTEBOOKLM_SERVER_CHAT_CONCURRENCY", "0")
    app = create_app(client_factory=_factory_for(FakeClient()))

    with pytest.raises(
        ValueError, match="NOTEBOOKLM_SERVER_CHAT_CONCURRENCY must be an integer >= 1"
    ):
        async with app.router.lifespan_context(app):
            pass


async def test_unknown_limit_group_fails_fast() -> None:
    limiters = ServerLimiters.from_env({})
    limiters.set_bound_loop(asyncio.get_running_loop())

    with pytest.raises(ValueError, match="Unknown limit group: typo"):
        limiters._semaphore_for(cast(LimitGroup, "typo"))

    assert limiters._chat is None
