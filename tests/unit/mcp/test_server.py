"""Unit tests for the MCP server construction, lifespan, and entrypoint."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from unittest.mock import MagicMock

import pytest

# Skip cleanly when the `mcp` extra (fastmcp) is absent; see conftest.py.
pytest.importorskip("fastmcp")

from fastmcp import Client, FastMCP  # noqa: E402 - after importorskip guard

from notebooklm.mcp import __main__ as entry  # noqa: E402 - after importorskip guard
from notebooklm.mcp._context import (  # noqa: E402 - after importorskip guard
    AppState,
    get_client,
)
from notebooklm.mcp.server import (  # noqa: E402 - after importorskip guard
    SERVER_NAME,
    create_server,
)


def test_create_server_returns_fastmcp(mock_client: MagicMock) -> None:
    @contextlib.asynccontextmanager
    async def factory() -> AsyncIterator[MagicMock]:
        yield mock_client

    server = create_server(client_factory=factory)
    assert isinstance(server, FastMCP)
    assert server.name == SERVER_NAME


async def test_in_memory_client_connects(server_factory) -> None:
    """The in-memory FastMCP Client can open a session against the server."""
    async with Client(server_factory()) as client:
        # Tool listing must succeed for the registered server surface.
        tools = await client.list_tools()
        assert isinstance(tools, list)


async def test_lifespan_binds_the_factory_client(mock_client: MagicMock) -> None:
    """The lifespan yields an AppState wrapping exactly the factory's client."""
    captured: dict[str, object] = {}

    @contextlib.asynccontextmanager
    async def factory() -> AsyncIterator[MagicMock]:
        captured["client"] = mock_client
        yield mock_client

    server = create_server(client_factory=factory)
    async with Client(server):
        pass
    assert captured["client"] is mock_client


def test_get_client_reads_appstate() -> None:
    """get_client unwraps the AppState bound in the request lifespan context."""
    sentinel = MagicMock()
    state = AppState(client=sentinel)

    ctx = MagicMock()
    ctx.request_context.lifespan_context = state
    assert get_client(ctx) is sentinel


# --------------------------------------------------------------------------- #
# Bind guard
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_bind_guard_allows_loopback(host: str) -> None:
    # Should not raise.
    entry._check_http_bind_allowed(host, allow_external=False)


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "::", "", "   "])
def test_bind_guard_refuses_non_loopback(host: str) -> None:
    # An empty / whitespace-only host is a fail-closed refusal too — it would
    # otherwise bind to all interfaces.
    with pytest.raises(SystemExit):
        entry._check_http_bind_allowed(host, allow_external=False)


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10"])
def test_bind_guard_override_allows_non_loopback(host: str) -> None:
    # With the explicit opt-in, a non-loopback host is allowed.
    entry._check_http_bind_allowed(host, allow_external=True)


# --------------------------------------------------------------------------- #
# main()
# --------------------------------------------------------------------------- #
def test_main_runs_stdio_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    runs: list[tuple] = []
    fake_server = MagicMock()
    fake_server.run = lambda *a, **k: runs.append((a, k))
    monkeypatch.setattr(entry, "create_server", lambda **kw: fake_server)

    entry.main([])
    assert len(runs) == 1
    # stdio transport => run() called with no transport kwarg (or transport=stdio).
    _, kwargs = runs[0]
    assert kwargs.get("transport", "stdio") == "stdio"


def test_main_http_refuses_external_bind(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOTEBOOKLM_MCP_ALLOW_EXTERNAL_BIND", raising=False)
    fake_server = MagicMock()
    monkeypatch.setattr(entry, "create_server", lambda **kw: fake_server)

    with pytest.raises(SystemExit):
        entry.main(["--transport", "http", "--host", "0.0.0.0"])
    fake_server.run.assert_not_called()


def test_main_http_loopback_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    runs: list[dict] = []
    fake_server = MagicMock()
    fake_server.run = lambda **k: runs.append(k)
    monkeypatch.setattr(entry, "create_server", lambda **kw: fake_server)

    entry.main(["--transport", "http", "--host", "127.0.0.1", "--port", "9123"])
    assert runs and runs[0]["transport"] == "http"
    assert runs[0]["host"] == "127.0.0.1"
    assert runs[0]["port"] == 9123


def test_main_passes_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}
    fake_server = MagicMock()
    fake_server.run = lambda *a, **k: None

    def fake_create(**kw: object) -> MagicMock:
        seen.update(kw)
        return fake_server

    monkeypatch.setattr(entry, "create_server", fake_create)
    entry.main(["--profile", "work"])
    assert seen["profile"] == "work"


# --------------------------------------------------------------------------- #
# Env-derived defaults (Fix E)
# --------------------------------------------------------------------------- #
def test_bad_transport_env_errors_cleanly(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bogus NOTEBOOKLM_MCP_TRANSPORT must fail, not silently fall back to stdio."""
    monkeypatch.setenv("NOTEBOOKLM_MCP_TRANSPORT", "bogus")
    fake_server = MagicMock()
    monkeypatch.setattr(entry, "create_server", lambda **kw: fake_server)

    with pytest.raises(SystemExit):
        entry.main([])
    fake_server.run.assert_not_called()


def test_bad_port_env_with_cli_override_works(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-int NOTEBOOKLM_MCP_PORT must not crash parser build; --port overrides it."""
    monkeypatch.setenv("NOTEBOOKLM_MCP_PORT", "not-an-int")
    monkeypatch.delenv("NOTEBOOKLM_MCP_ALLOW_EXTERNAL_BIND", raising=False)
    runs: list[dict] = []
    fake_server = MagicMock()
    fake_server.run = lambda **k: runs.append(k)
    monkeypatch.setattr(entry, "create_server", lambda **kw: fake_server)

    entry.main(["--transport", "http", "--host", "127.0.0.1", "--port", "9000"])
    assert runs and runs[0]["port"] == 9000


def test_bad_port_env_without_override_errors_cleanly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-int NOTEBOOKLM_MCP_PORT with no override fails cleanly (not a build crash)."""
    monkeypatch.setenv("NOTEBOOKLM_MCP_PORT", "not-an-int")
    fake_server = MagicMock()
    monkeypatch.setattr(entry, "create_server", lambda **kw: fake_server)

    # Building the parser must NOT raise; the error surfaces as a clean SystemExit
    # at parse/convert time, only when the http transport actually needs the port.
    with pytest.raises(SystemExit):
        entry.main(["--transport", "http", "--host", "127.0.0.1"])
    fake_server.run.assert_not_called()
