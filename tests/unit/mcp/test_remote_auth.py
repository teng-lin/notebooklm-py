"""Tests for ``notebooklm.mcp._auth`` — the remote-transport bearer gate.

Covers the custom :class:`McpBearerAuthProvider` (constant-time verify, redacted
repr), the env accessor (strip + empty→None), ``build_auth_provider`` mapping,
and the contract that ``create_server`` stays **env-free** (no auth attached
without an explicit ``auth=``).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("fastmcp")

from notebooklm.mcp._auth import (  # noqa: E402 - after importorskip guard
    MCP_TOKEN_ENV,
    McpBearerAuthProvider,
    build_auth_provider,
    get_configured_token,
)


@pytest.mark.asyncio
async def test_verify_token_accepts_exact_match_only() -> None:
    provider = McpBearerAuthProvider("correct-horse")
    ok = await provider.verify_token("correct-horse")
    assert ok is not None
    assert ok.client_id == "notebooklm-mcp"
    assert ok.scopes == []
    # The validated AccessToken must NOT echo the live bearer (it is stored on the
    # request scope; its pydantic repr would otherwise leak the token).
    assert ok.token != "correct-horse"
    assert await provider.verify_token("wrong") is None
    assert await provider.verify_token("") is None


@pytest.mark.asyncio
async def test_verify_token_matches_utf8_token_through_latin1_header() -> None:
    """A non-ASCII token sent UTF-8-encoded verifies correctly: Starlette latin-1-
    decodes the header, and verify_token round-trips it back to the request bytes.
    (Guards the encoding-mismatch bug gemini-code-assist flagged.)"""
    provider = McpBearerAuthProvider("café-Ω-token")  # configured (clean str)
    # What Starlette hands verify_token: the latin-1 view of the UTF-8 header bytes.
    as_received = "café-Ω-token".encode().decode("latin-1")
    assert await provider.verify_token(as_received) is not None
    # The raw (non-round-tripped) string must NOT match — and must not raise even
    # though it contains a non-latin-1 char.
    assert await provider.verify_token("café-Ω-token") is None


def test_repr_never_leaks_the_token() -> None:
    # repr is the realistic leak path (an f-string / logger.info(provider)); the
    # value must live in __dict__ to be compared, so we defend repr, not storage.
    provider = McpBearerAuthProvider("super-secret-value")
    assert "super-secret-value" not in repr(provider)
    assert "redacted" in repr(provider)
    # Mangled attribute name keeps it off `provider.token` / casual access.
    assert not hasattr(provider, "token")


def test_get_configured_token_strips_and_treats_empty_as_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(MCP_TOKEN_ENV, "  tok-123  ")
    assert get_configured_token() == "tok-123"
    monkeypatch.setenv(MCP_TOKEN_ENV, "   ")
    assert get_configured_token() is None
    monkeypatch.delenv(MCP_TOKEN_ENV, raising=False)
    assert get_configured_token() is None


def test_build_auth_provider_maps_token_to_provider() -> None:
    assert build_auth_provider(None) is None
    assert build_auth_provider("") is None
    provider = build_auth_provider("a-token")
    assert isinstance(provider, McpBearerAuthProvider)


def test_create_server_is_env_free(monkeypatch: pytest.MonkeyPatch) -> None:
    """``create_server`` must NOT read ``NOTEBOOKLM_MCP_TOKEN`` — auth is attached
    only when the caller passes it (so stdio + this suite never gate)."""
    from notebooklm.mcp.server import create_server

    @asynccontextmanager
    async def fake_factory():
        yield MagicMock()

    monkeypatch.setenv(MCP_TOKEN_ENV, "present-but-ignored")
    server = create_server(profile="x", client_factory=lambda: fake_factory())
    assert server.auth is None

    provider = build_auth_provider("explicit")
    server_with_auth = create_server(
        profile="x", client_factory=lambda: fake_factory(), auth=provider
    )
    assert server_with_auth.auth is provider


def test_http_app_rejects_request_without_bearer() -> None:
    """End-to-end through FastMCP's real auth middleware: a request to /mcp with
    no/invalid bearer is rejected (401) before reaching any tool/client."""
    starlette_testclient = pytest.importorskip("starlette.testclient")
    from notebooklm.mcp.server import create_server

    @asynccontextmanager
    async def fake_factory():
        yield MagicMock()

    server = create_server(
        profile="x", client_factory=lambda: fake_factory(), auth=build_auth_provider("right-token")
    )
    app = server.http_app()
    client = starlette_testclient.TestClient(app)
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    accept = {"Accept": "application/json, text/event-stream"}

    # No token → rejected at the auth layer (never reaches the transport/client).
    assert client.post("/mcp", json=body, headers=accept).status_code == 401
    # Wrong token → also rejected.
    assert (
        client.post(
            "/mcp", json=body, headers={**accept, "Authorization": "Bearer nope"}
        ).status_code
        == 401
    )


def test_http_app_accepts_correct_bearer() -> None:
    """Positive path: the CORRECT token is NOT rejected — it passes the auth gate
    and the MCP initialize handshake succeeds (guards against a blanket-reject
    regression that the 401-only tests would miss)."""
    starlette_testclient = pytest.importorskip("starlette.testclient")
    from notebooklm.mcp.server import create_server

    @asynccontextmanager
    async def fake_factory():
        yield MagicMock()

    server = create_server(
        profile="x", client_factory=lambda: fake_factory(), auth=build_auth_provider("right-token")
    )
    app = server.http_app()
    init = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"},
        },
    }
    headers = {
        "Accept": "application/json, text/event-stream",
        "Authorization": "Bearer right-token",
    }
    # `with` runs the lifespan so the streamable-HTTP session manager initializes.
    with starlette_testclient.TestClient(app) as client:
        r = client.post("/mcp", json=init, headers=headers)
    assert r.status_code != 401  # passed the bearer gate
    assert r.status_code == 200  # initialize handshake handled


@pytest.mark.asyncio
async def test_authenticated_tool_call_over_http_transport() -> None:
    """Full integration: a real MCP ``Client`` drives the real FastMCP HTTP
    transport + auth middleware + protocol handshake + tool dispatch, end to end.

    With the correct bearer the client completes the handshake and a
    ``notebook_list`` tool call (dispatched through ``_app`` to a stubbed
    NotebookLMClient) returns its structured result; a wrong bearer is rejected
    with 401. Runs entirely in-process over an httpx ASGI transport — no port, no
    network, no extra deps (the existing ``mcp_vcr`` suite covers the in-memory
    transport; this is the one path that also exercises HTTP + the bearer gate).
    """
    pytest.importorskip("starlette")
    import httpx
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    from notebooklm.mcp.server import create_server

    stub = MagicMock()
    stub.notebooks.list = AsyncMock(return_value=[])  # canned: empty list serializes cleanly

    @asynccontextmanager
    async def factory():
        yield stub

    server = create_server(
        client_factory=lambda: factory(), auth=build_auth_provider("right-token")
    )
    app = server.http_app()

    def httpx_factory(**kwargs):
        # fastmcp passes its own transport/headers; drive the app in-process via ASGI.
        kwargs.pop("transport", None)
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://mcp.test", **kwargs
        )

    def make_transport(token: str) -> StreamableHttpTransport:
        return StreamableHttpTransport(
            "http://mcp.test/mcp",
            headers={"Authorization": f"Bearer {token}"},
            httpx_client_factory=httpx_factory,
        )

    # Running the app lifespan binds the (stubbed) client for the request lifetime.
    async with app.router.lifespan_context(app):
        async with Client(make_transport("right-token")) as mcp:
            tools = await mcp.list_tools()
            assert any(t.name == "notebook_list" for t in tools)
            result = await mcp.call_tool("notebook_list", {})
            assert result.structured_content == {
                "notebooks": [],
                "total": 0,
                "offset": 0,
                "has_more": False,
            }
            stub.notebooks.list.assert_awaited()  # dispatch really reached the client

        # Wrong bearer → rejected by the auth middleware with 401 (never a tool).
        with pytest.raises(httpx.HTTPStatusError) as excinfo:
            async with Client(make_transport("wrong-token")) as mcp:
                await mcp.list_tools()
        assert excinfo.value.response.status_code == 401
