"""FastAPI application factory for the single-tenant REST server.

Design highlights (see ``docs/plans/2026-06-10-002-feat-rest-api-server-plan.md``):

- **One client per process, bound at lifespan.** The ASGI lifespan opens a
  single :class:`~notebooklm.client.NotebookLMClient` via ``from_storage()``
  inside the server loop (satisfies the ADR-0004 loop-affinity contract) and
  stows it on ``app.state`` for the process lifetime. Its keepalive task gives
  long sessions cookie rotation for free.
- **Transport-neutral.** Routes are thin adapters over the ``_app/`` cores and
  the public client namespaces; this package imports NO ``click`` / ``rich`` /
  ``cli`` (enforced by ``tests/_guardrails/test_server_boundary.py``).
- **No unauthenticated schema surface.** FastAPI mounts ``/docs`` / ``/redoc`` /
  ``/openapi.json`` *outside* the ``/v1`` auth dependency and *unauthenticated*
  by default. A server fronting account credentials must not expose its surface
  tokenless, so all three are disabled.
- **``/healthz`` is public, ``/v1`` is authed.** Health lives outside ``/v1`` so
  a liveness probe needs no token; it returns only ``{"ok": true}`` (no version
  or account info). Every ``/v1`` route is gated by the bearer-token +
  loopback-Host dependency (see :mod:`._auth`).

This module imports NO ``click`` / ``rich`` / ``cli``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import cast

from fastapi import APIRouter, Depends, FastAPI

from ..client import NotebookLMClient
from ._auth import require_auth
from ._context import AppState
from ._errors import install_exception_handlers
from ._pending import PendingRegistry
from .routes import artifacts, chat, notebooks, sources

__all__ = ["SERVER_NAME", "create_app"]

SERVER_NAME = "notebooklm-server"

#: A factory returns an async-context-manager that yields the client. The default
#: factory binds ``NotebookLMClient.from_storage()``; tests inject a factory
#: yielding a fake client so no real auth/network is needed.
ClientFactory = Callable[[], AbstractAsyncContextManager[NotebookLMClient]]


def _default_factory() -> AbstractAsyncContextManager[NotebookLMClient]:
    # ``from_storage`` returns a dual awaitable / async-context-manager; we use
    # only the async-context-manager protocol (the canonical, non-deprecated path).
    return cast(
        "AbstractAsyncContextManager[NotebookLMClient]",
        NotebookLMClient.from_storage(),
    )


def create_app(*, client_factory: ClientFactory | None = None) -> FastAPI:
    """Build the FastAPI application.

    Args:
        client_factory: Test seam — a zero-arg callable returning an async
            context manager that yields a client. Defaults to
            ``NotebookLMClient.from_storage()``.

    Returns:
        A configured :class:`~fastapi.FastAPI` app whose lifespan binds exactly
        one client, with the ``/v1`` resource routers (auth-gated) and a public
        ``/healthz`` mounted.
    """
    factory = client_factory or _default_factory

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with factory() as client:
            app.state.notebooklm = AppState(client=client, pending=PendingRegistry())
            try:
                yield
            finally:
                app.state.notebooklm = None

    app = FastAPI(
        title=SERVER_NAME,
        lifespan=lifespan,
        # Disable the unauthenticated schema surface (FastAPI mounts these
        # outside the /v1 auth dependency). A credential-fronting server must
        # not expose its surface tokenless.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    install_exception_handlers(app)

    @app.get("/healthz")
    async def healthz() -> dict[str, bool]:
        """Liveness probe — public, no token, no version/account info."""
        return {"ok": True}

    # Every /v1 route requires the bearer-token + loopback-Host dependency.
    v1 = APIRouter(prefix="/v1", dependencies=[Depends(require_auth)])
    v1.include_router(notebooks.router)
    v1.include_router(sources.router)
    v1.include_router(chat.router)
    v1.include_router(artifacts.router)
    app.include_router(v1)

    return app
