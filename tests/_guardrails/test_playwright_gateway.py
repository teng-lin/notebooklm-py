"""Keep raw synchronous Playwright startup behind its Windows-safe gateway.

The CLI installs ``WindowsSelectorEventLoopPolicy`` on Windows, while
Playwright needs the default Proactor policy to start its driver subprocess.
Every raw ``sync_playwright`` entry point must therefore stay inside the one
gateway that swaps and restores the policy. This guard prevents a new caller
from repeating #2011 by bypassing that gateway.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src" / "notebooklm"
GATEWAY = ("src/notebooklm/_browser/browser_capture.py", "sync_playwright_context")


class _RawSyncPlaywrightVisitor(ast.NodeVisitor):
    def __init__(self, path: Path) -> None:
        self._path = path.relative_to(REPO_ROOT).as_posix()
        self._owners = ["<module>"]
        self.sites: set[tuple[str, str, int]] = set()

    def _record(self, node: ast.AST) -> None:
        self.sites.add((self._path, self._owners[-1], node.lineno))

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self._owners.append(node.name)
        self.generic_visit(node)
        self._owners.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module == "playwright.sync_api" and any(
            alias.name == "sync_playwright" for alias in node.names
        ):
            self._record(node)
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id == "sync_playwright":
            self._record(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr == "sync_playwright":
            self._record(node)
        self.generic_visit(node)


def test_raw_sync_playwright_is_confined_to_policy_gateway() -> None:
    sites: set[tuple[str, str, int]] = set()
    for path in SRC_ROOT.rglob("*.py"):
        visitor = _RawSyncPlaywrightVisitor(path)
        visitor.visit(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        sites.update(visitor.sites)

    owners = {(path, owner) for path, owner, _line in sites}
    assert owners == {GATEWAY}, (
        f"Raw sync_playwright use must stay inside the policy-safe gateway; found {sorted(sites)}"
    )
