"""Execution contracts for typed generation requests."""

from __future__ import annotations

import contextlib
from unittest.mock import AsyncMock, MagicMock

import pytest

from notebooklm._app.generate import GenerationWaitStarted, execute_generation
from notebooklm._app.generation_requests import (
    UNSET,
    AudioGenerationRequest,
    DataTableGenerationRequest,
    MindMapGenerationRequest,
    ReviseSlideGenerationRequest,
)
from notebooklm.types import GenerationStatus, MindMapKind


async def _notebook(_client: object, reference: str) -> str:
    return f"resolved-{reference}"


async def _sources(_client: object, _notebook_id: str, refs: tuple[str, ...]) -> list[str]:
    return [f"resolved-{ref}" for ref in refs]


def _client() -> MagicMock:
    client = MagicMock()
    client.artifacts.generate_audio = AsyncMock(
        return_value=GenerationStatus(task_id="task", status="pending")
    )
    client.artifacts.generate_data_table = AsyncMock(
        return_value=GenerationStatus(task_id="table", status="pending")
    )
    client.artifacts.revise_slide = AsyncMock(
        return_value=GenerationStatus(task_id="slide", status="pending")
    )
    client.artifacts.generate_mind_map = AsyncMock(return_value={"root": "note"})
    client.mind_maps.generate = AsyncMock(return_value={"root": "interactive"})
    return client


@pytest.mark.asyncio
async def test_omitted_sources_and_language_are_not_forwarded() -> None:
    client = _client()
    request = AudioGenerationRequest(notebook_id="nb")
    result = await execute_generation(
        request,
        client,
        notebook_resolver=_notebook,
        source_resolver=_sources,
    )
    assert result.kind == "audio"
    client.artifacts.generate_audio.assert_awaited_once_with(
        "resolved-nb",
        source_ids=None,
        audio_format=request.audio_format,
        audio_length=request.audio_length,
    )


@pytest.mark.asyncio
async def test_explicit_empty_sources_and_none_language_are_preserved() -> None:
    client = _client()
    await execute_generation(
        AudioGenerationRequest(notebook_id="nb", source_ids=(), language=None),
        client,
        notebook_resolver=_notebook,
        source_resolver=_sources,
    )
    kwargs = client.artifacts.generate_audio.await_args.kwargs
    assert kwargs["source_ids"] == []
    assert "language" in kwargs and kwargs["language"] is None


@pytest.mark.asyncio
async def test_nonempty_sources_resolve_once() -> None:
    client = _client()
    resolver = AsyncMock(side_effect=_sources)
    await execute_generation(
        AudioGenerationRequest(notebook_id="nb", source_ids=("a", "b")),
        client,
        notebook_resolver=_notebook,
        source_resolver=resolver,
    )
    resolver.assert_awaited_once_with(client, "resolved-nb", ("a", "b"))
    assert client.artifacts.generate_audio.await_args.kwargs["source_ids"] == [
        "resolved-a",
        "resolved-b",
    ]


@pytest.mark.asyncio
async def test_wait_emits_one_frozen_semantic_event() -> None:
    client = _client()
    client.artifacts.wait_for_completion = AsyncMock(
        return_value=GenerationStatus(task_id="task", status="completed")
    )
    events: list[GenerationWaitStarted] = []

    @contextlib.asynccontextmanager
    async def wait_context(event: GenerationWaitStarted):
        assert event.kind == "audio"
        yield

    result = await execute_generation(
        AudioGenerationRequest(notebook_id="nb", wait=True),
        client,
        notebook_resolver=_notebook,
        source_resolver=_sources,
        wait_context=wait_context,
        wait_start_sink=events.append,
    )
    assert events == [GenerationWaitStarted(kind="audio", task_id="task", elapsed=0.0)]
    assert result.generation is not None and result.generation.status == "completed"


@pytest.mark.asyncio
async def test_revise_slide_uses_only_immutable_target_fields() -> None:
    client = _client()
    await execute_generation(
        ReviseSlideGenerationRequest(
            notebook_id="nb", artifact_id="artifact", slide_index=3, prompt="move"
        ),
        client,
        notebook_resolver=_notebook,
        source_resolver=_sources,
    )
    client.artifacts.revise_slide.assert_awaited_once_with(
        "resolved-nb", artifact_id="artifact", slide_index=3, prompt="move"
    )


@pytest.mark.asyncio
async def test_data_table_forwards_required_prompt() -> None:
    client = _client()
    await execute_generation(
        DataTableGenerationRequest(notebook_id="nb", instructions="compare", source_ids=UNSET),
        client,
        notebook_resolver=_notebook,
        source_resolver=_sources,
    )
    assert client.artifacts.generate_data_table.await_args.kwargs["instructions"] == "compare"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "expected_api"),
    [
        (MindMapKind.INTERACTIVE, "mind_maps"),
        (MindMapKind.NOTE_BACKED, "artifacts"),
    ],
)
async def test_mind_map_variants_dispatch_to_their_typed_owner(
    kind: MindMapKind, expected_api: str
) -> None:
    client = _client()
    result = await execute_generation(
        MindMapGenerationRequest(notebook_id="nb", map_kind=kind, instructions="focus"),
        client,
        notebook_resolver=_notebook,
        source_resolver=_sources,
    )
    assert result.kind == "mind-map"
    if expected_api == "mind_maps":
        client.mind_maps.generate.assert_awaited_once()
        client.artifacts.generate_mind_map.assert_not_awaited()
    else:
        client.artifacts.generate_mind_map.assert_awaited_once()
        client.mind_maps.generate.assert_not_awaited()
