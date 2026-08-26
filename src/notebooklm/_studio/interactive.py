"""Transport-neutral Quiz and Flashcards family behavior."""

from __future__ import annotations

from typing import Literal

from .._deadline import RuntimeDeadline
from .._semantic.backend import BackendAdapter
from .._semantic.records import (
    ARTIFACT_GENERATE_FLASHCARDS_DEF,
    ARTIFACT_GENERATE_QUIZ_DEF,
    ArtifactRecord,
    InteractiveGenerateRequest,
    InteractiveGenerateResult,
    InteractiveMetadataRecord,
)
from .catalog import StudioCatalog
from .generation import StudioGenerationInputs, _generation_budget


class InteractiveFamilyService:
    """Quiz/flashcard generation, discovery, and usable-readiness metadata."""

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

    async def generate_quiz(
        self,
        request: InteractiveGenerateRequest,
        *,
        deadline: RuntimeDeadline | None = None,
    ) -> InteractiveGenerateResult:
        deadline = _generation_budget(self._inputs, deadline)
        return await self._backend.invoke(
            ARTIFACT_GENERATE_QUIZ_DEF,
            await self._inputs.quiz(request, deadline=deadline),
            deadline=deadline,
        )

    async def generate_flashcards(
        self,
        request: InteractiveGenerateRequest,
        *,
        deadline: RuntimeDeadline | None = None,
    ) -> InteractiveGenerateResult:
        deadline = _generation_budget(self._inputs, deadline)
        return await self._backend.invoke(
            ARTIFACT_GENERATE_FLASHCARDS_DEF,
            await self._inputs.flashcards(request, deadline=deadline),
            deadline=deadline,
        )

    async def list_quizzes(
        self,
        notebook_id: str,
        *,
        deadline: RuntimeDeadline | None = None,
    ) -> tuple[ArtifactRecord, ...]:
        return await self._catalog.list_records(notebook_id, "quiz", deadline=deadline)

    async def list_flashcards(
        self,
        notebook_id: str,
        *,
        deadline: RuntimeDeadline | None = None,
    ) -> tuple[ArtifactRecord, ...]:
        return await self._catalog.list_records(notebook_id, "flashcards", deadline=deadline)

    async def get(
        self,
        notebook_id: str,
        artifact_id: str,
        family: Literal["quiz", "flashcards"],
        *,
        deadline: RuntimeDeadline | None = None,
    ) -> ArtifactRecord | None:
        if family not in {"quiz", "flashcards"}:
            raise ValueError("interactive family must be 'quiz' or 'flashcards'")
        record = await self._catalog.get_record(
            notebook_id,
            artifact_id,
            deadline=deadline,
        )
        return record if record is not None and record.family == family else None

    @staticmethod
    def metadata(record: ArtifactRecord) -> InteractiveMetadataRecord:
        """Preserve family user-state while separating usable from terminal."""

        if record.family not in {"quiz", "flashcards"}:
            raise ValueError("interactive metadata requires a quiz or flashcards record")
        return InteractiveMetadataRecord(
            artifact_id=record.id,
            family=record.family,
            lifecycle_status=record.status,
            usable=record.status == "completed",
            generation_prompt=record.generation_prompt,
            user_state=record.user_state,
        )


__all__ = ["InteractiveFamilyService"]
