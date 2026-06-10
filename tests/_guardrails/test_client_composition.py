"""Final client-composition architecture guards."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CLIENT_PATH = REPO_ROOT / "src" / "notebooklm" / "client.py"
ASSEMBLY_PATH = REPO_ROOT / "src" / "notebooklm" / "_client_assembly.py"
COMPOSED_PATH = REPO_ROOT / "src" / "notebooklm" / "_client_composed.py"

# Both composition-root files: ``client.py`` (the thin ``__init__``
# delegate) and ``_client_assembly.py`` (the shared assembly seam the
# constructor and the canonical test factory both run). The guards below
# scan both so moving wiring between them can't dodge the gate.
COMPOSITION_ROOT_PATHS = (CLIENT_PATH, ASSEMBLY_PATH)

# Names a composition-root scope may bind the client instance to:
# ``self`` inside ``NotebookLMClient`` methods, ``client`` inside
# ``_assemble_client``.
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
        f"{path.name} must keep composition holder state on ClientComposed:\n  "
        + "\n  ".join(violations)
    )


def test_client_composed_does_not_expose_collaborators_alias() -> None:
    tree = _tree(COMPOSED_PATH)
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "collaborators":
            violations.append(f"property/function line {node.lineno}: collaborators")
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Attribute) and target.attr == "collaborators":
                    violations.append(f"assignment line {node.lineno}: .collaborators")
    assert not violations, (
        "ClientComposed must expose runtime_collaborators, not collaborators:\n  "
        + "\n  ".join(violations)
    )
