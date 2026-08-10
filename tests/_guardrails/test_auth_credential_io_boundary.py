"""Dependency and capability guard for the sealed credential commit spine."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.repo_lint

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src" / "notebooklm"
AUTH_ROOT = SRC_ROOT / "_auth"
MODULE_PATH = AUTH_ROOT / "credential_io.py"
ImportRecord = tuple[str, int, str, str, str | None]

_BYPASS = "_atomic_write_json_unchecked"
_RAW_COMMIT = "_commit_json_unchecked"
_TYPED_COMMITS = frozenset({"_commit_profile_json", "_commit_master_token_json"})

_EXPECTED_IMPORTS: list[ImportRecord] = [
    ("module", 0, "__future__", "annotations", None),
    ("module", 0, "collections.abc", "Mapping", None),
    ("module", 0, "pathlib", "Path", None),
    ("module", 2, "_atomic_io", _BYPASS, None),
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
        self.records.extend((self.scope, 0, item.name, "", item.asname) for item in node.names)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        self.records.extend(
            (self.scope, node.level, node.module or "", item.name, item.asname)
            for item in node.names
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
        pytest.param("def lazy():\n    import logging\n", id="function-logging"),
        pytest.param("class Lazy:\n    from .paths import storage_path\n", id="class-paths"),
        pytest.param("import notebooklm._atomic_io as atomic\n", id="atomic-module-object"),
        pytest.param("import notebooklm.auth\n", id="public-facade"),
        pytest.param("def lazy():\n    from .._runtime import lifecycle\n", id="runtime"),
        pytest.param("class Lazy:\n    from ..cli import main\n", id="cli"),
        pytest.param(
            "def lazy():\n    from .._atomic_io import _atomic_write_json_unchecked\n",
            id="lazy-bypass",
        ),
    ],
)
def test_dependency_detector_bites_at_module_function_and_class_scope(source: str) -> None:
    assert _dependency_violations(ast.parse(source))


def test_credential_io_imports_are_exact_and_module_level() -> None:
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


def _imports_credential_io(path: Path, tree: ast.AST) -> bool:
    target = "notebooklm._auth.credential_io"
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(item.name == target for item in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            resolved = _resolved_module(path, node)
            if resolved == target or any(
                f"{resolved}.{item.name}" == target for item in node.names
            ):
                return True
    return False


@pytest.mark.parametrize(
    "source",
    [
        "from .credential_io import _commit_profile_json\n",
        "def lazy():\n    from . import credential_io\n",
        "class Lazy:\n    import notebooklm._auth.credential_io\n",
    ],
)
def test_importer_detector_bites_at_every_scope(source: str) -> None:
    assert _imports_credential_io(AUTH_ROOT / "synthetic.py", ast.parse(source))


def test_production_importers_are_exactly_profile_store_and_master_token_file() -> None:
    actual = {
        path.relative_to(AUTH_ROOT).as_posix()
        for path in AUTH_ROOT.rglob("*.py")
        if path != MODULE_PATH
        and _imports_credential_io(
            path,
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path)),
        )
    }
    assert actual == {"master_token_file.py", "profile_store.py"}


def _top_level_functions(tree: ast.Module) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    return {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }


def _direct_name_calls(node: ast.AST) -> list[str]:
    return [
        inner.func.id
        for inner in ast.walk(node)
        if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name)
    ]


def _name_escapes(tree: ast.Module, protected: set[str]) -> list[tuple[str, int]]:
    callees = {
        id(node.func)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in protected
    }
    return sorted(
        (node.id, node.lineno)
        for node in ast.walk(tree)
        if isinstance(node, ast.Name)
        and node.id in protected
        and isinstance(node.ctx, ast.Load)
        and id(node) not in callees
    )


def test_capability_call_graph_is_exact_and_has_no_escape() -> None:
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"), filename=str(MODULE_PATH))
    functions = _top_level_functions(tree)
    assert set(functions) == {_RAW_COMMIT, *_TYPED_COMMITS}
    assert [name for name in _direct_name_calls(functions[_RAW_COMMIT]) if name == _BYPASS] == [
        _BYPASS
    ]
    assert {
        owner for owner, node in functions.items() if _RAW_COMMIT in _direct_name_calls(node)
    } == _TYPED_COMMITS
    assert all(
        _direct_name_calls(functions[owner]).count(_RAW_COMMIT) == 1 for owner in _TYPED_COMMITS
    )
    assert _name_escapes(tree, {_BYPASS, _RAW_COMMIT, *_TYPED_COMMITS}) == []


@pytest.mark.parametrize(
    "body",
    [
        "def forbidden():\n    return _atomic_write_json_unchecked\n",
        "def forbidden():\n    sink(_commit_json_unchecked)\n",
        "def forbidden(fn=_commit_profile_json):\n    return fn\n",
        "WRITERS = {_commit_master_token_json}\n",
        "class Injected:\n    writer = _commit_profile_json\n",
        "def forbidden():\n    return Service(commit=_commit_profile_json)\n",
    ],
)
def test_capability_escape_detector_bites(body: str) -> None:
    preamble = (
        "from .._atomic_io import _atomic_write_json_unchecked\n"
        "def _commit_json_unchecked(path, payload):\n"
        "    _atomic_write_json_unchecked(path, payload)\n"
        "def _commit_profile_json(path, payload):\n"
        "    _commit_json_unchecked(path, payload)\n"
        "def _commit_master_token_json(path, payload):\n"
        "    _commit_json_unchecked(path, payload)\n"
    )
    assert _name_escapes(
        ast.parse(preamble + body),
        {_BYPASS, _RAW_COMMIT, *_TYPED_COMMITS},
    )


def test_extra_raw_capability_caller_is_rejected() -> None:
    tree = ast.parse(
        "def _commit_json_unchecked(path, payload):\n    pass\n"
        "def _commit_profile_json(path, payload):\n"
        "    _commit_json_unchecked(path, payload)\n"
        "def _commit_master_token_json(path, payload):\n"
        "    _commit_json_unchecked(path, payload)\n"
        "def forbidden(path, payload):\n    _commit_json_unchecked(path, payload)\n"
    )
    actual = {
        owner
        for owner, node in _top_level_functions(tree).items()
        if _RAW_COMMIT in _direct_name_calls(node)
    }
    assert actual == {*_TYPED_COMMITS, "forbidden"}
    assert actual != _TYPED_COMMITS


def test_credential_io_leaf_participates_in_no_auth_scc() -> None:
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"), filename=str(MODULE_PATH))
    internal = {
        _resolved_module(MODULE_PATH, node)
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and _resolved_module(MODULE_PATH, node).startswith("notebooklm._auth")
    }
    assert internal == set()
