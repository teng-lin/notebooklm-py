"""AST reach-in / runtime-import boundary gates for feature APIs and services.

This is the *gate* half of the historical ``tests/unit/test_init_order.py``
split (stages 2+3 of the test-guardrail consolidation). Every check here is a
static source-boundary contract: the ``self._core._private`` reach-in guard,
the ``self._api`` facade-reach-in guard, and the runtime-import boundary guard
for the artifact / source / notebook-composition service modules — plus the
self-tests for each AST visitor and the module-deletion asserts.

The construction / init-order behaviour tests that *exercise* the wired client
stay in ``tests/unit/test_init_order.py``; the two behaviour tests that still
need these AST visitors import them from here.
"""

from __future__ import annotations

import ast
import importlib
from collections import Counter
from pathlib import Path

import pytest

pytestmark = pytest.mark.repo_lint

SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "notebooklm"


_ALLOWED_CORE_PRIVATE_ACCESS_COUNTS: dict[tuple[str, str], int] = {}

_CORE_PRIVATE_GUARD_EXCLUDED_MODULES = {
    "__init__.py",
    "__main__.py",
    "_atomic_io.py",
    "_callbacks.py",
    "_core.py",
    "_env.py",
    "_idempotency.py",
    "_logging.py",
    "_mind_map.py",
    "_session.py",
    "_url_utils.py",
    "_version_check.py",
}

_ARTIFACT_SERVICE_MODULES = [
    "_artifact/formatters.py",
    "_artifact/listing.py",
    "_artifact/downloads.py",
    "_artifact/polling.py",
]

_SOURCE_SERVICE_MODULES = [
    "_source/listing.py",
    "_source/polling.py",
    "_source/add.py",
    "_source/upload.py",
    "_source/content.py",
]

_NOTEBOOK_COMPOSITION_SERVICE_MODULES = [
    "_notebook_metadata.py",
    "_sharing_manager.py",
    "_mind_map.py",
]

_FORBIDDEN_PRIVATE_SERVICE_RUNTIME_IMPORT_NAMES = {
    "ArtifactsAPI",
    "ChatAPI",
    "NotebookLMClient",
    "NotebooksAPI",
    "NotesAPI",
    "ResearchAPI",
    "SettingsAPI",
    "SharingAPI",
    "SourcesAPI",
}

_FORBIDDEN_PRIVATE_SERVICE_RUNTIME_IMPORT_MODULES = {
    "_artifacts",
    "_chat",
    "_core",
    "_notebooks",
    "_notes",
    "_research",
    "_session",
    "_settings",
    "_sharing",
    "_sources",
    "client",
    "notebooklm",
    "notebooklm._artifacts",
    "notebooklm._chat",
    "notebooklm._core",
    "notebooklm._notebooks",
    "notebooklm._notes",
    "notebooklm._research",
    "notebooklm" + "." + "_session",
    "notebooklm._settings",
    "notebooklm._sharing",
    "notebooklm._sources",
    "notebooklm.client",
}

_FORBIDDEN_ARTIFACT_SERVICE_RUNTIME_IMPORT_NAMES = _FORBIDDEN_PRIVATE_SERVICE_RUNTIME_IMPORT_NAMES

_FORBIDDEN_ARTIFACT_SERVICE_RUNTIME_IMPORT_MODULES = (
    _FORBIDDEN_PRIVATE_SERVICE_RUNTIME_IMPORT_MODULES
)

_FORBIDDEN_SOURCE_SERVICE_RUNTIME_IMPORT_NAMES = _FORBIDDEN_PRIVATE_SERVICE_RUNTIME_IMPORT_NAMES

_FORBIDDEN_SOURCE_SERVICE_RUNTIME_IMPORT_MODULES = _FORBIDDEN_PRIVATE_SERVICE_RUNTIME_IMPORT_MODULES

_FORBIDDEN_NOTEBOOK_COMPOSITION_SERVICE_RUNTIME_IMPORT_NAMES = (
    _FORBIDDEN_PRIVATE_SERVICE_RUNTIME_IMPORT_NAMES
)

_FORBIDDEN_NOTEBOOK_COMPOSITION_SERVICE_RUNTIME_IMPORT_MODULES = (
    _FORBIDDEN_PRIVATE_SERVICE_RUNTIME_IMPORT_MODULES
)


def _is_self_core(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "_core"
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
    )


def _is_private_attr(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr.startswith("_")
        and not node.attr.startswith("__")
    )


class _CorePrivateAccessVisitor(ast.NodeVisitor):
    """Collect ``self._core._x`` and simple aliases like ``core = self._core``."""

    def __init__(self, module_name: str) -> None:
        self.module_name = module_name
        self.observed: list[tuple[str, str]] = []
        self._core_alias_stack: list[set[str]] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function_scope(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function_scope(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self._visit_function_scope(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        if self._is_core_access_base(node.value):
            for target in node.targets:
                self._record_alias_target(target)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None and self._is_core_access_base(node.value):
            self._record_alias_target(node.target)
        self.generic_visit(node)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        if self._is_core_access_base(node.value):
            self._record_alias_target(node.target)
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if _is_private_attr(node) and self._is_core_access_base(node.value):
            self.observed.append((self.module_name, node.attr))
        self.generic_visit(node)

    def _visit_function_scope(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda,
    ) -> None:
        self._core_alias_stack.append(set())
        self.generic_visit(node)
        self._core_alias_stack.pop()

    def _record_alias_target(self, target: ast.AST) -> None:
        if isinstance(target, ast.Name) and self._core_alias_stack:
            self._core_alias_stack[-1].add(target.id)

    def _is_core_access_base(self, node: ast.AST) -> bool:
        return (
            _is_self_core(node)
            or (
                isinstance(node, ast.Name)
                and any(node.id in aliases for aliases in reversed(self._core_alias_stack))
            )
            or (isinstance(node, ast.NamedExpr) and self._is_core_access_base(node.value))
        )


def _feature_modules_for_core_private_guard() -> list[Path]:
    return [
        path
        for path in sorted(SRC_ROOT.glob("_*.py"))
        if path.name not in _CORE_PRIVATE_GUARD_EXCLUDED_MODULES
    ]


def _collect_core_private_accesses(path: Path) -> list[tuple[str, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    visitor = _CorePrivateAccessVisitor(path.name)
    visitor.visit(tree)
    return visitor.observed


def _self_attr_name(node: ast.AST) -> str | None:
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
    ):
        return node.attr
    return None


def _assigned_self_attr_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Assign):
        for target in node.targets:
            attr_name = _self_attr_name(target)
            if attr_name is not None:
                return attr_name
    if isinstance(node, ast.AnnAssign):
        return _self_attr_name(node.target)
    return None


def _assignment_value(node: ast.AST) -> ast.AST | None:
    if isinstance(node, ast.Assign):
        return node.value
    if isinstance(node, ast.AnnAssign):
        return node.value
    return None


def _self_attr_assignment(body: list[ast.stmt], attr_name: str) -> tuple[int, ast.stmt]:
    for index, statement in enumerate(body):
        if _assigned_self_attr_name(statement) == attr_name:
            return index, statement
    raise AssertionError(f"self.{attr_name} assignment not found")


def _method_body(tree: ast.AST, class_name: str, method_name: str) -> list[ast.stmt]:
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != class_name:
            continue
        for item in node.body:
            if (
                isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                and item.name == method_name
            ):
                return item.body
    raise AssertionError(f"{class_name}.{method_name} not found")


def _facade_call_name(node: ast.AST, facade_names: set[str]) -> str | None:
    if isinstance(node, ast.Name) and node.id in facade_names:
        return node.id
    if isinstance(node, ast.Attribute):
        if node.attr in facade_names:
            return node.attr
        return _facade_call_name(node.value, facade_names)
    return None


def _facade_construction_lines(tree: ast.AST, facade_names: set[str]) -> dict[str, list[int]]:
    lines: dict[str, list[int]] = {facade_name: [] for facade_name in facade_names}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        facade_name = _facade_call_name(node.func, facade_names)
        if facade_name is not None:
            lines[facade_name].append(node.lineno)
    return {facade_name: found for facade_name, found in lines.items() if found}


def _call_keyword_value(call: ast.Call, keyword_name: str) -> ast.AST:
    for keyword in call.keywords:
        if keyword.arg == keyword_name:
            return keyword.value
    raise AssertionError(f"keyword argument {keyword_name!r} not found")


def test_feature_apis_do_not_add_direct_core_private_state_access() -> None:
    """Pending guard: no new feature API reaches directly into Session internals."""
    observed_counts: Counter[tuple[str, str]] = Counter()
    for path in _feature_modules_for_core_private_guard():
        observed_counts.update(_collect_core_private_accesses(path))

    unexpected = {
        access: count
        for access, count in observed_counts.items()
        if count > _ALLOWED_CORE_PRIVATE_ACCESS_COUNTS.get(access, 0)
    }
    assert not unexpected, (
        "Feature APIs must not add new direct `self._core._private` accesses. "
        "Add a public Session capability first, or temporarily extend the "
        f"TODO baseline with a migration note. New accesses: {unexpected}"
    )

    stale = {
        access: allowed_count - observed_counts.get(access, 0)
        for access, allowed_count in _ALLOWED_CORE_PRIVATE_ACCESS_COUNTS.items()
        if observed_counts.get(access, 0) < allowed_count
    }
    assert not stale, (
        "Core-private access baseline has entries no longer present in code. "
        f"Remove them from _ALLOWED_CORE_PRIVATE_ACCESS_COUNTS: {stale}"
    )


# ----------------------------------------------------------------------------
# Artifact-service "reach-in" guard
#
# Modeled on the core-private-access guard above. Pins the invariant that
# artifact-service helper modules (currently ``_artifact/downloads.py``)
# do not retain or call back into the ``ArtifactsAPI`` facade. Each helper
# migration PR appends the helper's module name to
# ``_REACH_IN_MIGRATED_MODULES`` below.
# ----------------------------------------------------------------------------


_REACH_IN_MIGRATED_MODULES: list[str] = [
    "_artifact/downloads.py",
]


def _is_self_api(node: ast.AST) -> bool:
    """True for ast nodes representing ``self._api``."""
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "_api"
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
    )


class _ApiReachInVisitor(ast.NodeVisitor):
    """Collect reach-ins to ``self._api`` (direct, aliased, nested-scope).

    Modeled on :class:`_CorePrivateAccessVisitor` defined earlier in this
    file: function/async/lambda scopes are tracked, alias bindings recorded
    per-scope, and ``_is_api_access_base`` walks the entire active stack
    via ``reversed(self._alias_stack)`` so aliases in outer scopes are
    visible to attribute access in nested closures and comprehensions.

    ``_REACH_IN_MIGRATED_MODULES`` enumerates helpers already migrated to
    constructor injection; this guard is actively enforced for those
    modules. The remaining artifact-service helper
    (``_artifact/downloads.py``) is currently migrated;
    ``_artifact_generation.py`` was folded into the ``ArtifactsAPI``
    facade (issue #1205) so it is no longer a separate guarded module.
    """

    def __init__(self, module_name: str) -> None:
        self.module_name = module_name
        self.violations: list[tuple[int, str]] = []
        self._alias_stack: list[set[str]] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function_scope(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function_scope(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self._visit_function_scope(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        if self._is_api_access_base(node.value):
            for target in node.targets:
                self._record_alias_target(target)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None and self._is_api_access_base(node.value):
            self._record_alias_target(node.target)
        self.generic_visit(node)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        if self._is_api_access_base(node.value):
            self._record_alias_target(node.target)
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if self._is_api_access_base(node.value):
            base_repr = self._render_base(node.value)
            self.violations.append((node.lineno, f"{base_repr}.{node.attr}"))
        self.generic_visit(node)

    def visit_Return(self, node: ast.Return) -> None:
        if node.value is not None and self._is_api_access_base(node.value):
            base_repr = self._render_base(node.value)
            self.violations.append((node.lineno, f"bare retention: return {base_repr}"))
        self.generic_visit(node)

    def _visit_function_scope(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda,
    ) -> None:
        self._alias_stack.append(set())
        self.generic_visit(node)
        self._alias_stack.pop()

    def _record_alias_target(self, target: ast.AST) -> None:
        if isinstance(target, ast.Name) and self._alias_stack:
            self._alias_stack[-1].add(target.id)

    def _is_api_access_base(self, node: ast.AST) -> bool:
        return (
            _is_self_api(node)
            or (
                isinstance(node, ast.Name)
                and any(node.id in aliases for aliases in reversed(self._alias_stack))
            )
            or (isinstance(node, ast.NamedExpr) and self._is_api_access_base(node.value))
        )

    def _render_base(self, node: ast.AST) -> str:
        if _is_self_api(node):
            return "self._api"
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.NamedExpr):
            return "(walrus-expr)"
        return "<api>"


def test_artifact_services_have_no_facade_reach_in() -> None:
    """Pin the no-reach-in invariant for migrated artifact-service modules."""
    violations: dict[str, list[tuple[int, str]]] = {}
    for module_name in _REACH_IN_MIGRATED_MODULES:
        path = SRC_ROOT / module_name
        tree = ast.parse(path.read_text(encoding="utf-8"))
        visitor = _ApiReachInVisitor(module_name)
        visitor.visit(tree)
        if visitor.violations:
            violations[module_name] = visitor.violations
    assert not violations, (
        f"Encapsulation violations found: {violations}. "
        "Helpers must depend on explicit collaborators, not on ArtifactsAPI."
    )


def test_api_reach_in_visitor_catches_direct_access() -> None:
    tree = ast.parse(
        "class C:\n"
        "    def __init__(self, api): self._api = api\n"
        "    async def f(self): return self._api.foo\n"
    )
    visitor = _ApiReachInVisitor("test.py")
    visitor.visit(tree)
    assert any(v[1] == "self._api.foo" for v in visitor.violations)


def test_api_reach_in_visitor_catches_alias() -> None:
    tree = ast.parse(
        "class C:\n"
        "    def __init__(self, api): self._api = api\n"
        "    async def f(self):\n"
        "        api = self._api\n"
        "        return api.foo\n"
    )
    visitor = _ApiReachInVisitor("test.py")
    visitor.visit(tree)
    assert any(v[1] == "api.foo" for v in visitor.violations)


def test_api_reach_in_visitor_catches_comprehension_alias() -> None:
    """Comprehensions traverse within their enclosing function scope and
    must see aliases bound in that function. (List/set/dict comprehensions
    do not push a new scope onto ``_alias_stack`` because the visitor only
    overrides ``visit_FunctionDef`` / ``visit_AsyncFunctionDef`` /
    ``visit_Lambda``.)
    """
    tree = ast.parse(
        "class C:\n"
        "    def __init__(self, api): self._api = api\n"
        "    async def f(self):\n"
        "        api = self._api\n"
        "        return [api.foo for x in items]\n"
    )
    visitor = _ApiReachInVisitor("test.py")
    visitor.visit(tree)
    assert any(v[1] == "api.foo" for v in visitor.violations)


def test_api_reach_in_visitor_catches_nested_scope_alias() -> None:
    """Aliases bound in an outer function must be visible to attribute
    access in a nested function — exercises the ``reversed(_alias_stack)``
    multi-entry walk (the inner ``def g`` pushes a second entry onto the
    stack, and ``api`` is only bound in the outer scope).
    """
    tree = ast.parse(
        "class C:\n"
        "    def __init__(self, api): self._api = api\n"
        "    async def f(self):\n"
        "        api = self._api\n"
        "        async def g():\n"
        "            return api.foo\n"
        "        return await g()\n"
    )
    visitor = _ApiReachInVisitor("test.py")
    visitor.visit(tree)
    assert any(v[1] == "api.foo" for v in visitor.violations), (
        "Visitor must search outer scopes via reversed(_alias_stack)"
    )


def test_api_reach_in_visitor_catches_bare_retention() -> None:
    tree = ast.parse(
        "class C:\n"
        "    def __init__(self, api): self._api = api\n"
        "    def f(self): return self._api\n"
    )
    visitor = _ApiReachInVisitor("test.py")
    visitor.visit(tree)
    assert any("bare retention" in v[1] for v in visitor.violations)


def test_api_reach_in_visitor_catches_annassign_alias() -> None:
    tree = ast.parse(
        "class C:\n"
        "    def __init__(self, api): self._api = api\n"
        "    def f(self) -> None:\n"
        "        api: Any = self._api\n"
        "        return api.foo\n"
    )
    visitor = _ApiReachInVisitor("test.py")
    visitor.visit(tree)
    assert any(v[1] == "api.foo" for v in visitor.violations)


def test_legacy_capabilities_module_is_deleted() -> None:
    """Feature APIs now type against ``Session`` plus explicit collaborators."""
    assert not (SRC_ROOT / "_capabilities.py").exists()


def test_lifted_core_modules_are_retired() -> None:
    """Session collaborators should not regress to the old ``_core_*`` layout."""
    assert sorted(path.name for path in SRC_ROOT.glob("_core_*.py")) == []


def test_deleted_session_module_is_not_importable() -> None:
    """The deleted concrete session module MUST stay absent."""
    import importlib.util

    deleted_module = "notebooklm" + "." + "_session"
    assert importlib.util.find_spec(deleted_module) is None


def _is_type_checking_guard(node: ast.AST) -> bool:
    return (isinstance(node, ast.Name) and node.id == "TYPE_CHECKING") or (
        isinstance(node, ast.Attribute)
        and node.attr == "TYPE_CHECKING"
        and isinstance(node.value, ast.Name)
        and node.value.id == "typing"
    )


class _RuntimeImportVisitor(ast.NodeVisitor):
    def __init__(
        self,
        *,
        forbidden_names: set[str],
        forbidden_modules: set[str],
    ) -> None:
        self._forbidden_names = forbidden_names
        self._forbidden_modules = forbidden_modules
        self.forbidden: list[str] = []

    def visit_If(self, node: ast.If) -> None:
        if _is_type_checking_guard(node.test):
            for child in node.orelse:
                self.visit(child)
            return
        self.generic_visit(node)

    @staticmethod
    def _is_dunder_name(name: str) -> bool:
        return name.startswith("__") and name.endswith("__")

    @classmethod
    def _is_forbidden_module_reference(cls, name: str, forbidden_modules: set[str]) -> bool:
        if not name:
            return False

        if any(cls._is_dunder_name(part) for part in name.split(".")):
            return False

        for forbidden_module in forbidden_modules:
            if cls._is_dunder_name(forbidden_module):
                continue
            if name == forbidden_module or name.startswith(f"{forbidden_module}."):
                return True

        return False

    def visit_Import(self, node: ast.Import) -> None:
        self.forbidden.extend(
            alias.name
            for alias in node.names
            if self._is_forbidden_module_reference(alias.name, self._forbidden_modules)
        )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        if self._is_forbidden_module_reference(module, self._forbidden_modules):
            self.forbidden.extend(f"{module}.{alias.name}" for alias in node.names)
            return

        self.forbidden.extend(
            alias.name
            for alias in node.names
            if alias.name in self._forbidden_names
            or self._is_forbidden_module_reference(alias.name, self._forbidden_modules)
        )


def test_runtime_import_visitor_detects_nested_forbidden_modules() -> None:
    """The import-boundary guard must catch nested forbidden module paths."""
    tree = ast.parse(
        """
import notebooklm._sources.utils
import http.client
from notebooklm._sources.utils import SourceParser
from notebooklm import _sources
from . import _sources as relative_sources
from __future__ import annotations
"""
    )
    visitor = _RuntimeImportVisitor(
        forbidden_names=set(),
        forbidden_modules={"_sources", "notebooklm._sources", "__future__"},
    )

    visitor.visit(tree)

    assert visitor.forbidden == [
        "notebooklm._sources.utils",
        "notebooklm._sources.utils.SourceParser",
        "_sources",
        "_sources",
    ]


def test_runtime_import_visitor_detects_top_level_public_package_import() -> None:
    """Private services must not import the public package facade."""
    tree = ast.parse(
        """
import notebooklm
from notebooklm import NotebookLMClient
"""
    )
    visitor = _RuntimeImportVisitor(
        forbidden_names={"NotebookLMClient"},
        forbidden_modules={"notebooklm"},
    )

    visitor.visit(tree)

    assert visitor.forbidden == ["notebooklm", "notebooklm.NotebookLMClient"]


def test_facade_construction_lines_detects_chained_facade_access() -> None:
    """Facade construction guard must catch classmethod-style facade access."""
    tree = ast.parse("notebooklm.NotebookLMClient.from_storage()\n")

    assert _facade_construction_lines(tree, {"NotebookLMClient"}) == {"NotebookLMClient": [1]}


def test_artifact_service_modules_do_not_runtime_import_facades_or_core() -> None:
    """Guard future artifact service extraction modules against facade/core imports."""
    forbidden_by_module: dict[str, list[str]] = {}
    forbidden_construction_by_module: dict[str, dict[str, list[int]]] = {}
    for module_name in _ARTIFACT_SERVICE_MODULES:
        tree = ast.parse((SRC_ROOT / module_name).read_text(encoding="utf-8"))
        visitor = _RuntimeImportVisitor(
            forbidden_names=_FORBIDDEN_ARTIFACT_SERVICE_RUNTIME_IMPORT_NAMES,
            forbidden_modules=_FORBIDDEN_ARTIFACT_SERVICE_RUNTIME_IMPORT_MODULES,
        )
        visitor.visit(tree)
        if visitor.forbidden:
            forbidden_by_module[module_name] = visitor.forbidden

        construction_lines = _facade_construction_lines(
            tree,
            _FORBIDDEN_ARTIFACT_SERVICE_RUNTIME_IMPORT_NAMES,
        )
        if construction_lines:
            forbidden_construction_by_module[module_name] = construction_lines

    assert forbidden_by_module == {}
    assert forbidden_construction_by_module == {}


def test_source_service_modules_import_cleanly() -> None:
    """Source service skeletons must be import-safe before behavior moves."""
    for module_name in _SOURCE_SERVICE_MODULES:
        dotted = module_name.removesuffix(".py").replace("/", ".")
        importlib.import_module(f"notebooklm.{dotted}")


def test_source_service_modules_do_not_runtime_import_facades_or_core() -> None:
    """Guard future source service extraction modules against facade/core imports."""
    forbidden_by_module: dict[str, list[str]] = {}
    forbidden_construction_by_module: dict[str, dict[str, list[int]]] = {}
    for module_name in _SOURCE_SERVICE_MODULES:
        tree = ast.parse((SRC_ROOT / module_name).read_text(encoding="utf-8"))
        visitor = _RuntimeImportVisitor(
            forbidden_names=_FORBIDDEN_SOURCE_SERVICE_RUNTIME_IMPORT_NAMES,
            forbidden_modules=_FORBIDDEN_SOURCE_SERVICE_RUNTIME_IMPORT_MODULES,
        )
        visitor.visit(tree)
        if visitor.forbidden:
            forbidden_by_module[module_name] = visitor.forbidden

        construction_lines = _facade_construction_lines(
            tree,
            _FORBIDDEN_SOURCE_SERVICE_RUNTIME_IMPORT_NAMES,
        )
        if construction_lines:
            forbidden_construction_by_module[module_name] = construction_lines

    assert forbidden_by_module == {}
    assert forbidden_construction_by_module == {}


def test_notebook_composition_services_do_not_runtime_import_facades_or_core() -> None:
    """Notebook composition services stay below facade APIs and Session."""
    forbidden_by_module: dict[str, list[str]] = {}
    forbidden_construction_by_module: dict[str, dict[str, list[int]]] = {}

    for module_name in _NOTEBOOK_COMPOSITION_SERVICE_MODULES:
        tree = ast.parse((SRC_ROOT / module_name).read_text(encoding="utf-8"))
        visitor = _RuntimeImportVisitor(
            forbidden_names=_FORBIDDEN_NOTEBOOK_COMPOSITION_SERVICE_RUNTIME_IMPORT_NAMES,
            forbidden_modules=_FORBIDDEN_NOTEBOOK_COMPOSITION_SERVICE_RUNTIME_IMPORT_MODULES,
        )
        visitor.visit(tree)
        if visitor.forbidden:
            forbidden_by_module[module_name] = visitor.forbidden

        construction_lines = _facade_construction_lines(
            tree,
            _FORBIDDEN_NOTEBOOK_COMPOSITION_SERVICE_RUNTIME_IMPORT_NAMES,
        )
        if construction_lines:
            forbidden_construction_by_module[module_name] = construction_lines

    assert forbidden_by_module == {}
    assert forbidden_construction_by_module == {}


@pytest.mark.parametrize("module_name", _NOTEBOOK_COMPOSITION_SERVICE_MODULES)
def test_notebook_composition_services_import_cleanly(module_name: str) -> None:
    """Notebook composition services must be import-safe."""
    importlib.import_module(f"notebooklm.{module_name.removesuffix('.py')}")


def test_core_private_access_guard_detects_simple_aliases() -> None:
    tree = ast.parse(
        """
class Example:
    def method(self):
        core = self._core
        return core._pending_polls
"""
    )
    visitor = _CorePrivateAccessVisitor("example.py")
    visitor.visit(tree)
    assert visitor.observed == [("example.py", "_pending_polls")]


def test_core_private_access_guard_detects_chained_aliases() -> None:
    tree = ast.parse(
        """
class Example:
    def method(self):
        core = self._core
        same = core
        return same._pending_polls
"""
    )
    visitor = _CorePrivateAccessVisitor("example.py")
    visitor.visit(tree)
    assert visitor.observed == [("example.py", "_pending_polls")]


def test_core_private_access_guard_detects_closure_aliases() -> None:
    tree = ast.parse(
        """
class Example:
    def method(self):
        core = self._core
        def nested():
            return core._pending_polls
        return nested()
"""
    )
    visitor = _CorePrivateAccessVisitor("example.py")
    visitor.visit(tree)
    assert visitor.observed == [("example.py", "_pending_polls")]


def test_core_private_access_guard_detects_direct_access() -> None:
    tree = ast.parse(
        """
class Example:
    def method(self):
        return self._core._pending_polls
"""
    )
    visitor = _CorePrivateAccessVisitor("example.py")
    visitor.visit(tree)
    assert visitor.observed == [("example.py", "_pending_polls")]


def test_core_private_access_guard_counts_duplicate_call_sites() -> None:
    tree = ast.parse(
        """
class Example:
    def method(self):
        first = self._core._pending_polls
        second = self._core._pending_polls
        return first, second
"""
    )
    visitor = _CorePrivateAccessVisitor("example.py")
    visitor.visit(tree)
    assert visitor.observed == [
        ("example.py", "_pending_polls"),
        ("example.py", "_pending_polls"),
    ]


def test_core_private_access_guard_detects_walrus_aliases() -> None:
    tree = ast.parse(
        """
class Example:
    def method(self):
        return (core := self._core)._pending_polls
"""
    )
    visitor = _CorePrivateAccessVisitor("example.py")
    visitor.visit(tree)
    assert visitor.observed == [("example.py", "_pending_polls")]


def test_core_private_access_guard_ignores_public_core_methods() -> None:
    tree = ast.parse(
        """
class Example:
    def method(self):
        return self._core.update_auth_tokens(csrf, sid)
"""
    )
    visitor = _CorePrivateAccessVisitor("example.py")
    visitor.visit(tree)
    assert visitor.observed == []
