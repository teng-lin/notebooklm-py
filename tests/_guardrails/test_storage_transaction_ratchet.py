"""Shrink-only ownership gate for bounded and blocking profile transactions.

ADR-0034 PR6 moved the real transaction definition and mechanics into the
path-owned ``ProfileStore``. PR7A moved two account policy bodies onto its
bounded primitive; PR7B added browser/remint replacement there; PR7C adds the
login/import replacement; PR7D adds minted-session replacement. PR11B moves the
remaining credential write onto ``MasterTokenFile``. The frozen 2 raise / 1 skip /
2 report outcomes remain exact. Cookie persistence deliberately uses the separate
blocking primitive.
"""

from __future__ import annotations

import ast
import contextlib
from pathlib import Path

import pytest

from notebooklm._auth import profile_store, storage, storage_transaction
from notebooklm._auth.profile_store import ProfileStore
from notebooklm._auth.storage_lock import LockState
from notebooklm.exceptions import LockUnavailableError

pytestmark = pytest.mark.repo_lint

AUTH_ROOT = Path(__file__).resolve().parents[2] / "src" / "notebooklm" / "_auth"
STORAGE_PATH = AUTH_ROOT / "storage.py"
STORE_PATH = AUTH_ROOT / "profile_store.py"
TOKEN_FILE_PATH = AUTH_ROOT / "master_token_file.py"

_UNCONVERTED: frozenset[str] = frozenset()

_POLICY_CALLERS: dict[str, frozenset[str]] = {
    "raise_on_lock_unavailable": frozenset(
        {
            "profile_store.ProfileStore._update_account_if_document_unchanged",
            "profile_store.ProfileStore.update_account",
            "profile_store.ProfileStore.replace_minted_session",
        }
    ),
    "skip_on_lock_unavailable": frozenset({"profile_store.ProfileStore.clear_account"}),
    "report_on_lock_unavailable": frozenset(
        {
            "profile_store.ProfileStore.replace_from_login",
            "profile_store.ProfileStore.replace_from_remint",
        }
    ),
}

_STORAGE_TRANSACTION_CALLERS: frozenset[str] = frozenset()

_DIRECT_ACQUIRE_OWNERS = frozenset(
    {
        "storage._file_lock",
        "profile_store.ProfileStore._under_bounded_lock",
        "profile_store.ProfileStore._under_blocking_cookie_lock",
        "master_token_file.MasterTokenFile.write",
    }
)

_BOUNDED_STORE_OWNERS = frozenset(
    {
        "ProfileStore._update_account_if_document_unchanged",
        "ProfileStore.update_account",
        "ProfileStore.clear_account",
        "ProfileStore.replace_from_login",
        "ProfileStore.replace_from_remint",
        "ProfileStore.replace_minted_session",
    }
)


def _module_tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _owned_functions(path: Path) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    tree = _module_tree(path)
    module = path.stem
    found: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            found[f"{module}.{node.name}"] = node
        elif isinstance(node, ast.ClassDef):
            for member in node.body:
                if isinstance(member, ast.FunctionDef | ast.AsyncFunctionDef):
                    found[f"{module}.{node.name}.{member.name}"] = member
    return found


def _bare_calls(node: ast.AST) -> list[str]:
    return [
        inner.func.id
        for inner in ast.walk(node)
        if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name)
    ]


def _called_members(node: ast.AST) -> list[str]:
    return [
        inner.func.id if isinstance(inner.func, ast.Name) else inner.func.attr
        for inner in ast.walk(node)
        if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name | ast.Attribute)
    ]


def test_transaction_definition_and_compatibility_identities_are_exact() -> None:
    definitions = {
        path.name
        for path in AUTH_ROOT.glob("*.py")
        if any(
            isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            and node.name == "in_storage_transaction"
            for node in _module_tree(path).body
        )
    }
    assert definitions == {"profile_store.py"}
    assert storage.in_storage_transaction is profile_store.in_storage_transaction
    assert storage_transaction.in_storage_transaction is profile_store.in_storage_transaction
    assert storage.raise_on_lock_unavailable is profile_store.raise_on_lock_unavailable
    assert storage.skip_on_lock_unavailable is profile_store.skip_on_lock_unavailable
    assert storage.report_on_lock_unavailable is profile_store.report_on_lock_unavailable
    assert storage_transaction.raise_on_lock_unavailable is profile_store.raise_on_lock_unavailable
    assert storage_transaction.skip_on_lock_unavailable is profile_store.skip_on_lock_unavailable
    assert (
        storage_transaction.report_on_lock_unavailable is profile_store.report_on_lock_unavailable
    )


def _top_level_callers(path: Path, target: str) -> set[str]:
    callers: set[str] = set()
    for owner, node in _owned_functions(path).items():
        if target in _bare_calls(node):
            callers.add(owner.rsplit(".", 1)[-1])
    return callers


def test_storage_policy_body_routes_through_the_template_exactly() -> None:
    callers = _top_level_callers(STORAGE_PATH, "in_storage_transaction")
    assert callers == set(_STORAGE_TRANSACTION_CALLERS)
    assert frozenset() == _UNCONVERTED


def _qualified_callers(target: str) -> set[str]:
    callers: set[str] = set()
    for path in (STORE_PATH, STORAGE_PATH):
        for owner, node in _owned_functions(path).items():
            if target in _bare_calls(node):
                callers.add(owner)
    return callers


def test_lock_unavailable_policy_ownership_is_exact_3_1_2() -> None:
    actual = {policy: _qualified_callers(policy) for policy in _POLICY_CALLERS}
    assert actual == {policy: set(callers) for policy, callers in _POLICY_CALLERS.items()}
    assert {policy: len(callers) for policy, callers in actual.items()} == {
        "raise_on_lock_unavailable": 3,
        "skip_on_lock_unavailable": 1,
        "report_on_lock_unavailable": 2,
    }


def _is_lock_request_call(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and (
        (isinstance(node.func, ast.Name) and node.func.id == "LockRequest")
        or (isinstance(node.func, ast.Attribute) and node.func.attr == "LockRequest")
    )


def _direct_acquire_owners(source: str | None = None) -> set[str]:
    if source is not None:
        paths_and_functions = [("synthetic", _owned_functions_from_source(source))]
    else:
        paths_and_functions = [
            ("storage", _owned_functions(STORAGE_PATH)),
            ("profile_store", _owned_functions(STORE_PATH)),
            ("master_token_file", _owned_functions(TOKEN_FILE_PATH)),
        ]
    found: set[str] = set()
    for _module, functions in paths_and_functions:
        for owner, node in functions.items():
            request_names = {
                target.id
                for inner in ast.walk(node)
                if isinstance(inner, ast.Assign | ast.AnnAssign)
                for target in (
                    [*inner.targets] if isinstance(inner, ast.Assign) else [inner.target]
                )
                if isinstance(target, ast.Name)
                and inner.value is not None
                and _is_lock_request_call(inner.value)
            }
            for inner in ast.walk(node):
                if not (
                    isinstance(inner, ast.Call)
                    and isinstance(inner.func, ast.Attribute)
                    and inner.func.attr == "acquire"
                ):
                    continue
                arguments = [*inner.args, *(keyword.value for keyword in inner.keywords)]
                if any(
                    _is_lock_request_call(argument)
                    or (isinstance(argument, ast.Name) and argument.id in request_names)
                    for argument in arguments
                ):
                    found.add(owner)
    return found


def _owned_functions_from_source(
    source: str,
) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    tree = ast.parse(source)
    found: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            found[f"synthetic.{node.name}"] = node
        elif isinstance(node, ast.ClassDef):
            for member in node.body:
                if isinstance(member, ast.FunctionDef | ast.AsyncFunctionDef):
                    found[f"synthetic.{node.name}.{member.name}"] = member
    return found


@pytest.mark.parametrize(
    "body",
    [
        """\
def forbidden(path):
    with locks.acquire(LockRequest(path=path, blocking=False, operation="x")):
        pass
""",
        """\
def forbidden(path):
    request = locks.LockRequest(path=path, blocking=False, operation="x")
    with locks.acquire(request=request):
        pass
""",
        """\
def forbidden(path, enabled):
    request = LockRequest(path=path, blocking=False, operation="x")
    if enabled:
        with locks.acquire(request):
            pass
""",
        """\
def forbidden(path, value):
    request = LockRequest(path=path, blocking=False, operation="x")
    match value:
        case 1:
            with locks.acquire(request):
                pass
""",
        """\
def forbidden(path):
    request = LockRequest(path=path, blocking=False, operation="x")
    try:
        with locks.acquire(request):
            pass
    except Exception:
        pass
""",
        """\
def forbidden(path):
    request = LockRequest(path=path, blocking=False, operation="x")
    try:
        with locks.acquire(request):
            pass
    except* Exception:
        pass
""",
    ],
)
def test_direct_manager_detector_bites_inline_assigned_keyword_and_control_flow(body: str) -> None:
    if "except*" in body and not hasattr(ast, "TryStar"):
        pytest.skip("exception groups require Python 3.11+")
    assert _direct_acquire_owners(body) == {"synthetic.forbidden"}


def test_direct_manager_acquisitions_are_exactly_the_four_routing_seams() -> None:
    assert _direct_acquire_owners() == set(_DIRECT_ACQUIRE_OWNERS)


def _class_method(name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    return _owned_functions(STORE_PATH)[f"profile_store.ProfileStore.{name}"]


def test_cookie_methods_use_only_the_blocking_store_primitive() -> None:
    for name in ("merge_cookie_observation", "merge_legacy_cookie_observation"):
        calls = _called_members(_class_method(name))
        assert calls.count("_under_blocking_cookie_lock") == 1
        assert "_under_bounded_lock" not in calls


def test_bounded_store_owners_are_exact_and_use_no_other_transaction() -> None:
    methods = _owned_functions(STORE_PATH)
    actual = {
        owner.removeprefix("profile_store.")
        for owner, method in methods.items()
        if owner.startswith("profile_store.ProfileStore.")
        if "_under_bounded_lock" in _called_members(method)
    }
    assert actual == set(_BOUNDED_STORE_OWNERS)
    for name in (
        "_update_account_if_document_unchanged",
        "update_account",
        "clear_account",
        "replace_from_remint",
        "replace_from_login",
        "replace_minted_session",
    ):
        calls = _called_members(_class_method(name))
        assert calls.count("_under_bounded_lock") == 1
        assert "_under_blocking_cookie_lock" not in calls
        assert "in_storage_transaction" not in calls


def test_blocking_cookie_primitive_is_no_deadline_and_has_no_secure_parent_prep() -> None:
    method = _class_method("_under_blocking_cookie_lock")
    assert "_ensure_secure_parent_dir" not in _called_members(method)
    requests = [node for node in ast.walk(method) if _is_lock_request_call(node)]
    assert len(requests) == 1
    keywords = {item.arg: item.value for item in requests[0].keywords}
    assert isinstance(keywords["blocking"], ast.Constant) and keywords["blocking"].value is True
    assert (
        isinstance(keywords["deadline_seconds"], ast.Constant)
        and keywords["deadline_seconds"].value is None
    )


def test_bounded_primitive_is_nonblocking_and_prepares_parent() -> None:
    method = _class_method("_under_bounded_lock")
    assert _bare_calls(method)[0] == "_ensure_secure_parent_dir"
    requests = [node for node in ast.walk(method) if _is_lock_request_call(node)]
    assert len(requests) == 1
    keywords = {item.arg: item.value for item in requests[0].keywords}
    assert isinstance(keywords["blocking"], ast.Constant) and keywords["blocking"].value is False


def test_body_lock_unavailable_error_is_not_misclassified_as_lock_miss(tmp_path: Path) -> None:
    class HeldLocks:
        def acquire(self, request: object) -> contextlib.AbstractContextManager[LockState]:
            return contextlib.nullcontext(LockState.HELD)

    store = ProfileStore(tmp_path / "profile" / "storage_state.json", locks=HeldLocks())  # type: ignore[arg-type]
    sentinel = LockUnavailableError("body failure")
    policy_calls: list[Path] = []

    def body() -> None:
        raise sentinel

    def policy(lock_path: Path) -> None:
        policy_calls.append(lock_path)

    with pytest.raises(LockUnavailableError) as caught:
        store._under_bounded_lock(operation="test", body=body, on_unavailable=policy)
    assert caught.value is sentinel
    assert policy_calls == []
