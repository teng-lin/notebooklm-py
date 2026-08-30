"""Notebook-scoped exact reads and selection for Android Studio artifacts."""

from __future__ import annotations

import builtins
from typing import Any, Protocol, cast

from .._types.enums import ArtifactStatus, ArtifactTypeCode
from ..exceptions import (
    ArtifactNotFoundError,
    ArtifactNotReadyError,
    DecodingError,
    RPCError,
)
from ..types import Artifact, ArtifactType
from .artifact_outputs import decode_prefetched_artifacts
from .artifact_proto import ARTIFACTS_PROTO as _PROTO
from .codecs.artifacts import decode_artifact
from .errors import sanitize_escaping_exception
from .session import AndroidSession

_SERVICE = "google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService"
LIST_ARTIFACTS_METHOD = f"/{_SERVICE}/ListArtifacts"
GET_ARTIFACT_METHOD = f"/{_SERVICE}/GetArtifact"


class _StudioArtifactLister(Protocol):
    async def _list_all_studio(
        self,
        notebook_id: str,
        *,
        expected_epoch: int | None = None,
    ) -> builtins.list[Artifact]: ...


class AndroidArtifactReadMixin:
    """Exact-id reads fenced by a notebook-scoped artifact listing."""

    _transport: AndroidSession

    async def _require_studio_artifact_owned(
        self,
        notebook_id: str,
        artifact_id: str,
        *,
        expected_epoch: int,
        method_id: str,
    ) -> None:
        """Prove ownership without decoding unrelated artifact payloads."""

        response = await self._transport.unary(
            LIST_ARTIFACTS_METHOD,
            _PROTO.ListArtifactsRequest(project_id=notebook_id),
            replay_safe=True,
            response_type=_PROTO.ListArtifactsResponse,
            expected_epoch=expected_epoch,
        )
        if not any(row.artifact_id == artifact_id for row in response.artifacts):
            raise ArtifactNotFoundError(artifact_id, method_id=method_id)

    async def _get_studio_artifact(
        self,
        notebook_id: str,
        artifact_id: str,
        *,
        expected_epoch: int,
    ) -> Artifact | None:
        """Decode one exact ``GetArtifact`` response; ``NOT_FOUND`` is absence."""

        adapter = self
        result: Artifact | None = None
        failure: BaseException | None = None
        response: Any | None = None
        raw_artifact: Any | None = None
        try:
            await adapter._require_studio_artifact_owned(
                notebook_id,
                artifact_id,
                expected_epoch=expected_epoch,
                method_id=GET_ARTIFACT_METHOD,
            )
            response = await adapter._transport.unary(
                GET_ARTIFACT_METHOD,
                _PROTO.GetArtifactRequest(artifact_id=artifact_id),
                replay_safe=True,
                response_type=_PROTO.GetArtifactResponse,
                expected_epoch=expected_epoch,
            )
            assert response is not None
            if not response.HasField("artifact"):
                raise DecodingError(
                    "Android GetArtifact response omitted its artifact.",
                    method_id=GET_ARTIFACT_METHOD,
                )
            raw_artifact = response.artifact
            result = decode_artifact(raw_artifact, method_id=GET_ARTIFACT_METHOD)
            if result.id != artifact_id:
                raise DecodingError(
                    "Android GetArtifact response returned a different artifact id.",
                    method_id=GET_ARTIFACT_METHOD,
                )
        except RPCError as error:
            if error.rpc_code != 5:
                failure = sanitize_escaping_exception(error)
        except BaseException as error:
            failure = sanitize_escaping_exception(error)
        finally:
            if failure is not None:
                result = None
            del raw_artifact, response, self, adapter
        if failure is not None:
            failure.__cause__ = None
            failure.__context__ = None
            raise failure from None
        return result

    async def _get_raw_studio_artifact(
        self,
        notebook_id: str,
        artifact_id: str,
        *,
        expected_epoch: int,
    ) -> Any:
        """Return a detached exact protobuf for representation-only fields."""

        await self._require_studio_artifact_owned(
            notebook_id,
            artifact_id,
            expected_epoch=expected_epoch,
            method_id=GET_ARTIFACT_METHOD,
        )
        response = await self._transport.unary(
            GET_ARTIFACT_METHOD,
            _PROTO.GetArtifactRequest(artifact_id=artifact_id),
            replay_safe=True,
            response_type=_PROTO.GetArtifactResponse,
            expected_epoch=expected_epoch,
        )
        if not response.HasField("artifact"):
            raise DecodingError(
                "Android GetArtifact response omitted its artifact.",
                method_id=GET_ARTIFACT_METHOD,
            )
        if response.artifact.artifact_id != artifact_id:
            raise DecodingError(
                "Android GetArtifact response returned a different artifact id.",
                method_id=GET_ARTIFACT_METHOD,
            )
        artifact = _PROTO.Artifact()
        artifact.CopyFrom(response.artifact)
        return artifact

    async def _select_completed_studio_at_epoch(
        self,
        notebook_id: str,
        artifact_id: str | None,
        *,
        type_code: ArtifactTypeCode,
        artifact_type: str,
        kind: ArtifactType | None = None,
        expected_epoch: int,
        prefetched: builtins.list[Any] | None,
    ) -> Artifact:
        owner = cast(_StudioArtifactLister, self)
        candidates = (
            decode_prefetched_artifacts(prefetched, method_id=LIST_ARTIFACTS_METHOD)
            if prefetched is not None
            else (await owner._list_all_studio(notebook_id, expected_epoch=expected_epoch))
        )
        completed = [
            item
            for item in candidates
            if item._artifact_type == type_code.value
            and item.status == ArtifactStatus.COMPLETED.value
            and (kind is None or item.kind is kind)
        ]
        if artifact_id is not None:
            selected = next((item for item in completed if item.id == artifact_id), None)
        else:
            selected = max(
                completed,
                key=lambda item: (
                    item.last_modified_at.timestamp() if item.last_modified_at is not None else 0
                ),
                default=None,
            )
        if selected is None:
            raise ArtifactNotReadyError(artifact_type, artifact_id=artifact_id)
        if prefetched is not None:
            # A caller-supplied optimization hint is not an ownership capability.
            await self._require_studio_artifact_owned(
                notebook_id,
                selected.id,
                expected_epoch=expected_epoch,
                method_id=LIST_ARTIFACTS_METHOD,
            )
        return selected


__all__ = [
    "AndroidArtifactReadMixin",
    "GET_ARTIFACT_METHOD",
    "LIST_ARTIFACTS_METHOD",
]
