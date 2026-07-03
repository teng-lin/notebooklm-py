"""Integration tests driving the REST app through a **real** ``NotebookLMClient``
backed by **existing** VCR cassettes.

The rest of ``tests/server/`` uses an in-memory :class:`FakeClient`, which is fast
and good for adapter wiring but cannot catch divergence between the fake and the
real client runtime — real ``rpc/`` decode, real ``_app.serialize`` shapes, and
real ``_app.errors.classify`` → HTTP status projection.

These tests close that gap by binding ``create_app`` to a real client and
**replaying recorded cassettes** from ``tests/cassettes`` (the same recordings the
``tests/integration`` / CLI-VCR suites use). No new traffic is recorded. VCR
matches on ``rpcids`` + ``f.req`` body *shape* (not leaf id values), so a
placeholder notebook id drives any same-shape recording.

Surface sampled through the real stack (broad ``/v1`` coverage, not every route):

* notebooks — list, get, create, delete
* sources   — list, add url, add text, add file, delete
* chat      — ask
* artifacts — list, generate, poll, download
* errors    — a real 429 → RATE_LIMITED → 429 and a real 5xx → 502 projection

The artifact generate/poll/download and file-upload legs replay server-shaped
cassettes recorded by ``tests/scripts/record_server_cassettes.py`` (the server
emits a different ``f.req`` than the CLI, so the CLI cassettes don't match).

The gRPC-status-5 → 404 contract is covered by composition:
``tests/unit/test_decoder.py`` (real status-5 frame → ``ClientError(rpc_code=5)``)
+ ``tests/server/test_errors.py`` (that → 404).

Runs only under the ``server`` extra (``importorskip``); skipped without cassettes.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from notebooklm.auth import AuthTokens  # noqa: E402
from notebooklm.client import NotebookLMClient  # noqa: E402
from notebooklm.server.app import create_app  # noqa: E402
from tests.integration.conftest import skip_no_cassettes  # noqa: E402
from tests.vcr_config import notebooklm_vcr  # noqa: E402

from .conftest import TEST_TOKEN  # noqa: E402

pytestmark = skip_no_cassettes

#: Recorded placeholder ids (shape-matched by VCR — see module docstring).
_NB = "c3f6285f-1709-44c4-9cd6-e95cf0ea4f5e"
_SRC = "fdfc8ac4-3237-4f2a-8a79-3e24297a7040"
#: A notebook id recorded in ``real_api_get_notebook.yaml`` (asserted by value).
_GET_NB = "167481cd-23a3-4331-9a45-c8948900bf91"


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
def real_authed_client() -> Iterator[TestClient]:
    """A TestClient over an app whose lifespan opens a real ``NotebookLMClient``."""

    @asynccontextmanager
    async def factory() -> AsyncIterator[NotebookLMClient]:
        async with NotebookLMClient(_mock_auth()) as client:
            yield client

    app = create_app(client_factory=factory)
    headers = {"Authorization": f"Bearer {TEST_TOKEN}", "Host": "127.0.0.1"}
    with TestClient(
        app, headers=headers, client=("127.0.0.1", 5555), raise_server_exceptions=False
    ) as c:
        yield c


class TestNotebooks:
    @notebooklm_vcr.use_cassette("cli_notebook_list.yaml")
    def test_list(self, real_authed_client: TestClient) -> None:
        resp = real_authed_client.get("/v1/notebooks")
        assert resp.status_code == 200
        notebooks = resp.json()["notebooks"]
        assert notebooks
        assert isinstance(notebooks[0].get("id"), str) and notebooks[0]["id"]
        assert "title" in notebooks[0]

    @notebooklm_vcr.use_cassette("real_api_get_notebook.yaml", allow_playback_repeats=True)
    def test_get(self, real_authed_client: TestClient) -> None:
        resp = real_authed_client.get(f"/v1/notebooks/{_GET_NB}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == _GET_NB
        assert isinstance(body.get("title"), str) and body["title"]

    @notebooklm_vcr.use_cassette("cli_notebook_create.yaml", allow_playback_repeats=True)
    def test_create(self, real_authed_client: TestClient) -> None:
        resp = real_authed_client.post("/v1/notebooks", json={"title": "VCR CLI Test Notebook"})
        assert resp.status_code == 201
        body = resp.json()
        assert isinstance(body.get("id"), str) and body["id"]
        assert "title" in body

    @notebooklm_vcr.use_cassette("cli_notebook_delete.yaml", allow_playback_repeats=True)
    def test_delete(self, real_authed_client: TestClient) -> None:
        resp = real_authed_client.delete(f"/v1/notebooks/{_NB}")
        assert resp.status_code == 204
        assert resp.content == b""


class TestSources:
    @notebooklm_vcr.use_cassette("sources_list.yaml", allow_playback_repeats=True)
    def test_list(self, real_authed_client: TestClient) -> None:
        resp = real_authed_client.get(f"/v1/notebooks/{_NB}/sources")
        assert resp.status_code == 200
        body = resp.json()
        assert body["notebook_id"] == _NB
        assert isinstance(body["sources"], list)

    @notebooklm_vcr.use_cassette("sources_add_url.yaml", allow_playback_repeats=True)
    def test_add_url(self, real_authed_client: TestClient) -> None:
        resp = real_authed_client.post(
            f"/v1/notebooks/{_NB}/sources/url", json={"url": "https://example.com/x"}
        )
        assert resp.status_code == 201
        assert isinstance(resp.json().get("id"), str)

    @notebooklm_vcr.use_cassette("sources_add_text.yaml", allow_playback_repeats=True)
    def test_add_text(self, real_authed_client: TestClient) -> None:
        resp = real_authed_client.post(
            f"/v1/notebooks/{_NB}/sources/text", json={"text": "hello world", "title": "T"}
        )
        assert resp.status_code == 201
        assert isinstance(resp.json().get("id"), str)

    @notebooklm_vcr.use_cassette("sources_delete.yaml", allow_playback_repeats=True)
    def test_delete(self, real_authed_client: TestClient) -> None:
        resp = real_authed_client.delete(f"/v1/notebooks/{_NB}/sources/{_SRC}")
        assert resp.status_code == 204
        assert resp.content == b""


class TestChatAndArtifacts:
    @notebooklm_vcr.use_cassette("chat_ask.yaml", allow_playback_repeats=True)
    def test_chat_ask(self, real_authed_client: TestClient) -> None:
        resp = real_authed_client.post(
            f"/v1/notebooks/{_NB}/chat", json={"question": "What is this notebook about?"}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body.get("answer"), str) and body["answer"]
        assert body.get("conversation_id")

    @notebooklm_vcr.use_cassette("artifacts_list.yaml", allow_playback_repeats=True)
    def test_artifacts_list(self, real_authed_client: TestClient) -> None:
        resp = real_authed_client.get(f"/v1/notebooks/{_NB}/artifacts")
        assert resp.status_code == 200
        body = resp.json()
        assert body["notebook_id"] == _NB
        assert isinstance(body["artifacts"], list)


class TestArtifactLifecycle:
    """The generate → poll → download legs, replayed from server-shaped cassettes
    recorded by ``tests/scripts/record_server_cassettes.py``."""

    @notebooklm_vcr.use_cassette("server_generate_quiz.yaml", allow_playback_repeats=True)
    def test_generate_and_poll(self, real_authed_client: TestClient) -> None:
        # No ``source_ids`` → the server defaults to ALL sources (matching the
        # CLI). Before that fix this 502'd "Quiz generation is unavailable".
        gen = real_authed_client.post(f"/v1/notebooks/{_NB}/artifacts", json={"type": "quiz"})
        assert gen.status_code == 202
        body = gen.json()
        assert body["kind"] == "quiz"
        task_id = body["task_id"]
        assert isinstance(task_id, str) and task_id

        # Poll the freshly-recorded task (the pending registry resolved it).
        poll = real_authed_client.get(f"/v1/notebooks/{_NB}/artifacts/{task_id}")
        assert poll.status_code == 200
        assert poll.json()["notebook_id"] == _NB

    @notebooklm_vcr.use_cassette("server_download_mind_map.yaml", allow_playback_repeats=True)
    def test_download(self, real_authed_client: TestClient) -> None:
        resp = real_authed_client.post(
            f"/v1/notebooks/{_NB}/artifacts/download", json={"type": "mind-map"}
        )
        assert resp.status_code == 200
        assert resp.content  # streamed artifact bytes


class TestFileUpload:
    """The multipart file-upload leg through the real upload stack (INIT_UPLOAD +
    PUT bytes + ADD_SOURCE), replayed from ``server_add_file.yaml``."""

    @notebooklm_vcr.use_cassette("server_add_file.yaml", allow_playback_repeats=True)
    def test_add_file(self, real_authed_client: TestClient) -> None:
        # The upload must reproduce the caller's real basename (the resumable-init
        # 400s on an extensionless name and the source-id extraction keys off the
        # filename) — regression-guarded here through the real upload stack. The
        # filename matches the recording so the recorded registration replays.
        files = {"file": ("server-vcr-upload.txt", b"server upload body\n", "text/plain")}
        resp = real_authed_client.post(f"/v1/notebooks/{_NB}/sources/file", files=files)
        assert resp.status_code == 201
        assert isinstance(resp.json().get("id"), str)


class TestErrorProjection:
    """Real client-level error → ``classify`` → HTTP status, via synthetic-error
    cassettes (existing recordings reused by the CLI error-contract suite)."""

    @notebooklm_vcr.use_cassette("error_synthetic_429_rate_limit.yaml", allow_playback_repeats=True)
    def test_rate_limit_maps_to_429(self, real_authed_client: TestClient) -> None:
        resp = real_authed_client.get("/v1/notebooks")
        assert resp.status_code == 429
        assert resp.json()["error"]["category"] == "rate_limited"

    @notebooklm_vcr.use_cassette("error_synthetic_500_server.yaml", allow_playback_repeats=True)
    def test_server_error_maps_to_502(self, real_authed_client: TestClient) -> None:
        resp = real_authed_client.get("/v1/notebooks")
        assert resp.status_code == 502
