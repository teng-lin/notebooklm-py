"""Fail-closed boundary for refresh's typed profile-store persistence handoff."""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest

import notebooklm._auth as auth_package
import notebooklm.auth as auth_facade
from notebooklm._auth import refresh, storage
from notebooklm._auth.cookie_types import CookieJar
from tests._guardrails._ast_semantics import semantic_hash as _portable_semantic_hash

pytestmark = pytest.mark.repo_lint

REPO_ROOT = Path(__file__).resolve().parents[2]
REFRESH_PATH = REPO_ROOT / "src" / "notebooklm" / "_auth" / "refresh.py"

_HELPER = "_merge_domain_fetch_observation"
_FETCH = "fetch_tokens_with_domains"
_MERGE = "merge_cookie_observation"
_MODULE_HASH = "ab411febaecc908a4d7c2ff3b338dcefb7d979e44d1b36987a09808563ae863a"
_HELPER_HASH = "96fa4345674a291eee8906a51d5511438bb822b0c75785dc7ec52e5ffd82cc0c"
_FETCH_HASH = "b1d2f00fbaea93d720d75547788dda8aa12a29e00026911c565cba3e76c7cf60"


def _tree(source: str | None = None) -> ast.Module:
    if source is None:
        source = REFRESH_PATH.read_text(encoding="utf-8")
    return ast.parse(source, filename=str(REFRESH_PATH))


def _function(tree: ast.Module, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    matches = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name
    ]
    assert len(matches) == 1
    return matches[0]


def _semantic_hash(node: ast.AST) -> str:
    return _portable_semantic_hash(node)


def _qualified_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        owner = _qualified_name(node.value)
        return f"{owner}.{node.attr}" if owner else node.attr
    return None


def _static_string(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _static_string(node.left)
        right = _static_string(node.right)
        if left is not None and right is not None:
            return left + right
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            elif (
                isinstance(value, ast.FormattedValue)
                and value.conversion == -1
                and value.format_spec is None
            ):
                rendered = _static_string(value.value)
                if rendered is None:
                    return None
                parts.append(rendered)
            else:
                return None
        return "".join(parts)
    return None


def _target_names(node: ast.AST) -> set[str]:
    if isinstance(node, ast.Name):
        return {node.id}
    if isinstance(node, ast.Starred):
        return _target_names(node.value)
    if isinstance(node, ast.Tuple | ast.List):
        return {name for item in node.elts for name in _target_names(item)}
    return set()


def _possible_strings(node: ast.AST, bindings: dict[str, set[str]]) -> set[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, ast.Name):
        return bindings.get(node.id, set())
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return {
            left + right
            for left in _possible_strings(node.left, bindings)
            for right in _possible_strings(node.right, bindings)
        }
    if isinstance(node, ast.JoinedStr):
        possible = {""}
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                fragments = {value.value}
            elif (
                isinstance(value, ast.FormattedValue)
                and value.conversion == -1
                and value.format_spec is None
            ):
                fragments = _possible_strings(value.value, bindings)
            else:
                return set()
            possible = {prefix + fragment for prefix in possible for fragment in fragments}
        return possible
    return set()


def _string_bindings(tree: ast.AST) -> dict[str, set[str]]:
    """Conservatively retain every statically possible string binding."""
    bindings: dict[str, set[str]] = {}
    assignments: list[tuple[ast.AST, ast.AST]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            assignments.extend((target, node.value) for target in node.targets)
        elif (isinstance(node, ast.AnnAssign) and node.value is not None) or isinstance(
            node, ast.NamedExpr
        ):
            assignments.append((node.target, node.value))

    changed = True
    while changed:
        changed = False
        for target, value in assignments:
            possible = _possible_strings(value, bindings)
            for name in _target_names(target):
                current = bindings.setdefault(name, set())
                before = len(current)
                current.update(possible)
                changed |= len(current) != before
    return bindings


def _dynamic_accesses(tree: ast.AST) -> tuple[set[str], bool]:
    """Return statically named dynamic accesses and whether a mapping itself escapes."""
    bindings = _string_bindings(tree)
    dynamic_primitives = {
        "__builtins__",
        "__getattribute__",
        "__getitem__",
        "__import__",
        "attrgetter",
        "builtins",
        "compile",
        "eval",
        "exec",
        "getattr",
        "globals",
        "itemgetter",
        "locals",
        "vars",
    }
    namespace_attributes = {
        "ag_frame",
        "cell_contents",
        "cr_frame",
        "f_builtins",
        "f_globals",
        "f_locals",
        "gi_frame",
        "tb_frame",
    }
    mapping_functions = set(dynamic_primitives)
    assignments: list[tuple[ast.AST, ast.AST]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            assignments.extend((target, node.value) for target in node.targets)
        elif (isinstance(node, ast.AnnAssign) and node.value is not None) or isinstance(
            node, ast.NamedExpr
        ):
            assignments.append((node.target, node.value))

    changed = True
    while changed:
        changed = False
        for target, value in assignments:
            value_name = (_qualified_name(value) or "").rsplit(".", 1)[-1]
            if value_name not in mapping_functions:
                continue
            for name in _target_names(target):
                if name not in mapping_functions:
                    mapping_functions.add(name)
                    changed = True

    targets: set[str] = set()
    mapping_escape = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            mapping_escape |= node.id in dynamic_primitives
        elif isinstance(node, ast.ImportFrom):
            mapping_escape |= any(item.name in dynamic_primitives for item in node.names)
        elif isinstance(node, ast.Subscript):
            targets.update(_possible_strings(node.slice, bindings))
        elif isinstance(node, ast.Attribute):
            unsafe_dunder = (
                node.attr.startswith("__") and node.attr.endswith("__") and node.attr != "__name__"
            )
            mapping_escape |= (
                unsafe_dunder
                or node.attr in dynamic_primitives
                or node.attr in namespace_attributes
            )
        elif isinstance(node, ast.Call):
            targets.update(
                target
                for argument in (*node.args, *(keyword.value for keyword in node.keywords))
                for target in _possible_strings(argument, bindings)
            )
            call_name = (_qualified_name(node.func) or "").rsplit(".", 1)[-1]
            if call_name in mapping_functions:
                mapping_escape = True
    mapping_escape |= bool(targets & dynamic_primitives)
    return targets, mapping_escape


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    return {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}


def _owner(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> ast.AST | None:
    current = node
    while current in parents:
        current = parents[current]
        if isinstance(current, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            return current
    return None


def _is_awaited(call: ast.Call, parents: dict[ast.AST, ast.AST]) -> bool:
    return isinstance(parents.get(call), ast.Await)


def _is_direct_worker_handoff(node: ast.Name, parents: dict[ast.AST, ast.AST]) -> bool:
    call = parents.get(node)
    owner = _owner(node, parents)
    return (
        isinstance(call, ast.Call)
        and call.args
        and call.args[0] is node
        and _qualified_name(call.func) == "asyncio.to_thread"
        and _is_awaited(call, parents)
        and isinstance(owner, ast.AsyncFunctionDef)
        and owner.name == _FETCH
    )


def _capability_violations(tree: ast.Module) -> set[str]:
    """Reject every helper/store capability form except the one exact handoff."""
    parents = _parents(tree)
    violations: set[str] = set()
    if _semantic_hash(tree) != _MODULE_HASH:
        violations.add("module-semantic-drift")
    dynamic_targets, mapping_escape = _dynamic_accesses(tree)
    if mapping_escape:
        violations.add("dynamic-mapping")
    for target in dynamic_targets & {
        _HELPER,
        _MERGE,
        "to_thread",
        "save_cookies_to_storage",
    }:
        violations.add(f"dynamic:{target}")

    definitions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == _HELPER
    ]
    if definitions and (
        len(definitions) != 1
        or not isinstance(definitions[0], ast.FunctionDef)
        or not isinstance(parents.get(definitions[0]), ast.Module)
    ):
        violations.add("helper-definition")

    for node in ast.walk(tree):
        if isinstance(node, ast.arg) and node.arg == _HELPER:
            violations.add("helper-rebind")
        elif isinstance(node, ast.Name) and node.id == _HELPER:
            if isinstance(node.ctx, ast.Store):
                violations.add("helper-rebind")
            elif isinstance(node.ctx, ast.Load) and not _is_direct_worker_handoff(node, parents):
                violations.add("helper-escape")
        elif isinstance(node, ast.Attribute) and node.attr == _HELPER:
            violations.add("qualified-helper")
        elif isinstance(node, ast.ImportFrom) and any(item.name == _HELPER for item in node.names):
            violations.add("helper-import")
        elif isinstance(node, ast.Call):
            call_name = (_qualified_name(node.func) or "").rsplit(".", 1)[-1]
            if call_name == "getattr" and len(node.args) >= 2:
                target = _static_string(node.args[1])
                if target in {_HELPER, _MERGE, "to_thread", "save_cookies_to_storage"}:
                    violations.add(f"dynamic:{target}")

        if not isinstance(node, ast.Attribute) or node.attr != _MERGE:
            continue
        parent = parents.get(node)
        owner = _owner(node, parents)
        canonical_helper_call = (
            isinstance(owner, ast.FunctionDef)
            and owner.name == _HELPER
            and isinstance(parent, ast.Call)
            and parent.func is node
        )
        if canonical_helper_call:
            continue
        if isinstance(parent, ast.Call) and parent.func is node:
            violations.add("direct-store-dispatch")
        else:
            violations.add("bound-store-capability")
    return violations


def _handoff_violations(function: ast.AsyncFunctionDef) -> set[str]:
    """Pin the sole loader worker and sole persistence-capability worker calls."""
    parents = _parents(function)
    calls = sorted(
        (
            node
            for node in ast.walk(function)
            if isinstance(node, ast.Call) and _qualified_name(node.func) == "asyncio.to_thread"
        ),
        key=lambda node: node.lineno,
    )
    expected = [
        ["_auth_cookies._build_cookie_pair_from_storage", "storage_path"],
        [_HELPER, "store", "observation", "selected_baseline"],
    ]
    actual = [[ast.unparse(argument) for argument in call.args] for call in calls]
    violations: set[str] = set()
    if actual != expected or any(call.keywords or not _is_awaited(call, parents) for call in calls):
        violations.add("worker-projection")
        return violations

    pair_await = parents[calls[0]]
    pair_assignment = parents.get(pair_await)
    if not (
        isinstance(pair_assignment, ast.Assign)
        and len(pair_assignment.targets) == 1
        and isinstance(pair_assignment.targets[0], ast.Name)
        and pair_assignment.targets[0].id == "pair"
    ):
        violations.add("pair-result")

    merge_await = parents[calls[1]]
    merge_assignment = parents.get(merge_await)
    if not (
        isinstance(merge_assignment, ast.Assign)
        and len(merge_assignment.targets) == 1
        and isinstance(merge_assignment.targets[0], ast.Name)
        and merge_assignment.targets[0].id == "selected_baseline"
    ):
        violations.add("merge-result")
    return violations


def _legacy_saver_violations(tree: ast.AST) -> set[str]:
    violations: set[str] = set()
    dynamic_targets, mapping_escape = _dynamic_accesses(tree)
    if mapping_escape:
        violations.add("dynamic-mapping")
    if "save_cookies_to_storage" in dynamic_targets:
        violations.add("dynamic-saver")
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == "save_cookies_to_storage":
            violations.add("bare-saver")
        elif isinstance(node, ast.Attribute) and node.attr == "save_cookies_to_storage":
            violations.add("qualified-saver")
        elif isinstance(node, ast.Call) and (_qualified_name(node.func) or "").endswith("getattr"):
            if len(node.args) >= 2 and _static_string(node.args[1]) == "save_cookies_to_storage":
                violations.add("dynamic-saver")
        elif isinstance(node, ast.ImportFrom) and any(
            item.name == "save_cookies_to_storage" for item in node.names
        ):
            violations.add("imported-saver")
    return violations


def test_helper_fetch_bodies_signatures_and_typed_import_are_exact() -> None:
    tree = _tree()
    helper = _function(tree, _HELPER)
    fetch = _function(tree, _FETCH)
    assert isinstance(helper, ast.FunctionDef)
    assert isinstance(fetch, ast.AsyncFunctionDef)
    assert _semantic_hash(tree) == _MODULE_HASH
    assert _semantic_hash(helper) == _HELPER_HASH
    assert _semantic_hash(fetch) == _FETCH_HASH
    assert str(inspect.signature(refresh._merge_domain_fetch_observation)) == (
        "(store: 'ProfileStore', observation: 'CookieJar', baseline: 'CookieJar') -> 'CookieJar'"
    )
    assert str(inspect.signature(refresh.fetch_tokens_with_domains)) == (
        "(path: 'Path | None' = None, profile: 'str | None' = None, *, "
        "authuser: 'int | None' = None, account_email: 'str | None' = None, "
        "allow_headless: 'bool' = False) -> 'tuple[str, str]'"
    )
    assert [
        (node.level, node.module, [(item.name, item.asname) for item in node.names])
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module == "profile_store"
    ] == [(1, "profile_store", [("ProfileStore", None)])]
    assert _capability_violations(tree) == set()
    assert _legacy_saver_violations(tree) == set()


def test_helper_has_one_typed_merge_and_no_async_or_nested_capability() -> None:
    helper = _function(_tree(), _HELPER)
    assert isinstance(helper, ast.FunctionDef)
    calls = [
        node
        for node in ast.walk(helper)
        if isinstance(node, ast.Call)
        and _qualified_name(node.func) == "store.merge_cookie_observation"
    ]
    assert len(calls) == 1
    assert [ast.unparse(argument) for argument in calls[0].args] == ["observation"]
    assert [(keyword.arg, ast.unparse(keyword.value)) for keyword in calls[0].keywords] == [
        ("baseline", "baseline")
    ]
    assert not any(
        isinstance(
            node,
            ast.Await
            | ast.AsyncFor
            | ast.AsyncWith
            | ast.Lambda
            | ast.FunctionDef
            | ast.AsyncFunctionDef,
        )
        and node is not helper
        for node in ast.walk(helper)
    )


def test_fetch_has_exact_direct_positional_handoffs_and_ordering() -> None:
    fetch = _function(_tree(), _FETCH)
    assert isinstance(fetch, ast.AsyncFunctionDef)
    assert _handoff_violations(fetch) == set()

    calls = [node for node in ast.walk(fetch) if isinstance(node, ast.Call)]
    by_name: dict[str, list[ast.Call]] = {}
    for call in calls:
        by_name.setdefault(_qualified_name(call.func) or "", []).append(call)
    assert len(by_name["ProfileStore"]) == 1
    assert [ast.unparse(argument) for argument in by_name["ProfileStore"][0].args] == [
        "storage_path"
    ]
    assert by_name["ProfileStore"][0].keywords == []
    assert len(by_name["CookieJar.from_httpx"]) == 1
    assert [ast.unparse(argument) for argument in by_name["CookieJar.from_httpx"][0].args] == [
        "live"
    ]
    worker_calls = sorted(by_name["asyncio.to_thread"], key=lambda call: call.lineno)
    ordered = [
        worker_calls[0].lineno,
        by_name["_fetch_tokens_with_exact_baseline"][0].lineno,
        by_name["ProfileStore"][0].lineno,
        by_name["CookieJar.from_httpx"][0].lineno,
        worker_calls[1].lineno,
    ]
    assert ordered == sorted(ordered)
    assert not any(
        (_qualified_name(call.func) or "").rsplit(".", 1)[-1]
        in {
            _MERGE,
            "save_cookies_to_storage",
            "build_httpx_cookies_from_storage",
            "snapshot_cookie_jar",
            "create_task",
            "ensure_future",
        }
        for call in calls
    )


class _FakeStore:
    def __init__(self, result: SimpleNamespace) -> None:
        self.result = result
        self.calls: list[tuple[CookieJar, CookieJar]] = []

    def merge_cookie_observation(
        self, observation: CookieJar, *, baseline: CookieJar
    ) -> SimpleNamespace:
        self.calls.append((observation, baseline))
        return self.result


def test_helper_runtime_result_matrix_is_exact() -> None:
    observation = CookieJar()
    baseline = CookieJar()
    next_baseline = CookieJar()

    skipped_store = _FakeStore(
        SimpleNamespace(advances_ordering=False, next_baseline=next_baseline)
    )
    assert (
        refresh._merge_domain_fetch_observation(  # type: ignore[arg-type]
            skipped_store, observation, baseline
        )
        is baseline
    )
    assert skipped_store.calls == [(observation, baseline)]

    advanced_store = _FakeStore(
        SimpleNamespace(advances_ordering=True, next_baseline=next_baseline)
    )
    assert (
        refresh._merge_domain_fetch_observation(  # type: ignore[arg-type]
            advanced_store, observation, baseline
        )
        is next_baseline
    )
    assert advanced_store.calls == [(observation, baseline)]

    invalid_store = _FakeStore(SimpleNamespace(advances_ordering=True, next_baseline=None))
    with pytest.raises(AssertionError, match="advancing refresh merge must provide a baseline"):
        refresh._merge_domain_fetch_observation(  # type: ignore[arg-type]
            invalid_store, observation, baseline
        )
    assert invalid_store.calls == [(observation, baseline)]


def test_helper_is_internal_while_public_saver_seams_remain_exact() -> None:
    assert _HELPER not in getattr(refresh, "__all__", ())
    assert _HELPER not in getattr(auth_package, "__all__", ())
    assert _HELPER not in auth_facade.__all__
    assert not hasattr(auth_facade, _HELPER)
    assert not hasattr(refresh, "save_cookies_to_storage")
    assert "save_cookies_to_storage" in storage.__all__
    assert auth_facade.save_cookies_to_storage is storage.save_cookies_to_storage


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "def forbidden():\n"
            "    callback = _merge_domain_fetch_observation\n"
            "    return callback\n",
            "helper-escape",
        ),
        (
            "def forbidden(callbacks=(_merge_domain_fetch_observation,)):\n    return callbacks\n",
            "helper-escape",
        ),
        (
            "def _merge_domain_fetch_observation():\n    return None\n"
            "def outer():\n"
            "    def _merge_domain_fetch_observation():\n        return None\n",
            "helper-definition",
        ),
        (
            "async def fetch_tokens_with_domains(_merge_domain_fetch_observation):\n"
            "    return await asyncio.to_thread(_merge_domain_fetch_observation, store, obs, base)\n",
            "helper-rebind",
        ),
        (
            "async def fetch_tokens_with_domains():\n"
            "    return await asyncio.to_thread(lambda: _merge_domain_fetch_observation, store)\n",
            "helper-escape",
        ),
        (
            "async def fetch_tokens_with_domains():\n"
            "    return await asyncio.to_thread(store.merge_cookie_observation, obs, base)\n",
            "bound-store-capability",
        ),
        (
            "async def fetch_tokens_with_domains():\n"
            "    return store.merge_cookie_observation(obs, baseline=base)\n",
            "direct-store-dispatch",
        ),
        (
            "async def fetch_tokens_with_domains():\n"
            "    return getattr(refresh, '_merge_' + 'domain_fetch_observation')\n",
            "dynamic:_merge_domain_fetch_observation",
        ),
        (
            "from notebooklm._auth.refresh import _merge_domain_fetch_observation as callback\n",
            "helper-import",
        ),
        (
            "def forbidden():\n    return refresh._merge_domain_fetch_observation\n",
            "qualified-helper",
        ),
        (
            "def forbidden():\n    return getattr(storage, 'save_' + 'cookies_to_storage')\n",
            "dynamic:save_cookies_to_storage",
        ),
        (
            "_alternate_writer = vars(_auth_storage)['save_' + 'cookies_to_storage']\n",
            "dynamic-mapping",
        ),
        (
            "lookup = vars\n"
            "mapping = lookup(_auth_storage)\n"
            "key = 'save_' + 'cookies_to_storage'\n"
            "_alternate_writer = mapping[key]\n",
            "dynamic-saver",
        ),
        (
            "key = 'save_cookies_to_storage'\n_alternate_writer = _auth_storage.__dict__[key]\n",
            "dynamic-mapping",
        ),
        (
            "key = 'save_cookies_to_storage'\n_alternate_writer = globals()[key]\n",
            "dynamic-saver",
        ),
        (
            "_alternate_writer = getattr(_auth_storage, f\"save_{'cookies_to_storage'}\")\n",
            "dynamic:save_cookies_to_storage",
        ),
        (
            "getters = (getattr,)\n"
            "getter = getters[0]\n"
            "_alternate_writer = getter(_auth_storage, dynamic_name)\n",
            "dynamic-mapping",
        ),
        (
            "_alternate_writer = __builtins__['get' + 'attr'](\n"
            "    _auth_storage, ''.join(('save_', 'cookies_to_storage'))\n"
            ")\n",
            "dynamic-mapping",
        ),
        (
            "namespace = fetch_tokens_with_domains.__globals__\n"
            "builtins_map = namespace[''.join(('__built', 'ins__'))]\n"
            "_alternate_writer = builtins_map[''.join(('get', 'attr'))](\n"
            "    _auth_storage, ''.join(('save_', 'cookies_to_storage'))\n"
            ")\n",
            "dynamic-mapping",
        ),
    ],
)
def test_binding_callback_bound_method_and_dynamic_capability_bites(
    source: str, expected: str
) -> None:
    tree = _tree(source)
    violations = _capability_violations(tree) | _legacy_saver_violations(tree)
    assert expected in violations


@pytest.mark.parametrize(
    ("injection", "expected"),
    [
        (
            "_alternate_writer = vars(_auth_storage)['save_' + 'cookies_to_storage']",
            "dynamic-mapping",
        ),
        (
            "lookup = vars\n"
            "mapping = lookup(_auth_storage)\n"
            "key = 'save_' + 'cookies_to_storage'\n"
            "_alternate_writer = mapping[key]",
            "dynamic-saver",
        ),
        (
            "key = 'save_cookies_to_storage'\n_alternate_writer = _auth_storage.__dict__[key]",
            "dynamic-mapping",
        ),
        (
            "key = 'save_cookies_to_storage'\n_alternate_writer = globals()[key]",
            "dynamic-saver",
        ),
        (
            "_alternate_writer = getattr(_auth_storage, f\"save_{'cookies_to_storage'}\")",
            "dynamic:save_cookies_to_storage",
        ),
        (
            "getters = (getattr,)\n"
            "getter = getters[0]\n"
            "_alternate_writer = getter(_auth_storage, dynamic_name)",
            "dynamic-mapping",
        ),
        (
            "_alternate_writer = __builtins__['get' + 'attr'](\n"
            "    _auth_storage, ''.join(('save_', 'cookies_to_storage'))\n"
            ")",
            "dynamic-mapping",
        ),
        (
            "namespace = fetch_tokens_with_domains.__globals__\n"
            "builtins_map = namespace[''.join(('__built', 'ins__'))]\n"
            "_alternate_writer = builtins_map[''.join(('get', 'attr'))](\n"
            "    _auth_storage, ''.join(('save_', 'cookies_to_storage'))\n"
            ")",
            "dynamic-mapping",
        ),
    ],
)
def test_full_source_dynamic_mapping_aliases_bite_without_projection_drift(
    injection: str, expected: str
) -> None:
    source = REFRESH_PATH.read_text(encoding="utf-8")
    marker = "\n\nasync def fetch_tokens_passive("
    assert source.count(marker) == 1
    mutated = source.replace(marker, f"\n{injection}{marker}")
    tree = _tree(mutated)
    assert _semantic_hash(_function(tree, _HELPER)) == _HELPER_HASH
    assert _semantic_hash(_function(tree, _FETCH)) == _FETCH_HASH
    violations = _capability_violations(tree) | _legacy_saver_violations(tree)
    assert expected in violations


@pytest.mark.parametrize(
    "source",
    [
        "async def fetch_tokens_with_domains():\n"
        "    return await worker(_merge_domain_fetch_observation, store, observation, baseline)\n",
        "async def fetch_tokens_with_domains():\n"
        "    return await asyncio.to_thread(_merge_domain_fetch_observation, store=store, "
        "observation=observation, baseline=baseline)\n",
        "async def fetch_tokens_with_domains():\n"
        "    result = asyncio.to_thread(_merge_domain_fetch_observation, store, observation, "
        "baseline)\n"
        "    return await result\n",
    ],
)
def test_non_direct_non_positional_and_non_awaited_worker_forms_bite(source: str) -> None:
    function = _function(_tree(source), _FETCH)
    assert isinstance(function, ast.AsyncFunctionDef)
    assert _handoff_violations(function)
