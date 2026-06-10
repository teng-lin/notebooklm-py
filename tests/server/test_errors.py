"""U3: error projection from ``classify`` to HTTP status + typed envelope."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient

from notebooklm import exceptions as exc
from notebooklm.server._errors import _redact, error_response
from notebooklm.server.app import create_app

from .conftest import TEST_TOKEN
from .fakes import FakeClient


class _RaisingNotebooks:
    def __init__(self, error: BaseException) -> None:
        self._error = error

    async def list(self) -> list[object]:
        raise self._error


def _client_raising(error: BaseException) -> TestClient:
    fake = FakeClient()
    fake.notebooks = _RaisingNotebooks(error)  # type: ignore[assignment]

    @asynccontextmanager
    async def factory() -> AsyncIterator[FakeClient]:
        yield fake

    app = create_app(client_factory=factory)
    headers = {"Authorization": f"Bearer {TEST_TOKEN}", "Host": "127.0.0.1"}
    client = TestClient(app, headers=headers, raise_server_exceptions=False)
    client.__enter__()
    return client


@pytest.mark.parametrize(
    ("error", "status", "category"),
    [
        (exc.ClientError("missing", rpc_code=5), 404, "not_found"),
        (exc.ClientError("missing", rpc_code="5"), 404, "not_found"),
        (exc.RateLimitError("slow down"), 429, "rate_limited"),
        (exc.AuthError("expired"), 401, "auth"),
        (exc.ValidationError("bad"), 400, "validation"),
        (exc.RPCError("decode failed"), 502, "rpc"),
        (RuntimeError("boom"), 500, "unexpected"),
    ],
)
def test_exception_projects_to_status_and_category(
    error: BaseException, status: int, category: str
) -> None:
    client = _client_raising(error)
    try:
        resp = client.get("/v1/notebooks")
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == status
    body = resp.json()
    assert body["error"]["category"] == category


def test_status_5_preserves_the_scrubbed_message() -> None:
    """The 404 body carries the scrubbed account-routing hint (not dropped)."""
    client = _client_raising(exc.ClientError("wrong authuser hint", rpc_code=5))
    try:
        resp = client.get("/v1/notebooks")
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 404
    assert "wrong authuser hint" in resp.json()["error"]["message"]


def test_status_7_is_not_routed_to_404() -> None:
    """Code 7 (permission-denied) stays a generic RPC → 502, not 404."""
    client = _client_raising(exc.ClientError("denied", rpc_code=7))
    try:
        resp = client.get("/v1/notebooks")
    finally:
        client.__exit__(None, None, None)
    assert resp.status_code == 502
    assert resp.json()["error"]["category"] == "rpc"


def test_long_message_is_truncated() -> None:
    long = "x " * 400
    resp = error_response(exc.RPCError(long))
    body = resp.body.decode()
    assert "…" in body
    # The redacted message is capped well under the raw length.
    assert len(_redact(long)) <= 301


def test_request_validation_message_has_no_source_paths(authed_client: object) -> None:
    """A malformed body → 422 envelope with a compact field summary, NOT
    ``str(exc)`` (which embeds server file paths / frame info under pydantic v2)."""
    from fastapi.testclient import TestClient

    assert isinstance(authed_client, TestClient)
    resp = authed_client.post("/v1/notebooks/nb-1/chat", json={})
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["category"] == "validation"
    message = body["error"]["message"]
    # The missing field is named, but no server path / source file leaks.
    assert "question" in message
    assert ".py" not in message and "/home/" not in message and 'File "' not in message
