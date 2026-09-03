"""Equality-pinned auth-storage compatibility ledger for the ADR-0034 migration.

Existing writer/transaction/public-surface guards remain authoritative. This file owns only
callable shape, result projections, compatibility identities, and first-party facade callers.
"""

from __future__ import annotations

import ast
import contextlib
import dataclasses
import inspect
import json
import pickle
import sys
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path

import pytest

import notebooklm.auth as auth
from notebooklm import NotebookLMClient
from notebooklm._auth import (
    account,
    cookies,
    keepalive,
    master_token,
    master_token_bootstrap,
    profile_migration,
    profile_store,
    psidts_recovery,
    recovery,
    refresh,
    storage,
    tokens,
)
from notebooklm._auth.storage_lock import LockState, StorageLockManager
from notebooklm._auth.tokens import AuthTokens
from notebooklm._browser import browser_capture
from notebooklm._runtime import lifecycle
from notebooklm._web.transport.cookie_persistence import CookiePersistence

pytestmark = pytest.mark.repo_lint

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src" / "notebooklm"
REQUIRED = "<required>"
ParameterDescriptor = tuple[str, str, str | None, str]
SignatureDescriptor = tuple[tuple[ParameterDescriptor, ...], str | None]

_LEGACY_RESULT_NAMES = frozenset(
    {
        "CookieSaveResult",
        "LoginWriteOutcome",
        "LoginWriteStatus",
        "WriteOutcome",
        "WriteStatus",
    }
)
_EXPECTED_LEGACY_RESULT_DEPENDENCIES = {
    "CookieSaveResult": frozenset(
        {"_auth/storage.py", "_web/transport/cookie_persistence.py", "auth.py"}
    ),
    "LoginWriteOutcome": frozenset({"_auth/storage.py", "auth.py"}),
    "LoginWriteStatus": frozenset({"_auth/storage.py"}),
    "WriteOutcome": frozenset({"_auth/storage.py"}),
    "WriteStatus": frozenset({"_auth/storage.py"}),
}


def _legacy_result_dependency_paths(root: Path) -> dict[str, frozenset[str]]:
    """Conservatively inventory code references to the v0.x result family.

    Original import names are recorded even when locally aliased; qualified attributes,
    annotations, exact dynamic strings, and star imports are also visible. The proof is
    intentionally path-granular: these compatibility owners are reviewed as whole modules,
    while any new first-party module immediately changes the equality-pinned exception set.
    """
    result: dict[str, set[str]] = {name: set() for name in _LEGACY_RESULT_NAMES}
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            referenced: set[str] = set()
            if isinstance(node, ast.Name):
                referenced.add(node.id)
            elif isinstance(node, ast.Attribute):
                referenced.add(node.attr)
            elif isinstance(node, ast.alias):
                if node.name == "*":
                    referenced.update(_LEGACY_RESULT_NAMES)
                else:
                    referenced.add(node.name.rsplit(".", 1)[-1])
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                referenced.add(node.value)
            for name in referenced & _LEGACY_RESULT_NAMES:
                result[name].add(relative)
    return {name: frozenset(paths) for name, paths in sorted(result.items())}


def _annotation(value: object) -> str | None:
    if value is inspect.Signature.empty:
        return None
    return value if isinstance(value, str) else inspect.formatannotation(value)


def _default(value: object) -> str:
    if value is inspect.Signature.empty:
        return REQUIRED
    if isinstance(value, storage._AccountAction):
        return f"_AccountAction.{value.name}"
    return repr(value)


def _signature(value: Callable[..., object]) -> SignatureDescriptor:
    signature = inspect.signature(value)
    return (
        tuple(
            (
                parameter.name,
                parameter.kind.name,
                _annotation(parameter.annotation),
                _default(parameter.default),
            )
            for parameter in signature.parameters.values()
        ),
        _annotation(signature.return_annotation),
    )


P = "POSITIONAL_OR_KEYWORD"
K = "KEYWORD_ONLY"
R = REQUIRED

# Literal normalized descriptors: no live Signature/default object participates in equality.
EXPECTED_SIGNATURES: dict[str, SignatureDescriptor] = {
    "storage.in_storage_transaction": (
        (
            ("path", P, "Path", R),
            ("body", P, "Callable[[], Any]", R),
            ("log_prefix", K, "str", R),
            ("on_unavailable", K, "_LockUnavailablePolicy", R),
            ("deadline_seconds", K, "float", "90.0"),
        ),
        "Any",
    ),
    "storage.raise_on_lock_unavailable": (
        (("operation", P, "str", R),),
        "_LockUnavailablePolicy",
    ),
    "storage.report_on_lock_unavailable": (
        (("outcome", P, "Any", R),),
        "_LockUnavailablePolicy",
    ),
    "storage.skip_on_lock_unavailable": (
        (("message", P, "str", R),),
        "_LockUnavailablePolicy",
    ),
    "storage._cookie_snapshot_key_variants": (
        (("key", P, "CookieSnapshotKey", R),),
        "set[CookieSnapshotKey]",
    ),
    "storage._stored_cookie_snapshot_key": (
        (("stored_cookie", P, "Any", R),),
        "CookieSnapshotKey | None",
    ),
    "storage._merge_cookies_legacy": (
        (
            ("cookie_jar", P, "httpx.Cookies", R),
            ("storage_data", P, "dict[str, Any]", R),
        ),
        "int",
    ),
    "storage._merge_cookies_with_snapshot": (
        (
            ("cookie_jar", P, "httpx.Cookies", R),
            ("storage_data", P, "dict[str, Any]", R),
            ("original_snapshot", P, "CookieSnapshot", R),
            ("recovery_observation", K, "RecoveryCookieObservation | None", "None"),
        ),
        "tuple[int, frozenset[CookieSnapshotKey]]",
    ),
    "storage.merge_cookie_delta": (
        (
            ("cookie_jar", P, "httpx.Cookies", R),
            ("path", P, "Path | None", "None"),
            ("original_snapshot", K, "CookieSnapshot | None", "None"),
            ("recovery_observation", K, "RecoveryCookieObservation | None", "None"),
            ("return_result", K, "bool", "False"),
        ),
        "bool | CookieSaveResult",
    ),
    "storage.update_account_metadata": (
        (
            ("storage_path", P, "Path", R),
            ("authuser", K, "int", R),
            ("email", K, "str | None", "None"),
            ("only_if_absent", K, "bool", "False"),
        ),
        "bool",
    ),
    "storage.clear_in_band_account": ((("storage_path", P, "Path", R),), "None"),
    "storage.replace_from_remint": (
        (
            ("path", P, "Path", R),
            ("captured_state", P, "dict[str, Any]", R),
            ("carry_account", K, "bool", R),
            ("include_domains", K, "set[str] | None", "None"),
        ),
        "WriteOutcome",
    ),
    "storage.replace_from_login": (
        (
            ("path", P, "Path", R),
            ("state", P, "dict[str, Any]", R),
            ("include_domains", K, "set[str] | None", R),
            ("include_optional", K, "bool", "False"),
            ("account", K, "AccountArg", "_AccountAction.KEEP"),
            ("backup", K, "bool", "False"),
            ("io_policy", K, "object | None", "None"),
        ),
        "LoginWriteOutcome",
    ),
    "storage.persist_minted_jar": (
        (
            ("path", P, "Path", R),
            ("jar", P, "httpx.Cookies", R),
            ("email", K, "str | None", R),
            ("force", K, "bool", "False"),
            ("refuse_unknown_owner", K, "bool", "True"),
        ),
        "None",
    ),
    "master.persist_minted_jar": (
        (
            ("path", P, "Path", R),
            ("jar", P, "httpx.Cookies", R),
            ("email", K, "str | None", R),
            ("force", K, "bool", "False"),
            ("refuse_unknown_owner", K, "bool", "True"),
        ),
        "None",
    ),
    "storage.write_master_token": (
        (
            ("path", P, "Path", R),
            ("email", K, "str", R),
            ("master_token", K, "str", R),
            ("android_id", K, "str", R),
        ),
        "None",
    ),
    "master.write_master_token": (
        (
            ("path", P, "Path", R),
            ("email", K, "str", R),
            ("master_token", K, "str", R),
            ("android_id", K, "str", R),
        ),
        "None",
    ),
    "storage.save_cookies_to_storage": (
        (
            ("cookie_jar", P, "httpx.Cookies", R),
            ("path", P, "Path | None", "None"),
            ("original_snapshot", K, "CookieSnapshot | None", "None"),
            ("recovery_observation", K, "RecoveryCookieObservation | None", "None"),
            ("return_result", K, "bool", "False"),
        ),
        "bool | CookieSaveResult",
    ),
    "storage.snapshot_cookie_jar": ((("cookie_jar", P, "httpx.Cookies", R),), "CookieSnapshot"),
    "storage.advance_cookie_snapshot_after_save": (
        (
            ("original_snapshot", P, "CookieSnapshot | None", R),
            ("post_save_snapshot", P, "CookieSnapshot", R),
            ("cas_rejected_keys", P, "frozenset[CookieSnapshotKey]", R),
        ),
        "CookieSnapshot | None",
    ),
    "CookiePersistence.__init__": (
        (
            ("self", P, None, R),
            ("auth", P, "AuthTokens", R),
            ("default_path", P, "Path | None", R),
            ("save_lock", K, "threading.Lock | None", "None"),
        ),
        "None",
    ),
    "CookiePersistence.capture_open_snapshot": (
        (("self", P, None, R), ("jar", P, "httpx.Cookies", R)),
        "CookieSnapshot",
    ),
    "CookiePersistence._from_store": (
        (
            ("store", P, "ProfileStore | None", R),
            ("save_lock", K, "threading.Lock | None", "None"),
            ("initial_snapshot", K, "CookieSnapshot | None", "None"),
        ),
        "CookiePersistence",
    ),
    "CookiePersistence.register_open_baseline": (
        (
            ("self", P, None, R),
            ("store", P, "ProfileStore", R),
            ("baseline", P, "CookieJar", R),
        ),
        "None",
    ),
    "CookiePersistence._prepare_open_baseline": (
        (
            ("self", P, None, R),
            ("path", P, "Path | None", R),
            ("to_thread", K, "ToThread", R),
        ),
        "None",
    ),
    "CookiePersistence._save_canonical": (
        (
            ("self", P, None, R),
            ("jar", P, "httpx.Cookies", R),
            ("path", P, "Path | None", R),
            ("to_thread", K, "ToThread", R),
        ),
        "None",
    ),
    "CookiePersistence._save_v0_callback": (
        (
            ("self", P, None, R),
            ("jar", P, "httpx.Cookies", R),
            ("path", P, "Path | None", "None"),
            ("save_cookies_to_storage", K, "SaveCookiesToStorage", R),
            ("to_thread", K, "ToThread", R),
        ),
        "None",
    ),
    "NotebookLMClient.__init__": (
        (
            ("self", P, None, R),
            ("auth", P, "AuthTokens", R),
            ("timeout", P, "float", "30.0"),
            ("storage_path", P, "Path | None", "None"),
            ("keepalive", P, "float | None", "None"),
            ("keepalive_min_interval", P, "float", "60.0"),
            ("rate_limit_max_retries", P, "int", "3"),
            ("server_error_max_retries", P, "int", "3"),
            ("limits", P, "ConnectionLimits | None", "None"),
            ("max_concurrent_uploads", P, "int | None", "4"),
            ("max_concurrent_rpcs", P, "int | None", "16"),
            ("upload_timeout", P, "httpx.Timeout | None", "None"),
            ("on_rpc_event", P, "Callable[[RpcTelemetryEvent], object] | None", "None"),
            ("cookie_saver", P, "CookieSaver | None", "None"),
            ("cookie_rotator", P, "CookieRotator | None", "None"),
            ("chat_timeout", P, "float | None", "AUTO_READ_TIMEOUT"),
            ("chat_response_max_bytes", P, "int | None", "268435456"),
            ("import_research_timeout", P, "float | None", "AUTO_READ_TIMEOUT"),
            ("backend", K, "Literal['web', 'android'] | None", "None"),
        ),
        None,
    ),
    "AuthTokens.__init__": (
        (
            ("self", P, None, R),
            ("cookies", P, "DomainCookieMap", R),
            ("csrf_token", P, "str", R),
            ("session_id", P, "str", R),
            ("storage_path", P, "Path | None", "None"),
            ("cookie_jar", P, "httpx.Cookies | None", "None"),
            ("authuser", P, "int", "0"),
            ("cookie_snapshot", P, "CookieSnapshot | None", "None"),
            ("account_email", P, "str | None", "None"),
        ),
        "None",
    ),
    "AuthTokens.from_storage": (
        (
            ("path", P, "Path | None", "None"),
            ("profile", P, "str | None", "None"),
            ("allow_headless", K, "bool", "False"),
        ),
        "AuthTokens",
    ),
    "cookies.build_httpx_cookies_from_storage": (
        (("path", P, "Path | None", "None"),),
        "httpx.Cookies",
    ),
    "cookies._build_cookie_pair_from_storage": (
        (("path", P, "Path | None", "None"),),
        "_LoadedCookiePair",
    ),
    "cookies._build_cookie_pair_from_storage_state": (
        (
            ("storage_state", P, "dict[str, Any]", R),
            ("context", K, "str", "''"),
            ("require_routable", K, "bool", R),
        ),
        "_LoadedCookiePair",
    ),
    "recovery.try_headless_reauth": (
        (
            ("storage_path", K, "Path | None", R),
            ("cookie_jar", K, "httpx.Cookies", R),
            ("allow_headless", K, "bool", R),
        ),
        "bool",
    ),
    "recovery._try_headless_reauth_result": (
        (
            ("storage_path", K, "Path | None", R),
            ("allow_headless", K, "bool", R),
        ),
        "_LoadedCookiePair | None",
    ),
    "recovery.try_master_token_reauth": (
        (
            ("storage_path", K, "Path | None", R),
            ("cookie_jar", K, "httpx.Cookies", R),
        ),
        "bool",
    ),
    "recovery._try_master_token_reauth_result": (
        (("storage_path", K, "Path | None", R),),
        "_LoadedCookiePair | None",
    ),
    "tokens.load_auth_from_storage": ((("path", P, "Path | None", "None"),), "dict[str, str]"),
    "refresh.fetch_tokens": (
        (
            ("cookies", P, "_auth_cookies.CookieInput", R),
            ("storage_path", P, "Path | None", "None"),
            ("profile", P, "str | None", "None"),
            ("authuser", K, "int | None", "None"),
            ("account_email", K, "str | None", "None"),
        ),
        "tuple[str, str]",
    ),
    "refresh.fetch_tokens_passive": (
        (
            ("path", P, "Path | None", "None"),
            ("profile", P, "str | None", "None"),
            ("authuser", K, "int | None", "None"),
            ("account_email", K, "str | None", "None"),
        ),
        "tuple[str, str]",
    ),
    "refresh.fetch_tokens_with_domains": (
        (
            ("path", P, "Path | None", "None"),
            ("profile", P, "str | None", "None"),
            ("authuser", K, "int | None", "None"),
            ("account_email", K, "str | None", "None"),
            ("allow_headless", K, "bool", "False"),
        ),
        "tuple[str, str]",
    ),
    "psidts.validate_with_recovery": (
        (("rookiepy_cookies", P, "list[dict[str, Any]]", R),),
        "tuple[dict[str, Any], ValueError | None]",
    ),
    "psidts.recover_psidts_in_memory": (
        (("rookiepy_cookies", P, "list[dict[str, Any]]", R),),
        "bool",
    ),
    "storage.read_account_metadata": ((("storage_path", P, "Path | None", R),), "dict[str, Any]"),
    "storage.write_account_metadata": (
        (
            ("storage_path", P, "Path", R),
            ("authuser", K, "int", R),
            ("email", K, "str | None", "None"),
        ),
        "None",
    ),
    "storage.clear_account_metadata": ((("storage_path", P, "Path | None", R),), "None"),
    "storage.read_account_metadata_from_storage_state": (
        (("storage_state", P, "Any", R),),
        "dict[str, Any]",
    ),
    "storage.get_authuser_for_storage": ((("storage_path", P, "Path | None", R),), "int"),
    "storage.get_account_email_for_storage": (
        (("storage_path", P, "Path | None", R),),
        "str | None",
    ),
    "storage.drop_legacy_account_key": ((("storage_path", P, "Path", R),), "None"),
    "account.repair_account_metadata_from_playwright_storage": (
        (("storage_path", P, "Path", R), ("page_html", K, "str | None", "None")),
        "PlaywrightAccountRepairResult",
    ),
    "storage.resolve_account_identity": (
        (
            ("has_env_auth", K, "bool", R),
            ("storage_path", K, "Path | None", "None"),
            ("env_auth_storage_state", K, "Any", "None"),
        ),
        "dict[str, Any]",
    ),
    "master.assert_account_writable": (
        (("email", K, "str", R), ("storage_path", K, "Path", R), ("force", K, "bool", "False")),
        "None",
    ),
    "master.exchange_master_token": (
        (("email", P, "str", R), ("oauth_token", P, "str", R), ("android_id", P, "str", R)),
        "str",
    ),
    "master.generate_android_id": ((), "str"),
    "master.mint_cookies": (
        (("email", P, "str", R), ("master_token", P, "str", R), ("android_id", P, "str", R)),
        "httpx.Cookies",
    ),
    "master.read_master_token": ((("path", P, "Path", R),), "dict[str, Any] | None"),
    "master.bootstrap_from_oauth_token": (
        (
            ("email", K, "str", R),
            ("oauth_token", K, "str", R),
            ("storage_path", K, "Path", R),
            ("android_id", K, "str | None", "None"),
            ("verify", K, "bool", "True"),
            ("force", K, "bool", "False"),
        ),
        "int",
    ),
    "master.remint_from_stored_token": ((("storage_path", P, "Path", R),), "httpx.Cookies"),
    "master.bootstrap_missing_storage_from_master_token": (
        (("storage_path", P, "Path", R),),
        "bool",
    ),
    "master.bootstrap_storage_from_master_token": (
        (("storage_path", P, "Path", R),),
        "BootstrapOutcome",
    ),
}

LIVE_SIGNATURES: dict[str, Callable[..., object]] = {
    "storage.in_storage_transaction": storage.in_storage_transaction,
    "storage.raise_on_lock_unavailable": storage.raise_on_lock_unavailable,
    "storage.report_on_lock_unavailable": storage.report_on_lock_unavailable,
    "storage.skip_on_lock_unavailable": storage.skip_on_lock_unavailable,
    "storage._cookie_snapshot_key_variants": storage._cookie_snapshot_key_variants,
    "storage._stored_cookie_snapshot_key": storage._stored_cookie_snapshot_key,
    "storage._merge_cookies_legacy": storage._merge_cookies_legacy,
    "storage._merge_cookies_with_snapshot": storage._merge_cookies_with_snapshot,
    "storage.merge_cookie_delta": storage.merge_cookie_delta,
    "storage.update_account_metadata": storage.update_account_metadata,
    "storage.clear_in_band_account": storage.clear_in_band_account,
    "storage.replace_from_remint": storage.replace_from_remint,
    "storage.replace_from_login": storage.replace_from_login,
    "storage.persist_minted_jar": storage.persist_minted_jar,
    "master.persist_minted_jar": master_token.persist_minted_jar,
    "storage.write_master_token": storage.write_master_token,
    "master.write_master_token": master_token.write_master_token,
    "storage.save_cookies_to_storage": storage.save_cookies_to_storage,
    "storage.snapshot_cookie_jar": storage.snapshot_cookie_jar,
    "storage.advance_cookie_snapshot_after_save": storage.advance_cookie_snapshot_after_save,
    "CookiePersistence.__init__": CookiePersistence.__init__,
    "CookiePersistence._from_store": CookiePersistence._from_store,
    "CookiePersistence.register_open_baseline": CookiePersistence.register_open_baseline,
    "CookiePersistence._prepare_open_baseline": CookiePersistence._prepare_open_baseline,
    "CookiePersistence.capture_open_snapshot": CookiePersistence.capture_open_snapshot,
    "CookiePersistence._save_canonical": CookiePersistence._save_canonical,
    "CookiePersistence._save_v0_callback": CookiePersistence._save_v0_callback,
    "NotebookLMClient.__init__": NotebookLMClient.__init__,
    "AuthTokens.__init__": AuthTokens.__init__,
    "AuthTokens.from_storage": AuthTokens.from_storage,
    "cookies.build_httpx_cookies_from_storage": cookies.build_httpx_cookies_from_storage,
    "cookies._build_cookie_pair_from_storage": cookies._build_cookie_pair_from_storage,
    "cookies._build_cookie_pair_from_storage_state": (
        cookies._build_cookie_pair_from_storage_state
    ),
    "recovery.try_headless_reauth": recovery.try_headless_reauth,
    "recovery._try_headless_reauth_result": recovery._try_headless_reauth_result,
    "recovery.try_master_token_reauth": recovery.try_master_token_reauth,
    "recovery._try_master_token_reauth_result": recovery._try_master_token_reauth_result,
    "tokens.load_auth_from_storage": tokens.load_auth_from_storage,
    "refresh.fetch_tokens": refresh.fetch_tokens,
    "refresh.fetch_tokens_passive": refresh.fetch_tokens_passive,
    "refresh.fetch_tokens_with_domains": refresh.fetch_tokens_with_domains,
    "psidts.validate_with_recovery": psidts_recovery.validate_with_recovery,
    "psidts.recover_psidts_in_memory": psidts_recovery.recover_psidts_in_memory,
    "storage.read_account_metadata": storage.read_account_metadata,
    "storage.write_account_metadata": storage.write_account_metadata,
    "storage.clear_account_metadata": storage.clear_account_metadata,
    "storage.read_account_metadata_from_storage_state": storage.read_account_metadata_from_storage_state,
    "storage.get_authuser_for_storage": storage.get_authuser_for_storage,
    "storage.get_account_email_for_storage": storage.get_account_email_for_storage,
    "storage.drop_legacy_account_key": storage._drop_legacy_account_key,
    "account.repair_account_metadata_from_playwright_storage": account.repair_account_metadata_from_playwright_storage,
    "storage.resolve_account_identity": storage.resolve_account_identity,
    "master.assert_account_writable": master_token.assert_account_writable,
    "master.exchange_master_token": master_token.exchange_master_token,
    "master.generate_android_id": master_token.generate_android_id,
    "master.mint_cookies": master_token.mint_cookies,
    "master.read_master_token": master_token.read_master_token,
    "master.bootstrap_from_oauth_token": master_token.bootstrap_from_oauth_token,
    "master.remint_from_stored_token": master_token.remint_from_stored_token,
    "master.bootstrap_missing_storage_from_master_token": master_token.bootstrap_missing_storage_from_master_token,
    "master.bootstrap_storage_from_master_token": master_token.bootstrap_storage_from_master_token,
}

# Exact supplemental identity ledger for every ADR-0034 inventory callable exposed by
# ``notebooklm.auth`` that is not already owned by the special identity checks below or by
# ``test_public_facade_imports_are_identity_reexports`` (``AuthTokens``).
FACADE_INVENTORY_IDENTITIES: dict[str, Callable[..., object]] = {
    "advance_cookie_snapshot_after_save": storage.advance_cookie_snapshot_after_save,
    "assert_account_writable": master_token.assert_account_writable,
    "bootstrap_missing_storage_from_master_token": (
        master_token.bootstrap_missing_storage_from_master_token
    ),
    "clear_account_metadata": storage.clear_account_metadata,
    "drop_legacy_account_key": storage._drop_legacy_account_key,
    "exchange_master_token": master_token.exchange_master_token,
    "fetch_tokens": refresh.fetch_tokens,
    "fetch_tokens_passive": refresh.fetch_tokens_passive,
    "fetch_tokens_with_domains": refresh.fetch_tokens_with_domains,
    "generate_android_id": master_token.generate_android_id,
    "get_account_email_for_storage": storage.get_account_email_for_storage,
    "get_authuser_for_storage": storage.get_authuser_for_storage,
    "load_auth_from_storage": tokens.load_auth_from_storage,
    "mint_cookies": master_token.mint_cookies,
    "persist_minted_jar": master_token.persist_minted_jar,
    "read_account_metadata_from_storage_state": (storage.read_account_metadata_from_storage_state),
    "read_master_token": master_token.read_master_token,
    "recover_psidts_in_memory": psidts_recovery.recover_psidts_in_memory,
    "resolve_account_identity": storage.resolve_account_identity,
    "save_cookies_to_storage": storage.save_cookies_to_storage,
    "snapshot_cookie_jar": storage.snapshot_cookie_jar,
    "write_account_metadata": storage.write_account_metadata,
    "write_master_token": master_token.write_master_token,
}


def test_compatibility_signatures_are_literal_and_exact() -> None:
    assert set(LIVE_SIGNATURES) == set(EXPECTED_SIGNATURES)
    assert {
        name: _signature(value) for name, value in LIVE_SIGNATURES.items()
    } == EXPECTED_SIGNATURES


def test_result_projections_and_compatibility_value_identities() -> None:
    assert master_token.BootstrapOutcome is master_token_bootstrap.BootstrapOutcome
    assert master_token.BootstrapOutcome.__module__ == ("notebooklm._auth.master_token_bootstrap")
    assert tuple(member.value for member in master_token.BootstrapOutcome) == (
        "minted",
        "present_after_wait",
        "present_on_entry",
        "no_token",
    )
    assert not hasattr(auth, "BootstrapOutcome")
    assert not hasattr(auth, "MasterTokenBootstrapper")
    assert [field.name for field in dataclasses.fields(storage.CookieSaveResult)] == [
        "ok",
        "cas_rejected_keys",
    ]
    assert [field.name for field in dataclasses.fields(storage.WriteOutcome)] == ["status"]
    assert {
        name for name, value in vars(storage.WriteOutcome).items() if isinstance(value, property)
    } == {"ok", "lock_unavailable"}
    assert [field.name for field in dataclasses.fields(storage.LoginWriteOutcome)] == [
        "status",
        "missing_required",
        "present_names",
        "backup_path",
    ]
    assert {
        name
        for name, value in vars(storage.LoginWriteOutcome).items()
        if isinstance(value, property)
    } == {"ok", "lock_unavailable", "required_cookies_dropped"}
    assert storage.KEEP_ACCOUNT is storage._AccountAction.KEEP
    assert storage.CLEAR_ACCOUNT is storage._AccountAction.CLEAR
    assert storage.CookieSnapshotKey._fields == ("name", "domain", "path")
    assert storage.CookieSnapshotValue._fields == ("value", "expires", "secure", "http_only")
    assert storage.CookieSnapshotKey.__module__ == "notebooklm._auth.storage"
    assert storage.CookieSnapshotKey.__qualname__ == "CookieSnapshotKey"
    assert storage.CookieSnapshotValue.__module__ == "notebooklm._auth.storage"
    assert storage.CookieSnapshotValue.__qualname__ == "CookieSnapshotValue"
    assert storage.CookieSnapshot == dict[storage.CookieSnapshotKey, storage.CookieSnapshotValue]
    assert (
        storage.RecoveryCookieObservation == dict[storage.CookieSnapshotKey, frozenset[str | None]]
    )
    assert storage.CookieSaveResult.__module__ == "notebooklm._auth.storage"
    assert storage.CookieSaveResult.__qualname__ == "CookieSaveResult"
    assert storage.CookieSaveResult.__dataclass_params__.frozen is True
    assert storage.in_storage_transaction is profile_store.in_storage_transaction
    assert storage.raise_on_lock_unavailable is profile_store.raise_on_lock_unavailable
    assert storage.report_on_lock_unavailable is profile_store.report_on_lock_unavailable
    assert storage.skip_on_lock_unavailable is profile_store.skip_on_lock_unavailable
    assert not hasattr(browser_capture, "storage")
    assert browser_capture.ReplaceResult is profile_store.ReplaceResult
    assert not hasattr(refresh, "save_cookies_to_storage")
    assert callable(storage.save_cookies_to_storage)
    assert not hasattr(auth, "ProfileStore")
    assert not hasattr(auth, "ProfileAccount")
    assert "MintedSessionWriteRequest" not in storage.__all__
    assert not hasattr(auth, "MintedSessionWriteRequest")
    assert storage.MintedSessionWriteRequest is profile_store.MintedSessionWriteRequest


def test_phase9_closed_values_and_paired_compatibility_owners_are_exact() -> None:
    value_shapes = {
        tokens.InlineAuthSource: ([("document", "ProfileDocument", True)], False),
        tokens.FileAuthSource: (
            [("store", "ProfileStore", True), ("profile", "str | None", True)],
            False,
        ),
        tokens.SessionSeed: (
            [("live", "httpx.Cookies", False), ("baseline", "CookieJar", False)],
            False,
        ),
        tokens.LoadPolicy: ([("allow_headless", "bool", True)], True),
        tokens.TokenAcquisition: (
            [
                ("csrf_token", "str", False),
                ("session_id", "str", False),
                ("live", "httpx.Cookies", False),
                ("baseline_before_fetch_mutations", "CookieJar", False),
                ("final_account", "ProfileAccount | None", True),
            ],
            False,
        ),
        tokens.InlineLoadedAuth: ([("auth", "AuthTokens", True)], False),
        tokens.FileLoadedAuth: (
            [
                ("auth", "AuthTokens", True),
                ("store", "ProfileStore", True),
                ("persistence_baseline", "CookieJar", False),
            ],
            False,
        ),
        cookies._LoadedCookiePair: (
            [("live", "httpx.Cookies", False), ("baseline", "CookieJar", False)],
            False,
        ),
        profile_migration._LoadedProfilePair: (
            [("cookies", "_LoadedCookiePair", False), ("account", "ProfileAccount | None", True)],
            False,
        ),
        recovery.ColdRecoveryResult: (
            [
                ("cookie_jar", "httpx.Cookies", False),
                ("snapshot", "CookieSnapshot", False),
                ("baseline", "CookieJar", False),
            ],
            False,
        ),
    }
    for value_type, (fields, repr_enabled) in value_shapes.items():
        assert value_type.__dataclass_params__.frozen is True
        assert value_type.__dataclass_params__.repr is repr_enabled
        assert tuple(value_type.__slots__) == tuple(name for name, _type, _repr in fields)
        assert [
            (field.name, _annotation(field.type), field.repr)
            for field in dataclasses.fields(value_type)
        ] == fields

    assert tokens.ResolvedAuthSource.__args__ == (
        tokens.InlineAuthSource,
        tokens.FileAuthSource,
    )
    assert tokens.LoadedAuth.__args__ == (
        tokens.InlineLoadedAuth,
        tokens.FileLoadedAuth,
    )

    internal_names = {
        "InlineAuthSource",
        "FileAuthSource",
        "ResolvedAuthSource",
        "SessionSeed",
        "LoadPolicy",
        "TokenAcquisition",
        "InlineLoadedAuth",
        "FileLoadedAuth",
        "LoadedAuth",
        "_LoadedCookiePair",
        "ColdRecoveryResult",
        "ColdRecoveryCoordinator",
        "_build_cookie_pair_from_storage",
        "_build_cookie_pair_from_storage_state",
        "_try_headless_reauth_result",
        "_try_master_token_reauth_result",
    }
    assert internal_names.isdisjoint(tokens.__all__)
    assert internal_names.isdisjoint(auth.__all__)
    assert all(not hasattr(auth, name) for name in internal_names)

    assert auth.build_httpx_cookies_from_storage is cookies.build_httpx_cookies_from_storage
    exact_identities = {
        cookies.build_httpx_cookies_from_storage: (
            "notebooklm._auth.cookies",
            "build_httpx_cookies_from_storage",
        ),
        cookies._build_cookie_pair_from_storage: (
            "notebooklm._auth.cookies",
            "_build_cookie_pair_from_storage",
        ),
        cookies._build_cookie_pair_from_storage_state: (
            "notebooklm._auth.cookies",
            "_build_cookie_pair_from_storage_state",
        ),
        recovery.try_headless_reauth: (
            "notebooklm._auth.recovery",
            "try_headless_reauth",
        ),
        recovery._try_headless_reauth_result: (
            "notebooklm._auth.recovery",
            "_try_headless_reauth_result",
        ),
        recovery.try_master_token_reauth: (
            "notebooklm._auth.recovery",
            "try_master_token_reauth",
        ),
        recovery._try_master_token_reauth_result: (
            "notebooklm._auth.recovery",
            "_try_master_token_reauth_result",
        ),
    }
    assert {
        owner: (owner.__module__, owner.__qualname__) for owner in exact_identities
    } == exact_identities


def test_cookie_save_delegate_remains_same_module_and_late_bound() -> None:
    source = ast.parse(inspect.getsource(storage.save_cookies_to_storage))
    calls = [
        node.func.id
        for node in ast.walk(source)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    ]
    assert calls == ["merge_cookie_delta"]


def test_minted_facade_identities_and_master_wrapper_late_lookup_are_exact() -> None:
    assert auth.persist_minted_jar is master_token.persist_minted_jar
    assert master_token.persist_minted_jar is not storage.persist_minted_jar
    assert auth.write_master_token is master_token.write_master_token
    assert master_token.write_master_token is not storage.write_master_token

    source = ast.parse(inspect.getsource(master_token.persist_minted_jar))
    imports = [node for node in ast.walk(source) if isinstance(node, ast.ImportFrom)]
    assert [
        (node.level, node.module, [(alias.name, alias.asname) for alias in node.names])
        for node in imports
    ] == [(1, None, [("storage", None)])]
    calls = [
        ast.unparse(node.func)
        for node in ast.walk(source)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name | ast.Attribute)
    ]
    assert calls == ["storage.persist_minted_jar"]

    writer_source = ast.parse(inspect.getsource(master_token.write_master_token))
    writer_imports = [node for node in ast.walk(writer_source) if isinstance(node, ast.ImportFrom)]
    assert [
        (node.level, node.module, [(alias.name, alias.asname) for alias in node.names])
        for node in writer_imports
    ] == [(1, None, [("storage", None)])]
    writer_calls = [
        node
        for node in ast.walk(writer_source)
        if isinstance(node, ast.Call) and ast.unparse(node.func) == "storage.write_master_token"
    ]
    assert len(writer_calls) == 1
    writer_call = writer_calls[0]
    assert [ast.unparse(arg) for arg in writer_call.args] == ["path"]
    assert [(item.arg, ast.unparse(item.value)) for item in writer_call.keywords] == [
        ("email", "email"),
        ("master_token", "master_token"),
        ("android_id", "android_id"),
    ]


def test_lock_wrapper_shapes_ownership_and_projections_are_exact(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert str(inspect.signature(storage._file_lock)) == (
        "(lock_path: 'Path', *, blocking: 'bool', log_prefix: 'str') -> 'Iterator[str]'"
    )
    assert str(inspect.signature(keepalive._file_lock)) == (
        "(lock_path: 'Path', *, blocking: 'bool', log_prefix: 'str') -> 'Iterator[str]'"
    )
    assert str(inspect.signature(storage._file_lock_exclusive)) == (
        "(lock_path: 'Path') -> 'Iterator[None]'"
    )
    assert str(inspect.signature(keepalive._file_lock_try_exclusive)) == (
        "(lock_path: 'Path') -> 'Iterator[bool]'"
    )
    assert auth._file_lock is storage._file_lock
    assert auth._file_lock_exclusive is storage._file_lock_exclusive
    assert auth._file_lock_try_exclusive is keepalive._file_lock_try_exclusive
    assert storage._file_lock is not keepalive._file_lock
    assert storage._STORAGE_LOCKS is keepalive._STORAGE_LOCKS
    assert storage._STORAGE_LOCKS is StorageLockManager.process_default()
    assert profile_store._STORAGE_LOCKS is StorageLockManager.process_default()
    assert not hasattr(storage, "_FLOCK_UNAVAILABLE_WARNED")
    assert not hasattr(auth, "_FLOCK_UNAVAILABLE_WARNED")
    assert psidts_recovery._file_lock_try_exclusive is keepalive._file_lock_try_exclusive
    assert refresh._wait_for_refresh_holder is keepalive._wait_for_refresh_holder
    assert refresh.RefreshCmdDeps().flock() is keepalive._file_lock_try_exclusive

    class FixedLocks:
        def __init__(self, state: LockState) -> None:
            self.state = state

        def acquire(self, request: object) -> contextlib.AbstractContextManager[LockState]:
            return contextlib.nullcontext(self.state)

    path = tmp_path / ".storage_state.json.lock"
    for state in LockState:
        monkeypatch.setattr(storage, "_STORAGE_LOCKS", FixedLocks(state))
        with storage._file_lock(path, blocking=False, log_prefix="compat") as projected:
            assert projected == state.value
        monkeypatch.setattr(keepalive, "_STORAGE_LOCKS", FixedLocks(state))
        with keepalive._file_lock(path, blocking=False, log_prefix="compat") as projected:
            assert projected == state.value
        with keepalive._file_lock_try_exclusive(path) as projected:
            assert projected is (state is not LockState.CONTENDED)


def test_legacy_account_value_shape_identity_and_pickle_contract() -> None:
    assert dataclasses.is_dataclass(storage.AccountRecord)
    assert storage.AccountRecord.__dataclass_params__.frozen is True
    assert not hasattr(storage.AccountRecord, "__slots__")
    assert [field.name for field in dataclasses.fields(storage.AccountRecord)] == [
        "authuser",
        "email",
    ]
    assert str(inspect.signature(storage.AccountRecord)) == (
        "(authuser: 'int', email: 'str | None' = None) -> None"
    )
    assert storage.AccountRecord.__module__ == "notebooklm._auth.storage"
    assert storage.AccountRecord.__qualname__ == "AccountRecord"

    odd = storage.AccountRecord(authuser=True, email=17)  # type: ignore[arg-type]
    restored = pickle.loads(pickle.dumps(odd))
    assert type(restored) is storage.AccountRecord
    assert restored == odd
    assert restored.authuser is True
    assert restored.email == 17

    assert [(member.name, member.value) for member in storage._AccountAction] == [
        ("KEEP", "keep"),
        ("CLEAR", "clear"),
    ]
    assert storage._AccountAction.__module__ == "notebooklm._auth.storage"
    assert storage._AccountAction.__qualname__ == "_AccountAction"
    assert storage.KEEP_ACCOUNT is storage._AccountAction.KEEP
    assert storage.CLEAR_ACCOUNT is storage._AccountAction.CLEAR
    assert pickle.loads(pickle.dumps(storage.KEEP_ACCOUNT)) is storage.KEEP_ACCOUNT
    assert pickle.loads(pickle.dumps(storage.CLEAR_ACCOUNT)) is storage.CLEAR_ACCOUNT
    assert storage.AccountArg.__args__ == (storage._AccountAction, storage.AccountRecord)


def test_legacy_account_facade_shim_and_default_identities_are_exact() -> None:
    assert storage.sys is sys
    assert auth.AccountRecord is storage.AccountRecord
    assert auth.KEEP_ACCOUNT is storage.KEEP_ACCOUNT
    assert auth.CLEAR_ACCOUNT is storage.CLEAR_ACCOUNT
    assert auth.replace_from_login is storage.replace_from_login
    assert storage._drop_legacy_account_key is profile_migration._drop_legacy_account_key
    assert auth.drop_legacy_account_key is profile_migration._drop_legacy_account_key

    internal_components = {
        "LegacyAccountContext",
        "LegacyAccountMigrator",
        "LegacyPromotionScheduler",
        "LoginProfileWriter",
        "AccountMetadataWriter",
        "InBandAccount",
        "LegacyAccount",
        "NoAccount",
        "Promoted",
        "AlreadyInBand",
        "NoLegacyRecord",
        "PromotionFailed",
    }
    assert internal_components.isdisjoint(storage.__all__)
    assert all(not hasattr(auth, name) for name in internal_components)

    account_default = inspect.signature(storage.replace_from_login).parameters["account"].default
    assert account_default is storage.KEEP_ACCOUNT


def test_legacy_result_dependencies_are_only_compatibility_owners() -> None:
    assert _legacy_result_dependency_paths(SRC_ROOT) == _EXPECTED_LEGACY_RESULT_DEPENDENCIES


def test_legacy_result_dependency_inventory_bites_on_aliases_and_dynamic_access(
    tmp_path: Path,
) -> None:
    package = tmp_path / "notebooklm"
    package.mkdir()
    (package / "aliases.py").write_text(
        "from notebooklm._auth.storage import WriteOutcome as WO\n"
        "import notebooklm.auth as facade\n"
        "RESULT = 'LoginWriteOutcome'\n"
        "def use(value):\n"
        "    return WO, facade.LoginWriteStatus, getattr(facade, RESULT), "
        "facade.__dict__['CookieSaveResult']\n",
        encoding="utf-8",
    )
    (package / "star.py").write_text("from notebooklm._auth.storage import *\n", encoding="utf-8")

    inventory = _legacy_result_dependency_paths(package)
    assert inventory["WriteOutcome"] == frozenset({"aliases.py", "star.py"})
    assert inventory["LoginWriteOutcome"] == frozenset({"aliases.py", "star.py"})
    assert inventory["LoginWriteStatus"] == frozenset({"aliases.py", "star.py"})
    assert inventory["CookieSaveResult"] == frozenset({"aliases.py", "star.py"})
    assert inventory["WriteStatus"] == frozenset({"star.py"})


def test_legacy_account_writer_preserves_odd_values_and_clears_unknowns(tmp_path: Path) -> None:
    state = {
        "cookies": [
            {"name": "SID", "value": "sid", "domain": ".google.com", "path": "/"},
            {
                "name": "__Secure-1PSIDTS",
                "value": "ts",
                "domain": ".google.com",
                "path": "/",
            },
        ],
        "origins": [],
    }

    odd_path = tmp_path / "odd" / "storage_state.json"
    odd = storage.AccountRecord(authuser=True, email=17)  # type: ignore[arg-type]
    outcome = storage.replace_from_login(odd_path, state, include_domains=None, account=odd)
    assert outcome.ok
    odd_document = json.loads(odd_path.read_text(encoding="utf-8"))
    assert odd_document["notebooklm"]["account"] == {"authuser": True, "email": 17}

    unknown_path = tmp_path / "unknown" / "storage_state.json"
    unknown_state = {
        **state,
        "notebooklm": {
            "version": 1,
            "account": {"authuser": 8, "email": "stale@example.com"},
        },
    }
    unknown_outcome = storage.replace_from_login(
        unknown_path,
        unknown_state,
        include_domains=None,
        account=object(),  # type: ignore[arg-type]
    )
    assert unknown_outcome.ok
    unknown_document = json.loads(unknown_path.read_text(encoding="utf-8"))
    assert unknown_document["notebooklm"] == {
        "account_route_cleared": True,
        "version": 1,
    }


def test_facade_identities_and_explicit_cookie_save_adapter() -> None:
    assert auth.master_token_bootstrap is master_token.bootstrap_from_oauth_token
    assert auth.master_token_remint is master_token.remint_from_stored_token
    assert auth.replace_from_login is storage.replace_from_login
    assert auth.read_account_metadata is storage.read_account_metadata
    assert (
        auth.repair_account_metadata_from_playwright_storage
        is account.repair_account_metadata_from_playwright_storage
    )
    assert auth.validate_with_recovery is psidts_recovery.validate_with_recovery
    from notebooklm import _runtime
    from notebooklm._web.transport import cookie_persistence as persistence_module

    assert not hasattr(lifecycle, "_default_cookie_saver")
    assert not hasattr(persistence_module, "_default_cookie_saver")
    assert not hasattr(_runtime, "_default_cookie_saver")
    assert not hasattr(persistence_module, "_canonical_cookie_saver_is_current")
    assert "cookie_saver" in inspect.signature(NotebookLMClient.__init__).parameters
    assert (
        "save_cookies_to_storage"
        in inspect.signature(CookiePersistence._save_v0_callback).parameters
    )


def test_all_remaining_facade_inventory_callables_are_exact_identity_reexports() -> None:
    assert set(FACADE_INVENTORY_IDENTITIES) == {
        "advance_cookie_snapshot_after_save",
        "assert_account_writable",
        "bootstrap_missing_storage_from_master_token",
        "clear_account_metadata",
        "drop_legacy_account_key",
        "exchange_master_token",
        "fetch_tokens",
        "fetch_tokens_passive",
        "fetch_tokens_with_domains",
        "generate_android_id",
        "get_account_email_for_storage",
        "get_authuser_for_storage",
        "load_auth_from_storage",
        "mint_cookies",
        "persist_minted_jar",
        "read_account_metadata_from_storage_state",
        "read_master_token",
        "recover_psidts_in_memory",
        "resolve_account_identity",
        "save_cookies_to_storage",
        "snapshot_cookie_jar",
        "write_account_metadata",
        "write_master_token",
    }
    assert {
        name: getattr(auth, name) is canonical
        for name, canonical in FACADE_INVENTORY_IDENTITIES.items()
    } == dict.fromkeys(FACADE_INVENTORY_IDENTITIES, True)


EXPECTED_DIRECT_CALLERS = {
    "AuthTokens": [
        "src/notebooklm/__init__.py",
        "src/notebooklm/_client_assembly.py",
        "src/notebooklm/_runtime/init.py",
        "src/notebooklm/_web/transport/auth.py",
        "src/notebooklm/_web/transport/cookie_persistence.py",
        "src/notebooklm/_web/transport/kernel.py",
        "src/notebooklm/_web/transport/lifecycle.py",
        "src/notebooklm/cli/auth_runtime.py",
        "src/notebooklm/cli/helpers.py",
        "src/notebooklm/cli/language_cmd.py",
        "src/notebooklm/client.py",
    ],
    "MasterTokenError": [
        "src/notebooklm/_app/master_token.py",
        "src/notebooklm/cli/session_cmd.py",
    ],
    "app_host_scope_note": ["src/notebooklm/cli/services/login/cookie_jar.py"],
    "build_cookie_jar": ["src/notebooklm/_web/transport/kernel.py"],
    "build_httpx_cookies_from_storage": ["src/notebooklm/cli/auth_runtime.py"],
    "cookie_names_from_storage": [
        "src/notebooklm/_app/doctor.py",
        "src/notebooklm/cli/services/login/cookie_writes.py",
        "src/notebooklm/cli/services/login/refresh.py",
    ],
    "capture_browser_oauth_token": ["src/notebooklm/cli/services/login/master_token.py"],
    "check_headless_reauth_readiness": ["src/notebooklm/cli/doctor_cmd.py"],
    "fetch_tokens_passive": [
        "src/notebooklm/_app/auth_check.py",
        "src/notebooklm/cli/playwright_login_io.py",
    ],
    "fetch_tokens_with_domains": [
        "src/notebooklm/_app/auth_check.py",
        "src/notebooklm/cli/auth_runtime.py",
        "src/notebooklm/cli/services/login/__init__.py",
        "src/notebooklm/cli/services/login/cookie_writes.py",
        "src/notebooklm/cli/services/login/refresh.py",
        "src/notebooklm/cli/session_cmd.py",
    ],
    "filter_storage_state_cookies_by_domain_policy": ["src/notebooklm/cli/_cookie_import.py"],
    "missing_cookies_hint": [
        "src/notebooklm/cli/services/login/cookie_writes.py",
        "src/notebooklm/cli/services/login/refresh.py",
    ],
    "read_account_metadata": [
        "src/notebooklm/_app/profile.py",
        "src/notebooklm/cli/services/login/refresh.py",
    ],
    "repair_account_metadata_from_playwright_storage": ["src/notebooklm/_app/profile.py"],
    "ReplaceResult": ["src/notebooklm/cli/services/login/refresh.py"],
    "replace_profile_from_login": [
        "src/notebooklm/cli/_cookie_import.py",
        "src/notebooklm/cli/services/login/cookie_writes.py",
        "src/notebooklm/cli/services/login/refresh.py",
    ],
    "resolve_account_identity": [
        "src/notebooklm/_app/auth_check.py",
        "src/notebooklm/cli/auth_runtime.py",
    ],
    "validate_with_recovery": [
        "src/notebooklm/cli/services/login/cookie_jar.py",
        "src/notebooklm/cli/services/login/cookie_writes.py",
        "src/notebooklm/cli/services/login/refresh.py",
    ],
}

EXPECTED_ALIAS_CALLERS = {
    "Account": ["src/notebooklm/_app/login_cookie.py"],
    "GOOGLE_REGIONAL_CCTLDS": ["src/notebooklm/_app/login_cookie.py"],
    "MINIMUM_REQUIRED_COOKIES": ["src/notebooklm/_app/login_cookie.py"],
    "OPTIONAL_COOKIE_DOMAINS_BY_LABEL": ["src/notebooklm/_app/login_cookie.py"],
    "REQUIRED_COOKIE_DOMAINS": ["src/notebooklm/_app/login_cookie.py"],
    "ReplaceResult": ["src/notebooklm/_app/login_cookie.py"],
    "_has_valid_secondary_binding": ["src/notebooklm/_app/login_cookie.py"],
    "_sanitize_cookie_entry": ["src/notebooklm/_app/login_cookie.py"],
    "_sanitized_auth_entries": [
        "src/notebooklm/_app/auth_check.py",
        "src/notebooklm/_app/login_cookie.py",
    ],
    "_storage_entry_to_cookie": [
        "src/notebooklm/_app/auth_check.py",
        "src/notebooklm/_app/login_cookie.py",
    ],
    "_validate_cookie_shape": ["src/notebooklm/_app/login_cookie.py"],
    "_validate_routable_entries": [
        "src/notebooklm/_app/auth_check.py",
        "src/notebooklm/_app/login_cookie.py",
    ],
    "assert_account_writable": ["src/notebooklm/_app/master_token.py"],
    "bootstrap_missing_storage_from_master_token": ["src/notebooklm/_app/master_token.py"],
    "browser_login_channels": ["src/notebooklm/_app/login_browser.py"],
    "build_cookie_jar": [
        "src/notebooklm/_app/login_cookie.py",
        "src/notebooklm/cli/helpers.py",
    ],
    "convert_rookiepy_cookies_to_storage_state": ["src/notebooklm/_app/login_cookie.py"],
    "cookie_names_from_storage": ["src/notebooklm/_app/login_cookie.py"],
    "enumerate_accounts": ["src/notebooklm/_app/login_cookie.py"],
    "ensure_browser_login_available": ["src/notebooklm/_app/login_browser.py"],
    "extract_cookies_from_storage": [
        "src/notebooklm/_app/auth_check.py",
        "src/notebooklm/_app/login_cookie.py",
    ],
    "extract_cookies_with_domains": ["src/notebooklm/_app/login_cookie.py"],
    "load_auth_from_storage": ["src/notebooklm/cli/helpers.py"],
    "master_token_bootstrap": ["src/notebooklm/_app/master_token.py"],
    "master_token_remint": ["src/notebooklm/_app/master_token.py"],
    "missing_cookies_hint": ["src/notebooklm/_app/login_cookie.py"],
    "read_master_token": ["src/notebooklm/_app/master_token.py"],
    "run_browser_login_capture": ["src/notebooklm/_app/login_browser.py"],
}


def _resolved_module(path: Path, node: ast.ImportFrom) -> str:
    if node.level == 0:
        return node.module or ""
    relative = path.relative_to(REPO_ROOT / "src").with_suffix("")
    package = list(relative.parts[:-1])
    keep = len(package) - (node.level - 1)
    base = package[:keep]
    if node.module:
        base.extend(node.module.split("."))
    return ".".join(base)


def _direct_callers(root: Path) -> tuple[dict[str, list[str]], list[str]]:
    result: dict[str, set[str]] = defaultdict(set)
    direct_modules: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if path == root / "auth.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        relative = path.relative_to(REPO_ROOT).as_posix()
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and _resolved_module(path, node) == "notebooklm.auth"
            ):
                for imported in node.names:
                    result[imported.name].add(relative)
            elif isinstance(node, ast.Import):
                for imported in node.names:
                    if imported.name == "notebooklm.auth":
                        direct_modules.append(relative)
    return {name: sorted(paths) for name, paths in sorted(result.items())}, sorted(
        set(direct_modules)
    )


BindingState = frozenset[bool | None]
BindingSnapshot = tuple[dict[str, BindingState], ...]


@dataclasses.dataclass
class _LexicalScope:
    kind: str
    # ``None`` means this scope has no binding on that feasible path, so lookup
    # continues outward. ``False`` is a shadowing non-facade/unbound binding and
    # ``True`` is the facade. Unions preserve branch alternatives.
    bindings: dict[str, BindingState]
    globals: set[str] = dataclasses.field(default_factory=set)
    nonlocals: set[str] = dataclasses.field(default_factory=set)
    possible_facades: set[str] = dataclasses.field(default_factory=set)
    call_effects: dict[str, tuple[tuple[int, str], ...]] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class _FlowResult:
    normal: list[BindingSnapshot] = dataclasses.field(default_factory=list)
    breaks: list[BindingSnapshot] = dataclasses.field(default_factory=list)
    continues: list[BindingSnapshot] = dataclasses.field(default_factory=list)
    returns: list[BindingSnapshot] = dataclasses.field(default_factory=list)
    exceptions: list[BindingSnapshot] = dataclasses.field(default_factory=list)

    def absorb_abrupt(self, other: _FlowResult) -> None:
        self.breaks.extend(other.breaks)
        self.continues.extend(other.continues)
        self.returns.extend(other.returns)
        self.exceptions.extend(other.exceptions)


class _AliasUseCollector(ast.NodeVisitor):
    def __init__(self, path: Path) -> None:
        self.path = path
        self.scopes = [_LexicalScope("module", {})]
        self.attributes: set[str] = set()
        self._exception_sinks: list[list[BindingSnapshot]] = []

    def visit(self, node: ast.AST) -> object:
        if isinstance(node, ast.expr) and self._exception_sinks:
            before = self._snapshot_bindings()
            for sink in self._exception_sinks:
                sink.append(before[: len(sink[0])])
            result = super().visit(node)
            after = self._snapshot_bindings()
            for sink in self._exception_sinks:
                sink.append(after[: len(sink[0])])
            return result
        return super().visit(node)

    def _bind_at(self, index: int, name: str, is_facade: bool) -> None:
        scope = self.scopes[index]
        if name in scope.globals:
            self.scopes[0].bindings[name] = frozenset({is_facade})
            return
        if name in scope.nonlocals:
            for outer in reversed(self.scopes[:index]):
                if outer.kind == "function" and name in outer.bindings:
                    outer.bindings[name] = frozenset({is_facade})
                    return
            return
        scope.bindings[name] = frozenset({is_facade})

    def _bind(self, name: str, is_facade: bool) -> None:
        self._bind_at(len(self.scopes) - 1, name, is_facade)

    def _unbind_at(self, index: int, name: str) -> None:
        scope = self.scopes[index]
        if name in scope.globals:
            self.scopes[0].bindings.pop(name, None)
            return
        if name in scope.nonlocals:
            for outer in reversed(self.scopes[:index]):
                if outer.kind == "function" and name in outer.bindings:
                    outer.bindings[name] = frozenset({False})
                    return
            return
        if scope.kind == "function":
            # The local slot still exists lexically but is unbound at runtime.
            scope.bindings[name] = frozenset({False})
        else:
            # Module/class deletion removes the namespace entry. Class LOAD_NAME
            # may consequently fall through to the enclosing non-class scope.
            scope.bindings.pop(name, None)

    def _unbind(self, name: str) -> None:
        self._unbind_at(len(self.scopes) - 1, name)

    def _lookup(self, name: str) -> bool:
        return self._lookup_from(len(self.scopes) - 1, name, current=True)

    def _lookup_from(self, index: int, name: str, *, current: bool) -> bool:
        scope = self.scopes[index]
        if name in scope.globals:
            module = self.scopes[0]
            return (
                True in module.bindings.get(name, frozenset({None}))
                or name in module.possible_facades
            )
        if name in scope.nonlocals:
            for outer_index in range(index - 1, 0, -1):
                outer = self.scopes[outer_index]
                if outer.kind == "function" and name in outer.bindings:
                    return True in outer.bindings[name] or name in outer.possible_facades
            return False
        values = scope.bindings.get(name, frozenset({None}))
        if scope.kind != "class" or current:
            if True in values:
                return True
            if not current and name in scope.possible_facades:
                return True
            if None not in values:
                return False
        for outer_index in range(index - 1, -1, -1):
            outer = self.scopes[outer_index]
            if outer.kind == "class":
                continue
            return self._lookup_from(outer_index, name, current=False)
        return False

    def _snapshot_bindings(self) -> BindingSnapshot:
        return tuple(dict(scope.bindings) for scope in self.scopes)

    def _restore_bindings(self, snapshot: BindingSnapshot) -> None:
        assert len(snapshot) == len(self.scopes)
        for scope, bindings in zip(self.scopes, snapshot, strict=True):
            scope.bindings = dict(bindings)

    def _merged_snapshot(self, snapshots: list[BindingSnapshot]) -> BindingSnapshot:
        assert snapshots
        assert all(len(snapshot) == len(self.scopes) for snapshot in snapshots)
        result: list[dict[str, BindingState]] = []
        for index in range(len(self.scopes)):
            names = {name for snapshot in snapshots for name in snapshot[index]}
            merged: dict[str, BindingState] = {}
            for name in names:
                values = frozenset(
                    value
                    for snapshot in snapshots
                    for value in snapshot[index].get(name, frozenset({None}))
                )
                if values != frozenset({None}):
                    merged[name] = values
            result.append(merged)
        return tuple(result)

    def _merge_bindings(self, snapshots: list[BindingSnapshot]) -> None:
        self._restore_bindings(self._merged_snapshot(snapshots))

    def _evaluate_expression(
        self, snapshot: BindingSnapshot, node: ast.expr
    ) -> tuple[BindingSnapshot, list[BindingSnapshot]]:
        self._restore_bindings(snapshot)
        exceptions = [snapshot]
        self._exception_sinks.append(exceptions)
        self.visit(node)
        self._exception_sinks.pop()
        return self._snapshot_bindings(), exceptions

    def _visit_block(
        self, incoming: list[BindingSnapshot], statements: list[ast.stmt]
    ) -> _FlowResult:
        result = _FlowResult(normal=incoming)
        for statement in statements:
            if not result.normal:
                break
            entry = self._merged_snapshot(result.normal)
            statement_flow = self._flow_statement(entry, statement)
            result.normal = statement_flow.normal
            result.absorb_abrupt(statement_flow)
        return result

    def _flow_statement(self, entry: BindingSnapshot, node: ast.stmt) -> _FlowResult:
        if isinstance(node, ast.If):
            return self._flow_if(entry, node)
        if isinstance(node, ast.For | ast.AsyncFor):
            return self._flow_for(entry, node)
        if isinstance(node, ast.While):
            return self._flow_while(entry, node)
        if isinstance(node, ast.Try) or type(node).__name__ == "TryStar":
            return self._flow_try(entry, node)
        if isinstance(node, ast.Match):
            return self._flow_match(entry, node)
        if isinstance(node, ast.With | ast.AsyncWith):
            return self._flow_with(entry, node)
        if isinstance(node, ast.Break):
            return _FlowResult(breaks=[entry])
        if isinstance(node, ast.Continue):
            return _FlowResult(continues=[entry])
        if isinstance(node, ast.Return):
            if node.value is None:
                return _FlowResult(returns=[entry])
            exit_, exceptions = self._evaluate_expression(entry, node.value)
            return _FlowResult(returns=[exit_], exceptions=exceptions)
        if isinstance(node, ast.Raise):
            state = entry
            exceptions: list[BindingSnapshot] = [entry]
            for value in (node.exc, node.cause):
                if value is not None:
                    state, raised = self._evaluate_expression(state, value)
                    exceptions.extend(raised)
            exceptions.append(state)
            return _FlowResult(exceptions=exceptions)

        self._restore_bindings(entry)
        exceptions = [entry]
        self._exception_sinks.append(exceptions)
        self.visit(node)
        self._exception_sinks.pop()
        return _FlowResult(normal=[self._snapshot_bindings()], exceptions=exceptions)

    def _flow_if(self, entry: BindingSnapshot, node: ast.If) -> _FlowResult:
        branch_entry, test_exceptions = self._evaluate_expression(entry, node.test)
        body = self._visit_block([branch_entry], node.body)
        orelse = (
            self._visit_block([branch_entry], node.orelse)
            if node.orelse
            else _FlowResult(normal=[branch_entry])
        )
        result = _FlowResult(normal=[*body.normal, *orelse.normal], exceptions=test_exceptions)
        result.absorb_abrupt(body)
        result.absorb_abrupt(orelse)
        return result

    def _flow_with(self, entry: BindingSnapshot, node: ast.With | ast.AsyncWith) -> _FlowResult:
        state = entry
        exceptions: list[BindingSnapshot] = []
        for item in node.items:
            state, raised = self._evaluate_expression(state, item.context_expr)
            exceptions.extend(raised)
            if item.optional_vars is not None:
                self._restore_bindings(state)
                self.visit(item.optional_vars)
                state = self._snapshot_bindings()
        body = self._visit_block([state], node.body)
        # ``__exit__``/``__aexit__`` may suppress any body exception. Preserve
        # those states as both exceptional and feasible normal exits.
        body.normal.extend(body.exceptions)
        body.exceptions.extend(exceptions)
        return body

    def visit_Module(self, node: ast.Module) -> None:
        self.scopes[0].possible_facades = _possible_facade_bindings(self.path, node.body)
        result = self._visit_block([self._snapshot_bindings()], node.body)
        if result.normal:
            self._merge_bindings(result.normal)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = _resolved_module(self.path, node)
        for imported in node.names:
            bound = imported.asname or imported.name
            self._bind(bound, module == "notebooklm" and imported.name == "auth")
            snapshot = self._snapshot_bindings()
            for sink in self._exception_sinks:
                sink.append(snapshot[: len(sink[0])])

    def visit_Import(self, node: ast.Import) -> None:
        for imported in node.names:
            self._bind(
                imported.asname or imported.name.split(".")[0],
                imported.name == "notebooklm.auth" and imported.asname is not None,
            )
            snapshot = self._snapshot_bindings()
            for sink in self._exception_sinks:
                sink.append(snapshot[: len(sink[0])])

    def _visit_definition_header(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        for default in (*node.args.defaults, *node.args.kw_defaults):
            if default is not None:
                self.visit(default)
        arguments = (
            *node.args.posonlyargs,
            *node.args.args,
            *node.args.kwonlyargs,
            *([node.args.vararg] if node.args.vararg else []),
            *([node.args.kwarg] if node.args.kwarg else []),
        )
        for argument in arguments:
            if argument.annotation is not None:
                self.visit(argument.annotation)
        if node.returns is not None:
            self.visit(node.returns)
        for type_parameter in getattr(node, "type_params", ()):
            self.visit(type_parameter)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self._visit_definition_header(node)
        self._bind(node.name, False)
        globals_, nonlocals = _scope_directives(node)
        effects: list[tuple[int, str]] = []
        facade_effects = _explicit_facade_import_effects(self.path, node)
        effects.extend((0, name) for name in facade_effects & globals_)
        for name in facade_effects & nonlocals:
            for index in range(len(self.scopes) - 1, 0, -1):
                if self.scopes[index].kind == "function" and name in self.scopes[index].bindings:
                    effects.append((index, name))
                    break
        if effects:
            self.scopes[-1].call_effects[node.name] = tuple(effects)
        enclosing = self._snapshot_bindings()
        local = dict.fromkeys(_scope_bound_names(node) - globals_ - nonlocals, frozenset({False}))
        for argument in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs):
            local[argument.arg] = frozenset({False})
        if node.args.vararg:
            local[node.args.vararg.arg] = frozenset({False})
        if node.args.kwarg:
            local[node.args.kwarg.arg] = frozenset({False})
        self.scopes.append(
            _LexicalScope(
                "function",
                local,
                globals_,
                nonlocals,
                _possible_facade_bindings(self.path, node.body),
            )
        )
        enclosing_exception_sinks = self._exception_sinks
        self._exception_sinks = []
        self._visit_block([self._snapshot_bindings()], node.body)
        self._exception_sinks = enclosing_exception_sinks
        self.scopes.pop()
        # A definition inventories its body statically; it does not execute it.
        # Discard body writes through ``global``/``nonlocal`` at definition time.
        self._restore_bindings(enclosing)

    visit_FunctionDef = _visit_function
    visit_AsyncFunctionDef = _visit_function

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for decorator in node.decorator_list:
            self.visit(decorator)
        for base in node.bases:
            self.visit(base)
        for keyword in node.keywords:
            self.visit(keyword.value)
        for type_parameter in getattr(node, "type_params", ()):
            self.visit(type_parameter)
        self._bind(node.name, False)
        globals_, nonlocals = _scope_directives(node)
        self.scopes.append(
            _LexicalScope(
                "class",
                {},
                globals_,
                nonlocals,
                _possible_facade_bindings(self.path, node.body),
            )
        )
        result = self._visit_block([self._snapshot_bindings()], node.body)
        if result.normal:
            self._merge_bindings(result.normal)
        self.scopes.pop()

    def visit_Lambda(self, node: ast.Lambda) -> None:
        for default in (*node.args.defaults, *node.args.kw_defaults):
            if default is not None:
                self.visit(default)
        enclosing = self._snapshot_bindings()
        local = {
            argument.arg: frozenset({False})
            for argument in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
        }
        local.update(dict.fromkeys(_lambda_bound_names(node), frozenset({False})))
        if node.args.vararg:
            local[node.args.vararg.arg] = frozenset({False})
        if node.args.kwarg:
            local[node.args.kwarg.arg] = frozenset({False})
        self.scopes.append(_LexicalScope("function", local))
        enclosing_exception_sinks = self._exception_sinks
        self._exception_sinks = []
        self.visit(node.body)
        self._exception_sinks = enclosing_exception_sinks
        self.scopes.pop()
        self._restore_bindings(enclosing)

    def visit_Assign(self, node: ast.Assign) -> None:
        self.visit(node.value)
        for target in node.targets:
            self.visit(target)

    def visit_BoolOp(self, node: ast.BoolOp) -> None:
        # Every operand may be the short-circuit exit; continuing evaluates the
        # next operand from the state produced by the prior one.
        exits: list[BindingSnapshot] = []
        for value in node.values:
            self.visit(value)
            exits.append(self._snapshot_bindings())
        self._merge_bindings(exits)

    def visit_Compare(self, node: ast.Compare) -> None:
        # Chained comparisons evaluate left-to-right and may stop after every
        # comparator. Preserve each completed prefix as a feasible exit.
        self.visit(node.left)
        exits: list[BindingSnapshot] = []
        for comparator in node.comparators:
            self.visit(comparator)
            exits.append(self._snapshot_bindings())
        self._merge_bindings(exits)

    def visit_IfExp(self, node: ast.IfExp) -> None:
        self.visit(node.test)
        entry = self._snapshot_bindings()
        self._restore_bindings(entry)
        self.visit(node.body)
        body_exit = self._snapshot_bindings()
        self._restore_bindings(entry)
        self.visit(node.orelse)
        else_exit = self._snapshot_bindings()
        self._merge_bindings([body_exit, else_exit])

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self.visit(node.annotation)
        if node.value is not None:
            self.visit(node.value)
        self.visit(node.target)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        if not isinstance(node.target, ast.Name):
            self.visit(node.target)
        self.visit(node.value)
        if isinstance(node.target, ast.Name):
            self._bind(node.target.id, False)

    def visit_Call(self, node: ast.Call) -> None:
        self.visit(node.func)
        for argument in node.args:
            self.visit(argument)
        for keyword in node.keywords:
            self.visit(keyword.value)
        if isinstance(node.func, ast.Name):
            for scope in reversed(self.scopes):
                effects = scope.call_effects.get(node.func.id)
                if effects is not None:
                    for index, name in effects:
                        self._bind_at(index, name, True)
                    break

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self.visit(node.value)
        if isinstance(node.target, ast.Name) and self.scopes[-1].kind == "comprehension":
            for index in range(len(self.scopes) - 2, -1, -1):
                if self.scopes[index].kind != "comprehension":
                    self._bind_at(index, node.target.id, False)
                    return
        self.visit(node.target)

    def _visit_comprehension(
        self,
        generators: list[ast.comprehension],
        values: tuple[ast.expr, ...],
        *,
        eager: bool,
    ) -> None:
        first, *rest = generators
        # Python evaluates the first iterable in the enclosing scope. Everything
        # else executes in a child scope whose targets never bind the parent.
        self.visit(first.iter)
        zero_iteration = self._snapshot_bindings()
        target_names = {
            name for generator in generators for name in _target_names(generator.target)
        }
        self.scopes.append(
            _LexicalScope("comprehension", dict.fromkeys(target_names, frozenset({False})))
        )
        enclosing_exception_sinks = self._exception_sinks
        if not eager:
            self._exception_sinks = []
        self.visit(first.target)
        for condition in first.ifs:
            self.visit(condition)
        for generator in rest:
            self.visit(generator.iter)
            self.visit(generator.target)
            for condition in generator.ifs:
                self.visit(condition)
        for value in values:
            self.visit(value)
        if not eager:
            self._exception_sinks = enclosing_exception_sinks
        iteration_with_child = self._snapshot_bindings()
        self.scopes.pop()
        iteration_exit = iteration_with_child[:-1]
        if eager:
            self._merge_bindings([zero_iteration, iteration_exit])
        else:
            # A generator's body is inventoried, but creation only evaluates the
            # first iterable. An unconsumed generator cannot write through walrus.
            self._restore_bindings(zero_iteration)

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self._visit_comprehension(node.generators, (node.elt,), eager=True)

    visit_SetComp = visit_ListComp

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        self._visit_comprehension(node.generators, (node.elt,), eager=False)

    def visit_DictComp(self, node: ast.DictComp) -> None:
        self._visit_comprehension(node.generators, (node.key, node.value), eager=True)

    def _flow_for(self, entry: BindingSnapshot, node: ast.For | ast.AsyncFor) -> _FlowResult:
        zero_iteration, iterable_exceptions = self._evaluate_expression(entry, node.iter)
        head = zero_iteration
        body = _FlowResult()
        backedge = zero_iteration
        for _iteration in range(128):
            self._restore_bindings(head)
            self.visit(node.target)
            body = self._visit_block([self._snapshot_bindings()], node.body)
            backedge_states = [*body.normal, *body.continues]
            backedge = self._merged_snapshot(backedge_states) if backedge_states else zero_iteration
            next_head = self._merged_snapshot([zero_iteration, backedge])
            if next_head == head:
                break
            head = next_head
        else:  # pragma: no cover - finite three-value lattice must converge
            raise AssertionError("for-loop alias analysis did not converge")

        natural_exit = self._merged_snapshot([zero_iteration, backedge])
        orelse = (
            self._visit_block([natural_exit], node.orelse)
            if node.orelse
            else _FlowResult(normal=[natural_exit])
        )
        result = _FlowResult(
            normal=[*orelse.normal, *body.breaks],
            continues=orelse.continues,
            returns=[*body.returns, *orelse.returns],
            exceptions=[
                *iterable_exceptions,
                *body.exceptions,
                *orelse.exceptions,
            ],
        )
        return result

    def _flow_while(self, entry: BindingSnapshot, node: ast.While) -> _FlowResult:
        head = entry
        test_exit = entry
        test_exceptions: list[BindingSnapshot] = []
        body = _FlowResult()
        backedge = entry
        for _iteration in range(128):
            test_exit, test_exceptions = self._evaluate_expression(head, node.test)
            body = self._visit_block([test_exit], node.body)
            backedge_states = [*body.normal, *body.continues]
            backedge = self._merged_snapshot(backedge_states) if backedge_states else entry
            next_head = self._merged_snapshot([entry, backedge])
            if next_head == head:
                break
            head = next_head
        else:  # pragma: no cover - finite three-value lattice must converge
            raise AssertionError("while-loop alias analysis did not converge")

        orelse = (
            self._visit_block([test_exit], node.orelse)
            if node.orelse
            else _FlowResult(normal=[test_exit])
        )
        return _FlowResult(
            normal=[*orelse.normal, *body.breaks],
            continues=orelse.continues,
            returns=[*body.returns, *orelse.returns],
            exceptions=[*test_exceptions, *body.exceptions, *orelse.exceptions],
        )

    def _cleanup_handler_target(
        self, snapshots: list[BindingSnapshot], name: str | None
    ) -> list[BindingSnapshot]:
        if not name:
            return snapshots
        cleaned: list[BindingSnapshot] = []
        for snapshot in snapshots:
            self._restore_bindings(snapshot)
            self._unbind(name)
            cleaned.append(self._snapshot_bindings())
        return cleaned

    def _flow_handler(self, entry: BindingSnapshot, node: ast.ExceptHandler) -> _FlowResult:
        state = entry
        type_exceptions: list[BindingSnapshot] = []
        if node.type is not None:
            state, type_exceptions = self._evaluate_expression(state, node.type)
        self._restore_bindings(state)
        if node.name:
            self._bind(node.name, False)
        body = self._visit_block([self._snapshot_bindings()], node.body)
        body.normal = self._cleanup_handler_target(body.normal, node.name)
        body.breaks = self._cleanup_handler_target(body.breaks, node.name)
        body.continues = self._cleanup_handler_target(body.continues, node.name)
        body.returns = self._cleanup_handler_target(body.returns, node.name)
        body.exceptions = self._cleanup_handler_target(body.exceptions, node.name)
        body.exceptions.extend(type_exceptions)
        return body

    def _apply_finally(self, flow: _FlowResult, statements: list[ast.stmt]) -> _FlowResult:
        if not statements:
            return flow
        result = _FlowResult()
        for kind in ("normal", "breaks", "continues", "returns", "exceptions"):
            states = getattr(flow, kind)
            if not states:
                continue
            final = self._visit_block([self._merged_snapshot(states)], statements)
            result.absorb_abrupt(final)
            if final.normal:
                getattr(result, kind).extend(final.normal)
        return result

    def _flow_try(self, entry: BindingSnapshot, node: ast.Try) -> _FlowResult:
        body = self._visit_block([entry], node.body)
        handler_input = self._merged_snapshot([entry, *body.exceptions])
        orelse = (
            self._visit_block(body.normal, node.orelse)
            if node.orelse and body.normal
            else _FlowResult(normal=body.normal)
        )
        combined = _FlowResult(
            normal=list(orelse.normal),
            breaks=[*body.breaks, *orelse.breaks],
            continues=[*body.continues, *orelse.continues],
            returns=[*body.returns, *orelse.returns],
            exceptions=[*body.exceptions, *orelse.exceptions],
        )
        if type(node).__name__ == "TryStar":
            # ``except*`` clauses are non-exclusive and sequential; each may be
            # skipped, while later clauses see possible writes from earlier ones.
            star_state = handler_input
            for handler in node.handlers:
                handled = self._flow_handler(star_state, handler)
                combined.breaks.extend(handled.breaks)
                combined.continues.extend(handled.continues)
                combined.returns.extend(handled.returns)
                combined.exceptions.extend(handled.exceptions)
                if handled.normal:
                    star_state = self._merged_snapshot([star_state, *handled.normal])
            combined.normal.append(star_state)
        else:
            for handler in node.handlers:
                handled = self._flow_handler(handler_input, handler)
                combined.normal.extend(handled.normal)
                combined.absorb_abrupt(handled)
        return self._apply_finally(combined, node.finalbody)

    def _flow_match(self, entry: BindingSnapshot, node: ast.Match) -> _FlowResult:
        fallthrough, subject_exceptions = self._evaluate_expression(entry, node.subject)
        normal: list[BindingSnapshot] = []
        result = _FlowResult(exceptions=subject_exceptions)
        for case in node.cases:
            if fallthrough is None:
                break
            self._restore_bindings(fallthrough)
            pattern_exceptions = [fallthrough]
            self._exception_sinks.append(pattern_exceptions)
            self._visit_pattern_expressions(case.pattern)
            self._exception_sinks.pop()
            result.exceptions.extend(pattern_exceptions)
            pattern_failure = self._snapshot_bindings()
            case_captures = _pattern_capture_names(case.pattern)
            for name in case_captures:
                self._bind(name, False)
            if case.guard is not None:
                guarded, guard_exceptions = self._evaluate_expression(
                    self._snapshot_bindings(), case.guard
                )
                result.exceptions.extend(guard_exceptions)
            else:
                guarded = self._snapshot_bindings()
            body = self._visit_block([guarded], case.body)
            normal.extend(body.normal)
            result.absorb_abrupt(body)
            irrefutable = _pattern_is_irrefutable(case.pattern)
            if case.guard is not None:
                # A false guard retains successful pattern captures. A refutable
                # pattern also has the ordinary pattern-failure path.
                fallthrough = (
                    guarded if irrefutable else self._merged_snapshot([pattern_failure, guarded])
                )
            elif irrefutable:
                fallthrough = None
            else:
                fallthrough = pattern_failure
        if fallthrough is not None:
            normal.append(fallthrough)
        result.normal = normal
        return result

    def _visit_pattern_expressions(self, pattern: ast.pattern) -> None:
        if isinstance(pattern, ast.MatchValue):
            self.visit(pattern.value)
        elif isinstance(pattern, ast.MatchClass):
            self.visit(pattern.cls)
            for child in (*pattern.patterns, *pattern.kwd_patterns):
                self._visit_pattern_expressions(child)
        elif isinstance(pattern, ast.MatchMapping):
            for key in pattern.keys:
                self.visit(key)
            for child in pattern.patterns:
                self._visit_pattern_expressions(child)
        elif isinstance(pattern, ast.MatchSequence | ast.MatchOr):
            for child in pattern.patterns:
                self._visit_pattern_expressions(child)
        elif isinstance(pattern, ast.MatchAs) and pattern.pattern is not None:
            self._visit_pattern_expressions(pattern.pattern)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Store):
            self._bind(node.id, False)
        elif isinstance(node.ctx, ast.Del):
            self._unbind(node.id)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if isinstance(node.value, ast.Name) and self._lookup(node.value.id):
            self.attributes.add(node.attr)
        self.generic_visit(node)


def _scope_directives(
    node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
) -> tuple[set[str], set[str]]:
    globals_: set[str] = set()
    nonlocals: set[str] = set()

    class Collector(ast.NodeVisitor):
        def visit_Global(self, child: ast.Global) -> None:
            globals_.update(child.names)

        def visit_Nonlocal(self, child: ast.Nonlocal) -> None:
            nonlocals.update(child.names)

        def visit_FunctionDef(self, child: ast.FunctionDef) -> None:
            pass

        visit_AsyncFunctionDef = visit_FunctionDef
        visit_ClassDef = visit_FunctionDef
        visit_Lambda = visit_FunctionDef

    collector = Collector()
    for statement in node.body:
        collector.visit(statement)
    return globals_, nonlocals


def _possible_facade_bindings(path: Path, statements: list[ast.stmt]) -> set[str]:
    """Facade aliases that may be bound anywhere in this lexical scope."""
    names: set[str] = set()

    class Collector(ast.NodeVisitor):
        def visit_ImportFrom(self, child: ast.ImportFrom) -> None:
            if _resolved_module(path, child) == "notebooklm":
                names.update(
                    imported.asname or imported.name
                    for imported in child.names
                    if imported.name == "auth"
                )

        def visit_FunctionDef(self, child: ast.FunctionDef) -> None:
            pass

        visit_AsyncFunctionDef = visit_FunctionDef
        visit_ClassDef = visit_FunctionDef
        visit_Lambda = visit_FunctionDef

    collector = Collector()
    for statement in statements:
        collector.visit(statement)
    return names


def _explicit_facade_import_effects(
    path: Path, node: ast.FunctionDef | ast.AsyncFunctionDef
) -> set[str]:
    """Facade imports in this helper body, excluding nested definitions."""
    names: set[str] = set()

    class Collector(ast.NodeVisitor):
        def visit_ImportFrom(self, child: ast.ImportFrom) -> None:
            if _resolved_module(path, child) == "notebooklm":
                names.update(
                    imported.asname or imported.name
                    for imported in child.names
                    if imported.name == "auth"
                )

        def visit_FunctionDef(self, child: ast.FunctionDef) -> None:
            pass

        visit_AsyncFunctionDef = visit_FunctionDef
        visit_ClassDef = visit_FunctionDef
        visit_Lambda = visit_FunctionDef

    collector = Collector()
    for statement in node.body:
        collector.visit(statement)
    return names


def _lambda_bound_names(node: ast.Lambda) -> set[str]:
    """Walrus targets are local to their containing lambda across the whole body."""
    names: set[str] = set()

    class Collector(ast.NodeVisitor):
        def visit_NamedExpr(self, child: ast.NamedExpr) -> None:
            if isinstance(child.target, ast.Name):
                names.add(child.target.id)
            self.visit(child.value)

        def visit_Lambda(self, child: ast.Lambda) -> None:
            pass

    Collector().visit(node.body)
    return names


def _scope_bound_names(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> set[str]:
    """Names made local by this lexical scope, excluding nested scope bodies."""
    names: set[str] = set()

    class Collector(ast.NodeVisitor):
        def visit_Name(self, child: ast.Name) -> None:
            if isinstance(child.ctx, ast.Store | ast.Del):
                names.add(child.id)

        def visit_Import(self, child: ast.Import) -> None:
            names.update(item.asname or item.name.split(".")[0] for item in child.names)

        def visit_ImportFrom(self, child: ast.ImportFrom) -> None:
            names.update(item.asname or item.name for item in child.names)

        def visit_FunctionDef(self, child: ast.FunctionDef) -> None:
            names.add(child.name)

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_ClassDef(self, child: ast.ClassDef) -> None:
            names.add(child.name)

        def visit_Lambda(self, child: ast.Lambda) -> None:
            pass

        def visit_ExceptHandler(self, child: ast.ExceptHandler) -> None:
            if child.name:
                names.add(child.name)
            if child.type is not None:
                self.visit(child.type)
            for statement in child.body:
                self.visit(statement)

        def visit_Match(self, child: ast.Match) -> None:
            names.update(
                name for case in child.cases for name in _pattern_capture_names(case.pattern)
            )
            self.visit(child.subject)
            for case in child.cases:
                if case.guard is not None:
                    self.visit(case.guard)
                for statement in case.body:
                    self.visit(statement)

        def _visit_comprehension(
            self,
            generators: list[ast.comprehension],
            values: tuple[ast.expr, ...],
        ) -> None:
            for generator in generators:
                self.visit(generator.iter)
                for condition in generator.ifs:
                    self.visit(condition)
            for value in values:
                self.visit(value)

        def visit_ListComp(self, child: ast.ListComp) -> None:
            self._visit_comprehension(child.generators, (child.elt,))

        visit_SetComp = visit_ListComp
        visit_GeneratorExp = visit_ListComp

        def visit_DictComp(self, child: ast.DictComp) -> None:
            self._visit_comprehension(child.generators, (child.key, child.value))

    collector = Collector()
    for statement in node.body:
        collector.visit(statement)
    return names


def _target_names(target: ast.AST) -> set[str]:
    return {
        node.id
        for node in ast.walk(target)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store | ast.Del)
    }


def _pattern_capture_names(pattern: ast.pattern) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(pattern):
        if isinstance(node, ast.MatchAs | ast.MatchStar) and node.name:
            names.add(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            names.add(node.rest)
    return names


def _pattern_is_irrefutable(pattern: ast.pattern) -> bool:
    if isinstance(pattern, ast.MatchAs):
        return pattern.pattern is None or _pattern_is_irrefutable(pattern.pattern)
    if isinstance(pattern, ast.MatchOr):
        return any(_pattern_is_irrefutable(child) for child in pattern.patterns)
    return False


def _alias_callers(root: Path) -> dict[str, list[str]]:
    result: dict[str, set[str]] = defaultdict(set)
    for path in sorted(root.rglob("*.py")):
        if path == root / "auth.py":
            continue
        collector = _AliasUseCollector(path)
        collector.visit(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        relative = path.relative_to(REPO_ROOT).as_posix()
        for attribute in collector.attributes:
            result[attribute].add(relative)
    return {name: sorted(paths) for name, paths in sorted(result.items())}


def test_first_party_facade_callers_are_frozen_in_both_import_idioms() -> None:
    direct, module_imports = _direct_callers(SRC_ROOT)
    aliases = _alias_callers(SRC_ROOT)
    assert direct == EXPECTED_DIRECT_CALLERS
    assert module_imports == []
    assert aliases == EXPECTED_ALIAS_CALLERS
    union = {(name, path) for name, paths in direct.items() for path in paths}
    union |= {(name, path) for name, paths in aliases.items() for path in paths}
    assert len(direct) == 18
    assert sum(map(len, direct.values())) == 44
    assert len(aliases) == 28
    assert sum(map(len, aliases.values())) == 33
    assert len({name for name, _path in union}) == 42
    assert len(union) == 77


@pytest.mark.skipif(not hasattr(ast, "TryStar"), reason="exception-group AST requires 3.11+")
def test_facade_caller_detectors_bite_on_aliases_relative_imports_and_shadowing(
    tmp_path: Path,
) -> None:
    root = tmp_path / "src" / "notebooklm"
    package = root / "feature"
    package.mkdir(parents=True)
    (package / "one.py").write_text(
        "from notebooklm.auth import Thing as Renamed\n", encoding="utf-8"
    )
    (package / "two.py").write_text(
        "from .. import auth as facade\nfacade.alpha()\n", encoding="utf-8"
    )
    (package / "three.py").write_text(
        "from .. import auth as facade\n"
        "def f(facade):\n    facade.hidden()\n"
        "def g():\n    facade.visible()\n",
        encoding="utf-8",
    )
    (package / "four.py").write_text(
        "from .. import auth as facade\ndef f():\n    facade = object()\n    facade.shadowed()\n",
        encoding="utf-8",
    )
    (package / "five.py").write_text(
        "from .. import auth as facade\n"
        "@facade.function_decorator\n"
        "def function(value: facade.Parameter = facade.default_value) -> facade.ReturnValue:\n"
        "    pass\n"
        "@facade.class_decorator\n"
        "class Example(facade.Base, metaclass=facade.Meta):\n"
        "    pass\n",
        encoding="utf-8",
    )
    (package / "six.py").write_text(
        "from .. import auth as facade\n"
        "class Example:\n"
        "    facade.class_before()\n"
        "    facade = object()\n"
        "    facade.class_after()\n"
        "    def method():\n"
        "        facade.method_free()\n",
        encoding="utf-8",
    )
    (package / "seven.py").write_text(
        "from .. import auth as facade\n"
        "def use_global():\n"
        "    global facade\n"
        "    facade.global_use()\n"
        "def outer():\n"
        "    from .. import auth as nested\n"
        "    def inner():\n"
        "        nonlocal nested\n"
        "        nested.nonlocal_use()\n",
        encoding="utf-8",
    )
    (package / "eight.py").write_text(
        "from .. import auth as facade\n"
        "[facade.inside_module_comp for facade in facade.module_iterable]\n"
        "facade.after_module_comp()\n"
        "def comp_function():\n"
        "    [facade.inside_function_comp for facade in facade.function_iterable]\n"
        "    facade.after_function_comp()\n",
        encoding="utf-8",
    )
    (package / "nine.py").write_text(
        "from .. import auth as facade\n"
        "try:\n"
        "    pass\n"
        "except facade.Error as facade:\n"
        "    facade.inside_except()\n"
        "facade.after_except()\n",
        encoding="utf-8",
    )
    (package / "ten.py").write_text(
        "from .. import auth as facade\n"
        "match facade.subject_as:\n"
        "    case facade.Pattern() as facade:\n"
        "        facade.inside_as()\n"
        "facade.after_as()\n",
        encoding="utf-8",
    )
    (package / "eleven.py").write_text(
        "from .. import auth as facade\n"
        "match facade.subject_star:\n"
        "    case [*facade]:\n"
        "        facade.inside_star()\n"
        "facade.after_star()\n",
        encoding="utf-8",
    )
    (package / "twelve.py").write_text(
        "from .. import auth as facade\n"
        "match facade.subject_rest:\n"
        "    case {**facade}:\n"
        "        facade.inside_rest()\n"
        "facade.after_rest()\n",
        encoding="utf-8",
    )
    (package / "thirteen.py").write_text(
        "from .. import auth as facade\n"
        "match facade.subject_plain:\n"
        "    case facade:\n"
        "        facade.inside_plain()\n"
        "facade.after_plain()\n",
        encoding="utf-8",
    )
    (package / "fourteen.py").write_text(
        "from .. import auth as facade\n"
        "match facade.subject_or:\n"
        "    case [facade, 1] | [facade, 2]:\n"
        "        facade.inside_or()\n"
        "facade.after_or()\n",
        encoding="utf-8",
    )
    (package / "fifteen.py").write_text(
        "from .. import auth as facade\n"
        "def deleted_local():\n"
        "    facade.before_function_del()\n"
        "    del facade\n"
        "facade.after_deleted_function()\n"
        "value = lambda: (\n"
        "    facade.before_lambda_walrus(),\n"
        "    (facade := object()),\n"
        "    facade.after_lambda_walrus(),\n"
        ")\n"
        "facade.after_lambda_definition()\n",
        encoding="utf-8",
    )
    (package / "sixteen.py").write_text(
        "from .. import auth as facade\n"
        "for facade in facade.for_iterable:\n"
        "    facade.inside_for()\n"
        "facade.after_for()\n"
        "async def async_loop():\n"
        "    from .. import auth as async_facade\n"
        "    async for async_facade in async_facade.async_iterable:\n"
        "        async_facade.inside_async_for()\n"
        "    async_facade.after_async_for()\n",
        encoding="utf-8",
    )
    (package / "seventeen.py").write_text(
        "from .. import auth as facade\n"
        "facade += facade.aug_rhs\n"
        "facade.after_name_aug()\n"
        "from .. import auth as facade\n"
        "facade.aug_target += facade.aug_attribute_rhs\n"
        "facade.after_attribute_aug()\n",
        encoding="utf-8",
    )
    (package / "eighteen.py").write_text(
        "from .. import auth as facade\n"
        "try:\n"
        "    pass\n"
        "except* facade.ExceptionGroup as facade:\n"
        "    facade.inside_try_star()\n"
        "facade.after_try_star()\n",
        encoding="utf-8",
    )
    (package / "nineteen.py").write_text(
        "from .. import auth as facade\n"
        "try:\n"
        "    pass\n"
        "except facade.FirstError as facade:\n"
        "    facade.inside_first_handler()\n"
        "except facade.SecondError:\n"
        "    facade.second_handler()\n"
        "facade.after_multi_try()\n",
        encoding="utf-8",
    )
    (package / "twenty.py").write_text(
        "from .. import auth as facade\n"
        "match facade.subject_cases:\n"
        "    case facade.FirstPattern() as facade:\n"
        "        facade.inside_first_case()\n"
        "    case facade.SecondPattern():\n"
        "        facade.second_case()\n"
        "facade.after_cases()\n",
        encoding="utf-8",
    )
    (package / "twenty_one.py").write_text(
        "from .. import auth as facade\n"
        "def global_mutator():\n"
        "    global facade\n"
        "    facade.global_body_before_write()\n"
        "    facade = object()\n"
        "facade.after_global_definition()\n"
        "def outer():\n"
        "    from .. import auth as nested\n"
        "    def inner():\n"
        "        nonlocal nested\n"
        "        nested.nonlocal_body_before_write()\n"
        "        nested = object()\n"
        "    nested.after_inner_definition()\n",
        encoding="utf-8",
    )
    (package / "twenty_two.py").write_text(
        "from .. import auth as facade\n"
        "if flag:\n"
        "    facade = object()\n"
        "facade.after_optional_shadow()\n"
        "choice = object()\n"
        "if flag:\n"
        "    from .. import auth as choice\n"
        "choice.after_optional_import()\n"
        "while flag:\n"
        "    facade = object()\n"
        "facade.after_while()\n"
        "from .. import auth as facade\n"
        "facade and (facade := object())\n"
        "facade.after_boolop()\n"
        "from .. import auth as facade\n"
        "(facade := object()) if flag else facade.ifexp_else()\n"
        "facade.after_ifexp()\n"
        "from .. import auth as facade\n"
        "for item in facade.break_iterable:\n"
        "    facade.break_body()\n"
        "    break\n"
        "else:\n"
        "    facade = object()\n"
        "facade.after_break_else()\n",
        encoding="utf-8",
    )
    (package / "twenty_three.py").write_text(
        "from .. import auth as facade\n"
        "[(facade.eager_body(), (facade := object())) "
        "for item in facade.eager_iterable]\n"
        "facade.after_empty_eager()\n"
        "from .. import auth as facade\n"
        "generator = ((facade.generator_body(), (facade := object())) "
        "for item in facade.generator_iterable)\n"
        "facade.after_unconsumed_generator()\n",
        encoding="utf-8",
    )
    (package / "twenty_four.py").write_text(
        "from .. import auth as facade\n"
        "candidate = object()\n"
        "try:\n"
        "    from .. import auth as candidate\n"
        "    raise facade.TryError\n"
        "except facade.HandlerError:\n"
        "    candidate.handler_after_import()\n"
        "candidate.after_try_import()\n"
        "star = object()\n"
        "try:\n"
        "    raise facade.GroupError\n"
        "except* facade.FirstGroup:\n"
        "    from .. import auth as star\n"
        "except* facade.SecondGroup:\n"
        "    star.second_star_handler()\n"
        "star.after_star_handlers()\n",
        encoding="utf-8",
    )
    (package / "twenty_five.py").write_text(
        "from .. import auth as facade\n"
        "match facade.false_guard_subject:\n"
        "    case facade if False:\n"
        "        facade.inside_false_guard()\n"
        "    case object():\n"
        "        facade.next_after_false_guard()\n"
        "facade.after_false_guard()\n",
        encoding="utf-8",
    )
    (package / "twenty_six.py").write_text(
        "candidate = object()\n"
        "for item in (1, 2):\n"
        "    candidate.on_second_for()\n"
        "    from .. import auth as candidate\n"
        "candidate.after_second_for()\n"
        "tail = object()\n"
        "for item in items:\n"
        "    from .. import auth as tail\n"
        "    tail.before_continue()\n"
        "    continue\n"
        "    tail = object()\n"
        "tail.after_continue()\n"
        "loop = object()\n"
        "while condition:\n"
        "    loop.on_second_while()\n"
        "    from .. import auth as loop\n"
        "loop.after_second_while()\n",
        encoding="utf-8",
    )
    (package / "twenty_seven.py").write_text(
        "def future_global():\n"
        "    facade.future_global()\n"
        "callback = lambda: facade.future_lambda()\n"
        "generator = (facade.future_generator() for item in items)\n"
        "from .. import auth as facade\n"
        "def outer():\n"
        "    def inner():\n"
        "        nested.future_nonlocal()\n"
        "    from .. import auth as nested\n",
        encoding="utf-8",
    )
    (package / "twenty_eight.py").write_text(
        "from .. import auth as facade\n"
        "candidate = object()\n"
        "try:\n"
        "    if condition:\n"
        "        from .. import auth as candidate\n"
        "        candidate.may_raise()\n"
        "        candidate = object()\n"
        "except facade.NestedError:\n"
        "    candidate.handler_from_nested_prefix()\n"
        "final_candidate = object()\n"
        "try:\n"
        "    from .. import auth as final_candidate\n"
        "    raise facade.FinalError\n"
        "finally:\n"
        "    final_candidate.finally_after_import()\n",
        encoding="utf-8",
    )
    (package / "twenty_nine.py").write_text(
        "from .. import auth as facade\n"
        "facade.compare_left() < facade.compare_middle() < (facade := object())\n"
        "facade.after_compare()\n",
        encoding="utf-8",
    )
    (package / "thirty.py").write_text(
        "candidate = object()\n"
        "try:\n"
        "    from .. import auth as candidate, definitely_missing\n"
        "except ImportError:\n"
        "    candidate.after_partial_import()\n",
        encoding="utf-8",
    )
    (package / "thirty_one.py").write_text(
        "import contextlib\n"
        "suppressed = object()\n"
        "with contextlib.suppress(Exception):\n"
        "    from .. import auth as suppressed\n"
        "    raise RuntimeError\n"
        "suppressed.after_suppressed_exception()\n",
        encoding="utf-8",
    )
    (package / "thirty_two.py").write_text(
        "global_alias = object()\n"
        "def bind_global():\n"
        "    global global_alias\n"
        "    from .. import auth as global_alias\n"
        "bind_global()\n"
        "global_alias.after_called_global()\n"
        "uncalled_alias = object()\n"
        "def bind_uncalled():\n"
        "    global uncalled_alias\n"
        "    from .. import auth as uncalled_alias\n"
        "uncalled_alias.after_uncalled_global()\n"
        "def outer():\n"
        "    nested = object()\n"
        "    def bind_nested():\n"
        "        nonlocal nested\n"
        "        from .. import auth as nested\n"
        "    bind_nested()\n"
        "    nested.after_called_nonlocal()\n",
        encoding="utf-8",
    )
    # The scanners resolve relative imports relative to their supplied synthetic repo root.
    global REPO_ROOT
    old_root = REPO_ROOT
    try:
        REPO_ROOT = tmp_path
        direct, modules = _direct_callers(root)
        aliases = _alias_callers(root)
    finally:
        REPO_ROOT = old_root
    assert modules == []
    assert direct == {"Thing": ["src/notebooklm/feature/one.py"]}
    assert aliases == {
        "Base": ["src/notebooklm/feature/five.py"],
        "Error": ["src/notebooklm/feature/nine.py"],
        "ExceptionGroup": ["src/notebooklm/feature/eighteen.py"],
        "FirstError": ["src/notebooklm/feature/nineteen.py"],
        "FirstPattern": ["src/notebooklm/feature/twenty.py"],
        "Meta": ["src/notebooklm/feature/five.py"],
        "Parameter": ["src/notebooklm/feature/five.py"],
        "Pattern": ["src/notebooklm/feature/ten.py"],
        "ReturnValue": ["src/notebooklm/feature/five.py"],
        "SecondError": ["src/notebooklm/feature/nineteen.py"],
        "SecondPattern": ["src/notebooklm/feature/twenty.py"],
        "alpha": ["src/notebooklm/feature/two.py"],
        "after_as": ["src/notebooklm/feature/ten.py"],
        "after_async_for": ["src/notebooklm/feature/sixteen.py"],
        "after_attribute_aug": ["src/notebooklm/feature/seventeen.py"],
        "after_cases": ["src/notebooklm/feature/twenty.py"],
        "after_deleted_function": ["src/notebooklm/feature/fifteen.py"],
        "after_except": ["src/notebooklm/feature/nine.py"],
        "after_for": ["src/notebooklm/feature/sixteen.py"],
        "after_function_comp": ["src/notebooklm/feature/eight.py"],
        "after_lambda_definition": ["src/notebooklm/feature/fifteen.py"],
        "after_module_comp": ["src/notebooklm/feature/eight.py"],
        "after_multi_try": ["src/notebooklm/feature/nineteen.py"],
        "after_or": ["src/notebooklm/feature/fourteen.py"],
        "after_rest": ["src/notebooklm/feature/twelve.py"],
        "after_star": ["src/notebooklm/feature/eleven.py"],
        "after_try_star": ["src/notebooklm/feature/eighteen.py"],
        "async_iterable": ["src/notebooklm/feature/sixteen.py"],
        "aug_attribute_rhs": ["src/notebooklm/feature/seventeen.py"],
        "aug_rhs": ["src/notebooklm/feature/seventeen.py"],
        "aug_target": ["src/notebooklm/feature/seventeen.py"],
        "class_before": ["src/notebooklm/feature/six.py"],
        "class_decorator": ["src/notebooklm/feature/five.py"],
        "default_value": ["src/notebooklm/feature/five.py"],
        "for_iterable": ["src/notebooklm/feature/sixteen.py"],
        "function_decorator": ["src/notebooklm/feature/five.py"],
        "function_iterable": ["src/notebooklm/feature/eight.py"],
        "global_use": ["src/notebooklm/feature/seven.py"],
        "method_free": ["src/notebooklm/feature/six.py"],
        "module_iterable": ["src/notebooklm/feature/eight.py"],
        "nonlocal_use": ["src/notebooklm/feature/seven.py"],
        "second_case": ["src/notebooklm/feature/twenty.py"],
        "second_handler": ["src/notebooklm/feature/nineteen.py"],
        "subject_as": ["src/notebooklm/feature/ten.py"],
        "subject_cases": ["src/notebooklm/feature/twenty.py"],
        "subject_or": ["src/notebooklm/feature/fourteen.py"],
        "subject_plain": ["src/notebooklm/feature/thirteen.py"],
        "subject_rest": ["src/notebooklm/feature/twelve.py"],
        "subject_star": ["src/notebooklm/feature/eleven.py"],
        "FirstGroup": ["src/notebooklm/feature/twenty_four.py"],
        "GroupError": ["src/notebooklm/feature/twenty_four.py"],
        "HandlerError": ["src/notebooklm/feature/twenty_four.py"],
        "SecondGroup": ["src/notebooklm/feature/twenty_four.py"],
        "TryError": ["src/notebooklm/feature/twenty_four.py"],
        "after_boolop": ["src/notebooklm/feature/twenty_two.py"],
        "after_break_else": ["src/notebooklm/feature/twenty_two.py"],
        "after_empty_eager": ["src/notebooklm/feature/twenty_three.py"],
        "after_global_definition": ["src/notebooklm/feature/twenty_one.py"],
        "after_ifexp": ["src/notebooklm/feature/twenty_two.py"],
        "after_inner_definition": ["src/notebooklm/feature/twenty_one.py"],
        "after_optional_import": ["src/notebooklm/feature/twenty_two.py"],
        "after_optional_shadow": ["src/notebooklm/feature/twenty_two.py"],
        "after_star_handlers": ["src/notebooklm/feature/twenty_four.py"],
        "after_try_import": ["src/notebooklm/feature/twenty_four.py"],
        "after_unconsumed_generator": ["src/notebooklm/feature/twenty_three.py"],
        "after_while": ["src/notebooklm/feature/twenty_two.py"],
        "break_body": ["src/notebooklm/feature/twenty_two.py"],
        "break_iterable": ["src/notebooklm/feature/twenty_two.py"],
        "eager_body": ["src/notebooklm/feature/twenty_three.py"],
        "eager_iterable": ["src/notebooklm/feature/twenty_three.py"],
        "false_guard_subject": ["src/notebooklm/feature/twenty_five.py"],
        "generator_body": ["src/notebooklm/feature/twenty_three.py"],
        "generator_iterable": ["src/notebooklm/feature/twenty_three.py"],
        "global_body_before_write": ["src/notebooklm/feature/twenty_one.py"],
        "handler_after_import": ["src/notebooklm/feature/twenty_four.py"],
        "ifexp_else": ["src/notebooklm/feature/twenty_two.py"],
        "nonlocal_body_before_write": ["src/notebooklm/feature/twenty_one.py"],
        "second_star_handler": ["src/notebooklm/feature/twenty_four.py"],
        "FinalError": ["src/notebooklm/feature/twenty_eight.py"],
        "NestedError": ["src/notebooklm/feature/twenty_eight.py"],
        "after_compare": ["src/notebooklm/feature/twenty_nine.py"],
        "after_continue": ["src/notebooklm/feature/twenty_six.py"],
        "after_called_global": ["src/notebooklm/feature/thirty_two.py"],
        "after_called_nonlocal": ["src/notebooklm/feature/thirty_two.py"],
        "after_second_for": ["src/notebooklm/feature/twenty_six.py"],
        "after_second_while": ["src/notebooklm/feature/twenty_six.py"],
        "after_partial_import": ["src/notebooklm/feature/thirty.py"],
        "after_suppressed_exception": ["src/notebooklm/feature/thirty_one.py"],
        "before_continue": ["src/notebooklm/feature/twenty_six.py"],
        "compare_left": ["src/notebooklm/feature/twenty_nine.py"],
        "compare_middle": ["src/notebooklm/feature/twenty_nine.py"],
        "finally_after_import": ["src/notebooklm/feature/twenty_eight.py"],
        "future_generator": ["src/notebooklm/feature/twenty_seven.py"],
        "future_global": ["src/notebooklm/feature/twenty_seven.py"],
        "future_lambda": ["src/notebooklm/feature/twenty_seven.py"],
        "future_nonlocal": ["src/notebooklm/feature/twenty_seven.py"],
        "handler_from_nested_prefix": ["src/notebooklm/feature/twenty_eight.py"],
        "may_raise": ["src/notebooklm/feature/twenty_eight.py"],
        "on_second_for": ["src/notebooklm/feature/twenty_six.py"],
        "on_second_while": ["src/notebooklm/feature/twenty_six.py"],
        "visible": ["src/notebooklm/feature/three.py"],
    }
