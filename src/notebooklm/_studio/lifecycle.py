"""Lifecycle-terminal Studio polling over the semantic backend."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .._artifact.polling import ArtifactPollingService, PollStatusCallback, StatusChangeCallback
from .._deadline import RuntimeDeadline
from .._polling_registry import PollRegistry
from .._semantic.backend import BackendAdapter
from .._semantic.records import ARTIFACT_WAIT_DEF, ArtifactPollInput, GenerationStatusRecord

if TYPE_CHECKING:
    from .._artifact.polling import OperationScopeProvider
    from .._runtime.contracts import LoopGuard


class ArtifactLifecycleService:
    """Own shared polling while preserving the facade's terminal contract."""

    __slots__ = ("_backend", "_polling")

    def __init__(
        self,
        backend: BackendAdapter | None,
        *,
        loop_guard: LoopGuard,
        op_scope: OperationScopeProvider,
        poll_registry: PollRegistry | None = None,
    ) -> None:
        self._backend = backend
        self._polling = ArtifactPollingService(
            loop_guard=loop_guard,
            op_scope=op_scope,
            poll_registry=poll_registry,
        )

    async def observe(
        self,
        notebook_id: str,
        task_id: str,
        *,
        deadline: RuntimeDeadline | None = None,
    ) -> GenerationStatusRecord:
        if self._backend is None:
            raise RuntimeError("Artifact lifecycle observation requires a semantic backend")
        result = await self._backend.invoke(
            ARTIFACT_WAIT_DEF,
            ArtifactPollInput(notebook_id, task_id),
            deadline=deadline,
        )
        return result.status

    async def _wait_for_completion(
        self,
        notebook_id: str,
        task_id: str,
        *,
        initial_interval: float,
        max_interval: float,
        timeout: float,
        max_not_found: int,
        min_not_found_window: float,
        poll_status: PollStatusCallback,
        on_status_change: StatusChangeCallback | None,
        deadline: RuntimeDeadline | None = None,
    ) -> object:
        """Delegate to the shared poll loop.

        Not I1-public: the return value and ``on_status_change`` payload are
        the caller-supplied :class:`~notebooklm.types.GenerationStatus`
        values the ``poll_status``/``on_status_change`` callbacks themselves
        carry (the facade builds both), so this collaboration seam stays
        private rather than naming that public type in its own signature.
        """
        return await self._polling.wait_for_completion(
            notebook_id,
            task_id,
            initial_interval=initial_interval,
            max_interval=max_interval,
            timeout=timeout,
            max_not_found=max_not_found,
            min_not_found_window=min_not_found_window,
            poll_status=poll_status,
            on_status_change=on_status_change,
            deadline=deadline,
        )

    async def drain(self) -> None:
        await self._polling.drain()


__all__ = ["ArtifactLifecycleService"]
