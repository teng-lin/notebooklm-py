#!/usr/bin/env python3
"""Resilient launcher for the NotebookLM MCP server in Claude Desktop.

Claude Desktop (and similar MCP hosts) often run with a *restricted* ``PATH``
that omits user-local install dirs, so ``uvx`` may not be discoverable via the
shell ``PATH`` alone. This launcher searches the common install locations before
falling back to ``PATH``, then execs::

    uvx --from "notebooklm-py[mcp]" notebooklm-mcp [<forwarded args>]

forwarding the host's stdin/stdout/stderr through cleanly. The stdio passthrough
is critical: the MCP host speaks JSON-RPC over the child's stdin/stdout, so this
launcher must never write its own bytes to stdout. Diagnostics go to STDERR
only, and a missing ``uvx`` exits non-zero with a clear message.

Bundled inside the ``.mcpb`` extension and invoked via ``manifest.json``::

    "command": "python3", "args": ["${__dirname}/run_server.py"]

The module exposes ``find_uvx`` / ``build_command`` / ``main`` as importable
functions so the bundle can be unit-tested without execing anything.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys

#: The PyPI distribution + ``mcp`` extra to resolve, and the console script to
#: run. ``uvx --from "<PACKAGE>" <CONSOLE_SCRIPT>`` fetches the package (with the
#: extra) into an ephemeral environment and runs the script.
PACKAGE = "notebooklm-py[mcp]"
CONSOLE_SCRIPT = "notebooklm-mcp"


def _candidate_uvx_paths() -> list[str]:
    """Return common ``uvx`` install locations for the current platform."""
    home = os.path.expanduser("~")
    system = platform.system()

    if system == "Windows":
        appdata = os.environ.get("LOCALAPPDATA", os.path.join(home, "AppData", "Local"))
        return [
            os.path.join(home, ".local", "bin", "uvx.exe"),
            os.path.join(home, ".cargo", "bin", "uvx.exe"),
            os.path.join(appdata, "uv", "uvx.exe"),
            os.path.join(home, "scoop", "shims", "uvx.exe"),
        ]

    # POSIX (macOS + Linux): the uv installer drops uvx in ~/.local/bin or
    # ~/.cargo/bin; Homebrew on Apple Silicon uses /opt/homebrew/bin; Intel macOS
    # and most Linux package managers use /usr/local/bin. /snap/bin covers the
    # Ubuntu snap.
    return [
        os.path.join(home, ".local", "bin", "uvx"),
        os.path.join(home, ".cargo", "bin", "uvx"),
        "/opt/homebrew/bin/uvx",
        "/usr/local/bin/uvx",
        "/snap/bin/uvx",
    ]


def find_uvx() -> str | None:
    """Locate the ``uvx`` executable.

    Checks ``PATH`` first (honors a host that already exposes ``uvx``), then the
    common per-platform install locations. Returns the absolute path, or
    ``None`` when ``uvx`` cannot be found anywhere.
    """
    on_path = shutil.which("uvx")
    if on_path:
        return on_path

    for candidate in _candidate_uvx_paths():
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate

    return None


def build_command(uvx: str, argv: list[str]) -> list[str]:
    """Build the ``uvx`` exec argv, forwarding ``argv`` to the console script."""
    return [uvx, "--from", PACKAGE, CONSOLE_SCRIPT, *argv]


def main() -> None:
    """Find ``uvx`` and exec the NotebookLM MCP server, or fail cleanly."""
    uvx = find_uvx()

    if not uvx:
        # STDERR only — stdout is the JSON-RPC channel and must stay pristine.
        print(
            "Error: could not find 'uvx'. Install uv first:\n"
            "  curl -LsSf https://astral.sh/uv/install.sh | sh\n"
            '  # Windows: powershell -c "irm https://astral.sh/uv/install.ps1 | iex"\n'
            "\n"
            "Then restart your MCP host (e.g. Claude Desktop).",
            file=sys.stderr,
        )
        sys.exit(1)

    # Explicit stdin/stdout/stderr passthrough is critical: the MCP host
    # communicates with the server via stdin/stdout JSON-RPC. We do NOT capture
    # them — they flow straight through to the child process.
    cmd = build_command(uvx, sys.argv[1:])
    try:
        result = subprocess.run(  # noqa: S603 - argv is constructed, not shell-interpolated
            cmd,
            stdin=sys.stdin,
            stdout=sys.stdout,
            stderr=sys.stderr,
            check=False,
        )
    except KeyboardInterrupt:
        sys.exit(130)
    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
