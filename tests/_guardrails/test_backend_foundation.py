"""Dependency-direction gates for the private semantic backend port."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import get_type_hints

import notebooklm
import notebooklm._semantic.backend as backend_module
import notebooklm._semantic.records as records_module
from notebooklm._semantic.backend import BackendAdapter

_ROOT = Path(__file__).resolve().parents[2]
_BACKEND = _ROOT / "src" / "notebooklm" / "_semantic" / "backend.py"
_RECORDS = _ROOT / "src" / "notebooklm" / "_semantic" / "records" / "__init__.py"
_FAKE = _ROOT / "tests" / "_fixtures" / "recording_backend.py"

_FORBIDDEN_MODULE_PARTS = frozenset({"httpx", "cookie", "protobuf", "cli", "mcp", "server", "rpc"})
_FORBIDDEN_IDENTIFIERS = frozenset(
    {"RPCMethod", "NotebookLMClient", "Request", "Response", "CookieJar"}
)


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.add(node.module)
    return modules


def _identifiers(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}


def test_backend_port_and_records_import_only_neutral_dependencies() -> None:
    for path in (_BACKEND, _RECORDS):
        imported = _imported_modules(path)
        assert not {
            module
            for module in imported
            if any(part in _FORBIDDEN_MODULE_PARTS for part in module.split("."))
        }
        assert not (_identifiers(path) & _FORBIDDEN_IDENTIFIERS)


def test_backend_protocol_annotations_contain_no_adapter_or_wire_shapes() -> None:
    hints = get_type_hints(BackendAdapter.invoke)
    rendered = " ".join(repr(value) for value in hints.values())

    assert "RuntimeDeadline" in rendered
    assert "OperationDef" in rendered
    assert not any(identifier in rendered for identifier in _FORBIDDEN_IDENTIFIERS)
    assert not any(token in rendered.lower() for token in ("http", "cookie", "protobuf"))
    assert "list[" not in rendered
    assert "tuple[" not in rendered


def test_foundation_modules_are_private_and_define_no_public_package_exports() -> None:
    assert backend_module.__name__ == "notebooklm._semantic.backend"
    assert records_module.__name__ == "notebooklm._semantic.records"
    assert not (set(backend_module.__all__) | set(records_module.__all__)) & set(notebooklm.__all__)


def test_recording_fake_has_no_client_runtime_or_transport_dependency() -> None:
    imported = _imported_modules(_FAKE)
    forbidden = {"client", "middleware", "cookie", "httpx", "rpc"}

    assert not {
        module for module in imported if any(part in forbidden for part in module.split("."))
    }
