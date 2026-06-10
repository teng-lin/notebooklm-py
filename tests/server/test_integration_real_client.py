"""Integration tests driving the REST app through a **real** ``NotebookLMClient``.

The rest of ``tests/server/`` uses an in-memory :class:`FakeClient`, which is fast
and good for adapter wiring but cannot catch divergence between the fake and the
real client runtime — real ``rpc/`` decode, real ``_app.serialize`` shapes, and
real ``_app.errors.classify`` on an *actual* RPC error frame.

These tests close that gap: they bind ``create_app`` to a real client whose HTTP
transport is mocked at the wire level (``pytest-httpx``), using the same
``build_rpc_response`` / raw-frame helpers the ``tests/integration`` suite uses.
The highest-value thing they pin is the gRPC-status-5 → 404 projection through
the genuine decode → classify → HTTP path (the fake only ever returns ``None``).

Runs only under the ``server`` extra (``importorskip``), i.e. in the dedicated
``REST server`` CI job.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("pytest_httpx")

from fastapi.testclient import TestClient  # noqa: E402
from pytest_httpx import HTTPXMock  # noqa: E402

from notebooklm.auth import AuthTokens  # noqa: E402
from notebooklm.client import NotebookLMClient  # noqa: E402
from notebooklm.rpc.types import RPCMethod  # noqa: E402
from notebooklm.server.app import create_app  # noqa: E402

from .conftest import TEST_TOKEN  # noqa: E402


def _mock_auth() -> AuthTokens:
    """Replay-mode auth — values are irrelevant; the wire is mocked."""
    return AuthTokens(
        cookies={
            "SID": "mock_sid",
            "HSID": "mock_hsid",
            "SSID": "mock_ssid",
            "APISID": "mock_apisid",
            "SAPISID": "mock_sapisid",
        },
        csrf_token="mock_csrf_token",
        session_id="mock_session_id",
    )


@pytest.fixture
def real_client_app() -> Any:
    """A FastAPI app whose lifespan opens a real ``NotebookLMClient``."""

    @asynccontextmanager
    async def factory() -> AsyncIterator[NotebookLMClient]:
        async with NotebookLMClient(_mock_auth()) as client:
            yield client

    return create_app(client_factory=factory)


@pytest.fixture
def real_authed_client(real_client_app: Any) -> Iterator[TestClient]:
    headers = {"Authorization": f"Bearer {TEST_TOKEN}", "Host": "127.0.0.1"}
    with TestClient(real_client_app, headers=headers, raise_server_exceptions=False) as c:
        yield c


def _status5_get_notebook_frame() -> bytes:
    """A raw GET_NOTEBOOK response carrying gRPC status-5 (Not found).

    Mirrors ``tests/unit/test_decoder.py``: the 6th ``wrb.fr`` slot is the
    error-info list; ``[5]`` makes ``rpc/decoder`` raise ``ClientError(rpc_code=5)``.
    """
    rpc_id = RPCMethod.GET_NOTEBOOK.value
    chunk = json.dumps(["wrb.fr", rpc_id, None, None, None, [5], "generic"])
    return f")]}}'\n{len(chunk)}\n{chunk}\n".encode()


class TestRealSerialization:
    def test_list_notebooks_real_shape(
        self,
        real_authed_client: TestClient,
        httpx_mock: HTTPXMock,
        mock_list_notebooks_response: str,
    ) -> None:
        # Real LIST_NOTEBOOKS frame → real client decode → real to_jsonable.
        httpx_mock.add_response(content=mock_list_notebooks_response.encode())

        resp = real_authed_client.get("/v1/notebooks")
        assert resp.status_code == 200
        body = resp.json()
        ids = [n["id"] for n in body["notebooks"]]
        assert ids == ["nb_001", "nb_002"]
        titles = [n["title"] for n in body["notebooks"]]
        assert titles == ["My First Notebook", "Research Notes"]

    def test_get_notebook_real_shape(
        self,
        real_authed_client: TestClient,
        httpx_mock: HTTPXMock,
        build_rpc_response: Any,
    ) -> None:
        frame = build_rpc_response(
            RPCMethod.GET_NOTEBOOK,
            [
                [
                    "Test Notebook",
                    [[["src_001"], "Source 1", [None, 0], [None, 2]]],
                    "nb_123",
                    "📘",
                    None,
                    [None, None, None, None, None, [1704067200, 0]],
                ]
            ],
        )
        httpx_mock.add_response(content=frame.encode())

        resp = real_authed_client.get("/v1/notebooks/nb_123")
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == "nb_123"
        assert body["title"] == "Test Notebook"


class TestRealErrorClassification:
    def test_status5_get_notebook_maps_to_404(
        self, real_authed_client: TestClient, httpx_mock: HTTPXMock
    ) -> None:
        # The exact bug the brainstorm flagged: a real gRPC status-5 frame must
        # project to 404 not_found through decode → classify, NOT 502. The fake
        # can't prove this (it returns None); only a real decode can.
        httpx_mock.add_response(content=_status5_get_notebook_frame())

        resp = real_authed_client.get("/v1/notebooks/does-not-exist")
        assert resp.status_code == 404
        body = resp.json()
        assert body["error"]["category"] == "not_found"
        # Real (scrubbed) decoder message is projected, not a generic string.
        assert "status code 5" in body["error"]["message"]
        # Defense-in-depth: no credential shapes leak into the wire message.
        assert "mock_sapisid" not in body["error"]["message"]
