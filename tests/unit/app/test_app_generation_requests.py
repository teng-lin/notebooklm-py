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
    GenerationRequestValidationError,
    GenerationValidationCode,
    MindMapGenerationRequest,
    ReportGenerationRequest,
    ReviseSlideGenerationRequest,
    VideoGenerationRequest,
    build_generation_request,
    generation_option_choices,
)
from notebooklm.exceptions import ValidationError
from notebooklm.types import (
    InfographicOrientation,
    MindMapKind,
    QuizDifficulty,
    ReportFormat,
    VideoFormat,
    VideoStyle,
)

_GENERATION_KINDS = [
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
]


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
    [kind for kind in _GENERATION_KINDS if kind not in ("data-table", "revise-slide")],
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


@pytest.mark.parametrize(
    ("kind", "video_format"),
    [
        ("video", VideoFormat.CINEMATIC),
        ("cinematic-video", VideoFormat.EXPLAINER),
    ],
)
@pytest.mark.parametrize("video_style", list(VideoStyle))
def test_factory_ignores_every_video_style_for_cinematic(
    kind: str,
    video_format: VideoFormat,
    video_style: VideoStyle,
) -> None:
    request = build_generation_request(
        kind,  # type: ignore[arg-type]
        notebook_id="nb",
        video_format=video_format,
        video_style=video_style,
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
    ("style_prompt", "expected"),
    [(UNSET, UNSET), (None, None), ("", ""), ("   ", "")],
)
def test_video_style_prompt_preserves_unset_and_explicit_values(
    style_prompt: object, expected: object
) -> None:
    request = build_generation_request(
        "video",
        notebook_id="nb",
        style_prompt=style_prompt,  # type: ignore[arg-type]
    )
    assert isinstance(request, VideoGenerationRequest)
    if expected is UNSET:
        assert request.style_prompt is UNSET
    else:
        assert request.style_prompt == expected


@pytest.mark.parametrize(
    ("kwargs", "code", "match"),
    [
        (
            {"video_style": VideoStyle.CUSTOM},
            "custom_style_prompt_required",
            "video_style=custom",
        ),
        (
            {"style_prompt": "ink"},
            "style_prompt_requires_custom",
            "requires video_style=custom",
        ),
        (
            {"video_format": VideoFormat.SHORT, "video_style": VideoStyle.CLASSIC},
            "short_video_style",
            "fixed visual style",
        ),
    ],
)
def test_video_validation(
    kwargs: dict[str, object], code: GenerationValidationCode, match: str
) -> None:
    with pytest.raises(GenerationRequestValidationError, match=match) as exc_info:
        VideoGenerationRequest(notebook_id="nb", **kwargs)  # type: ignore[arg-type]
    assert exc_info.value.code == code


@pytest.mark.parametrize(
    ("kind", "video_format"),
    [
        ("video", VideoFormat.CINEMATIC),
        ("cinematic-video", VideoFormat.EXPLAINER),
    ],
)
@pytest.mark.parametrize(
    ("kwargs", "code", "match"),
    [
        (
            {"style_prompt": "ink"},
            "cinematic_style_prompt",
            "style_prompt is not supported",
        ),
        (
            {"video_style": VideoStyle.CUSTOM, "style_prompt": "ink"},
            "cinematic_style_prompt",
            "style_prompt is not supported",
        ),
    ],
)
def test_factory_validates_video_options_before_cinematic_normalization(
    kind: str,
    video_format: VideoFormat,
    kwargs: dict[str, object],
    code: GenerationValidationCode,
    match: str,
) -> None:
    with pytest.raises(GenerationRequestValidationError, match=match) as exc_info:
        build_generation_request(
            kind,  # type: ignore[arg-type]
            notebook_id="nb",
            video_format=video_format,
            **kwargs,  # type: ignore[arg-type]
        )
    assert exc_info.value.code == code


def test_factory_non_cinematic_custom_style_still_requires_prompt() -> None:
    with pytest.raises(
        GenerationRequestValidationError,
        match="video_style=custom requires a non-empty style_prompt",
    ) as exc_info:
        build_generation_request(
            "video",
            notebook_id="nb",
            video_style=VideoStyle.CUSTOM,
        )
    assert exc_info.value.code == "custom_style_prompt_required"
    assert "--" not in str(exc_info.value)


@pytest.mark.parametrize(
    ("kind", "kwargs", "option", "allowed"),
    [
        (
            "audio",
            {"orientation": InfographicOrientation.LANDSCAPE},
            "orientation",
            ("audio_format", "audio_length"),
        ),
        ("quiz", {"video_style": VideoStyle.CLASSIC}, "style", ("quantity", "difficulty")),
        ("data-table", {"difficulty": QuizDifficulty.EASY}, "difficulty", ()),
    ],
)
def test_factory_rejects_direct_wrong_kind_options_with_typed_context(
    kind: str,
    kwargs: dict[str, object],
    option: str,
    allowed: tuple[str, ...],
) -> None:
    with pytest.raises(GenerationRequestValidationError) as exc_info:
        build_generation_request(
            kind,  # type: ignore[arg-type]
            notebook_id="nb",
            instructions="required",
            **kwargs,  # type: ignore[arg-type]
        )

    exc = exc_info.value
    assert exc.code == "option_not_valid_for_kind"
    assert exc.params == {
        "kind": kind,
        "option": option,
        "value": next(iter(kwargs.values())),
        "allowed_options": allowed,
    }


def test_factory_normalizes_schema_options_and_preserves_style_prompt_none() -> None:
    request = build_generation_request(
        "video",
        notebook_id="nb",
        option_values={
            "video_format": "brief",
            "style": "custom",
            "style_prompt": "  ink drawing  ",
        },
    )
    explicit_none = build_generation_request(
        "video",
        notebook_id="nb",
        option_values={"video_format": None, "style": None, "style_prompt": None},
    )

    assert isinstance(request, VideoGenerationRequest)
    assert request.video_format is VideoFormat.BRIEF
    assert request.video_style is VideoStyle.CUSTOM
    assert request.style_prompt == "ink drawing"
    assert isinstance(explicit_none, VideoGenerationRequest)
    assert explicit_none.video_format is VideoFormat.EXPLAINER
    assert explicit_none.video_style is VideoStyle.AUTO_SELECT
    assert explicit_none.style_prompt is None


def test_factory_schema_wrong_kind_matches_direct_typed_error_shape() -> None:
    with pytest.raises(GenerationRequestValidationError) as exc_info:
        build_generation_request(
            "audio",
            notebook_id="nb",
            option_values={"orientation": "landscape"},
        )

    assert exc_info.value.code == "option_not_valid_for_kind"
    assert exc_info.value.params == {
        "kind": "audio",
        "option": "orientation",
        "value": "landscape",
        "allowed_options": ("audio_format", "audio_length"),
    }
    assert generation_option_choices("audio") == {
        "audio_format": ("deep-dive", "brief", "critique", "debate"),
        "audio_length": ("short", "default", "long"),
    }


def test_factory_invalid_schema_value_has_typed_choice_context() -> None:
    with pytest.raises(GenerationRequestValidationError) as exc_info:
        build_generation_request(
            "video",
            notebook_id="nb",
            option_values={"style": "professional"},
        )

    exc = exc_info.value
    assert exc.code == "invalid_option_value"
    assert exc.params["kind"] == "video"
    assert exc.params["option"] == "style"
    assert exc.params["value"] == "professional"
    assert exc.params["choices"] == tuple(generation_option_choices("video")["style"] or ())


@pytest.mark.parametrize("kind", _GENERATION_KINDS)
def test_factory_applies_common_validation_to_every_variant(kind: str) -> None:
    with pytest.raises(ValidationError, match="notebook_id"):
        build_generation_request(
            kind,  # type: ignore[arg-type]
            notebook_id=" ",
            instructions="required",
            artifact_id="artifact",
        )


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
