"""Unit tests for the transport-neutral Play Books executors (#2292)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from notebooklm._app.source_play_books import (
    SourceAddPlayBookPlan,
    execute_source_add_play_book,
    fetch_play_books,
)
from notebooklm.types import PlayBook, Source


def _client() -> MagicMock:
    client = MagicMock()
    client.sources = MagicMock()
    client.sources.list_play_books = AsyncMock(return_value=[])
    client.sources.add_play_book = AsyncMock()
    return client


@pytest.mark.asyncio
async def test_fetch_play_books_delegates() -> None:
    client = _client()
    books = [
        PlayBook("CID", "Title", ("A",), None, None, False, None, 4.5, None),
    ]
    client.sources.list_play_books = AsyncMock(return_value=books)
    assert await fetch_play_books(client) is books
    client.sources.list_play_books.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_execute_add_play_book_forwards_and_echoes() -> None:
    client = _client()
    added = Source(id="src_book", title="The Odyssey")
    client.sources.add_play_book = AsyncMock(return_value=added)
    plan = SourceAddPlayBookPlan(
        notebook_id="nb_1", content_id="6hwZEAAAQBAJ", wait=True, wait_timeout=90.0
    )
    result = await execute_source_add_play_book(client, plan)
    assert result.source is added
    assert result.notebook_id == "nb_1"
    assert result.content_id == "6hwZEAAAQBAJ"
    client.sources.add_play_book.assert_awaited_once_with(
        "nb_1", "6hwZEAAAQBAJ", wait=True, wait_timeout=90.0
    )


@pytest.mark.asyncio
async def test_execute_add_play_book_propagates_client_error() -> None:
    client = _client()
    client.sources.add_play_book = AsyncMock(side_effect=RuntimeError("boom"))
    plan = SourceAddPlayBookPlan(notebook_id="nb_1", content_id="CID")
    with pytest.raises(RuntimeError, match="boom"):
        await execute_source_add_play_book(client, plan)
