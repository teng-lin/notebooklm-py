"""U1: app scaffold, lifespan, healthz, and the disabled schema surface."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from fastapi.testclient import TestClient

from notebooklm.client import NotebookLMClient
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


def test_create_app_default_factory_threads_profile(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """#1769: create_app(profile=X) must reach from_storage(profile=X) through the real
    default-factory closure — not just the create_app boundary. Guards the mocked-boundary
    blind spot: a regression to `lambda: _default_factory()` (dropping profile) would pass
    every test that mocks create_app itself.

    Patches the imported ``NotebookLMClient`` class object (not an import-string), which is
    the robust form the ADR-0007 no-forbidden-monkeypatch guard permits.
    """
    seen: dict[str, object] = {}

    @asynccontextmanager
    async def _fake_ctx() -> AsyncIterator[FakeClient]:
        yield FakeClient()

    def _spy_from_storage(**kwargs: object) -> AbstractAsyncContextManager[FakeClient]:
        seen.update(kwargs)
        return _fake_ctx()

    monkeypatch.setattr(NotebookLMClient, "from_storage", staticmethod(_spy_from_storage))
    app = create_app(profile="work")  # no client_factory → exercises the default factory
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
    assert seen == {"profile": "work"}


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
