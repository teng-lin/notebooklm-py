"""Focused composition coverage for the client auth-refresh seam."""

from __future__ import annotations

import httpx
import pytest

from notebooklm import NotebookLMClient
from notebooklm.auth import AuthTokens

REFRESH_HTML = '"SNlM0e":"fresh_csrf" "FdrFJe":"fresh_session"'
TEST_EPOCH = 1


@pytest.mark.asyncio
async def test_coordinator_refresh_crosses_client_and_session_composition_seam() -> None:
    """The assembled coordinator invokes the client's real refresh pipeline."""
    auth = AuthTokens(
        cookies={"SID": "test_sid"},
        csrf_token="stale_csrf",
        session_id="stale_session",
    )
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, text=REFRESH_HTML, request=request)

    def client_factory(**kwargs: object) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

    client = NotebookLMClient(auth)
    # Keep production assembly and callback wiring; replace only the network
    # transport so this boundary test remains deterministic and offline.
    client._web_runtime.kernel._async_client_factory = client_factory

    async with client:
        callback = client._web_runtime.auth_coord._refresh_callback
        assert callback is not None
        assert client._web_runtime.auth_coord._active_epoch == TEST_EPOCH

        await client._web_runtime.auth_coord.await_refresh(TEST_EPOCH)

    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert requests[0].url == "https://notebook.google.com/"
    assert client.auth is auth
    assert client.auth.csrf_token == "fresh_csrf"
    assert client.auth.session_id == "fresh_session"
