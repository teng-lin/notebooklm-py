"""Semantic Studio catalog over the neutral backend port."""

from __future__ import annotations

from .._backend import BackendAdapter
from .._deadline import RuntimeDeadline
from .._records import (
    ARTIFACT_GET_DEF,
    ARTIFACT_LIST_DEF,
    ArtifactGetInput,
    ArtifactListInput,
    ArtifactRecord,
)
from .classifiers import matches_artifact_family


class StudioCatalog:
    """List and select complete heterogeneous Studio records.

    Public projection to :class:`~notebooklm.types.Artifact` is a facade
    responsibility (P10 invariant I1); callers project ``list_records``/
    ``get_record`` output themselves.
    """

    __slots__ = ("_backend",)

    def __init__(self, backend: BackendAdapter) -> None:
        self._backend = backend

    async def list_records(
        self,
        notebook_id: str,
        family: str | None = None,
        *,
        deadline: RuntimeDeadline | None = None,
    ) -> tuple[ArtifactRecord, ...]:
        """Return neutral records for family services without a second fetch."""

        result = await self._backend.invoke(
            ARTIFACT_LIST_DEF,
            ArtifactListInput(notebook_id, family),
            deadline=deadline,
        )
        return tuple(
            record for record in result.artifacts if matches_artifact_family(record, family)
        )

    async def get_record(
        self,
        notebook_id: str,
        artifact_id: str,
        *,
        deadline: RuntimeDeadline | None = None,
    ) -> ArtifactRecord | None:
        """Return one complete neutral row without public projection."""

        result = await self._backend.invoke(
            ARTIFACT_GET_DEF,
            ArtifactGetInput(notebook_id, artifact_id),
            deadline=deadline,
        )
        return result.artifact


__all__ = ["StudioCatalog"]
