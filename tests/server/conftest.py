"""Shared fixtures for the REST server test suite.

The canonical contributor install omits the ``server`` extra (no fastapi), and CI
runs the full pytest after that install — so importing fastapi at collection time
would break the whole run. ``pytest.importorskip("fastapi")`` skips this entire
directory cleanly when the extra is absent; the server suite runs under
``--extra server``.

Every test drives the app through a FastAPI ``TestClient`` with an injected FAKE
client (``client_factory``) — no real auth, no network. The shared
``authed_client`` fixture sets the ``Authorization: Bearer`` header and a loopback
``Host`` on every request, satisfying both the bearer-token gate and the
DNS-rebinding (loopback-Host) guard.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import asynccontextmanager
from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402 - after importorskip guard

# Import the server package only after the importorskip guard.
from notebooklm.server._auth import SERVER_TOKEN_ENV  # noqa: E402
from notebooklm.server.app import create_app  # noqa: E402

from .fakes import FakeClient  # noqa: E402

#: The bearer token the test server validates against.
TEST_TOKEN = "test-secret-token"


@pytest.fixture
def fake_client() -> FakeClient:
    """A fresh in-memory fake client with no notebooks/sources/artifacts."""
    return FakeClient()


@pytest.fixture(autouse=True)
def _server_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configure the bearer token for every server test."""
    monkeypatch.setenv(SERVER_TOKEN_ENV, TEST_TOKEN)


def _factory_for(client: FakeClient) -> Any:
    @asynccontextmanager
    async def factory() -> Any:
        yield client

    return factory


@pytest.fixture
def app(fake_client: FakeClient) -> Any:
    """A FastAPI app bound to the fake client."""
    return create_app(client_factory=_factory_for(fake_client))


@pytest.fixture
def raw_client(app: Any) -> Iterator[TestClient]:
    """A TestClient WITHOUT auth headers (for auth-rejection tests).

    ``client=("127.0.0.1", …)`` gives requests a loopback PEER address so the
    unspoofable peer-loopback guard passes — leaving the Host-header /
    bearer-token gates as the thing under test.
    """
    # raise_server_exceptions=False so a projected 500 envelope (the UNEXPECTED
    # bug path) is returned rather than re-raised into the test.
    with TestClient(app, client=("127.0.0.1", 5555), raise_server_exceptions=False) as client:
        yield client


@pytest.fixture
def authed_client(app: Any) -> Iterator[TestClient]:
    """A TestClient with a valid bearer token + loopback Host + loopback peer."""
    headers = {"Authorization": f"Bearer {TEST_TOKEN}", "Host": "127.0.0.1"}
    with TestClient(
        app, headers=headers, client=("127.0.0.1", 5555), raise_server_exceptions=False
    ) as client:
        yield client
