"""Client-owned composition holder state."""

from __future__ import annotations

import asyncio
from contextlib import AbstractAsyncContextManager, nullcontext
from typing import TYPE_CHECKING, Any

from ._session_config import DEFAULT_MAX_CONCURRENT_RPCS

if TYPE_CHECKING:
    from ._rpc_executor import RpcExecutor
    from ._session_init import SessionCollaborators
    from ._session_transport import SessionTransport


class ClientComposed:
    """Mutable holder for composition state that is migrating off ``Session``."""

    def __init__(
        self,
        *,
        max_concurrent_rpcs: int | None = DEFAULT_MAX_CONCURRENT_RPCS,
    ) -> None:
        if max_concurrent_rpcs is not None and max_concurrent_rpcs < 1:
            raise ValueError(f"max_concurrent_rpcs must be >= 1, got {max_concurrent_rpcs!r}")
        self.max_concurrent_rpcs = max_concurrent_rpcs
        self._rpc_semaphore: asyncio.Semaphore | None = None

        # Phase 2 migration placeholders. Phase 1 still returns the canonical
        # `ComposedSession` bundle, but it also populates these fields so the
        # next phase can move reads onto this holder without another state move.
        self.transport: SessionTransport | None = None
        self.executor: RpcExecutor | None = None
        # Avoid a plain `.collaborators` attribute here: the ADR-014 lint
        # reserves that name for the deleted Stage A Session accessor.
        self.session_collaborators: SessionCollaborators | None = None

    def get_rpc_semaphore(self) -> AbstractAsyncContextManager[Any]:
        """Return the lazy per-client RPC semaphore, or a no-op context."""
        if self.max_concurrent_rpcs is None:
            return nullcontext()
        if self._rpc_semaphore is None:
            self._rpc_semaphore = asyncio.Semaphore(self.max_concurrent_rpcs)
        return self._rpc_semaphore


__all__ = ["ClientComposed"]
