"""Init-order regression tests for ``ArtifactsAPI`` / ``NotesAPI``.

Before the fix, :class:`ArtifactsAPI` required ``notes_api=client.notes`` at
construction time, so :class:`NotesAPI` had to be built first. The shared
:mod:`_mind_map` module decouples the two APIs — these tests pin that
invariant down so the load-bearing init order can't silently come back.
"""

from __future__ import annotations

import ast
import importlib
import json
from collections import Counter
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from _helpers.session_factory import build_session_for_tests
from notebooklm._artifacts import ArtifactsAPI
from notebooklm._notes import NotesAPI
from notebooklm.auth import AuthTokens
from notebooklm.client import NotebookLMClient

pytestmark = pytest.mark.repo_lint

SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "notebooklm"

# Feature APIs should not reach into Session private state directly.
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
    "_artifact_formatters.py",
    "_artifact_listing.py",
    "_artifact_downloads.py",
    "_artifact_generation.py",
    "_artifact_polling.py",
]

_SOURCE_SERVICE_MODULES = [
    "_source_listing.py",
    "_source_polling.py",
    "_source_add.py",
    "_source_upload.py",
    "_source_content.py",
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
    "notebooklm._session",
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
# artifact-service helper modules (``_artifact_downloads.py`` and
# ``_artifact_generation.py``) do not retain or call back into the
# ``ArtifactsAPI`` facade. Each helper migration PR appends the helper's
# module name to ``_REACH_IN_MIGRATED_MODULES`` below.
# ----------------------------------------------------------------------------


# Modules already migrated to constructor-injected collaborators — the guard
# below enforces no residual ``self._api`` reach-in.
# Bookkeeping (mirrors the ``_ALLOWED_CORE_PRIVATE_ACCESS_COUNTS`` pattern):
#   * ``_artifact_downloads.py`` migrated (PR #896, T2 of the
#     encapsulation-reach-in-remediation phase).
#   * ``_artifact_generation.py`` migrated (T3 of the same phase, this
#     PR; the bidirectional facade↔service reach via
#     ``ArtifactsAPI._call_generate`` / ``ArtifactsAPI._parse_generation_result``
#     was closed simultaneously by promoting the service-side methods to
#     ``call_generate`` / ``parse_generation_result``).
# Both artifact-service helpers are now constructor-injected. No further
# migration PRs in this series are pending.
_REACH_IN_MIGRATED_MODULES: list[str] = [
    "_artifact_downloads.py",
    "_artifact_generation.py",
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
    modules. Both artifact-service helpers
    (``_artifact_downloads.py`` and ``_artifact_generation.py``) are
    currently migrated.
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


# ---------------------------------------------------------------------------
# Constructor-DI seams (``docs/improvement.md`` §4.1 + §4.2)
#
# These pin tests guard the post-refactor wiring shape so a future
# refactor cannot silently re-introduce the retired module-level
# late-binding wrappers (``_decode_response_late_bound``,
# ``_sleep_late_bound``, ``_live_is_auth_error``) or the retired
# ``Kernel.http_client`` setter.
# ---------------------------------------------------------------------------


def test_compose_session_internals_exposes_constructor_di_seams() -> None:
    """``compose_session_internals`` MUST expose the four constructor-DI seams.

    Stage B1 PR 2 of the post-refactoring plan moved the composition
    root out of ``Session.__init__`` (which now takes
    ``(*, collaborators, config, auth)`` only) into
    ``notebooklm._session.compose_session_internals``. The seams live
    on the helper (and on the canonical test builder
    ``build_session_for_tests``), NOT on ``NotebookLMClient.__init__``
    (which preserves the production surface).

    The seams replace the retired module-level late-binding wrappers
    (see ``docs/improvement.md`` §4.1) and the retired
    ``Kernel.http_client`` setter (§4.2). Each must be keyword-only and
    default to ``None`` so the helper can resolve the canonical seam via
    a fresh module-attribute lookup at construction time (preserving
    pre-construction monkeypatch propagation).
    """
    import inspect

    from notebooklm._session import compose_session_internals

    sig = inspect.signature(compose_session_internals)
    for name in ("decode_response", "sleep", "is_auth_error", "async_client_factory"):
        assert name in sig.parameters, (
            f"compose_session_internals must expose constructor-DI kwarg {name!r}"
        )
        param = sig.parameters[name]
        assert param.kind == inspect.Parameter.KEYWORD_ONLY, (
            f"{name!r} must be keyword-only; got {param.kind!r}"
        )
        assert param.default is None, (
            f"{name!r} must default to None (None-sentinel + fresh module "
            f"lookup); got default {param.default!r}"
        )


def test_session_retired_late_bound_wrappers_are_gone() -> None:
    """The three module-level late-binding wrappers MUST stay deleted.

    See ``docs/improvement.md`` §4.1. The constructor-DI seams above
    (``decode_response`` / ``sleep`` / ``is_auth_error``) replaced them;
    re-adding the wrappers would re-introduce the test-shaped production
    code the refactor removed.
    """
    import notebooklm._session as session_mod

    for symbol in (
        "_decode_response_late_bound",
        "_sleep_late_bound",
        "_live_is_auth_error",
    ):
        assert not hasattr(session_mod, symbol), (
            f"notebooklm._session.{symbol} must stay deleted (see docs/improvement.md §4.1)"
        )


def test_session_wires_seam_attributes_for_executor_and_chain() -> None:
    """Constructor-injected seams MUST reach the executor and chain builder.

    The ``RpcExecutor`` resolves ``decode_response`` / ``is_auth_error`` /
    ``sleep`` through closures over ``self._decode_response`` etc., so that
    legacy tests which rebind ``session._decode_response = stub`` after
    ``NotebookLMClient.__init__`` (which eagerly builds the executor via
    the ``rpc_executor`` accessor wired into sub-APIs in Wave 7) still take
    effect. This test pins both halves: constructor-injected callables
    reach the executor, AND post-construction rebinds also take effect.
    """
    from notebooklm.auth import AuthTokens

    auth = AuthTokens(
        cookies={"SID": "x"},
        csrf_token="csrf",
        session_id="sid",
    )

    def custom_decode(*_a, **_kw):
        return ["custom"]

    async def custom_sleep(_seconds):
        return None

    def custom_is_auth_error(_exc):
        return True

    core = build_session_for_tests(
        auth,
        decode_response=custom_decode,
        sleep=custom_sleep,
        is_auth_error=custom_is_auth_error,
    )

    assert core._decode_response is custom_decode
    assert core._sleep is custom_sleep
    assert core._is_auth_error is custom_is_auth_error

    executor = core._rpc_executor
    # Constructor-injected callables propagate through the closure.
    assert executor._decode_response() == ["custom"]
    assert executor._is_auth_error(object()) is True

    # Post-construction rebind takes effect (late-binding contract).
    def rebound_decode(*_a, **_kw):
        return ["rebound"]

    core._decode_response = rebound_decode
    assert executor._decode_response() == ["rebound"]


def test_kernel_http_client_is_read_only_property() -> None:
    """``Kernel.http_client`` MUST have no setter (``docs/improvement.md`` §4.2)."""
    from notebooklm._kernel import Kernel

    descriptor = Kernel.__dict__["http_client"]
    assert isinstance(descriptor, property)
    assert descriptor.fset is None, (
        "Kernel.http_client must remain read-only; the retired setter was a "
        "test-injection seam that constructor-time async_client_factory "
        "injection now replaces (see docs/improvement.md §4.2)."
    )


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
        importlib.import_module(f"notebooklm.{module_name.removesuffix('.py')}")


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


def test_phase8_source_listing_service_name_and_facade_wiring_are_current() -> None:
    """Downstream notebook-metadata work depends on the finalized lister name."""
    from notebooklm._source_listing import SourceLister
    from notebooklm._sources import SourcesAPI

    core = MagicMock()
    api = SourcesAPI(core, uploader=MagicMock())

    assert isinstance(api._lister, SourceLister)


def test_phase7_artifact_download_patch_seams_are_current() -> None:
    """Artifact downloads must use canonical helpers and collaborators.

    Phase 5 (refactor-history.md Migration Plan steps 6-7) moves the mind-map
    create/list/extract paths off the ``_mind_map`` module-level seams
    and onto the injected ``NoteService`` + ``NoteBackedMindMapService``
    instances. Downloads should now import their canonical helpers directly
    rather than resolving through ``notebooklm._artifacts`` at runtime or in
    type-checking-only imports.
    """
    import notebooklm._artifact_downloads as artifact_downloads
    import notebooklm._artifact_formatters as artifact_formatters
    import notebooklm._artifacts as artifacts
    import notebooklm._mind_map as mind_map
    import notebooklm.auth as auth

    tree = ast.parse((SRC_ROOT / "_artifact_downloads.py").read_text(encoding="utf-8"))
    artifact_facade_imports: list[str] = []
    artifact_facade_modules = {"_artifacts", "notebooklm._artifacts"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            artifact_facade_imports.extend(
                alias.name for alias in node.names if alias.name in artifact_facade_modules
            )
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module in artifact_facade_modules:
                artifact_facade_imports.extend(f"{module}.{alias.name}" for alias in node.names)
            elif module in {"", "notebooklm"}:
                artifact_facade_imports.extend(
                    alias.name for alias in node.names if alias.name == "_artifacts"
                )

    assert artifact_facade_imports == []
    assert artifacts._mind_map is mind_map
    assert not hasattr(artifact_downloads, "_artifact_seams")
    assert artifact_downloads.load_httpx_cookies is auth.load_httpx_cookies
    assert artifact_downloads._extract_app_data is artifact_formatters._extract_app_data
    assert (
        artifact_downloads._format_interactive_content
        is artifact_formatters._format_interactive_content
    )
    assert artifact_downloads._parse_data_table is artifact_formatters._parse_data_table


def test_notebooks_api_has_no_hidden_sources_api_runtime_dependency() -> None:
    """Notebook metadata must use a narrow lister, not hidden SourcesAPI construction."""
    notebooks_tree = ast.parse((SRC_ROOT / "_notebooks.py").read_text(encoding="utf-8"))
    visitor = _RuntimeImportVisitor(
        forbidden_names={"SourcesAPI"},
        forbidden_modules={"_sources", "notebooklm._sources"},
    )
    visitor.visit(notebooks_tree)

    assert visitor.forbidden == []
    assert _facade_construction_lines(notebooks_tree, {"SourcesAPI"}) == {}

    metadata_tree = ast.parse((SRC_ROOT / "_notebook_metadata.py").read_text(encoding="utf-8"))
    metadata_visitor = _RuntimeImportVisitor(
        forbidden_names={"SourcesAPI"},
        forbidden_modules={"_sources", "notebooklm._sources"},
    )
    metadata_visitor.visit(metadata_tree)

    assert metadata_visitor.forbidden == []
    assert _facade_construction_lines(metadata_tree, {"SourcesAPI"}) == {}


def test_client_constructs_sources_before_notebooks_and_injects_sources_api() -> None:
    """Client wiring must avoid hidden SourcesAPI construction inside NotebooksAPI."""
    client_tree = ast.parse((SRC_ROOT / "client.py").read_text(encoding="utf-8"))
    init_body = _method_body(client_tree, "NotebookLMClient", "__init__")
    sources_index, sources_assignment = _self_attr_assignment(init_body, "sources")
    notebooks_index, notebook_assignment = _self_attr_assignment(init_body, "notebooks")

    assert sources_index < notebooks_index

    sources_value = _assignment_value(sources_assignment)
    assert isinstance(sources_value, ast.Call)
    assert isinstance(sources_value.func, ast.Name)
    assert sources_value.func.id == "SourcesAPI"

    notebooks_value = _assignment_value(notebook_assignment)
    assert isinstance(notebooks_value, ast.Call)
    notebooks_call = notebooks_value
    assert isinstance(notebooks_call.func, ast.Name)
    assert notebooks_call.func.id == "NotebooksAPI"

    assert _self_attr_name(_call_keyword_value(notebooks_call, "sources_api")) == "sources"


def test_client_constructs_chat_before_notes_and_injects_save_chat_answer() -> None:
    """Phase 6 (refactor-history.md Step 8, ADR-013): the composition root MUST
    construct ``self.chat`` before ``self.notes`` so the
    ``save_chat_answer=self.chat.save_answer_as_note`` callback exists
    at ``NotesAPI`` construction time.

    The check is structural (AST) so a future refactor that re-orders
    the assignments — or drops the ``save_chat_answer`` kwarg — fails
    loudly here rather than at runtime under a missing-attribute
    error.
    """
    client_tree = ast.parse((SRC_ROOT / "client.py").read_text(encoding="utf-8"))
    init_body = _method_body(client_tree, "NotebookLMClient", "__init__")
    chat_index, _chat_assignment = _self_attr_assignment(init_body, "chat")
    notes_index, notes_assignment = _self_attr_assignment(init_body, "notes")

    assert chat_index < notes_index, (
        "client.chat must be constructed before client.notes — Phase 6 "
        "passes self.chat.save_answer_as_note as a required kwarg to NotesAPI."
    )

    notes_value = _assignment_value(notes_assignment)
    assert isinstance(notes_value, ast.Call)
    save_chat_answer_arg = _call_keyword_value(notes_value, "save_chat_answer")
    assert save_chat_answer_arg is not None, (
        "NotesAPI must receive a save_chat_answer= callback (Phase 6)."
    )
    # The expected wiring is `save_chat_answer=self.chat.save_answer_as_note`.
    assert isinstance(save_chat_answer_arg, ast.Attribute)
    assert save_chat_answer_arg.attr == "save_answer_as_note"
    inner = save_chat_answer_arg.value
    assert isinstance(inner, ast.Attribute)
    assert inner.attr == "chat"


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


@pytest.fixture
def mock_auth() -> AuthTokens:
    return AuthTokens(
        cookies={"SID": "test"},
        csrf_token="csrf",
        session_id="session",
    )


def test_client_exposes_artifacts_and_notes(mock_auth: AuthTokens) -> None:
    """The client should construct both APIs regardless of order."""
    client = NotebookLMClient(mock_auth)
    assert isinstance(client.artifacts, ArtifactsAPI)
    assert isinstance(client.notes, NotesAPI)


def test_artifacts_constructible_without_notes_api(mock_auth: AuthTokens) -> None:
    """``ArtifactsAPI`` no longer takes ``notes_api`` at all (per
    docs/refactor-history.md Step 4) — the parameter was removed in favor of
    explicit ``mind_maps`` + ``note_service`` (Phase 5). The mind-map
    decoupling is now structural."""
    from notebooklm._mind_map import NoteBackedMindMapService
    from notebooklm._note_service import NoteService

    core = MagicMock()
    api = ArtifactsAPI(
        rpc=core,
        drain=core,
        lifecycle=core,
        notebooks=MagicMock(),
        mind_maps=MagicMock(spec=NoteBackedMindMapService),
        note_service=MagicMock(spec=NoteService),
    )
    assert api is not None
    # The legacy private attribute must not leak back: code that depends on
    # ``self._notes`` would re-introduce the coupling.
    assert not hasattr(api, "_notes")


def test_artifacts_rejects_legacy_notes_api_kwarg(mock_auth: AuthTokens) -> None:
    """The legacy ``notes_api=`` kwarg was removed in Phase 3
    (docs/refactor-history.md Step 4). Passing it must raise ``TypeError``."""
    from notebooklm._mind_map import NoteBackedMindMapService
    from notebooklm._note_service import NoteService

    core = MagicMock()
    notes = NotesAPI(
        notes=MagicMock(spec=NoteService),
        mind_maps=MagicMock(spec=NoteBackedMindMapService),
        save_chat_answer=AsyncMock(),
    )
    with pytest.raises(TypeError):
        ArtifactsAPI(  # type: ignore[call-arg]
            core,
            notes_api=notes,
            notebooks=MagicMock(),
            mind_maps=MagicMock(spec=NoteBackedMindMapService),
            note_service=MagicMock(spec=NoteService),
        )


def test_artifacts_before_notes_construction_order(mock_auth: AuthTokens) -> None:
    """Both construction orders must succeed and produce working APIs.

    Per Phase 6 (refactor-history.md Step 8, ADR-013), ``NotesAPI`` requires an
    injected ``save_chat_answer`` callback at construction; in the
    composition root that callback is
    ``ChatAPI.save_answer_as_note``. This test uses an ``AsyncMock``
    stand-in so the ordering constraint stays explicit without pulling
    in the full ``ChatAPI`` build path.
    """
    from notebooklm._mind_map import NoteBackedMindMapService
    from notebooklm._note_service import NoteService

    core = MagicMock()

    def _make_artifacts() -> ArtifactsAPI:
        return ArtifactsAPI(
            rpc=core,
            drain=core,
            lifecycle=core,
            notebooks=MagicMock(),
            mind_maps=MagicMock(spec=NoteBackedMindMapService),
            note_service=MagicMock(spec=NoteService),
        )

    def _make_notes() -> NotesAPI:
        return NotesAPI(
            notes=MagicMock(spec=NoteService),
            mind_maps=MagicMock(spec=NoteBackedMindMapService),
            save_chat_answer=AsyncMock(),
        )

    artifacts_first = _make_artifacts()
    notes_first = _make_notes()
    # Build in the opposite order too, just to make the symmetry explicit.
    notes_then = _make_notes()
    artifacts_then = _make_artifacts()
    assert artifacts_first is not None
    assert notes_first is not None
    assert artifacts_then is not None
    assert notes_then is not None


# ---------------------------------------------------------------------------
# Mind-map regression — ``generate_mind_map`` + ``list`` + ``download_mind_map``
# must keep working without an explicit ``NotesAPI`` injection.
# ---------------------------------------------------------------------------


def _make_core_for_mind_map_flow() -> tuple[MagicMock, list[tuple[Any, Any]]]:
    """Build a ``MagicMock`` core whose ``rpc_call`` returns canned mind-map
    responses keyed on the RPC method.

    Returns ``(core, calls)`` where ``calls`` is a list of ``(method, params)``
    tuples populated as the test exercises the API.
    """
    calls: list[tuple[Any, Any]] = []

    mind_map_payload = {
        "name": "Mind Map Title",
        "children": [{"name": "child"}],
    }
    mind_map_json = json.dumps(mind_map_payload)

    async def fake_rpc_call(method: Any, params: Any, **_: Any) -> Any:
        calls.append((method, params))
        name = getattr(method, "name", str(method))
        if name == "GENERATE_MIND_MAP":
            return [[mind_map_json]]
        if name == "CREATE_NOTE":
            return [["note_abc"]]
        if name == "UPDATE_NOTE":
            return None
        if name == "GET_NOTES_AND_MIND_MAPS":
            return [
                [
                    [
                        "note_abc",
                        ["note_abc", mind_map_json, [], None, "Mind Map Title"],
                    ]
                ]
            ]
        if name == "LIST_ARTIFACTS":
            return [[]]
        return None

    core = MagicMock()
    # The fake mirrors production: ``rpc_executor`` is the ``RpcCaller``
    # collaborator. ``MagicMock`` auto-vivifies the other ArtifactsRuntime
    # surfaces (``assert_bound_loop`` / ``operation_scope`` /
    # ``register_drain_hook``) on ``core.rpc_executor`` so it satisfies
    # the composite Protocol when threaded into ``ArtifactsAPI``.
    core.rpc_executor.rpc_call = AsyncMock(side_effect=fake_rpc_call)
    # MagicMock blocks ``assert``-prefixed attribute access — install the
    # no-op ``assert_bound_loop`` stub on ``core`` explicitly since it is
    # also passed as the ``drain`` / ``lifecycle`` collaborator.
    core.assert_bound_loop = MagicMock()
    core.get_source_ids = AsyncMock(return_value=["src_1"])
    return core, calls


def _build_artifacts_with_real_mind_map_service(core: MagicMock) -> ArtifactsAPI:
    """Build an ``ArtifactsAPI`` whose mind-map services are real
    instances backed by ``core.rpc_executor`` so the mind-map flow
    exercises the live RPC callbacks against the canned executor.
    """
    from notebooklm._mind_map import NoteBackedMindMapService
    from notebooklm._note_service import NoteService

    note_service = NoteService(core.rpc_executor)
    mind_maps = NoteBackedMindMapService(note_service)
    return ArtifactsAPI(
        rpc=core.rpc_executor,
        drain=core,
        lifecycle=core,
        notebooks=MagicMock(get_source_ids=AsyncMock(return_value=["src_1"])),
        mind_maps=mind_maps,
        note_service=note_service,
    )


@pytest.mark.asyncio
async def test_generate_mind_map_works_without_notes_injection() -> None:
    """``generate_mind_map`` must persist the mind map via ``_mind_map``
    primitives, not via an injected ``NotesAPI``."""
    core, calls = _make_core_for_mind_map_flow()
    api = _build_artifacts_with_real_mind_map_service(core)

    result = await api.generate_mind_map("nb_123", source_ids=["src_1"])

    assert isinstance(result, dict)
    assert result["note_id"] == "note_abc"
    assert result["mind_map"]["name"] == "Mind Map Title"

    # The flow must have gone GENERATE_MIND_MAP -> CREATE_NOTE -> UPDATE_NOTE
    method_names = [getattr(m, "name", str(m)) for m, _ in calls]
    assert "GENERATE_MIND_MAP" in method_names
    assert "CREATE_NOTE" in method_names
    assert "UPDATE_NOTE" in method_names


@pytest.mark.asyncio
async def test_artifacts_list_pulls_mind_maps_without_notes_injection(
    tmp_path: Any,
) -> None:
    """``ArtifactsAPI.list`` must read mind maps through ``_mind_map`` —
    no ``NotesAPI`` reference required."""
    core, _ = _make_core_for_mind_map_flow()
    api = _build_artifacts_with_real_mind_map_service(core)

    artifacts = await api.list("nb_123")
    # One mind map should surface from GET_NOTES_AND_MIND_MAPS.
    assert any(a.kind.name == "MIND_MAP" for a in artifacts)


@pytest.mark.asyncio
async def test_download_mind_map_works_without_notes_injection(
    tmp_path: Any,
) -> None:
    """``download_mind_map`` reaches into mind-map storage via ``_mind_map``
    rather than ``self._notes``."""
    core, _ = _make_core_for_mind_map_flow()
    api = _build_artifacts_with_real_mind_map_service(core)

    output = tmp_path / "mm.json"
    returned = await api.download_mind_map("nb_123", str(output))

    assert returned == str(output)
    saved = json.loads(output.read_text(encoding="utf-8"))
    assert saved["name"] == "Mind Map Title"
