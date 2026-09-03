"""Fail-closed executable boundary for ADR-0034 Phase 12C owners."""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

import notebooklm.auth as auth_facade
from notebooklm._auth import (
    account,
    account_types,
    cookie_types,
    cookies,
    keepalive,
    master_token,
    master_token_types,
    psidts_recovery,
    recovery,
    single_flight,
    storage,
)
from tests._guardrails._ast_semantics import semantic_hash as _portable_semantic_hash

pytestmark = pytest.mark.repo_lint

REPO_ROOT = Path(__file__).resolve().parents[2]
AUTH_ROOT = REPO_ROOT / "src" / "notebooklm" / "_auth"

_MODULE_HASHES = {
    "account.py": "077461a8ff59a6ca94280f9c2023a72d75bee205a2bb3cd65f724c82d24e36f9",
    "account_email.py": "ef1acba0e3abab00de5ea243ab4e3f21bb53a21947dc69ab0fabfcd773fcf715",
    "account_repair.py": "4d6cd6af598c26d04bbeed07970708f587f23a24358ccd0125e5883470e8d074",
    "account_types.py": "ae645b74c6d3f46ee9532179672c90d5b690877b900120889022d32d8efa372a",
    "profile_account.py": "899baf4cc0c748740247b68c3adfa2a3754bb565b143d4a1150329a05f456ce4",
    "cookie_types.py": "203e902e76add32859cab6bdad2c4855d34dea57dfc4f1ed56b47587c36dd6d8",
    "cookies.py": "575a6a071af1b0ff266f385678c9a1bf763a1a27a07be61bf51bb3637298b393",
    "keepalive.py": "505cfcf1d093d7aea2f26c2b7745a27ed8f7b51698e1bebdc3fafe4d6f78f065",
    "master_token.py": "42b3d3c3a4bc96c860d454ef8defd243b5d55617df7d3b4fc95467ae675bab78",
    "master_token_types.py": "856c741582249f7049fca0030e7af84cbda9141c7099a7aad6be6066090e5d57",
    "profile_migration.py": "ea6408a76890563c6f0f948e8031038436687611c0dd832b19c07b0177ede582",
    "profile_store.py": "26d63e626a6bf5333bb5e73f371177bff7c41baa6e11206ae8fbe5151a65b793",
    "psidts_recovery.py": "cde36fed0fcdc319a3e1d3a165c33d2a05385141b318667d4ce8c2c54ff7e51f",
    "recovery.py": "7a728071e7f763b13f68639b86be263c200c1c88c338599df0a692d00f15b4f7",
    "refresh.py": "99bc87088b997b534c1ce9e12844d6103a223b4fddf459df00d5812501ed866f",
    "single_flight.py": "8e298fe515dd667a3dfc95449165ab45345d327381951e98474aafa67510f246",
    "storage.py": "7c106fc41e4945c82d95fd57c7e29855b7e862e02d8d81db931df3df133f8b1e",
}

_NODE_HASHES = {
    (
        "single_flight.py",
        "Flight",
    ): "364b4f2727eec7cb1ed82f8dd8f2450c033edf8dcb171b3904e9506b78b44f77",
    (
        "single_flight.py",
        "SingleFlight",
    ): "661fa857608b0fcd03ce7f4328ed8339bf6d879b2f1d3faecea627ee0c8d17ad",
    (
        "single_flight.py",
        "read_success_epoch",
    ): "f1c7f2651c6a40a81ba0d53f9a9b1e1438d4cc986bcf5694029a3ea9185ed104",
    (
        "single_flight.py",
        "note_success",
    ): "b62ebf8da9b649949033cf5d3222810bc41c7f2a212cd906a2c096b27fc85cfb",
    (
        "single_flight.py",
        "claim",
    ): "1aaf9bccee23ceee88f158c42bc33002619ec0eee36330c3c1905b63351569f1",
    (
        "single_flight.py",
        "claim_if_epoch_current",
    ): "c289101193ffef23aa312395e11eafe00248411f8deebcc937131e829e87e817",
    (
        "single_flight.py",
        "await_flight",
    ): "e953486b72aeeb4b416e0c7e9151385b7fc157d5e90eefc8a848dc7c6cf5e14a",
    (
        "single_flight.py",
        "_reset_for_tests",
    ): "c2cb0853ff0e59a0387238cd2ff8705d6be16854ec7519c7da9d3ecdab8ac2ea",
    (
        "recovery.py",
        "ColdRecoveryResult",
    ): "badaf44569bcaca9411c61f1f29b5675c79e67218eb84c21ba72abd5ba992da0",
    (
        "recovery.py",
        "_ColdRecoveryExhaustion",
    ): "7c5ff11d83b86baedd79982e96763f7be914a9badcf9b79c0e789c15b3f51973",
    (
        "recovery.py",
        "ColdRecoveryState",
    ): "e440a56209af00bffa5a8270658b071e9f670adbf4e46a69d97a388ef1968b0c",
    (
        "recovery.py",
        "ColdRecoveryCoordinator",
    ): "ebc51a5732119e65bc53999db2749985464ee2a587127c6f32db08a414455c19",
    (
        "recovery.py",
        "_run_cold_recovery",
    ): "8ce6b98a05dd206649f505c48d081982a683053f32facc72969652521e7f54cc",
    (
        "recovery.py",
        "coalesced_cold_recovery",
    ): "a315319e656e0b6bfc2bab5cf8d5b19e155f060d9f66f24848c2edd1dec7f92b",
    (
        "keepalive.py",
        "RotationState",
    ): "35cf8fc9ffb360dfad3a4303e9fdf43848b2adb6dd320828dc4c88c12ec57d80",
    (
        "keepalive.py",
        "_get_poke_lock",
    ): "55f69d24acfd1c39b5d042da3fc2b150b6d79559d3530a3b60fd019b854cb933",
    (
        "keepalive.py",
        "_try_claim_rotation",
    ): "eaece5c9e7bba35e82796080ef309f825f57a2334aee64732ee4f8b8413b495e",
    (
        "keepalive.py",
        "_reset_poke_state_for_tests",
    ): "519ca98233234d20c7174c7f6adf2e93e9c41e2383df18d1b14756317f44d397",
    (
        "account_types.py",
        "Account",
    ): "c3b0693c59373e2372249e343860fe5c2d8938ef56c66d60620770e51523b666",
    (
        "account_types.py",
        "PlaywrightAccountRepairResult",
    ): "f8e364cba9f0e4233daaf2a77db2c6fa93745ca0d9c10ccb3189d96c0b2900d2",
    (
        "account_repair.py",
        "AccountRepairService",
    ): "394d0694023a2ac933fcca1ec38833e7c177fdca6b7e8fb516b34acd12244a47",
    (
        "account_repair.py",
        "_compose_account_repair_service",
    ): "562c7fe55147af50e3fdfb4ffbb7f1f81ea9f436f51ba733a3b414ccca62dbd6",
    (
        "account.py",
        "_enumerate_accounts_for_repair",
    ): "4f73aa3e1ea45ac6757cd522893931038cf4a17eec177a431e8a2d5a49d42440",
    (
        "account.py",
        "_select_account_for_repair",
    ): "f8015517f3f7b546bf4a2eaef71f272981e0c91a242f04a80ac4d4858d11559f",
    (
        "account.py",
        "_extract_active_email_for_repair",
    ): "7ceb5392f6ec749028cfab21f0e2c785b6dbe3dcdaae36750d16dd960c732a0a",
    (
        "account.py",
        "repair_account_metadata_from_playwright_storage",
    ): "6b7b72092b12bc05c4103bc160ec0274cd8d8a1ff18dd531a981ac74c96e62fc",
    (
        "cookie_types.py",
        "CookieJar",
    ): "045b876520bdb73a5b26f4983e34529196ebe6f05af70b66dc7e70cb8a4a0254",
    (
        "cookies.py",
        "load_session_jar",
    ): "0d60f34c0951b7a34a03ca0c9c48832f7a900aa4a590b5d09dac9292b89d4d6e",
    (
        "psidts_recovery.py",
        "_read_storage_for_recovery",
    ): "2f3bd102b7fa3ecd50665d60e76c2dfefa4e3c54bc26139887b566041d0e52ca",
    (
        "psidts_recovery.py",
        "_recovery_observation",
    ): "89168199b2b7c8dc8e60ce7f23803dfa51c63bceb8fbb6e9ed3c8d091ddef34c",
    (
        "psidts_recovery.py",
        "_attempt_rotation",
    ): "0c758ef4a8c225f08054f0ffc7e6b13677dc74cafe929c7c4632b62a5fcaee9c",
    (
        "psidts_recovery.py",
        "_psidts_is_live",
    ): "44a2553d6d7377cf2cbdb6fe53f450d93ee6ac68ee06b5d10488d395c99e194f",
    (
        "psidts_recovery.py",
        "_psidts_routes_to_rotate",
    ): "55968e7a32561622e990b6364026716d0f73bba1994e2c3ff5855001be4aeffe",
    (
        "master_token_types.py",
        "MasterTokenError",
    ): "e95a867fd295954b8d60697336a8c71fa2a40f67a63d98b360d1f5c2cf839c8c",
    (
        "storage.py",
        "_cookie_jar_for_merge",
    ): "fa39a3a4c3cc8b103bf02c3e7226a5d5cece3d45fac6203a0ca0231d6e48472f",
    (
        "storage.py",
        "persist_minted_jar",
    ): "9bb2e0e2344a750ba2ae6f62c909fc6e7c2b20e525adf7eb4cfdd69debaa11fe",
}

_FORBIDDEN_DYNAMIC = {
    "__dict__",
    "__import__",
    "eval",
    "exec",
    "getattr",
    "globals",
    "import_module",
    "locals",
    "setattr",
    "vars",
}


def _tree(name: str) -> ast.Module:
    return ast.parse((AUTH_ROOT / name).read_text(encoding="utf-8"), filename=name)


def _hash(node: ast.AST) -> str:
    return _portable_semantic_hash(node)


def _top_node(tree: ast.Module, name: str) -> ast.stmt:
    matches = [node for node in tree.body if getattr(node, "name", None) == name]
    assert len(matches) == 1
    return matches[0]


def _static_string(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _static_string(node.left)
        right = _static_string(node.right)
        return left + right if left is not None and right is not None else None
    if isinstance(node, ast.JoinedStr):
        pieces = [_static_string(value) for value in node.values]
        return (
            "".join(piece for piece in pieces if piece is not None)
            if all(piece is not None for piece in pieces)
            else None
        )
    return None


def _qualified(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        owner = _qualified(node.value)
        return f"{owner}.{node.attr}" if owner else node.attr
    return ""


def _capability_violations(tree: ast.AST, forbidden_imports: set[str]) -> set[str]:
    violations: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                if item.name in forbidden_imports:
                    violations.add(f"import:{item.name}")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module in forbidden_imports or any(
                item.name in forbidden_imports for item in node.names
            ):
                violations.add(f"import:{module}")
        elif isinstance(node, ast.Call):
            name = _qualified(node.func).rsplit(".", 1)[-1]
            if name in _FORBIDDEN_DYNAMIC:
                violations.add(f"call:{name}")
            if name in {"__import__", "import_module"} and node.args:
                target = _static_string(node.args[0])
                if target:
                    violations.add(f"dynamic-import:{target}")
        elif isinstance(node, ast.Attribute) and node.attr == "__dict__":
            violations.add("attribute:__dict__")
        elif isinstance(node, ast.Name) and node.id in {"builtins", "sys", "inspect"}:
            violations.add(f"name:{node.id}")
    return violations


def _self_fields(owner: ast.ClassDef) -> set[str]:
    constructor = next(
        node for node in owner.body if isinstance(node, ast.FunctionDef) and node.name == "__init__"
    )
    fields: set[str] = set()
    for node in ast.walk(constructor):
        target = (
            node.targets[0]
            if isinstance(node, ast.Assign) and len(node.targets) == 1
            else node.target
            if isinstance(node, ast.AnnAssign)
            else None
        )
        if (
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id == "self"
        ):
            fields.add(target.attr)
    return fields


def test_all_touched_executable_modules_and_owner_nodes_are_exact() -> None:
    for name, expected in _MODULE_HASHES.items():
        assert _hash(_tree(name)) == expected, name
    for (name, node_name), expected in _NODE_HASHES.items():
        assert _hash(_top_node(_tree(name), node_name)) == expected, (name, node_name)


def test_owner_fields_process_adapters_and_compatibility_views_are_exact() -> None:
    assert _self_fields(_top_node(_tree("single_flight.py"), "SingleFlight")) == {
        "_lock",
        "_flights",
        "_leader_tasks",
        "_success_epochs",
    }
    assert _self_fields(_top_node(_tree("recovery.py"), "ColdRecoveryState")) == {
        "_lock",
        "_locks_by_loop",
        "_success_generations",
    }
    assert _self_fields(_top_node(_tree("keepalive.py"), "RotationState")) == {
        "_lock",
        "_locks_by_loop",
        "_last_attempt_monotonic",
    }
    assert (
        single_flight.SingleFlight.process_default() is single_flight.SingleFlight.process_default()
    )
    assert (
        recovery.ColdRecoveryState.process_default() is recovery.ColdRecoveryState.process_default()
    )
    rotation = keepalive.RotationState.process_default()
    assert rotation is keepalive.RotationState.process_default()
    assert keepalive._POKE_STATE_LOCK is rotation._lock
    assert keepalive._POKE_LOCKS_BY_LOOP is rotation._locks_by_loop
    assert keepalive._LAST_POKE_ATTEMPT_MONOTONIC is rotation._last_attempt_monotonic
    assert auth_facade._POKE_STATE_LOCK is rotation._lock
    assert auth_facade._POKE_LOCKS_BY_LOOP is rotation._locks_by_loop
    assert auth_facade._LAST_POKE_ATTEMPT_MONOTONIC is rotation._last_attempt_monotonic


@pytest.mark.parametrize(
    ("module_name", "owner_name"),
    [("recovery.py", "ColdRecoveryState"), ("keepalive.py", "RotationState")],
)
def test_loop_lock_owners_use_weak_inner_values(module_name: str, owner_name: str) -> None:
    owner = _top_node(_tree(module_name), owner_name)
    called_attributes = {
        node.func.attr
        for node in ast.walk(owner)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "WeakKeyDictionary" in called_attributes
    assert "WeakValueDictionary" in called_attributes


def test_relocated_values_errors_helpers_and_retired_upward_aliases_are_exact() -> None:
    assert account.Account is account_types.Account
    assert account.PlaywrightAccountRepairResult is account_types.PlaywrightAccountRepairResult
    assert account.Account.__module__ == "notebooklm._auth.account"
    assert account.PlaywrightAccountRepairResult.__module__ == "notebooklm._auth.account"
    assert master_token.MasterTokenError is master_token_types.MasterTokenError
    assert storage.MasterTokenError is master_token_types.MasterTokenError
    assert auth_facade.MasterTokenError is master_token_types.MasterTokenError
    assert master_token_types.MasterTokenError.__module__ == "notebooklm._auth.master_token"
    assert master_token_types.MasterTokenError.__qualname__ == "MasterTokenError"
    assert str(inspect.signature(cookie_types.CookieJar.from_live_httpx_for_merge)) == (
        "(jar: 'httpx.Cookies', *, include_none: 'bool') -> 'CookieJar'"
    )
    assert storage._cookie_jar_for_merge.__module__ == "notebooklm._auth.storage"
    assert cookies.load_session_jar.__module__ == "notebooklm._auth.cookies"
    for name in {
        "load_session_jar",
        "_load_storage_state",
        "_storage_entry_to_cookie",
        "_safe_to_cookie",
        "_validate_cookie_shape",
    }:
        assert not hasattr(psidts_recovery, name)


@pytest.mark.parametrize(
    ("module_name", "forbidden"),
    [
        ("single_flight.py", {"account", "cookies", "refresh", "recovery", "storage"}),
        ("account_types.py", {"account", "account_repair", "profile_store", "storage"}),
        ("account_repair.py", {"account", "storage"}),
        ("cookie_types.py", {"cookies", "profile_store", "storage"}),
        ("master_token_types.py", {"master_token", "profile_store", "storage"}),
        ("psidts_recovery.py", {"cookies", "storage"}),
    ],
)
def test_dependency_bottom_modules_have_no_upward_or_dynamic_capability(
    module_name: str, forbidden: set[str]
) -> None:
    assert _capability_violations(_tree(module_name), forbidden) == set()


@pytest.mark.parametrize(
    "source",
    [
        "import storage\n",
        "from . import storage\n",
        "from .storage import ProfileStore\n",
        "import importlib\nimportlib.import_module('notebooklm._auth.' + 'storage')\n",
        "__import__(f\"notebooklm._auth.{ 'storage' }\")\n",
        "getattr(owner, 'state')\n",
        "vars(owner)\n",
        "globals()['state']\n",
        "owner.__dict__['state']\n",
    ],
)
def test_import_reflection_alias_and_constant_assembly_bites(source: str) -> None:
    assert _capability_violations(ast.parse(source), {"storage"})


def test_alias_rebinding_and_hidden_peer_registry_bites() -> None:
    rebinding = ast.parse("_POKE_LOCKS_BY_LOOP = {}\n")
    assignments = {
        target.id
        for node in ast.walk(rebinding)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    assert assignments & {"_POKE_STATE_LOCK", "_POKE_LOCKS_BY_LOOP", "_LAST_POKE_ATTEMPT_MONOTONIC"}
    peer = ast.parse(
        "class Owner:\n"
        " def __init__(self):\n"
        "  self._lock = lock()\n"
        "  self._locks_by_loop = {}\n"
        "  self._second_registry = {}\n"
    )
    assert _self_fields(peer.body[0]) - {"_lock", "_locks_by_loop"} == {"_second_registry"}


def test_one_shot_services_have_no_callback_or_secret_fields_after_settlement_shape() -> None:
    coordinator_fields = _self_fields(_top_node(_tree("recovery.py"), "ColdRecoveryCoordinator"))
    assert coordinator_fields == {
        "_claim_lock",
        "_state",
        "_single_flight",
        "_should_try_refresh",
        "_resolve_refresh_path",
        "_run_refresh_attempt",
        "_load_cookie_pair",
        "_run_headless_attempt",
        "_run_master_token_attempt",
        "_validate_recovered",
        "_fetch_recovered",
        "_replace_cookie_jar",
        "_snapshot_cookie_jar",
        "_clone_cookie_jar",
        "_used",
    }
    repair_fields = _self_fields(_top_node(_tree("account_repair.py"), "AccountRepairService"))
    assert repair_fields == {
        "_claim_lock",
        "_claimed",
        "_writer_factory",
        "_load_cookie_jar",
        "_enumerate_accounts",
        "_select_account",
        "_extract_active_email",
        "_poke_session",
    }
    for file_name, class_name, method_name, deleted in [
        (
            "recovery.py",
            "ColdRecoveryCoordinator",
            "recover",
            coordinator_fields - {"_claim_lock", "_state", "_single_flight", "_used"},
        ),
        (
            "account_repair.py",
            "AccountRepairService",
            "repair",
            repair_fields - {"_claim_lock", "_claimed"},
        ),
    ]:
        owner = _top_node(_tree(file_name), class_name)
        method = next(node for node in owner.body if getattr(node, "name", None) == method_name)
        deleted_fields = {
            node.targets[0].attr
            for node in ast.walk(method)
            if isinstance(node, ast.Delete)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Attribute)
        }
        assert deleted_fields == deleted
