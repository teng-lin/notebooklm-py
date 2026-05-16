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

import pytest

CLI_ROOT = pathlib.Path(__file__).resolve().parents[2] / "src" / "notebooklm" / "cli"


def _is_private_segment(seg: str) -> bool:
    """True if ``seg`` is a single-underscore-prefixed name (not a dunder).

    Empty strings and dunders (``__version__``) are not private.
    """
    return bool(seg) and seg.startswith("_") and not seg.startswith("__")


def _has_private_segment(parts: list[str]) -> bool:
    """True if any segment in ``parts`` is private (per Rule 1)."""
    return any(_is_private_segment(p) for p in parts)


def _is_rpc_path(parts: list[str]) -> bool:
    """True if ``parts`` is the RPC layer or a sub-path (per Rule 2).

    ``parts`` is the path *below* the ``notebooklm`` prefix, e.g.
    ``["rpc"]`` or ``["rpc", "types"]``.
    """
    return bool(parts) and parts[0] == "rpc"


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
