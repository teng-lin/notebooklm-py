"""AST lint: enforce the CLI -> client/types boundary.

Block-list rules applied to every ``src/notebooklm/cli/**/*.py`` file:

1. No imports of private modules anywhere in the ``notebooklm`` tree:
   any path segment that starts with a single underscore (and is not a
   dunder) is rejected. Catches ``from notebooklm._foo import ...``,
   ``from notebooklm.pkg._bar import ...``, ``from .._foo import ...``,
   ``from notebooklm import _foo``, ``from .. import _foo``, etc.
2. No imports from the RPC layer:
   ``from notebooklm.rpc`` / ``from notebooklm.rpc.<x>`` / ``from ..rpc`` /
   ``from ..rpc.<x>`` / ``import notebooklm.rpc`` / ``from .. import rpc``
   are all rejected. The CLI must consume RPC enums via the public
   ``notebooklm.types`` re-export.
3. No private-name leakage from a public module:
   ``from notebooklm.<public...> import _symbol`` /
   ``from ..<public...> import _symbol`` is rejected when no segment of
   the source path is itself underscored. This stops the CLI from
   reaching into a public module's internals (e.g.
   ``from notebooklm.auth import _internal_helper``). Dunders
   (``__version__``) remain allowed.

Allowed:
- Intra-cli imports (level == 1): ``from ._encoding import ...``, including
  underscored siblings — those are the CLI's own private modules.
- Imports of non-underscored siblings/parents:
  ``from ..types import ...``, ``from ..research import ...``, etc.
"""

from __future__ import annotations

import ast
import pathlib
from collections.abc import Iterator

import pytest

CLI_ROOT = pathlib.Path(__file__).resolve().parents[2] / "src" / "notebooklm" / "cli"
OPTIONS_PATH = CLI_ROOT / "options.py"
SERVICES_ROOT = CLI_ROOT / "services"
RENDERING_PATH = CLI_ROOT / "rendering.py"
CONTEXT_PATH = CLI_ROOT / "context.py"
COMPLETION_CALLBACKS = {
    "_complete_artifacts",
    "_complete_notebooks",
    "_complete_sources",
    "_resolve_notebook_for_completion",
}
COMPLETION_FORBIDDEN_SYMBOLS = {
    "NotebookLMClient",
    "get_auth_tokens",
    "run_async",
}
FUNCTION_DEF_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef)
BLOCK_DEF_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
CLI_COMMAND_MODULES = {
    "agent",
    "artifact",
    "chat",
    "doctor",
    "download",
    "generate",
    "language",
    "note",
    "notebook",
    "profile",
    "research",
    "session",
    "share",
    "skill",
    "source",
}
RENDERING_FORBIDDEN_MODULES = CLI_COMMAND_MODULES | {
    "auth_runtime",
    "completion",
    "context",
    "input",
    "resolve",
    "runtime",
}
CONTEXT_FORBIDDEN_MODULES = CLI_COMMAND_MODULES | {
    "auth_runtime",
    "completion",
    "input",
    "rendering",
    "resolve",
    "runtime",
}


def _is_private_segment(seg: str) -> bool:
    """True if ``seg`` is a single-underscore-prefixed name (not a dunder).

    Empty strings and dunders (``__version__``) are not private.
    """
    return bool(seg) and seg.startswith("_") and not seg.startswith("__")


def _is_dunder_name(name: str) -> bool:
    """True for double-underscore names that are intentionally public."""
    return name.startswith("__") and name.endswith("__")


def _has_private_segment(parts: list[str]) -> bool:
    """True if any segment in ``parts`` is private (per Rule 1)."""
    return any(_is_private_segment(p) for p in parts)


def _is_rpc_path(parts: list[str]) -> bool:
    """True if ``parts`` is the RPC layer or a sub-path (per Rule 2).

    ``parts`` is the path *below* the ``notebooklm`` prefix, e.g.
    ``["rpc"]`` or ``["rpc", "types"]``.
    """
    return bool(parts) and parts[0] == "rpc"


def _cli_module_imports(path: pathlib.Path) -> set[str]:
    """Return direct ``notebooklm.cli`` module imports used by a CLI file."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            mod_parts = mod.split(".") if mod else []
            if node.level == 1:
                if mod:
                    imports.add(mod_parts[0])
                else:
                    imports.update(alias.name.split(".")[0] for alias in node.names)
            elif node.level >= 2 and mod_parts[:1] == ["cli"]:
                if len(mod_parts) > 1:
                    imports.add(mod_parts[1])
                else:
                    imports.update(alias.name.split(".")[0] for alias in node.names)
            elif node.level == 0 and mod_parts[:2] == ["notebooklm", "cli"]:
                if len(mod_parts) > 2:
                    imports.add(mod_parts[2])
                elif len(mod_parts) == 2:
                    imports.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                if parts[:2] == ["notebooklm", "cli"] and len(parts) > 2:
                    imports.add(parts[2])
    return imports


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("from notebooklm.cli import completion\n", {"completion"}),
        ("from notebooklm.cli.runtime import with_client\n", {"runtime"}),
        ("from ..cli import rendering\n", {"rendering"}),
        ("from ..cli.context import get_current_notebook\n", {"context"}),
    ],
)
def test_cli_module_imports_detects_cli_import_forms(
    tmp_path: pathlib.Path, source: str, expected: set[str]
) -> None:
    path = tmp_path / "sample.py"
    path.write_text(source, encoding="utf-8")

    assert _cli_module_imports(path) == expected


def _violations(tree: ast.AST) -> list[str]:  # noqa: C901 - flat dispatch on import shape
    bad: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            mod_parts = mod.split(".") if mod else []

            if node.level == 0:
                # Absolute import: only inspect notebooklm.* roots.
                if mod_parts and mod_parts[0] == "notebooklm":
                    if len(mod_parts) >= 2:
                        sub_parts = mod_parts[1:]
                        # Rule 1 (any private segment) or Rule 2 (rpc layer).
                        if _has_private_segment(sub_parts) or _is_rpc_path(sub_parts):
                            bad.append(f"from {mod} import ...")
                        else:
                            # Rule 3: private-name leakage from a public module.
                            for alias in node.names:
                                if _is_private_segment(alias.name):
                                    bad.append(f"from {mod} import {alias.name}")
                    else:
                        # ``from notebooklm import X`` — inspect each name.
                        # Rule 1 (private name) or Rule 2 (``rpc`` sub-package).
                        for alias in node.names:
                            if _is_private_segment(alias.name) or alias.name == "rpc":
                                bad.append(f"from notebooklm import {alias.name}")
            elif node.level >= 2:
                # Relative parent-package import (cli reaches into notebooklm/*).
                if mod:
                    # Rule 1 (any private segment) or Rule 2 (rpc layer).
                    if _has_private_segment(mod_parts) or _is_rpc_path(mod_parts):
                        bad.append(f"from {'.' * node.level}{mod} import ...")
                        continue
                    # Rule 3: private-name leakage from a public source module.
                    for alias in node.names:
                        if _is_private_segment(alias.name):
                            bad.append(f"from {'.' * node.level}{mod} import {alias.name}")
                else:
                    # ``from .. import X`` — inspect each imported name.
                    # Rule 1 (private name) or Rule 2 (``rpc`` sub-package).
                    for alias in node.names:
                        if _is_private_segment(alias.name) or alias.name == "rpc":
                            bad.append(f"from {'.' * node.level} import {alias.name}")
            else:
                # level == 1 (intra-cli). Inspect ``from . import X`` only for
                # the explicit ``rpc`` name — siblings starting with ``_`` are
                # cli's own private modules and remain allowed.
                if not mod:
                    for alias in node.names:
                        if alias.name == "rpc":
                            bad.append(f"from . import {alias.name}")

        elif isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                if not (len(parts) >= 2 and parts[0] == "notebooklm"):
                    continue
                sub_parts = parts[1:]
                # Rule 1 (any private segment) or Rule 2 (rpc layer).
                if _has_private_segment(sub_parts) or _is_rpc_path(sub_parts):
                    bad.append(f"import {alias.name}")
    return bad


def _iter_block_body_nodes(block: ast.AST) -> Iterator[ast.AST]:
    """Yield nodes from a block signature/body without descending into nested blocks."""

    def walk(node: ast.AST, *, is_root: bool = False) -> Iterator[ast.AST]:
        if not is_root and isinstance(node, BLOCK_DEF_TYPES):
            return
        if not is_root:
            yield node
        for child in ast.iter_child_nodes(node):
            yield from walk(child)

    yield from walk(block, is_root=True)


def _has_forbidden_completion_boundary(parts: list[str]) -> bool:
    """Match forbidden names on exact dotted prefixes or final symbol names."""
    dotted_matches = (
        ".".join(parts[:index]) in COMPLETION_FORBIDDEN_SYMBOLS
        for index in range(1, len(parts) + 1)
    )
    return any(dotted_matches) or bool(parts and parts[-1] in COMPLETION_FORBIDDEN_SYMBOLS)


def _completion_boundary_violations(tree: ast.AST) -> tuple[set[str], list[str]]:
    forbidden_names = set(COMPLETION_FORBIDDEN_SYMBOLS)
    import_offenders: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module_parts = (node.module or "").split(".") if node.module else []
            module_forbidden = _has_forbidden_completion_boundary(module_parts)
            for alias in node.names:
                if _is_dunder_name(alias.name):
                    continue
                if not module_forbidden and alias.name not in COMPLETION_FORBIDDEN_SYMBOLS:
                    continue
                alias_name = alias.asname or alias.name
                forbidden_names.add(alias_name)
                import_offenders.append(f"import: {alias.name}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                if any(_is_dunder_name(part) for part in parts):
                    continue
                if not _has_forbidden_completion_boundary(parts):
                    continue
                if alias.asname:
                    forbidden_names.add(alias.asname)
                elif len(parts) == 1:
                    forbidden_names.add(alias.name)
                import_offenders.append(f"import: {alias.name}")

    check_targets: list[tuple[str, ast.AST]] = [("<module>", tree)]
    for node in ast.walk(tree):
        if isinstance(node, FUNCTION_DEF_TYPES):
            check_targets.append((node.name, node))
        elif isinstance(node, ast.ClassDef):
            check_targets.append((f"class {node.name}", node))

    top_level_functions = {node.name for node in tree.body if isinstance(node, FUNCTION_DEF_TYPES)}
    missing_callbacks = COMPLETION_CALLBACKS - top_level_functions

    offenders = list(import_offenders)
    for context_name, block_node in sorted(check_targets, key=lambda item: item[0]):
        for node in _iter_block_body_nodes(block_node):
            if isinstance(node, ast.Name) and node.id in forbidden_names:
                offenders.append(f"{context_name}: {node.id}")
            elif (
                isinstance(node, ast.Attribute)
                and node.attr in COMPLETION_FORBIDDEN_SYMBOLS
                and not _is_dunder_name(node.attr)
            ):
                offenders.append(f"{context_name}: .{node.attr}")

    return missing_callbacks, offenders


def test_no_private_module_imports_in_cli():
    offenders: list[tuple[str, list[str]]] = []
    for path in sorted(CLI_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        bad = _violations(tree)
        if bad:
            offenders.append((str(path.relative_to(CLI_ROOT.parent)), bad))
    assert not offenders, (
        "CLI must not import notebooklm._* (private modules), notebooklm.rpc.*, "
        "or `_private` names out of public notebooklm modules. "
        "Promote needed symbols to a public module (config/urls/log/research/types) "
        f"and import from there.\nOffenders: {offenders}"
    )


def test_cli_services_stay_on_public_library_boundary() -> None:
    """Keep service-layer modules from binding directly to private library/RPC APIs."""
    offenders: list[tuple[str, list[str]]] = []
    for path in sorted(SERVICES_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        bad = _violations(tree)
        if bad:
            offenders.append((str(path.relative_to(CLI_ROOT.parent)), bad))

    assert not offenders, (
        "notebooklm.cli.services.* must not import notebooklm._* private modules, "
        "notebooklm.rpc.*, or `_private` names from public notebooklm modules. "
        "Route service collaborators through public library APIs or CLI facades.\n"
        f"Offenders: {offenders}"
    )


def test_rendering_stays_on_low_level_cli_import_boundary() -> None:
    imports = _cli_module_imports(RENDERING_PATH)

    assert not (imports & RENDERING_FORBIDDEN_MODULES), (
        "cli.rendering must not import runtime/auth/context/resolve/input/completion "
        f"or command modules. Offenders: {sorted(imports & RENDERING_FORBIDDEN_MODULES)}"
    )


def test_context_stays_on_low_level_cli_import_boundary() -> None:
    imports = _cli_module_imports(CONTEXT_PATH)

    assert not (imports & CONTEXT_FORBIDDEN_MODULES), (
        "cli.context must not import runtime/auth/rendering/resolve/input/completion "
        "or command modules. "
        f"Offenders: {sorted(imports & CONTEXT_FORBIDDEN_MODULES)}"
    )


def test_options_completion_callbacks_stay_on_completion_provider_boundary() -> None:
    """Keep live completion auth/client/runtime work out of ``cli.options``."""
    tree = ast.parse(OPTIONS_PATH.read_text(encoding="utf-8"))
    missing_callbacks, offenders = _completion_boundary_violations(tree)
    assert not missing_callbacks, (
        "Expected top-level completion callbacks missing from cli.options: "
        f"{sorted(missing_callbacks)}"
    )

    assert not offenders, (
        "cli.options must delegate completion live auth/client/runtime work to "
        "cli.completion instead of constructing clients, loading auth, or running "
        f"async work directly: {offenders}"
    )


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "from notebooklm import NotebookLMClient as Client\n"
            "def _complete_artifacts():\n"
            "    return Client\n",
            ["import: NotebookLMClient", "_complete_artifacts: Client"],
        ),
        (
            "import run_async as runner\ndef _complete_notebooks():\n    return runner\n",
            ["import: run_async", "_complete_notebooks: runner"],
        ),
        (
            "import pkg.run_async\ndef _complete_sources():\n    return None\n",
            ["import: pkg.run_async"],
        ),
        (
            "from .runtime import run_async as runner\n"
            "def _complete_sources():\n"
            "    return runner\n",
            ["import: run_async", "_complete_sources: runner"],
        ),
        (
            "class Provider:\n"
            "    client = NotebookLMClient\n"
            "def _resolve_notebook_for_completion():\n"
            "    return None\n",
            ["class Provider: NotebookLMClient"],
        ),
        (
            "@run_async\n"
            "def _complete_artifacts(client: NotebookLMClient = None) -> NotebookLMClient:\n"
            "    return client\n",
            [
                "_complete_artifacts: run_async",
                "_complete_artifacts: NotebookLMClient",
            ],
        ),
        (
            "class Provider(NotebookLMClient):\n"
            "    pass\n"
            "def _resolve_notebook_for_completion():\n"
            "    return None\n",
            ["class Provider: NotebookLMClient"],
        ),
    ],
)
def test_completion_boundary_detects_import_and_block_shapes(
    source: str, expected: list[str]
) -> None:
    """Self-check the AST guardrail paths used by the live options.py test."""
    _, offenders = _completion_boundary_violations(ast.parse(source))

    assert set(offenders) == set(expected), f"Expected {expected}, got {offenders}"


def test_completion_boundary_reports_missing_callbacks() -> None:
    missing_callbacks, _ = _completion_boundary_violations(ast.parse(""))

    assert missing_callbacks == COMPLETION_CALLBACKS


def test_completion_boundary_ignores_dunder_imports() -> None:
    _, offenders = _completion_boundary_violations(
        ast.parse("from notebooklm import __version__\n")
    )

    assert offenders == []


def test_completion_boundary_allows_standard_library_and_safe_relative_imports() -> None:
    _, offenders = _completion_boundary_violations(
        ast.parse("import pathlib\nfrom collections import abc\nfrom .completion import complete\n")
    )

    assert offenders == []


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "from notebooklm._auth.tokens import AuthTokens",
            "from notebooklm._auth.tokens import ...",
        ),
        ("import notebooklm._auth.tokens", "import notebooklm._auth.tokens"),
        ("from .._auth.tokens import AuthTokens", "from .._auth.tokens import ..."),
        ("from .. import _auth", "from .. import _auth"),
    ],
)
def test_cli_boundary_blocks_auth_internal_import_shapes(source: str, expected: str) -> None:
    """CLI auth imports must stay on notebooklm.auth, even if internals move to _auth."""
    assert expected in _violations(ast.parse(source))
