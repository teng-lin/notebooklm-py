"""Web payload codecs for report and Video Overview generation."""

from __future__ import annotations

import types  # ``from types import …`` reads as a public-model import to the P3 guardrail
from collections.abc import Mapping
from typing import Any, TypeVar, cast

from ..._backend import BackendContractError, BackendError, BackendErrorReason
from ..._binding import CodecPayload
from ..._operations import Operation
from ..._records import (
    ArtifactRetryInput,
    ArtifactRetryResult,
    ArtifactReviseSlideInput,
    ArtifactReviseSlideResult,
    GenerationStatusRecord,
    ReportGenerateInput,
    VideoGenerateInput,
)
from ...exceptions import DecodingError
from ...rpc import ReportFormat, RPCMethod, VideoFormat, VideoStyle, safe_index
from ...rpc.types import artifact_status_to_str
from .artifact_payloads import (
    build_cinematic_video_artifact_params,
    build_report_artifact_params,
    build_retry_artifact_params,
    build_revise_slide_params,
    build_video_artifact_params,
)

_VIDEO_FORMATS = {
    "explainer": VideoFormat.EXPLAINER,
    "brief": VideoFormat.BRIEF,
    "cinematic": VideoFormat.CINEMATIC,
    "short": VideoFormat.SHORT,
}
_VIDEO_STYLES = {
    "auto_select": VideoStyle.AUTO_SELECT,
    "custom": VideoStyle.CUSTOM,
    "classic": VideoStyle.CLASSIC,
    "whiteboard": VideoStyle.WHITEBOARD,
    "kawaii": VideoStyle.KAWAII,
    "anime": VideoStyle.ANIME,
    "watercolor": VideoStyle.WATERCOLOR,
    "retro_print": VideoStyle.RETRO_PRINT,
    "heritage": VideoStyle.HERITAGE,
    "paper_craft": VideoStyle.PAPER_CRAFT,
}
_REPORT_FORMATS = {value.value: value for value in ReportFormat}

_OptionT = TypeVar("_OptionT")


def wire_option(
    value: str | None,
    options: Mapping[str, _OptionT],
    *,
    operation: Operation,
) -> _OptionT | None:
    """Map one already-validated option string onto its wire enum.

    Since P10 R5.1a the reviewed vocabulary is a service concern (ADR-0035
    addendum D1(a)), so a value missing here means an *unresolved* input reached
    the port — a contract breach, and named as one rather than as the
    user-facing rejection ``_studio/generation.py`` raises.
    """
    if value is None:
        return None
    option = options.get(value)
    if option is None:
        raise BackendContractError(
            f"unresolved generation input: {value!r} is not a reviewed option",
            operation=operation,
        )
    return option


def encode_video_generation(value: VideoGenerateInput) -> list[Any]:
    """Encode one pre-resolved video input into the exact CREATE_ARTIFACT payload."""

    operation = Operation.ARTIFACT_GENERATE_VIDEO
    video_format = wire_option(value.video_format, _VIDEO_FORMATS, operation=operation)
    if value.cinematic_route:
        return build_cinematic_video_artifact_params(
            value.notebook_id,
            list(value.source_ids),
            language=value.language,
            instructions=value.instructions,
        )
    return build_video_artifact_params(
        value.notebook_id,
        list(value.source_ids),
        language=value.language,
        instructions=value.instructions,
        video_format=video_format,
        video_style=wire_option(value.video_style, _VIDEO_STYLES, operation=operation),
        style_prompt=value.style_prompt,
    )


def encode_report_generation(value: ReportGenerateInput) -> list[Any]:
    """Encode one pre-resolved report input into the exact CREATE_ARTIFACT payload."""

    report_format = wire_option(
        value.report_format, _REPORT_FORMATS, operation=Operation.ARTIFACT_GENERATE_REPORT
    )
    if report_format is None:  # pragma: no cover - the field is never ``None``
        raise BackendContractError(
            "unresolved generation input: report format is required",
            operation=Operation.ARTIFACT_GENERATE_REPORT,
        )
    return build_report_artifact_params(
        value.notebook_id,
        list(value.source_ids),
        report_format=report_format,
        language=value.language,
        custom_prompt=value.custom_prompt,
        extra_instructions=value.extra_instructions,
    )


def decode_generation_status(
    result: Any,
    *,
    method_id: str = RPCMethod.CREATE_ARTIFACT.value,
    source: str = "_parse_generation_result",
) -> GenerationStatusRecord | None:
    """Decode the common CREATE_ARTIFACT task row; ``None`` means a null task id."""

    artifact_id = safe_index(
        result,
        0,
        0,
        method_id=method_id,
        source=source,
    )
    if artifact_id is None:
        return None
    if not artifact_id:
        raise DecodingError(
            f"No artifact id (source={source})",
            method_id=method_id,
        )
    status_code = safe_index(
        result,
        0,
        4,
        method_id=method_id,
        source=source,
    )
    status = "pending" if status_code is None else artifact_status_to_str(status_code)
    return GenerationStatusRecord(task_id=cast(str, artifact_id), status=status)


# --- P9.3 Studio codec rows ----------------------------------------------------
# Row-facing payload builders and decoders for the slide-revision and retry
# kickoffs behind ``_web/bindings/studio.py``; neither names a method.


def artifact_feature_unavailable(
    operation: Operation,
    artifact_type: str,
    *,
    method_id: str,
) -> BackendError:
    """The closed ``ARTIFACT_FEATURE_UNAVAILABLE`` error for a null kickoff response."""

    return BackendError(
        message=f"{artifact_type.capitalize()} generation is unavailable",
        operation=operation,
        diagnostics=types.MappingProxyType(
            {
                "artifact_type": artifact_type,
                "method_id": method_id,
                "raw_response": None,
            }
        ),
        reason=BackendErrorReason.ARTIFACT_FEATURE_UNAVAILABLE,
    )


def encode_artifact_revise_slide(value: ArtifactReviseSlideInput) -> CodecPayload:
    """Payload for the ``artifact.revise_slide`` codec row."""

    return CodecPayload(
        params=build_revise_slide_params(value.artifact_id, value.slide_index, value.prompt),
        source_path=f"/notebook/{value.notebook_id}",
        allow_null=True,
        raise_on_null_status=True,
    )


def decode_artifact_revise_slide(
    value: ArtifactReviseSlideInput, result: object
) -> ArtifactReviseSlideResult:
    """Row decoder for ``artifact.revise_slide``; a null status is the closed unavailable error."""

    del value
    method_id = RPCMethod.REVISE_SLIDE.value
    status = decode_generation_status(result, method_id=method_id) if result is not None else None
    if status is None:
        raise artifact_feature_unavailable(
            Operation.ARTIFACT_REVISE_SLIDE,
            "slide revision",
            method_id=method_id,
        )
    return ArtifactReviseSlideResult(status)


def encode_artifact_retry(value: ArtifactRetryInput) -> CodecPayload:
    """Payload for the ``artifact.retry`` codec row."""

    return CodecPayload(
        params=build_retry_artifact_params(value.artifact_id),
        source_path=f"/notebook/{value.notebook_id}",
        allow_null=True,
        raise_on_null_status=True,
    )


def decode_artifact_retry(value: ArtifactRetryInput, result: object) -> ArtifactRetryResult:
    """Row decoder for ``artifact.retry``; a null status is the closed unavailable error."""

    del value
    method_id = RPCMethod.RETRY_ARTIFACT.value
    status = decode_generation_status(result, method_id=method_id) if result is not None else None
    if status is None:
        raise artifact_feature_unavailable(
            Operation.ARTIFACT_RETRY,
            "retry",
            method_id=method_id,
        )
    return ArtifactRetryResult(status)


__all__ = [
    "artifact_feature_unavailable",
    "decode_artifact_retry",
    "decode_artifact_revise_slide",
    "decode_generation_status",
    "encode_artifact_retry",
    "encode_artifact_revise_slide",
    "encode_report_generation",
    "encode_video_generation",
    "wire_option",
]
