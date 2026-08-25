"""Transport-neutral semantic service for the P2.2 notebook mutation slice."""

from __future__ import annotations

import logging
from types import MappingProxyType

from ._backend import (
    BackendAdapter,
    BackendDeadlineExceededError,
    BackendError,
    BackendErrorReason,
    annotate_backend_error,
    mark_backend_outcome_unknown,
    rebind_operation,
    require_leaves,
)
from ._deadline import RuntimeDeadline, RuntimeDeadlineFactory
from ._idempotency_create import idempotent_create, semantic_may_have_committed
from ._operations import Operation
from ._records import (
    NOTEBOOK_ALLOCATE_DEF,
    NOTEBOOK_CREATE_DEF,
    NOTEBOOK_DELETE_DEF,
    NOTEBOOK_GET_DEF,
    NOTEBOOK_LIST_DEF,
    NOTEBOOK_PATCH_DEF,
    NOTEBOOK_REMOVE_RECENT_DEF,
    NOTEBOOK_UPDATE_DEF,
    SETTINGS_GET_LIMITS_DEF,
    NotebookAllocateInput,
    NotebookCreateInput,
    NotebookDeleteInput,
    NotebookGetInput,
    NotebookListInput,
    NotebookPatchInput,
    NotebookRecord,
    NotebookRemoveRecentInput,
    NotebookUpdateInput,
    SettingsGetLimitsInput,
)
from .exceptions import ValidationError

logger = logging.getLogger("notebooklm._notebooks")

_CREATE_CONTEXT_FAILURE = "create_context_failure"
_RECONCILIATION_PROBE_FAILURE = "reconciliation_probe_failure"
_RECONCILIATION_UNRESOLVED = "notebook_create_reconciliation_unresolved"
_DIRECT_PROBE_REASONS = frozenset(
    {
        BackendErrorReason.AUTH,
        BackendErrorReason.NETWORK,
        BackendErrorReason.RATE_LIMIT,
        BackendErrorReason.SERVER,
        BackendErrorReason.TIMEOUT,
    }
)


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
    ) -> NotebookRecord:
        """Snapshot, allocate once, and reconcile any uncertain commit."""
        value = NotebookCreateInput(title)
        workflow = NOTEBOOK_CREATE_DEF.key
        require_leaves(
            self._backend,
            NOTEBOOK_LIST_DEF.key,
            NOTEBOOK_ALLOCATE_DEF.key,
            SETTINGS_GET_LIMITS_DEF.key,
        )
        deadline = self._start_deadline(deadline)

        baseline_ids = set()
        baseline_available = True
        baseline_error_name: str | None = None
        try:
            baseline = await self._backend.invoke(
                NOTEBOOK_LIST_DEF,
                NotebookListInput(),
                deadline=deadline,
            )
            baseline_ids = {notebook.id for notebook in baseline.notebooks}
        except Exception as error:
            baseline_ids.clear()
            baseline_available = False
            baseline_error_name = self._failure_type_name(error)
            logger.warning(
                "create: baseline list() failed (%s); the idempotency probe can no "
                "longer tell a notebook this call created from one that was already "
                "there, so a transport failure will surface as an ambiguity error "
                "instead of recovering",
                baseline_error_name,
                exc_info=True,
            )

        last_allocate_error: BackendError | None = None

        async def allocate() -> NotebookRecord:
            nonlocal last_allocate_error
            try:
                result = await self._backend.invoke(
                    NOTEBOOK_ALLOCATE_DEF,
                    NotebookAllocateInput(value.title),
                    deadline=deadline,
                )
            except BackendError as leaf_error:
                error = rebind_operation(leaf_error, workflow)
                if (error.diagnostics or {}).get("quota_rejection") is True:
                    limit_error = await self._notebook_limit_error(error, deadline=deadline)
                    if limit_error is not None:
                        raise limit_error from None
                last_allocate_error = error
                raise error from leaf_error.__cause__
            return result.notebook

        async def probe() -> NotebookRecord | None:
            create_error = last_allocate_error
            if create_error is None:
                raise BackendError(
                    "notebook.create reconciliation started without an allocation failure",
                    operation=workflow,
                )
            try:
                current = await self._backend.invoke(
                    NOTEBOOK_LIST_DEF,
                    NotebookListInput(),
                    deadline=deadline,
                )
            except BackendError as leaf_error:
                error = rebind_operation(leaf_error, workflow)
                if error.reason in _DIRECT_PROBE_REASONS:
                    if error.reason is not BackendErrorReason.TIMEOUT:
                        logger.warning(
                            "create: probe list() failed with transport/auth error; "
                            "propagating so the caller can avoid a duplicate-resource retry"
                        )
                    error = self._attach_create_context(error, create_error)
                    error = mark_backend_outcome_unknown(error)
                    raise error from leaf_error.__cause__
                logger.warning(
                    "create: probe list() failed with a non-transport error (%s); the "
                    "create cannot be confirmed, so it will not be retried",
                    self._failure_type_name(leaf_error),
                    exc_info=True,
                )
                raise self._unresolved_probe_error(
                    value,
                    leaf_error,
                    create_error,
                ) from leaf_error.__cause__
            except Exception as error:
                logger.warning(
                    "create: probe list() failed with a non-transport error (%s); the "
                    "create cannot be confirmed, so it will not be retried",
                    type(error).__name__,
                    exc_info=True,
                )
                raise self._unresolved_probe_error(
                    value,
                    None,
                    create_error,
                    failure_name=type(error).__name__,
                ) from error
            matches = tuple(
                notebook for notebook in current.notebooks if notebook.title == value.title
            )
            if baseline_available:
                matches = tuple(notebook for notebook in matches if notebook.id not in baseline_ids)
            elif matches:
                raise self._unconfirmed_create_error(
                    "Cannot disambiguate notebook with title "
                    f"{value.title!r} — check your notebook list before retrying: the "
                    "pre-create baseline snapshot failed "
                    f"({baseline_error_name}), so "
                    f"{', '.join(f'{item.id} ({item.title!r})' for item in matches)} may "
                    "either predate this create or be the notebook it just created.",
                    create_error,
                )
            if len(matches) == 1:
                return next(iter(matches))
            if len(matches) > 1:
                raise self._unconfirmed_create_error(
                    f"Cannot disambiguate notebook with title {value.title!r}: "
                    f"probe found {len(matches)} new notebooks with this title",
                    create_error,
                )
            return None

        result = await idempotent_create(
            allocate,
            probe,
            may_have_committed=semantic_may_have_committed,
            label=f"notebook.create[{value.title!r}]",
        )
        return result.value

    async def _notebook_limit_error(
        self,
        error: BackendError,
        *,
        deadline: RuntimeDeadline | None,
    ) -> BackendError | None:
        try:
            settings = await self._backend.invoke(
                SETTINGS_GET_LIMITS_DEF,
                SettingsGetLimitsInput(),
                deadline=deadline,
            )
        except Exception:
            logger.debug(
                "Could not fetch account limits after CREATE_NOTEBOOK failure; "
                "leaving original RPC error unchanged",
                exc_info=True,
            )
            return None
        limit = settings.limits.notebook_limit
        if limit is None:
            return None
        try:
            listed = await self._backend.invoke(
                NOTEBOOK_LIST_DEF,
                NotebookListInput(),
                deadline=deadline,
            )
        except Exception:
            logger.debug(
                "Could not list notebooks after CREATE_NOTEBOOK failure; "
                "leaving original RPC error unchanged",
                exc_info=True,
            )
            return None
        owned_count = sum(1 for notebook in listed.notebooks if notebook.is_owner)
        if owned_count < max(limit - 1, 0):
            return None
        return BackendError(
            message="notebook limit reached",
            operation=Operation.NOTEBOOK_CREATE,
            diagnostics=MappingProxyType(
                {
                    "current_count": owned_count,
                    "limit": limit,
                    "original_message": error.message,
                    "original_reason": (error.reason.value if error.reason is not None else None),
                    "original_diagnostics": dict(error.diagnostics or {}),
                }
            ),
            reason=BackendErrorReason.NOTEBOOK_LIMIT,
        )

    @staticmethod
    def _failure_type_name(error: Exception) -> str:
        cause = error.__cause__
        return type(cause).__name__ if cause is not None else type(error).__name__

    @staticmethod
    def _public_failure(error: BackendError) -> object | None:
        return (error.diagnostics or {}).get("public_error_failure")

    @classmethod
    def _attach_create_context(
        cls,
        error: BackendError,
        create_error: BackendError,
    ) -> BackendError:
        failure = cls._public_failure(create_error)
        if failure is None:
            return error
        return annotate_backend_error(error, **{_CREATE_CONTEXT_FAILURE: failure})

    @classmethod
    def _unconfirmed_create_error(
        cls,
        message: str,
        create_error: BackendError,
    ) -> BackendError:
        diagnostics = {_RECONCILIATION_UNRESOLVED: True}  # type: dict[str, object]
        create_failure = cls._public_failure(create_error)
        if create_failure is not None:
            diagnostics.update({_CREATE_CONTEXT_FAILURE: create_failure})
        return BackendError(
            message,
            operation=Operation.NOTEBOOK_CREATE,
            outcome_unknown=True,
            diagnostics=MappingProxyType(diagnostics),
            reason=BackendErrorReason.RPC,
        )

    @classmethod
    def _unresolved_probe_error(
        cls,
        value: NotebookCreateInput,
        probe_error: BackendError | None,
        create_error: BackendError,
        *,
        failure_name: str | None = None,
    ) -> BackendError:
        reported_name = failure_name or (
            cls._failure_type_name(probe_error) if probe_error is not None else "Exception"
        )
        error = cls._unconfirmed_create_error(
            "UNRESOLVED — do not blindly retry; check your notebook list first. "
            f"Cannot confirm notebook with title {value.title!r}: the create failed at "
            "the transport level and may or may not have committed, and the idempotency "
            "probe that would settle it failed too "
            f"({reported_name}). "
            "No FURTHER attempt was made.",
            create_error,
        )
        probe_failure = cls._public_failure(probe_error) if probe_error is not None else None
        if probe_failure is None:
            return error
        return annotate_backend_error(error, **{_RECONCILIATION_PROBE_FAILURE: probe_failure})

    async def update(
        self,
        notebook_id: str,
        *,
        title: str | None = None,
        emoji: str | None = None,
        deadline: RuntimeDeadline | None = None,
    ) -> NotebookRecord:
        """Patch notebook properties, then read the complete record back once."""
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
            return result.notebook
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
    ) -> NotebookRecord:
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
