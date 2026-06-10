"""Per-request access to the lifespan-bound client.

The REST server binds exactly one
:class:`~notebooklm.client.NotebookLMClient` for the process lifetime via the
ASGI lifespan (one client, bound to the server's event loop, satisfying the
ADR-0004 loop-affinity contract). Route handlers reach it through the
:func:`get_client` FastAPI dependency, so they never touch app-state internals
directly.

This module imports NO ``click`` / ``rich`` / ``cli``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from fastapi import Request

from ._pending import PendingRegistry

if TYPE_CHECKING:
    from ..client import NotebookLMClient

__all__ = ["AppState", "get_client", "get_pending"]


@dataclass
class AppState:
    """Lifespan state: the single long-lived client bound to the server loop.

    ``pending`` is the process-lifetime provenance registry consulted by the
    source / artifact poll handlers (see :mod:`._pending`).
    """

    client: NotebookLMClient
    pending: PendingRegistry


def get_client(request: Request) -> NotebookLMClient:
    """Return the lifespan-bound client for the current request.

    The client is stowed on ``app.state`` by the lifespan in :mod:`.app`; it is
    always present during a real request (the lifespan runs before any request
    is served).

    Raises:
        RuntimeError: If no client was bound (the lifespan did not run — should
            never happen during a real request).
    """
    return _state(request).client


def get_pending(request: Request) -> PendingRegistry:
    """Return the process-lifetime pending-id registry for the current request."""
    return _state(request).pending


def _state(request: Request) -> AppState:
    state: AppState | None = getattr(request.app.state, "notebooklm", None)
    if state is None:  # pragma: no cover - lifespan always binds before requests
        raise RuntimeError("no client bound to the server (lifespan did not run)")
    return state
