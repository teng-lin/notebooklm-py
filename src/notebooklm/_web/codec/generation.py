"""Row-facing request payloads and kickoff decoding for the Studio generate families.

P9.4b: the option vocabularies and payload assembly the P5 handlers carried on
the chain live here, behind ``encode_*`` functions that return the full
:class:`CodecPayload` one ``CREATE_ARTIFACT`` kickoff dispatches (params plus the
notebook route and the guarded ``allow_null``/``raise_on_null_status`` options)
without naming a method — the row's ``NativeCallSpec`` is the sole method
authority.  Option validation raises the same ``BackendContractError`` messages
the handlers raised, before any payload is assembled, so a rejected input never
reaches the transport.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, TypeVar, cast

from ..._artifact.payloads import (
    build_audio_artifact_params,
    build_data_table_artifact_params,
    build_flashcards_artifact_params,
    build_infographic_artifact_params,
    build_quiz_artifact_params,
    build_slide_deck_artifact_params,
)
from ..._backend import BackendContractError
from ..._binding import CodecPayload
from ..._operations import Operation
from ..._records import (
    AudioGenerateInput,
    DataTableGenerateInput,
    GenerationStatusRecord,
    InfographicGenerateInput,
    InteractiveGenerateInput,
    ReportGenerateInput,
    SlideDeckGenerateInput,
    VideoGenerateInput,
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
from .studio_documents import (
    artifact_feature_unavailable,
    decode_generation_status,
    encode_report_generation,
    encode_video_generation,
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
_InteractiveOptionT = TypeVar("_InteractiveOptionT", QuizQuantity, QuizDifficulty)
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


# --- option validation (runs before any native call) ------------------------


def validate_audio_options(value: AudioGenerateInput) -> None:
    """Reject unreviewed audio options with the handler's exact contract errors."""
    if value.audio_format is not None and value.audio_format not in _AUDIO_FORMATS:
        raise BackendContractError(
            f"unrecognized audio format {value.audio_format!r}",
            operation=Operation.ARTIFACT_GENERATE_AUDIO,
        )
    if value.audio_length is not None and value.audio_length not in _AUDIO_LENGTHS:
        raise BackendContractError(
            f"unrecognized audio length {value.audio_length!r}",
            operation=Operation.ARTIFACT_GENERATE_AUDIO,
        )


def _interactive_option(
    value: str | None,
    options: Mapping[str, _InteractiveOptionT],
    *,
    parameter: str,
    operation: Operation,
) -> _InteractiveOptionT | None:
    if value is None:
        return None
    option = options.get(value)
    if option is None:
        raise BackendContractError(
            f"unrecognized interactive {parameter} {value!r}",
            operation=operation,
        )
    return option


def validate_interactive_options(
    value: InteractiveGenerateInput, *, operation: Operation
) -> tuple[QuizQuantity | None, QuizDifficulty | None]:
    """Resolve quiz/flashcard options, rejecting unreviewed values first."""
    quantity = _interactive_option(
        value.quantity, _QUIZ_QUANTITIES, parameter="quantity", operation=operation
    )
    difficulty = _interactive_option(
        value.difficulty, _QUIZ_DIFFICULTIES, parameter="difficulty", operation=operation
    )
    return quantity, difficulty


def _visual_option(
    value: str | None,
    options: Mapping[str, object],
    *,
    parameter: str,
    operation: Operation,
) -> object | None:
    if value is None:
        return None
    option = options.get(value)
    if option is None:
        raise BackendContractError(
            f"unrecognized visual {parameter} {value!r}",
            operation=operation,
        )
    return option


def validate_infographic_options(
    value: InfographicGenerateInput,
) -> tuple[InfographicOrientation | None, InfographicDetail | None, InfographicStyle | None]:
    operation = Operation.ARTIFACT_GENERATE_INFOGRAPHIC
    orientation = _visual_option(
        value.orientation, _INFOGRAPHIC_ORIENTATIONS, parameter="orientation", operation=operation
    )
    detail_level = _visual_option(
        value.detail_level, _INFOGRAPHIC_DETAILS, parameter="detail level", operation=operation
    )
    style = _visual_option(value.style, _INFOGRAPHIC_STYLES, parameter="style", operation=operation)
    return (
        cast(InfographicOrientation | None, orientation),
        cast(InfographicDetail | None, detail_level),
        cast(InfographicStyle | None, style),
    )


def validate_slide_deck_options(
    value: SlideDeckGenerateInput,
) -> tuple[SlideDeckFormat | None, SlideDeckLength | None]:
    operation = Operation.ARTIFACT_GENERATE_SLIDE_DECK
    slide_format = _visual_option(
        value.slide_format, _SLIDE_DECK_FORMATS, parameter="format", operation=operation
    )
    slide_length = _visual_option(
        value.slide_length, _SLIDE_DECK_LENGTHS, parameter="length", operation=operation
    )
    return cast(SlideDeckFormat | None, slide_format), cast(SlideDeckLength | None, slide_length)


# --- kickoff payloads ------------------------------------------------------------


def encode_audio_generation(
    value: AudioGenerateInput, *, source_ids: tuple[str, ...], language: str
) -> CodecPayload:
    """Payload for the ``artifact.generate_audio`` kickoff (options already validated)."""
    return _kickoff_payload(
        value.notebook_id,
        build_audio_artifact_params(
            value.notebook_id,
            list(source_ids),
            language=language,
            instructions=value.instructions,
            audio_format=(
                None if value.audio_format is None else _AUDIO_FORMATS[value.audio_format]
            ),
            audio_length=(
                None if value.audio_length is None else _AUDIO_LENGTHS[value.audio_length]
            ),
        ),
    )


def encode_interactive_generation(
    value: InteractiveGenerateInput,
    *,
    family: InteractiveFamily,
    source_ids: tuple[str, ...],
    quantity: QuizQuantity | None,
    difficulty: QuizDifficulty | None,
) -> CodecPayload:
    """Payload for the quiz or flashcard kickoff (options already validated)."""
    builder = build_quiz_artifact_params if family == "quiz" else build_flashcards_artifact_params
    return _kickoff_payload(
        value.notebook_id,
        builder(
            value.notebook_id,
            list(source_ids),
            instructions=value.instructions,
            quantity=quantity,
            difficulty=difficulty,
        ),
    )


def encode_infographic_generation(
    value: InfographicGenerateInput,
    *,
    source_ids: tuple[str, ...],
    language: str,
    orientation: InfographicOrientation | None,
    detail_level: InfographicDetail | None,
    style: InfographicStyle | None,
) -> CodecPayload:
    return _kickoff_payload(
        value.notebook_id,
        build_infographic_artifact_params(
            value.notebook_id,
            list(source_ids),
            language=language,
            instructions=value.instructions,
            orientation=orientation,
            detail_level=detail_level,
            style=style,
        ),
    )


def encode_slide_deck_generation(
    value: SlideDeckGenerateInput,
    *,
    source_ids: tuple[str, ...],
    language: str,
    slide_format: SlideDeckFormat | None,
    slide_length: SlideDeckLength | None,
) -> CodecPayload:
    return _kickoff_payload(
        value.notebook_id,
        build_slide_deck_artifact_params(
            value.notebook_id,
            list(source_ids),
            language=language,
            instructions=value.instructions,
            slide_format=slide_format,
            slide_length=slide_length,
        ),
    )


def encode_data_table_generation(
    value: DataTableGenerateInput, *, source_ids: tuple[str, ...], language: str
) -> CodecPayload:
    return _kickoff_payload(
        value.notebook_id,
        build_data_table_artifact_params(
            value.notebook_id,
            list(source_ids),
            language=language,
            instructions=value.instructions,
        ),
    )


def encode_video_kickoff(
    value: VideoGenerateInput, *, source_ids: tuple[str, ...], language: str
) -> CodecPayload:
    """Payload for the video kickoff; an unreviewed option is the handler's contract error."""
    try:
        params = encode_video_generation(value, source_ids=source_ids, language=language)
    except KeyError as exc:
        raise BackendContractError(
            f"unrecognized video option {exc.args[0]!r}",
            operation=Operation.ARTIFACT_GENERATE_VIDEO,
        ) from None
    return _kickoff_payload(value.notebook_id, params)


def encode_report_kickoff(
    value: ReportGenerateInput, *, source_ids: tuple[str, ...], language: str
) -> CodecPayload:
    """Payload for the report kickoff; an unreviewed format is the handler's contract error."""
    try:
        params = encode_report_generation(value, source_ids=source_ids, language=language)
    except KeyError as exc:
        raise BackendContractError(
            f"unrecognized report format {exc.args[0]!r}",
            operation=Operation.ARTIFACT_GENERATE_REPORT,
        ) from None
    return _kickoff_payload(value.notebook_id, params)


# --- kickoff decoding ----------------------------------------------------------


def decode_generation_kickoff(
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


__all__ = [
    "InteractiveFamily",
    "decode_generation_kickoff",
    "encode_audio_generation",
    "encode_data_table_generation",
    "encode_infographic_generation",
    "encode_interactive_generation",
    "encode_report_kickoff",
    "encode_slide_deck_generation",
    "encode_video_kickoff",
    "validate_audio_options",
    "validate_infographic_options",
    "validate_interactive_options",
    "validate_slide_deck_options",
]
