"""Canonical-location and identity guards for the Phase A10 transport move."""

from __future__ import annotations

import subprocess
import sys
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
REPO_ROOT = SRC_ROOT.parents[1]

CURRENT_LAYOUT_DOCS = (
    "docs/architecture.md",
    "docs/conventions.md",
    "docs/development.md",
    "docs/python-api.md",
)

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

REMOVED_PYTHON_API_MODULE_TOKENS = {
    "`_cookie_persistence`",
    "`_error_injection`",
    "`_kernel`",
    "`_reqid_counter`",
    "`_request_types`",
    "`_rpc_executor`",
    "`_streaming_post`",
    "`_transport_errors`",
}


def test_transport_implementations_exist_only_at_the_web_paths() -> None:
    stale = sorted(path for path in REMOVED_TRANSPORT_PATHS if (SRC_ROOT / path).exists())
    assert stale == []


def test_runtime_package_does_not_eagerly_reexport_moved_web_names() -> None:
    assert MOVED_RUNTIME_EXPORTS.isdisjoint(runtime_package.__all__)
    assert all(not hasattr(runtime_package, name) for name in MOVED_RUNTIME_EXPORTS)
    assert runtime_package.LoopGuard is LoopGuard


def test_current_docs_use_canonical_transport_paths_and_contract_homes() -> None:
    texts = {path: (REPO_ROOT / path).read_text(encoding="utf-8") for path in CURRENT_LAYOUT_DOCS}
    stale = {
        path: sorted(token for token in REMOVED_TRANSPORT_PATHS if token in content)
        for path, content in texts.items()
    }
    assert {path: tokens for path, tokens in stale.items() if tokens} == {}
    assert all(
        token not in texts["docs/python-api.md"] for token in REMOVED_PYTHON_API_MODULE_TOKENS
    )

    assert "`RpcCaller` object Protocol from `_web/contracts.py`" in texts["docs/conventions.md"]
    assert "| `_web/contracts.py` | Web transport Protocols |" in texts["docs/development.md"]
    assert "see `Kernel` Protocol in `_web/contracts.py`" in texts["docs/python-api.md"]


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


def test_runtime_config_package_initialization_stays_web_free() -> None:
    script = """
import importlib
import sys
import types

package = types.ModuleType("notebooklm")
package.__package__ = "notebooklm"
package.__path__ = [sys.argv[1]]
sys.modules["notebooklm"] = package

importlib.import_module("notebooklm._runtime.config")
loaded_web = sorted(name for name in sys.modules if name.startswith("notebooklm._web"))
if loaded_web:
    raise AssertionError(f"runtime config loaded web modules: {loaded_web}")
"""
    subprocess.run(
        [sys.executable, "-I", "-c", script, str(SRC_ROOT)],
        check=True,
        capture_output=True,
        text=True,
    )


def test_web_transport_leaves_import_without_the_public_composition_root() -> None:
    script = """
import importlib
import sys
import types

package = types.ModuleType("notebooklm")
package.__package__ = "notebooklm"
package.__path__ = [sys.argv[1]]
sys.modules["notebooklm"] = package
importlib.import_module(sys.argv[2])
"""
    leaves = (
        "notebooklm._web.transport.auth",
        "notebooklm._web.transport.error_injection",
        "notebooklm._web.transport.middleware.auth_refresh",
        "notebooklm._web.transport.runtime",
    )
    for leaf in leaves:
        subprocess.run(
            [sys.executable, "-I", "-c", script, str(SRC_ROOT), leaf],
            check=True,
            capture_output=True,
            text=True,
        )
