"""Transport-neutral Infographic and Slide Deck family behavior."""

from __future__ import annotations

from typing import Literal

from .._backend import BackendAdapter
from .._deadline import RuntimeDeadline
from .._records import (
    ARTIFACT_GENERATE_INFOGRAPHIC_DEF,
    ARTIFACT_GENERATE_SLIDE_DECK_DEF,
    ArtifactRecord,
    InfographicGenerateRequest,
    SlideDeckGenerateRequest,
    VisualGenerateResult,
    VisualMetadataRecord,
)
from .catalog import StudioCatalog
from .generation import StudioGenerationInputs, _generation_budget


class VisualFamilyService:
    """Visual generation, discovery, and usable-readiness metadata."""

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

    async def generate_infographic(
        self,
        request: InfographicGenerateRequest,
        *,
        deadline: RuntimeDeadline | None = None,
    ) -> VisualGenerateResult:
        deadline = _generation_budget(self._inputs, deadline)
        return await self._backend.invoke(
            ARTIFACT_GENERATE_INFOGRAPHIC_DEF,
            await self._inputs.infographic(request, deadline=deadline),
            deadline=deadline,
        )

    async def generate_slide_deck(
        self,
        request: SlideDeckGenerateRequest,
        *,
        deadline: RuntimeDeadline | None = None,
    ) -> VisualGenerateResult:
        deadline = _generation_budget(self._inputs, deadline)
        return await self._backend.invoke(
            ARTIFACT_GENERATE_SLIDE_DECK_DEF,
            await self._inputs.slide_deck(request, deadline=deadline),
            deadline=deadline,
        )

    async def list_infographics(
        self,
        notebook_id: str,
        *,
        deadline: RuntimeDeadline | None = None,
    ) -> tuple[ArtifactRecord, ...]:
        return await self._catalog.list_records(notebook_id, "infographic", deadline=deadline)

    async def list_slide_decks(
        self,
        notebook_id: str,
        *,
        deadline: RuntimeDeadline | None = None,
    ) -> tuple[ArtifactRecord, ...]:
        return await self._catalog.list_records(notebook_id, "slide_deck", deadline=deadline)

    async def get(
        self,
        notebook_id: str,
        artifact_id: str,
        family: Literal["infographic", "slide_deck"],
        *,
        deadline: RuntimeDeadline | None = None,
    ) -> ArtifactRecord | None:
        if family not in {"infographic", "slide_deck"}:
            raise ValueError("visual family must be 'infographic' or 'slide_deck'")
        record = await self._catalog.get_record(notebook_id, artifact_id, deadline=deadline)
        return record if record is not None and record.family == family else None

    @staticmethod
    def metadata(record: ArtifactRecord) -> VisualMetadataRecord:
        """Preserve rendered accessibility data and distinguish usable from terminal."""

        if record.family not in {"infographic", "slide_deck"}:
            raise ValueError("visual metadata requires an infographic or slide deck record")
        has_representation = bool(
            record.url
            or any(item.image_url for item in record.infographics)
            or any(item.image_url for item in record.slides)
        )
        return VisualMetadataRecord(
            artifact_id=record.id,
            family=record.family,
            lifecycle_status=record.status,
            usable=record.status == "completed" and has_representation,
            slides=record.slides,
            infographics=record.infographics,
            preferred_url=record.url,
            generation_prompt=record.generation_prompt,
            created_at=record.created_at,
        )


__all__ = ["VisualFamilyService"]
