"""Guardrails for the private ``notebooklm._types`` implementation boundary."""

from __future__ import annotations

import ast
import importlib
import os
import pkgutil
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src" / "notebooklm"
TYPES_PATH = SRC_ROOT / "types.py"
PRIVATE_TYPES_ROOT = SRC_ROOT / "_types"
CLI_ROOT = SRC_ROOT / "cli"
PUBLIC_DOC_ROOTS = (PROJECT_ROOT / "README.md", PROJECT_ROOT / "docs")
INTERNAL_ARCHITECTURE_DOCS = {
    PROJECT_ROOT / "docs" / "development.md",
    PROJECT_ROOT / "docs" / "rpc-development.md",
    PROJECT_ROOT / "docs" / "refactor-history.md",
}

# Add names here only for explicit facade wrappers that must keep a public
# monkeypatch seam while delegating implementation to a private _types module.
ALLOWED_TYPES_WRAPPER_BODIES: set[str] = set()
PRIVATE_NOTEBOOKLM_IMPORT_RE = re.compile(
    r"\b(?:from\s+notebooklm(?:\._(?!_)\w+(?:\.\w+)*|\s+import\s+_(?!_)\w+)\b"
    r"|import\s+notebooklm\._(?!_)\w+(?:\.\w+)*\b)"
)


def _iter_private_type_module_names() -> list[str]:
    """Return importable private type implementation modules that have landed."""
    return sorted(
        module_info.name
        for module_info in pkgutil.iter_modules([str(PRIVATE_TYPES_ROOT)])
        if not module_info.ispkg and module_info.name != "__init__"
    )


def _private_type_module_qualname(module_name: str) -> str:
    return f"notebooklm._types.{module_name}"


def _iter_python_files(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*.py") if "__pycache__" not in path.parts)


def _import_statement_text(node: ast.Import | ast.ImportFrom) -> str:
    return ast.unparse(node)


def _cli_private_types_import_offenders(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    offenders: list[str] = []
    relative_parts = path.relative_to(CLI_ROOT).parts
    cli_package_level = len(relative_parts)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "notebooklm._types" or alias.name.startswith("notebooklm._types."):
                    offenders.append(f"line {node.lineno}: {_import_statement_text(node)}")
            continue

        if not isinstance(node, ast.ImportFrom):
            continue

        module = node.module or ""
        module_parts = module.split(".") if module else []
        is_absolute_private_types = module_parts[:2] == ["notebooklm", "_types"]
        is_from_notebooklm_import_types = (
            node.level == 0
            and module_parts == ["notebooklm"]
            and any(alias.name == "_types" for alias in node.names)
        )
        # A root CLI module needs ``..`` to reach notebooklm; a nested CLI module
        # needs one more dot per package segment, so only levels above this depth
        # can resolve to notebooklm._types rather than a hypothetical cli._types.
        is_relative_private_types = node.level > cli_package_level and (
            module_parts[:1] == ["_types"]
            or (not module_parts and any(alias.name == "_types" for alias in node.names))
        )

        if (
            is_absolute_private_types
            or is_from_notebooklm_import_types
            or is_relative_private_types
        ):
            offenders.append(f"line {node.lineno}: {_import_statement_text(node)}")

    return offenders


def _iter_public_docs() -> list[Path]:
    docs: list[Path] = []
    for root in PUBLIC_DOC_ROOTS:
        if root.is_file():
            docs.append(root)
        elif root.is_dir():
            docs.extend(
                path for path in root.rglob("*.md") if path not in INTERNAL_ARCHITECTURE_DOCS
            )
    return sorted(docs)


def _public_docs_private_import_offenders() -> list[str]:
    offenders: list[str] = []
    for path in _iter_public_docs():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if PRIVATE_NOTEBOOKLM_IMPORT_RE.search(line):
                relative = path.relative_to(PROJECT_ROOT)
                offenders.append(f"{relative}:{lineno}: {line.strip()}")
    return offenders


def _top_level_name(target: ast.expr) -> str | None:
    return target.id if isinstance(target, ast.Name) else None


def _assignment_target_names(node: ast.Assign | ast.AnnAssign) -> list[str]:
    if isinstance(node, ast.AnnAssign):
        name = _top_level_name(node.target)
        return [name] if name else []
    return [name for target in node.targets if (name := _top_level_name(target))]


def _private_type_module_aliases(tree: ast.Module) -> set[str]:
    aliases: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if (node.level == 1 and module == "_types") or (
                node.level == 0 and module == "notebooklm._types"
            ):
                aliases.update(alias.asname or alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("notebooklm._types."):
                    aliases.add(alias.asname or alias.name.rsplit(".", maxsplit=1)[-1])
    return aliases


def _module_attribute_alias(
    value: ast.AST | None, target_name: str, private_type_module_aliases: set[str]
) -> bool:
    """Return True for compatibility aliases like ``_safe = _source_types._safe``."""
    return (
        isinstance(value, ast.Attribute)
        and value.attr == target_name
        and isinstance(value.value, ast.Name)
        and value.value.id in private_type_module_aliases
    )


def _private_type_module_symbols() -> tuple[set[str], set[str]]:
    public_type_names: set[str] = set()
    private_helper_names: set[str] = set()

    for path in sorted(PRIVATE_TYPES_ROOT.glob("*.py")):
        if path.name == "__init__.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
                public_type_names.add(node.name)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith(
                "_"
            ):
                private_helper_names.add(node.name)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                for name in _assignment_target_names(node):
                    if name.startswith("_") and not name.startswith("__"):
                        private_helper_names.add(name)

    return public_type_names, private_helper_names


def _types_facade_body_offenders() -> list[str]:
    public_type_names, private_helper_names = _private_type_module_symbols()
    tree = ast.parse(TYPES_PATH.read_text(encoding="utf-8"))
    private_type_module_aliases = _private_type_module_aliases(tree)
    offenders: list[str] = []

    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name in public_type_names:
            offenders.append(f"{node.name} (ClassDef line {node.lineno})")
            continue

        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
            node.name in private_helper_names
        ):
            if node.name not in ALLOWED_TYPES_WRAPPER_BODIES:
                offenders.append(f"{node.name} ({type(node).__name__} line {node.lineno})")
            continue

        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            target_names = _assignment_target_names(node)
            for name in target_names:
                if name not in private_helper_names:
                    continue
                if _module_attribute_alias(node.value, name, private_type_module_aliases):
                    continue
                offenders.append(f"{name} ({type(node).__name__} line {node.lineno})")

    return sorted(offenders)


def _types_all_names() -> list[str]:
    tree = ast.parse(TYPES_PATH.read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if any(isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets):
            value = ast.literal_eval(node.value)
            if not isinstance(value, list):
                raise AssertionError("notebooklm.types.__all__ must be assigned a list literal")
            return [str(item) for item in value]
    raise AssertionError("notebooklm.types.__all__ assignment not found")


@pytest.mark.parametrize("module_name", _iter_private_type_module_names())
def test_private_type_modules_import_directly(module_name: str) -> None:
    """Each landed ``_types`` module is independently importable."""
    module = importlib.import_module(_private_type_module_qualname(module_name))

    assert isinstance(module, ModuleType)


def test_private_type_modules_import_directly_in_clean_interpreter() -> None:
    """Smoke test the landed private type modules from a fresh import graph."""
    module_names = [
        _private_type_module_qualname(module_name)
        for module_name in _iter_private_type_module_names()
    ]
    code = "\n".join(
        [
            "import importlib",
            f"modules = {module_names!r}",
            "for module in modules:",
            "    importlib.import_module(module)",
        ]
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = (
        f"{PROJECT_ROOT / 'src'}{os.pathsep}{env['PYTHONPATH']}"
        if env.get("PYTHONPATH")
        else str(PROJECT_ROOT / "src")
    )

    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=PROJECT_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_types_facade_identity_reexports_for_landed_private_types() -> None:
    """``notebooklm.types`` must return the canonical objects from landed modules."""
    import notebooklm.types as public_types

    public_type_names, _ = _private_type_module_symbols()
    checked: set[str] = set()
    for module_name in _iter_private_type_module_names():
        module = importlib.import_module(_private_type_module_qualname(module_name))
        for name in sorted(public_type_names):
            if not hasattr(module, name):
                continue
            assert getattr(public_types, name) is getattr(module, name), (
                f"notebooklm.types.{name} must be an identity re-export from "
                f"{module.__name__}.{name}"
            )
            checked.add(name)

    assert checked == public_type_names, (
        "Not every landed private type identity was checked. "
        f"Missing: {sorted(public_type_names - checked)}"
    )


def test_types_all_does_not_export_private_helper_names() -> None:
    """The public ``types.__all__`` surface must not expose compatibility seams."""
    private_exports = [name for name in _types_all_names() if name.startswith("_")]

    assert private_exports == []


def test_types_facade_has_no_landed_implementation_bodies() -> None:
    """Landed boundaries live in ``_types``; ``types.py`` keeps only facade aliases."""
    offenders = _types_facade_body_offenders()

    assert not offenders, (
        "notebooklm.types regained implementation bodies for landed _types symbols. "
        "Move implementations back to their private modules, or add only an explicit "
        f"compatibility wrapper. Offending AST node names: {offenders}"
    )


def test_private_type_modules_keep_runtime_config_imports_limited() -> None:
    """Only sharing.py may import get_base_url to construct public notebook share URLs."""
    allowed = {"sharing.py": {"get_base_url"}}
    offenders: list[str] = []

    for path in sorted(PRIVATE_TYPES_ROOT.glob("*.py")):
        if path.name == "__init__.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            module = node.module or ""
            imports_env = (node.level == 2 and module == "_env") or (
                node.level == 0 and module == "notebooklm._env"
            )
            if not imports_env:
                continue
            imported_names = {alias.name for alias in node.names}
            if imported_names <= allowed.get(path.name, set()):
                continue
            offenders.append(f"{path.name}: line {node.lineno}: {ast.unparse(node)}")

    assert offenders == [], (
        "Private _types modules must not import notebooklm._env names beyond the allowlist. "
        f"Offenders: {offenders}"
    )


def test_cli_modules_do_not_import_private_type_modules() -> None:
    """CLI code consumes type objects through ``notebooklm.types`` only."""
    offenders: list[str] = []
    for path in _iter_python_files(CLI_ROOT):
        path_offenders = _cli_private_types_import_offenders(path)
        offenders.extend(
            f"{path.relative_to(PROJECT_ROOT)}: {offender}" for offender in path_offenders
        )

    assert offenders == [], (
        "CLI modules must not import notebooklm._types directly; use notebooklm.types. "
        f"Offenders: {offenders}"
    )


def test_public_docs_do_not_recommend_private_module_imports() -> None:
    """User-facing docs should never present private notebooklm modules as imports."""
    offenders = _public_docs_private_import_offenders()

    assert offenders == [], (
        "Public docs must document public notebooklm modules, not private notebooklm._* imports. "
        f"Offenders: {offenders}"
    )
