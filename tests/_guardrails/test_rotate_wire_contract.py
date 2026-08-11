"""Fail-closed ownership guard for the single ``RotateCookies`` wire contract.

Phase 11C moves the raw URL/body/POST pair to dependency-bottom
``_auth/mint_service.py``. Stateful ``keepalive.py`` retains policy and exact
compatibility bindings; PSIDTS recovery continues consuming those bindings.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

pytestmark = pytest.mark.repo_lint

_SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "notebooklm"
_AUTH_ROOT = _SRC_ROOT / "_auth"
_WIRE_OWNER = _AUTH_ROOT / "mint_service.py"
_KEEPALIVE_PATH = _AUTH_ROOT / "keepalive.py"
_PSIDTS_PATH = _AUTH_ROOT / "psidts_recovery.py"
_WIRE_MODULE = "notebooklm._auth.mint_service"

_ROTATE_URL_LITERAL = "https://accounts.google.com/RotateCookies"
_ROTATE_BODY_LITERAL = '[000,"-0000000000000000000"]'
_WIRE_FUNCTIONS = frozenset({"_rotate_post", "_rotate_post_sync"})
_REEXPORTS = (
    "KEEPALIVE_ROTATE_URL",
    "_KEEPALIVE_ROTATE_HEADERS",
    "_KEEPALIVE_ROTATE_BODY",
    "_KEEPALIVE_POKE_TIMEOUT",
    "_ROTATE_POST_KWARGS",
    "_rotate_post",
    "_rotate_post_sync",
)


def _iter_src_modules(root: Path = _SRC_ROOT) -> list[Path]:
    return sorted(root.rglob("*.py"))


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _static_string(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _static_string(node.left)
        right = _static_string(node.right)
        return left + right if left is not None and right is not None else None
    if isinstance(node, ast.JoinedStr):
        pieces = [_static_string(item) for item in node.values]
        return (
            "".join(piece for piece in pieces if piece is not None) if None not in pieces else None
        )
    return None


def _source_package(path: Path, src_root: Path) -> tuple[str, ...]:
    relative = path.relative_to(src_root)
    return ("notebooklm", *relative.parent.parts)


def _resolve_import_from(node: ast.ImportFrom, *, path: Path, src_root: Path) -> str | None:
    if node.level == 0:
        return node.module or ""
    package = _source_package(path, src_root)
    parents = node.level - 1
    if parents >= len(package):
        return None
    prefix = package[: len(package) - parents]
    suffix = tuple((node.module or "").split(".")) if node.module else ()
    return ".".join((*prefix, *suffix))


def _bound_name(node: ast.AST, bindings: dict[str, str]) -> str | None:
    if isinstance(node, ast.Name):
        return bindings.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        prefix = _bound_name(node.value, bindings)
        return f"{prefix}.{node.attr}" if prefix else None
    return None


def _expression_origin(node: ast.AST, bindings: dict[str, str]) -> str | None:
    origin = _bound_name(node, bindings)
    if origin is not None:
        return origin
    if isinstance(node, ast.Call):
        callable_name = _bound_name(node.func, bindings)
        if callable_name in {"importlib.import_module", "__import__"} and node.args:
            return _static_string(node.args[0])
        if callable_name == "getattr" and len(node.args) >= 2:
            owner = _expression_origin(node.args[0], bindings)
            member = _static_string(node.args[1])
            if owner is not None and member is not None:
                return f"{owner}.{member}"
    return None


def _bind_target(target: ast.AST, value: ast.AST, bindings: dict[str, str]) -> bool:
    changed = False
    if isinstance(target, ast.Name):
        origin = _expression_origin(value, bindings)
        if origin is not None and target.id not in bindings:
            bindings[target.id] = origin
            changed = True
    elif (
        isinstance(target, ast.Tuple | ast.List)
        and isinstance(value, ast.Tuple | ast.List)
        and len(target.elts) == len(value.elts)
    ):
        for child_target, child_value in zip(target.elts, value.elts, strict=True):
            changed |= _bind_target(child_target, child_value, bindings)
    return changed


def _bindings(tree: ast.Module, *, path: Path, src_root: Path) -> dict[str, str]:
    """Resolve imports and aliases conservatively despite later rebinding."""
    bindings: dict[str, str] = {}
    module_name = ".".join(("notebooklm", *path.relative_to(src_root).with_suffix("").parts))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                bound = item.asname or item.name.split(".", 1)[0]
                bindings.setdefault(bound, item.name if item.asname else bound)
        elif isinstance(node, ast.ImportFrom):
            module = _resolve_import_from(node, path=path, src_root=src_root)
            if module is None:
                continue
            for item in node.names:
                if item.name != "*":
                    bindings.setdefault(item.asname or item.name, f"{module}.{item.name}")
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            bindings.setdefault(node.name, f"{module_name}.{node.name}")

    assignments: list[tuple[ast.AST, ast.AST]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            assignments.extend((target, node.value) for target in node.targets)
        elif (isinstance(node, ast.AnnAssign) and node.value is not None) or isinstance(
            node, ast.NamedExpr
        ):
            assignments.append((node.target, node.value))
    for _ in range(len(assignments) + 1):
        changed = False
        for target, value in assignments:
            changed |= _bind_target(target, value, bindings)
        if not changed:
            break
    return bindings


def _files_with_constant(value: str) -> list[Path]:
    hits: list[Path] = []
    for path in _iter_src_modules():
        if any(
            isinstance(node, ast.Constant) and node.value == value for node in ast.walk(_tree(path))
        ):
            hits.append(path)
    return hits


def _is_rotate_url(node: ast.AST, bindings: dict[str, str]) -> bool:
    origin = _expression_origin(node, bindings)
    return origin == "KEEPALIVE_ROTATE_URL" or bool(
        origin and origin.endswith(".KEEPALIVE_ROTATE_URL")
    )


def _method_name(node: ast.AST, bindings: dict[str, str]) -> str | None:
    origin = _expression_origin(node, bindings)
    if origin is not None:
        return origin.rsplit(".", 1)[-1]
    return None


def _is_bespoke_rotate_post(node: ast.AST, bindings: dict[str, str]) -> bool:
    if not isinstance(node, ast.Call):
        return False
    method = _method_name(node.func, bindings)
    if method == "post":
        targets = list(node.args[:1]) + [kw.value for kw in node.keywords if kw.arg == "url"]
        return any(_is_rotate_url(target, bindings) for target in targets)
    if method != "request":
        return False
    methods = list(node.args[:1]) + [kw.value for kw in node.keywords if kw.arg == "method"]
    targets = list(node.args[1:2]) + [kw.value for kw in node.keywords if kw.arg == "url"]
    return any((_static_string(item) or "").upper() == "POST" for item in methods) and any(
        _is_rotate_url(target, bindings) for target in targets
    )


def _bespoke_rotate_posts(source: str) -> list[ast.Call]:
    tree = ast.parse(source)
    path = _AUTH_ROOT / "synthetic.py"
    bindings = _bindings(tree, path=path, src_root=_SRC_ROOT)
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _is_bespoke_rotate_post(node, bindings)
    ]


class _WireCallCollector(ast.NodeVisitor):
    def __init__(self, bindings: dict[str, str]) -> None:
        self.bindings = bindings
        self.scopes: list[str] = []
        self.calls: set[tuple[str, str]] = set()

    @property
    def owner(self) -> str:
        return ".".join(self.scopes) if self.scopes else "<module>"

    def _visit_scope(self, node: ast.AST, name: str) -> None:
        self.scopes.append(name)
        self.generic_visit(node)
        self.scopes.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._visit_scope(node, node.name)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_scope(node, node.name)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self._visit_scope(node, "<lambda>")

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self._visit_scope(node, "<comprehension>")

    visit_SetComp = visit_ListComp
    visit_DictComp = visit_ListComp
    visit_GeneratorExp = visit_ListComp

    def visit_Call(self, node: ast.Call) -> None:
        origin = _expression_origin(node.func, self.bindings)
        if origin is not None and origin.rsplit(".", 1)[-1] in _WIRE_FUNCTIONS:
            self.calls.add((self.owner, origin.rsplit(".", 1)[-1]))
        self.generic_visit(node)


def _wire_call_owners(root: Path = _SRC_ROOT) -> set[tuple[str, str, str]]:
    owners: set[tuple[str, str, str]] = set()
    for path in _iter_src_modules(root):
        tree = _tree(path)
        collector = _WireCallCollector(_bindings(tree, path=path, src_root=root))
        collector.visit(tree)
        relative = path.relative_to(root).as_posix()
        owners.update((relative, owner, name) for owner, name in collector.calls)
    return owners


def _wire_definitions(root: Path = _SRC_ROOT) -> set[tuple[str, str]]:
    definitions: set[tuple[str, str]] = set()
    for path in _iter_src_modules(root):
        for node in ast.walk(_tree(path)):
            if (
                isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
                and node.name in _WIRE_FUNCTIONS
            ):
                definitions.add((path.relative_to(root).as_posix(), node.name))
    return definitions


def _keepalive_reexports(tree: ast.Module) -> dict[str, str]:
    result: dict[str, str] = {}
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id in _REEXPORTS
        ):
            result[node.targets[0].id] = ast.unparse(node.value)
    return result


def test_rotate_literals_and_definitions_have_one_dependency_bottom_owner() -> None:
    assert _files_with_constant(_ROTATE_URL_LITERAL) == [_WIRE_OWNER]
    assert _files_with_constant(_ROTATE_BODY_LITERAL) == [_WIRE_OWNER]
    assert _wire_definitions() == {
        ("_auth/mint_service.py", "_rotate_post"),
        ("_auth/mint_service.py", "_rotate_post_sync"),
    }


def test_no_bespoke_rotate_post_outside_wire_owner() -> None:
    violations: list[str] = []
    for path in _iter_src_modules():
        if path == _WIRE_OWNER:
            continue
        tree = _tree(path)
        bindings = _bindings(tree, path=path, src_root=_SRC_ROOT)
        violations.extend(
            f"{path.relative_to(_SRC_ROOT.parent.parent)}:{node.lineno}"
            for node in ast.walk(tree)
            if _is_bespoke_rotate_post(node, bindings)
        )
    assert not violations, f"RotateCookies POST assembled outside mint_service: {violations}"


def test_keepalive_reexports_exact_wire_bindings_by_identity() -> None:
    assert _keepalive_reexports(_tree(_KEEPALIVE_PATH)) == {
        name: f"_mint_service.{name}" for name in _REEXPORTS
    }
    keepalive = importlib.import_module("notebooklm._auth.keepalive")
    mint_service = importlib.import_module(_WIRE_MODULE)
    for name in _REEXPORTS:
        assert getattr(keepalive, name) is getattr(mint_service, name)


def test_rotate_callers_remain_exact_policy_owners() -> None:
    assert _wire_call_owners() == {
        ("_auth/keepalive.py", "_rotate_cookies", "_rotate_post"),
        ("_auth/mint_service.py", "MintService.mint", "_rotate_post"),
        ("_auth/psidts_recovery.py", "_attempt_rotation", "_rotate_post_sync"),
        ("_auth/psidts_recovery.py", "recover_psidts_in_memory", "_rotate_post_sync"),
    }


@pytest.mark.parametrize(
    "source",
    [
        "client.post(KEEPALIVE_ROTATE_URL)",
        "client.post(url=KEEPALIVE_ROTATE_URL)",
        "url = KEEPALIVE_ROTATE_URL\nclient.post(url)",
        "url = KEEPALIVE_ROTATE_URL\nurl = harmless\nclient.post(url=url)",
        "send = client.post\nsend(KEEPALIVE_ROTATE_URL)",
        "getattr(client, 'po' + 'st')(KEEPALIVE_ROTATE_URL)",
        "client.request('POST', KEEPALIVE_ROTATE_URL)",
        "client.request(method='POST', url=KEEPALIVE_ROTATE_URL)",
        "run = lambda: client.post(KEEPALIVE_ROTATE_URL)",
        "calls = [client.post(KEEPALIVE_ROTATE_URL) for _ in (0,)]",
        "if enabled:\n    client.post(url=mod.KEEPALIVE_ROTATE_URL)",
    ],
)
def test_bespoke_post_detector_bites_alias_dynamic_and_deferred_shapes(source: str) -> None:
    assert _bespoke_rotate_posts(source)


@pytest.mark.parametrize(
    "source",
    [
        'httpx.Request("POST", KEEPALIVE_ROTATE_URL)',
        "client.post(SOME_OTHER_URL, content=b)",
        "await _rotate_post(client)",
        "client.request('GET', KEEPALIVE_ROTATE_URL)",
    ],
)
def test_bespoke_post_detector_ignores_non_sending_or_unrelated_shapes(source: str) -> None:
    assert not _bespoke_rotate_posts(source)


@pytest.mark.parametrize(
    "body",
    [
        "from notebooklm._auth.mint_service import _rotate_post as send\n"
        "async def later(client):\n    await send(client)\n",
        "import notebooklm._auth.mint_service as wire\n"
        "run = lambda client: wire._rotate_post_sync(client)\n",
        "import importlib\n"
        "wire = importlib.import_module('notebooklm._auth.' + 'mint_service')\n"
        "calls = [wire._rotate_post_sync(client) for client in clients]\n",
        "import notebooklm._auth.mint_service as wire\n"
        "send = getattr(wire, '_rotate_' + 'post')\n"
        "if ready:\n    send(client)\n",
    ],
)
def test_wire_caller_collector_bites_alias_dynamic_and_deferred_shapes(
    tmp_path: Path, body: str
) -> None:
    root = tmp_path / "notebooklm"
    source = root / "_auth" / "consumer.py"
    source.parent.mkdir(parents=True)
    source.write_text(body, encoding="utf-8")
    assert _wire_call_owners(root)
