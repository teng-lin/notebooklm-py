"""Focused composition coverage for the client auth-refresh seam."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import httpx
import pytest

from notebooklm import NotebookLMClient
from notebooklm._web.transport import session_auth
from notebooklm.auth import AuthTokens
from notebooklm.exceptions import RPCError
from notebooklm.rpc import RPCMethod
from tests._helpers.client_factory import build_client_shell_for_tests

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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mint_recovers", "retry_rejected"),
    [(True, False), (True, True), (False, False)],
    ids=["recovered", "retry-rejected", "refresh-failed"],
)
async def test_decoded_auth_rejection_recovers_tokenless_homepage_and_replays_once(
    monkeypatch: pytest.MonkeyPatch, mint_recovers: bool, retry_rejected: bool
) -> None:
    """The default callback reaches L4 after code 16, keeping the one-replay budget."""
    auth = AuthTokens(
        cookies={"SID": "test_sid"}, csrf_token="stale_csrf", session_id="stale_session"
    )
    requests: list[httpx.Request] = []
    recovered = False
    post_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal post_count
        requests.append(request)
        if request.method == "GET":
            html = REFRESH_HTML if recovered else "<html>Signed out app shell</html>"
            return httpx.Response(200, text=html, request=request)
        post_count += 1
        # Use the decoder's existing null-result status shape to exercise the
        # real decode/auth predicate/coordinator/HTTP replay composition.
        if post_count == 1 or retry_rejected:
            frame = ["wrb.fr", RPCMethod.LIST_NOTEBOOKS.value, None, None, None, [16], "generic"]
        else:
            frame = ["wrb.fr", RPCMethod.LIST_NOTEBOOKS.value, "[]", None, None, None, "generic"]
        return httpx.Response(200, text=json.dumps([frame]), request=request)

    async def remint(**kwargs: object) -> bool:
        nonlocal recovered
        recovered = mint_recovers
        return True

    master_token = AsyncMock(side_effect=remint)
    monkeypatch.setattr(session_auth, "_try_storage_cookie_reload", AsyncMock(return_value=False))
    monkeypatch.setattr(session_auth, "_try_refresh_cmd_reauth", AsyncMock(return_value=False))
    monkeypatch.setattr(session_auth, "_try_headless_reauth", AsyncMock(return_value=False))
    monkeypatch.setattr(session_auth, "_try_master_token_reauth", master_token)

    def client_factory(**kwargs: object) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

    client = NotebookLMClient(auth)
    client._web_runtime.kernel._async_client_factory = client_factory
    client._web_runtime.composed.chain_host._refresh_retry_delay = 0
    async with client:
        if retry_rejected or not mint_recovers:
            with pytest.raises(RPCError) as error:
                await client.raw.call(RPCMethod.LIST_NOTEBOOKS, [])
            assert error.value.rpc_code == 16
        else:
            assert await client.raw.call(RPCMethod.LIST_NOTEBOOKS, []) == []

    assert [request.method for request in requests] == (
        ["POST", "GET", "GET", "POST"] if mint_recovers else ["POST", "GET", "GET"]
    )
    assert "at=stale_csrf" in requests[0].content.decode()
    if mint_recovers:
        assert "at=fresh_csrf" in requests[-1].content.decode()
        assert requests[-1].url.params["f.sid"] == "fresh_session"
    else:
        assert (auth.csrf_token, auth.session_id) == ("stale_csrf", "stale_session")
    master_token.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("callback_enabled", [False, True])
async def test_explicit_headless_recovery_retries_tokenless_shell_with_wider_policy(
    monkeypatch: pytest.MonkeyPatch, callback_enabled: bool
) -> None:
    """A failed base refresh must let the explicit L3 opt-in reach its recovery rung."""
    auth = AuthTokens(
        cookies={"SID": "test_sid"}, csrf_token="stale_csrf", session_id="stale_session"
    )
    requests: list[httpx.Request] = []
    recovered = False

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            text=REFRESH_HTML if recovered else "<html>Signed out app shell</html>",
            request=request,
        )

    async def headless(**kwargs: object) -> bool:
        nonlocal recovered
        recovered = bool(kwargs["allow_headless"])
        return recovered

    recovery = AsyncMock(side_effect=headless)
    monkeypatch.setattr(session_auth, "_try_storage_cookie_reload", AsyncMock(return_value=False))
    monkeypatch.setattr(session_auth, "_try_refresh_cmd_reauth", AsyncMock(return_value=False))
    monkeypatch.setattr(session_auth, "_try_headless_reauth", recovery)
    monkeypatch.setattr(session_auth, "_try_master_token_reauth", AsyncMock(return_value=False))

    def client_factory(**kwargs: object) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

    client = (
        NotebookLMClient(auth)
        if callback_enabled
        else build_client_shell_for_tests(auth, refresh_callback=None)
    )
    client._web_runtime.kernel._async_client_factory = client_factory
    async with client:
        assert await client.refresh_auth(allow_headless=True) is auth

    assert len(requests) == (3 if callback_enabled else 2)
    assert [call.kwargs["allow_headless"] for call in recovery.await_args_list] == (
        [False, True] if callback_enabled else [True]
    )
    assert (auth.csrf_token, auth.session_id) == ("fresh_csrf", "fresh_session")
