"""Focused service tests for the neutral P5.5 visual families."""

from __future__ import annotations

import inspect
from dataclasses import FrozenInstanceError, replace

import pytest

from notebooklm._artifacts import ArtifactsAPI
from notebooklm._deadline import RuntimeDeadline
from notebooklm._operations import CallPolicy, Operation
from notebooklm._read_services import NotebookReadService
from notebooklm._records import (
    ARTIFACT_GENERATE_INFOGRAPHIC_DEF,
    ARTIFACT_GENERATE_SLIDE_DECK_DEF,
    ArtifactInfographicRecord,
    ArtifactRecord,
    ArtifactSlideRecord,
    GenerationStatusRecord,
    InfographicGenerateInput,
    InfographicGenerateRequest,
    SlideDeckGenerateInput,
    SlideDeckGenerateRequest,
    VisualGenerateResult,
    VisualMetadataRecord,
)
from notebooklm._studio import (
    StudioCatalog,
    StudioGenerationInputs,
    VisualFamilyService,
)
from tests._fixtures.recording_backend import RecordingBackend, set_studio_catalog


def _generation_inputs(backend: RecordingBackend) -> StudioGenerationInputs:
    """The R5.1a resolver every generate family now takes."""
    return StudioGenerationInputs(NotebookReadService(backend))


def _infographic() -> ArtifactRecord:
    return ArtifactRecord(
        "info",
        "Infographic",
        "infographic",
        "completed",
        url="https://example.invalid/info.png",
        generation_prompt="Private visual prompt",
        infographics=(
            ArtifactInfographicRecord(
                "Overview",
                "https://example.invalid/info.png",
                2752,
                1536,
                "Accessible overview",
                "Visible infographic text",
            ),
        ),
    )


def _slide_deck() -> ArtifactRecord:
    return ArtifactRecord(
        "deck",
        "Slide Deck",
        "slide_deck",
        "completed",
        slides=(
            ArtifactSlideRecord(
                "https://example.invalid/slide.png",
                1376,
                768,
                "Accessible slide",
                "Visible slide text",
            ),
        ),
    )


def test_visual_records_are_frozen_slotted_closed_and_redacted() -> None:
    status = GenerationStatusRecord("task", "pending", "https://example.invalid/task")
    infographic = InfographicGenerateInput(
        "nb", ("src",), "en", "Private prompt", "portrait", "detailed", "anime"
    )
    slides = SlideDeckGenerateInput(
        "nb", ("src",), "en", "Private prompt", "presenter_slides", "short"
    )
    result = VisualGenerateResult(status)
    metadata = VisualMetadataRecord(
        "task", "infographic", "completed", True, generation_prompt="Private prompt"
    )

    assert all(not hasattr(item, "__dict__") for item in (status, infographic, slides, result))
    assert all(item == replace(item) for item in (status, infographic, slides, result, metadata))
    assert ARTIFACT_GENERATE_INFOGRAPHIC_DEF.policy is CallPolicy.STATEFUL_START
    assert ARTIFACT_GENERATE_SLIDE_DECK_DEF.policy is CallPolicy.STATEFUL_START
    assert "Private" not in repr(infographic)
    assert "Private" not in repr(slides)
    assert "example.invalid" not in repr(status)
    assert "Private" not in repr(metadata)
    with pytest.raises(FrozenInstanceError):
        status.__setattr__("task_id", "changed")


def test_visual_facade_has_one_generation_and_listing_authority() -> None:
    for method in (
        ArtifactsAPI.generate_infographic,
        ArtifactsAPI.generate_slide_deck,
    ):
        source = inspect.getsource(method)
        assert "self._generation_workflow.generate_once" in source
        assert "self._visuals" not in source
    for method in (
        ArtifactsAPI.list_infographics,
        ArtifactsAPI.list_slide_decks,
    ):
        source = inspect.getsource(method)
        assert "self._visuals" in source
        assert "self._generation" not in source


@pytest.mark.asyncio
async def test_visual_generation_records_exact_operations_values_and_deadline() -> None:
    backend = RecordingBackend()
    result = VisualGenerateResult(GenerationStatusRecord("task", "pending"))
    backend.set_result(ARTIFACT_GENERATE_INFOGRAPHIC_DEF, result)
    backend.set_result(ARTIFACT_GENERATE_SLIDE_DECK_DEF, result)
    service = VisualFamilyService(backend, StudioCatalog(backend), _generation_inputs(backend))
    deadline = RuntimeDeadline(timeout=5.0, started_at=10.0, monotonic=lambda: 11.0)
    infographic = InfographicGenerateRequest("nb", ("src",), orientation="square")
    slides = SlideDeckGenerateRequest("nb", ("src",), slide_format="presenter_slides")

    assert await service.generate_infographic(infographic, deadline=deadline) == result
    assert await service.generate_slide_deck(slides, deadline=deadline) == result
    assert [item.operation for item in backend.invocations] == [
        Operation.ARTIFACT_GENERATE_INFOGRAPHIC,
        Operation.ARTIFACT_GENERATE_SLIDE_DECK,
    ]
    assert [item.value for item in backend.invocations] == [
        InfographicGenerateInput("nb", ("src",), "en", orientation="square"),
        SlideDeckGenerateInput("nb", ("src",), "en", slide_format="presenter_slides"),
    ]
    assert all(item.deadline is deadline for item in backend.invocations)


@pytest.mark.asyncio
async def test_visual_catalog_and_get_preserve_rendered_accessibility_metadata() -> None:
    infographic = _infographic()
    slides = _slide_deck()
    backend = RecordingBackend()
    set_studio_catalog(backend, (infographic, slides))
    service = VisualFamilyService(backend, StudioCatalog(backend), _generation_inputs(backend))

    assert await service.list_infographics("nb") == (infographic,)
    assert await service.list_slide_decks("nb") == (slides,)
    assert await service.get("nb", "deck", "slide_deck") is slides
    infographic_metadata = service.metadata(infographic)
    slide_metadata = service.metadata(slides)

    assert infographic_metadata.infographics[0].width == 2752
    assert infographic_metadata.infographics[0].height == 1536
    assert infographic_metadata.infographics[0].alt_text == "Accessible overview"
    assert infographic_metadata.infographics[0].text == "Visible infographic text"
    assert slide_metadata.slides[0].width == 1376
    assert slide_metadata.slides[0].height == 768
    assert slide_metadata.slides[0].alt_text == "Accessible slide"
    assert slide_metadata.slides[0].text == "Visible slide text"
    # Two family listings skip the note-backed merge; the identity read cannot.
    assert [item.operation for item in backend.invocations] == [
        Operation.ARTIFACT_CATALOG,
        Operation.ARTIFACT_CATALOG,
        Operation.ARTIFACT_CATALOG,
        Operation.MIND_MAP_LIST,
    ]


def test_visual_usable_readiness_requires_terminal_state_and_a_representation() -> None:
    without_render = ArtifactRecord("empty", "Empty", "slide_deck", "completed")
    pending = replace(_infographic(), status="pending")

    assert VisualFamilyService.metadata(without_render).usable is False
    assert VisualFamilyService.metadata(pending).usable is False
    assert VisualFamilyService.metadata(_infographic()).usable is True
    assert VisualFamilyService.metadata(_slide_deck()).usable is True
