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
    """``--transport http`` with the default host binds the loopback port."""
    fake_server = MagicMock()
    monkeypatch.setattr(
        entry, "create_server", lambda *, profile=None, client_factory=None: fake_server
    )
    monkeypatch.delenv("NOTEBOOKLM_MCP_ALLOW_EXTERNAL_BIND", raising=False)

    entry.main(["--transport", "http", "--host", "127.0.0.1", "--port", "8123"])

    fake_server.run.assert_called_once_with(transport="http", host="127.0.0.1", port=8123)
