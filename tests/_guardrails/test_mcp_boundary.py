"""Meta-lint: the MCP layer must not import ``click`` / ``rich`` / the CLI.

The redesigned MCP server (``src/notebooklm/mcp/``) is a transport-neutral
adapter built on the ``_app/`` business-logic layer — a sibling of ``cli/``, not
a consumer of it. Importing ``click`` / ``rich`` would drag presentation +
exit-code concerns into the server, and importing ``notebooklm.cli`` (or a bare
``cli.*``) would couple it to the Click adapter instead of the neutral core.

This scans every module under ``mcp/`` (so new tool groups are covered
automatically) and fails on any banned import — including inside
``TYPE_CHECKING`` (there is no legitimate reason for the MCP layer to reference
these even for typing).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MCP_DIR = REPO_ROOT / "src" / "notebooklm" / "mcp"

# Banned top-level import roots. The MCP layer must build on ``_app/`` only.
_BANNED_PREFIXES = (
    "click",
    "rich",
    "notebooklm.cli",
    "cli",
)


def _mcp_files() -> list[Path]:
    return sorted(MCP_DIR.rglob("*.py"))


def _is_banned(name: str) -> bool:
    return any(name == prefix or name.startswith(prefix + ".") for prefix in _BANNED_PREFIXES)


def _violations(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    bad: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            bad += [alias.name for alias in node.names if _is_banned(alias.name)]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            if _is_banned(node.module):
                bad.append(node.module)
    return bad


def test_mcp_dir_exists() -> None:
    assert MCP_DIR.is_dir(), f"expected MCP package at {MCP_DIR}"


@pytest.mark.parametrize("path", _mcp_files(), ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_no_click_rich_or_cli_coupling_in_mcp(path: Path) -> None:
    bad = _violations(path)
    assert not bad, (
        f"{path.relative_to(REPO_ROOT)} imports a forbidden module {bad}. "
        "The MCP layer must stay click/rich/cli-free; build on the _app/ cores "
        "and the public namespaced client APIs instead."
    )
