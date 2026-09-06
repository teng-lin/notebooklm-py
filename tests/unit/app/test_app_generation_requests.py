"""Contracts for frozen, discriminated generation requests."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from typing import get_args

import pytest

from notebooklm._app.generation_requests import (
    UNSET,
    AudioGenerationRequest,
    CinematicVideoGenerationRequest,
    DataTableGenerationRequest,
    GenerationRequest,
    MindMapGenerationRequest,
    ReportGenerationRequest,
    ReviseSlideGenerationRequest,
    VideoGenerationRequest,
    build_generation_request,
)
from notebooklm.exceptions import ValidationError
from notebooklm.types import MindMapKind, ReportFormat, VideoFormat, VideoStyle


def test_union_has_exactly_eleven_frozen_variants() -> None:
    variants = get_args(GenerationRequest)
    assert len(variants) == 11
    assert {variant.__dataclass_fields__["kind"].default for variant in variants} == {
        "audio",
        "video",
        "cinematic-video",
        "slide-deck",
        "revise-slide",
        "quiz",
        "flashcards",
        "infographic",
        "data-table",
        "mind-map",
        "report",
    }
    assert all(variant.__dataclass_params__.frozen for variant in variants)


def test_unset_is_distinct_from_explicit_none_and_empty_sources() -> None:
    omitted = AudioGenerationRequest(notebook_id="nb")
    explicit = AudioGenerationRequest(notebook_id="nb", language=None, source_ids=())
    assert omitted.language is UNSET
    assert omitted.source_ids is UNSET
    assert explicit.language is None
    assert explicit.source_ids == ()


def test_requests_are_immutable() -> None:
    request = AudioGenerationRequest(notebook_id="nb")
    with pytest.raises(FrozenInstanceError):
        request.notebook_id = "changed"  # type: ignore[misc]


@pytest.mark.parametrize(
    "kind",
    [
        "audio",
        "video",
        "cinematic-video",
        "slide-deck",
        "quiz",
        "flashcards",
        "infographic",
        "mind-map",
        "report",
    ],
)
def test_factory_builds_each_default_variant(kind: str) -> None:
    request = build_generation_request(kind, notebook_id="nb")  # type: ignore[arg-type]
    assert request.kind == kind


def test_factory_builds_required_variants() -> None:
    table = build_generation_request("data-table", notebook_id="nb", instructions="compare")
    revise = build_generation_request(
        "revise-slide",
        notebook_id="nb",
        artifact_id="artifact",
        slide_index=2,
        instructions="move title",
    )
    assert isinstance(table, DataTableGenerationRequest)
    assert isinstance(revise, ReviseSlideGenerationRequest)


def test_video_cinematic_normalizes_to_distinct_variant() -> None:
    request = build_generation_request(
        "video", notebook_id="nb", video_format=VideoFormat.CINEMATIC
    )
    assert isinstance(request, CinematicVideoGenerationRequest)


def test_video_custom_style_trims_prompt() -> None:
    request = VideoGenerationRequest(
        notebook_id="nb",
        video_style=VideoStyle.CUSTOM,
        style_prompt="  ink drawing  ",
    )
    assert request.style_prompt == "ink drawing"


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"video_style": VideoStyle.CUSTOM}, "--style custom"),
        ({"style_prompt": "ink"}, "requires --style custom"),
        (
            {"video_format": VideoFormat.SHORT, "video_style": VideoStyle.CLASSIC},
            "fixed visual style",
        ),
    ],
)
def test_video_validation(kwargs: dict[str, object], match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        VideoGenerationRequest(notebook_id="nb", **kwargs)  # type: ignore[arg-type]


def test_common_validation() -> None:
    with pytest.raises(ValidationError, match="notebook_id"):
        AudioGenerationRequest(notebook_id=" ")
    with pytest.raises(ValidationError, match="timeout"):
        AudioGenerationRequest(notebook_id="nb", timeout=0)
    with pytest.raises(ValidationError, match="interval"):
        AudioGenerationRequest(notebook_id="nb", interval=0)
    with pytest.raises(ValidationError, match="max_retries"):
        AudioGenerationRequest(notebook_id="nb", max_retries=-1)


def test_kind_specific_fields_remain_typed() -> None:
    report = ReportGenerationRequest(
        notebook_id="nb", report_format=ReportFormat.STUDY_GUIDE, custom_prompt=None
    )
    mind_map = MindMapGenerationRequest(notebook_id="nb", map_kind=MindMapKind.NOTE_BACKED)
    assert report.report_format is ReportFormat.STUDY_GUIDE
    assert report.custom_prompt is None
    assert mind_map.map_kind is MindMapKind.NOTE_BACKED
