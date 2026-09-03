"""Meta-lint: the lazy client open must resolve *inside* the MCP error boundary.

The MCP client is opened lazily (#2330), so ``await get_client(ctx)`` can now
raise a real auth/network error — where the old synchronous accessor could only
raise ``RuntimeError`` for a missing lifespan. A call left *above* the
``with mcp_errors():`` block therefore escapes as a raw exception and FastMCP
reports it as an opaque internal error, losing the structured
``{code, message, retriable, hint}`` contract every tool advertises — which is
precisely the opacity #2330 set out to remove. The original fix shipped 33 such
call sites; this gate is why they cannot come back.

Two rules, over every module under ``mcp/``:

* ``get_client`` — the call must be lexically inside a ``with mcp_errors():``
  in the *same* function (a nested ``def`` resets the scope: its body runs after
  the enclosing ``with`` has exited).
* ``get_client_from_app`` — the ``/files/*`` routes have no ``mcp_errors``; the
  call must instead sit in a ``try`` whose handler catches ``Exception`` (a
  narrower ``except RuntimeError`` is what this rule exists to catch).

Both rules resolve the *binding* names actually imported in each module, so an
``import ... as`` alias or an attribute spelling (``_context.get_client(ctx)``)
is covered rather than silently passing.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.repo_lint

REPO_ROOT = Path(__file__).resolve().parents[2]
MCP_DIR = REPO_ROOT / "src" / "notebooklm" / "mcp"

_CLIENT_ACCESSOR = "get_client"
_APP_ACCESSOR = "get_client_from_app"
_BOUNDARY = "mcp_errors"

#: Functions whose caller supplies the boundary. Each entry must name a *helper*
#: (not an ``@mcp.tool``) whose every call site is itself inside ``mcp_errors``.
#: Shrink-only: removing an entry is fine, adding one needs the same proof.
_WRAPPED_BY_CALLER = {
    # meta.py::server_info calls this from inside its own ``with mcp_errors():``.
    ("tools/meta.py", "_account_block"),
}


def _bound_names(tree: ast.AST, target: str) -> set[str]:
    """Local names ``target`` is reachable through: direct, aliased, or module-qualified."""
    names = {target}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == target and alias.asname:
                    names.add(alias.asname)
    return names


def _is_call_to(node: ast.AST, names: set[str]) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id in names
    if isinstance(func, ast.Attribute):  # e.g. ``_context.get_client(ctx)``
        return func.attr in names
    return False


def _guarded_by_except_exception(node: ast.Try) -> bool:
    for handler in node.handlers:
        if handler.type is None:  # bare ``except:``
            return True
        candidates = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
        for exc in candidates:
            if isinstance(exc, ast.Name) and exc.id in {"Exception", "BaseException"}:
                return True
    return False


def _violations(source: str, rel: str) -> list[str]:
    """Report accessor calls that sit outside their required guard."""
    tree = ast.parse(source)
    client_names = _bound_names(tree, _CLIENT_ACCESSOR)
    app_names = _bound_names(tree, _APP_ACCESSOR)
    boundary_names = _bound_names(tree, _BOUNDARY)
    found: list[str] = []

    def walk(node: ast.AST, *, in_boundary: bool, in_try: bool, func: str) -> None:
        if isinstance(node, ast.Try):
            # ONLY ``Try.body`` is covered by the handlers. ``orelse`` runs after the
            # body succeeded and ``finalbody`` during unwinding — an exception from
            # either escapes this ``except`` entirely, so protection must not leak
            # into them (nor into ``handlers``, where a raise also escapes).
            guarded = in_try or _guarded_by_except_exception(node)
            for stmt in node.body:
                walk(stmt, in_boundary=in_boundary, in_try=guarded, func=func)
            for branch in (node.handlers, node.orelse, node.finalbody):
                for stmt in branch:
                    walk(stmt, in_boundary=in_boundary, in_try=in_try, func=func)
            return

        for child in ast.iter_child_nodes(node):
            child_boundary, child_try, child_func = in_boundary, in_try, func
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # A nested body runs after the enclosing ``with`` / ``try`` exits.
                child_boundary, child_try, child_func = False, False, child.name
            elif isinstance(child, (ast.With, ast.AsyncWith)):
                if any(_is_call_to(item.context_expr, boundary_names) for item in child.items):
                    child_boundary = True
            elif _is_call_to(child, client_names) and not in_boundary:
                if (rel, func) not in _WRAPPED_BY_CALLER:
                    found.append(f"{rel}:{child.lineno} {func}() — outside `with mcp_errors():`")
            elif _is_call_to(child, app_names) and not in_try:
                found.append(f"{rel}:{child.lineno} {func}() — outside `except Exception`")
            walk(child, in_boundary=child_boundary, in_try=child_try, func=child_func)

    walk(tree, in_boundary=False, in_try=False, func="<module>")
    return found


# --------------------------------------------------------------------------- #
# The real gate
# --------------------------------------------------------------------------- #
def test_every_lazy_client_call_sits_inside_its_guard() -> None:
    found: list[str] = []
    for path in sorted(MCP_DIR.rglob("*.py")):
        found += _violations(path.read_text(encoding="utf-8"), str(path.relative_to(MCP_DIR)))
    assert not found, "lazy client open escaping its error boundary (#2330):\n" + "\n".join(found)


def test_the_gate_actually_covers_the_call_sites() -> None:
    """Non-vacuity: the scan must FIND the accessor, not pass on an empty sweep."""
    total = sum(
        path.read_text(encoding="utf-8").count(f"await {_CLIENT_ACCESSOR}(")
        for path in MCP_DIR.rglob("*.py")
    )
    assert total >= 30, f"expected the tool surface to still call the accessor, saw {total}"


def test_allowlist_entries_all_exist() -> None:
    """Shrink-only guard: a stale allowlist entry must not silently excuse nothing."""
    for rel, func in _WRAPPED_BY_CALLER:
        source = (MCP_DIR / rel).read_text(encoding="utf-8")
        assert f"def {func}(" in source, f"allowlist names a missing function: {rel}::{func}"


# --------------------------------------------------------------------------- #
# Injection self-checks — prove the detector bites, in every spelling
# --------------------------------------------------------------------------- #
_OUTSIDE = """
from ._context import get_client
from ._errors import mcp_errors

async def tool(ctx):
    client = await get_client(ctx)
    with mcp_errors():
        return await client.notebooks.list()
"""

_INSIDE = """
from ._context import get_client
from ._errors import mcp_errors

async def tool(ctx):
    with mcp_errors():
        client = await get_client(ctx)
        return await client.notebooks.list()
"""

_ALIASED = """
from ._context import get_client as fetch_client
from ._errors import mcp_errors

async def tool(ctx):
    client = await fetch_client(ctx)
    with mcp_errors():
        return client
"""

_ATTRIBUTE = """
from . import _context
from ._errors import mcp_errors

async def tool(ctx):
    client = await _context.get_client(ctx)
    with mcp_errors():
        return client
"""

_NESTED_DEF = """
from ._context import get_client
from ._errors import mcp_errors

def register(mcp):
    with mcp_errors():
        async def tool(ctx):
            return await get_client(ctx)
"""

_ROUTE_NARROW = """
from ._context import get_client_from_app

async def route(request):
    try:
        client = await get_client_from_app(request)
    except RuntimeError:
        return None
    return client
"""

_ROUTE_WIDE = """
from ._context import get_client_from_app

async def route(request):
    try:
        client = await get_client_from_app(request)
    except Exception:
        return None
    return client
"""


_ROUTE_ELSE = """
from ._context import get_client_from_app

async def route(request):
    try:
        pass
    except Exception:
        return None
    else:
        return await get_client_from_app(request)
"""

_ROUTE_FINALLY = """
from ._context import get_client_from_app

async def route(request):
    try:
        pass
    except Exception:
        return None
    finally:
        await get_client_from_app(request)
"""

_ROUTE_HANDLER = """
from ._context import get_client_from_app

async def route(request):
    try:
        pass
    except Exception:
        return await get_client_from_app(request)
"""

_TOOL_TRY_DOES_NOT_SUBSTITUTE = """
from ._context import get_client
from ._errors import mcp_errors

async def tool(ctx):
    try:
        client = await get_client(ctx)
    except Exception:
        raise
    with mcp_errors():
        return client
"""


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param(_OUTSIDE, True, id="call-above-the-boundary-is-caught"),
        pytest.param(_INSIDE, False, id="call-inside-the-boundary-is-clean"),
        pytest.param(_ALIASED, True, id="aliased-import-cannot-bypass"),
        pytest.param(_ATTRIBUTE, True, id="attribute-spelling-cannot-bypass"),
        pytest.param(_NESTED_DEF, True, id="enclosing-with-does-not-cover-a-nested-def"),
        pytest.param(_ROUTE_NARROW, True, id="except-RuntimeError-is-too-narrow"),
        pytest.param(_ROUTE_WIDE, False, id="except-Exception-is-accepted"),
        pytest.param(_ROUTE_ELSE, True, id="try-else-is-not-covered-by-the-handler"),
        pytest.param(_ROUTE_FINALLY, True, id="try-finally-is-not-covered-by-the-handler"),
        pytest.param(_ROUTE_HANDLER, True, id="the-handler-suite-is-not-covered-by-itself"),
        pytest.param(
            _TOOL_TRY_DOES_NOT_SUBSTITUTE,
            True,
            id="a-try-block-is-not-a-substitute-for-mcp_errors",
        ),
    ],
)
def test_detector_self_checks(source: str, expected: bool) -> None:
    assert bool(_violations(source, "synthetic.py")) is expected
