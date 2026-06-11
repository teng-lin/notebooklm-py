"""Meta-lint: the REST server layer must not import ``click`` / ``rich`` / the CLI.

The single-tenant REST server (``src/notebooklm/server/``) is a transport-neutral
adapter built on the ``_app/`` business-logic layer — a sibling of ``cli/`` and
the drafted ``mcp/``, not a consumer of either. Importing ``click`` / ``rich``
would drag presentation + exit-code concerns into the server, and importing
``notebooklm.cli`` (or a bare ``cli.*``) would couple it to the Click adapter
instead of the neutral core.

This scans every module under ``server/`` (so new route groups are covered
automatically) and fails on any banned import — including inside
``TYPE_CHECKING`` (there is no legitimate reason for the server layer to
reference these even for typing). This mirrors ``test_mcp_boundary.py``; the AST
walk is read directly off the source, so it needs no ``server`` extra installed.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVER_DIR = REPO_ROOT / "src" / "notebooklm" / "server"

# Banned top-level import roots. The server layer must build on ``_app/`` only.
_BANNED_PREFIXES = (
    "click",
    "rich",
    "notebooklm.cli",
    "cli",
)


def _server_files() -> list[Path]:
    return sorted(SERVER_DIR.rglob("*.py"))


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


def test_server_dir_exists() -> None:
    assert SERVER_DIR.is_dir(), f"expected server package at {SERVER_DIR}"


@pytest.mark.parametrize("path", _server_files(), ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_no_click_rich_or_cli_coupling_in_server(path: Path) -> None:
    bad = _violations(path)
    assert not bad, (
        f"{path.relative_to(REPO_ROOT)} imports a forbidden module {bad}. "
        "The server layer must stay click/rich/cli-free; build on the _app/ cores "
        "and the public namespaced client APIs instead."
    )
