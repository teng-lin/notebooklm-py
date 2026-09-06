"""P0 coupling measurements and shrink-only ratchet semantics."""

from __future__ import annotations

from pathlib import Path

import pytest
from scripts.audit_backend_coupling import (
    BACKEND_STAGES,
    _scan_static_source,
    build_static_import_adjacency,
    build_static_projection,
    runtime_projection_growth,
    static_projection_growth,
)

from tests._baselines.registry import baseline_by_name

pytestmark = [pytest.mark.repo_lint, pytest.mark.refactor_qualification]


def _runtime_baseline() -> dict[str, object]:
    value = baseline_by_name("backend_runtime_coupling").load()
    assert isinstance(value, dict)
    return value


def _stage(backend: str, stage: str) -> dict[str, object]:
    backends = _runtime_baseline()["backends"]
    assert isinstance(backends, dict)
    backend_value = backends[backend]
    assert isinstance(backend_value, dict)
    stages = backend_value["stages"]
    assert isinstance(stages, dict)
    value = stages[stage]
    assert isinstance(value, dict)
    return value


def _reachable(adjacency: dict[str, set[str]], source: str, target: str) -> bool:
    pending = [source]
    seen: set[str] = set()
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        if current == target:
            return True
        pending.extend(adjacency.get(current, ()))
    return False


def test_public_entries_are_distinct_probes_with_current_clean_deltas() -> None:
    entries = _runtime_baseline()["public_entries"]
    assert isinstance(entries, dict)
    assert set(entries) == {
        "android_raw_api",
        "client_reexport",
        "legacy_client_attribute",
        "package",
        "raw_module",
        "web_raw_api",
    }
    deltas = {name: set(value["module_delta"]) for name, value in entries.items()}
    assert deltas["package"] == deltas["client_reexport"] == deltas["raw_module"]

    # Each explicit raw-class probe adds only its selected backend's raw
    # implementation.  The legacy lazy attribute intentionally resolves the
    # compatibility seam, while the Web raw probe resolves the Web/RPC facade.
    assert deltas["android_raw_api"] - deltas["package"] == {
        "notebooklm._android",
        "notebooklm._android.errors",
        "notebooklm._android.raw",
        "notebooklm._android.retry_policy",
    }
    assert deltas["legacy_client_attribute"] - deltas["android_raw_api"] == {
        "notebooklm._web",
        "notebooklm._web.transport",
        "notebooklm._web.transport.seams",
    }
    assert deltas["web_raw_api"] - deltas["package"] == {
        "notebooklm._web",
        "notebooklm._web.raw",
        "notebooklm.rpc",
        "notebooklm.rpc._identifiers",
        "notebooklm.rpc.types",
    }

    # Clean subprocesses resolve relative imports into exact module names;
    # selected raw probes therefore expose their concrete implementation edges.
    assert "notebooklm._android.raw" in deltas["android_raw_api"]
    assert "notebooklm._web.raw" in deltas["web_raw_api"]


def test_backend_stage_matrix_is_complete_and_records_every_dimension() -> None:
    backends = _runtime_baseline()["backends"]
    assert isinstance(backends, dict)
    assert set(backends) == set(BACKEND_STAGES)
    for backend, expected_stages in BACKEND_STAGES.items():
        backend_value = backends[backend]
        assert isinstance(backend_value, dict)
        stages = backend_value["stages"]
        assert isinstance(stages, dict)
        assert set(stages) == set(expected_stages)
        for stage in stages.values():
            assert set(stage) == {
                "backend_objects",
                "lifecycle",
                "module_counts",
                "module_delta",
                "network_destinations",
                "optional_android_module_delta",
                "profile_reads",
                "profile_writes",
            }


def test_current_android_homepage_compatibility_and_sidecar_boundary_are_explicit() -> None:
    built = _stage("android", "build_from_storage")
    assert built["network_destinations"] == {"GET https://notebook.google.com/": 1}
    assert built["profile_writes"] == {}

    constructed = _stage("android", "construct_direct")
    backend_objects = constructed["backend_objects"]
    assert isinstance(backend_objects, dict)
    assert not [name for name in backend_objects if name.startswith("notebooklm._web")]
    lifecycle = constructed["lifecycle"]
    assert isinstance(lifecycle, dict)
    expected_sidecar = "notebooklm._client_compat.LazyWebSidecar:deprecated-web-sidecar"
    assert lifecycle["transports"].count(expected_sidecar) == 1
    assert lifecycle["loop_participants"].count(expected_sidecar) == 1

    typed = _stage("android", "typed_operation")
    assert typed["network_destinations"] == {
        "GET https://notebook.google.com/": 1,
        "GRPC notebooklm-pa.googleapis.com:443": 1,
    }
    compatible = _stage("android", "deprecated_rpc_call")
    assert compatible["network_destinations"] == {
        "GET https://notebook.google.com/": 1,
        "GRPC notebooklm-pa.googleapis.com:443": 1,
        "POST https://notebook.google.com/_/LabsTailwindUi/data/batchexecute": 1,
    }
    assert compatible["backend_objects"]["notebooklm._web.transport.init.WebRuntime"] == 1


def test_web_object_graph_remains_android_object_free() -> None:
    for stage in BACKEND_STAGES["web"]:
        objects = _stage("web", stage)["backend_objects"]
        assert isinstance(objects, dict)
        assert not [name for name in objects if name.startswith("notebooklm._android")]


def test_core_backend_stages_do_not_eagerly_initialize_the_app_package() -> None:
    for backend, stages in BACKEND_STAGES.items():
        for stage in stages:
            modules = _stage(backend, stage)["module_delta"]
            assert isinstance(modules, list)
            assert not [name for name in modules if name.startswith("notebooklm._app")]


def test_backend_batch_facades_do_not_import_the_app_layer() -> None:
    adjacency = build_static_import_adjacency()
    for module in ("notebooklm._android.source_batch", "notebooklm._web.sources"):
        assert not [
            target
            for target in adjacency[module]
            if target == "notebooklm._app" or target.startswith("notebooklm._app.")
        ]


def test_static_scanner_resolves_relative_local_type_and_dynamic_imports() -> None:
    imports, dynamic = _scan_static_source(
        """
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from .._web.rows import ProjectRow

def load():
    from .._android import codec
    import importlib as loader
    return loader.import_module(".rows", "notebooklm._web")
""",
        module="notebooklm._types.synthetic",
        package="notebooklm._types",
        modules={"notebooklm._android.codec", "notebooklm._web.rows"},
    )

    selected = [
        (item.target, item.scope, item.scope_kind, item.type_only)
        for item in imports
        if item.target.startswith("notebooklm.")
    ]
    assert selected == [
        ("notebooklm._web.rows", None, "module", True),
        ("notebooklm._android.codec", "load", "function", False),
    ]
    assert [(item.callee, item.target, item.scope, item.type_only) for item in dynamic] == [
        ("loader.import_module", "notebooklm._web.rows", "load", False)
    ]


def test_static_projection_excludes_generated_android_protobuf(tmp_path: Path) -> None:
    source_root = tmp_path / "notebooklm"
    generated = source_root / "_android" / "proto" / "generated.py"
    authored = source_root / "_web" / "authored.py"
    generated.parent.mkdir(parents=True)
    authored.parent.mkdir(parents=True)
    generated.write_text("import notebooklm.client\n", encoding="utf-8")
    authored.write_text("from .. import client\n", encoding="utf-8")

    projection = build_static_projection(source_root)

    assert projection["summary"]["authored_modules"] == 1
    assert projection["subsystems"] == {"web": {"lines": 1, "modules": 1}}


def test_static_import_adjacency_keeps_same_subsystem_cycle_bridges(tmp_path: Path) -> None:
    source_root = tmp_path / "notebooklm"
    sources = {
        "rpc/types.py": "from notebooklm._web.wire import overrides\n",
        "_web/wire/overrides.py": "from . import bridge\n",
        "_web/wire/bridge.py": "from notebooklm.rpc import types\n",
    }
    for relative, source in sources.items():
        path = source_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")

    adjacency = build_static_import_adjacency(source_root)

    assert adjacency == {
        "notebooklm._web.wire.bridge": {"notebooklm.rpc.types"},
        "notebooklm._web.wire.overrides": {"notebooklm._web.wire.bridge"},
        "notebooklm.rpc.types": {"notebooklm._web.wire.overrides"},
    }
    assert _reachable(
        adjacency,
        "notebooklm._web.wire.overrides",
        "notebooklm.rpc.types",
    )


def test_rpc_identifier_boundary_is_one_way_in_the_all_scope_graph() -> None:
    """The compatibility lazy edge must never close a types/overrides SCC."""
    projection = build_static_projection()
    edges = projection["edges"]
    assert isinstance(edges, list)

    rpc_nodes = {
        "notebooklm.rpc.types",
        "notebooklm.rpc._identifiers",
        "notebooklm._web.wire.overrides",
    }
    boundary_edges = [
        (
            edge["source"],
            edge["target"],
            edge["kind"],
            edge["scope"],
            edge["scope_kind"],
            edge["type_only"],
        )
        for edge in edges
        if edge["source"] in rpc_nodes and edge["target"] in rpc_nodes
    ]
    assert boundary_edges == [
        (
            "notebooklm._web.wire.overrides",
            "notebooklm.rpc._identifiers",
            "from",
            None,
            "module",
            False,
        ),
        (
            "notebooklm.rpc.types",
            "notebooklm._web.wire.overrides",
            "from",
            "__getattr__",
            "function",
            False,
        ),
    ]

    adjacency = build_static_import_adjacency()
    assert _reachable(adjacency, "notebooklm.rpc.types", "notebooklm._web.wire.overrides")
    assert not _reachable(adjacency, "notebooklm._web.wire.overrides", "notebooklm.rpc.types")


def test_coupling_growth_policies_reject_replacement_and_count_growth() -> None:
    previous_runtime = {
        "public_entries": {
            "package": {"module_delta": ["notebooklm"], "optional_android_module_delta": []}
        },
        "backends": {
            "web": {
                "stages": {
                    "import_client": {
                        "module_delta": ["notebooklm"],
                        "optional_android_module_delta": [],
                        "backend_objects": {"notebooklm._web.Owner": 1},
                        "network_destinations": {},
                        "profile_reads": {},
                        "profile_writes": {},
                        "lifecycle": {"transports": [], "loop_participants": []},
                    }
                }
            }
        },
    }
    current_runtime = {
        "public_entries": {
            "package": {
                "module_delta": ["notebooklm", "notebooklm._web.new"],
                "optional_android_module_delta": [],
            }
        },
        "backends": {
            "web": {
                "stages": {
                    "import_client": {
                        "module_delta": ["notebooklm"],
                        "optional_android_module_delta": [],
                        "backend_objects": {"notebooklm._web.Owner": 2},
                        "network_destinations": {},
                        "profile_reads": {},
                        "profile_writes": {},
                        "lifecycle": {"transports": [], "loop_participants": []},
                    }
                }
            }
        },
    }
    assert runtime_projection_growth(previous_runtime, current_runtime) == [
        "package.module_delta: new notebooklm._web.new",
        "web.import_client.backend_objects.notebooklm._web.Owner: 1 -> 2",
    ]

    edge = {
        "source": "notebooklm.client",
        "target": "notebooklm._web.runtime",
        "kind": "from",
        "scope": None,
        "scope_kind": "module",
        "type_only": False,
        "lineno": 1,
    }
    assert static_projection_growth(
        {"edges": [], "dynamic_imports": []},
        {"edges": [edge], "dynamic_imports": []},
    ) == [
        'edges: new {"kind": "from", "scope": null, "scope_kind": "module", '
        '"source": "notebooklm.client", "target": "notebooklm._web.runtime", '
        '"type_only": false}'
    ]
