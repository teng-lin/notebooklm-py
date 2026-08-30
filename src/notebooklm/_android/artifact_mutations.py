"""Web-derived artifact mutations supported by the Android mobile backend."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit

from .._artifact import validation as _artifact_validation
from .._idempotency import mark_unconfirmed
from .._types.artifacts import _status_from_code
from .._types.enums import ExportType
from ..exceptions import (
    ArtifactFeatureUnavailableError,
    DecodingError,
    RPCError,
    ValidationError,
)
from ..types import Artifact, GenerationStatus
from .artifact_proto import ARTIFACTS_PROTO as _PROTO
from .artifact_proto import empty_response_type
from .codecs.artifacts import decode_artifact
from .session import AndroidSession
from .write_safety import call_unconfirmed_on_transport_loss

_SERVICE = "google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService"
GENERATE_ARTIFACT_METHOD = f"/{_SERVICE}/GenerateArtifact"
EXPORT_TO_DRIVE_METHOD = f"/{_SERVICE}/ExportToDrive"


def android_request_context() -> Any:
    from .upload import android_request_context as build_request_context

    return build_request_context()


async def retry_failed_artifact(
    session: AndroidSession,
    require_owned: Callable[..., Awaitable[None]],
    notebook_id: str,
    artifact_id: str,
) -> GenerationStatus:
    async with session.operation_scope("artifacts.retry_failed") as lease:
        await require_owned(
            notebook_id,
            artifact_id,
            expected_epoch=lease.epoch,
            method_id=GENERATE_ARTIFACT_METHOD,
        )
        response = await call_unconfirmed_on_transport_loss(
            lambda: session.unary(
                GENERATE_ARTIFACT_METHOD,
                _PROTO.GenerateArtifactRequest(
                    request_context=android_request_context(),
                    artifact_id=artifact_id,
                ),
                replay_safe=False,
                response_type=_PROTO.GenerateArtifactResponse,
                expected_epoch=lease.epoch,
            )
        )
    try:
        if not response.HasField("artifact") or not response.artifact.artifact_id:
            raise ArtifactFeatureUnavailableError("artifact", method_id=GENERATE_ARTIFACT_METHOD)
        artifact = decode_artifact(response.artifact, method_id=GENERATE_ARTIFACT_METHOD)
        if artifact.id != artifact_id:
            raise DecodingError(
                "Android artifact retry returned a different artifact id.",
                method_id=GENERATE_ARTIFACT_METHOD,
            )
    except (ArtifactFeatureUnavailableError, DecodingError) as error:
        raise mark_unconfirmed(error) from None
    return GenerationStatus(
        task_id=artifact.id,
        status=_status_from_code(artifact.status),
        url=artifact.url,
    )


async def delete_artifact(
    session: AndroidSession,
    list_studio: Callable[..., Awaitable[list[Artifact]]],
    notebook_id: str,
    artifact_id: str,
    *,
    method: str,
) -> None:
    """Delete only after proving the global artifact id belongs to the notebook."""

    async with session.operation_scope("artifacts.delete") as lease:
        artifacts = await list_studio(notebook_id, expected_epoch=lease.epoch)
        if not any(artifact.id == artifact_id for artifact in artifacts):
            return
        try:
            await session.unary(
                method,
                _PROTO.DeleteArtifactRequest(artifact_id=artifact_id),
                replay_safe=False,
                response_type=empty_response_type(),
                expected_epoch=lease.epoch,
            )
        except RPCError as error:
            if error.rpc_code != 5:
                raise


async def export_to_drive(
    session: AndroidSession,
    require_owned: Callable[..., Awaitable[None]],
    notebook_id: str,
    *,
    artifact_id: str | None,
    content: str | None,
    title: str,
    export_type: ExportType,
) -> Any:
    _artifact_validation.check_exactly_one_export_target(artifact_id, content)
    if not isinstance(title, str):
        raise ValidationError("title must be a string")
    if not isinstance(export_type, ExportType):
        raise ValidationError("export_type must be an ExportType value")
    request = _PROTO.ExportToDriveRequest(
        request_context=android_request_context(),
        title=title,
        destination=int(export_type.value),
    )
    if artifact_id is not None:
        request.artifact_id = artifact_id
    else:
        request.content = content
    async with session.operation_scope("artifacts.export") as lease:
        if artifact_id is not None:
            await require_owned(
                notebook_id,
                artifact_id,
                expected_epoch=lease.epoch,
                method_id=EXPORT_TO_DRIVE_METHOD,
            )
        response = await call_unconfirmed_on_transport_loss(
            lambda: session.unary(
                EXPORT_TO_DRIVE_METHOD,
                request,
                replay_safe=False,
                response_type=_PROTO.ExportToDriveResponse,
                expected_epoch=lease.epoch,
            )
        )
    parsed = urlsplit(response.url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise mark_unconfirmed(
            DecodingError(
                "Android ExportToDrive response omitted a valid HTTPS URL.",
                method_id=EXPORT_TO_DRIVE_METHOD,
            )
        )
    return response.url


__all__ = [
    "EXPORT_TO_DRIVE_METHOD",
    "GENERATE_ARTIFACT_METHOD",
    "delete_artifact",
    "export_to_drive",
    "retry_failed_artifact",
]
