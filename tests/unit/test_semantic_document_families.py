"""Focused service tests for the neutral P5.4 report and video families."""

from __future__ import annotations

import inspect
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone

import pytest

from notebooklm._artifacts import ArtifactsAPI
from notebooklm._deadline import RuntimeDeadline
from notebooklm._operations import CallPolicy, Operation
from notebooklm._read_services import NotebookReadService
from notebooklm._records import (
    ARTIFACT_GENERATE_REPORT_DEF,
    ARTIFACT_GENERATE_VIDEO_DEF,
    ARTIFACT_GET_DEF,
    ARTIFACT_LIST_DEF,
    ArtifactGetResult,
    ArtifactListResult,
    ArtifactMediaRecord,
    ArtifactRecord,
    GenerationStatusRecord,
    ReportGenerateInput,
    ReportGenerateResult,
    ReportMetadataRecord,
    VideoGenerateInput,
    VideoGenerateRequest,
    VideoGenerateResult,
    VideoMetadataRecord,
)
from notebooklm._studio import (
    DocumentOptionError,
    ReportFamilyService,
    StudioCatalog,
    StudioGenerationInputs,
    VideoFamilyService,
)
from tests._fixtures.recording_backend import RecordingBackend


def _generation_inputs(backend: RecordingBackend) -> StudioGenerationInputs:
    """The R5.1a resolver every generate family now takes."""
    return StudioGenerationInputs(NotebookReadService(backend))


def _artifact(
    artifact_id: str,
    family: str,
    *,
    status: str = "completed",
    url: str | None = None,
    report_kind: str | None = None,
) -> ArtifactRecord:
    return ArtifactRecord(
        id=artifact_id,
        title=artifact_id.title(),
        family=family,
        status=status,
        created_at=datetime(2026, 8, 24, tzinfo=timezone.utc),
        url=url,
        generation_prompt="Private generation prompt",
        media_urls=(
            ArtifactMediaRecord(
                "https://example.invalid/video.mp4",
                "download",
                None,
                "video/mp4",
            ),
        ),
        duration_seconds=42.5,
        report_kind=report_kind,
        source_ids=("src-a", "src-b"),
    )


def test_document_records_are_frozen_slotted_typed_and_redacted() -> None:
    status = GenerationStatusRecord("task", "pending", "https://example.invalid/task")
    video = VideoGenerateInput(
        "nb",
        ("src",),
        "en",
        "Private instructions",
        "brief",
        "custom",
        "Private style prompt",
    )
    report = ReportGenerateInput(
        "nb",
        ("src",),
        "en",
        "custom",
        "Private custom prompt",
        "Private extras",
    )
    values = (
        status,
        video,
        report,
        VideoGenerateResult(status),
        ReportGenerateResult(status),
        VideoMetadataRecord("v", "completed", True, "https://example.invalid/video"),
        ReportMetadataRecord("r", "completed", True, generation_prompt="Private prompt"),
    )

    assert all(not hasattr(value, "__dict__") for value in values)
    assert all(value == replace(value) for value in values)
    assert all(isinstance(hash(value), int) for value in values)
    assert ARTIFACT_GENERATE_VIDEO_DEF.key is Operation.ARTIFACT_GENERATE_VIDEO
    assert ARTIFACT_GENERATE_REPORT_DEF.key is Operation.ARTIFACT_GENERATE_REPORT
    assert ARTIFACT_GENERATE_VIDEO_DEF.policy is CallPolicy.STATEFUL_START
    assert ARTIFACT_GENERATE_REPORT_DEF.policy is CallPolicy.STATEFUL_START
    assert "example.invalid" not in repr(status)
    assert "Private" not in repr(video)
    assert "Private" not in repr(report)
    assert "Private" not in repr(values[-1])
    with pytest.raises(FrozenInstanceError):
        status.__setattr__("task_id", "changed")


def test_public_facade_signatures_and_single_generation_authorities_are_preserved() -> None:
    assert list(inspect.signature(ArtifactsAPI.generate_video).parameters) == [
        "self",
        "notebook_id",
        "source_ids",
        "language",
        "instructions",
        "video_format",
        "video_style",
        "style_prompt",
    ]
    assert list(inspect.signature(ArtifactsAPI.generate_cinematic_video).parameters) == [
        "self",
        "notebook_id",
        "source_ids",
        "language",
        "instructions",
    ]
    assert list(inspect.signature(ArtifactsAPI.generate_report).parameters) == [
        "self",
        "notebook_id",
        "report_format",
        "source_ids",
        "language",
        "custom_prompt",
        "extra_instructions",
    ]
    assert list(inspect.signature(ArtifactsAPI.generate_study_guide).parameters) == [
        "self",
        "notebook_id",
        "source_ids",
        "language",
        "extra_instructions",
    ]
    for method in (
        ArtifactsAPI.generate_video,
        ArtifactsAPI.generate_cinematic_video,
        ArtifactsAPI.generate_report,
    ):
        source = inspect.getsource(method)
        assert "self._generation_workflow.generate_once" in source
        assert "self._video.generate" not in source
        assert "self._reports.generate" not in source
    assert "self.generate_report" in inspect.getsource(ArtifactsAPI.generate_study_guide)


@pytest.mark.asyncio
async def test_video_service_normalizes_prompt_and_preserves_deadline_identity() -> None:
    backend = RecordingBackend()
    backend.set_result(
        ARTIFACT_GENERATE_VIDEO_DEF,
        VideoGenerateResult(GenerationStatusRecord("task", "pending")),
    )
    service = VideoFamilyService(backend, StudioCatalog(backend), _generation_inputs(backend))
    deadline = RuntimeDeadline(timeout=5.0, started_at=10.0, monotonic=lambda: 11.0)

    result = await service.generate(
        VideoGenerateRequest(
            "nb",
            ("src",),
            video_style="custom",
            style_prompt="  Hand-drawn diagrams  ",
        ),
        deadline=deadline,
    )

    assert result.status.task_id == "task"
    assert backend.invocations[0].operation is Operation.ARTIFACT_GENERATE_VIDEO
    assert backend.invocations[0].deadline is deadline
    assert isinstance(backend.invocations[0].value, VideoGenerateInput)
    assert backend.invocations[0].value.style_prompt == "Hand-drawn diagrams"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("value", "message"),
    [
        (VideoGenerateRequest("nb", (), video_style="custom"), "style_prompt is required"),
        (
            VideoGenerateRequest("nb", (), video_format="short", video_style="anime"),
            "not supported for short videos",
        ),
        (
            VideoGenerateRequest("nb", (), video_style="anime", style_prompt="Ink"),
            "style_prompt requires",
        ),
    ],
)
async def test_video_behavior_rejects_invalid_options_before_backend(
    value: VideoGenerateRequest,
    message: str,
) -> None:
    backend = RecordingBackend()
    service = VideoFamilyService(backend, StudioCatalog(backend), _generation_inputs(backend))

    with pytest.raises(DocumentOptionError, match=message):
        await service.generate(value)

    assert backend.invocations == []


@pytest.mark.asyncio
async def test_report_and_video_services_filter_catalog_records_without_extra_fetches() -> None:
    video = _artifact("video", "video", url="https://example.invalid/video.mp4")
    report = _artifact("report", "report", report_kind="Study Guide")
    list_backend = RecordingBackend()
    list_backend.set_result(ARTIFACT_LIST_DEF, ArtifactListResult((video, report)))

    videos = await VideoFamilyService(
        list_backend, StudioCatalog(list_backend), _generation_inputs(list_backend)
    ).list("nb")
    reports = await ReportFamilyService(
        list_backend, StudioCatalog(list_backend), _generation_inputs(list_backend)
    ).list("nb")

    assert videos == (video,)
    assert reports == (report,)
    assert len(list_backend.invocations) == 2

    get_backend = RecordingBackend()
    get_backend.set_result(ARTIFACT_GET_DEF, ArtifactGetResult(report))
    video_service = VideoFamilyService(
        get_backend, StudioCatalog(get_backend), _generation_inputs(get_backend)
    )
    assert await video_service.get("nb", "report") is None
    assert len(get_backend.invocations) == 1


def test_document_metadata_keeps_lifecycle_and_family_usability_distinct() -> None:
    video_without_url = _artifact("video", "video", url=None)
    report = _artifact("report", "report", report_kind="Concept Explanation")
    future = _artifact("future", "report", report_kind="Future Report Shape")

    video_meta = VideoFamilyService.metadata(video_without_url)
    report_meta = ReportFamilyService.metadata(report)
    future_meta = ReportFamilyService.metadata(future)

    assert (video_meta.lifecycle_status, video_meta.usable, video_meta.preferred_url) == (
        "completed",
        False,
        None,
    )
    assert (report_meta.usable, report_meta.report_kind, report_meta.report_format) == (
        True,
        "Concept Explanation",
        "concept_explanation",
    )
    assert future_meta.report_kind == "Future Report Shape"
    assert future_meta.report_format is None
    assert future_meta.source_ids == ("src-a", "src-b")
