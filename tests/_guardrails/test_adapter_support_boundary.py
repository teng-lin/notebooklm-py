"""Architecture guards for infrastructure shared by MCP and REST adapters."""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import notebooklm._adapter_support as support
import notebooklm._loop_bound as loop_bound
import notebooklm._redact as redact_module
import notebooklm._runtime.config as runtime_config
import notebooklm._serving as serving

SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "notebooklm"
ADAPTER_ROOTS = (SRC_ROOT / "mcp", SRC_ROOT / "server")
SHARED_INFRASTRUCTURE = frozenset(
    {
        "notebooklm._loop_bound",
        "notebooklm._redact",
        "notebooklm._runtime.config",
        "notebooklm._serving",
    }
)
EXPECTED_SUPPORT_CONSUMERS = frozenset(
    {
        "mcp/__main__.py",
        "mcp/_chattasks.py",
        "mcp/_clientprovider.py",
        "mcp/_errors.py",
        "mcp/_host_guard.py",
        "mcp/server.py",
        "server/__main__.py",
        "server/_auth.py",
        "server/_errors.py",
        "server/_limits.py",
        "server/app.py",
        "server/routes/meta.py",
    }
)


def _module_identity(path: Path) -> tuple[str, str]:
    relative = path.relative_to(SRC_ROOT).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
        module = ".".join(("notebooklm", *parts))
        return module, module
    module = ".".join(("notebooklm", *parts))
    return module, module.rpartition(".")[0]


def _resolved_imports(path: Path) -> set[str]:
    """Resolve absolute, relative, and recognized dynamic imports."""
    _, package = _module_identity(path)
    targets: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
        if isinstance(node, ast.Import):
            targets.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                module = importlib.util.resolve_name(f"{'.' * node.level}{module}", package)
            targets.add(module)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name | ast.Attribute)
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            function_name = node.func.id if isinstance(node.func, ast.Name) else node.func.attr
            if function_name in {"__import__", "import_module"}:
                targets.add(node.args[0].value)
    return targets


def test_adapters_use_the_shared_support_leaf_for_repeated_infrastructure() -> None:
    support_consumers: set[str] = set()
    direct_violations: list[str] = []
    for root in ADAPTER_ROOTS:
        for path in sorted(root.rglob("*.py")):
            relative = path.relative_to(SRC_ROOT).as_posix()
            imports = _resolved_imports(path)
            if "notebooklm._adapter_support" in imports:
                support_consumers.add(relative)
            forbidden = sorted(imports & SHARED_INFRASTRUCTURE)
            direct_violations.extend(f"{relative}: {target}" for target in forbidden)

    assert direct_violations == []
    assert support_consumers == EXPECTED_SUPPORT_CONSUMERS


def test_adapter_specific_atomic_io_dependency_stays_explicit() -> None:
    consumers = {
        path.relative_to(SRC_ROOT).as_posix()
        for root in ADAPTER_ROOTS
        for path in root.rglob("*.py")
        if "notebooklm._atomic_io" in _resolved_imports(path)
    }
    assert consumers == {"mcp/_oauth.py"}


def test_support_leaf_preserves_canonical_identities() -> None:
    assert support.LoopBoundPrimitive is loop_bound.LoopBoundPrimitive
    assert support.redact is redact_module.redact
    assert (
        support.DEFAULT_SERVER_KEEPALIVE_INTERVAL
        == runtime_config.DEFAULT_SERVER_KEEPALIVE_INTERVAL
    )
    assert support.LOOPBACK_HOSTNAMES is serving.LOOPBACK_HOSTNAMES
    assert support.addr_is_loopback is serving.addr_is_loopback
    assert support.check_bind_allowed is serving.check_bind_allowed
    assert support.host_header_is_loopback is serving.host_header_is_loopback
    assert support.is_loopback is serving.is_loopback


def test_support_leaf_exports_only_adapter_hosting_primitives() -> None:
    assert support.__all__ == [
        "DEFAULT_SERVER_KEEPALIVE_INTERVAL",
        "LOOPBACK_HOSTNAMES",
        "LoopBoundPrimitive",
        "addr_is_loopback",
        "check_bind_allowed",
        "client_generation_epoch",
        "host_header_is_loopback",
        "is_loopback",
        "redact",
    ]
