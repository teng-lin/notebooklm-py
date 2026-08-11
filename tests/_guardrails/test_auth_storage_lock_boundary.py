"""Dependency and ownership guard for the auth storage-lock leaf."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.repo_lint

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src" / "notebooklm"
AUTH_ROOT = SRC_ROOT / "_auth"
MODULE_PATH = AUTH_ROOT / "storage_lock.py"
ImportRecord = tuple[str, int, str, str, str | None]

_EXPECTED_IMPORTS: list[ImportRecord] = [
    ("module", 0, "__future__", "annotations", None),
    ("module", 0, "contextlib", "", None),
    ("module", 0, "errno", "", None),
    ("module", 0, "logging", "", None),
    ("module", 0, "os", "", None),
    ("module", 0, "random", "", None),
    ("module", 0, "sys", "", None),
    ("module", 0, "threading", "", None),
    ("module", 0, "time", "", None),
    ("module", 0, "collections.abc", "Callable", None),
    ("module", 0, "collections.abc", "Iterator", None),
    ("module", 0, "dataclasses", "dataclass", None),
    ("module", 0, "enum", "Enum", None),
    ("module", 0, "pathlib", "Path", None),
    ("module", 0, "typing", "ClassVar", None),
    ("function", 0, "fcntl", "", None),
    ("function", 0, "msvcrt", "", None),
    ("function", 0, "msvcrt", "", None),
    ("function", 0, "fcntl", "", None),
]
_EXPECTED_PRODUCTION_IMPORTERS = {
    "keepalive.py",
    "master_token_file.py",
    "profile_store.py",
    "storage.py",
}


class _ImportCollector(ast.NodeVisitor):
    def __init__(self) -> None:
        self.function_depth = 0
        self.records: list[ImportRecord] = []

    @property
    def scope(self) -> str:
        return "function" if self.function_depth else "module"

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.function_depth += 1
        self.generic_visit(node)
        self.function_depth -= 1

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Import(self, node: ast.Import) -> None:
        self.records.extend(
            (self.scope, 0, imported.name, "", imported.asname) for imported in node.names
        )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        self.records.extend(
            (self.scope, node.level, node.module or "", imported.name, imported.asname)
            for imported in node.names
        )


def _collect_imports(tree: ast.AST) -> list[ImportRecord]:
    collector = _ImportCollector()
    collector.visit(tree)
    return collector.records


def _violations(tree: ast.AST) -> list[ImportRecord]:
    return [record for record in _collect_imports(tree) if record not in _EXPECTED_IMPORTS]


@pytest.mark.parametrize(
    "source",
    [
        "from .storage import in_storage_transaction\n",
        "def lazy():\n    from .keepalive import _file_lock\n",
        "import notebooklm.auth\n",
        "def lazy():\n    from .._runtime import lifecycle\n",
    ],
)
def test_upward_import_detector_bites_at_module_and_function_scope(source: str) -> None:
    assert _violations(ast.parse(source))


def test_storage_lock_imports_are_exactly_stdlib_and_lazy_platform_modules() -> None:
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"), filename=str(MODULE_PATH))
    assert _collect_imports(tree) == _EXPECTED_IMPORTS
    assert _violations(tree) == []


def _resolved_module(path: Path, node: ast.ImportFrom) -> str:
    if node.level == 0:
        return node.module or ""
    relative = path.relative_to(REPO_ROOT / "src").with_suffix("")
    package = list(relative.parts[:-1])
    keep = len(package) - (node.level - 1)
    base = package[:keep]
    if node.module:
        base.extend(node.module.split("."))
    return ".".join(base)


def _imports_storage_lock(path: Path, tree: ast.AST) -> bool:
    target = "notebooklm._auth.storage_lock"
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(imported.name == target for imported in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            resolved = _resolved_module(path, node)
            if resolved == target or any(
                f"{resolved}.{imported.name}" == target for imported in node.names
            ):
                return True
    return False


@pytest.mark.parametrize(
    "source",
    [
        "from .storage_lock import StorageLockManager\n",
        "def lazy():\n    from . import storage_lock\n",
        "import notebooklm._auth.storage_lock\n",
    ],
)
def test_importer_detector_bites_at_every_scope(source: str) -> None:
    assert _imports_storage_lock(AUTH_ROOT / "synthetic.py", ast.parse(source))


def _assert_exact_production_importers(importers: set[str]) -> None:
    assert importers == _EXPECTED_PRODUCTION_IMPORTERS


def test_production_importers_are_exactly_the_four_approved_owners() -> None:
    importers = {
        path.relative_to(AUTH_ROOT).as_posix()
        for path in AUTH_ROOT.rglob("*.py")
        if path != MODULE_PATH
        and _imports_storage_lock(
            path,
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path)),
        )
    }
    _assert_exact_production_importers(importers)


def test_exact_importer_equality_bites_on_unapproved_fifth_importer() -> None:
    importers = set(_EXPECTED_PRODUCTION_IMPORTERS)
    source = "from .storage_lock import StorageLockManager\n"
    if _imports_storage_lock(AUTH_ROOT / "unapproved.py", ast.parse(source)):
        importers.add("unapproved.py")
    with pytest.raises(AssertionError):
        _assert_exact_production_importers(importers)


def test_storage_lock_leaf_participates_in_no_strongly_connected_component() -> None:
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"), filename=str(MODULE_PATH))
    internal_targets = {
        _resolved_module(MODULE_PATH, node)
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and _resolved_module(MODULE_PATH, node).startswith("notebooklm")
    }
    assert internal_targets == set()
