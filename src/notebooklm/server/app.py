"""FastAPI application factory for the local REST server.

Design highlights:

- **Single-profile default, optional Android profiles.** Multi-profile mode
  binds one isolated client and pending registry per configured profile.
  Route-group limiters remain shared by the process. The default mode opens
  a single :class:`~notebooklm.client.NotebookLMClient` via ``from_storage()``
  inside the server loop (satisfies the ADR-0004 loop-affinity contract) and
  stows it on ``app.state`` for the process lifetime. Its 600-second keepalive
  rotates cookies while the server runs. If startup auth is stale, the app
  keeps diagnostics available and retries the single client bind on the next
  client-dependent request.
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
import os
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import Literal, cast

from fastapi import APIRouter, Depends, FastAPI, Request, Response
from starlette._utils import get_route_path
from starlette.exceptions import HTTPException as StarletteHTTPException

from .._adapter_support import DEFAULT_SERVER_KEEPALIVE_INTERVAL
from .._app.android_profiles import android_profile_client
from ..client import NotebookLMClient
from ..exceptions import AuthError, NotebookLMError
from ..paths import get_active_profile, resolve_profile, set_active_profile
from ._auth import require_auth
from ._context import AppState, ProfileRegistry, get_state, require_profile
from ._errors import http_error_response, install_exception_handlers
from ._limits import ServerLimiters
from ._pending import PendingRegistry
from ._profiles import PROFILE_HEADER, configured_profiles, profile_startup_timeout
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
ProfileClientFactory = Callable[[str], AbstractAsyncContextManager[NotebookLMClient]]
logger = logging.getLogger(__name__)

_STALE_AUTH_STARTUP_MARKERS = (
    "authentication expired",
    "authentication expired or invalid",
    "run 'notebooklm login'",
)

# Preserve one immediate request-time retry after a degraded startup, then
# bound repeated full client bootstraps while the profile remains stale. Each
# bootstrap can perform a homepage fetch (and operator-enabled recovery), so an
# unbounded retry per request would amplify both latency and upstream traffic.
_SERVER_AUTH_RETRY_INTERVAL_SECONDS = 5.0


def _default_factory(
    profile: str | None = None,
    backend: Literal["web", "android"] | None = None,
) -> AbstractAsyncContextManager[NotebookLMClient]:
    # ``from_storage`` returns a dual awaitable / async-context-manager; we use
    # only the async-context-manager protocol (the canonical, non-deprecated path).
    from .._app.client_config import adapter_client_config

    return cast(
        "AbstractAsyncContextManager[NotebookLMClient]",
        NotebookLMClient.from_storage(
            profile=profile,
            config=adapter_client_config(
                backend=backend, keepalive=DEFAULT_SERVER_KEEPALIVE_INTERVAL
            ),
        ),
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
    *,
    profile: str | None = None,
    profiles: Sequence[str] | None = None,
    profile_client_factory: ProfileClientFactory | None = None,
    backend: Literal["web", "android"] | None = None,
    client_factory: ClientFactory | None = None,
    _download_temp_factory: Callable[[], str] | None = None,
) -> FastAPI:
    """Build the FastAPI application.

    Args:
        profile: Auth profile bound by the default factory (``from_storage(profile=)``).
            ``None`` resolves the active profile. Also drives process-wide profile
            resolution for diagnostics such as ``/v1/server/info``.
        profiles: Static explicit profile names. More than one requires Android
            and the X-NotebookLM-Profile header on every /v1 request. Duplicate
            canonical paths are refused; copied master-token credentials are allowed.
        profile_client_factory: Multi-profile test seam, called with each name.
        backend: Preferred API backend for the default client factory. An explicit
            value takes precedence over ``NOTEBOOKLM_BACKEND``.
        client_factory: Test seam — a zero-arg callable returning an async
            context manager that yields a client. Defaults to
            ``NotebookLMClient.from_storage(profile=profile, keepalive=600.0)``.
        _download_temp_factory: Private per-app spool allocation seam. ``None``
            retains private directories allocated by ``tempfile.mkdtemp``.

    Returns:
        A configured :class:`~fastapi.FastAPI` app whose lifespan binds exactly
        one client per selected profile, with the ``/v1`` resource routers (auth-gated) and a public
        ``/healthz`` mounted.
    """
    if profiles is not None and profile is not None:
        raise ValueError("profile and profiles are mutually exclusive")
    profile_paths = configured_profiles(profiles) if profiles is not None else {}
    multi_profile = len(profile_paths) > 1
    if profile_paths and not multi_profile:
        profile = next(iter(profile_paths))
    selected_backend = backend or os.environ.get("NOTEBOOKLM_BACKEND", "web")
    if multi_profile and selected_backend != "android":
        raise ValueError("Multi-profile REST requires backend='android'")
    if multi_profile and client_factory is not None:
        raise ValueError("Use profile_client_factory for multi-profile clients")
    if not multi_profile and profile_client_factory is not None:
        raise ValueError("profile_client_factory requires multiple profiles")
    startup_timeout = profile_startup_timeout() if multi_profile else None
    factory = client_factory or (lambda: _default_factory(profile, backend))

    async def bind_state(
        name: str,
        factory: ClientFactory,
        clients: AsyncExitStack,
        limiters: ServerLimiters,
    ) -> AppState:
        state = AppState(
            client=None,
            pending=PendingRegistry(),
            limiters=limiters,
            profile=name,
            backend=selected_backend,
            isolated=multi_profile,
            storage_path=profile_paths.get(name),
        )
        client_lock = asyncio.Lock()
        last_load_error: AuthError | RuntimeError | None = None
        retry_not_before = 0.0

        async def load_client(
            observed_generation: int,
            *,
            startup: bool = False,
        ) -> NotebookLMClient:
            """Bind once and coalesce concurrent attempts by generation."""
            nonlocal last_load_error, retry_not_before
            async with client_lock:
                if state.client is not None:
                    return state.client
                if state.client_generation != observed_generation and last_load_error is not None:
                    raise last_load_error.__class__(str(last_load_error)) from None
                if last_load_error is not None and time.monotonic() < retry_not_before:
                    raise last_load_error.__class__(str(last_load_error)) from None
                try:
                    if multi_profile:
                        # Bound local credential inspection and readiness as one
                        # attempt. Keep timeout errors inside the loader so they
                        # receive the same diagnostics, generation, and cooldown.
                        client = await asyncio.wait_for(
                            clients.enter_async_context(factory()), timeout=startup_timeout
                        )
                    else:
                        client = await clients.enter_async_context(factory())
                except Exception as exc:
                    auth_error = _normalize_client_startup_error(exc)
                    if auth_error is None:
                        safe_error = RuntimeError(
                            "Client startup failed "
                            f"({type(exc).__name__}); retry temporarily rate-limited."
                        )
                        state.client_error = safe_error
                        last_load_error = safe_error
                        state.client_generation += 1
                        retry_not_before = (
                            0.0
                            if startup
                            else time.monotonic() + _SERVER_AUTH_RETRY_INTERVAL_SECONDS
                        )
                        raise
                    state.client_error = auth_error
                    last_load_error = auth_error
                    state.client_generation += 1
                    retry_not_before = (
                        0.0 if startup else time.monotonic() + _SERVER_AUTH_RETRY_INTERVAL_SECONDS
                    )
                    raise AuthError(str(auth_error)) from None
                state.client = client
                state.client_error = None
                last_load_error = None
                retry_not_before = 0.0
                state.client_generation += 1
                return client

        state.client_loader = load_client
        try:
            await load_client(state.client_generation, startup=True)
        except AuthError:
            pass
        except Exception:
            if not multi_profile:
                raise
            # A bad/missing profile must not prevent healthy siblings serving.
            # The loader retains only a sanitized diagnostic and retry state.
            logger.warning("Android profile %s is unavailable at startup", name)
        return state

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        previous_profile = get_active_profile()
        if not multi_profile:
            set_active_profile(resolve_profile(profile))
        try:
            # Route-group capacity is process-wide. Per-client RPC limits remain
            # independent; adding profiles must not multiply upload/chat capacity.
            limiters = ServerLimiters.from_env()
            limiters.set_bound_loop(asyncio.get_running_loop())
            limiters.reset_after_open()
            async with AsyncExitStack() as clients:
                if multi_profile:
                    registry = ProfileRegistry({})
                    bindings = []
                    for name, path in profile_paths.items():
                        selected_factory = (
                            (lambda name=name: profile_client_factory(name))
                            if profile_client_factory is not None
                            else (lambda path=path: android_profile_client(path))
                        )
                        bindings.append(
                            asyncio.create_task(
                                bind_state(name, selected_factory, clients, limiters)
                            )
                        )
                    try:
                        states = await asyncio.gather(*bindings)
                    finally:
                        # Settle all enters before the stack closes, including
                        # when startup is cancelled while another profile opens.
                        for binding in bindings:
                            if not binding.done():
                                binding.cancel()
                        await asyncio.gather(*bindings, return_exceptions=True)
                    registry.profiles.update(zip(profile_paths, states, strict=True))
                    app.state.notebooklm = registry
                else:
                    app.state.notebooklm = await bind_state(
                        resolve_profile(profile), factory, clients, limiters
                    )
                try:
                    yield
                finally:
                    app.state.notebooklm = None
        finally:
            if not multi_profile:
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
    app.state._download_temp_factory = _download_temp_factory

    install_exception_handlers(app)

    @app.middleware("http")
    async def _limit_request_body(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        state: AppState | ProfileRegistry | None = getattr(request.app.state, "notebooklm", None)
        if isinstance(state, AppState) and not hasattr(
            request.state, "notebooklm_client_generation"
        ):
            request.state.notebooklm_client_generation = state.client_generation
        if isinstance(state, ProfileRegistry) and get_route_path(request.scope).startswith("/v1/"):
            try:
                await require_auth(request)
                selected = get_state(request)
            except StarletteHTTPException as exc:
                rejection = http_error_response(
                    exc.status_code, exc.detail, code=getattr(exc, "code", None)
                )
                rejection.headers["Cache-Control"] = "no-store"
                rejection.headers["Vary"] = PROFILE_HEADER
                return rejection
            response = await _limit_profile_request(request, call_next, selected)
            return response
        return await _admit_body(request, call_next)

    async def _limit_profile_request(
        request: Request, call_next: Callable[[Request], Awaitable[Response]], state: AppState
    ) -> Response:
        response = await _admit_body(request, call_next)
        response.headers[PROFILE_HEADER] = state.profile or ""
        response.headers["Cache-Control"] = "no-store"
        vary = response.headers.get("Vary", "")
        response.headers["Vary"] = f"{vary}, {PROFILE_HEADER}" if vary else PROFILE_HEADER
        logger.info(
            "REST profile=%s method=%s status=%s",
            state.profile,
            request.method,
            response.status_code,
        )
        return response

    async def _admit_body(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        # Reject oversized request bodies by declared Content-Length BEFORE the
        # route reads/parses them. Multipart keeps the large upload cap; JSON
        # mutation routes get much smaller route-specific caps so a caller cannot
        # allocate upload-sized Pydantic payloads.
        content_type = request.headers.get("content-type", "")
        path = get_route_path(request.scope)
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
                return http_error_response(
                    exc.status_code, exc.detail, code=getattr(exc, "code", None)
                )
            try:
                state = get_state(request)
            except StarletteHTTPException as exc:
                return http_error_response(
                    exc.status_code, exc.detail, code=getattr(exc, "code", None)
                )
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
    v1 = APIRouter(prefix="/v1", dependencies=[Depends(require_auth), Depends(require_profile)])
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
