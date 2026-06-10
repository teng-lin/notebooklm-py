"""Transport-neutral ``mcp install`` business logic.

The Click-free core of ``cli/mcp_cmd.py``: it owns the supported-MCP-client
catalog, the per-OS config-path resolution, the ``uvx``-based server-block
builder, and the read-modify-merge of that block into a client's ``mcpServers``
config object. The CLI adapter keeps the atomic file write (via
``notebooklm.io.atomic_update_json``), the ``--config-path`` override plumbing,
and the Rich output / exit-code policy.

Every supported client (Claude Desktop, Claude Code, Cursor, Windsurf) reads an
``mcpServers`` JSON object mapping a server name to a ``{command, args}`` block.
The robust default we write runs the server via ``uvx``::

    uvx --from "notebooklm-py[mcp]" notebooklm-mcp

so the install needs only ``uv`` on the host — no global ``pip install`` of the
package, and the same invocation as the ``.mcpb`` desktop bundle's launcher.

This module is transport-neutral — no ``click`` / ``rich`` / ``cli`` /
``fastmcp`` imports (enforced by ``tests/_guardrails/test_app_boundary.py``).
"""

from __future__ import annotations

import copy
import os
import platform as _platform
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..exceptions import ValidationError

__all__ = [
    "MCP_CLIENTS",
    "SERVER_KEY",
    "SUPPORTED_CLIENTS",
    "McpClient",
    "UnsupportedClientError",
    "build_server_block",
    "merge_server_config",
    "resolve_config_path",
]

#: Name of the ``mcpServers`` entry we write. Stable so a re-install updates the
#: same block rather than appending a duplicate.
SERVER_KEY = "notebooklm"

#: PyPI distribution + ``mcp`` extra, and the console script ``uvx`` runs. Kept
#: in lockstep with ``desktop-extension/run_server.py`` (the .mcpb launcher).
_PACKAGE_SPEC = "notebooklm-py[mcp]"
_CONSOLE_SCRIPT = "notebooklm-mcp"


class UnsupportedClientError(ValidationError):
    """The requested MCP client is not one we know how to configure."""


def build_server_block() -> dict[str, Any]:
    """Build the ``{command, args}`` MCP server block (a fresh dict each call).

    Uses ``uvx`` so the install only needs ``uv`` on the host — the package is
    resolved on demand, exactly like the desktop bundle's launcher.
    """
    return {
        "command": "uvx",
        "args": ["--from", _PACKAGE_SPEC, _CONSOLE_SCRIPT],
    }


@dataclass(frozen=True)
class McpClient:
    """A supported MCP client and how to locate its config file.

    ``path_for(system, home)`` returns the absolute config path for the given
    ``platform.system()`` string and home directory. ``config_key`` is the JSON
    object the server block is merged into (every supported client uses
    ``"mcpServers"`` today, but the field keeps that explicit and overridable).
    """

    key: str
    display_name: str
    config_key: str
    path_for: Callable[[str, Path], Path]


def _windows_appdata_base(home: Path) -> Path:
    """Windows roaming-config base: ``%APPDATA%`` (already ``...\\AppData\\Roaming``),
    falling back to that path under ``home`` when the env var is unset."""
    appdata = os.environ.get("APPDATA")
    return Path(appdata) if appdata else home / "AppData" / "Roaming"


def _claude_desktop_path(system: str, home: Path) -> Path:
    if system == "Darwin":
        return home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    if system == "Windows":
        return _windows_appdata_base(home) / "Claude" / "claude_desktop_config.json"
    # Linux / other POSIX.
    return home / ".config" / "Claude" / "claude_desktop_config.json"


def _claude_code_path(system: str, home: Path) -> Path:
    # Claude Code reads user-scope MCP servers from ~/.claude.json (NOT inside
    # the ~/.claude/ directory) on every OS.
    return home / ".claude.json"


def _cursor_path(system: str, home: Path) -> Path:
    # Cursor reads global MCP servers from a FIXED home-dir dotfile on EVERY OS
    # (macOS / Linux / Windows) — there is no XDG / %APPDATA% variant. Vendor
    # docs (https://cursor.com/docs/context/mcp): "Create ~/.cursor/mcp.json in
    # your home directory for tools available everywhere." The ``system`` arg is
    # accepted to match the ``path_for`` signature but intentionally ignored.
    return home / ".cursor" / "mcp.json"


def _windsurf_path(system: str, home: Path) -> Path:
    # Windsurf (Codeium / Cascade) reads MCP servers from a FIXED home-dir
    # dotfile on EVERY OS. Vendor docs
    # (https://docs.windsurf.com/windsurf/cascade/mcp): "The
    # ~/.codeium/windsurf/mcp_config.json file is a JSON file that contains a
    # list of servers that Cascade can connect to." The ``system`` arg is
    # accepted to match the ``path_for`` signature but intentionally ignored.
    return home / ".codeium" / "windsurf" / "mcp_config.json"


#: Catalog of supported clients, keyed by the CLI ``<client>`` argument.
MCP_CLIENTS: dict[str, McpClient] = {
    "claude-desktop": McpClient(
        key="claude-desktop",
        display_name="Claude Desktop",
        config_key="mcpServers",
        path_for=_claude_desktop_path,
    ),
    "claude-code": McpClient(
        key="claude-code",
        display_name="Claude Code",
        config_key="mcpServers",
        path_for=_claude_code_path,
    ),
    "cursor": McpClient(
        key="cursor",
        display_name="Cursor",
        config_key="mcpServers",
        path_for=_cursor_path,
    ),
    "windsurf": McpClient(
        key="windsurf",
        display_name="Windsurf",
        config_key="mcpServers",
        path_for=_windsurf_path,
    ),
}

#: Tuple of supported client keys (stable order for help text / Click choices).
SUPPORTED_CLIENTS: tuple[str, ...] = tuple(MCP_CLIENTS)


def _require_client(client: str) -> McpClient:
    try:
        return MCP_CLIENTS[client]
    except KeyError:
        raise UnsupportedClientError(
            f"Unknown MCP client {client!r}. Supported clients: {', '.join(SUPPORTED_CLIENTS)}."
        ) from None


def resolve_config_path(
    client: str,
    *,
    system: str | None = None,
    home: Path | None = None,
) -> Path:
    """Resolve the absolute MCP-config path for ``client`` on this platform.

    Args:
        client: One of :data:`SUPPORTED_CLIENTS`.
        system: ``platform.system()`` override (``"Darwin"``/``"Linux"``/
            ``"Windows"``); defaults to the real platform. Injectable for tests.
        home: Home directory override; defaults to :meth:`Path.home`.

    Raises:
        UnsupportedClientError: ``client`` is not in the catalog.
    """
    spec = _require_client(client)
    resolved_system = system if system is not None else _platform.system()
    resolved_home = home if home is not None else Path.home()
    return spec.path_for(resolved_system, resolved_home)


def merge_server_config(
    existing: dict[str, Any],
    *,
    server_key: str = SERVER_KEY,
    config_key: str = "mcpServers",
) -> tuple[dict[str, Any], str]:
    """Merge our server block into a client's config, read-modify-merge style.

    Returns ``(new_config, action)`` where ``action`` is:

    * ``"created"`` — our ``server_key`` was absent and is now added,
    * ``"updated"`` — it was present but stale and is now refreshed, or
    * ``"unchanged"`` — it already matched the desired block (a no-op).

    The input is never mutated (a deep copy is taken). Unrelated top-level keys
    and other entries in the ``mcpServers`` object are preserved. A corrupt /
    non-dict ``mcpServers`` value is replaced with a fresh object rather than
    crashing — the file is a config we own a slice of, not a source of truth.
    """
    # A corrupt / non-dict JSON root is replaced with a fresh object rather than
    # crashing on ``.get`` below — the file is a config we own a slice of.
    new_config = copy.deepcopy(existing) if isinstance(existing, dict) else {}

    servers = new_config.get(config_key)
    if not isinstance(servers, dict):
        servers = {}
    new_config[config_key] = servers

    desired = build_server_block()
    current = servers.get(server_key)
    if current == desired:
        action = "unchanged"
    elif server_key in servers:
        action = "updated"
    else:
        action = "created"

    servers[server_key] = desired
    return new_config, action
