"""Baoku clone REST server — multi-user, JWT-authenticated, CORS-enabled.

This server runs separately from the existing ``notebooklm-server`` (which is a
single-user /v1 NotebookLM proxy).  The baoku server provides its own auth,
notebook, source, chat, generation, and external-KB APIs under ``/api/*``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.responses import FileResponse
from starlette.status import HTTP_503_SERVICE_UNAVAILABLE

from notebooklm.client import NotebookLMClient

from ._context import AppState, PendingRegistry, ServerLimiters
from ._errors import install_exception_handlers
from .auth_deps import get_current_user
from .database import init_db
from .models import User
from .routes import (
    auth,
    baoku_chat_sessions,
    baoku_notes,
    chat,
    external_kb,
    generation,
    notebooks,
    sources,
)

SERVER_NAME = "baoku-server"
logger = logging.getLogger(__name__)

_frontend_dist = os.path.join(os.path.dirname(__file__), "..", "..", "..", "frontend", "dist")

_PROFILES_DIR = Path.home() / ".notebooklm" / "profiles"


def _profile_storage_path(profile_name: str) -> Path:
    return _PROFILES_DIR / profile_name / "storage_state.json"


def _make_state(
    client: NotebookLMClient | None,
    pending: PendingRegistry,
    limiters: ServerLimiters,
    client_error: BaseException | None,
) -> AppState:
    return AppState(client=client, pending=pending, limiters=limiters, client_error=client_error)


def create_app() -> FastAPI:
    dev_mode = os.environ.get("BAOKU_DEV", "0") == "1"

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        init_db()
        pending = PendingRegistry()
        limiters = ServerLimiters.from_env()

        app.state.notebooklm = _make_state(
            client=None, pending=pending, limiters=limiters,
            client_error=RuntimeError("NotebookLM client is initializing..."),
        )

        async def _init_client() -> None:
            storage_path = _profile_storage_path("default")
            if not storage_path.exists():
                app.state.notebooklm = _make_state(
                    client=None, pending=pending, limiters=limiters,
                    client_error=RuntimeError(
                        "NotebookLM client is not configured. "
                        "Go to Settings → NotebookLM 连接 to upload your cookies."
                    ),
                )
                return
            try:
                ctx = NotebookLMClient.from_storage(path=str(storage_path), timeout=60)
                client = await ctx.__aenter__()
                app.state._nlm_ctx = ctx
                app.state.notebooklm = _make_state(
                    client=client, pending=pending, limiters=limiters, client_error=None,
                )
                logger.info("NotebookLM client connected successfully")
            except Exception as exc:
                app.state.notebooklm = _make_state(
                    client=None, pending=pending, limiters=limiters,
                    client_error=RuntimeError(str(exc) or type(exc).__name__),
                )
                logger.warning(
                    "NotebookLM client connection failed: %s: %s",
                    type(exc).__name__, exc,
                )

        task = asyncio.ensure_future(_init_client())

        yield

        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        ctx = getattr(app.state, "_nlm_ctx", None)
        if ctx:
            try:
                await ctx.__aexit__(None, None, None)
            except Exception:
                pass
        app.state.notebooklm = None

    app = FastAPI(
        title=SERVER_NAME,
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
    )

    install_exception_handlers(app)

    if dev_mode:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["http://localhost:5173"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    else:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=[],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    app.include_router(auth.router)
    app.include_router(generation.router)
    app.include_router(external_kb.router)
    app.include_router(notebooks.router, prefix="/api")
    app.include_router(sources.router, prefix="/api")
    app.include_router(chat.router, prefix="/api")
    app.include_router(baoku_notes.router, prefix="/api")
    app.include_router(baoku_chat_sessions.router, prefix="/api")

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/settings/notebooklm-status")
    async def notebooklm_status(request: Request) -> dict[str, str | bool | None]:
        state = getattr(request.app.state, "notebooklm", None)
        if state is None or state.client is None:
            err = state.client_error if state and state.client_error else None
            return {"connected": False, "error": str(err) if err else "Not configured"}
        return {"connected": True, "error": None}

    @app.post("/api/settings/notebooklm-connect")
    async def notebooklm_connect(
        request: Request,
        current_user: User = Depends(get_current_user),
    ) -> dict[str, str | bool]:
        body = await request.json()
        cookies_json = body.get("cookies_json", "")
        profile_name = body.get("profile_name", "default")

        if not cookies_json.strip():
            raise HTTPException(400, "cookies_json is required")

        try:
            json.loads(cookies_json)
        except json.JSONDecodeError:
            raise HTTPException(400, "cookies_json is not valid JSON") from None

        storage_path = _profile_storage_path(profile_name)
        storage_path.parent.mkdir(parents=True, exist_ok=True)
        storage_path.write_text(cookies_json)

        old_state = getattr(request.app.state, "notebooklm", None)
        if old_state and old_state.client:
            try:
                await old_state.client.aclose()
            except Exception:
                pass

        try:
            ctx = NotebookLMClient.from_storage(
                path=str(storage_path),
                timeout=60,
            )
            client = await ctx.__aenter__()
            request.app.state._nlm_ctx = ctx

            pending = PendingRegistry()
            limiters = ServerLimiters.from_env()
            import asyncio

            loop = asyncio.get_running_loop()
            limiters.set_bound_loop(loop)

            request.app.state.notebooklm = AppState(
                client=client,
                pending=pending,
                limiters=limiters,
            )
            return {"success": True, "connected": True}
        except Exception as exc:
            err_msg = str(exc)
            request.app.state.notebooklm = AppState(
                client=None,
                pending=PendingRegistry(),
                limiters=ServerLimiters.from_env(),
                client_error=RuntimeError(err_msg),
            )
            raise HTTPException(
                HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Failed to connect: {err_msg}",
            ) from exc

    @app.post("/api/settings/notebooklm-retry")
    async def notebooklm_retry(
        request: Request,
        current_user: User = Depends(get_current_user),
    ) -> dict[str, str | bool | None]:
        storage_path = _profile_storage_path("default")
        if not storage_path.exists():
            return {"connected": False, "error": "No storage_state.json found. Run `notebooklm login` first."}

        old_state = getattr(request.app.state, "notebooklm", None)
        if old_state and old_state.client:
            try:
                await old_state.client.aclose()
            except Exception:
                pass

        pending = PendingRegistry()
        limiters = ServerLimiters.from_env()
        limiters.set_bound_loop(asyncio.get_running_loop())

        try:
            ctx = NotebookLMClient.from_storage(path=str(storage_path), timeout=60)
            client = await ctx.__aenter__()
            request.app.state._nlm_ctx = ctx
            request.app.state.notebooklm = _make_state(
                client=client, pending=pending, limiters=limiters, client_error=None,
            )
            logger.info("NotebookLM client reconnected successfully")
            return {"connected": True, "error": None}
        except Exception as exc:
            err_msg = str(exc) or type(exc).__name__
            request.app.state.notebooklm = _make_state(
                client=None, pending=pending, limiters=limiters,
                client_error=RuntimeError(err_msg),
            )
            logger.warning("NotebookLM reconnect failed: %s: %s", type(exc).__name__, exc)
            return {"connected": False, "error": err_msg}

    if not dev_mode and os.path.isdir(_frontend_dist):
        app.mount("/assets", StaticFiles(directory=os.path.join(_frontend_dist, "assets")), name="assets")

        @app.get("/{full_path:path}")
        async def serve_spa(full_path: str) -> FileResponse:
            index = os.path.join(_frontend_dist, "index.html")
            return FileResponse(index, media_type="text/html")

    return app
