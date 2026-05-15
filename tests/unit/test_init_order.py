"""Init-order regression tests for ``ArtifactsAPI`` / ``NotesAPI`` (T6.F).

Before T6.F, :class:`ArtifactsAPI` required ``notes_api=client.notes`` at
construction time, so :class:`NotesAPI` had to be built first. The shared
:mod:`_mind_map` module decouples the two APIs — these tests pin that
invariant down so the load-bearing init order can't silently come back.
"""

from __future__ import annotations

import ast
import json
from collections import Counter
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from notebooklm._artifacts import ArtifactsAPI
from notebooklm._notes import NotesAPI
from notebooklm.auth import AuthTokens
from notebooklm.client import NotebookLMClient

SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "notebooklm"

# TODO(architecture-remediation): later capability-migration tasks should move
# these feature APIs off ClientCore private state and then shrink this baseline
# to an empty set. Until then, this guard blocks new direct private-state access
# without failing the legitimate pre-extraction call sites.
_ALLOWED_CORE_PRIVATE_ACCESS_COUNTS = {
    ("_artifacts.py", "_begin_transport_task"): 1,
    ("_artifacts.py", "_finish_transport_post"): 1,
    ("_artifacts.py", "_pending_polls"): 1,
    ("_sources.py", "_begin_transport_post"): 1,
    ("_sources.py", "_finish_transport_post"): 1,
}

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
    "_url_utils.py",
    "_version_check.py",
}


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


def test_feature_apis_do_not_add_direct_core_private_state_access() -> None:
    """Pending guard: no new feature API reaches directly into ClientCore internals."""
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
        "Add a public ClientCore capability first, or temporarily extend the "
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
        return self._core.rpc_call(method, params)
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
    """``ArtifactsAPI`` must be constructible without ``notes_api`` — that is
    the whole point of the T6.F decoupling."""
    core = MagicMock()
    api = ArtifactsAPI(core)
    assert api is not None
    # The legacy private attribute must not leak back: code that depends on
    # ``self._notes`` would re-introduce the coupling.
    assert not hasattr(api, "_notes")


def test_artifacts_accepts_legacy_notes_api_kwarg(mock_auth: AuthTokens) -> None:
    """Existing callers passing ``notes_api=`` must keep working as a no-op
    for the deprecation cycle."""
    core = MagicMock()
    notes = NotesAPI(core)
    api = ArtifactsAPI(core, notes_api=notes)
    assert api is not None
    # Even when supplied, the legacy attribute is intentionally not stored.
    assert not hasattr(api, "_notes")


def test_artifacts_before_notes_construction_order(mock_auth: AuthTokens) -> None:
    """Both construction orders must succeed and produce working APIs."""
    core = MagicMock()
    artifacts_first = ArtifactsAPI(core)
    notes_first = NotesAPI(core)
    # Build in the opposite order too, just to make the symmetry explicit.
    notes_then = NotesAPI(core)
    artifacts_then = ArtifactsAPI(core)
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
    core.rpc_call = AsyncMock(side_effect=fake_rpc_call)
    core.get_source_ids = AsyncMock(return_value=["src_1"])
    return core, calls


@pytest.mark.asyncio
async def test_generate_mind_map_works_without_notes_injection() -> None:
    """``generate_mind_map`` must persist the mind map via ``_mind_map``
    primitives, not via an injected ``NotesAPI``."""
    core, calls = _make_core_for_mind_map_flow()
    api = ArtifactsAPI(core)

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
    api = ArtifactsAPI(core)

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
    api = ArtifactsAPI(core)

    output = tmp_path / "mm.json"
    returned = await api.download_mind_map("nb_123", str(output))

    assert returned == str(output)
    saved = json.loads(output.read_text(encoding="utf-8"))
    assert saved["name"] == "Mind Map Title"
