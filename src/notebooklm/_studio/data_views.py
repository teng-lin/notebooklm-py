"""Transport-neutral mind-map and data-table family behavior."""

from __future__ import annotations

from .._backend import BackendAdapter
from .._deadline import RuntimeDeadline
from .._records import (
    ARTIFACT_GENERATE_DATA_TABLE_DEF,
    ARTIFACT_GENERATE_MIND_MAP_DEF,
    ArtifactRecord,
    DataTableGenerateRequest,
    DataTableGenerateResult,
    MindMapGenerateInput,
    MindMapGenerateResult,
)
from .catalog import StudioCatalog
from .generation import StudioGenerationInputs, _generation_budget


class DataTableFamilyService:
    """Data-table generation and complete catalog selection."""

    __slots__ = ("_backend", "_catalog", "_inputs")

    def __init__(
        self,
        backend: BackendAdapter,
        catalog: StudioCatalog,
        inputs: StudioGenerationInputs,
    ) -> None:
        self._backend = backend
        self._catalog = catalog
        self._inputs = inputs

    async def generate(
        self,
        request: DataTableGenerateRequest,
        *,
        deadline: RuntimeDeadline | None = None,
    ) -> DataTableGenerateResult:
        deadline = _generation_budget(self._inputs, deadline)
        return await self._backend.invoke(
            ARTIFACT_GENERATE_DATA_TABLE_DEF,
            await self._inputs.data_table(request, deadline=deadline),
            deadline=deadline,
        )

    async def list(
        self,
        notebook_id: str,
        *,
        deadline: RuntimeDeadline | None = None,
    ) -> tuple[ArtifactRecord, ...]:
        return await self._catalog.list_records(notebook_id, "data_table", deadline=deadline)

    async def get(
        self,
        notebook_id: str,
        artifact_id: str,
        *,
        deadline: RuntimeDeadline | None = None,
    ) -> ArtifactRecord | None:
        record = await self._catalog.get_record(notebook_id, artifact_id, deadline=deadline)
        return record if record is not None and record.family == "data_table" else None


class NoteBackedMindMapFamilyService:
    """Mind-map generation and dual-backing catalog selection.

    The catalog implements ADR-0019 partial availability: an ordinary RPC
    failure in the optional note-backed subfetch leaves interactive Studio
    maps available, while transport and decoding failures still surface.
    """

    __slots__ = ("_backend", "_catalog")

    def __init__(self, backend: BackendAdapter, catalog: StudioCatalog) -> None:
        self._backend = backend
        self._catalog = catalog

    async def generate(
        self,
        value: MindMapGenerateInput,
        *,
        deadline: RuntimeDeadline | None = None,
    ) -> MindMapGenerateResult:
        return await self._backend.invoke(
            ARTIFACT_GENERATE_MIND_MAP_DEF,
            value,
            deadline=deadline,
        )

    async def list(
        self,
        notebook_id: str,
        *,
        deadline: RuntimeDeadline | None = None,
    ) -> tuple[ArtifactRecord, ...]:
        return await self._catalog.list_records(notebook_id, "mind_map", deadline=deadline)

    async def get(
        self,
        notebook_id: str,
        artifact_id: str,
        *,
        deadline: RuntimeDeadline | None = None,
    ) -> ArtifactRecord | None:
        record = await self._catalog.get_record(notebook_id, artifact_id, deadline=deadline)
        return record if record is not None and record.family == "mind_map" else None


__all__ = ["DataTableFamilyService", "NoteBackedMindMapFamilyService"]
