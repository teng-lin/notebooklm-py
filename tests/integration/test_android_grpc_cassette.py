"""Public-client replay coverage for the Android gRPC cassette seam."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import grpc
import httpx
import pytest

from notebooklm import AuthTokens, NotebookLMClient
from notebooklm._android import auth as android_auth
from notebooklm._android import session as android_session
from tests._helpers.android_grpc_cassette import ReplayBearer, ReplayGrpcModule

pytestmark = pytest.mark.grpc_cassette

CASSETTE = (
    Path(__file__).resolve().parents[1] / "cassettes" / "android" / "get_project_recorded.grpc.json"
)
PROJECT_ID = "00000000-0000-4000-8000-000000000001"


def test_grpc_cassette_replay_marker_blocks_unbound_aio_channels() -> None:
    with pytest.raises(RuntimeError, match="refusing an unbound grpc.aio channel"):
        grpc.aio.secure_channel("localhost:443", grpc.ssl_channel_credentials())
    with pytest.raises(RuntimeError, match="refusing an unbound grpc.aio channel"):
        grpc.aio.insecure_channel("localhost:443")


@pytest.mark.asyncio
async def test_grpc_cassette_replay_marker_blocks_unbound_http_fallbacks() -> None:
    async with httpx.AsyncClient() as http_client:
        with pytest.raises(RuntimeError, match="refusing unbound httpx request"):
            await http_client.get(
                "https://notebooklm.google.com/_/LabsTailwindUi/data/batchexecute"
            )
        with pytest.raises(RuntimeError, match="refusing unbound httpx stream"):
            http_client.stream("POST", "https://notebooklm.google.com/_/LabsTailwindUi/data/stream")


@pytest.mark.asyncio
async def test_public_android_client_replays_get_project_without_live_grpc_or_bearer_mint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replay = ReplayGrpcModule(CASSETTE)
    bearer = ReplayBearer()
    production_session = android_session.AndroidSession

    monkeypatch.setattr(
        android_auth,
        "_make_bearer_provider",
        lambda _storage_path: bearer,
    )

    def replay_session(
        bearer_provider: Any,
        supervisor: Any,
        *,
        timeout: float | None,
    ) -> android_session.AndroidSession:
        assert bearer_provider is bearer
        return production_session(
            bearer_provider,
            supervisor,
            timeout=timeout,
            grpc_loader=lambda: replay,
        )

    monkeypatch.setattr(android_session, "AndroidSession", replay_session)
    auth = AuthTokens(
        cookies={"SID": "synthetic-cookie"},
        csrf_token="synthetic-csrf",
        session_id="synthetic-session",
    )
    async with NotebookLMClient(auth, backend="android") as client:
        assert set(client.backends.values()) == {"android"}
        notebook = await client.notebooks.get(PROJECT_ID)

    assert notebook.id == PROJECT_ID
    assert notebook.title == "SCRUBBED_STRING_0001"
    assert notebook.sources_count == 0
    replay.assert_consumed()
    assert bearer.activations == [1]
    assert bearer.gets == [1]
    assert replay.secure_channel_calls == 1
