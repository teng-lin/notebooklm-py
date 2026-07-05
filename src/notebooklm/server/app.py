"""FastAPI application factory for the single-tenant REST server.

Design highlights:

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

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import cast

from fastapi import APIRouter, Depends, FastAPI, Request, Response

from ..client import NotebookLMClient
from ..paths import get_active_profile, resolve_profile, set_active_profile
from ._auth import require_auth
from ._context import AppState
from ._errors import http_error_response, install_exception_handlers
from ._pending import PendingRegistry
from .routes import artifacts, chat, meta, notebooks, notes, research, share, sources
from .routes.sources import MAX_UPLOAD_BYTES

__all__ = ["SERVER_NAME", "create_app"]

SERVER_NAME = "notebooklm-server"

#: A factory returns an async-context-manager that yields the client. The default
#: factory binds ``NotebookLMClient.from_storage()``; tests inject a factory
#: yielding a fake client so no real auth/network is needed.
ClientFactory = Callable[[], AbstractAsyncContextManager[NotebookLMClient]]


def _default_factory(profile: str | None = None) -> AbstractAsyncContextManager[NotebookLMClient]:
    # ``from_storage`` returns a dual awaitable / async-context-manager; we use
    # only the async-context-manager protocol (the canonical, non-deprecated path).
    return cast(
        "AbstractAsyncContextManager[NotebookLMClient]",
        NotebookLMClient.from_storage(profile=profile),
    )


def create_app(
    *, profile: str | None = None, client_factory: ClientFactory | None = None
) -> FastAPI:
    """Build the FastAPI application.

    Args:
        profile: Auth profile bound by the default factory (``from_storage(profile=)``).
            ``None`` resolves the active profile. Also drives process-wide profile
            resolution for diagnostics such as ``/v1/server/info``.
        client_factory: Test seam — a zero-arg callable returning an async
            context manager that yields a client. Defaults to
            ``NotebookLMClient.from_storage(profile=profile)``.

    Returns:
        A configured :class:`~fastapi.FastAPI` app whose lifespan binds exactly
        one client, with the ``/v1`` resource routers (auth-gated) and a public
        ``/healthz`` mounted.
    """
    factory = client_factory or (lambda: _default_factory(profile))

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        previous_profile = get_active_profile()
        set_active_profile(resolve_profile(profile))
        try:
            async with factory() as client:
                app.state.notebooklm = AppState(client=client, pending=PendingRegistry())
                try:
                    yield
                finally:
                    app.state.notebooklm = None
        finally:
            set_active_profile(previous_profile)

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

    @app.middleware("http")
    async def _limit_request_body(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        # Reject an oversized upload by its declared Content-Length BEFORE the
        # route reads/parses the body — Starlette's multipart parser spools file
        # parts to disk unbounded, so a post-parse check is too late (the body is
        # already on disk). This Content-Length pre-check is the actual disk-
        # exhaustion mitigation. Nothing legitimate exceeds the upload cap, so
        # applying it to every request is safe.
        content_type = request.headers.get("content-type", "")
        is_multipart = content_type.lstrip().lower().startswith("multipart/form-data")
        content_length = request.headers.get("content-length")
        if content_length is None:
            # A chunked (no-Content-Length) multipart upload would otherwise let
            # Starlette spool the full part to disk before any per-chunk cap runs.
            # Require an up-front declared length for multipart so the size can be
            # bounded before a byte is spooled; other verbs (GET/JSON) are
            # unaffected. Route through the shared projector for a uniform envelope.
            if is_multipart:
                return http_error_response(411, "Content-Length is required for multipart uploads")
        else:
            try:
                declared = int(content_length)
            except ValueError:
                declared = -1
            if declared < 0 and is_multipart:
                return http_error_response(411, "A valid Content-Length is required for uploads")
            if declared > MAX_UPLOAD_BYTES:
                # Route through the shared projector so the envelope shape +
                # lowercase category match every other error response.
                return http_error_response(413, "Request body exceeds the size limit")
        return await call_next(request)

    @app.get("/healthz")
    async def healthz() -> dict[str, bool]:
        """Liveness probe — public, no token, no version/account info."""
        return {"ok": True}

    # Every /v1 route requires the bearer-token + loopback-Host dependency.
    v1 = APIRouter(prefix="/v1", dependencies=[Depends(require_auth)])
    v1.include_router(notebooks.router)
    v1.include_router(sources.router)
    v1.include_router(notes.router)
    v1.include_router(chat.router)
    v1.include_router(artifacts.router)
    v1.include_router(research.router)
    v1.include_router(share.router)
    v1.include_router(meta.router)
    app.include_router(v1)

    return app
