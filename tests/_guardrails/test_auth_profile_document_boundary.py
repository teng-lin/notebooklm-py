"""Dependency and consumer guards for the lossless profile-document leaf."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.repo_lint

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src" / "notebooklm"
AUTH_ROOT = SRC_ROOT / "_auth"
MODULE_PATH = AUTH_ROOT / "profile_document.py"
ADR_PATH = REPO_ROOT / "docs" / "adr" / "0034-auth-storage-object-model.md"
ImportRecord = tuple[str, int, str, str, str | None]

_ALLOWED_IMPORTS = {
    (0, "__future__", "annotations", None),
    (0, "collections.abc", "Iterable", None),
    (0, "collections.abc", "Mapping", None),
    (0, "copy", "deepcopy", None),
    (0, "dataclasses", "dataclass", None),
    (0, "dataclasses", "field", None),
    (0, "types", "MappingProxyType", None),
    (0, "typing", "Literal", None),
    (1, "cookie_types", "CookieJar", None),
    (1, "profile_account", "AccountView", None),
    (1, "profile_account", "ProfileAccount", None),
    (1, "profile_account", "parse_domain_selection", None),
    (1, "profile_account", "parse_profile_account", None),
}


class _ImportCollector(ast.NodeVisitor):
    def __init__(self) -> None:
        self.function_depth = 0
        self.class_depth = 0
        self.records: list[ImportRecord] = []

    @property
    def scope(self) -> str:
        if self.function_depth:
            return "function"
        if self.class_depth:
            return "class"
        return "module"

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.function_depth += 1
        self.generic_visit(node)
        self.function_depth -= 1

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.class_depth += 1
        self.generic_visit(node)
        self.class_depth -= 1

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


def _dependency_violations(tree: ast.AST) -> list[ImportRecord]:
    return [
        record
        for record in _collect_imports(tree)
        if record[0] != "module" or record[1:] not in _ALLOWED_IMPORTS
    ]


@pytest.mark.parametrize(
    "source",
    [
        pytest.param("from .storage import merge_cookie_delta\n", id="module-persistence"),
        pytest.param(
            "def lazy():\n    from .storage import merge_cookie_delta\n",
            id="lazy-persistence",
        ),
        pytest.param("import notebooklm.auth\n", id="module-facade"),
        pytest.param(
            "def lazy():\n    from .._runtime import lifecycle\n",
            id="lazy-parent-runtime",
        ),
        pytest.param("from ..cli import main\n", id="module-parent-cli"),
        pytest.param(
            "def lazy():\n    from .cookie_types import CookieJar\n",
            id="lazy-approved-leaf",
        ),
        pytest.param(
            "class Lazy:\n    from .cookie_types import CookieJar\n",
            id="class-body-approved-leaf",
        ),
    ],
)
def test_dependency_detector_bites_on_module_and_lazy_upward_imports(source: str) -> None:
    assert _dependency_violations(ast.parse(source))


def test_profile_document_has_only_exact_module_level_leaf_imports() -> None:
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"), filename=str(MODULE_PATH))
    records = _collect_imports(tree)
    assert records == [
        ("module", 0, "__future__", "annotations", None),
        ("module", 0, "collections.abc", "Iterable", None),
        ("module", 0, "collections.abc", "Mapping", None),
        ("module", 0, "copy", "deepcopy", None),
        ("module", 0, "dataclasses", "dataclass", None),
        ("module", 0, "dataclasses", "field", None),
        ("module", 0, "types", "MappingProxyType", None),
        ("module", 0, "typing", "Literal", None),
        ("module", 1, "cookie_types", "CookieJar", None),
        ("module", 1, "profile_account", "AccountView", None),
        ("module", 1, "profile_account", "ProfileAccount", None),
        ("module", 1, "profile_account", "parse_domain_selection", None),
        ("module", 1, "profile_account", "parse_profile_account", None),
    ]
    assert _dependency_violations(tree) == []


def _resolved_import_targets(path: Path, tree: ast.AST) -> set[str]:
    relative = path.relative_to(REPO_ROOT / "src").with_suffix("")
    package = list(relative.parts[:-1])
    targets: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            targets.update(imported.name for imported in node.names)
            continue
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.level:
            keep = len(package) - (node.level - 1)
            base = package[:keep]
            if node.module:
                base.extend(node.module.split("."))
        else:
            base = (node.module or "").split(".") if node.module else []
        if base:
            targets.add(".".join(base))
        for imported in node.names:
            targets.add(".".join([*base, *imported.name.split(".")]))
    return targets


def _imports_profile_document(path: Path, tree: ast.AST) -> bool:
    target = "notebooklm._auth.profile_document"
    return any(
        imported == target or imported.startswith(f"{target}.")
        for imported in _resolved_import_targets(path, tree)
    )


@pytest.mark.parametrize(
    "source",
    [
        "from .profile_document import ProfileDocument\n",
        "def lazy():\n    from . import profile_document\n",
        "import notebooklm._auth.profile_document\n",
    ],
)
def test_production_consumer_detector_bites_at_every_scope(source: str) -> None:
    path = AUTH_ROOT / "synthetic_consumer.py"
    assert _imports_profile_document(path, ast.parse(source))


def test_production_consumers_are_exactly_the_approved_set() -> None:
    consumers = []
    for path in sorted(SRC_ROOT.rglob("*.py")):
        if path == MODULE_PATH:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if _imports_profile_document(path, tree):
            consumers.append(path.relative_to(REPO_ROOT).as_posix())
    assert consumers == [
        "src/notebooklm/_auth/account_email.py",
        "src/notebooklm/_auth/cookie_merge.py",
        "src/notebooklm/_auth/profile_migration.py",
        "src/notebooklm/_auth/profile_store.py",
        "src/notebooklm/_auth/psidts_recovery.py",
        "src/notebooklm/_auth/storage.py",
        "src/notebooklm/_auth/tokens.py",
        "src/notebooklm/_browser/browser_capture.py",
    ]


def test_profile_document_size_and_documentation_pins_hold() -> None:
    # Kept equal to the ordinary module-size ratchet so this consumer boundary
    # cannot retain stale pre-extraction prose or bank facade slack.
    assert len((AUTH_ROOT / "storage.py").read_text(encoding="utf-8").splitlines()) == 1089
    assert len((AUTH_ROOT / "cookies.py").read_text(encoding="utf-8").splitlines()) == 961
    assert len(ADR_PATH.read_text(encoding="utf-8").splitlines()) < 250
