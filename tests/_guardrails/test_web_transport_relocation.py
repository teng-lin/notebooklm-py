"""Canonical-location and identity guards for the Phase A10 transport move."""

from __future__ import annotations

from pathlib import Path

import notebooklm._runtime as runtime_package
import notebooklm.client as client_module
from notebooklm._runtime.contracts import LoopGuard
from notebooklm._web.contracts import Kernel as KernelContract
from notebooklm._web.contracts import RpcCaller
from notebooklm._web.transport import executor, seams
from notebooklm._web.transport.auth import AuthRefreshCoordinator
from notebooklm._web.transport.cookie_persistence import CookiePersistence
from notebooklm._web.transport.kernel import Kernel
from notebooklm._web.transport.reqid_counter import ReqidCounter
from notebooklm._web.transport.runtime import RuntimeTransport

SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "notebooklm"

REMOVED_TRANSPORT_PATHS = frozenset(
    {
        "_auth_refresh_retry.py",
        "_chat/transport.py",
        "_client_seams.py",
        "_cookie_persistence.py",
        "_error_injection.py",
        "_kernel.py",
        "_middleware/__init__.py",
        "_reqid_counter.py",
        "_request_types.py",
        "_rpc_executor.py",
        "_runtime/auth.py",
        "_runtime/transport.py",
        "_streaming_post.py",
        "_transport_errors.py",
    }
)

MOVED_RUNTIME_EXPORTS = frozenset(
    {
        "auth",
        "transport",
        "AuthRefreshCoordinator",
        "Kernel",
        "RpcCaller",
        "RuntimeTransport",
    }
)


def test_transport_implementations_exist_only_at_the_web_paths() -> None:
    stale = sorted(path for path in REMOVED_TRANSPORT_PATHS if (SRC_ROOT / path).exists())
    assert stale == []


def test_runtime_package_does_not_eagerly_reexport_moved_web_names() -> None:
    assert MOVED_RUNTIME_EXPORTS.isdisjoint(runtime_package.__all__)
    assert all(not hasattr(runtime_package, name) for name in MOVED_RUNTIME_EXPORTS)
    assert runtime_package.LoopGuard is LoopGuard


def test_client_module_keeps_required_identity_attributes() -> None:
    assert client_module.ClientSeams is seams.ClientSeams
    assert client_module.resolve_client_seams is seams.resolve_client_seams
    assert client_module.RpcExecutor is executor.RpcExecutor


def test_transport_and_contract_objects_report_canonical_modules() -> None:
    assert KernelContract.__module__ == "notebooklm._web.contracts"
    assert RpcCaller.__module__ == "notebooklm._web.contracts"
    assert LoopGuard.__module__ == "notebooklm._runtime.contracts"
    assert AuthRefreshCoordinator.__module__ == "notebooklm._web.transport.auth"
    assert CookiePersistence.__module__ == "notebooklm._web.transport.cookie_persistence"
    assert Kernel.__module__ == "notebooklm._web.transport.kernel"
    assert ReqidCounter.__module__ == "notebooklm._web.transport.reqid_counter"
    assert RuntimeTransport.__module__ == "notebooklm._web.transport.runtime"
