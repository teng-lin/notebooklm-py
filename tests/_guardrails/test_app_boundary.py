"""AST lint: enforce the transport-neutral boundary of ``notebooklm._app``.

``_app`` is the shared business-logic layer consumed by every transport
adapter (the Click CLI, the FastMCP server, future HTTP). To stay reusable it
MUST NOT import any transport dependency. This guardrail walks every
``src/notebooklm/_app/**/*.py`` file and rejects an import of:

* ``click`` (and any ``click.*`` submodule),
* ``rich`` (and any ``rich.*`` submodule),
* ``notebooklm.cli`` (and any ``notebooklm.cli.*`` submodule), via absolute
  (``import notebooklm.cli...`` / ``from notebooklm.cli... import``) or
  relative (``from ..cli import`` / ``from ..cli.x import``) forms, and
* ``fastmcp`` (and any ``fastmcp.*`` submodule).

The walk is a full ``ast.walk`` over *all* import statements, so an import
hidden inside ``if TYPE_CHECKING:`` (or any other block) is still caught —
even a type-only ``click`` import would couple ``_app`` to the CLI surface.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

APP_ROOT = pathlib.Path(__file__).resolve().parents[2] / "src" / "notebooklm" / "_app"

# Forbidden top-level *external* package roots.
FORBIDDEN_EXTERNAL_ROOTS = {"click", "rich", "fastmcp"}


def _is_forbidden_external(parts: list[str]) -> bool:
    """True if a dotted module path's root is a forbidden external package."""
    return bool(parts) and parts[0] in FORBIDDEN_EXTERNAL_ROOTS


def _is_notebooklm_cli(parts: list[str]) -> bool:
    """True if ``parts`` (below no prefix) is ``notebooklm.cli`` or a sub-path."""
    return parts[:2] == ["notebooklm", "cli"]


def _app_package_level(relative_parts: tuple[str, ...]) -> int:
    """Relative ``level`` at which ``..`` points at the ``notebooklm`` package.

    A module ``_app/serialize.py`` has ``relative_parts == ("serialize.py",)``
    so ``level == 1`` is the ``_app`` package and ``level == 2`` is
    ``notebooklm`` — i.e. ``from ..cli import x``. A nested
    ``_app/sub/mod.py`` shifts that by its directory depth.
    """
    # Number of directory segments between the file and the _app package root.
    dir_depth = len(relative_parts) - 1
    # level == dir_depth + 1 -> _app; +2 -> notebooklm.
    return dir_depth + 2


def _boundary_violations(tree: ast.AST, relative_parts: tuple[str, ...]) -> list[str]:
    """Return human-readable descriptions of every boundary-violating import."""
    bad: list[str] = []
    notebooklm_level = _app_package_level(relative_parts)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                if _is_forbidden_external(parts) or _is_notebooklm_cli(parts):
                    bad.append(f"import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            mod_parts = mod.split(".") if mod else []
            if node.level == 0:
                # Absolute import.
                if _is_forbidden_external(mod_parts) or _is_notebooklm_cli(mod_parts):
                    bad.append(f"from {mod} import ...")
                elif mod_parts == ["notebooklm"]:
                    bad.extend(
                        f"from notebooklm import {alias.name}"
                        for alias in node.names
                        if alias.name == "cli"
                    )
            else:
                # Relative import. Resolve the form against ``notebooklm.cli``.
                if node.level == notebooklm_level and mod_parts[:1] == ["cli"]:
                    bad.append(f"from {'.' * node.level}{mod} import ...")
                elif node.level == notebooklm_level and not mod:
                    bad.extend(
                        f"from {'.' * node.level} import {alias.name}"
                        for alias in node.names
                        if alias.name == "cli"
                    )
    return bad


def test_app_has_no_transport_dependency_imports() -> None:
    offenders: list[tuple[str, list[str]]] = []
    for path in sorted(APP_ROOT.rglob("*.py")):
        relative_parts = path.relative_to(APP_ROOT).parts
        tree = ast.parse(path.read_text(encoding="utf-8"))
        bad = _boundary_violations(tree, relative_parts)
        if bad:
            offenders.append((str(path.relative_to(APP_ROOT.parent.parent)), bad))

    assert not offenders, (
        "notebooklm._app must stay transport-neutral: no imports of click, rich, "
        "notebooklm.cli.*, or fastmcp (even under TYPE_CHECKING). Move "
        "transport-specific code into the adapter (cli/ or mcp/).\n"
        f"Offenders: {offenders}"
    )


# --- self-checks for the AST matcher ---------------------------------------


@pytest.mark.parametrize(
    "source",
    [
        "import click\n",
        "import click.testing\n",
        "from click import echo\n",
        "from click.testing import CliRunner\n",
        "import rich\n",
        "from rich.console import Console\n",
        "import fastmcp\n",
        "from fastmcp import FastMCP\n",
        "import notebooklm.cli\n",
        "import notebooklm.cli.error_handler\n",
        "from notebooklm.cli import error_handler\n",
        "from notebooklm.cli.resolve import validate_id\n",
        "from notebooklm import cli\n",
        "if False:\n    import click\n",  # block-nested still flagged
    ],
)
def test_matcher_flags_forbidden_absolute_imports(source: str) -> None:
    assert _boundary_violations(ast.parse(source), ("serialize.py",))


@pytest.mark.parametrize(
    ("source", "relative_parts"),
    [
        ("from ..cli import error_handler\n", ("serialize.py",)),
        ("from ..cli.resolve import validate_id\n", ("serialize.py",)),
        ("from .. import cli\n", ("serialize.py",)),
        # Nested file: ``notebooklm`` is one level deeper.
        ("from ...cli import error_handler\n", ("sub", "mod.py")),
        ("from ... import cli\n", ("sub", "mod.py")),
    ],
)
def test_matcher_flags_forbidden_relative_cli_imports(
    source: str, relative_parts: tuple[str, ...]
) -> None:
    assert _boundary_violations(ast.parse(source), relative_parts)


@pytest.mark.parametrize(
    "source",
    [
        "from __future__ import annotations\n",
        "import dataclasses\n",
        "from datetime import date\n",
        "from ..exceptions import ValidationError\n",  # public sibling — allowed
        "from .errors import classify\n",  # intra-_app — allowed
        "from notebooklm.exceptions import NotebookLMError\n",  # public — allowed
        "from notebooklm.types import Notebook\n",  # public — allowed
    ],
)
def test_matcher_allows_neutral_imports(source: str) -> None:
    assert _boundary_violations(ast.parse(source), ("serialize.py",)) == []
