"""Transport-neutral Audio Overview family behavior."""

from __future__ import annotations

from .._backend import BackendAdapter
from .._deadline import RuntimeDeadline
from .._semantic.records import (
    ARTIFACT_GENERATE_AUDIO_DEF,
    ArtifactRecord,
    AudioGenerateRequest,
    AudioGenerateResult,
    AudioMetadataRecord,
)
from .catalog import StudioCatalog
from .generation import StudioGenerationInputs, _generation_budget


class AudioFamilyService:
    """Audio generation, discovery, readiness, and representation selection."""

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
        request: AudioGenerateRequest,
        *,
        deadline: RuntimeDeadline | None = None,
    ) -> AudioGenerateResult:
        deadline = _generation_budget(self._inputs, deadline)
        return await self._backend.invoke(
            ARTIFACT_GENERATE_AUDIO_DEF,
            await self._inputs.audio(request, deadline=deadline),
            deadline=deadline,
        )

    async def list(
        self,
        notebook_id: str,
        *,
        deadline: RuntimeDeadline | None = None,
    ) -> tuple[ArtifactRecord, ...]:
        return await self._catalog.list_records(
            notebook_id,
            "audio",
            deadline=deadline,
        )

    async def get(
        self,
        notebook_id: str,
        artifact_id: str,
        *,
        deadline: RuntimeDeadline | None = None,
    ) -> ArtifactRecord | None:
        record = await self._catalog.get_record(
            notebook_id,
            artifact_id,
            deadline=deadline,
        )
        return record if record is not None and record.family == "audio" else None

    @staticmethod
    def metadata(record: ArtifactRecord) -> AudioMetadataRecord:
        """Project wait/download-facing metadata without changing lifecycle state."""

        return AudioMetadataRecord(
            artifact_id=record.id,
            lifecycle_status=record.status,
            usable=record.status == "completed" and bool(record.url),
            preferred_url=record.url,
            media_urls=record.media_urls,
            duration_seconds=record.duration_seconds,
            generation_prompt=record.generation_prompt,
            created_at=record.created_at,
        )

    async def select_download(
        self,
        notebook_id: str,
        artifact_id: str | None,
        *,
        deadline: RuntimeDeadline | None = None,
    ) -> AudioMetadataRecord | None:
        """Select one completed audio using the legacy latest-first rule."""

        records = tuple(
            record
            for record in await self.list(notebook_id, deadline=deadline)
            if record.status == "completed"
        )
        if artifact_id:
            selected = next((record for record in records if record.id == artifact_id), None)
        else:
            selected = max(
                records,
                key=lambda record: (
                    int(record.created_at.timestamp()) if record.created_at is not None else 0
                ),
                default=None,
            )
        return None if selected is None else self.metadata(selected)


__all__ = ["AudioFamilyService"]
