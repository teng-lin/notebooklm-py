"""Tests for building the remote file-transfer config in ``mcp/__main__.py``.

Pins: neither URL set → ``None`` (and **no** ``SystemExit`` — a bearer-only remote
server must keep starting); ``NOTEBOOKLM_MCP_PUBLIC_URL`` set → a config is built;
an invalid public URL → ``SystemExit`` (same validation as the OAuth base URL); and
the OAuth-base-URL fallback. Plus the http-path wiring threads it into
``create_server``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

# Skip cleanly when the `mcp` extra (fastmcp) is absent; see conftest.py.
pytest.importorskip("fastmcp")

from notebooklm.mcp import __main__ as entry  # noqa: E402 - after importorskip guard
from notebooklm.mcp._filelink import FileTransferConfig  # noqa: E402 - after importorskip guard

PUBLIC = entry.PUBLIC_URL_ENV
OAUTH_BASE = entry.OAUTH_BASE_URL_ENV
ALLOW_EXTERNAL = entry.ALLOW_EXTERNAL_BIND_ENV
TOKEN = entry.MCP_TOKEN_ENV


@pytest.fixture(autouse=True)
def _clear(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (PUBLIC, OAUTH_BASE, ALLOW_EXTERNAL, TOKEN, "NOTEBOOKLM_MCP_OAUTH_PASSWORD"):
        monkeypatch.delenv(var, raising=False)


def test_build_none_when_no_url() -> None:
    assert entry._build_file_transfer() is None


def test_build_from_public_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(PUBLIC, "https://files.example")
    cfg = entry._build_file_transfer()
    assert isinstance(cfg, FileTransferConfig)
    assert cfg.base_url == "https://files.example"
    assert len(cfg.signer.key) == 32  # ephemeral per-process key


def test_build_strips_whitespace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(PUBLIC, "  https://files.example  ")
    cfg = entry._build_file_transfer()
    assert cfg is not None and cfg.base_url == "https://files.example"


def test_build_falls_back_to_oauth_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(OAUTH_BASE, "https://oauth.example")
    cfg = entry._build_file_transfer()
    assert cfg is not None and cfg.base_url == "https://oauth.example"


def test_public_url_wins_over_oauth_base(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(PUBLIC, "https://files.example")
    monkeypatch.setenv(OAUTH_BASE, "https://oauth.example")
    cfg = entry._build_file_transfer()
    assert cfg is not None and cfg.base_url == "https://files.example"


@pytest.mark.parametrize("bad", ["http://files.example", "https://files.example/mcp", "not-a-url"])
def test_invalid_public_url_exits(monkeypatch: pytest.MonkeyPatch, bad: str) -> None:
    monkeypatch.setenv(PUBLIC, bad)
    with pytest.raises(SystemExit):
        entry._build_file_transfer()


def test_http_external_bind_no_url_starts_with_file_transfer_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """External http bind + a bearer + NO public URL → starts fine, file_transfer
    is None (no SystemExit)."""
    monkeypatch.setenv(ALLOW_EXTERNAL, "1")
    monkeypatch.setenv(TOKEN, "a-strong-random-bearer-token")
    seen: dict[str, object] = {}
    fake_server = MagicMock()
    fake_server.run = lambda **k: None

    def fake_create(**kw: object) -> MagicMock:
        seen.update(kw)
        return fake_server

    monkeypatch.setattr(entry, "create_server", fake_create)
    entry.main(["--transport", "http", "--host", "0.0.0.0", "--port", "9123"])
    assert "file_transfer" in seen
    assert seen["file_transfer"] is None


def test_http_with_public_url_threads_config_into_create_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(PUBLIC, "https://files.example")
    seen: dict[str, object] = {}
    fake_server = MagicMock()
    fake_server.run = lambda **k: None

    def fake_create(**kw: object) -> MagicMock:
        seen.update(kw)
        return fake_server

    monkeypatch.setattr(entry, "create_server", fake_create)
    # Loopback http needs no auth; the public URL still builds the config.
    entry.main(["--transport", "http", "--host", "127.0.0.1", "--port", "9123"])
    assert isinstance(seen["file_transfer"], FileTransferConfig)
    assert seen["file_transfer"].base_url == "https://files.example"
