"""U1: app scaffold, lifespan, healthz, and the disabled schema surface."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractAsyncContextManager, asynccontextmanager

import pytest
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from notebooklm import paths
from notebooklm.client import NotebookLMClient
from notebooklm.server import app as app_module
from notebooklm.server.app import create_app

from .conftest import TEST_TOKEN, stale_auth_factory
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


def test_stale_auth_startup_still_serves_healthz(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale profile must not keep the process from serving liveness diagnostics."""
    monkeypatch.setattr(paths, "_active_profile", "before")

    app = create_app(profile="work", client_factory=stale_auth_factory())
    with TestClient(app) as client:
        resp = client.get("/healthz")
        assert paths.get_active_profile() == "work"

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert paths.get_active_profile() == "before"


def test_stale_auth_startup_projects_client_routes_to_auth_error() -> None:
    """Live-client routes fail through the structured auth envelope, not startup."""
    app = create_app(client_factory=stale_auth_factory())
    headers = {"Authorization": f"Bearer {TEST_TOKEN}", "Host": "127.0.0.1"}

    with TestClient(
        app, headers=headers, client=("127.0.0.1", 5555), raise_server_exceptions=False
    ) as client:
        resp = client.get("/v1/notebooks")

    assert resp.status_code == 401
    err = resp.json()["error"]
    assert err["category"] == "auth"
    assert err["retriable"] is False
    assert err["hint"] == "Re-authenticate and retry."


def test_stale_auth_startup_retries_and_binds_client_without_restart() -> None:
    """A later credential refresh lets the next client route heal the process."""
    fake = FakeClient()
    attempts = 0
    closed = False

    @asynccontextmanager
    async def factory() -> AsyncIterator[FakeClient]:
        nonlocal attempts, closed
        attempts += 1
        if attempts == 1:
            raise ValueError("Authentication expired. Run 'notebooklm login' to re-authenticate.")
        try:
            yield fake
        finally:
            closed = True

    app = create_app(client_factory=factory)
    headers = {"Authorization": f"Bearer {TEST_TOKEN}", "Host": "127.0.0.1"}
    with TestClient(
        app, headers=headers, client=("127.0.0.1", 5555), raise_server_exceptions=False
    ) as client:
        assert attempts == 1
        assert client.get("/healthz").status_code == 200

        first = client.get("/v1/notebooks")
        second = client.get("/v1/notebooks")

        assert first.status_code == 200
        assert second.status_code == 200
        assert attempts == 2
        assert app.state.notebooklm.client is fake
        assert app.state.notebooklm.client_error is None
        assert closed is False

    assert closed is True


def test_stale_auth_startup_raises_fresh_client_error_per_request() -> None:
    """The stored startup error must not grow tracebacks across repeated requests."""
    app = create_app(client_factory=stale_auth_factory())
    headers = {"Authorization": f"Bearer {TEST_TOKEN}", "Host": "127.0.0.1"}

    with TestClient(
        app, headers=headers, client=("127.0.0.1", 5555), raise_server_exceptions=False
    ) as client:
        stored_error = app.state.notebooklm.client_error
        assert stored_error is not None
        assert stored_error.__traceback__ is None

        assert client.get("/v1/notebooks").status_code == 401
        assert stored_error.__traceback__ is None
        assert app.state.notebooklm.client_error.__traceback__ is None

        assert client.get("/v1/notebooks").status_code == 401
        assert stored_error.__traceback__ is None
        assert app.state.notebooklm.client_error.__traceback__ is None


def test_concurrent_requests_share_one_failed_startup_retry() -> None:
    attempts = 0
    release_retry = threading.Event()
    retry_started = threading.Event()
    both_requests_started = threading.Event()
    request_count = 0

    @asynccontextmanager
    async def factory() -> AsyncIterator[FakeClient]:
        nonlocal attempts
        attempts += 1
        if attempts > 1:
            retry_started.set()
            await asyncio.to_thread(release_retry.wait)
        raise ValueError("Authentication expired. Run 'notebooklm login' to re-authenticate.")
        yield FakeClient()  # pragma: no cover - makes this an async context manager

    app = create_app(client_factory=factory)

    @app.middleware("http")
    async def count_requests(request, call_next):  # type: ignore[no-untyped-def]
        nonlocal request_count
        downstream = asyncio.create_task(call_next(request))
        if request.url.path == "/v1/notebooks":
            for _ in range(100):
                if hasattr(request.state, "notebooklm_client_generation"):
                    break
                await asyncio.sleep(0)
            else:
                downstream.cancel()
                raise AssertionError("request ingress did not capture the client generation")
            request_count += 1
            if request_count == 2:
                both_requests_started.set()
        return await downstream

    headers = {"Authorization": f"Bearer {TEST_TOKEN}", "Host": "127.0.0.1"}
    with (
        TestClient(
            app, headers=headers, client=("127.0.0.1", 5555), raise_server_exceptions=False
        ) as client,
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        first = pool.submit(client.get, "/v1/notebooks")
        second = pool.submit(client.get, "/v1/notebooks")
        try:
            assert retry_started.wait(timeout=2)
            assert both_requests_started.wait(timeout=2)
        finally:
            release_retry.set()
        responses = (first.result(timeout=2), second.result(timeout=2))

    assert [response.status_code for response in responses] == [401, 401]
    assert attempts == 2


def test_sequential_failed_startup_retries_are_rate_limited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0
    now = 100.0

    @asynccontextmanager
    async def factory() -> AsyncIterator[FakeClient]:
        nonlocal attempts
        attempts += 1
        raise ValueError("Authentication expired. Run 'notebooklm login' to re-authenticate.")
        yield FakeClient()  # pragma: no cover - makes this an async context manager

    monkeypatch.setattr(app_module.time, "monotonic", lambda: now)
    app = create_app(client_factory=factory)
    headers = {"Authorization": f"Bearer {TEST_TOKEN}", "Host": "127.0.0.1"}
    with TestClient(
        app, headers=headers, client=("127.0.0.1", 5555), raise_server_exceptions=False
    ) as client:
        assert attempts == 1

        assert client.get("/v1/notebooks").status_code == 401
        assert attempts == 2

        assert client.get("/v1/notebooks").status_code == 401
        assert attempts == 2

        now += app_module._SERVER_AUTH_RETRY_INTERVAL_SECONDS
        assert client.get("/v1/notebooks").status_code == 401
        assert attempts == 3


def test_non_auth_client_startup_failure_is_not_swallowed() -> None:
    """Only normalized auth bootstrap failures are degraded into diagnostic state."""

    @asynccontextmanager
    async def factory() -> AsyncIterator[FakeClient]:
        raise FileNotFoundError("missing storage_state.json")
        yield FakeClient()  # pragma: no cover - makes this an async context manager

    app = create_app(client_factory=factory)
    with pytest.raises(FileNotFoundError, match="missing storage_state.json"), TestClient(app):
        pass


def test_non_auth_retry_failure_preserves_and_rate_limits_structured_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StructuredStartupError(Exception):
        def __init__(self, message: str, *, code: int) -> None:
            super().__init__(message)
            self.code = code

    attempts = 0
    now = 100.0

    @asynccontextmanager
    async def factory() -> AsyncIterator[FakeClient]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ValueError("Authentication expired. Run 'notebooklm login' to re-authenticate.")
        raise StructuredStartupError("structured retry failure", code=503)
        yield FakeClient()  # pragma: no cover - makes this an async context manager

    app = create_app(client_factory=factory)

    @app.exception_handler(StructuredStartupError)
    async def structured_error_handler(request, exc):  # type: ignore[no-untyped-def]
        return JSONResponse(status_code=503, content={"code": exc.code, "message": str(exc)})

    monkeypatch.setattr(app_module.time, "monotonic", lambda: now)
    headers = {"Authorization": f"Bearer {TEST_TOKEN}", "Host": "127.0.0.1"}
    with TestClient(
        app, headers=headers, client=("127.0.0.1", 5555), raise_server_exceptions=False
    ) as client:
        original = client.get("/v1/notebooks")
        throttled = client.get("/v1/notebooks")
        assert attempts == 2

        now += app_module._SERVER_AUTH_RETRY_INTERVAL_SECONDS
        retried = client.get("/v1/notebooks")

    assert original.status_code == 503
    assert original.json() == {"code": 503, "message": "structured retry failure"}
    assert throttled.status_code == 500
    assert throttled.json()["error"]["category"] == "unexpected"
    assert retried.status_code == 503
    assert retried.json() == {"code": 503, "message": "structured retry failure"}
    assert attempts == 3


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
    assert seen == {"profile": "work", "keepalive": 600.0}


def test_create_app_default_factory_threads_backend(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    seen: dict[str, object] = {}

    @asynccontextmanager
    async def _fake_ctx() -> AsyncIterator[FakeClient]:
        yield FakeClient()

    def _spy_from_storage(**kwargs: object) -> AbstractAsyncContextManager[FakeClient]:
        seen.update(kwargs)
        return _fake_ctx()

    monkeypatch.setattr(NotebookLMClient, "from_storage", staticmethod(_spy_from_storage))
    app = create_app(profile="work", backend="android")
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
    assert seen == {"profile": "work", "keepalive": 600.0, "backend": "android"}


def test_backend_does_not_reparameterize_injected_factory() -> None:
    calls = 0

    @asynccontextmanager
    async def factory() -> AsyncIterator[FakeClient]:
        nonlocal calls
        calls += 1
        yield FakeClient()

    app = create_app(backend="android", client_factory=factory)
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
    assert calls == 1


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
