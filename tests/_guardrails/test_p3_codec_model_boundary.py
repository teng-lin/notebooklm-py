"""Fail-closed P3 wire-codec/public-model dependency and ownership gates."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.repo_lint

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src" / "notebooklm"
_CODECS = _SRC / "_web" / "codec"
_PROJECTORS = _SRC / "_projectors.py"

_PUBLIC_FACTORY_CALLS = {
    ("Notebook", "from_api_response"),
    ("Source", "from_row"),
    ("Source", "from_api_response"),
    ("Artifact", "from_api_response"),
    ("Artifact", "from_mind_map"),
    ("Label", "from_api_response"),
    ("Collection", "from_api_response"),
    ("SharedUser", "from_api_response"),
    ("ShareStatus", "from_api_response"),
    ("NotebookDescription", "from_api_response"),
    ("ReportSuggestion", "from_api_response"),
}
_PUBLIC_FACTORY_METHODS = {method for _, method in _PUBLIC_FACTORY_CALLS}
_REVIEWED_CODEC_VALUE_IMPORTS = {
    "documents.py": {
        ("_types.documents", "StructuredDocument"),
    },
    "chat_saved_note.py": {
        ("_types.documents", "utf16_len"),
    },
    # P10 R2.1 retired this module's three ``types`` allowances: the streamed
    # parser emits ``ChatReferenceRecord`` / ``ChatTurnKeyRecord`` /
    # ``ChatNextStepRecord`` and the facade projects them onto the public
    # ``ChatReference`` / ``ConversationTurnKey`` / ``NextStepSuggestion``.
    # Only the document value types remain (ADR-0035's closed-stdlib exemption).
    "chat_stream.py": {
        ("_types.documents", "DocumentAnnotation"),
        ("_types.documents", "StructuredDocument"),
        ("_types.documents", "_utf16_slice"),
        ("_types.documents", "utf16_len"),
    },
}


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_retained_public_factories_have_no_production_callers() -> None:
    violations: list[str] = []
    for path in sorted(_SRC.rglob("*.py")):
        if "_types" in path.parts:
            continue
        for node in ast.walk(_tree(path)):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr in _PUBLIC_FACTORY_METHODS:
                violations.append(
                    f"{path.relative_to(_ROOT)}:{node.lineno}:{ast.unparse(node.func)}"
                )
    assert violations == []


def test_web_codecs_do_not_import_or_construct_public_models() -> None:
    forbidden_names = {owner for owner, _ in _PUBLIC_FACTORY_CALLS}
    violations: list[str] = []
    for path in sorted(_CODECS.glob("*.py")):
        tree = _tree(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module == "types" or node.module.startswith(
                    ("_types", "notebooklm.types", "notebooklm._types")
                ):
                    imported = {(node.module, alias.name) for alias in node.names}
                    unexpected = imported - _REVIEWED_CODEC_VALUE_IMPORTS.get(path.name, set())
                    violations.extend(
                        f"{path.name}:{node.lineno}:import {module}.{name}"
                        for module, name in sorted(unexpected)
                    )
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id in forbidden_names:
                    violations.append(f"{path.name}:{node.lineno}:construct {node.func.id}")
    assert violations == []
    actual_reviewed = {
        path.name: {
            (node.module, alias.name)
            for node in ast.walk(_tree(path))
            if isinstance(node, ast.ImportFrom)
            and node.module
            and (
                node.module == "types"
                or node.module.startswith(("_types", "notebooklm.types", "notebooklm._types"))
            )
            for alias in node.names
        }
        for path in sorted(_CODECS.glob("*.py"))
        if path.name in _REVIEWED_CODEC_VALUE_IMPORTS
    }
    assert actual_reviewed == _REVIEWED_CODEC_VALUE_IMPORTS


def test_projectors_have_no_wire_factories_rpc_ids_or_positional_indices() -> None:
    violations: list[str] = []
    for node in ast.walk(_tree(_PROJECTORS)):
        if isinstance(node, ast.Attribute) and node.attr in {
            "from_api_response",
            "from_row",
            "from_mind_map",
        }:
            violations.append(f"factory:{node.lineno}")
        elif isinstance(node, ast.Name) and node.id == "RPCMethod":
            violations.append(f"rpc:{node.lineno}")
        elif (
            isinstance(node, ast.Subscript)
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, int)
        ):
            violations.append(f"positional:{node.lineno}")
    assert violations == []


def test_public_model_wire_dependency_allowlist_cannot_grow() -> None:
    model_dir = _SRC / "_types"
    rpc_importers: set[str] = set()
    forbidden: list[str] = []
    for path in sorted(model_dir.glob("*.py")):
        for node in ast.walk(_tree(path)):
            if not isinstance(node, ast.ImportFrom) or node.module is None:
                continue
            if node.module.startswith("_web") or "._web" in node.module:
                forbidden.append(f"{path.name}:{node.lineno}:{node.module}")
            if node.module.startswith("rpc") or node.module.startswith("..rpc"):
                rpc_importers.add(path.name)
    assert forbidden == []
    assert rpc_importers == {
        "artifacts.py",
        "chat.py",
        "notebooks.py",
        "research.py",
        "sharing.py",
        "sources.py",
    }
