"""Final client-composition architecture guards."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CLIENT_PATH = REPO_ROOT / "src" / "notebooklm" / "client.py"
ASSEMBLY_PATH = REPO_ROOT / "src" / "notebooklm" / "_client_assembly.py"
CLIENT_COMPAT_PATH = REPO_ROOT / "src" / "notebooklm" / "_client_compat.py"
WEB_ASSEMBLY_PATH = REPO_ROOT / "src" / "notebooklm" / "_web" / "assembly.py"
ANDROID_ASSEMBLY_PATH = REPO_ROOT / "src" / "notebooklm" / "_android" / "assembly.py"
COMPOSED_PATH = REPO_ROOT / "src" / "notebooklm" / "_web" / "transport" / "composed.py"

# Both composition-root files: ``client.py`` (the thin ``__init__``
# delegate) and ``_client_assembly.py`` (the shared assembly seam the
# constructor and the canonical test factory both run). The guards below
# scan both so moving wiring between them can't dodge the gate.
COMPOSITION_ROOT_PATHS = (
    CLIENT_PATH,
    ASSEMBLY_PATH,
    WEB_ASSEMBLY_PATH,
    ANDROID_ASSEMBLY_PATH,
)

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
    "AndroidArtifactsAPI",
    "AndroidChatAPI",
    "AndroidCollectionsAPI",
    "AndroidLabelsAPI",
    "AndroidMindMapsAPI",
    "AndroidNotebooksAPI",
    "AndroidNotesAPI",
    "AndroidResearchAPI",
    "AndroidSettingsAPI",
    "AndroidSharingAPI",
    "AndroidSourcesAPI",
    "NoteBackedMindMapArtifactAdapter",
}

WEB_ONLY_NAMESPACE_IMPORTS = {
    "WebArtifactsAPI": "_web.artifacts",
    "WebChatAPI": "_web.chat",
    "WebCollectionsAPI": "_web.collections",
    "WebLabelsAPI": "_web.labels",
    "WebMindMapsAPI": "_web.mind_maps",
    "WebNotebooksAPI": "_web.notebooks",
    "WebNotesAPI": "_web.notes",
    "WebResearchAPI": "_web.research",
    "WebSettingsAPI": "_web.settings",
    "WebSharingAPI": "_web.sharing",
    "WebSourcesAPI": "_web.sources",
    "NoteBackedMindMapService": "_web.mind_maps",
    "NoteService": "_web.notes",
}

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


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("path", (CLIENT_PATH, ASSEMBLY_PATH), ids=lambda p: p.name)
def test_public_client_and_root_selector_import_no_web_namespaces(path: Path) -> None:
    """Only the selected Web assembler may import Web namespace implementations."""
    imported_from: dict[str, str] = {}
    for node in ast.walk(_tree(path)):
        if not isinstance(node, ast.ImportFrom) or node.module is None:
            continue
        for alias in node.names:
            if alias.name in WEB_ONLY_NAMESPACE_IMPORTS:
                imported_from[alias.name] = node.module

    assert imported_from == {}


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
    for node in ast.walk(_tree(WEB_ASSEMBLY_PATH)):
        if not isinstance(node, ast.ImportFrom) or node.module is None:
            continue
        for alias in node.names:
            if alias.name in WEB_NAMESPACE_IMPLEMENTATION_IMPORTS:
                imported_from[alias.name] = f"_web.{node.module.lstrip('.')}"

    assert imported_from == WEB_NAMESPACE_IMPLEMENTATION_IMPORTS


def test_web_assembly_imports_every_namespace_from_its_canonical_module() -> None:
    imported_from: dict[str, str] = {}
    for node in ast.walk(_tree(WEB_ASSEMBLY_PATH)):
        if not isinstance(node, ast.ImportFrom) or node.module is None:
            continue
        for alias in node.names:
            if alias.name in WEB_ONLY_NAMESPACE_IMPORTS:
                imported_from[alias.name] = f"_web.{node.module.lstrip('.')}"

    assert imported_from == WEB_ONLY_NAMESPACE_IMPORTS


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
        if isinstance(node, ast.FunctionDef) and node.name in {
            "collaborators",
            "runtime_collaborators",
            "bind_runtime_collaborators",
        }:
            violations.append(f"property/function line {node.lineno}: {node.name}")
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Attribute) and target.attr in {
                    "collaborators",
                    "_runtime_collaborators",
                }:
                    violations.append(f"assignment line {node.lineno}: .{target.attr}")
    assert not violations, (
        "ClientComposed must not retain the final shared runtime bundle:\n  "
        + "\n  ".join(violations)
    )


def test_android_web_compatibility_installer_has_one_root_owner() -> None:
    """One pure factory owns sidecar construction and its lazy Web builder."""
    sidecar_constructors: list[str] = []
    runtime_builder_calls: list[str] = []
    lifecycle_calls: list[str] = []
    for path in sorted((REPO_ROOT / "src" / "notebooklm").rglob("*.py")):
        relative = path.relative_to(REPO_ROOT).as_posix()
        for node in ast.walk(_tree(path)):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id == "LazyWebSidecar":
                    sidecar_constructors.append(f"{relative}:{node.lineno}")
                if node.func.id == "build_compatibility_runtime":
                    runtime_builder_calls.append(f"{relative}:{node.lineno}")
                if node.func.id == "ClientLifecycle":
                    lifecycle_calls.append(f"{relative}:{node.lineno}")

    assert len(sidecar_constructors) == 1
    assert sidecar_constructors[0].startswith("src/notebooklm/_client_compat.py:")
    assert len(runtime_builder_calls) == 1
    assert runtime_builder_calls[0].startswith("src/notebooklm/_client_compat.py:")
    assert len(lifecycle_calls) == 1
    assert lifecycle_calls[0].startswith("src/notebooklm/_client_assembly.py:")


def test_android_web_compatibility_import_and_lifecycle_placement_are_exact() -> None:
    """Only root installation and loaded-auth finalization mutate a client."""
    tree = _tree(CLIENT_COMPAT_PATH)
    factory = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "build_compatibility_sidecar"
    )
    builder = next(
        node
        for node in factory.body
        if isinstance(node, ast.FunctionDef) and node.name == "build_sidecar_runtime"
    )
    web_imports = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "_web.assembly"
    ]
    assert len(web_imports) == 1
    assert web_imports[0] in list(ast.walk(builder))
    assert not [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        and (
            any(alias.name.startswith("notebooklm._android") for alias in node.names)
            if isinstance(node, ast.Import)
            else node.module is not None and "_android" in node.module
        )
    ]

    scoped = (WEB_ASSEMBLY_PATH, ANDROID_ASSEMBLY_PATH, CLIENT_COMPAT_PATH, ASSEMBLY_PATH)
    violations: list[str] = []
    for path in scoped:
        for function in (
            node for node in ast.walk(_tree(path)) if isinstance(node, ast.FunctionDef)
        ):
            client_parameters = {arg.arg for arg in function.args.args if arg.arg == "client"}
            client_parameters.update(
                arg.arg for arg in function.args.kwonlyargs if arg.arg == "client"
            )
            if not client_parameters or (
                path == ASSEMBLY_PATH
                and function.name in {"_install_client", "_finalize_loaded_client"}
            ):
                continue
            for node in ast.walk(function):
                targets: list[ast.expr] = []
                if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                    if isinstance(node, ast.Assign):
                        targets.extend(node.targets)
                    else:
                        targets.append(node.target)
                if any(
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "client"
                    for target in targets
                ):
                    violations.append(f"{path.name}:{function.name}:{node.lineno}")
    assert violations == []


def test_android_rpc_call_materializes_sidecar_inside_operation_lease() -> None:
    """The temporary Web runtime must never materialize outside root admission."""
    rpc_call = next(
        node
        for node in ast.walk(_tree(CLIENT_PATH))
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "rpc_call"
    )
    materialize_calls = [
        node
        for node in ast.walk(rpc_call)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "materialize"
    ]
    leases = [
        node
        for node in ast.walk(rpc_call)
        if isinstance(node, ast.AsyncWith)
        and any(
            isinstance(item.context_expr, ast.Call)
            and isinstance(item.context_expr.func, ast.Attribute)
            and item.context_expr.func.attr == "operation_scope"
            and len(item.context_expr.args) == 1
            and isinstance(item.context_expr.args[0], ast.Constant)
            and item.context_expr.args[0].value == "rpc_call.sidecar"
            for item in node.items
        )
    ]
    assert len(materialize_calls) == 1
    assert len(leases) == 1
    assert materialize_calls[0] in [
        child for statement in leases[0].body for child in ast.walk(statement)
    ]
