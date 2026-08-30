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
    "WebChatAPI",
    "WebCollectionsAPI",
    "WebLabelsAPI",
    "WebMindMapsAPI",
    "WebNotebooksAPI",
    "WebArtifactsAPI",
    "NoteBackedMindMapService",
    "WebNotesAPI",
    "WebResearchAPI",
    "WebSettingsAPI",
    "WebSharingAPI",
    "WebSourcesAPI",
    "SourceUploadPipeline",
    "NoteService",
}

WEB_ONLY_NAMESPACE_IMPORTS: dict[str, str] = {}

NEUTRAL_NAMESPACE_IMPORTS = {
    "CollectionsAPI": "_collections",
    "LabelsAPI": "_labels",
    "BaseResearchAPI": "_research",
}

WEB_NAMESPACE_IMPLEMENTATION_IMPORTS = {
    "WebCollectionsAPI": "_web.collections",
    "WebLabelsAPI": "_web.labels",
    "WebResearchAPI": "_web.research",
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
def test_web_only_namespaces_are_composed_from_their_canonical_modules(path: Path) -> None:
    """Client annotations and assembly share the exact moved class objects."""
    imported_from: dict[str, str] = {}
    for node in ast.walk(_tree(path)):
        if not isinstance(node, ast.ImportFrom) or node.module is None:
            continue
        for alias in node.names:
            if alias.name in WEB_ONLY_NAMESPACE_IMPORTS:
                imported_from[alias.name] = node.module

    assert imported_from == WEB_ONLY_NAMESPACE_IMPORTS


def test_client_annotations_use_neutral_namespace_contracts() -> None:
    """The public client shape must not expose concrete web implementations."""
    imported_from: dict[str, str] = {}
    for node in ast.walk(_tree(CLIENT_PATH)):
        if not isinstance(node, ast.ImportFrom) or node.module is None:
            continue
        for alias in node.names:
            if alias.name in NEUTRAL_NAMESPACE_IMPORTS:
                imported_from[alias.name] = node.module

    assert imported_from == NEUTRAL_NAMESPACE_IMPORTS


def test_assembly_uses_concrete_web_namespace_implementations() -> None:
    """The composition root, and only it, selects the web backend classes."""
    imported_from: dict[str, str] = {}
    for node in ast.walk(_tree(ASSEMBLY_PATH)):
        if not isinstance(node, ast.ImportFrom) or node.module is None:
            continue
        for alias in node.names:
            if alias.name in WEB_NAMESPACE_IMPLEMENTATION_IMPORTS:
                imported_from[alias.name] = node.module

    assert imported_from == WEB_NAMESPACE_IMPLEMENTATION_IMPORTS


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
