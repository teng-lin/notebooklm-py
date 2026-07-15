"""Guard: an empty ``FASTMCP_STATELESS_HTTP`` must not crash the MCP server at import.

A docker-compose ``${FASTMCP_STATELESS_HTTP:-}`` passthrough injects an empty string when the
operator hasn't set the var, and FastMCP's pydantic ``Settings()`` bool-parses it at import time
and raises on ``''`` — crash-looping the container. ``notebooklm.mcp.__init__`` treats an empty
value as unset before the FastMCP import runs; this pins that behavior against a real subprocess
import (the in-process module is already cached, so the crash only reproduces in a fresh process).
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

pytest.importorskip("fastmcp")


def test_empty_stateless_env_does_not_crash_mcp_import() -> None:
    env = {
        **os.environ,
        "FASTMCP_STATELESS_HTTP": "",
        "PYTHONPATH": os.pathsep.join(p for p in sys.path if p),  # subprocess resolves notebooklm
    }
    result = subprocess.run(
        [sys.executable, "-c", "import notebooklm.mcp; print('imported-ok')"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"import notebooklm.mcp crashed with FASTMCP_STATELESS_HTTP='':\n{result.stderr}"
    )
    assert "imported-ok" in result.stdout


def test_valid_stateless_env_is_left_untouched() -> None:
    # A real bool value must survive the guard (only the empty string is treated as unset).
    env = {
        **os.environ,
        "FASTMCP_STATELESS_HTTP": "true",
        "PYTHONPATH": os.pathsep.join(p for p in sys.path if p),  # subprocess resolves notebooklm
    }
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import notebooklm.mcp, os; print(os.environ.get('FASTMCP_STATELESS_HTTP'))",
        ],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "true"
