"""Encode normalized artifact domain requests into Web protocol fields only."""

from __future__ import annotations

from typing import Any

from typing_extensions import assert_never

from ..._artifact import creation_normalized as n
from ..._types.enums import INTERACTIVE_MIND_MAP_VARIANT, ArtifactTypeCode, VideoFormat, VideoStyle
from ...rpc import nest_source_ids
from .artifacts import _artifact_client_options


def encode_creation(request: n.NormalizedArtifactCreationRequest) -> tuple[list[Any], str]:
    sources = nest_source_ids(list(request.source_ids), 2)
    double = nest_source_ids(list(request.source_ids), 1)
    if isinstance(request, n.NormalizedAudio):
        kind, slot, label = ArtifactTypeCode.AUDIO.value, 6, "audio"
        config: Any = [
            None,
            [
                request.instructions,
                request.length_code,
                None,
                double,
                request.language,
                None,
                request.format_code,
            ],
        ]
    elif isinstance(request, n.NormalizedVideo):
        kind, slot, label = ArtifactTypeCode.VIDEO.value, 8, "video"
        video = [
            double,
            request.language,
            request.instructions,
            None,
            request.format_code,
            None if request.style_code == VideoStyle.CUSTOM.value else request.style_code,
        ]
        if request.style_code == VideoStyle.CUSTOM.value and request.style_prompt:
            video.append(request.style_prompt)
        config = [None, None, video]
    elif isinstance(request, n.NormalizedCinematicVideo):
        kind, slot, label = ArtifactTypeCode.VIDEO.value, 8, "cinematic video"
        config = [
            None,
            None,
            [double, request.language, request.instructions, None, VideoFormat.CINEMATIC.value],
        ]
    elif isinstance(request, n.NormalizedReport):
        kind, slot, label = ArtifactTypeCode.REPORT.value, 7, "report"
        config = [
            None,
            [
                request.title,
                request.description,
                None,
                double,
                request.language,
                request.directive,
                None,
                True,
            ],
        ]
    elif isinstance(request, n.NormalizedQuiz):
        kind, slot, label = ArtifactTypeCode.QUIZ_FLASHCARD.value, 9, "quiz"
        config = [
            None,
            [
                2,
                None,
                request.instructions,
                None,
                None,
                None,
                None,
                [request.quantity_code, request.difficulty_code],
            ],
        ]
    elif isinstance(request, n.NormalizedFlashcards):
        kind, slot, label = ArtifactTypeCode.QUIZ_FLASHCARD.value, 9, "flashcards"
        config = [
            None,
            [
                1,
                None,
                request.instructions,
                None,
                None,
                None,
                [request.quantity_code, request.difficulty_code],
            ],
        ]
    elif isinstance(request, n.NormalizedInteractiveMindMap):
        kind, slot, label = ArtifactTypeCode.QUIZ_FLASHCARD.value, 9, "mind_map"
        config = [
            None,
            [INTERACTIVE_MIND_MAP_VARIANT, None, request.instructions]
            if request.instructions
            else [INTERACTIVE_MIND_MAP_VARIANT],
        ]
    elif isinstance(request, n.NormalizedInfographic):
        kind, slot, label = ArtifactTypeCode.INFOGRAPHIC.value, 14, "infographic"
        config = [
            [
                request.instructions,
                request.language,
                None,
                request.orientation_code,
                request.detail_code,
                request.style_code,
            ]
        ]
    elif isinstance(request, n.NormalizedSlideDeck):
        kind, slot, label = ArtifactTypeCode.SLIDE_DECK.value, 16, "slide deck"
        config = [
            [request.instructions, request.language, request.format_code, request.length_code]
        ]
    elif isinstance(request, n.NormalizedDataTable):
        kind, slot, label = ArtifactTypeCode.DATA_TABLE.value, 18, "data table"
        config = [None, [request.instructions, request.language]]
    else:
        assert_never(request)
    descriptor: list[Any] = [None, None, kind, sources]
    descriptor.extend([None] * (slot - len(descriptor)))
    descriptor.append(config)
    params: list[Any] = [_artifact_client_options(), request.notebook_id, descriptor]
    if isinstance(request, n.NormalizedAudio) and request.later:
        # CreateArtifactRequest #7 wraps the deferred-generation bool at #1.
        # See docs/android/deferred-audio-generation-evidence.md.
        params.extend([None, None, None, [True]])
    return params, label
