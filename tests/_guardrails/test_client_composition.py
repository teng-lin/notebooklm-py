"""Final client-composition architecture guards."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CLIENT_PATH = REPO_ROOT / "src" / "notebooklm" / "client.py"
COMPOSITION_PATH = REPO_ROOT / "src" / "notebooklm" / "_client_composition.py"
RUNTIME_INIT_PATH = REPO_ROOT / "src" / "notebooklm" / "_runtime" / "init.py"
EXECUTABLE_ROOTS = (REPO_ROOT / "tests", REPO_ROOT / "scripts")

# Both production composition-root files: the public constructor delegate and
# its private production-only graph builder. Tests never call either builder
# directly.
COMPOSITION_ROOT_PATHS = (CLIENT_PATH, COMPOSITION_PATH)

# Names a composition-root scope may bind the client instance to:
# ``self`` inside ``NotebookLMClient`` methods, ``client`` inside
# ``compose_client``.
CLIENT_HOST_NAMES = {"self", "client"}

FEATURE_API_NAMES = {
    "ArtifactsAPI",
    "ChatAPI",
    "LabelsAPI",
    "MindMapsAPI",
    "NotebooksAPI",
    "NoteBackedMindMapService",
    "NotesAPI",
    "ResearchAPI",
    "SettingsAPI",
    "SharingAPI",
    "SourcesAPI",
    "SourceUploadPipeline",
    "NoteService",
}

INLINE_CLIENT_ATTRS = {
    "_transport",
    "_chain_host",
    "_chain_builder",
    "_middlewares",
    "_rpc_semaphore",
    "_max_concurrent_rpcs",
}

RETIRED_CLIENT_RUNTIME_ATTRS = {
    "_collaborators",
    "_composed",
    "_rpc_executor",
}

# These two focused lifecycle harnesses define their own tiny host objects with
# an executor field; neither object is a NotebookLMClient. Keep the exemption
# exact to ``self._rpc_executor`` so a client-shaped local in the same file
# still fails the audit.
NON_CLIENT_EXECUTOR_HOST_FILES = {
    "tests/unit/concurrency/test_session_close_refresh_race.py",
    "tests/unit/test_runtime_lifecycle.py",
}


def _tree(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("path", COMPOSITION_ROOT_PATHS, ids=lambda p: p.name)
def test_features_receive_specific_collaborators_not_whole_client(path: Path) -> None:
    tree = _tree(path)
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id not in FEATURE_API_NAMES:
            continue
        for arg in node.args:
            if isinstance(arg, ast.Name) and arg.id in CLIENT_HOST_NAMES:
                violations.append(f"{node.func.id} line {node.lineno}: passes {arg.id}")
        for kw in node.keywords:
            if isinstance(kw.value, ast.Name) and kw.value.id in CLIENT_HOST_NAMES:
                violations.append(f"{node.func.id} line {node.lineno}: passes {kw.value.id}")

    assert not violations, (
        f"Feature APIs in {path.name} must receive explicit collaborators, "
        "not the whole client:\n  " + "\n  ".join(violations)
    )


@pytest.mark.parametrize("path", COMPOSITION_ROOT_PATHS, ids=lambda p: p.name)
def test_notebooklm_client_does_not_inline_composition_holder_state(path: Path) -> None:
    tree = _tree(path)
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        if node.attr not in INLINE_CLIENT_ATTRS:
            continue
        if isinstance(node.value, ast.Name) and node.value.id in CLIENT_HOST_NAMES:
            violations.append(f"line {node.lineno}: {node.value.id}.{node.attr}")

    assert not violations, (
        f"{path.name} must keep runtime state on the backend-owned atomic runtime:\n  "
        + "\n  ".join(violations)
    )


def test_mutable_client_composed_holder_is_retired() -> None:
    assert not (REPO_ROOT / "src" / "notebooklm" / "_client_composed.py").exists()
    for path in COMPOSITION_ROOT_PATHS:
        assert "ClientComposed" not in path.read_text(encoding="utf-8")


def test_executable_tests_and_scripts_use_backend_owned_runtime_leaves() -> None:
    """Retired client aliases cannot survive as executable test/diagnostic seams."""
    violations: list[str] = []
    for root in EXECUTABLE_ROOTS:
        for path in root.rglob("*.py"):
            relative = path.relative_to(REPO_ROOT).as_posix()
            tree = _tree(path)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Attribute):
                    continue
                if node.attr not in RETIRED_CLIENT_RUNTIME_ATTRS:
                    continue
                if (
                    node.attr == "_rpc_executor"
                    and relative in NON_CLIENT_EXECUTOR_HOST_FILES
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "self"
                ):
                    continue
                violations.append(f"{relative}:{node.lineno}: {ast.unparse(node)}")

    assert not violations, (
        "Executable tests/scripts must reach runtime seams through backend-owned leaves:\n  "
        + "\n  ".join(violations)
    )


def test_client_internals_is_a_frozen_complete_runtime_record() -> None:
    tree = _tree(RUNTIME_INIT_PATH)
    classes = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == "ClientInternals"
    ]
    assert len(classes) == 1
    class_node = classes[0]
    fields = {
        node.target.id
        for node in class_node.body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    }
    assert fields == {
        "metrics",
        "drain_tracker",
        "reqid",
        "auth_coord",
        "provider_kernel",
        "backend_kernel",
        "backend_session",
        "lifecycle",
        "cookie_persistence",
        "provider",
        "executor",
        "web_transport_factory",
        "rpc_semaphore",
        "transport",
        "pipeline",
    }
    decorator = class_node.decorator_list[0]
    assert isinstance(decorator, ast.Call)
    assert any(
        keyword.arg == "frozen" and isinstance(keyword.value, ast.Constant) and keyword.value.value
        for keyword in decorator.keywords
    )
