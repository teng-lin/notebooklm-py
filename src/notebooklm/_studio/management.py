"""Semantic Studio management, revision, retry, and suggestion services."""

from __future__ import annotations

from types import MappingProxyType

from .._deadline import RuntimeDeadline, RuntimeDeadlineFactory
from .._semantic.backend import (
    BackendAdapter,
    BackendDeadlineExceededError,
    BackendError,
    BackendErrorReason,
    mark_backend_outcome_unknown,
    rebind_operation,
    require_leaves,
)
from .._semantic.operations import Operation
from .._semantic.records import (
    ARTIFACT_CATALOG_DEF,
    ARTIFACT_DELETE_DEF,
    ARTIFACT_PATCH_TITLE_DEF,
    ARTIFACT_RETRY_DEF,
    ARTIFACT_REVISE_SLIDE_DEF,
    ARTIFACT_SUGGEST_REPORTS_DEF,
    ArtifactCatalogInput,
    ArtifactDeleteInput,
    ArtifactPatchTitleInput,
    ArtifactRenameInput,
    ArtifactRenameResult,
    ArtifactRetryInput,
    ArtifactRetryResult,
    ArtifactReviseSlideInput,
    ArtifactReviseSlideResult,
    ArtifactSuggestReportsInput,
    ArtifactSuggestReportsResult,
)

ARTIFACT_NOT_FOUND_PHASE_KEY = "phase"
ARTIFACT_NOT_FOUND_RENAME_READBACK = "rename_readback"


class StudioManagementService:
    """Manage Studio artifacts and own the rename leaf sequence."""

    __slots__ = ("_backend", "_deadline_factory")

    def __init__(
        self,
        backend: BackendAdapter,
        *,
        deadline_factory: RuntimeDeadlineFactory | None = None,
    ) -> None:
        self._backend = backend
        self._deadline_factory = deadline_factory

    async def delete(
        self,
        value: ArtifactDeleteInput,
        *,
        deadline: RuntimeDeadline | None = None,
    ) -> None:
        await self._backend.invoke(ARTIFACT_DELETE_DEF, value, deadline=deadline)

    async def rename(
        self,
        value: ArtifactRenameInput,
        *,
        deadline: RuntimeDeadline | None = None,
    ) -> ArtifactRenameResult:
        """Set one title, then verify and return the row from a plain catalog read."""

        workflow = Operation.ARTIFACT_RENAME
        require_leaves(
            self._backend,
            ARTIFACT_PATCH_TITLE_DEF.key,
            ARTIFACT_CATALOG_DEF.key,
        )
        deadline = self._start_deadline(deadline)
        write_dispatched = False
        try:
            await self._backend.invoke(
                ARTIFACT_PATCH_TITLE_DEF,
                ArtifactPatchTitleInput(
                    value.notebook_id,
                    value.artifact_id,
                    value.new_title,
                ),
                deadline=deadline,
            )
            write_dispatched = True
            catalog = await self._backend.invoke(
                ARTIFACT_CATALOG_DEF,
                ArtifactCatalogInput(value.notebook_id),
                deadline=deadline,
            )
            artifact = next(
                (item for item in catalog.artifacts if item.id == value.artifact_id),
                None,
            )
            if artifact is None:
                raise BackendError(
                    message=f"Artifact not found: {value.artifact_id}",
                    operation=workflow,
                    diagnostics=MappingProxyType(
                        {
                            "artifact_id": value.artifact_id,
                            "artifact_type": None,
                            ARTIFACT_NOT_FOUND_PHASE_KEY: ARTIFACT_NOT_FOUND_RENAME_READBACK,
                            "raw_response": None,
                        }
                    ),
                    reason=BackendErrorReason.ARTIFACT_NOT_FOUND,
                )
            return ArtifactRenameResult(artifact=artifact)
        except BackendError as error:
            if error.operation is workflow:
                raise
            leaf_cause = error.__cause__
            if write_dispatched and isinstance(error, BackendDeadlineExceededError):
                error = mark_backend_outcome_unknown(error)
            raise rebind_operation(error, workflow) from leaf_cause

    def _start_deadline(self, deadline: RuntimeDeadline | None) -> RuntimeDeadline | None:
        """Mint one workflow deadline unless the caller supplied its own."""

        if deadline is not None or self._deadline_factory is None:
            return deadline
        return self._deadline_factory.start()

    async def revise_slide(
        self,
        value: ArtifactReviseSlideInput,
        *,
        deadline: RuntimeDeadline | None = None,
    ) -> ArtifactReviseSlideResult:
        return await self._backend.invoke(ARTIFACT_REVISE_SLIDE_DEF, value, deadline=deadline)

    async def retry(
        self,
        value: ArtifactRetryInput,
        *,
        deadline: RuntimeDeadline | None = None,
    ) -> ArtifactRetryResult:
        return await self._backend.invoke(ARTIFACT_RETRY_DEF, value, deadline=deadline)


class ReportSuggestionService:
    """Obtain report-format suggestions as neutral records."""

    __slots__ = ("_backend",)

    def __init__(self, backend: BackendAdapter) -> None:
        self._backend = backend

    async def suggest(
        self,
        value: ArtifactSuggestReportsInput,
        *,
        deadline: RuntimeDeadline | None = None,
    ) -> ArtifactSuggestReportsResult:
        return await self._backend.invoke(ARTIFACT_SUGGEST_REPORTS_DEF, value, deadline=deadline)


__all__ = [
    "ARTIFACT_NOT_FOUND_PHASE_KEY",
    "ARTIFACT_NOT_FOUND_RENAME_READBACK",
    "ReportSuggestionService",
    "StudioManagementService",
]
