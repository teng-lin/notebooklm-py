"""AST lint: enforce the layered boundary of ``notebooklm._app``.

``_app`` is the shared business-logic layer consumed by every transport
adapter (the Click CLI, the FastMCP server, future HTTP). Its contract is two
fold: it must stay transport-neutral AND it must depend only on the **public**
``notebooklm`` surface (plus intra-``_app`` siblings). It must never reach
sideways/downward into a runtime-internal layer. This guardrail walks every
``src/notebooklm/_app/**/*.py`` file and rejects an import of:

* a forbidden *external* transport package — ``click`` / ``rich`` / ``fastmcp``
  (and any ``*.<submodule>``), or
* a forbidden ``notebooklm`` *sibling* sub-target, via absolute
  (``import notebooklm.X...`` / ``from notebooklm.X... import``) or relative
  (``from ..X import`` / ``from ..X.y import`` / ``from .. import X``) forms,
  where ``X`` is:

  - ``cli`` — the transport adapter (``notebooklm.cli.*``),
  - ``rpc`` — the batchexecute runtime layer (``notebooklm.rpc.*``); ``_app``
    must consume RPC enums through their public ``notebooklm.types`` re-export,
    not by reaching into ``rpc.types`` directly, or
  - any *private* sibling whose name starts with ``_`` (``notebooklm._kernel``,
    ``notebooklm._runtime``, ``notebooklm._middleware``,
    ``notebooklm._rpc_executor``, ``notebooklm._auth``, …) — the client
    runtime internals. ``_app`` may only depend on the public facade.

``_app``'s *own* package is ``notebooklm._app``; intra-``_app`` imports are
**relative within ``_app``** (e.g. ``from .events import ...`` /
``from .errors import ...``) and resolve at a shallower level than the ``..``
that points at ``notebooklm``, so they are never flagged. Only a relative import
whose ``..`` resolves to the ``notebooklm`` package *and* targets a forbidden
sibling is a violation.

The walk is a full ``ast.walk`` over *all* import statements, so an import
hidden inside ``if TYPE_CHECKING:`` (or any other block) is still caught —
even a type-only ``click`` (or ``rpc.types``) import would couple ``_app`` to a
layer it must not depend on.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

APP_ROOT = pathlib.Path(__file__).resolve().parents[2] / "src" / "notebooklm" / "_app"

# Forbidden top-level *external* package roots. ``fastapi`` / ``uvicorn`` /
# ``starlette`` are the REST adapter's framework — ``_app`` must never import
# them (same posture as ``fastmcp`` for the MCP adapter).
FORBIDDEN_EXTERNAL_ROOTS = {"click", "rich", "fastmcp", "fastapi", "uvicorn", "starlette"}

# Forbidden non-private ``notebooklm`` siblings. ``cli`` is the Click transport
# adapter; ``server`` is the REST transport adapter; ``rpc`` is the batchexecute
# runtime layer (consume its enums via the public ``notebooklm.types``
# re-export instead). Private ``_*`` siblings are caught separately by
# :func:`_is_forbidden_notebooklm_child`.
FORBIDDEN_NOTEBOOKLM_CHILDREN = {"cli", "server", "rpc"}


def _is_forbidden_external(parts: list[str]) -> bool:
    """True if a dotted module path's root is a forbidden external package."""
    return bool(parts) and parts[0] in FORBIDDEN_EXTERNAL_ROOTS


def _is_forbidden_notebooklm_child(child: str) -> bool:
    """True if ``child`` is a forbidden ``notebooklm`` sibling for ``_app``.

    Forbidden siblings are the explicit transport/runtime layers
    (``cli`` / ``rpc``) plus every private ``_*`` runtime-internal module or
    package (``_kernel``, ``_runtime``, ``_middleware``, ``_rpc_executor``,
    ``_auth``, …). ``_app`` is allowed to import only the *public* surface
    (``exceptions`` / ``types`` / ``client`` / ``urls`` / ``auth`` /
    ``migration`` / ``artifacts`` / …, none of which start with ``_``), so any
    underscore-prefixed sibling is rejected.
    """
    if not child:
        return False
    if child in FORBIDDEN_NOTEBOOKLM_CHILDREN:
        return True
    # Public dunder attributes of the package (``__version__``, …) are part of
    # the public surface, not a private runtime module — allow them.
    if child.startswith("__"):
        return False
    # ``notebooklm._app`` is the package under audit itself: an *absolute*
    # intra-_app import (``import notebooklm._app.events`` / ``from
    # notebooklm._app.events import X``) is allowed, exactly like the relative
    # ``from .events import ...`` form. Only a *sideways* reach into a different
    # ``notebooklm._*`` runtime internal is forbidden.
    if child == "_app":
        return False
    return child.startswith("_")


def _is_forbidden_notebooklm_path(parts: list[str]) -> bool:
    """True if ``parts`` is ``notebooklm.<forbidden-child>`` or a sub-path."""
    return (
        parts[:1] == ["notebooklm"] and len(parts) >= 2 and _is_forbidden_notebooklm_child(parts[1])
    )


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
                if _is_forbidden_external(parts) or _is_forbidden_notebooklm_path(parts):
                    bad.append(f"import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            mod_parts = mod.split(".") if mod else []
            if node.level == 0:
                # Absolute import.
                if _is_forbidden_external(mod_parts) or _is_forbidden_notebooklm_path(mod_parts):
                    bad.append(f"from {mod} import ...")
                elif mod_parts == ["notebooklm"]:
                    bad.extend(
                        f"from notebooklm import {alias.name}"
                        for alias in node.names
                        if _is_forbidden_notebooklm_child(alias.name)
                    )
            elif node.level == notebooklm_level:
                # Relative import whose ``..`` resolves to ``notebooklm``.
                if mod_parts and _is_forbidden_notebooklm_child(mod_parts[0]):
                    bad.append(f"from {'.' * node.level}{mod} import ...")
                elif not mod:
                    # ``from .. import X`` — X is a notebooklm sibling.
                    bad.extend(
                        f"from {'.' * node.level} import {alias.name}"
                        for alias in node.names
                        if _is_forbidden_notebooklm_child(alias.name)
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
        "notebooklm._app must depend only on the public notebooklm surface "
        "(+ intra-_app): no imports of click, rich, fastmcp, notebooklm.cli.*, "
        "notebooklm.rpc.*, or any private notebooklm._* runtime sibling (even "
        "under TYPE_CHECKING). Consume RPC enums via their notebooklm.types "
        "re-export; move transport-specific code into the adapter (cli/ or mcp/).\n"
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
        "import fastapi\n",
        "from fastapi import FastAPI\n",
        "import uvicorn\n",
        "import starlette.responses\n",
        "from starlette.responses import JSONResponse\n",
        "import notebooklm.server\n",
        "from notebooklm.server import create_app\n",
        "from notebooklm import server\n",
        "import notebooklm.cli\n",
        "import notebooklm.cli.error_handler\n",
        "from notebooklm.cli import error_handler\n",
        "from notebooklm.cli.resolve import validate_id\n",
        "from notebooklm import cli\n",
        # rpc runtime layer — consume enums via notebooklm.types instead.
        "import notebooklm.rpc\n",
        "import notebooklm.rpc.types\n",
        "from notebooklm.rpc import RPCMethod\n",
        "from notebooklm.rpc.types import ChatGoal\n",
        "from notebooklm import rpc\n",
        # private runtime-internal siblings.
        "import notebooklm._kernel\n",
        "from notebooklm._kernel import Kernel\n",
        "from notebooklm._runtime.config import Config\n",
        "from notebooklm._rpc_executor import execute\n",
        "from notebooklm._middleware import retry\n",
        "from notebooklm import _kernel\n",
        "if False:\n    import click\n",  # block-nested still flagged
        "if False:\n    from notebooklm.rpc.types import ChatGoal\n",
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
        # rpc runtime layer (the historical evadable seam, issue #1493).
        ("from ..rpc import RPCMethod\n", ("serialize.py",)),
        ("from ..rpc.types import ChatGoal, ChatResponseLength\n", ("serialize.py",)),
        ("from .. import rpc\n", ("serialize.py",)),
        # private runtime-internal siblings.
        ("from .._kernel import Kernel\n", ("serialize.py",)),
        ("from .._runtime.config import Config\n", ("serialize.py",)),
        ("from .._rpc_executor import execute\n", ("serialize.py",)),
        ("from .. import _kernel\n", ("serialize.py",)),
        # Nested file: ``notebooklm`` is one level deeper.
        ("from ...cli import error_handler\n", ("sub", "mod.py")),
        ("from ... import cli\n", ("sub", "mod.py")),
        ("from ...rpc.types import ChatGoal\n", ("sub", "mod.py")),
        ("from ..._kernel import Kernel\n", ("sub", "mod.py")),
    ],
)
def test_matcher_flags_forbidden_relative_imports(
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
        "from ..types import ChatGoal, ChatResponseLength\n",  # public re-export — allowed
        "from ..artifacts import retry_artifact\n",  # public sibling — allowed
        "from .. import artifacts\n",  # public sibling — allowed
        "from .. import __version__\n",  # public package attr — allowed
        "from .errors import classify\n",  # intra-_app (relative) — allowed
        "from .events import ProgressSink\n",  # intra-_app (relative) — allowed
        "import notebooklm._app.events\n",  # intra-_app (absolute) — allowed (#1493)
        "from notebooklm._app.events import ProgressSink\n",  # intra-_app (absolute) — allowed (#1493)
        "from notebooklm.exceptions import NotebookLMError\n",  # public — allowed
        "from notebooklm.types import Notebook\n",  # public — allowed
        "from notebooklm.urls import is_youtube_url\n",  # public — allowed
    ],
)
def test_matcher_allows_neutral_imports(source: str) -> None:
    assert _boundary_violations(ast.parse(source), ("serialize.py",)) == []
