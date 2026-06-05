"""Final client-composition architecture guards."""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CLIENT_PATH = REPO_ROOT / "src" / "notebooklm" / "client.py"
COMPOSED_PATH = REPO_ROOT / "src" / "notebooklm" / "_client_composed.py"

FEATURE_API_NAMES = {
    "ArtifactsAPI",
    "ChatAPI",
    "NotebooksAPI",
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


def test_features_receive_specific_collaborators_not_whole_client() -> None:
    tree = _tree(CLIENT_PATH)
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id not in FEATURE_API_NAMES:
            continue
        for arg in node.args:
            if isinstance(arg, ast.Name) and arg.id == "self":
                violations.append(f"{node.func.id} line {node.lineno}: passes self")
        for kw in node.keywords:
            if isinstance(kw.value, ast.Name) and kw.value.id == "self":
                violations.append(f"{node.func.id} line {node.lineno}: passes self")

    assert not violations, (
        "Feature APIs must receive explicit collaborators, not the whole client:\n  "
        + "\n  ".join(violations)
    )


def test_notebooklm_client_does_not_inline_composition_holder_state() -> None:
    tree = _tree(CLIENT_PATH)
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        if node.attr not in INLINE_CLIENT_ATTRS:
            continue
        if isinstance(node.value, ast.Name) and node.value.id == "self":
            violations.append(f"line {node.lineno}: self.{node.attr}")

    assert not violations, (
        "NotebookLMClient must keep composition holder state on ClientComposed:\n  "
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
