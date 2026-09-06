#!/usr/bin/env python3
"""Report runtime and static backend coupling without importing the package in-process.

The runtime projection starts a clean interpreter for every public entry point and
for each backend's cumulative construction/lifecycle workflow.  This matters when
the caller is pytest: deriving a footprint from pytest's already-populated
``sys.modules`` would silently turn eager imports into false negatives.

The static projection resolves relative imports, distinguishes module-, class-,
and function-local edges, preserves ``TYPE_CHECKING`` knowledge separately, and
reports recognized dynamic imports.  Generated Android protobuf modules are
excluded from authored-code totals and edges.
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from collections import Counter, deque
from dataclasses import dataclass
from pathlib import Path
from types import FunctionType, MethodType, ModuleType
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE_ROOT = REPO_ROOT / "src" / "notebooklm"

PUBLIC_ENTRY_PROBES: dict[str, str] = {
    "android_raw_api": "from notebooklm.raw import AndroidRawAPI",
    "client_reexport": "from notebooklm import NotebookLMClient",
    "package": "import notebooklm",
    "raw_module": "import notebooklm.raw",
    "web_raw_api": "from notebooklm.raw import WebRawAPI",
    # This explicit access is the comparison point for P1's lazy compatibility
    # attributes.  It is intentionally eager and baseline-identical today.
    "legacy_client_attribute": "from notebooklm.client import ClientSeams",
}

BACKEND_STAGES: dict[str, tuple[str, ...]] = {
    "web": (
        "import_client",
        "construct_direct",
        "build_from_storage",
        "open_close",
        "typed_operation",
    ),
    "android": (
        "import_client",
        "construct_direct",
        "build_from_storage",
        "open_close",
        "typed_operation",
        "deprecated_rpc_call",
    ),
}

_GENERATED_PROTO_PARTS = ("_android", "proto")
_PROFILE_READ_METHODS = (
    "read_master_token",
    "read_document",
    "read_session",
    "_read_account_document",
    "read_account",
    "_read_cookie_document",
)
_PROFILE_WRITE_METHODS = (
    "write_master_token",
    "replace_from_remint",
    "replace_from_login",
    "replace_minted_session",
    "update_account",
    "_update_account_if_document_unchanged",
    "clear_account",
    "merge_cookie_observation",
    "merge_legacy_cookie_observation",
)


def _is_notebooklm_module(name: str) -> bool:
    return name == "notebooklm" or name.startswith("notebooklm.")


def _is_optional_android_module(name: str) -> bool:
    return (
        name == "grpc"
        or name.startswith("grpc.")
        or name == "gpsoauth"
        or name.startswith("gpsoauth.")
        or name == "google.protobuf"
        or name.startswith("google.protobuf.")
    )


def _subsystem(name: str) -> str:
    for prefix, subsystem in (
        ("notebooklm._web", "web"),
        ("notebooklm._android", "android"),
        ("notebooklm._auth", "auth"),
        ("notebooklm._runtime", "runtime"),
        ("notebooklm._types", "types"),
        ("notebooklm._app", "app"),
        ("notebooklm.cli", "cli"),
        ("notebooklm.mcp", "mcp"),
        ("notebooklm.server", "server"),
        ("notebooklm.rpc", "rpc"),
    ):
        if name == prefix or name.startswith(f"{prefix}."):
            return subsystem
    return "root"


def _counted(values: list[str]) -> dict[str, int]:
    return dict(sorted(Counter(values).items()))


def _module_measurement(startup_modules: set[str]) -> tuple[list[str], dict[str, int], list[str]]:
    delta = sorted(
        name for name in set(sys.modules) - startup_modules if _is_notebooklm_module(name)
    )
    optional = sorted(
        name for name in set(sys.modules) - startup_modules if _is_optional_android_module(name)
    )
    return delta, _counted([_subsystem(name) for name in delta]), optional


def _backend_objects(root: object | None) -> dict[str, int]:
    """Count concrete backend objects reachable through one assembled client graph."""
    if root is None:
        return {}
    queue: deque[object] = deque([root])
    seen: set[int] = set()
    found: Counter[str] = Counter()
    while queue:
        value = queue.popleft()
        identity = id(value)
        if identity in seen:
            continue
        seen.add(identity)
        if isinstance(value, (str, bytes, bytearray, int, float, complex, bool, type(None))):
            continue
        if isinstance(value, (ModuleType, FunctionType, MethodType, type)):
            continue

        value_type = type(value)
        module = value_type.__module__
        if module.startswith("notebooklm._web") or module.startswith("notebooklm._android"):
            found[f"{module}.{value_type.__qualname__}"] += 1

        if isinstance(value, dict):
            queue.extend(value.keys())
            queue.extend(value.values())
        elif isinstance(value, (list, tuple, set, frozenset, deque)):
            queue.extend(value)
        elif module.startswith("notebooklm"):
            try:
                queue.extend(vars(value).values())
            except TypeError:
                pass
            for owner in value_type.__mro__:
                slots = owner.__dict__.get("__slots__", ())
                if isinstance(slots, str):
                    slots = (slots,)
                for slot in slots:
                    if slot in {"__dict__", "__weakref__"}:
                        continue
                    try:
                        queue.append(getattr(value, slot))
                    except AttributeError:
                        pass
    return dict(sorted(found.items()))


def _lifecycle_measurement(client: object | None) -> dict[str, list[str]]:
    if client is None:
        return {"loop_participants": [], "transports": []}
    lifecycle = client._lifecycle  # type: ignore[attr-defined]

    def labels(values: tuple[object, ...]) -> list[str]:
        result: list[str] = []
        for value in values:
            value_type = type(value)
            label = f"{value_type.__module__}.{value_type.__qualname__}"
            name = getattr(value, "name", None)
            if isinstance(name, str):
                label = f"{label}:{name}"
            result.append(label)
        return result

    return {
        "loop_participants": labels(lifecycle._loop_participants),
        "transports": labels(lifecycle._transports),
    }


def _runtime_stage_measurement(
    *,
    startup_modules: set[str],
    client: object | None,
    network_calls: list[str],
    profile_reads: list[str],
    profile_writes: list[str],
) -> dict[str, object]:
    modules, module_counts, optional = _module_measurement(startup_modules)
    return {
        "module_delta": modules,
        "module_counts": module_counts,
        "optional_android_module_delta": optional,
        "backend_objects": _backend_objects(client),
        "network_destinations": _counted(network_calls),
        "profile_reads": _counted(profile_reads),
        "profile_writes": _counted(profile_writes),
        "lifecycle": _lifecycle_measurement(client),
    }


def _runtime_entry_worker(source_root: Path, statement: str) -> dict[str, object]:
    sys.path.insert(0, str(source_root.parent))
    startup_modules = set(sys.modules)
    exec(statement, {})
    modules, module_counts, optional = _module_measurement(startup_modules)
    return {
        "module_delta": modules,
        "module_counts": module_counts,
        "optional_android_module_delta": optional,
    }


def _storage_payload() -> dict[str, object]:
    return {
        "cookies": [
            {"name": "SID", "value": "sid", "domain": ".google.com", "path": "/"},
            {"name": "HSID", "value": "hsid", "domain": ".google.com", "path": "/"},
            {"name": "SSID", "value": "ssid", "domain": ".google.com", "path": "/"},
            {
                "name": "__Secure-1PSIDTS",
                "value": "psidts",
                "domain": ".google.com",
                "path": "/",
            },
        ],
        "origins": [],
    }


def _install_profile_instrumentation(profile_reads: list[str], profile_writes: list[str]) -> None:
    from notebooklm._auth.profile_store import ProfileStore

    def wrap(name: str, destination: list[str]) -> None:
        original = getattr(ProfileStore, name)

        def recording(self: object, *args: object, **kwargs: object) -> object:
            destination.append(f"ProfileStore.{name}")
            return original(self, *args, **kwargs)

        setattr(ProfileStore, name, recording)

    for method in _PROFILE_READ_METHODS:
        wrap(method, profile_reads)
    for method in _PROFILE_WRITE_METHODS:
        wrap(method, profile_writes)


def _recording_http_factory(network_calls: list[str]) -> Any:
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        destination = (
            f"{request.method} {request.url.scheme}://{request.url.host}{request.url.path}"
        )
        network_calls.append(destination)
        if request.url.path == "/":
            return httpx.Response(
                200,
                text='"SNlM0e":"csrf" "FdrFJe":"session"',
                request=request,
            )
        if request.url.path.endswith("/RotateCookies"):
            return httpx.Response(200, request=request)
        if request.url.path.endswith("/batchexecute"):
            rpc_id = request.url.params.get("rpcids", "")
            payload = json.dumps(
                [["wrb.fr", rpc_id, "[]", None, None, None, "generic"]],
                separators=(",", ":"),
            )
            body = f")]}}'\n\n{len(payload.encode())}\n{payload}\n"
            return httpx.Response(200, text=body, request=request)
        raise AssertionError(f"unrecognized coupling-probe request: {request.method} {request.url}")

    def factory(**kwargs: object) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

    return factory


async def _runtime_backend_worker(source_root: Path, backend: str) -> dict[str, object]:
    import time
    from types import MethodType

    sys.path.insert(0, str(source_root.parent))
    startup_modules = set(sys.modules)
    network_calls: list[str] = []
    profile_reads: list[str] = []
    profile_writes: list[str] = []
    stages: dict[str, object] = {}

    from notebooklm.client import NotebookLMClient

    stages["import_client"] = _runtime_stage_measurement(
        startup_modules=startup_modules,
        client=None,
        network_calls=network_calls,
        profile_reads=profile_reads,
        profile_writes=profile_writes,
    )

    import httpx

    from notebooklm.auth import AuthTokens

    with tempfile.TemporaryDirectory(prefix="notebooklm-coupling-") as temp_dir:
        profile_dir = Path(temp_dir)
        storage_path = profile_dir / "storage_state.json"
        storage_path.write_text(json.dumps(_storage_payload()), encoding="utf-8")
        (profile_dir / "master_token.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "email": "probe@example.com",
                    "android_id": "1234",
                    "master_token": "probe-master-secret",
                }
            ),
            encoding="utf-8",
        )

        auth = AuthTokens(
            cookies={"SID": "sid", "HSID": "hsid", "SSID": "ssid"},
            csrf_token="csrf",
            session_id="session",
            storage_path=storage_path,
            cookie_jar=httpx.Cookies(
                {
                    "SID": "sid",
                    "HSID": "hsid",
                    "SSID": "ssid",
                }
            ),
        )
        client = NotebookLMClient(auth, backend=backend)
        stages["construct_direct"] = _runtime_stage_measurement(
            startup_modules=startup_modules,
            client=client,
            network_calls=network_calls,
            profile_reads=profile_reads,
            profile_writes=profile_writes,
        )

        import notebooklm._curl_cffi_transport as curl_transport

        http_factory = _recording_http_factory(network_calls)
        curl_transport.resolve_transport_factory = lambda: http_factory
        _install_profile_instrumentation(profile_reads, profile_writes)

        wrapper = NotebookLMClient.from_storage(path=str(storage_path), backend=backend)
        client = await wrapper._build()
        stages["build_from_storage"] = _runtime_stage_measurement(
            startup_modules=startup_modules,
            client=client,
            network_calls=network_calls,
            profile_reads=profile_reads,
            profile_writes=profile_writes,
        )

        if backend == "android":
            android = client._android_runtime
            assert android is not None
            from notebooklm._auth.mint_service import MintedOAuthToken

            async def mint_oauth(_record: object, _spec: object) -> MintedOAuthToken:
                return MintedOAuthToken(token="probe-bearer", expires_at=int(time.time()) + 3600)

            android.bearer_provider._oauth_minter.mint_oauth = mint_oauth

        await client.__aenter__()
        await client.close()
        stages["open_close"] = _runtime_stage_measurement(
            startup_modules=startup_modules,
            client=client,
            network_calls=network_calls,
            profile_reads=profile_reads,
            profile_writes=profile_writes,
        )

        await client.__aenter__()
        if backend == "web":
            await client.notebooks.list()
        else:
            android = client._android_runtime
            assert android is not None

            async def unary(
                _session: object,
                _method: str,
                _request: object,
                **kwargs: object,
            ) -> object:
                epoch = android.session.active_epoch
                assert epoch is not None
                await android.bearer_provider.get(epoch)
                network_calls.append("GRPC notebooklm-pa.googleapis.com:443")
                response_type = kwargs["response_type"]
                assert isinstance(response_type, type)
                return response_type()

            android.session.unary = MethodType(unary, android.session)
            await client.notebooks.list()
        await client.close()
        stages["typed_operation"] = _runtime_stage_measurement(
            startup_modules=startup_modules,
            client=client,
            network_calls=network_calls,
            profile_reads=profile_reads,
            profile_writes=profile_writes,
        )

        if backend == "android":
            from notebooklm.rpc import RPCMethod

            await client.__aenter__()
            await client.rpc_call(RPCMethod.LIST_NOTEBOOKS, [None, 1, None, [2]])
            await client.close()
            stages["deprecated_rpc_call"] = _runtime_stage_measurement(
                startup_modules=startup_modules,
                client=client,
                network_calls=network_calls,
                profile_reads=profile_reads,
                profile_writes=profile_writes,
            )

    return {"backend": backend, "stages": stages}


def _clean_worker(*arguments: str) -> object:
    command = [sys.executable, "-I", str(Path(__file__).resolve()), *arguments]
    env = os.environ.copy()
    for name in tuple(env):
        if name.startswith("NOTEBOOKLM_") or name == "PYTHONPATH":
            env.pop(name, None)
    completed = subprocess.run(
        command,
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"coupling audit worker failed ({' '.join(arguments)}):\n{completed.stderr}"
        )
    return json.loads(completed.stdout)


def build_runtime_projection() -> dict[str, object]:
    """Derive public-entry and backend-stage measurements in clean interpreters."""
    entries = {
        name: _clean_worker("--worker-entry", statement)
        for name, statement in sorted(PUBLIC_ENTRY_PROBES.items())
    }
    backends = {
        backend: _clean_worker("--worker-backend", backend) for backend in sorted(BACKEND_STAGES)
    }
    return {"version": 1, "public_entries": entries, "backends": backends}


def _mapping(value: object, key: str) -> dict[str, object]:
    if not isinstance(value, dict) or not isinstance(value.get(key), dict):
        raise ValueError(f"backend coupling baseline must contain a {key!r} mapping")
    return value[key]


def _set_growth(before: object, after: object, label: str) -> list[str]:
    if not isinstance(before, list) or not all(isinstance(item, str) for item in before):
        raise ValueError(f"{label} must be a string list")
    if not isinstance(after, list) or not all(isinstance(item, str) for item in after):
        raise ValueError(f"{label} must be a string list")
    return [f"{label}: new {item}" for item in sorted(set(after) - set(before))]


def _count_growth(before: object, after: object, label: str) -> list[str]:
    if not isinstance(before, dict) or not isinstance(after, dict):
        raise ValueError(f"{label} must be an integer mapping")
    growth: list[str] = []
    for key, current in sorted(after.items()):
        previous = before.get(key, 0)
        if (
            not isinstance(key, str)
            or not isinstance(previous, int)
            or not isinstance(current, int)
        ):
            raise ValueError(f"{label} must be an integer mapping")
        if current > previous:
            growth.append(f"{label}.{key}: {previous} -> {current}")
    return growth


def runtime_projection_growth(previous: object, current: object) -> list[str]:
    """Describe additions/increases across every shrink-only runtime dimension."""
    growth: list[str] = []
    previous_entries = _mapping(previous, "public_entries")
    current_entries = _mapping(current, "public_entries")
    for probe, after_value in sorted(current_entries.items()):
        before_value = previous_entries.get(probe, {})
        if not isinstance(before_value, dict) or not isinstance(after_value, dict):
            raise ValueError("public entry measurements must be mappings")
        for key in ("module_delta", "optional_android_module_delta"):
            growth.extend(
                _set_growth(before_value.get(key, []), after_value.get(key, []), f"{probe}.{key}")
            )

    previous_backends = _mapping(previous, "backends")
    current_backends = _mapping(current, "backends")
    for backend, after_backend in sorted(current_backends.items()):
        before_backend = previous_backends.get(backend, {})
        if not isinstance(before_backend, dict) or not isinstance(after_backend, dict):
            raise ValueError("backend measurements must be mappings")
        before_stages = _mapping(before_backend, "stages")
        after_stages = _mapping(after_backend, "stages")
        for stage, after_value in sorted(after_stages.items()):
            before_value = before_stages.get(stage, {})
            if not isinstance(before_value, dict) or not isinstance(after_value, dict):
                raise ValueError("stage measurements must be mappings")
            label = f"{backend}.{stage}"
            for key in ("module_delta", "optional_android_module_delta"):
                growth.extend(
                    _set_growth(
                        before_value.get(key, []),
                        after_value.get(key, []),
                        f"{label}.{key}",
                    )
                )
            for key in (
                "backend_objects",
                "network_destinations",
                "profile_reads",
                "profile_writes",
            ):
                growth.extend(
                    _count_growth(
                        before_value.get(key, {}),
                        after_value.get(key, {}),
                        f"{label}.{key}",
                    )
                )
            before_lifecycle = _mapping(before_value, "lifecycle")
            after_lifecycle = _mapping(after_value, "lifecycle")
            for key in ("transports", "loop_participants"):
                growth.extend(
                    _set_growth(
                        before_lifecycle.get(key, []),
                        after_lifecycle.get(key, []),
                        f"{label}.lifecycle.{key}",
                    )
                )
    return growth


@dataclass(frozen=True)
class StaticImport:
    source: str
    target: str
    kind: str
    scope: str | None
    scope_kind: str
    type_only: bool
    lineno: int


@dataclass(frozen=True)
class DynamicImport:
    source: str
    callee: str
    target: str | None
    expression: str
    scope: str | None
    scope_kind: str
    type_only: bool
    lineno: int


def _is_type_checking_guard(node: ast.AST) -> bool:
    return (isinstance(node, ast.Name) and node.id == "TYPE_CHECKING") or (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "typing"
        and node.attr == "TYPE_CHECKING"
    )


def _module_identity(path: Path, source_root: Path) -> tuple[str, str]:
    relative = path.relative_to(source_root)
    parts = list(relative.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
        module = ".".join(("notebooklm", *parts))
        return module, module
    module = ".".join(("notebooklm", *parts))
    return module, module.rpartition(".")[0]


class _StaticVisitor(ast.NodeVisitor):
    def __init__(self, *, source: str, package: str, modules: set[str]) -> None:
        self.source = source
        self.package = package
        self.modules = modules
        self.scopes: list[tuple[str, str]] = []
        self.type_only = False
        self.imports: list[StaticImport] = []
        self.dynamic: list[DynamicImport] = []
        self.importlib_aliases = {"importlib"}
        self.import_module_aliases = {"import_module"}

    @property
    def scope(self) -> str | None:
        return ".".join(name for name, _kind in self.scopes) or None

    @property
    def scope_kind(self) -> str:
        return self.scopes[-1][1] if self.scopes else "module"

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.scopes.append((node.name, "class"))
        self.generic_visit(node)
        self.scopes.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.scopes.append((node.name, "function"))
        self.generic_visit(node)
        self.scopes.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_If(self, node: ast.If) -> None:
        if not _is_type_checking_guard(node.test):
            self.generic_visit(node)
            return
        previous = self.type_only
        self.type_only = True
        for child in node.body:
            self.visit(child)
        self.type_only = previous
        for child in node.orelse:
            self.visit(child)

    def _record(self, target: str, kind: str, lineno: int) -> None:
        self.imports.append(
            StaticImport(
                source=self.source,
                target=target,
                kind=kind,
                scope=self.scope,
                scope_kind=self.scope_kind,
                type_only=self.type_only,
                lineno=lineno,
            )
        )

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._record(alias.name, "import", node.lineno)
            if alias.name == "importlib":
                self.importlib_aliases.add(alias.asname or alias.name)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        if node.level:
            module = importlib.util.resolve_name(f"{'.' * node.level}{module}", self.package)
        for alias in node.names:
            candidate = f"{module}.{alias.name}" if module else alias.name
            target = candidate if candidate in self.modules else module
            self._record(target, "from", node.lineno)
            if module == "importlib" and alias.name == "import_module":
                self.import_module_aliases.add(alias.asname or alias.name)

    def visit_Call(self, node: ast.Call) -> None:
        callee: str | None = None
        if isinstance(node.func, ast.Name):
            if node.func.id == "__import__" or node.func.id in self.import_module_aliases:
                callee = node.func.id
        elif (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in self.importlib_aliases
            and node.func.attr == "import_module"
        ):
            callee = f"{node.func.value.id}.import_module"
        if callee is not None and node.args:
            expression = ast.unparse(node.args[0])
            target: str | None = None
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                target = first.value
                if target.startswith("."):
                    package = self.package
                    if len(node.args) > 1:
                        second = node.args[1]
                        if isinstance(second, ast.Constant) and isinstance(second.value, str):
                            package = second.value
                    target = importlib.util.resolve_name(target, package)
            self.dynamic.append(
                DynamicImport(
                    source=self.source,
                    callee=callee,
                    target=target,
                    expression=expression,
                    scope=self.scope,
                    scope_kind=self.scope_kind,
                    type_only=self.type_only,
                    lineno=node.lineno,
                )
            )
        self.generic_visit(node)


def _authored_paths(source_root: Path) -> list[Path]:
    return [
        path
        for path in sorted(source_root.rglob("*.py"))
        if path.relative_to(source_root).parts[:2] != _GENERATED_PROTO_PARTS
    ]


def _scan_static_source(
    source: str,
    *,
    module: str,
    package: str,
    modules: set[str],
) -> tuple[list[StaticImport], list[DynamicImport]]:
    visitor = _StaticVisitor(source=module, package=package, modules=modules)
    visitor.visit(ast.parse(source))
    return visitor.imports, visitor.dynamic


def build_static_import_adjacency(source_root: Path = SOURCE_ROOT) -> dict[str, set[str]]:
    """Build the complete authored-module import adjacency for cycle checks."""
    paths = _authored_paths(source_root)
    identities = {path: _module_identity(path, source_root) for path in paths}
    modules = {module for module, _package in identities.values()}
    adjacency = {module: set() for module in modules}
    for path in paths:
        module, package = identities[path]
        imports, _dynamic = _scan_static_source(
            path.read_text(encoding="utf-8"),
            module=module,
            package=package,
            modules=modules,
        )
        adjacency[module].update(item.target for item in imports if item.target in modules)
    return adjacency


def build_static_projection(source_root: Path = SOURCE_ROOT) -> dict[str, object]:
    """Build the deterministic authored cross-subsystem import report."""
    paths = _authored_paths(source_root)
    identities = {path: _module_identity(path, source_root) for path in paths}
    modules = {module for module, _package in identities.values()}
    imports: list[StaticImport] = []
    dynamic: list[DynamicImport] = []
    subsystem_lines: Counter[str] = Counter()
    subsystem_modules: Counter[str] = Counter()
    for path in paths:
        module, package = identities[path]
        source = path.read_text(encoding="utf-8")
        found_imports, found_dynamic = _scan_static_source(
            source,
            module=module,
            package=package,
            modules=modules,
        )
        imports.extend(found_imports)
        dynamic.extend(found_dynamic)
        subsystem = _subsystem(module)
        subsystem_lines[subsystem] += len(source.splitlines())
        subsystem_modules[subsystem] += 1

    # ``from module import A, B`` is one module edge, not two symbol edges.
    # Keep the earliest source location for a repeated exact edge so the report
    # remains actionable without inflating the graph's fan-out totals.
    cross_edge_by_identity: dict[tuple[object, ...], StaticImport] = {}
    for item in imports:
        if not _is_notebooklm_module(item.target) or _subsystem(item.source) == _subsystem(
            item.target
        ):
            continue
        identity = (
            item.source,
            item.target,
            item.kind,
            item.scope,
            item.scope_kind,
            item.type_only,
        )
        cross_edge_by_identity.setdefault(identity, item)
    cross_edges = list(cross_edge_by_identity.values())
    dynamic_rows = [item.__dict__ for item in dynamic]
    edge_rows = [item.__dict__ for item in cross_edges]
    edge_rows.sort(
        key=lambda item: (
            str(item["source"]),
            int(item["lineno"]),
            str(item["target"]),
            str(item["scope"]),
        )
    )
    dynamic_rows.sort(
        key=lambda item: (
            str(item["source"]),
            int(item["lineno"]),
            str(item["callee"]),
        )
    )
    return {
        "version": 1,
        "summary": {
            "authored_modules": len(paths),
            "authored_lines": sum(subsystem_lines.values()),
            "cross_subsystem_edges": len(edge_rows),
            "module_level_edges": sum(item.scope_kind == "module" for item in cross_edges),
            "function_local_edges": sum(item.scope_kind == "function" for item in cross_edges),
            "class_local_edges": sum(item.scope_kind == "class" for item in cross_edges),
            "type_only_edges": sum(item.type_only for item in cross_edges),
            "dynamic_import_calls": len(dynamic_rows),
        },
        "subsystems": {
            name: {"lines": subsystem_lines[name], "modules": subsystem_modules[name]}
            for name in sorted(subsystem_modules)
        },
        "edges": edge_rows,
        "dynamic_imports": dynamic_rows,
    }


def _edge_identity(value: object, section: str) -> set[str]:
    if not isinstance(value, dict) or not isinstance(value.get(section), list):
        raise ValueError(f"static coupling baseline must contain a {section!r} list")
    result: set[str] = set()
    for row in value[section]:
        if not isinstance(row, dict):
            raise ValueError(f"static coupling {section} rows must be mappings")
        if section == "edges":
            keys = ("source", "target", "kind", "scope", "scope_kind", "type_only")
        else:
            keys = ("source", "callee", "target", "expression", "scope", "scope_kind", "type_only")
        result.add(json.dumps({key: row.get(key) for key in keys}, sort_keys=True))
    return result


def static_projection_growth(previous: object, current: object) -> list[str]:
    """Describe new cross-subsystem or dynamic import identities."""
    growth: list[str] = []
    for section in ("edges", "dynamic_imports"):
        before = _edge_identity(previous, section)
        after = _edge_identity(current, section)
        growth.extend(f"{section}: new {identity}" for identity in sorted(after - before))
    return growth


def _worker_main(args: argparse.Namespace) -> int:
    if args.worker_entry is not None:
        result = _runtime_entry_worker(Path(args.source_root), args.worker_entry)
    else:
        import asyncio

        result = asyncio.run(_runtime_backend_worker(Path(args.source_root), args.worker_backend))
    json.dump(result, sys.stdout, sort_keys=True)
    sys.stdout.write("\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=("runtime", "static", "all"),
        default="all",
        help="projection to print (default: all)",
    )
    parser.add_argument("--source-root", type=Path, default=SOURCE_ROOT)
    worker = parser.add_mutually_exclusive_group()
    worker.add_argument("--worker-entry")
    worker.add_argument("--worker-backend", choices=tuple(BACKEND_STAGES))
    args = parser.parse_args(argv)
    if args.worker_entry is not None or args.worker_backend is not None:
        return _worker_main(args)

    projection: object
    if args.mode == "runtime":
        projection = build_runtime_projection()
    elif args.mode == "static":
        projection = build_static_projection(args.source_root)
    else:
        projection = {
            "runtime": build_runtime_projection(),
            "static": build_static_projection(args.source_root),
        }
    json.dump(projection, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
