"""FastAPI application factory for the single-tenant REST server.

Design highlights:

- **One client per process, attempted at lifespan.** The ASGI lifespan opens a
  single :class:`~notebooklm.client.NotebookLMClient` via ``from_storage()``
  inside the server loop (satisfies the ADR-0004 loop-affinity contract) and
  stows it on ``app.state`` for the process lifetime. If startup auth is stale,
  the app records that failure so diagnostics can still be served.
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

import asyncio
import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import cast

logger = logging.getLogger(__name__)

from fastapi import APIRouter, Depends, FastAPI, Request, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..client import NotebookLMClient
from ..exceptions import AuthError, NotebookLMError
from ..paths import get_active_profile, resolve_profile, set_active_profile
from ._auth import require_auth
from ._context import AppState
from ._errors import http_error_response, install_exception_handlers
from ._limits import ServerLimiters
from ._pending import PendingRegistry
from .routes import artifacts, chat, meta, notebooks, notes, research, share, sources
from .routes.sources import MAX_UPLOAD_BYTES

__all__ = ["SERVER_NAME", "create_app"]

SERVER_NAME = "notebooklm-server"

DEFAULT_JSON_BODY_BYTES = 1024 * 1024
SOURCE_TEXT_JSON_BODY_BYTES = 10 * 1024 * 1024
NOTE_JSON_BODY_BYTES = 5 * 1024 * 1024
BATCH_JSON_BODY_BYTES = 256 * 1024
WAIT_JSON_BODY_BYTES = 64 * 1024
SHORT_JSON_BODY_BYTES = 16 * 1024
MEDIUM_JSON_BODY_BYTES = 64 * 1024

_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH"})
_NOTEBOOK_ID = r"[^/]+"
_RESOURCE_ID = r"[^/]+"
_FILE_UPLOAD_PATH = re.compile(rf"^/v1/notebooks/{_NOTEBOOK_ID}/sources/file$")
_NO_BODY_MUTATION_ROUTES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "POST",
        re.compile(rf"^/v1/notebooks/{_NOTEBOOK_ID}/artifacts/{_RESOURCE_ID}/retry$"),
    ),
    (
        "POST",
        re.compile(rf"^/v1/notebooks/{_NOTEBOOK_ID}/research/{_RESOURCE_ID}/import$"),
    ),
)


@dataclass(frozen=True)
class _BodyLimit:
    method: str
    path: re.Pattern[str]
    max_bytes: int
    name: str


JSON_BODY_LIMITS: tuple[_BodyLimit, ...] = (
    _BodyLimit("POST", re.compile(r"^/v1/notebooks$"), SHORT_JSON_BODY_BYTES, "notebook create"),
    _BodyLimit(
        "PATCH",
        re.compile(rf"^/v1/notebooks/{_NOTEBOOK_ID}$"),
        SHORT_JSON_BODY_BYTES,
        "notebook rename",
    ),
    _BodyLimit(
        "POST",
        re.compile(rf"^/v1/notebooks/{_NOTEBOOK_ID}/sources/url$"),
        MEDIUM_JSON_BODY_BYTES,
        "source URL add",
    ),
    _BodyLimit(
        "POST",
        re.compile(rf"^/v1/notebooks/{_NOTEBOOK_ID}/sources/text$"),
        SOURCE_TEXT_JSON_BODY_BYTES,
        "source text add",
    ),
    _BodyLimit(
        "POST",
        re.compile(rf"^/v1/notebooks/{_NOTEBOOK_ID}/sources/drive$"),
        MEDIUM_JSON_BODY_BYTES,
        "source Drive add",
    ),
    _BodyLimit(
        "POST",
        re.compile(rf"^/v1/notebooks/{_NOTEBOOK_ID}/sources/batch$"),
        BATCH_JSON_BODY_BYTES,
        "source URL batch add",
    ),
    _BodyLimit(
        "POST",
        re.compile(rf"^/v1/notebooks/{_NOTEBOOK_ID}/sources/wait$"),
        WAIT_JSON_BODY_BYTES,
        "source wait",
    ),
    _BodyLimit(
        "PATCH",
        re.compile(rf"^/v1/notebooks/{_NOTEBOOK_ID}/sources/{_RESOURCE_ID}$"),
        SHORT_JSON_BODY_BYTES,
        "source rename",
    ),
    _BodyLimit(
        "POST",
        re.compile(rf"^/v1/notebooks/{_NOTEBOOK_ID}/notes$"),
        NOTE_JSON_BODY_BYTES,
        "note create",
    ),
    _BodyLimit(
        "PUT",
        re.compile(rf"^/v1/notebooks/{_NOTEBOOK_ID}/notes/{_RESOURCE_ID}$"),
        NOTE_JSON_BODY_BYTES,
        "note update",
    ),
    _BodyLimit(
        "POST",
        re.compile(rf"^/v1/notebooks/{_NOTEBOOK_ID}/chat$"),
        DEFAULT_JSON_BODY_BYTES,
        "chat ask",
    ),
    _BodyLimit(
        "POST",
        re.compile(rf"^/v1/notebooks/{_NOTEBOOK_ID}/chat/configure$"),
        MEDIUM_JSON_BODY_BYTES,
        "chat configure",
    ),
    _BodyLimit(
        "POST",
        re.compile(rf"^/v1/notebooks/{_NOTEBOOK_ID}/artifacts$"),
        DEFAULT_JSON_BODY_BYTES,
        "artifact generate",
    ),
    _BodyLimit(
        "PATCH",
        re.compile(rf"^/v1/notebooks/{_NOTEBOOK_ID}/artifacts/{_RESOURCE_ID}$"),
        SHORT_JSON_BODY_BYTES,
        "artifact rename",
    ),
    _BodyLimit(
        "POST",
        re.compile(rf"^/v1/notebooks/{_NOTEBOOK_ID}/artifacts/download$"),
        SHORT_JSON_BODY_BYTES,
        "artifact download",
    ),
    _BodyLimit(
        "POST",
        re.compile(rf"^/v1/notebooks/{_NOTEBOOK_ID}/research$"),
        DEFAULT_JSON_BODY_BYTES,
        "research start",
    ),
    _BodyLimit(
        "POST",
        re.compile(rf"^/v1/notebooks/{_NOTEBOOK_ID}/share/public$"),
        SHORT_JSON_BODY_BYTES,
        "share public",
    ),
    _BodyLimit(
        "POST",
        re.compile(rf"^/v1/notebooks/{_NOTEBOOK_ID}/share/users$"),
        MEDIUM_JSON_BODY_BYTES,
        "share user add",
    ),
    _BodyLimit(
        "PATCH",
        re.compile(rf"^/v1/notebooks/{_NOTEBOOK_ID}/share/users/{_RESOURCE_ID}$"),
        SHORT_JSON_BODY_BYTES,
        "share user update",
    ),
    _BodyLimit(
        "POST",
        re.compile(rf"^/v1/notebooks/{_NOTEBOOK_ID}/share/view-level$"),
        SHORT_JSON_BODY_BYTES,
        "share view-level",
    ),
)


def _media_type(content_type: str) -> str:
    return content_type.split(";", 1)[0].strip().lower()


def _is_json_content_type(content_type: str) -> bool:
    media_type = _media_type(content_type)
    return media_type == "application/json" or media_type.endswith("+json")


def _is_no_body_mutation_route(method: str, path: str) -> bool:
    method = method.upper()
    return any(
        route_method == method and route_path.fullmatch(path)
        for route_method, route_path in _NO_BODY_MUTATION_ROUTES
    )


def _json_body_limit(method: str, path: str, content_type: str) -> _BodyLimit | None:
    method = method.upper()
    for limit in JSON_BODY_LIMITS:
        if method == limit.method and limit.path.fullmatch(path):
            return limit
    if _is_no_body_mutation_route(method, path):
        return None
    if (
        method in _MUTATING_METHODS
        and path.startswith("/v1/")
        and _is_json_content_type(content_type)
    ):
        return _BodyLimit(method, re.compile(r".*"), DEFAULT_JSON_BODY_BYTES, "JSON")
    return None


def _is_file_upload_route(method: str, path: str) -> bool:
    return method.upper() == "POST" and _FILE_UPLOAD_PATH.fullmatch(path) is not None


def _parse_content_length(value: str) -> int | None:
    try:
        declared = int(value)
    except ValueError:
        return None
    return declared if declared >= 0 else None


#: A factory returns an async-context-manager that yields the client. The default
#: factory binds ``NotebookLMClient.from_storage()``; tests inject a factory
#: yielding a fake client so no real auth/network is needed.
ClientFactory = Callable[[], AbstractAsyncContextManager[NotebookLMClient]]

_STALE_AUTH_STARTUP_MARKERS = (
    "authentication expired",
    "authentication expired or invalid",
    "run 'notebooklm login'",
)


def _default_factory(profile: str | None = None) -> AbstractAsyncContextManager[NotebookLMClient]:
    # ``from_storage`` returns a dual awaitable / async-context-manager; we use
    # only the async-context-manager protocol (the canonical, non-deprecated path).
    return cast(
        "AbstractAsyncContextManager[NotebookLMClient]",
        NotebookLMClient.from_storage(profile=profile),
    )


def _normalize_client_startup_error(exc: Exception) -> AuthError | None:
    """Project stale auth bootstrap ``ValueError``s onto the library auth category.

    The auth bootstrap path historically raises plain ``ValueError`` for stale
    local profiles. Keep that compatibility at the SDK layer; the REST server
    only normalizes the exception it records in app state so its existing error
    projector can return an auth envelope instead of a generic unexpected bug.
    """
    if isinstance(exc, AuthError):
        return AuthError(str(exc))
    if isinstance(exc, NotebookLMError):
        return None
    if isinstance(exc, ValueError):
        message = " ".join(str(exc).split()).casefold()
        if any(marker in message for marker in _STALE_AUTH_STARTUP_MARKERS):
            return AuthError(str(exc))
    return None


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
        pending = PendingRegistry()
        client_started = False
        try:
            limiters = ServerLimiters.from_env()
            limiters.set_bound_loop(asyncio.get_running_loop())
            limiters.reset_after_open()
            try:
                async with factory() as client:
                    client_started = True
                    app.state.notebooklm = AppState(
                        client=client,
                        pending=pending,
                        limiters=limiters,
                    )
                    try:
                        yield
                    finally:
                        app.state.notebooklm = None
            except Exception as exc:
                if client_started:
                    raise
                startup_error = _normalize_client_startup_error(exc)
                if startup_error is None:
                    raise
                app.state.notebooklm = AppState(
                    client=None,
                    pending=pending,
                    limiters=limiters,
                    client_error=startup_error,
                )
                try:
                    yield
                finally:
                    app.state.notebooklm = None
        finally:
            set_active_profile(previous_profile)
            try:
                from .routes.middleware import cleanup_old_logs
                asyncio.ensure_future(asyncio.to_thread(cleanup_old_logs))
            except Exception:
                pass

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

    try:
        from .database import init_db as _init_server_db
        _init_server_db()
    except Exception as exc:
        logger.warning("Failed to initialize database: %s", exc)

    from .routes.middleware import RequestLogMiddleware
    app.add_middleware(RequestLogMiddleware)

    @app.middleware("http")
    async def _limit_request_body(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        # Reject oversized request bodies by declared Content-Length BEFORE the
        # route reads/parses them. Multipart keeps the large upload cap; JSON
        # mutation routes get much smaller route-specific caps so a caller cannot
        # allocate upload-sized Pydantic payloads.
        content_type = request.headers.get("content-type", "")
        path = request.scope.get("path", request.url.path)
        content_length = request.headers.get("content-length")
        if _is_file_upload_route(request.method, path):
            # A chunked (no-Content-Length) upload request would otherwise let
            # Starlette parse or spool the full request before any per-chunk cap
            # runs. Require an up-front declared length for the upload route so
            # the size can be bounded before parsing starts.
            if content_length is None:
                return http_error_response(411, "Content-Length is required for uploads")
            declared = _parse_content_length(content_length)
            if declared is None:
                return http_error_response(411, "A valid Content-Length is required for uploads")
            if declared > MAX_UPLOAD_BYTES:
                return http_error_response(413, "Request body exceeds the size limit")
            try:
                await require_auth(request)
            except StarletteHTTPException as exc:
                return http_error_response(exc.status_code, exc.detail)
            state = getattr(request.app.state, "notebooklm", None)
            if state is not None:
                async with state.limiters.acquire("source_mutation"):
                    return await call_next(request)
        elif limit := _json_body_limit(request.method, path, content_type):
            # Without Content-Length, a chunked JSON body could exceed the cap
            # while FastAPI is already buffering/parsing it. Require the same
            # predeclared length contract as multipart for body-limited routes.
            if content_length is None:
                return http_error_response(
                    411, "Content-Length is required for JSON request bodies"
                )
            declared = _parse_content_length(content_length)
            if declared is None:
                return http_error_response(
                    411, "A valid Content-Length is required for JSON request bodies"
                )
            if declared > limit.max_bytes:
                return http_error_response(413, f"{limit.name} request body exceeds the size limit")
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

    try:
        from .routes import auth as auth_routes
        app.include_router(auth_routes.router)
    except Exception as exc:
        logger.warning("Failed to register auth routes: %s", exc)

    return app
