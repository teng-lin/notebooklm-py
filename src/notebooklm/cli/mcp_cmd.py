"""``notebooklm mcp`` command group — wire the MCP server into MCP clients.

Thin Click adapter over the transport-neutral
:mod:`notebooklm._app.mcp_install` core. The supported-client catalog, the
per-OS config-path resolution, the ``uvx`` server-block builder, and the
read-modify-merge into ``mcpServers`` all live in ``_app``; this module imports
those names into its own namespace (so ``patch.object(mcp_cmd, ...)`` test seams
and ``from notebooklm.cli.mcp_cmd import ...`` keep resolving) and owns the
Click I/O, the atomic file write, and the exit-code policy.

The merge is applied **inside** :func:`notebooklm.io.atomic_update_json` so the
read-modify-write of the client's config file is locked, crash-safe, and never
clobbers unrelated keys (the file belongs to the client; we only own our one
``mcpServers`` entry).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import click

from .._app.mcp_install import (
    SERVER_KEY,
    SUPPORTED_CLIENTS,
    build_server_block,
    merge_server_config,
    resolve_config_path,
)
from ..io import atomic_update_json
from .error_handler import handle_errors
from .rendering import console

__all__ = [
    "SERVER_KEY",
    "SUPPORTED_CLIENTS",
    "build_server_block",
    "merge_server_config",
    "mcp",
    "resolve_config_path",
]


@click.group()
def mcp() -> None:
    """Wire the notebooklm-py MCP server into your MCP client(s).

    The ``mcp`` group is binned into the "Command Groups" help section in
    ``cli/grouped.py``, so the no-orphans guardrail in
    ``tests/unit/cli/test_grouped.py`` is satisfied by that binning (no
    ``category`` tag needed).
    """


@mcp.command(name="install")
@click.argument("client", type=click.Choice(SUPPORTED_CLIENTS))
@click.option(
    "--config-path",
    "config_path",
    type=click.Path(path_type=Path),
    default=None,
    help=(
        "Override the MCP-config file to write (default: the client's standard "
        "per-OS location). Useful for testing or non-standard installs."
    ),
)
def install(client: str, config_path: Path | None) -> None:
    """Add the notebooklm-py MCP server to CLIENT's config.

    \b
    Supported clients:
      claude-desktop   Claude Desktop app
      claude-code      Claude Code (user scope, ~/.claude.json)
      cursor           Cursor editor
      windsurf         Windsurf (Codeium) editor

    Writes a server block that launches the server via 'uvx' (so only 'uv'
    needs to be installed). Re-running is idempotent and never clobbers other
    servers or unrelated keys in the config file.
    """
    # Route every failure through the shared CLI error handler so a corrupt
    # target config (``json.JSONDecodeError``), a contended lock
    # (``filelock.Timeout``), a filesystem error (``OSError``), or an
    # unsupported client (``UnsupportedClientError`` / ``ValidationError``)
    # surfaces as a friendly message + nonzero exit instead of a raw traceback.
    # We deliberately do NOT pass ``recover_from_corrupt`` to
    # ``atomic_update_json`` — refusing to clobber an unparseable file the
    # client owns is the safe behavior; the user gets a clear error and an
    # intact file rather than a silently-rewritten config.
    with handle_errors():
        target = config_path if config_path is not None else resolve_config_path(client)

        # The merge is pure; running it as the atomic_update_json mutator makes
        # the read-modify-write locked + crash-safe and preserves every other
        # key. The action ("created"/"updated"/"unchanged") is captured out of
        # the closure.
        captured: dict[str, str] = {}

        def _mutate(current: dict[str, Any]) -> dict[str, Any]:
            new_config, action = merge_server_config(current)
            captured["action"] = action
            return new_config

        atomic_update_json(target, _mutate)

    action = captured.get("action", "created")
    if action == "unchanged":
        console.print(
            f"[green]Already configured[/green] — '{SERVER_KEY}' is up to date in {target}"
        )
        return

    verb = "Updated" if action == "updated" else "Installed"
    console.print(f"[green]{verb}[/green] the notebooklm-py MCP server for [cyan]{client}[/cyan]")
    console.print(f"  Config: {target}")
    console.print(f"  Server: {SERVER_KEY}")
    console.print("")
    console.print(
        f"Restart {client} to load the server. First run will prompt for Google auth "
        "if you have not run [cyan]notebooklm login[/cyan] yet."
    )
