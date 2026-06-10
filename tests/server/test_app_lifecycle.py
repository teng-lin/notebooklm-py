"""U1: app scaffold, lifespan, healthz, and the disabled schema surface."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi.testclient import TestClient

from notebooklm.server.app import create_app

from .fakes import FakeClient


def test_healthz_is_public_and_minimal() -> None:
    """GET /healthz (outside /v1, no token) returns exactly {"ok": true}."""
    app = create_app(client_factory=_factory(FakeClient()))
    with TestClient(app) as client:
        resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_lifespan_opens_exactly_one_client_and_closes_it() -> None:
    """The lifespan opens the client once on startup and closes it on shutdown."""
    fake = FakeClient()
    opens = 0
    closed = False

    @asynccontextmanager
    async def factory() -> AsyncIterator[FakeClient]:
        nonlocal opens, closed
        opens += 1
        try:
            yield fake
        finally:
            closed = True

    app = create_app(client_factory=factory)
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        assert opens == 1
        assert closed is False
    # Context exit shuts the lifespan down.
    assert opens == 1
    assert closed is True


def test_docs_and_openapi_are_disabled() -> None:
    """The unauthenticated schema UI is off (no tokenless surface)."""
    app = create_app(client_factory=_factory(FakeClient()))
    with TestClient(app) as client:
        assert client.get("/docs").status_code == 404
        assert client.get("/redoc").status_code == 404
        assert client.get("/openapi.json").status_code == 404


def _factory(client: FakeClient):  # type: ignore[no-untyped-def]
    @asynccontextmanager
    async def factory() -> AsyncIterator[FakeClient]:
        yield client

    return factory
