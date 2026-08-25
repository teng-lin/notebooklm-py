"""Transport-neutral semantic service for the P2.2 notebook mutation slice."""

from __future__ import annotations

from types import MappingProxyType
from typing import TYPE_CHECKING

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
from ._operations import Operation
from ._projectors import project_notebook
from ._records import (
    NOTEBOOK_CREATE_DEF,
    NOTEBOOK_DELETE_DEF,
    NOTEBOOK_GET_DEF,
    NOTEBOOK_PATCH_DEF,
    NOTEBOOK_REMOVE_RECENT_DEF,
    NOTEBOOK_UPDATE_DEF,
    NotebookCreateInput,
    NotebookDeleteInput,
    NotebookGetInput,
    NotebookPatchInput,
    NotebookRemoveRecentInput,
    NotebookUpdateInput,
)
from .exceptions import ValidationError

if TYPE_CHECKING:
    from .types import Notebook


class NotebookMutationService:
    """Validate notebook mutations and invoke their typed backend operations."""

    __slots__ = ("_backend", "_deadline_factory")

    def __init__(
        self,
        backend: BackendAdapter,
        *,
        deadline_factory: RuntimeDeadlineFactory | None = None,
    ) -> None:
        self._backend = backend
        self._deadline_factory = deadline_factory

    async def create(
        self,
        title: str,
        *,
        deadline: RuntimeDeadline | None = None,
    ) -> Notebook:
        result = await self._backend.invoke(
            NOTEBOOK_CREATE_DEF,
            NotebookCreateInput(title),
            deadline=deadline,
        )
        return project_notebook(result.notebook)

    async def update(
        self,
        notebook_id: str,
        *,
        title: str | None = None,
        emoji: str | None = None,
        deadline: RuntimeDeadline | None = None,
    ) -> Notebook:
        """Patch notebook properties, then read the complete model back once."""
        if title is None and emoji is None:
            raise ValidationError("At least one of title or emoji must be provided")
        value = NotebookUpdateInput(notebook_id, title=title, emoji=emoji)
        workflow = NOTEBOOK_UPDATE_DEF.key
        require_leaves(self._backend, NOTEBOOK_PATCH_DEF.key, NOTEBOOK_GET_DEF.key)
        deadline = self._start_deadline(deadline)
        write_dispatched = False
        try:
            await self._backend.invoke(
                NOTEBOOK_PATCH_DEF,
                NotebookPatchInput(
                    value.notebook_id,
                    title=value.title,
                    emoji=value.emoji,
                ),
                deadline=deadline,
            )
            write_dispatched = True
            result = await self._backend.invoke(
                NOTEBOOK_GET_DEF,
                NotebookGetInput(value.notebook_id, require_notebook=True),
                deadline=deadline,
            )
            if result.notebook is None:
                raise self._not_found(value)
            return project_notebook(result.notebook)
        except BackendError as error:
            if error.operation is workflow:
                raise
            leaf_cause = error.__cause__
            if error.reason is BackendErrorReason.NOT_FOUND:
                raise self._not_found(value, leaf_error=error) from leaf_cause
            if write_dispatched and isinstance(error, BackendDeadlineExceededError):
                error = mark_backend_outcome_unknown(error)
            raise rebind_operation(error, workflow) from leaf_cause

    def _start_deadline(self, deadline: RuntimeDeadline | None) -> RuntimeDeadline | None:
        """Mint the one workflow deadline unless the caller supplied its own."""
        if deadline is not None or self._deadline_factory is None:
            return deadline
        return self._deadline_factory.start()

    @staticmethod
    def _not_found(
        value: NotebookUpdateInput,
        *,
        leaf_error: BackendError | None = None,
    ) -> BackendError:
        diagnostics = dict(leaf_error.diagnostics or {}) if leaf_error is not None else {}
        # The domain-specific projector reconstructs the legacy ClientError
        # cause from the copied RPC fields; generic leaf projection evidence
        # belongs to reason=NOT_FOUND and must not be replayed as this reason.
        diagnostics.pop("public_error_failure", None)
        diagnostics.update(
            {
                "notebook_id": value.notebook_id,
                "leaf_operation": Operation.NOTEBOOK_GET,
            }
        )
        return BackendError(
            message=f"Notebook not found: {value.notebook_id}",
            operation=Operation.NOTEBOOK_UPDATE,
            diagnostics=MappingProxyType(diagnostics),
            reason=BackendErrorReason.NOTEBOOK_NOT_FOUND,
        )

    async def update_title(
        self,
        notebook_id: str,
        title: str,
        *,
        deadline: RuntimeDeadline | None = None,
    ) -> Notebook:
        """Compatibility convenience over the generic property mutation."""
        return await self.update(notebook_id, title=title, deadline=deadline)

    async def delete(
        self,
        notebook_id: str,
        *,
        deadline: RuntimeDeadline | None = None,
    ) -> None:
        await self._backend.invoke(
            NOTEBOOK_DELETE_DEF,
            NotebookDeleteInput(notebook_id),
            deadline=deadline,
        )

    async def remove_from_recent(
        self,
        notebook_id: str,
        *,
        deadline: RuntimeDeadline | None = None,
    ) -> None:
        await self._backend.invoke(
            NOTEBOOK_REMOVE_RECENT_DEF,
            NotebookRemoveRecentInput(notebook_id),
            deadline=deadline,
        )


__all__ = ["NotebookMutationService"]
