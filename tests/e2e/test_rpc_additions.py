"""Live public-client coverage for RPCs added after v0.8.1.

Every mutation is confined to disposable notebooks. The tests intentionally
call the public APIs rather than wire services so they qualify the same facade
and backend assembly users exercise.
"""

from __future__ import annotations

import asyncio
import contextlib
from uuid import uuid4

import pytest

from notebooklm import (
    ArtifactCustomizationChoices,
    ChatSessionStatus,
    CopiedArtifact,
    CopiedSource,
    NextStepSuggestion,
    RelevantChunk,
    ResearchStatus,
    SourceType,
)

from .conftest import requires_auth

pytestmark = [pytest.mark.e2e, requires_auth]


@pytest.mark.asyncio
async def test_ranked_search_suggestions_and_customization_choices(client, temp_notebook) -> None:
    chunks = await client.sources.search(
        temp_notebook.id,
        "artificial intelligence and machine learning",
        limit=2,
    )
    assert chunks and all(isinstance(chunk, RelevantChunk) for chunk in chunks)
    filtered = await client.sources.search(
        temp_notebook.id,
        "artificial intelligence and machine learning",
        source_ids=[chunks[0].source_id],
    )
    assert filtered and all(chunk.source_id == chunks[0].source_id for chunk in filtered)

    suggestions = await client.notebooks.suggest_next_steps(temp_notebook.id)
    assert suggestions and all(isinstance(item, NextStepSuggestion) for item in suggestions)

    choices = await client.artifacts.get_customization_choices(temp_notebook.id)
    assert isinstance(choices, ArtifactCustomizationChoices)
    assert choices.audio and choices.video and choices.slide_deck and choices.reports


@pytest.mark.asyncio
async def test_synchronous_research_discovery(client, temp_notebook) -> None:
    task = await client.research.discover(temp_notebook.id, "history of machine learning")
    assert task.status is ResearchStatus.COMPLETED
    assert task.task_id and task.summary and task.sources


@pytest.mark.asyncio
@pytest.mark.timeout(300)
async def test_notebook_and_source_transfer_lifecycle(
    client,
    temp_notebook,
    created_notebooks,
) -> None:
    copied_notebook = await client.notebooks.copy(
        temp_notebook.id,
        f"E2E RPC copy {uuid4().hex[:8]}",
    )
    created_notebooks.append(copied_notebook.id)
    assert copied_notebook.id != temp_notebook.id

    target = await client.notebooks.create(f"E2E RPC transfer {uuid4().hex[:8]}")
    created_notebooks.append(target.id)

    queued = await client.sources.add_urls_async(temp_notebook.id, ["https://example.com/"])
    assert len(queued) == 1 and queued[0].id
    await client.sources.wait_until_ready(temp_notebook.id, queued[0].id, timeout=120)

    sources = await client.sources.list(temp_notebook.id)
    seed = next(source for source in sources if source.id != queued[0].id)
    before = await client.sources.get_fulltext(temp_notebook.id, seed.id)
    marker = f"E2E append marker {uuid4().hex}"
    await client.sources.append_text(temp_notebook.id, seed.id, marker, header="E2E")
    deadline = asyncio.get_running_loop().time() + 30
    while True:
        after = await client.sources.get_fulltext(temp_notebook.id, seed.id)
        if marker in after.content:
            break
        if asyncio.get_running_loop().time() >= deadline:
            pytest.fail("AppendSource content was not visible within 30 seconds")
        await asyncio.sleep(1)
    assert len(after.content) > len(before.content)

    copied_sources = await client.sources.copy(temp_notebook.id, [seed.id], target.id)
    assert len(copied_sources) == 1
    assert isinstance(copied_sources[0], CopiedSource)
    assert copied_sources[0].original_id == seed.id


@pytest.mark.asyncio
async def test_completed_artifact_copy(client, read_only_notebook_id, temp_notebook) -> None:
    artifacts = await client.artifacts.list(read_only_notebook_id)
    donor = next((artifact for artifact in artifacts if artifact.is_completed), None)
    if donor is None:
        pytest.fail("read-only E2E notebook must contain a completed artifact to copy")

    copied = await client.artifacts.copy(read_only_notebook_id, [donor.id], temp_notebook.id)
    assert len(copied) == 1
    assert isinstance(copied[0], CopiedArtifact)
    assert copied[0].original_id == donor.id
    assert copied[0].artifact.id != donor.id


@pytest.mark.asyncio
async def test_play_books_list_and_add(client, temp_notebook) -> None:
    books = await client.sources.list_play_books()
    exportable = next((book for book in books if not book.export_disabled), None)
    if exportable is None:
        pytest.skip("E2E account has no exportable Google Play Book")

    source = await client.sources.add_play_book(
        temp_notebook.id,
        exportable.content_id,
        wait=False,
    )
    assert source.id
    assert source.kind is SourceType.EXPERT_INTELLIGENCE


@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_chat_session_status_and_cancel(client, temp_notebook) -> None:
    seed = await client.chat.ask(temp_notebook.id, "Summarize this notebook briefly.")
    generation = asyncio.create_task(
        client.chat.ask(
            temp_notebook.id,
            "Write a detailed, exhaustive explanation of every source in this notebook.",
            conversation_id=seed.conversation_id,
        )
    )
    try:
        deadline = asyncio.get_running_loop().time() + 30
        while True:
            active = await client.chat.session_status(temp_notebook.id, seed.conversation_id)
            if active.generating:
                break
            if generation.done():
                await generation
                pytest.fail("Chat generation completed before an active status was observable")
            if asyncio.get_running_loop().time() >= deadline:
                pytest.fail("Chat session did not enter the generating state within 30 seconds")
            await asyncio.sleep(0.1)

        assert isinstance(active, ChatSessionStatus)
        assert active.token
        assert await client.chat.cancel(temp_notebook.id, seed.conversation_id) is None

        deadline = asyncio.get_running_loop().time() + 30
        while True:
            terminal = await client.chat.session_status(temp_notebook.id, seed.conversation_id)
            if not terminal.generating:
                break
            if asyncio.get_running_loop().time() >= deadline:
                pytest.fail("Cancelled chat session did not become idle within 30 seconds")
            await asyncio.sleep(0.25)

        assert isinstance(terminal, ChatSessionStatus)
        assert terminal.token is None
    finally:
        generation.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await generation
