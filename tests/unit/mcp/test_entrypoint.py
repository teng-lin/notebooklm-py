"""Unit tests for the ``notebooklm-mcp`` console-script entry point.

These pin the argparse contract of :func:`notebooklm.mcp.__main__.main` so the
``uvx --from "notebooklm-py[mcp]" notebooklm-mcp`` / installed-console-script
distribution path stays wired:

* ``main(["--help"])`` prints argparse help and exits 0, and
* the default invocation wires the documented defaults (stdio transport,
  loopback host, INFO log level) through to ``create_server`` / ``server.run``
  without touching the network.

The server is stubbed (``create_server`` patched) so no real ``NotebookLMClient``
or transport is constructed — this is a pure CLI-surface test.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

# Skip cleanly when the `mcp` extra (fastmcp) is absent; see conftest.py.
pytest.importorskip("fastmcp")

from notebooklm.mcp import __main__ as entry  # noqa: E402 - after importorskip guard


def test_help_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    """``main(["--help"])`` prints argparse help and exits 0."""
    with pytest.raises(SystemExit) as excinfo:
        entry.main(["--help"])
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert "notebooklm-mcp" in out
    assert "--transport" in out


def test_defaults_wire_stdio_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bare ``main([])`` builds the server and runs the stdio transport.

    Asserts the documented defaults are wired through to ``server.run`` without
    constructing a real client or binding any socket.
    """
    fake_server = MagicMock()
    created: dict[str, object] = {}

    def fake_create_server(*, profile: str | None = None, client_factory=None):
        created["profile"] = profile
        return fake_server

    monkeypatch.setattr(entry, "create_server", fake_create_server)
    # No NOTEBOOKLM_* overrides — exercise the argparse defaults.
    for var in (
        "NOTEBOOKLM_PROFILE",
        "NOTEBOOKLM_MCP_TRANSPORT",
        "NOTEBOOKLM_MCP_HOST",
        "NOTEBOOKLM_MCP_PORT",
        "NOTEBOOKLM_LOG_LEVEL",
    ):
        monkeypatch.delenv(var, raising=False)

    entry.main([])

    # Default profile is unset (active profile bound at from_storage time).
    assert created["profile"] is None
    # stdio is the default transport; banner suppressed for clean JSON-RPC stdout.
    fake_server.run.assert_called_once_with(transport="stdio", show_banner=False)


def test_explicit_http_transport_binds_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    """``--transport http`` on loopback binds the port and needs NO token (auth=None)."""
    fake_server = MagicMock()
    captured: dict[str, object] = {}

    def fake_create_server(*, profile=None, client_factory=None, auth=None):
        captured["auth"] = auth
        return fake_server

    monkeypatch.setattr(entry, "create_server", fake_create_server)
    monkeypatch.delenv("NOTEBOOKLM_MCP_ALLOW_EXTERNAL_BIND", raising=False)
    monkeypatch.delenv("NOTEBOOKLM_MCP_TOKEN", raising=False)

    entry.main(["--transport", "http", "--host", "127.0.0.1", "--port", "8123"])

    fake_server.run.assert_called_once_with(transport="http", host="127.0.0.1", port=8123)
    # Loopback + no token → unauthenticated (today's local-dev behavior preserved).
    assert captured["auth"] is None


def test_http_default_port_is_9420(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default HTTP port is 9420 (no --port / NOTEBOOKLM_MCP_PORT)."""
    fake_server = MagicMock()
    monkeypatch.setattr(
        entry, "create_server", lambda *, profile=None, client_factory=None, auth=None: fake_server
    )
    monkeypatch.delenv("NOTEBOOKLM_MCP_PORT", raising=False)
    monkeypatch.delenv("NOTEBOOKLM_MCP_ALLOW_EXTERNAL_BIND", raising=False)
    monkeypatch.delenv("NOTEBOOKLM_MCP_TOKEN", raising=False)

    entry.main(["--transport", "http"])

    fake_server.run.assert_called_once_with(transport="http", host="127.0.0.1", port=9420)


def test_bogus_transport_env_default_fails_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    """An invalid env-derived transport default must SystemExit, not silently run
    stdio (argparse ``choices`` validates an explicit flag but not the env default)."""
    monkeypatch.setenv("NOTEBOOKLM_MCP_TRANSPORT", "websocket")
    monkeypatch.setattr(entry, "create_server", MagicMock())

    with pytest.raises(SystemExit) as excinfo:
        entry.main([])

    assert "Invalid transport" in str(excinfo.value)


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("127.0.0.1", True),
        ("::1", True),
        ("localhost", True),
        ("LOCALHOST", True),  # hostnames are case-insensitive
        (" localhost ", True),  # surrounding whitespace tolerated
        ("0.0.0.0", False),  # all-interfaces is NOT loopback → token required
        ("::", False),
        ("example.com", False),  # public DNS name → fail closed
        ("192.168.1.5", False),  # LAN address → not loopback
    ],
)
def test_is_loopback_classification(host: str, expected: bool) -> None:
    assert entry._is_loopback(host) is expected


def test_http_loopback_with_token_attaches_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """Loopback + a token set → auth IS attached (the safe, more-restrictive
    interaction): setting the token opts even a loopback bind into the gate."""
    fake_server = MagicMock()
    captured: dict[str, object] = {}

    def fake_create_server(*, profile=None, client_factory=None, auth=None):
        captured["auth"] = auth
        return fake_server

    monkeypatch.setattr(entry, "create_server", fake_create_server)
    monkeypatch.delenv("NOTEBOOKLM_MCP_ALLOW_EXTERNAL_BIND", raising=False)
    monkeypatch.setenv("NOTEBOOKLM_MCP_TOKEN", "loopback-token")

    entry.main(["--transport", "http", "--host", "127.0.0.1", "--port", "8124"])

    from notebooklm.mcp._auth import McpBearerAuthProvider

    assert isinstance(captured["auth"], McpBearerAuthProvider)


def test_http_non_loopback_without_token_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail closed: a network-reachable bind without a token must not start —
    even with the external-bind override set, the server is built nowhere."""
    built = MagicMock(side_effect=AssertionError("create_server must not be reached"))
    monkeypatch.setattr(entry, "create_server", built)
    monkeypatch.setenv("NOTEBOOKLM_MCP_ALLOW_EXTERNAL_BIND", "1")
    monkeypatch.delenv("NOTEBOOKLM_MCP_TOKEN", raising=False)

    with pytest.raises(SystemExit) as excinfo:
        entry.main(["--transport", "http", "--host", "0.0.0.0", "--port", "8000"])

    assert "NOTEBOOKLM_MCP_TOKEN" in str(excinfo.value)
    built.assert_not_called()


def test_http_non_loopback_with_token_attaches_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """A network bind WITH a token builds the server with a bearer auth provider."""
    from notebooklm.mcp._auth import McpBearerAuthProvider

    fake_server = MagicMock()
    captured: dict[str, object] = {}

    def fake_create_server(*, profile=None, client_factory=None, auth=None):
        captured["auth"] = auth
        return fake_server

    monkeypatch.setattr(entry, "create_server", fake_create_server)
    monkeypatch.setenv("NOTEBOOKLM_MCP_ALLOW_EXTERNAL_BIND", "1")
    monkeypatch.setenv("NOTEBOOKLM_MCP_TOKEN", "s3cret-token")

    entry.main(["--transport", "http", "--host", "0.0.0.0", "--port", "8000"])

    fake_server.run.assert_called_once_with(transport="http", host="0.0.0.0", port=8000)
    assert isinstance(captured["auth"], McpBearerAuthProvider)
