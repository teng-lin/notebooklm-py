"""Private artifact listing and selection helpers."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import httpx

from .rpc import ArtifactStatus, ArtifactTypeCode, RPCError, RPCMethod
from .types import Artifact, ArtifactNotReadyError, ArtifactType

logger = logging.getLogger(__name__)

RpcCall = Callable[..., Awaitable[Any]]
ListRawCallback = Callable[[str], Awaitable[list[Any]]]
ListMindMapsCallback = Callable[[str], Awaitable[list[Any]]]
ListArtifactsCallback = Callable[[str], Awaitable[list[Artifact]]]


class ArtifactListingService:
    """List, filter, and select artifacts without depending on the facade."""

    async def list_raw(self, notebook_id: str, *, rpc_call: RpcCall) -> list[Any]:
        """Get raw studio artifact rows from NotebookLM."""
        params = [[2], notebook_id, 'NOT artifact.status = "ARTIFACT_STATUS_SUGGESTED"']
        result = await rpc_call(
            RPCMethod.LIST_ARTIFACTS,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )
        if (
            isinstance(result, list)
            and len(result) == 1
            and isinstance(result[0], list)
            and (not result[0] or isinstance(result[0][0], list))
        ):
            return result[0]
        if isinstance(result, list):
            return result
        return []

    async def list_artifacts(
        self,
        notebook_id: str,
        artifact_type: ArtifactType | None,
        *,
        list_raw: ListRawCallback,
        list_mind_maps: ListMindMapsCallback,
    ) -> list[Artifact]:
        """List public artifacts from studio rows plus mind-map rows."""
        artifacts = self._filter_studio_artifacts(await list_raw(notebook_id), artifact_type)

        if artifact_type is None or artifact_type == ArtifactType.MIND_MAP:
            try:
                artifacts.extend(
                    self._filter_mind_map_artifacts(
                        await list_mind_maps(notebook_id),
                        artifact_type,
                    )
                )
            except (RPCError, httpx.HTTPError) as e:
                # Network/API errors - log and continue with studio artifacts.
                # This ensures users can see audio/video/reports even if the
                # mind-map endpoint is temporarily unavailable.
                logger.warning("Failed to fetch mind maps: %s", e)

        return artifacts

    async def get(
        self,
        notebook_id: str,
        artifact_id: str,
        *,
        list_artifacts: ListArtifactsCallback,
    ) -> Artifact | None:
        """Get a public artifact by ID from the public artifact listing."""
        artifacts = await list_artifacts(notebook_id)
        for artifact in artifacts:
            if artifact.id == artifact_id:
                return artifact
        return None

    def select_artifact(
        self,
        candidates: Sequence[Any],
        artifact_id: str | None,
        type_name: str,
        no_result_error_key: str,
        *,
        type_code: ArtifactTypeCode,
    ) -> Any:
        """Select an artifact from candidates by ID or return latest completed.

        The error-key asymmetry is intentional: explicit-ID misses derive the
        key from ``type_name`` while empty-filter results use
        ``no_result_error_key`` verbatim.
        """
        filtered = [
            a
            for a in candidates
            if isinstance(a, list)
            and len(a) > 4
            and a[2] == type_code
            and a[4] == ArtifactStatus.COMPLETED
        ]

        if artifact_id:
            artifact = next((a for a in filtered if a[0] == artifact_id), None)
            if not artifact:
                raise ArtifactNotReadyError(
                    type_name.lower().replace(" ", "_"), artifact_id=artifact_id
                )
            return artifact

        if not filtered:
            raise ArtifactNotReadyError(no_result_error_key)

        filtered.sort(
            key=lambda a: (
                (a[15][0] or 0) if len(a) > 15 and isinstance(a[15], list) and a[15] else 0
            ),
            reverse=True,
        )
        return filtered[0]

    def _filter_studio_artifacts(
        self,
        artifacts_data: Sequence[Any],
        artifact_type: ArtifactType | None,
    ) -> list[Artifact]:
        artifacts: list[Artifact] = []
        for art_data in artifacts_data:
            if isinstance(art_data, list) and len(art_data) > 0:
                artifact = Artifact.from_api_response(art_data)
                if artifact_type is None or artifact.kind == artifact_type:
                    artifacts.append(artifact)
        return artifacts

    def _filter_mind_map_artifacts(
        self,
        mind_maps: Sequence[Any],
        artifact_type: ArtifactType | None,
    ) -> list[Artifact]:
        artifacts: list[Artifact] = []
        for mm_data in mind_maps:
            if isinstance(mm_data, list):
                mind_map_artifact = Artifact.from_mind_map(mm_data)
                if mind_map_artifact is not None:
                    if artifact_type is None or mind_map_artifact.kind == artifact_type:
                        artifacts.append(mind_map_artifact)
        return artifacts
