"""Fail-closed dependency boundary for optional browser acquisition code."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from scripts.audit_auth_import_graph import build_projection

pytestmark = pytest.mark.repo_lint

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src" / "notebooklm"
AUTH_ROOT = SRC_ROOT / "_auth"
BROWSER_ROOT = SRC_ROOT / "_browser"
_ALLOWED_ROOTS = {"_auth", "_browser", "_env", "_url_utils", "config", "exceptions", "paths"}


def _resolved_module(path: Path, node: ast.ImportFrom) -> str:
    if node.level == 0:
        return node.module or ""
    relative = path.relative_to(REPO_ROOT / "src").with_suffix("")
    package = list(relative.parts[:-1])
    base = package[: len(package) - (node.level - 1)]
    if node.module:
        base.extend(node.module.split("."))
    return ".".join(base)


def _notebooklm_roots(path: Path, tree: ast.AST) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                if parts[:1] == ["notebooklm"] and len(parts) > 1:
                    roots.add(parts[1])
        elif isinstance(node, ast.ImportFrom):
            resolved = _resolved_module(path, node)
            parts = resolved.split(".") if resolved else []
            if parts == ["notebooklm"]:
                roots.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif parts[:1] == ["notebooklm"] and len(parts) > 1:
                roots.add(parts[1])
    return roots


def _forbidden_browser_imports(path: Path, tree: ast.AST) -> set[str]:
    return _notebooklm_roots(path, tree) - _ALLOWED_ROOTS


def _auth_browser_or_playwright_imports(path: Path, tree: ast.AST) -> set[str]:
    violations = {root for root in _notebooklm_roots(path, tree) if root == "_browser"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(
                alias.name == "playwright" or alias.name.startswith("playwright.")
                for alias in node.names
            ):
                violations.add("playwright")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "playwright" or module.startswith("playwright."):
                violations.add("playwright")
    return violations


def test_browser_package_uses_only_approved_notebooklm_roots() -> None:
    violations = {
        path.relative_to(SRC_ROOT).as_posix(): sorted(_forbidden_browser_imports(path, tree))
        for path in sorted(BROWSER_ROOT.glob("*.py"))
        if (tree := ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        and _forbidden_browser_imports(path, tree)
    }
    assert violations == {}


def test_auth_package_imports_neither_browser_nor_playwright() -> None:
    violations = {
        path.name: sorted(_auth_browser_or_playwright_imports(path, tree))
        for path in sorted(AUTH_ROOT.glob("*.py"))
        if (tree := ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        and _auth_browser_or_playwright_imports(path, tree)
    }
    assert violations == {}


def test_browser_import_projection_records_exact_cross_package_edges() -> None:
    projection = build_projection(
        BROWSER_ROOT,
        package_prefix="notebooklm._browser",
        include_external=True,
    )
    edges = {(row["source"], row["target"], row["scope"]) for row in projection["edges"]}
    assert edges == {
        ("browser_capture", "_auth.cookie_policy", "module"),
        ("browser_capture", "_auth.cookies", "module"),
        ("browser_capture", "_auth.profile_account", "module"),
        ("browser_capture", "_auth.profile_document", "module"),
        ("browser_capture", "_auth.profile_store", "module"),
        ("browser_capture", "_auth.psidts_recovery", "module"),
        ("browser_capture", "_auth.storage", "module"),
        ("browser_capture", "_env", "module"),
        ("browser_capture", "browser_launch_errors", "module"),
        ("browser_capture", "config", "module"),
        ("browser_capture", "exceptions", "module"),
        ("browser_capture", "navigation_errors", "module"),
        ("headless_reauth", "_auth.recovery_rungs", "module"),
        ("headless_reauth", "browser_capture", "module"),
        ("headless_reauth", "exceptions", "module"),
        ("headless_reauth", "paths", "function"),
        ("headless_reauth", "paths", "module"),
        ("oauth_token", "_auth.master_token_types", "module"),
        ("oauth_token", "browser_capture", "module"),
    }
    assert projection["sccs"] == {"module_level": [], "all_scopes": []}


@pytest.mark.parametrize(
    "source",
    [
        "from .._app import login_browser\n",
        "def lazy():\n    from notebooklm.cli import main\n",
        "if False:\n    import notebooklm._web.transport\n",
        "from notebooklm import auth\n",
    ],
)
def test_browser_dependency_detector_bites_at_every_scope(source: str) -> None:
    path = BROWSER_ROOT / "synthetic.py"
    tree = ast.parse(source)
    assert _forbidden_browser_imports(path, tree)


@pytest.mark.parametrize(
    "source",
    [
        "from .._browser import browser_capture\n",
        "def lazy():\n    import notebooklm._browser.headless_reauth\n",
        "if False:\n    from playwright.sync_api import sync_playwright\n",
    ],
)
def test_auth_browser_detector_bites_at_every_scope(source: str) -> None:
    path = AUTH_ROOT / "synthetic.py"
    tree = ast.parse(source)
    assert _auth_browser_or_playwright_imports(path, tree)
