"""Unit tests for the transport-neutral ``mcp install`` core.

Covers the Click-free logic in :mod:`notebooklm._app.mcp_install`:

* the supported-client catalog + per-OS config-path resolution,
* the ``uvx``-based server block builder, and
* the read-modify-merge into ``mcpServers`` that is idempotent and never
  clobbers unrelated keys.

The CLI adapter (``cli/mcp_cmd.py``) owns the atomic file write and Rich
output; these tests exercise the pure functions only.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from notebooklm._app.mcp_install import (
    SERVER_KEY,
    SUPPORTED_CLIENTS,
    UnsupportedClientError,
    build_server_block,
    merge_server_config,
    resolve_config_path,
)
from notebooklm.exceptions import ValidationError


def test_supported_clients_set() -> None:
    assert set(SUPPORTED_CLIENTS) == {
        "claude-desktop",
        "claude-code",
        "cursor",
        "windsurf",
    }


def test_build_server_block_uses_uvx() -> None:
    block = build_server_block()
    assert block == {
        "command": "uvx",
        "args": ["--from", "notebooklm-py[mcp]", "notebooklm-mcp"],
    }
    # A fresh dict each call — callers mutate/merge freely.
    assert build_server_block() is not block


def test_unknown_client_raises_unsupported() -> None:
    with pytest.raises(UnsupportedClientError) as excinfo:
        resolve_config_path("emacs", home=Path("/home/u"))
    # It's a ValidationError subclass so the CLI's classify ladder maps it.
    assert isinstance(excinfo.value, ValidationError)
    # The message lists the supported clients so the user can recover.
    msg = str(excinfo.value)
    assert "emacs" in msg
    for client in SUPPORTED_CLIENTS:
        assert client in msg


# --------------------------------------------------------------------------- #
# resolve_config_path — per-OS locations
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "client,system,expected_rel",
    [
        (
            "claude-desktop",
            "Darwin",
            "Library/Application Support/Claude/claude_desktop_config.json",
        ),
        ("claude-desktop", "Linux", ".config/Claude/claude_desktop_config.json"),
        ("claude-code", "Darwin", ".claude.json"),
        ("claude-code", "Linux", ".claude.json"),
        ("claude-code", "Windows", ".claude.json"),
        # Cursor + Windsurf use a FIXED home-dir dotfile on EVERY OS (vendor docs:
        # ~/.cursor/mcp.json and ~/.codeium/windsurf/mcp_config.json), so the
        # path must NOT change with platform. Pinned on Darwin/Linux/Windows.
        ("cursor", "Darwin", ".cursor/mcp.json"),
        ("cursor", "Linux", ".cursor/mcp.json"),
        ("cursor", "Windows", ".cursor/mcp.json"),
        ("windsurf", "Darwin", ".codeium/windsurf/mcp_config.json"),
        ("windsurf", "Linux", ".codeium/windsurf/mcp_config.json"),
        ("windsurf", "Windows", ".codeium/windsurf/mcp_config.json"),
    ],
)
def test_resolve_config_path(client: str, system: str, expected_rel: str) -> None:
    home = Path("/home/u")
    got = resolve_config_path(client, system=system, home=home)
    assert got == home / expected_rel


def test_resolve_config_path_defaults_to_real_home(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no explicit home, it falls back to Path.home()."""
    fake_home = Path("/tmp/fakehome")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    got = resolve_config_path("cursor", system="Linux")
    assert got == fake_home / ".cursor" / "mcp.json"


# --------------------------------------------------------------------------- #
# merge_server_config — read-modify-merge into mcpServers
# --------------------------------------------------------------------------- #


def test_merge_into_empty_config_creates() -> None:
    new, action = merge_server_config({})
    assert action == "created"
    assert new == {"mcpServers": {SERVER_KEY: build_server_block()}}


def test_merge_preserves_unrelated_keys_and_other_servers() -> None:
    existing = {
        "theme": "dark",
        "mcpServers": {
            "other-server": {"command": "node", "args": ["x.js"]},
        },
        "telemetry": {"enabled": False},
    }
    new, action = merge_server_config(existing)
    assert action == "created"  # our key was absent
    # Unrelated top-level keys survive untouched.
    assert new["theme"] == "dark"
    assert new["telemetry"] == {"enabled": False}
    # The other server survives, ours is added.
    assert new["mcpServers"]["other-server"] == {"command": "node", "args": ["x.js"]}
    assert new["mcpServers"][SERVER_KEY] == build_server_block()


def test_merge_is_idempotent_when_already_present() -> None:
    base, _ = merge_server_config({})
    # Re-merging the same config reports "unchanged" and is a no-op.
    again, action = merge_server_config(base)
    assert action == "unchanged"
    assert again == base


def test_merge_updates_a_stale_block() -> None:
    stale = {
        "mcpServers": {
            SERVER_KEY: {"command": "old-binary", "args": []},
        }
    }
    new, action = merge_server_config(stale)
    assert action == "updated"
    assert new["mcpServers"][SERVER_KEY] == build_server_block()


def test_merge_does_not_mutate_input() -> None:
    existing = {"mcpServers": {"other": {"command": "x"}}}
    snapshot = {"mcpServers": {"other": {"command": "x"}}}
    merge_server_config(existing)
    assert existing == snapshot, "merge_server_config must not mutate its input"


def test_merge_coerces_non_dict_mcpservers() -> None:
    """A corrupt/non-dict ``mcpServers`` value is replaced, not crashed on."""
    new, action = merge_server_config({"mcpServers": ["garbage"]})
    assert action == "created"
    assert new["mcpServers"] == {SERVER_KEY: build_server_block()}


def test_custom_server_key() -> None:
    new, action = merge_server_config({}, server_key="notebooklm-mcp")
    assert action == "created"
    assert "notebooklm-mcp" in new["mcpServers"]
