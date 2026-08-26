"""Focused service tests for the neutral P5.2 Audio family."""

from __future__ import annotations

import inspect
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone

import pytest

from notebooklm._artifacts import ArtifactsAPI
from notebooklm._deadline import RuntimeDeadline
from notebooklm._operations import CallPolicy, Operation
from notebooklm._semantic.records import (
    ARTIFACT_GENERATE_AUDIO_DEF,
    ArtifactMediaRecord,
    ArtifactRecord,
    AudioGenerateInput,
    AudioGenerateRequest,
    AudioGenerateResult,
    AudioMetadataRecord,
    GenerationStatusRecord,
)
from notebooklm._semantic.services.read import NotebookReadService
from notebooklm._studio import (
    AudioFamilyService,
    StudioCatalog,
    StudioGenerationInputs,
)
from tests._fixtures.recording_backend import RecordingBackend, set_studio_catalog


def _generation_inputs(backend: RecordingBackend) -> StudioGenerationInputs:
    """The R5.1a resolver every generate family now takes."""
    return StudioGenerationInputs(NotebookReadService(backend))


def _audio(
    artifact_id: str,
    *,
    status: str = "completed",
    created_at: datetime | None = None,
    url: str | None = "https://example.invalid/audio.m4a",
) -> ArtifactRecord:
    return ArtifactRecord(
        id=artifact_id,
        title="Audio",
        family="audio",
        status=status,
        created_at=created_at,
        url=url,
        generation_prompt="Private generation prompt",
        media_urls=(
            ArtifactMediaRecord(
                "https://example.invalid/audio.m4a",
                "progressive",
                None,
                "audio/mp4",
            ),
        ),
        duration_seconds=42.25,
    )


def test_audio_records_are_frozen_slotted_closed_and_redacted() -> None:
    status = GenerationStatusRecord("task", "pending", "https://example.invalid/task")
    value = AudioGenerateInput("nb", ("src",), "en", "Private instructions")
    result = AudioGenerateResult(status)
    metadata = AudioMetadataRecord(
        "task",
        "completed",
        True,
        "https://example.invalid/audio",
        generation_prompt="Private prompt",
    )

    assert all(not hasattr(item, "__dict__") for item in (status, value, result, metadata))
    assert all(item == replace(item) for item in (status, value, result, metadata))
    assert ARTIFACT_GENERATE_AUDIO_DEF.key is Operation.ARTIFACT_GENERATE_AUDIO
    assert ARTIFACT_GENERATE_AUDIO_DEF.policy is CallPolicy.STATEFUL_START
    assert "example.invalid" not in repr(status)
    assert "example.invalid" not in repr(metadata)
    assert "Private instructions" not in repr(value)
    assert "Private prompt" not in repr(metadata)
    with pytest.raises(FrozenInstanceError):
        status.__setattr__("task_id", "changed")


def test_audio_facade_has_one_generation_authority() -> None:
    source = inspect.getsource(ArtifactsAPI.generate_audio)

    assert "self._generation_workflow.generate_once" in source
    assert "self._audio.generate" not in source


@pytest.mark.asyncio
async def test_audio_generate_records_deadline_and_neutral_options() -> None:
    backend = RecordingBackend()
    backend.set_result(
        ARTIFACT_GENERATE_AUDIO_DEF,
        AudioGenerateResult(GenerationStatusRecord("task", "pending")),
    )
    service = AudioFamilyService(backend, StudioCatalog(backend), _generation_inputs(backend))
    deadline = RuntimeDeadline(timeout=5.0, started_at=10.0, monotonic=lambda: 11.0)
    request = AudioGenerateRequest("nb", ("src",), "en", "Focus", "brief", "short")

    result = await service.generate(request, deadline=deadline)

    assert result.status.task_id == "task"
    assert backend.invocations[0].operation is Operation.ARTIFACT_GENERATE_AUDIO
    assert backend.invocations[0].value == AudioGenerateInput(
        "nb", ("src",), "en", "Focus", "brief", "short"
    )
    assert backend.invocations[0].deadline is deadline


@pytest.mark.asyncio
async def test_audio_get_reuses_one_catalog_fetch_and_rejects_other_family() -> None:
    audio = _audio("audio")
    backend = RecordingBackend()
    set_studio_catalog(backend, (audio,))
    service = AudioFamilyService(backend, StudioCatalog(backend), _generation_inputs(backend))

    assert await service.get("nb", "audio") == audio
    # One complete catalog read: artifact.catalog plus the mind-map merge.
    assert len(backend.invocations) == 2

    report_backend = RecordingBackend()
    set_studio_catalog(report_backend, (ArtifactRecord("report", "Report", "report", "completed"),))
    report_service = AudioFamilyService(
        report_backend, StudioCatalog(report_backend), _generation_inputs(report_backend)
    )
    assert await report_service.get("nb", "report") is None


@pytest.mark.asyncio
async def test_audio_catalog_and_download_metadata_use_exact_recency() -> None:
    older = _audio(
        "old",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    newer = _audio(
        "new",
        created_at=datetime(2026, 2, 1, tzinfo=timezone.utc),
    )
    backend = RecordingBackend()
    set_studio_catalog(backend, (older, newer))
    service = AudioFamilyService(backend, StudioCatalog(backend), _generation_inputs(backend))

    latest = await service.select_download("nb", None)
    empty_id = await service.select_download("nb", "")
    explicit = await service.select_download("nb", "old")

    assert latest is not None
    assert (latest.artifact_id, latest.duration_seconds, latest.generation_prompt) == (
        "new",
        42.25,
        "Private generation prompt",
    )
    assert explicit is not None and explicit.artifact_id == "old"
    assert empty_id is not None and empty_id.artifact_id == "new"
    assert len(backend.invocations) == 3


@pytest.mark.asyncio
async def test_audio_latest_selection_preserves_legacy_seconds_precision_and_stability() -> None:
    first = _audio(
        "first",
        created_at=datetime(2026, 1, 1, 0, 0, 0, 100_000, tzinfo=timezone.utc),
    )
    second = _audio(
        "second",
        created_at=datetime(2026, 1, 1, 0, 0, 0, 900_000, tzinfo=timezone.utc),
    )
    backend = RecordingBackend()
    set_studio_catalog(backend, (first, second))

    selected = await AudioFamilyService(
        backend, StudioCatalog(backend), _generation_inputs(backend)
    ).select_download("nb", None)

    assert selected is not None and selected.artifact_id == "first"


def test_audio_usable_readiness_does_not_redefine_lifecycle_terminal_state() -> None:
    completed_without_url = _audio("done", status="completed", url=None)
    metadata = AudioFamilyService.metadata(completed_without_url)

    assert metadata.lifecycle_status == "completed"
    assert metadata.usable is False
    assert metadata.preferred_url is None
