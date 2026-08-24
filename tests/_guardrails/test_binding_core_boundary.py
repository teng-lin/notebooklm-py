"""P9.0: the neutral binding core imports nothing wire-specific.

``_binding.py`` is what lets dispatch be type-checked end to end, and that
neutrality is only worth something if it is pinned.  The allowed import set is
an exact literal: adding a module is a reviewed derivation change, never a
loosening.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "notebooklm"
BINDING_PATH = SRC_ROOT / "_binding.py"

ALLOWED_STDLIB_IMPORTS = frozenset(
    {
        "__future__",
        "collections.abc",
        "dataclasses",
        "enum",
        "types",
        "typing",
    }
)
ALLOWED_FIRST_PARTY_IMPORTS = frozenset({"_backend", "_deadline", "_operations"})
FORBIDDEN_PREFIXES = ("_web", "rpc", "_auth", "httpx", "_runtime")


def collect_imports(path: Path) -> tuple[frozenset[str], frozenset[str]]:
    """Return ``(absolute modules, relative first-party modules)`` imported by ``path``."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    absolute: set[str] = set()
    relative: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            absolute.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                relative.add(node.module or "")
            else:
                absolute.add(node.module or "")
    return frozenset(absolute), frozenset(relative)


def test_binding_core_import_set_is_exact() -> None:
    absolute, relative = collect_imports(BINDING_PATH)
    assert absolute == ALLOWED_STDLIB_IMPORTS
    assert relative == ALLOWED_FIRST_PARTY_IMPORTS
    assert not any(module.startswith(FORBIDDEN_PREFIXES) for module in absolute | relative)


def test_binding_core_never_names_the_native_method_enum() -> None:
    tree = ast.parse(BINDING_PATH.read_text(encoding="utf-8"), filename=str(BINDING_PATH))
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    assert "RPCMethod" not in names | attributes


def test_collector_sees_relative_and_absolute_imports(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text(
        "import httpx\nfrom ._web.backend import WebRpcBackend\nfrom typing import Any\n",
        encoding="utf-8",
    )
    absolute, relative = collect_imports(sample)
    assert absolute == {"httpx", "typing"}
    assert relative == {"_web.backend"}
