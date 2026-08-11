"""Dependency, caller, and cycle guards for the pure cookie-merge leaf."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.repo_lint

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src" / "notebooklm"
AUTH_ROOT = SRC_ROOT / "_auth"
MODULE_PATH = AUTH_ROOT / "cookie_merge.py"
ImportRecord = tuple[str, int, str, str, str | None]
Caller = tuple[str, str, str]

_DECISIONS = frozenset({"decide_cookie_merge", "decide_legacy_cookie_overlay"})

_EXPECTED_IMPORTS: list[ImportRecord] = [
    ("module", 0, "__future__", "annotations", None),
    ("module", 0, "collections.abc", "Mapping", None),
    ("module", 0, "dataclasses", "dataclass", None),
    ("module", 0, "dataclasses", "field", None),
    ("module", 0, "types", "MappingProxyType", None),
    ("module", 0, "typing", "Literal", None),
    ("module", 1, "", "cookie_policy", "_cookie_policy"),
    ("module", 1, "", "cookie_semantics", "_cookie_semantics"),
    ("module", 1, "cookie_types", "Cookie", None),
    ("module", 1, "cookie_types", "CookieIdentity", None),
    ("module", 1, "cookie_types", "CookieJar", None),
    ("module", 1, "profile_document", "ProfileDocument", None),
]


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
    return [record for record in _collect_imports(tree) if record not in _EXPECTED_IMPORTS]


@pytest.mark.parametrize(
    "source",
    [
        pytest.param("from .storage import merge_cookie_delta\n", id="module-storage"),
        pytest.param("def lazy():\n    import httpx\n", id="function-httpx"),
        pytest.param("class Lazy:\n    from .cookies import Cookie\n", id="class-cookies"),
        pytest.param("import notebooklm.auth\n", id="module-facade"),
        pytest.param(
            "def lazy():\n    from .._runtime import lifecycle\n",
            id="function-runtime",
        ),
        pytest.param("class Lazy:\n    from ..cli import main\n", id="class-cli"),
        pytest.param(
            "def lazy():\n    from .cookie_types import CookieJar\n",
            id="lazy-approved-leaf",
        ),
    ],
)
def test_dependency_detector_bites_at_module_function_and_class_scope(source: str) -> None:
    assert _dependency_violations(ast.parse(source))


def test_cookie_merge_imports_are_exactly_the_approved_dependency_bottom_set() -> None:
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"), filename=str(MODULE_PATH))
    assert _collect_imports(tree) == _EXPECTED_IMPORTS
    assert _dependency_violations(tree) == []


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


def _imports_cookie_merge(path: Path, tree: ast.AST) -> bool:
    target = "notebooklm._auth.cookie_merge"
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(
                imported.name == target or imported.name.startswith(f"{target}.")
                for imported in node.names
            ):
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
        "from .cookie_merge import decide_cookie_merge\n",
        "def lazy():\n    from . import cookie_merge\n",
        "class Lazy:\n    import notebooklm._auth.cookie_merge\n",
    ],
)
def test_production_importer_detector_bites_at_every_scope(source: str) -> None:
    assert _imports_cookie_merge(AUTH_ROOT / "synthetic.py", ast.parse(source))


def test_production_importers_are_exactly_profile_store_and_storage() -> None:
    importers = {
        path.relative_to(AUTH_ROOT).as_posix()
        for path in AUTH_ROOT.rglob("*.py")
        if path != MODULE_PATH
        and _imports_cookie_merge(
            path,
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path)),
        )
    }
    assert importers == {"profile_store.py", "psidts_recovery.py", "storage.py"}


def _decision_bindings(path: Path, tree: ast.AST) -> tuple[dict[str, str], set[str]]:
    names: dict[str, str] = {}
    modules: set[str] = set()
    target = "notebooklm._auth.cookie_merge"
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and _resolved_module(path, node) == target:
            for imported in node.names:
                if imported.name in _DECISIONS:
                    names[imported.asname or imported.name] = imported.name
        elif (
            isinstance(node, ast.ImportFrom) and _resolved_module(path, node) == "notebooklm._auth"
        ):
            for imported in node.names:
                if imported.name == "cookie_merge":
                    modules.add(imported.asname or imported.name)
        elif isinstance(node, ast.Import):
            for imported in node.names:
                if imported.name == target:
                    modules.add(imported.asname or imported.name)
    return names, modules


def _qualified_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        owner = _qualified_name(node.value)
        if owner is not None:
            return f"{owner}.{node.attr}"
    return None


class _DecisionCallCollector(ast.NodeVisitor):
    def __init__(self, module: str, names: dict[str, str], modules: set[str]) -> None:
        self.module = module
        self.names = names
        self.modules = modules
        self.function_stack: list[str] = []
        self.calls: set[Caller] = set()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.function_stack.append(node.name)
        self.generic_visit(node)
        self.function_stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Call(self, node: ast.Call) -> None:
        decision: str | None = None
        if isinstance(node.func, ast.Name):
            decision = self.names.get(node.func.id)
        else:
            qualified = _qualified_name(node.func)
            if qualified is not None:
                module, separator, member = qualified.rpartition(".")
                if separator and module in self.modules and member in _DECISIONS:
                    decision = member
        if decision is not None:
            owner = self.function_stack[0] if self.function_stack else "<module>"
            self.calls.add((self.module, owner, decision))
        self.generic_visit(node)


def _decision_callers(path: Path, tree: ast.AST) -> set[Caller]:
    names, modules = _decision_bindings(path, tree)
    collector = _DecisionCallCollector(path.relative_to(AUTH_ROOT).as_posix(), names, modules)
    collector.visit(tree)
    return collector.calls


_EXPECTED_CALLERS: set[Caller] = {
    ("profile_store.py", "merge_cookie_observation", "decide_cookie_merge"),
    (
        "profile_store.py",
        "merge_legacy_cookie_observation",
        "decide_legacy_cookie_overlay",
    ),
    ("storage.py", "_merge_cookies_legacy", "decide_legacy_cookie_overlay"),
    ("storage.py", "_merge_cookies_with_snapshot", "decide_cookie_merge"),
}


def test_direct_production_callers_are_function_granular_and_exact() -> None:
    actual: set[Caller] = set()
    for path in sorted(AUTH_ROOT.rglob("*.py")):
        if path == MODULE_PATH:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        actual.update(_decision_callers(path, tree))
    assert actual == _EXPECTED_CALLERS


def test_synthetic_extra_production_caller_is_rejected() -> None:
    path = AUTH_ROOT / "unexpected.py"
    tree = ast.parse(
        "from .cookie_merge import decide_cookie_merge as decide\n"
        "class Service:\n"
        "    def run(self):\n"
        "        return decide(stored=None, observation=None, baseline=None, "
        "recovery_observation=None)\n"
    )
    actual = _decision_callers(path, tree)
    assert actual == {("unexpected.py", "run", "decide_cookie_merge")}
    assert not actual <= _EXPECTED_CALLERS


def test_unaliased_absolute_import_cannot_hide_extra_storage_caller() -> None:
    path = AUTH_ROOT / "storage.py"
    tree = ast.parse(
        "import notebooklm._auth.cookie_merge\n"
        "def forbidden():\n"
        "    return notebooklm._auth.cookie_merge.decide_cookie_merge(\n"
        "        stored=None, observation=None, baseline=None, recovery_observation=None\n"
        "    )\n"
    )
    assert _imports_cookie_merge(path, tree)
    actual = _decision_callers(path, tree)
    assert actual == {("storage.py", "forbidden", "decide_cookie_merge")}
    assert not actual <= _EXPECTED_CALLERS


def _auth_edges() -> dict[str, set[str]]:
    modules = {path.stem for path in AUTH_ROOT.glob("*.py")}
    graph = {module: set() for module in modules}
    prefix = "notebooklm._auth."
    for path in AUTH_ROOT.glob("*.py"):
        source = path.stem
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            targets: list[str] = []
            if isinstance(node, ast.Import):
                targets = [imported.name for imported in node.names]
            elif isinstance(node, ast.ImportFrom):
                resolved = _resolved_module(path, node)
                targets = [resolved, *(f"{resolved}.{item.name}" for item in node.names)]
            for target in targets:
                if target.startswith(prefix):
                    candidate = target.removeprefix(prefix).split(".", 1)[0]
                    if candidate in modules and candidate != source:
                        graph[source].add(candidate)
    return graph


def _reachable(graph: dict[str, set[str]], source: str, target: str) -> bool:
    pending = list(graph.get(source, set()))
    seen: set[str] = set()
    while pending:
        node = pending.pop()
        if node == target:
            return True
        if node not in seen:
            seen.add(node)
            pending.extend(graph.get(node, set()))
    return False


def test_cookie_merge_leaf_participates_in_no_strongly_connected_component() -> None:
    graph = _auth_edges()
    assert not any(
        _reachable(graph, "cookie_merge", module) and _reachable(graph, module, "cookie_merge")
        for module in graph
        if module != "cookie_merge"
    )


def test_cycle_detector_bites_on_a_synthetic_two_node_cycle() -> None:
    graph = {"cookie_merge": {"profile_document"}, "profile_document": {"cookie_merge"}}
    assert _reachable(graph, "cookie_merge", "profile_document")
    assert _reachable(graph, "profile_document", "cookie_merge")
