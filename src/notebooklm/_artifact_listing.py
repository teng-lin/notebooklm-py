"""Private artifact listing and selection helpers."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import httpx

from ._row_adapters import ArtifactRow
from .rpc import ArtifactTypeCode, RPCError, RPCMethod
from .types import Artifact, ArtifactNotReadyError, ArtifactType

logger = logging.getLogger(__name__)

RpcCall = Callable[..., Awaitable[Any]]
ListRawCallback = Callable[[str], Awaitable[list[Any]]]
ListMindMapsCallback = Callable[[str], Awaitable[list[Any]]]
ListArtifactsCallback = Callable[[str], Awaitable[list[Artifact]]]

_ARTIFACT_TYPE_CODES_BY_KIND = {
    ArtifactType.AUDIO: ArtifactTypeCode.AUDIO.value,
    ArtifactType.REPORT: ArtifactTypeCode.REPORT.value,
    ArtifactType.VIDEO: ArtifactTypeCode.VIDEO.value,
    ArtifactType.MIND_MAP: ArtifactTypeCode.MIND_MAP.value,
    ArtifactType.INFOGRAPHIC: ArtifactTypeCode.INFOGRAPHIC.value,
    ArtifactType.SLIDE_DECK: ArtifactTypeCode.SLIDE_DECK.value,
    ArtifactType.DATA_TABLE: ArtifactTypeCode.DATA_TABLE.value,
}
_KNOWN_ARTIFACT_TYPE_CODES = frozenset(_ARTIFACT_TYPE_CODES_BY_KIND.values())


def iter_artifact_rows(candidates: Sequence[Any]) -> list[ArtifactRow]:
    """Wrap raw list-shaped artifact candidates in ``ArtifactRow`` adapters."""
    return [ArtifactRow(candidate) for candidate in candidates if isinstance(candidate, list)]


def find_artifact_row_by_id(candidates: Sequence[Any], artifact_id: str) -> ArtifactRow | None:
    """Find any artifact row by ID without filtering by completion status."""
    for row in iter_artifact_rows(candidates):
        if row.id == artifact_id:
            return row
    return None


def _matches_artifact_type(artifact: Artifact, artifact_type: ArtifactType | None) -> bool:
    """Return whether ``artifact`` matches ``artifact_type`` without noisy kind warnings."""
    if artifact_type is None:
        return True

    if artifact_type == ArtifactType.QUIZ:
        return artifact._artifact_type == ArtifactTypeCode.QUIZ.value and artifact._variant == 2
    if artifact_type == ArtifactType.FLASHCARDS:
        return artifact._artifact_type == ArtifactTypeCode.QUIZ.value and artifact._variant == 1
    if artifact_type == ArtifactType.UNKNOWN:
        if artifact._artifact_type == ArtifactTypeCode.QUIZ.value:
            return artifact._variant not in (1, 2)
        return artifact._artifact_type not in _KNOWN_ARTIFACT_TYPE_CODES

    type_code = _ARTIFACT_TYPE_CODES_BY_KIND.get(artifact_type)
    if type_code is not None:
        return artifact._artifact_type == type_code

    return False


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

        Position knowledge (``a[2]`` type, ``a[4]`` status, ``a[15][0]``
        timestamp) is delegated to
        :class:`notebooklm._row_adapters.ArtifactRow` — when Google
        reshapes the wire, the position constants change there and this
        method adapts automatically.

        The error-key asymmetry is intentional: explicit-ID misses
        derive the key from ``type_name`` while empty-filter results use
        ``no_result_error_key`` verbatim.

        Returns the **raw row** (not an :class:`ArtifactRow`) to preserve
        the historical private helper contract. New internal callers that
        need typed access should use :meth:`select_completed_artifact_row`.
        """
        return self.select_completed_artifact_row(
            candidates,
            artifact_id,
            type_name,
            no_result_error_key,
            type_code=type_code,
        ).raw

    def select_completed_artifact_row(
        self,
        candidates: Sequence[Any],
        artifact_id: str | None,
        type_name: str,
        no_result_error_key: str,
        *,
        type_code: ArtifactTypeCode,
    ) -> ArtifactRow:
        """Select a completed artifact row by ID or latest timestamp."""
        rows = iter_artifact_rows(candidates)
        filtered = [row for row in rows if row.matches_type(type_code, completed_only=True)]

        if artifact_id:
            match = next((row for row in filtered if row.id == artifact_id), None)
            if not match:
                raise ArtifactNotReadyError(
                    type_name.lower().replace(" ", "_"), artifact_id=artifact_id
                )
            return match

        if not filtered:
            raise ArtifactNotReadyError(no_result_error_key)

        # Sort by raw timestamp so missing / ``None`` / non-list shapes
        # coerce to ``0`` without crashing the comparison (mirrors the
        # historical ``(a[15][0] or 0)`` falsy-coerce trick that pinned
        # the ``test_handles_none_at_timestamp_position_without_typeerror``
        # contract).
        filtered.sort(key=lambda row: row.created_at_raw or 0, reverse=True)
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
                if _matches_artifact_type(artifact, artifact_type):
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
                    if _matches_artifact_type(mind_map_artifact, artifact_type):
                        artifacts.append(mind_map_artifact)
        return artifacts
