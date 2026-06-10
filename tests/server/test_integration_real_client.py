"""Integration tests driving the REST app through a **real** ``NotebookLMClient``
backed by **existing** VCR cassettes.

The rest of ``tests/server/`` uses an in-memory :class:`FakeClient`, which is fast
and good for adapter wiring but cannot catch divergence between the fake and the
real client runtime — real ``rpc/`` decode and real ``_app.serialize`` shapes.

These tests close that gap by binding ``create_app`` to a real client and
**replaying recorded cassettes** from ``tests/cassettes`` (the same recordings
the ``tests/integration`` / CLI-VCR suites use). No new traffic is recorded — they
reuse existing cassettes, so a real Google response shape flows through the full
``decode → serialize → HTTP`` path of the server adapter.

Note on error-classification coverage: the gRPC-status-5 → 404 contract is
exercised end-to-end without a (nonexistent) not-found cassette by composing two
existing tests — ``tests/unit/test_decoder.py`` proves a real status-5 frame
decodes to ``ClientError(rpc_code=5)``, and ``tests/server/test_errors.py`` proves
that exception projects to ``404 not_found`` through the server's ``classify``.

Runs only under the ``server`` extra (``importorskip``), i.e. in the dedicated
``REST server`` CI job; skipped when cassettes are unavailable.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

# VCR replay machinery (shared with the integration / CLI-VCR suites). Importing
# the modules only pulls in the objects — the integration conftest's autouse
# fixtures/hooks stay scoped to their own directory tree.
from tests.integration.conftest import skip_no_cassettes  # noqa: E402
from tests.vcr_config import notebooklm_vcr  # noqa: E402

from notebooklm.auth import AuthTokens  # noqa: E402
from notebooklm.client import NotebookLMClient  # noqa: E402
from notebooklm.server.app import create_app  # noqa: E402

from .conftest import TEST_TOKEN  # noqa: E402

pytestmark = skip_no_cassettes


def _mock_auth() -> AuthTokens:
    """Replay-mode auth — values are irrelevant; the cassette supplies the wire."""
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


#: A notebook id recorded in ``real_api_get_notebook.yaml`` (GET_NOTEBOOK).
_RECORDED_NB_ID = "167481cd-23a3-4331-9a45-c8948900bf91"


class TestRealSerialization:
    @notebooklm_vcr.use_cassette("cli_notebook_list.yaml")
    def test_list_notebooks_real_shape(self, real_authed_client: TestClient) -> None:
        # Real LIST_NOTEBOOKS cassette → real client decode → real to_jsonable.
        resp = real_authed_client.get("/v1/notebooks")
        assert resp.status_code == 200
        notebooks = resp.json()["notebooks"]
        assert notebooks, "recorded LIST_NOTEBOOKS should yield a non-empty list"
        # The real serialized shape carries id + title on every notebook.
        first = notebooks[0]
        assert isinstance(first.get("id"), str) and first["id"]
        assert "title" in first

    @notebooklm_vcr.use_cassette("real_api_get_notebook.yaml", allow_playback_repeats=True)
    def test_get_notebook_real_shape(self, real_authed_client: TestClient) -> None:
        # Real GET_NOTEBOOK cassette → real Notebook serialization for a known id.
        resp = real_authed_client.get(f"/v1/notebooks/{_RECORDED_NB_ID}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == _RECORDED_NB_ID
        assert isinstance(body.get("title"), str) and body["title"]
