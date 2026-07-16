"""Baoku clone REST server — multi-user, JWT-authenticated, CORS-enabled.

This server runs separately from the existing ``notebooklm-server`` (which is a
single-user /v1 NotebookLM proxy).  The baoku server provides its own auth,
notebook, source, chat, generation, and external-KB APIs under ``/api/*``.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.responses import FileResponse

from .database import init_db
from .routes import auth, chat, external_kb, generation, notebooks, sources

SERVER_NAME = "baoku-server"

_frontend_dist = os.path.join(os.path.dirname(__file__), "..", "..", "..", "frontend", "dist")


def create_app() -> FastAPI:
    dev_mode = os.environ.get("BAOKU_DEV", "0") == "1"

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        init_db()
        yield

    app = FastAPI(
        title=SERVER_NAME,
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
    )

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

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    if not dev_mode and os.path.isdir(_frontend_dist):
        app.mount("/assets", StaticFiles(directory=os.path.join(_frontend_dist, "assets")), name="assets")

        @app.get("/{full_path:path}")
        async def serve_spa(full_path: str) -> FileResponse:
            index = os.path.join(_frontend_dist, "index.html")
            return FileResponse(index, media_type="text/html")

    return app
