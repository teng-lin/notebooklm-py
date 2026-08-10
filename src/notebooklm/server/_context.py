"""Per-request access to the lifespan-bound client.

The REST server binds exactly one
:class:`~notebooklm.client.NotebookLMClient` for the process lifetime via the
ASGI lifespan (one client, bound to the server's event loop, satisfying the
ADR-0004 loop-affinity contract). Route handlers reach it through the
:func:`get_client` FastAPI dependency, so they never touch app-state internals
directly. If startup could not bind a live client, diagnostics can still inspect
the recorded failure while the next client-dependent request retries the bind;
concurrent requests share the same attempt generation.

This module imports NO ``click`` / ``rich`` / ``cli``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from fastapi import Request

from ._limits import LimitGroup, ServerLimiters
from ._pending import PendingRegistry

if TYPE_CHECKING:
    from ..client import NotebookLMClient

__all__ = [
    "AppState",
    "get_client",
    "get_client_error",
    "get_pending",
    "limit_chat",
    "limit_download",
    "limit_generation",
    "limit_research",
    "limit_source_mutation",
    "limit_source_wait",
]


@dataclass
class AppState:
    """Lifespan state: the single long-lived client bound to the server loop.

    ``pending`` is the process-lifetime provenance registry consulted by the
    source / artifact poll handlers (see :mod:`._pending`).

    ``client_loader`` is installed only by the application lifespan. It binds
    at most one client and lets a stale-auth startup recover after another
    process refreshes the selected profile.
    """

    client: NotebookLMClient | None
    pending: PendingRegistry
    limiters: ServerLimiters
    client_error: BaseException | None = None
    client_loader: Callable[[int], Awaitable[NotebookLMClient]] | None = None
    client_generation: int = 0


async def get_client(request: Request) -> NotebookLMClient:
    """Return the lifespan-bound client for the current request.

    The client, or the retryable startup failure that prevented creating it, is
    stowed on ``app.state`` by the lifespan in :mod:`.app`.

    Raises:
        RuntimeError: If no client was bound (the lifespan did not run — should
            never happen during a real request).
    """
    state = _state(request)
    if state.client is not None:
        return state.client
    if state.client_loader is not None:
        observed_generation = getattr(
            request.state,
            "notebooklm_client_generation",
            state.client_generation,
        )
        return await state.client_loader(observed_generation)
    if state.client_error is not None:
        raise _fresh_exception(state.client_error)
    raise RuntimeError("no client bound to the server")  # pragma: no cover


def get_client_error(request: Request) -> BaseException | None:
    """Return the startup failure that prevented binding a live client, if any."""
    error = _state(request).client_error
    return _fresh_exception(error) if error is not None else None


def get_pending(request: Request) -> PendingRegistry:
    """Return the process-lifetime pending-id registry for the current request."""
    return _state(request).pending


async def limit_source_mutation(request: Request) -> AsyncIterator[None]:
    """Backpressure source create/rename/delete routes."""
    async with _limit(request, "source_mutation"):
        yield


async def limit_source_wait(request: Request) -> AsyncIterator[None]:
    """Backpressure source wait routes."""
    async with _limit(request, "source_wait"):
        yield


async def limit_generation(request: Request) -> AsyncIterator[None]:
    """Backpressure artifact generation routes."""
    async with _limit(request, "generation"):
        yield


async def limit_download(request: Request) -> AsyncIterator[None]:
    """Backpressure artifact download routes."""
    async with _limit(request, "download"):
        yield


async def limit_research(request: Request) -> AsyncIterator[None]:
    """Backpressure research mutation/import routes."""
    async with _limit(request, "research"):
        yield


async def limit_chat(request: Request) -> AsyncIterator[None]:
    """Backpressure blocking chat ask routes."""
    async with _limit(request, "chat"):
        yield


@asynccontextmanager
async def _limit(request: Request, group: LimitGroup) -> AsyncIterator[None]:
    async with _state(request).limiters.acquire(group):
        yield


def _state(request: Request) -> AppState:
    state: AppState | None = getattr(request.app.state, "notebooklm", None)
    if state is None:  # pragma: no cover - lifespan always binds before requests
        raise RuntimeError("no client bound to the server (lifespan did not run)")
    return state


def _fresh_exception(exc: BaseException) -> BaseException:
    """Clone a stored startup error so repeated requests do not mutate traceback state."""
    return exc.__class__(*exc.args)
