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

_STRONG_PW = "a-strong-random-password-1234567890"


@pytest.fixture(autouse=True)
def _clear_oauth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """``main()`` reads the self-hosted-OAuth env on every HTTP run; clear it by
    default so an ambient dev/CI environment can't perturb the bearer-focused tests
    (the OAuth tests set the vars explicitly after this autouse cleanup)."""
    for var in (
        "NOTEBOOKLM_MCP_OAUTH_PASSWORD",
        "NOTEBOOKLM_MCP_OAUTH_BASE_URL",
        "NOTEBOOKLM_MCP_TRUST_PROXY",
    ):
        monkeypatch.delenv(var, raising=False)


def _set_oauth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOTEBOOKLM_MCP_OAUTH_PASSWORD", _STRONG_PW)
    monkeypatch.setenv("NOTEBOOKLM_MCP_OAUTH_BASE_URL", "https://host.example.com")


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

    def fake_create_server(*, profile: str | None = None, client_factory=None, file_transfer=None):
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

    def fake_create_server(*, profile=None, client_factory=None, auth=None, file_transfer=None):
        captured["auth"] = auth
        return fake_server

    monkeypatch.setattr(entry, "create_server", fake_create_server)
    monkeypatch.delenv("NOTEBOOKLM_MCP_ALLOW_EXTERNAL_BIND", raising=False)
    monkeypatch.delenv("NOTEBOOKLM_MCP_TOKEN", raising=False)

    entry.main(["--transport", "http", "--host", "127.0.0.1", "--port", "8123"])

    fake_server.run.assert_called_once_with(
        transport="http", host="127.0.0.1", port=8123, uvicorn_config={"proxy_headers": False}
    )
    # Loopback + no token → unauthenticated (today's local-dev behavior preserved).
    assert captured["auth"] is None


def test_http_threads_profile_into_oauth_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """#1765 regression guard: the ``--profile`` flag must reach ``get_oauth_config`` so
    OAuth state persists under the profile the server actually drives. Without this, the
    entrypoint could silently regress to ``get_oauth_config()`` and the direct unit test
    would still pass."""
    seen: dict[str, object] = {}

    def spy_get_oauth_config(profile: str | None = None):
        seen["profile"] = profile
        return None  # OAuth off → loopback needs no auth, so main() proceeds

    monkeypatch.setattr(entry, "get_oauth_config", spy_get_oauth_config)
    monkeypatch.setattr(
        entry,
        "create_server",
        lambda *, profile=None, client_factory=None, auth=None, file_transfer=None: MagicMock(),
    )
    monkeypatch.delenv("NOTEBOOKLM_MCP_TOKEN", raising=False)
    monkeypatch.delenv("NOTEBOOKLM_MCP_ALLOW_EXTERNAL_BIND", raising=False)

    entry.main(
        ["--transport", "http", "--host", "127.0.0.1", "--port", "8124", "--profile", "work"]
    )

    assert seen["profile"] == "work"


def test_http_default_port_is_9420(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default HTTP port is 9420 (no --port / NOTEBOOKLM_MCP_PORT)."""
    fake_server = MagicMock()
    monkeypatch.setattr(
        entry,
        "create_server",
        lambda *, profile=None, client_factory=None, auth=None, file_transfer=None: fake_server,
    )
    monkeypatch.delenv("NOTEBOOKLM_MCP_PORT", raising=False)
    monkeypatch.delenv("NOTEBOOKLM_MCP_ALLOW_EXTERNAL_BIND", raising=False)
    monkeypatch.delenv("NOTEBOOKLM_MCP_TOKEN", raising=False)

    entry.main(["--transport", "http"])

    fake_server.run.assert_called_once_with(
        transport="http", host="127.0.0.1", port=9420, uvicorn_config={"proxy_headers": False}
    )


def test_http_run_disables_uvicorn_proxy_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    """The HTTP run path must pass uvicorn_config proxy_headers=False so Uvicorn does NOT
    rewrite the ASGI peer from X-Forwarded-For — otherwise a request could forge its source
    IP and defeat the OAuth login throttle's per-IP keying (which reads request.client.host)."""
    fake_server = MagicMock()
    monkeypatch.setattr(
        entry,
        "create_server",
        lambda *, profile=None, client_factory=None, auth=None, file_transfer=None: fake_server,
    )
    monkeypatch.delenv("NOTEBOOKLM_MCP_ALLOW_EXTERNAL_BIND", raising=False)
    monkeypatch.delenv("NOTEBOOKLM_MCP_TOKEN", raising=False)

    entry.main(["--transport", "http"])

    assert fake_server.run.call_args.kwargs["uvicorn_config"] == {"proxy_headers": False}


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

    def fake_create_server(*, profile=None, client_factory=None, auth=None, file_transfer=None):
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

    def fake_create_server(*, profile=None, client_factory=None, auth=None, file_transfer=None):
        captured["auth"] = auth
        return fake_server

    monkeypatch.setattr(entry, "create_server", fake_create_server)
    monkeypatch.setenv("NOTEBOOKLM_MCP_ALLOW_EXTERNAL_BIND", "1")
    monkeypatch.setenv("NOTEBOOKLM_MCP_TOKEN", "s3cret-token")

    entry.main(["--transport", "http", "--host", "0.0.0.0", "--port", "8000"])

    fake_server.run.assert_called_once_with(
        transport="http", host="0.0.0.0", port=8000, uvicorn_config={"proxy_headers": False}
    )
    assert isinstance(captured["auth"], McpBearerAuthProvider)


def test_http_non_loopback_with_oauth_only_satisfies_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """A network bind with self-hosted OAuth configured (no bearer) starts and attaches
    the OAuth provider — OAuth alone satisfies the fail-closed auth requirement."""
    from notebooklm.mcp._auth import McpBearerAuthProvider

    fake_server = MagicMock()
    captured: dict[str, object] = {}

    def fake_create_server(*, profile=None, client_factory=None, auth=None, file_transfer=None):
        captured["auth"] = auth
        return fake_server

    monkeypatch.setattr(entry, "create_server", fake_create_server)
    monkeypatch.setenv("NOTEBOOKLM_MCP_ALLOW_EXTERNAL_BIND", "1")
    monkeypatch.delenv("NOTEBOOKLM_MCP_TOKEN", raising=False)
    _set_oauth_env(monkeypatch)

    entry.main(["--transport", "http", "--host", "0.0.0.0", "--port", "8000"])

    fake_server.run.assert_called_once()
    auth = captured["auth"]
    assert auth is not None and not isinstance(auth, McpBearerAuthProvider)  # the OAuth provider


def test_http_non_loopback_with_oauth_and_bearer_composes_multiauth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bearer + self-hosted OAuth → MultiAuth (claude.ai uses OAuth, Claude Code the bearer)."""
    from fastmcp.server.auth import MultiAuth

    fake_server = MagicMock()
    captured: dict[str, object] = {}

    def fake_create_server(*, profile=None, client_factory=None, auth=None, file_transfer=None):
        captured["auth"] = auth
        return fake_server

    monkeypatch.setattr(entry, "create_server", fake_create_server)
    monkeypatch.setenv("NOTEBOOKLM_MCP_ALLOW_EXTERNAL_BIND", "1")
    monkeypatch.setenv("NOTEBOOKLM_MCP_TOKEN", "s3cret-token")
    _set_oauth_env(monkeypatch)

    entry.main(["--transport", "http", "--host", "0.0.0.0", "--port", "8000"])

    assert isinstance(captured["auth"], MultiAuth)


def test_http_non_loopback_partial_oauth_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    """Partial OAuth config (password without base URL) fails closed at startup."""
    built = MagicMock(side_effect=AssertionError("create_server must not be reached"))
    monkeypatch.setattr(entry, "create_server", built)
    monkeypatch.setenv("NOTEBOOKLM_MCP_ALLOW_EXTERNAL_BIND", "1")
    monkeypatch.delenv("NOTEBOOKLM_MCP_TOKEN", raising=False)
    monkeypatch.setenv("NOTEBOOKLM_MCP_OAUTH_PASSWORD", _STRONG_PW)  # base URL missing

    with pytest.raises(SystemExit):
        entry.main(["--transport", "http", "--host", "0.0.0.0", "--port", "8000"])
    built.assert_not_called()
