"""Dependency, identity, and caller guard for the cookie-filter leaf."""

from __future__ import annotations

import ast
import logging
from pathlib import Path

import pytest

import notebooklm.auth as auth
from notebooklm._auth import cookie_filter, storage
from notebooklm._browser import browser_capture

pytestmark = pytest.mark.repo_lint

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src" / "notebooklm"
AUTH_ROOT = SRC_ROOT / "_auth"
MODULE_PATH = AUTH_ROOT / "cookie_filter.py"
TARGET = "filter_storage_state_cookies_by_domain_policy"
ImportRecord = tuple[str, int, str, str, str | None]
Caller = tuple[str, str]

_EXPECTED_IMPORTS: list[ImportRecord] = [
    ("module", 0, "__future__", "annotations", None),
    ("module", 0, "logging", "", None),
    ("module", 0, "typing", "Any", None),
    ("module", 1, "", "cookie_policy", "_cookie_policy"),
    ("module", 1, "", "cookie_semantics", "_cookie_semantics"),
]


class _ImportCollector(ast.NodeVisitor):
    def __init__(self) -> None:
        self.stack: list[str] = []
        self.records: list[ImportRecord] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.stack.append("function")
        self.generic_visit(node)
        self.stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.stack.append("class")
        self.generic_visit(node)
        self.stack.pop()

    def visit_Import(self, node: ast.Import) -> None:
        scope = self.stack[-1] if self.stack else "module"
        self.records.extend((scope, 0, item.name, "", item.asname) for item in node.names)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        scope = self.stack[-1] if self.stack else "module"
        self.records.extend(
            (scope, node.level, node.module or "", item.name, item.asname) for item in node.names
        )


def _imports(tree: ast.AST) -> list[ImportRecord]:
    collector = _ImportCollector()
    collector.visit(tree)
    return collector.records


def _dependency_violations(tree: ast.AST) -> list[ImportRecord]:
    return [record for record in _imports(tree) if record not in _EXPECTED_IMPORTS]


@pytest.mark.parametrize(
    "source",
    [
        "from .storage import WriteOutcome\n",
        "def lazy():\n    from .profile_store import ProfileStore\n",
        "class Lazy:\n    from ..cli import main\n",
        "from .browser_capture import run_browser_capture\n",
    ],
)
def test_dependency_detector_bites_at_every_scope(source: str) -> None:
    assert _dependency_violations(ast.parse(source))


def test_cookie_filter_imports_are_exact_and_module_level() -> None:
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"), filename=str(MODULE_PATH))
    assert _imports(tree) == _EXPECTED_IMPORTS
    assert _dependency_violations(tree) == []


def _imports_cookie_filter(path: Path, tree: ast.AST) -> bool:
    target = "notebooklm._auth.cookie_filter"
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
        "from .cookie_filter import filter_storage_state_cookies_by_domain_policy\n",
        "def lazy():\n    from . import cookie_filter\n",
        "class Lazy:\n    import notebooklm._auth.cookie_filter\n",
    ],
)
def test_importer_detector_bites_at_every_scope(source: str) -> None:
    assert _imports_cookie_filter(AUTH_ROOT / "synthetic.py", ast.parse(source))


def test_direct_production_importers_are_exactly_store_and_storage() -> None:
    actual = {
        path.relative_to(AUTH_ROOT).as_posix()
        for path in AUTH_ROOT.rglob("*.py")
        if path != MODULE_PATH
        and _imports_cookie_filter(
            path,
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path)),
        )
    }
    assert actual == {"profile_store.py", "storage.py"}


_OWNED_DEFINITIONS = {
    "_safe_cookie_shape",
    "_MALFORMED_ROW_WARNINGS",
    "_report_malformed_row",
    TARGET,
}


def _top_level_definitions(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    definitions: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            definitions.add(node.name)
        elif isinstance(node, ast.Assign):
            definitions.update(target.id for target in node.targets if isinstance(target, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            definitions.add(node.target.id)
    return definitions


def test_four_filter_definitions_have_one_owner_and_no_copies() -> None:
    owners = {
        path.relative_to(AUTH_ROOT).as_posix(): _top_level_definitions(path) & _OWNED_DEFINITIONS
        for path in AUTH_ROOT.rglob("*.py")
    }
    assert {path: names for path, names in owners.items() if names} == {
        "cookie_filter.py": _OWNED_DEFINITIONS
    }


def test_compatibility_aliases_and_logger_are_exact() -> None:
    canonical = cookie_filter.filter_storage_state_cookies_by_domain_policy
    assert storage._safe_cookie_shape is cookie_filter._safe_cookie_shape
    assert browser_capture._safe_cookie_shape is storage._safe_cookie_shape
    assert storage.filter_storage_state_cookies_by_domain_policy is canonical
    assert browser_capture.filter_storage_state_cookies_by_domain_policy is canonical
    assert auth.filter_storage_state_cookies_by_domain_policy is canonical
    assert cookie_filter.logger is logging.getLogger("notebooklm.auth")


def test_malformed_diagnostics_never_render_a_raw_value(caplog: pytest.LogCaptureFixture) -> None:
    secret = "raw-cookie-value-must-not-escape"
    row = {
        "name": 17,
        "value": secret,
        "domain": ".google.com",
        "path": "/",
        "unknown": {"nested": secret},
    }
    with caplog.at_level(logging.WARNING, logger="notebooklm.auth"):
        result = cookie_filter.filter_storage_state_cookies_by_domain_policy({"cookies": [row]})
    assert result == {"cookies": [], "origins": []}
    assert secret not in cookie_filter._safe_cookie_shape(row)
    assert secret not in caplog.text


def _resolved_module(path: Path, node: ast.ImportFrom) -> str:
    if node.level == 0:
        return node.module or ""
    relative = path.relative_to(REPO_ROOT / "src").with_suffix("")
    package = list(relative.parts[:-1])
    base = package[: len(package) - (node.level - 1)]
    if node.module:
        base.extend(node.module.split("."))
    return ".".join(base)


_PROVIDERS = {
    "notebooklm._auth.cookie_filter",
    "notebooklm._auth.profile_store",
    "notebooklm._auth.storage",
    "notebooklm._browser.browser_capture",
    "notebooklm.auth",
}


def _qualified_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        owner = _qualified_name(node.value)
        if owner is not None:
            return f"{owner}.{node.attr}"
    return None


def _bindings(path: Path, tree: ast.AST) -> tuple[set[str], set[str]]:
    names: set[str] = {TARGET} if path == MODULE_PATH else set()
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            resolved = _resolved_module(path, node)
            if resolved in _PROVIDERS:
                names.update(item.asname or item.name for item in node.names if item.name == TARGET)
            modules.update(
                item.asname or item.name
                for item in node.names
                if f"{resolved}.{item.name}" in _PROVIDERS
            )
        elif isinstance(node, ast.Import):
            modules.update(
                item.asname or item.name for item in node.names if item.name in _PROVIDERS
            )
    return names, modules


class _CallCollector(ast.NodeVisitor):
    def __init__(self, module: str, names: set[str], modules: set[str]) -> None:
        self.module = module
        self.names = names
        self.modules = modules
        self.classes: list[str] = []
        self.functions: list[str] = []
        self.calls: set[Caller] = set()
        self.escapes: set[Caller] = set()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.classes.append(node.name)
        self.generic_visit(node)
        self.classes.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.functions.append(node.name)
        self.generic_visit(node)
        self.functions.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def _owner(self) -> Caller:
        owner = ".".join([*self.classes, *self.functions[:1]]) or "<module>"
        return self.module, owner

    def _is_filter_reference(self, node: ast.expr) -> bool:
        if isinstance(node, ast.Name):
            return node.id in self.names
        name = _qualified_name(node)
        module, separator, member = (name or "").rpartition(".")
        return bool(separator and module in self.modules and member == TARGET)

    def visit_Call(self, node: ast.Call) -> None:
        if self._is_filter_reference(node.func):
            self.calls.add(self._owner())
            for argument in node.args:
                self.visit(argument)
            for keyword in node.keywords:
                self.visit(keyword.value)
            return
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load) and self._is_filter_reference(node):
            self.escapes.add(self._owner())

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if isinstance(node.ctx, ast.Load) and self._is_filter_reference(node):
            self.escapes.add(self._owner())
            return
        self.generic_visit(node)


def _filter_calls(path: Path, tree: ast.AST) -> set[Caller]:
    names, modules = _bindings(path, tree)
    collector = _CallCollector(path.relative_to(SRC_ROOT).as_posix(), names, modules)
    collector.visit(tree)
    return collector.calls


def _filter_escapes(path: Path, tree: ast.AST) -> set[Caller]:
    names, modules = _bindings(path, tree)
    collector = _CallCollector(path.relative_to(SRC_ROOT).as_posix(), names, modules)
    collector.visit(tree)
    return collector.escapes


def test_filter_behavior_callers_are_exact() -> None:
    actual: set[Caller] = set()
    escapes: set[Caller] = set()
    for path in sorted(SRC_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        actual.update(_filter_calls(path, tree))
        escapes.update(_filter_escapes(path, tree))
    assert actual == {
        ("_auth/profile_store.py", "ProfileStore.replace_from_login"),
        ("_auth/profile_store.py", "ProfileStore.replace_from_remint"),
        ("_auth/profile_store.py", "ProfileStore.replace_minted_session"),
        ("_browser/browser_capture.py", "run_browser_capture"),
        ("_browser/browser_capture.py", "run_cdp_capture"),
    }
    assert escapes == {
        ("auth.py", "<module>"),
        ("cli/_cookie_import.py", "_import_cookie_json"),
    }


def test_alias_detectors_bite_and_a_seventh_caller_is_visible() -> None:
    direct = ast.parse(
        "from notebooklm._auth.cookie_filter import "
        "filter_storage_state_cookies_by_domain_policy as filt\n"
        "def seventh(state):\n    return filt(state)\n"
    )
    assert _filter_calls(SRC_ROOT / "synthetic.py", direct) == {("synthetic.py", "seventh")}
    module = ast.parse(
        "from notebooklm._auth import cookie_filter as cf\n"
        "def seventh(state):\n    return cf.filter_storage_state_cookies_by_domain_policy(state)\n"
    )
    assert _filter_calls(SRC_ROOT / "synthetic.py", module) == {("synthetic.py", "seventh")}
    absolute = ast.parse(
        "import notebooklm._auth.cookie_filter\n"
        "def seventh(state):\n"
        "    return notebooklm._auth.cookie_filter."
        "filter_storage_state_cookies_by_domain_policy(state)\n"
    )
    assert _imports_cookie_filter(SRC_ROOT / "synthetic.py", absolute)
    assert _filter_calls(SRC_ROOT / "synthetic.py", absolute) == {("synthetic.py", "seventh")}


@pytest.mark.parametrize(
    "source",
    [
        (
            "from notebooklm._auth import profile_store as provider\n"
            "def seventh(state):\n"
            "    return provider.filter_storage_state_cookies_by_domain_policy(state)\n"
        ),
        (
            "from notebooklm._auth import storage as provider\n"
            "def seventh(state):\n"
            "    return provider.filter_storage_state_cookies_by_domain_policy(state)\n"
        ),
        (
            "import notebooklm._browser.browser_capture as provider\n"
            "def seventh(state):\n"
            "    return provider.filter_storage_state_cookies_by_domain_policy(state)\n"
        ),
        (
            "import notebooklm.auth as provider\n"
            "def seventh(state):\n"
            "    return provider.filter_storage_state_cookies_by_domain_policy(state)\n"
        ),
    ],
)
def test_every_preserved_provider_module_alias_is_detected(source: str) -> None:
    assert _filter_calls(SRC_ROOT / "synthetic.py", ast.parse(source)) == {
        ("synthetic.py", "seventh")
    }


@pytest.mark.parametrize(
    "body",
    [
        "    assigned = filt\n    return assigned(state)\n",
        "    return consume(callback=filt)\n",
        "    registry = {'filter': filt}\n    return registry\n",
    ],
    ids=["assigned-callable", "keyword-argument", "bare-registry-reference"],
)
def test_non_call_filter_references_are_rejected(body: str) -> None:
    tree = ast.parse(
        "from notebooklm._auth.cookie_filter import "
        "filter_storage_state_cookies_by_domain_policy as filt\n"
        "def seventh(state, consume=None):\n"
        f"{body}"
    )
    assert _filter_escapes(SRC_ROOT / "synthetic.py", tree) == {("synthetic.py", "seventh")}
