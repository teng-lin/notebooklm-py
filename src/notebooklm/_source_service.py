"""Transport-neutral semantic service for the migrated P6.7 Source slice."""

from __future__ import annotations

from pathlib import Path
from types import MappingProxyType

from ._backend import (
    BackendAdapter,
    BackendDeadlineExceededError,
    BackendError,
    BackendErrorReason,
    mark_backend_outcome_unknown,
    rebind_operation,
    require_leaves,
)
from ._deadline import RuntimeDeadline, RuntimeDeadlineFactory
from ._records import (
    SOURCE_ADD_DRIVE_DEF,
    SOURCE_ADD_FILE_DEF,
    SOURCE_ADD_TEXT_DEF,
    SOURCE_ADD_URL_BATCH_DEF,
    SOURCE_CHECK_FRESHNESS_DEF,
    SOURCE_DELETE_DEF,
    SOURCE_GET_DEF,
    SOURCE_GET_FULLTEXT_DEF,
    SOURCE_GET_GUIDE_DEF,
    SOURCE_PATCH_TITLE_DEF,
    SOURCE_REFRESH_DEF,
    SOURCE_UPDATE_DEF,
    SOURCE_WAIT_DEF,
    SourceAddDriveInput,
    SourceAddDriveResult,
    SourceAddFileInput,
    SourceAddFileResult,
    SourceAddTextInput,
    SourceAddTextResult,
    SourceAddUrlBatchInput,
    SourceAddUrlBatchResult,
    SourceDeleteInput,
    SourceFileInputKind,
    SourceFreshnessInput,
    SourceFulltextInput,
    SourceFulltextResult,
    SourceGetInput,
    SourceGuideInput,
    SourceGuideResult,
    SourcePatchTitleInput,
    SourceProgressCallback,
    SourceRecord,
    SourceRefreshInput,
    SourceUpdateResult,
    SourceWaitSnapshotInput,
    SourceWaitSnapshotResult,
)


class SourceService:
    """Invoke typed Source operations without web/RPC/public-model vocabulary."""

    __slots__ = ("_backend", "_deadline_factory")

    def __init__(
        self,
        backend: BackendAdapter,
        *,
        deadline_factory: RuntimeDeadlineFactory | None = None,
    ) -> None:
        self._backend = backend
        # P9.2 contract 3: a service-owned workflow mints one deadline
        # before its first leaf and threads that identity through every phase.
        self._deadline_factory = deadline_factory

    async def add_urls_batch(
        self,
        notebook_id: str,
        urls: tuple[str, ...],
        *,
        deadline: RuntimeDeadline | None = None,
    ) -> SourceAddUrlBatchResult:
        return await self._backend.invoke(
            SOURCE_ADD_URL_BATCH_DEF,
            SourceAddUrlBatchInput(notebook_id, urls),
            deadline=deadline,
        )

    async def add_text(
        self,
        notebook_id: str,
        title: str,
        content: str,
        *,
        wait: bool,
        wait_timeout: float,
        idempotent: bool,
        deadline: RuntimeDeadline | None = None,
    ) -> SourceAddTextResult:
        return await self._backend.invoke(
            SOURCE_ADD_TEXT_DEF,
            SourceAddTextInput(
                notebook_id,
                title,
                content,
                wait=wait,
                wait_timeout=wait_timeout,
                idempotent=idempotent,
            ),
            deadline=deadline,
        )

    async def add_drive(
        self,
        notebook_id: str,
        file_id: str,
        title: str,
        *,
        mime_type: str,
        wait: bool,
        wait_timeout: float,
        deadline: RuntimeDeadline | None = None,
    ) -> SourceAddDriveResult:
        return await self._backend.invoke(
            SOURCE_ADD_DRIVE_DEF,
            SourceAddDriveInput(
                notebook_id,
                file_id,
                title,
                mime_type,
                wait=wait,
                wait_timeout=wait_timeout,
            ),
            deadline=deadline,
        )

    async def finalize_drive_title(
        self,
        notebook_id: str,
        source: SourceRecord,
        requested_title: str,
    ) -> SourceAddDriveResult:
        """Apply a waited Drive title under the original add operation."""

        return await self._backend.invoke(
            SOURCE_ADD_DRIVE_DEF,
            SourceAddDriveInput(
                notebook_id,
                "",
                requested_title,
                "application/vnd.google-apps.document",
                finalize_source=source,
            ),
            deadline=None,
        )

    async def add_file(
        self,
        notebook_id: str,
        file_path: str | Path,
        *,
        mime_type: str | None,
        wait: bool,
        wait_timeout: float,
        title: str | None,
        on_progress: SourceProgressCallback | None,
    ) -> SourceAddFileResult:
        return await self._backend.invoke(
            SOURCE_ADD_FILE_DEF,
            SourceAddFileInput(
                notebook_id,
                SourceFileInputKind.LOCAL,
                file_path=file_path,
                mime_type=mime_type,
                title=title,
                wait=wait,
                wait_timeout=wait_timeout,
                on_progress=on_progress,
            ),
            deadline=None,
        )

    async def add_drive_file(
        self,
        notebook_id: str,
        document_id: str,
        *,
        title: str | None,
        wait: bool,
        wait_timeout: float,
    ) -> SourceAddFileResult:
        return await self._backend.invoke(
            SOURCE_ADD_FILE_DEF,
            SourceAddFileInput(
                notebook_id,
                SourceFileInputKind.DRIVE_DOWNLOAD,
                document_id=document_id,
                title=title,
                wait=wait,
                wait_timeout=wait_timeout,
            ),
            deadline=None,
        )

    async def finalize_file_title(
        self,
        notebook_id: str,
        source: SourceRecord,
        requested_title: str,
    ) -> SourceAddFileResult:
        """Apply a waited upload title under the original add operation."""

        return await self._backend.invoke(
            SOURCE_ADD_FILE_DEF,
            SourceAddFileInput(
                notebook_id,
                SourceFileInputKind.LOCAL,
                title=requested_title,
                finalize_source=source,
            ),
            deadline=None,
        )

    async def delete(self, notebook_id: str, source_id: str) -> None:
        await self._backend.invoke(
            SOURCE_DELETE_DEF,
            SourceDeleteInput(notebook_id, source_id),
            deadline=None,
        )

    async def update(
        self,
        notebook_id: str,
        source_id: str,
        new_title: str,
        *,
        return_object: bool,
        deadline: RuntimeDeadline | None = None,
    ) -> SourceUpdateResult:
        """Rename through one patch leaf and hydrate only a null echo."""
        workflow = SOURCE_UPDATE_DEF.key
        # The conditional leaf set is still checked up front: a later backend
        # must never perform the title write and only then discover it cannot
        # execute the null-echo hydration path.
        require_leaves(self._backend, SOURCE_PATCH_TITLE_DEF.key, SOURCE_GET_DEF.key)
        if deadline is None and self._deadline_factory is not None:
            deadline = self._deadline_factory.start()

        write_dispatched = False
        try:
            patched = await self._backend.invoke(
                SOURCE_PATCH_TITLE_DEF,
                SourcePatchTitleInput(notebook_id, source_id, new_title),
                deadline=deadline,
            )
            write_dispatched = True
            source = patched.source
            if source is None:
                hydrated = await self._backend.invoke(
                    SOURCE_GET_DEF,
                    SourceGetInput(notebook_id, source_id),
                    deadline=deadline,
                )
                source = hydrated.source
                if source is None:
                    raise BackendError(
                        message=f"Source not found: {source_id}",
                        operation=workflow,
                        diagnostics=MappingProxyType(
                            {
                                "source_id": source_id,
                                "raw_response": None,
                            }
                        ),
                        reason=BackendErrorReason.SOURCE_NOT_FOUND,
                    )
            return SourceUpdateResult(source if return_object else None)
        except BackendError as error:
            if error.operation is workflow:
                raise
            if write_dispatched and isinstance(error, BackendDeadlineExceededError):
                # The patch landed but the hydration read exhausted the shared
                # budget: the workflow outcome is now unsafe to retry blindly.
                error = mark_backend_outcome_unknown(error)
            raise rebind_operation(error, workflow) from error.__cause__

    async def refresh(self, notebook_id: str, source_id: str) -> None:
        await self._backend.invoke(
            SOURCE_REFRESH_DEF,
            SourceRefreshInput(notebook_id, source_id),
            deadline=None,
        )

    async def check_freshness(self, notebook_id: str, source_id: str) -> bool:
        result = await self._backend.invoke(
            SOURCE_CHECK_FRESHNESS_DEF,
            SourceFreshnessInput(notebook_id, source_id),
            deadline=None,
        )
        return result.fresh

    async def wait_snapshot(self, notebook_id: str) -> SourceWaitSnapshotResult:
        """Fetch one neutral snapshot for a facade-owned source poll tick."""
        return await self._backend.invoke(
            SOURCE_WAIT_DEF,
            SourceWaitSnapshotInput(notebook_id),
            # Source wait historically owns a relative polling budget and does
            # not clamp an in-flight GET_NOTEBOOK read to the remaining time.
            deadline=None,
        )

    async def get_guide(self, notebook_id: str, source_id: str) -> SourceGuideResult:
        return await self._backend.invoke(
            SOURCE_GET_GUIDE_DEF,
            SourceGuideInput(notebook_id, source_id),
            deadline=None,
        )

    async def get_fulltext(
        self,
        notebook_id: str,
        source_id: str,
        *,
        output_format: str,
    ) -> SourceFulltextResult:
        return await self._backend.invoke(
            SOURCE_GET_FULLTEXT_DEF,
            SourceFulltextInput(notebook_id, source_id, output_format),
            deadline=None,
        )


__all__ = ["SourceService"]
