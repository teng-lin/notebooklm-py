"""Row-facing request payloads and kickoff decoding for the Studio generate families.

Since P10 R5.1a the eight ``CREATE_ARTIFACT`` generate families are single-native
codec rows.  Under ADR-0035 addendum D1(a) their inputs arrive **pre-resolved**:
``_studio/generation.py`` has already chosen the source set, the language and a
reviewed option value before ``invoke`` reaches this module, so nothing here
defaults anything and nothing here judges a vocabulary.  What is left is the
mapping from the neutral option strings onto the wire enums, the guarded
``CREATE_ARTIFACT`` payload each family dispatches (params plus the notebook
route and the ``allow_null``/``raise_on_null_status`` options) without naming a
method — the row's ``NativeCallSpec`` is the sole method authority — and the
per-family kickoff decode.
"""

from __future__ import annotations

from typing import Any, Literal

from ..._binding import CodecPayload
from ..._operations import Operation
from ..._records import (
    AudioGenerateInput,
    AudioGenerateResult,
    DataTableGenerateInput,
    DataTableGenerateResult,
    GenerationStatusRecord,
    InfographicGenerateInput,
    InteractiveGenerateInput,
    InteractiveGenerateResult,
    ReportGenerateInput,
    ReportGenerateResult,
    SlideDeckGenerateInput,
    VideoGenerateInput,
    VideoGenerateResult,
    VisualGenerateResult,
)
from ...rpc import (
    AudioFormat,
    AudioLength,
    InfographicDetail,
    InfographicOrientation,
    InfographicStyle,
    QuizDifficulty,
    QuizQuantity,
    RPCMethod,
    SlideDeckFormat,
    SlideDeckLength,
)
from .artifact_payloads import (
    build_audio_artifact_params,
    build_data_table_artifact_params,
    build_flashcards_artifact_params,
    build_infographic_artifact_params,
    build_quiz_artifact_params,
    build_slide_deck_artifact_params,
)
from .studio_documents import (
    artifact_feature_unavailable,
    decode_generation_status,
    encode_report_generation,
    encode_video_generation,
    wire_option,
)

_AUDIO_FORMATS = {
    "deep_dive": AudioFormat.DEEP_DIVE,
    "brief": AudioFormat.BRIEF,
    "critique": AudioFormat.CRITIQUE,
    "debate": AudioFormat.DEBATE,
}
_AUDIO_LENGTHS = {
    "short": AudioLength.SHORT,
    "default": AudioLength.DEFAULT,
    "long": AudioLength.LONG,
}
_QUIZ_QUANTITIES = {member.name.lower(): member for member in QuizQuantity}
_QUIZ_DIFFICULTIES = {member.name.lower(): member for member in QuizDifficulty}
_INFOGRAPHIC_ORIENTATIONS = {
    "landscape": InfographicOrientation.LANDSCAPE,
    "portrait": InfographicOrientation.PORTRAIT,
    "square": InfographicOrientation.SQUARE,
}
_INFOGRAPHIC_DETAILS = {
    "concise": InfographicDetail.CONCISE,
    "standard": InfographicDetail.STANDARD,
    "detailed": InfographicDetail.DETAILED,
}
_INFOGRAPHIC_STYLES = {
    "auto_select": InfographicStyle.AUTO_SELECT,
    "sketch_note": InfographicStyle.SKETCH_NOTE,
    "professional": InfographicStyle.PROFESSIONAL,
    "bento_grid": InfographicStyle.BENTO_GRID,
    "editorial": InfographicStyle.EDITORIAL,
    "instructional": InfographicStyle.INSTRUCTIONAL,
    "bricks": InfographicStyle.BRICKS,
    "clay": InfographicStyle.CLAY,
    "anime": InfographicStyle.ANIME,
    "kawaii": InfographicStyle.KAWAII,
    "scientific": InfographicStyle.SCIENTIFIC,
}
_SLIDE_DECK_FORMATS = {
    "detailed_deck": SlideDeckFormat.DETAILED_DECK,
    "presenter_slides": SlideDeckFormat.PRESENTER_SLIDES,
}
_SLIDE_DECK_LENGTHS = {
    "default": SlideDeckLength.DEFAULT,
    "short": SlideDeckLength.SHORT,
}

InteractiveFamily = Literal["quiz", "flashcards"]


def _kickoff_payload(notebook_id: str, params: list[Any]) -> CodecPayload:
    """The guarded ``CREATE_ARTIFACT`` request every generate family dispatches."""
    return CodecPayload(
        params=params,
        source_path=f"/notebook/{notebook_id}",
        allow_null=True,
        raise_on_null_status=True,
    )


def _kickoff_status(
    result: object, *, operation: Operation, artifact_type: str
) -> GenerationStatusRecord:
    """Decode one ``CREATE_ARTIFACT`` kickoff; a null response or task id is unavailable."""
    method_id = RPCMethod.CREATE_ARTIFACT.value
    if result is None:
        raise artifact_feature_unavailable(operation, artifact_type, method_id=method_id)
    status = decode_generation_status(result)
    if status is None:
        raise artifact_feature_unavailable(operation, "artifact", method_id=method_id)
    return status


# --- kickoff payloads ------------------------------------------------------------


def encode_audio_generation(value: AudioGenerateInput) -> CodecPayload:
    """Payload for the ``artifact.generate_audio`` kickoff."""
    operation = Operation.ARTIFACT_GENERATE_AUDIO
    return _kickoff_payload(
        value.notebook_id,
        build_audio_artifact_params(
            value.notebook_id,
            list(value.source_ids),
            language=value.language,
            instructions=value.instructions,
            audio_format=wire_option(value.audio_format, _AUDIO_FORMATS, operation=operation),
            audio_length=wire_option(value.audio_length, _AUDIO_LENGTHS, operation=operation),
        ),
    )


def _interactive_payload(
    value: InteractiveGenerateInput, *, family: InteractiveFamily, operation: Operation
) -> CodecPayload:
    builder = build_quiz_artifact_params if family == "quiz" else build_flashcards_artifact_params
    return _kickoff_payload(
        value.notebook_id,
        builder(
            value.notebook_id,
            list(value.source_ids),
            instructions=value.instructions,
            quantity=wire_option(value.quantity, _QUIZ_QUANTITIES, operation=operation),
            difficulty=wire_option(value.difficulty, _QUIZ_DIFFICULTIES, operation=operation),
        ),
    )


def encode_quiz_generation(value: InteractiveGenerateInput) -> CodecPayload:
    """Payload for the ``artifact.generate_quiz`` kickoff."""
    return _interactive_payload(value, family="quiz", operation=Operation.ARTIFACT_GENERATE_QUIZ)


def encode_flashcards_generation(value: InteractiveGenerateInput) -> CodecPayload:
    """Payload for the ``artifact.generate_flashcards`` kickoff."""
    return _interactive_payload(
        value, family="flashcards", operation=Operation.ARTIFACT_GENERATE_FLASHCARDS
    )


def encode_infographic_generation(value: InfographicGenerateInput) -> CodecPayload:
    """Payload for the ``artifact.generate_infographic`` kickoff."""
    operation = Operation.ARTIFACT_GENERATE_INFOGRAPHIC
    return _kickoff_payload(
        value.notebook_id,
        build_infographic_artifact_params(
            value.notebook_id,
            list(value.source_ids),
            language=value.language,
            instructions=value.instructions,
            orientation=wire_option(
                value.orientation, _INFOGRAPHIC_ORIENTATIONS, operation=operation
            ),
            detail_level=wire_option(value.detail_level, _INFOGRAPHIC_DETAILS, operation=operation),
            style=wire_option(value.style, _INFOGRAPHIC_STYLES, operation=operation),
        ),
    )


def encode_slide_deck_generation(value: SlideDeckGenerateInput) -> CodecPayload:
    """Payload for the ``artifact.generate_slide_deck`` kickoff."""
    operation = Operation.ARTIFACT_GENERATE_SLIDE_DECK
    return _kickoff_payload(
        value.notebook_id,
        build_slide_deck_artifact_params(
            value.notebook_id,
            list(value.source_ids),
            language=value.language,
            instructions=value.instructions,
            slide_format=wire_option(value.slide_format, _SLIDE_DECK_FORMATS, operation=operation),
            slide_length=wire_option(value.slide_length, _SLIDE_DECK_LENGTHS, operation=operation),
        ),
    )


def encode_data_table_generation(value: DataTableGenerateInput) -> CodecPayload:
    """Payload for the ``artifact.generate_data_table`` kickoff."""
    return _kickoff_payload(
        value.notebook_id,
        build_data_table_artifact_params(
            value.notebook_id,
            list(value.source_ids),
            language=value.language,
            instructions=value.instructions,
        ),
    )


def encode_video_kickoff(value: VideoGenerateInput) -> CodecPayload:
    """Payload for the ``artifact.generate_video`` kickoff."""
    return _kickoff_payload(value.notebook_id, encode_video_generation(value))


def encode_report_kickoff(value: ReportGenerateInput) -> CodecPayload:
    """Payload for the ``artifact.generate_report`` kickoff."""
    return _kickoff_payload(value.notebook_id, encode_report_generation(value))


# --- kickoff decoding ----------------------------------------------------------


def decode_audio_generation(value: AudioGenerateInput, result: Any) -> AudioGenerateResult:
    del value
    return AudioGenerateResult(
        _kickoff_status(result, operation=Operation.ARTIFACT_GENERATE_AUDIO, artifact_type="audio")
    )


def decode_quiz_generation(
    value: InteractiveGenerateInput, result: Any
) -> InteractiveGenerateResult:
    del value
    return InteractiveGenerateResult(
        _kickoff_status(result, operation=Operation.ARTIFACT_GENERATE_QUIZ, artifact_type="quiz")
    )


def decode_flashcards_generation(
    value: InteractiveGenerateInput, result: Any
) -> InteractiveGenerateResult:
    del value
    return InteractiveGenerateResult(
        _kickoff_status(
            result,
            operation=Operation.ARTIFACT_GENERATE_FLASHCARDS,
            artifact_type="flashcards",
        )
    )


def decode_infographic_generation(
    value: InfographicGenerateInput, result: Any
) -> VisualGenerateResult:
    del value
    return VisualGenerateResult(
        _kickoff_status(
            result,
            operation=Operation.ARTIFACT_GENERATE_INFOGRAPHIC,
            artifact_type="infographic",
        )
    )


def decode_slide_deck_generation(
    value: SlideDeckGenerateInput, result: Any
) -> VisualGenerateResult:
    del value
    return VisualGenerateResult(
        _kickoff_status(
            result,
            operation=Operation.ARTIFACT_GENERATE_SLIDE_DECK,
            artifact_type="slide deck",
        )
    )


def decode_data_table_generation(
    value: DataTableGenerateInput, result: Any
) -> DataTableGenerateResult:
    del value
    return DataTableGenerateResult(
        _kickoff_status(
            result,
            operation=Operation.ARTIFACT_GENERATE_DATA_TABLE,
            artifact_type="data table",
        )
    )


def decode_report_generation(value: ReportGenerateInput, result: Any) -> ReportGenerateResult:
    del value
    return ReportGenerateResult(
        _kickoff_status(
            result, operation=Operation.ARTIFACT_GENERATE_REPORT, artifact_type="report"
        )
    )


def decode_video_generation_kickoff(value: VideoGenerateInput, result: Any) -> VideoGenerateResult:
    """The cinematic route names its own artifact type in the unavailable error."""
    return VideoGenerateResult(
        _kickoff_status(
            result,
            operation=Operation.ARTIFACT_GENERATE_VIDEO,
            artifact_type="cinematic video" if value.cinematic_route else "video",
        )
    )


__all__ = [
    "InteractiveFamily",
    "decode_audio_generation",
    "decode_data_table_generation",
    "decode_flashcards_generation",
    "decode_infographic_generation",
    "decode_quiz_generation",
    "decode_report_generation",
    "decode_slide_deck_generation",
    "decode_video_generation_kickoff",
    "encode_audio_generation",
    "encode_data_table_generation",
    "encode_flashcards_generation",
    "encode_infographic_generation",
    "encode_quiz_generation",
    "encode_report_kickoff",
    "encode_slide_deck_generation",
    "encode_video_kickoff",
]
