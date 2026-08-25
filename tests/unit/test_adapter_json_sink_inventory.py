"""Tests for the fail-closed adapter JSON sink inventory."""

from __future__ import annotations

import copy
import json
import shutil
from collections.abc import Callable
from pathlib import Path, PurePath, PureWindowsPath

import pytest
from scripts.audit_adapter_json_sinks import (
    _adapter_function_call_graph,
    assert_exact_sink_dispositions,
    assert_no_unreviewed_cli_json_serialization,
    assert_no_unreviewed_direct_json_emissions,
    assert_supported_adapter_registrations,
    discover_adapter_json_sinks,
    fingerprint_adapter_helpers,
)
from scripts.audit_adapter_json_sinks import (
    _relative_source_path as _sink_relative_source_path,
)
from scripts.audit_adapter_projection_paths import (
    PrivateDataclassProjectionPath,
    discover_private_dataclass_projection_paths,
)

import notebooklm
from tests._baselines import adapter_sink_reachability as reachability
from tests._baselines.json_envelope_contracts import (
    _relative_source_path as _contract_relative_source_path,
)

pytestmark = pytest.mark.repo_lint


def _source_root() -> Path:
    return Path(notebooklm.__file__).resolve().parents[1]


def _write_adapter_source(root: Path, relative_path: str, source: str) -> None:
    path = root / "notebooklm" / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


def test_transitive_graph_retains_each_branch_local_callable_alias(tmp_path: Path) -> None:
    _write_adapter_source(
        tmp_path,
        "sample.py",
        """
def left():
    return 1

def right():
    return 2

def dispatch(use_left):
    if use_left:
        builder = left
    else:
        builder = right
    return builder()
""",
    )

    graph, _functions = _adapter_function_call_graph(tmp_path)

    assert graph["notebooklm.sample.dispatch"] == {
        "notebooklm.sample.left",
        "notebooklm.sample.right",
    }


@pytest.mark.parametrize(
    "relative_source_path",
    [_sink_relative_source_path, _contract_relative_source_path],
)
def test_relative_source_paths_are_posix_on_windows(
    relative_source_path: Callable[[PurePath, PurePath], str],
) -> None:
    root = PureWindowsPath(r"C:\repo\src")
    path = root / "notebooklm" / "cli" / "artifact_cmd.py"

    assert relative_source_path(path, root) == "notebooklm/cli/artifact_cmd.py"


def test_inventory_covers_every_current_terminal_adapter_site() -> None:
    sinks = discover_adapter_json_sinks(_source_root())

    by_channel = {
        channel: [sink for sink in sinks if sink.channel == channel]
        for channel in (
            "cli --json",
            "mcp tool result",
            "mcp auxiliary response",
            "rest response",
        )
    }
    assert {channel: len(rows) for channel, rows in by_channel.items()} == {
        "cli --json": 160,
        "mcp tool result": 95,
        "mcp auxiliary response": 34,
        "rest response": 61,
    }
    assert {
        role: sum(sink.site_role == role for sink in by_channel["cli --json"])
        for role in ("projection", "error-projection", "forwarding-infrastructure")
    } == {
        "projection": 105,
        "error-projection": 49,
        "forwarding-infrastructure": 6,
    }
    assert len({sink.id for sink in sinks}) == len(sinks)
    assert all(sink.expression_fingerprint.startswith("sha256:") for sink in sinks)
    assert all(sink.owner_fingerprint.startswith("sha256:") for sink in sinks)
    assert_no_unreviewed_direct_json_emissions(sinks)
    assert_no_unreviewed_cli_json_serialization(_source_root())
    assert_supported_adapter_registrations(_source_root())


def test_private_dto_catalog_covers_every_annotation_proven_public_model_path() -> None:
    rows = discover_private_dataclass_projection_paths()

    # 37 since P10 R6.4: ``_ImportProbeOutcome`` stopped carrying public
    # ``ResearchSource`` rows when the reconciliation moved onto neutral
    # ``ResearchImportCandidate`` records (defect N1), so that annotation-proven
    # private path to a public model no longer exists.
    assert len(rows) == 37
    triples = {(row.private_model, row.field_path, row.public_model) for row in rows}
    assert (
        "notebooklm._app.source_mutations.SourceRenameResult",
        "source",
        "notebooklm.types.Source",
    ) in triples
    assert (
        "notebooklm._app.source_mutations.SourceRefreshResult",
        "result",
        "notebooklm.types.Source",
    ) in triples
    assert (
        "notebooklm._app.source_add.SourceAddResult",
        "source",
        "notebooklm.types.Source",
    ) in triples
    assert (
        "notebooklm._app.labels.LabelGenerateResult",
        "labels[]",
        "notebooklm.types.Label",
    ) in triples
    assert {
        (
            "notebooklm._auth.web_provider_refresh.WebProviderRefresh",
            "auth",
            "notebooklm.auth.AuthTokens",
        ),
        (
            "notebooklm._auth.web_provider_storage.WebProviderBootstrap",
            "auth",
            "notebooklm.auth.AuthTokens",
        ),
    } <= triples
    assert (
        "notebooklm._chat_records.ChatAskResultRecord",
        "answer_document",
        "notebooklm._types.documents.StructuredDocument",
    ) in triples
    assert (
        "notebooklm._web.codec.chat_stream.StreamingChatParseResult",
        "references[]",
        "notebooklm.types.ChatReference",
    ) in triples
    assert (
        "notebooklm._source.batch.SourceUrlBatchItem",
        "source",
        "notebooklm.types.Source",
    ) in triples
    assert (
        "notebooklm._auth.tokens.FileLoadedAuth",
        "auth",
        "notebooklm.auth.AuthTokens",
    ) in triples
    assert not any(row.private_model == "notebooklm._types.chat.AskResult" for row in rows)


def test_package_wide_private_path_mutation_requires_an_exact_disposition(
    tmp_path: Path,
) -> None:
    copied_source_root = tmp_path / "src"
    shutil.copytree(_source_root(), copied_source_root)
    batch_path = copied_source_root / "notebooklm" / "_source" / "batch.py"
    source = batch_path.read_text(encoding="utf-8")
    old = "    source: Source | None = None\n    error: SourceAddError | None = None"
    new = (
        "    source: Source | None = None\n"
        "    secondary_source: Source | None = None\n"
        "    error: SourceAddError | None = None"
    )
    assert old in source
    batch_path.write_text(source.replace(old, new, 1), encoding="utf-8")

    with pytest.raises(ValueError, match="private-path allocations are not exact"):
        reachability.derive_adapter_sink_reachability_contract(
            copied_source_root,
            known_projection_ids=_allocation_projection_ids(),
        )


def test_private_dto_catalog_follows_nested_container_paths_and_mutations(
    tmp_path: Path,
) -> None:
    aliases = {"notebooklm.types.Public": "notebooklm.types.Public"}
    _write_adapter_source(
        tmp_path,
        "_app/results.py",
        """
from dataclasses import InitVar, dataclass
from typing import ClassVar
from notebooklm.types import Public

@dataclass
class Inner:
    public: list[Public]
    class_only: ClassVar[Public]
    init_only: InitVar[Public]

@dataclass
class Outer:
    inner: Inner | None
""",
    )
    before = discover_private_dataclass_projection_paths(
        tmp_path,
        relative_roots=("notebooklm/_app",),
        public_model_aliases=aliases,
    )

    assert {(row.private_model, row.field_path, row.public_model) for row in before} == {
        ("notebooklm._app.results.Inner", "public[]", "notebooklm.types.Public"),
        (
            "notebooklm._app.results.Outer",
            "inner.public[]",
            "notebooklm.types.Public",
        ),
    }

    _write_adapter_source(
        tmp_path,
        "_app/results.py",
        """
from dataclasses import dataclass
from notebooklm.types import Public

@dataclass
class Inner:
    public: list[Public]
    secondary: Public | None

@dataclass
class Outer:
    inner: Inner | None
""",
    )
    after = discover_private_dataclass_projection_paths(
        tmp_path,
        relative_roots=("notebooklm/_app",),
        public_model_aliases=aliases,
    )

    assert len(after) == len(before) + 2
    assert {row.field_path for row in after} - {row.field_path for row in before} == {
        "secondary",
        "inner.secondary",
    }


def test_private_dto_catalog_rejects_unresolved_annotations(tmp_path: Path) -> None:
    _write_adapter_source(
        tmp_path,
        "_app/results.py",
        """
from dataclasses import dataclass

@dataclass
class Result:
    missing: MissingType
""",
    )

    with pytest.raises(ValueError, match="unresolved annotation name 'MissingType'"):
        discover_private_dataclass_projection_paths(
            tmp_path,
            relative_roots=("notebooklm/_app",),
            public_model_aliases={"notebooklm.types.Public": "notebooklm.types.Public"},
        )


def test_private_dto_catalog_resolves_decorator_and_type_alias_chains(tmp_path: Path) -> None:
    _write_adapter_source(
        tmp_path,
        "_app/results.py",
        """
from dataclasses import dataclass as record
from typing import TypeAlias
from notebooklm.types import Public

dto = record
PublicAlias = Public
PublicRows: TypeAlias = list[PublicAlias]

@dto
class Result:
    direct: PublicAlias
    rows: PublicRows
""",
    )

    rows = discover_private_dataclass_projection_paths(
        tmp_path,
        relative_roots=("notebooklm/_app",),
        public_model_aliases={"notebooklm.types.Public": "notebooklm.types.Public"},
    )
    assert [row.to_dict() for row in rows] == [
        {
            "private_model": "notebooklm._app.results.Result",
            "field_path": "direct",
            "public_model": "notebooklm.types.Public",
        },
        {
            "private_model": "notebooklm._app.results.Result",
            "field_path": "rows[]",
            "public_model": "notebooklm.types.Public",
        },
    ]


def test_private_dto_catalog_rejects_nested_dataclass_aliases(tmp_path: Path) -> None:
    _write_adapter_source(
        tmp_path,
        "_app/results.py",
        """
from notebooklm.types import Public

def build():
    from dataclasses import dataclass as record
    @record
    class Result:
        value: Public
    return Result
""",
    )

    with pytest.raises(ValueError, match="nested dataclasses are unsupported"):
        discover_private_dataclass_projection_paths(
            tmp_path,
            relative_roots=("notebooklm/_app",),
            public_model_aliases={"notebooklm.types.Public": "notebooklm.types.Public"},
        )


def test_private_dto_catalog_rejects_unresolved_type_aliases(tmp_path: Path) -> None:
    _write_adapter_source(
        tmp_path,
        "_app/results.py",
        """
from dataclasses import dataclass
from typing import TypeAlias

MissingAlias: TypeAlias = MissingType

@dataclass
class Result:
    missing: MissingAlias
""",
    )

    with pytest.raises(ValueError, match="unresolved annotation name 'MissingType'"):
        discover_private_dataclass_projection_paths(
            tmp_path,
            relative_roots=("notebooklm/_app",),
            public_model_aliases={"notebooklm.types.Public": "notebooklm.types.Public"},
        )


def test_private_dto_catalog_does_not_import_optional_adapter_modules(tmp_path: Path) -> None:
    _write_adapter_source(
        tmp_path,
        "mcp/optional_results.py",
        """
import dependency_that_is_intentionally_not_installed
from dataclasses import dataclass
from notebooklm.types import Public

@dataclass
class Result:
    value: Public
""",
    )

    rows = discover_private_dataclass_projection_paths(
        tmp_path,
        relative_roots=("notebooklm/mcp",),
        public_model_aliases={"notebooklm.types.Public": "notebooklm.types.Public"},
    )

    assert [row.to_dict() for row in rows] == [
        {
            "private_model": "notebooklm.mcp.optional_results.Result",
            "field_path": "value",
            "public_model": "notebooklm.types.Public",
        }
    ]


def test_dead_private_path_evidence_rejects_new_source_valued_return(
    tmp_path: Path,
) -> None:
    copied_source_root = tmp_path / "src"
    shutil.copytree(_source_root(), copied_source_root)
    mutations_path = copied_source_root / "notebooklm" / "_app" / "source_mutations.py"
    source = mutations_path.read_text(encoding="utf-8")
    old = (
        "return SourceRefreshResult(source_id=resolved_id, notebook_id=plan.notebook_id, "
        "result=None)"
    )
    new = (
        "return SourceRefreshResult(source_id=resolved_id, notebook_id=plan.notebook_id, "
        "result=Source(id=resolved_id))"
    )
    assert old in source
    mutations_path.write_text(source.replace(old, new, 1), encoding="utf-8")

    with pytest.raises(
        ValueError,
        match="missing required adapter evidence AST fragments|source evidence changed",
    ):
        reachability.derive_adapter_sink_reachability_contract(
            copied_source_root,
            known_projection_ids=_allocation_projection_ids(),
        )


def test_sink_identity_changes_with_exact_wrapper_shape(tmp_path: Path) -> None:
    _write_adapter_source(
        tmp_path,
        "cli/example.py",
        """
def command():
    json_output_response({"source": value})
""",
    )
    before = discover_adapter_json_sinks(tmp_path)

    _write_adapter_source(
        tmp_path,
        "cli/example.py",
        """
def command():
    json_output_response({"result": {"source": value}})
""",
    )
    after = discover_adapter_json_sinks(tmp_path)

    assert len(before) == len(after) == 1
    assert before[0].id != after[0].id
    assert before[0].expression_fingerprint != after[0].expression_fingerprint
    assert before[0].owner_fingerprint != after[0].owner_fingerprint


def test_delegated_helper_fingerprint_changes_with_projection_body(tmp_path: Path) -> None:
    _write_adapter_source(
        tmp_path,
        "cli/project.py",
        """
def source_payload(source):
    return {"id": source.id}
""",
    )
    symbol = "notebooklm.cli.project.source_payload"
    before = fingerprint_adapter_helpers(tmp_path, [symbol])

    _write_adapter_source(
        tmp_path,
        "cli/project.py",
        """
def source_payload(source):
    return {"id": source.id, "title": source.title}
""",
    )
    after = fingerprint_adapter_helpers(tmp_path, [symbol])

    assert before[symbol] != after[symbol]
    with pytest.raises(ValueError, match="unresolved delegated adapter projection helpers"):
        fingerprint_adapter_helpers(tmp_path, ["notebooklm.cli.project.missing"])


def test_nonpublic_delegated_helper_mutation_changes_reachability_contract(
    tmp_path: Path,
) -> None:
    known_projection_ids = _allocation_projection_ids()
    before = reachability.derive_adapter_sink_reachability_contract(
        _source_root(), known_projection_ids=known_projection_ids
    )
    copied_source_root = tmp_path / "src"
    shutil.copytree(_source_root(), copied_source_root)
    serializer_path = copied_source_root / "notebooklm" / "_app" / "serialize.py"
    source = serializer_path.read_text(encoding="utf-8")
    old = "return to_jsonable(obj.value)"
    new = "return str(obj.value)"
    assert old in source
    serializer_path.write_text(source.replace(old, new, 1), encoding="utf-8")

    after = reachability.derive_adapter_sink_reachability_contract(
        copied_source_root, known_projection_ids=known_projection_ids
    )
    symbol = "notebooklm._app.serialize.to_jsonable"
    assert symbol in before["delegated_helper_fingerprints"]
    assert (
        before["delegated_helper_fingerprints"][symbol]
        != after["delegated_helper_fingerprints"][symbol]
    )


def test_transitive_public_error_helper_mutation_changes_reachability_contract(
    tmp_path: Path,
) -> None:
    known_projection_ids = _allocation_projection_ids()
    before = reachability.derive_adapter_sink_reachability_contract(
        _source_root(), known_projection_ids=known_projection_ids
    )
    copied_source_root = tmp_path / "src"
    shutil.copytree(_source_root(), copied_source_root)
    helper_path = copied_source_root / "notebooklm" / "_app" / "source_add.py"
    source = helper_path.read_text(encoding="utf-8")
    old = """    src = await add_source(
        client.sources,
        notebook_id=plan.notebook_id,
        plan=plan.plan,
    )
    return SourceAddResult(source=src)
"""
    new = """    src = await add_source(
        client.sources,
        notebook_id=plan.notebook_id,
        plan=plan.plan,
    )
    if src.status:
        raise SourceAddValidationError(
            f"source {src.id} failed with status {src.status}"
        )
    return SourceAddResult(source=src)
"""
    assert old in source
    helper_path.write_text(source.replace(old, new, 1), encoding="utf-8")

    after = reachability.derive_adapter_sink_reachability_contract(
        copied_source_root, known_projection_ids=known_projection_ids
    )
    assert before["site_count"] == after["site_count"] == 350
    assert (
        before["private_dataclass_projection_paths"] == after["private_dataclass_projection_paths"]
    )
    assert before["transitive_helper_graph"]["node_count"] == 534
    assert (
        before["transitive_helper_graph"]["aggregate_fingerprint"]
        != after["transitive_helper_graph"]["aggregate_fingerprint"]
    )


def test_cli_clear_cache_helper_is_fingerprinted(tmp_path: Path) -> None:
    known_projection_ids = _allocation_projection_ids()
    before = reachability.derive_adapter_sink_reachability_contract(
        _source_root(), known_projection_ids=known_projection_ids
    )
    copied_source_root = tmp_path / "src"
    shutil.copytree(_source_root(), copied_source_root)
    chat_path = copied_source_root / "notebooklm" / "cli" / "chat_cmd.py"
    source = chat_path.read_text(encoding="utf-8")
    old = 'return {"cleared": result.cleared, "count": result.count}'
    new = 'return {"cleared": result.cleared, "count": result.count, "extra": True}'
    assert old in source
    chat_path.write_text(source.replace(old, new, 1), encoding="utf-8")

    after = reachability.derive_adapter_sink_reachability_contract(
        copied_source_root, known_projection_ids=known_projection_ids
    )
    symbol = "notebooklm.cli.chat_cmd._clear_cache_json_payload"
    assert symbol in before["delegated_helper_fingerprints"]
    assert (
        before["delegated_helper_fingerprints"][symbol]
        != after["delegated_helper_fingerprints"][symbol]
    )


def test_duplicate_sink_expression_keeps_multiplicity(tmp_path: Path) -> None:
    _write_adapter_source(
        tmp_path,
        "mcp/tools/example.py",
        """
def register(mcp):
    @mcp.tool
    def example(flag):
        if flag:
            return payload
        return payload
""",
    )

    sinks = discover_adapter_json_sinks(tmp_path)

    assert len(sinks) == 2
    assert sinks[0].expression_fingerprint == sinks[1].expression_fingerprint
    assert sinks[0].id.endswith(":1")
    assert sinks[1].id.endswith(":2")


def test_router_return_is_discovered_but_ordinary_helper_return_is_not(tmp_path: Path) -> None:
    _write_adapter_source(
        tmp_path,
        "server/routes/example.py",
        """
def helper():
    return {"ignored": True}

@router.get("/example")
def example():
    return helper()
""",
    )

    sinks = discover_adapter_json_sinks(tmp_path)

    assert len(sinks) == 1
    assert sinks[0].owner == "example"


def test_only_mcp_and_router_decorator_owners_define_result_sinks(tmp_path: Path) -> None:
    _write_adapter_source(
        tmp_path,
        "mcp/tools/example.py",
        """
@registry.tool
def false_tool():
    return {"ignored": True}

@mcp.tool
def real_tool():
    return {"included": True}
""",
    )
    _write_adapter_source(
        tmp_path,
        "server/routes/example.py",
        """
@cache.get("key")
def false_route():
    return {"ignored": True}

@router.get("/example")
def real_route():
    return {"included": True}
""",
    )

    sinks = discover_adapter_json_sinks(tmp_path)

    assert [(sink.channel, sink.owner) for sink in sinks] == [
        ("mcp tool result", "real_tool"),
        ("rest response", "real_route"),
    ]


def test_framework_instance_aliases_and_all_http_verbs_are_discovered(tmp_path: Path) -> None:
    _write_adapter_source(
        tmp_path,
        "mcp/tools/example.py",
        """
from fastmcp import FastMCP as Server

def register(mcp: Server):
    registry = mcp
    @registry.tool
    def aliased_tool():
        return {"included": True}
""",
    )
    _write_adapter_source(
        tmp_path,
        "server/routes/example.py",
        """
from fastapi import APIRouter as Router

RouterFactory = Router
api = RouterFactory()

@api.options("/example")
def options_route():
    return {"method": "options"}

@api.head("/example")
def head_route():
    return {"method": "head"}

@api.trace("/example")
def trace_route():
    return {"method": "trace"}
""",
    )

    assert_supported_adapter_registrations(tmp_path)
    sinks = discover_adapter_json_sinks(tmp_path)
    assert [(sink.channel, sink.owner) for sink in sinks] == [
        ("mcp tool result", "register.aliased_tool"),
        ("rest response", "head_route"),
        ("rest response", "options_route"),
        ("rest response", "trace_route"),
    ]


@pytest.mark.parametrize(
    ("relative_path", "source", "registration"),
    [
        (
            "server/routes/websocket.py",
            """
from fastapi import APIRouter
api = APIRouter()
@api.websocket("/hidden")
async def hidden(socket):
    await socket.send_json({"secret": value})
""",
            "api.websocket",
        ),
        (
            "server/app.py",
            """
from fastapi import FastAPI
service = FastAPI()
service.mount("/hidden", hidden_app)
""",
            "service.mount",
        ),
        (
            "mcp/prompts.py",
            """
from fastmcp import FastMCP
registry: FastMCP
@registry.prompt
def hidden_prompt():
    return secret
""",
            "registry.prompt",
        ),
        (
            "mcp/mounted.py",
            """
from fastmcp import FastMCP
registry = FastMCP()
registry.mount(child_server)
""",
            "registry.mount",
        ),
        (
            "server/middleware.py",
            """
from fastapi import FastAPI
service = FastAPI()
service.add_middleware(JsonMiddleware)
""",
            "service.add_middleware",
        ),
        (
            "mcp/middleware.py",
            """
from fastmcp import FastMCP
registry = FastMCP()
registry.add_middleware(JsonResultMiddleware())
""",
            "registry.add_middleware",
        ),
    ],
)
def test_unsupported_framework_registration_forms_are_rejected(
    tmp_path: Path, relative_path: str, source: str, registration: str
) -> None:
    _write_adapter_source(tmp_path, relative_path, source)

    with pytest.raises(ValueError, match=registration):
        assert_supported_adapter_registrations(tmp_path)


@pytest.mark.parametrize(
    ("relative_path", "source", "owner"),
    [
        (
            "mcp/tools/error_only.py",
            """
from notebooklm.types import Source
@mcp.tool
def error_only(source: Source):
    raise ToolError(json.dumps({"source_id": source.id}))
""",
            "error_only",
        ),
        (
            "server/routes/error_only.py",
            """
from notebooklm.types import Source
@router.get("/error-only")
def error_only(source: Source):
    raise HTTPException(409, detail={"source_id": source.id})
""",
            "error_only",
        ),
    ],
)
def test_registered_error_only_handler_cannot_have_zero_inventoried_terminals(
    tmp_path: Path, relative_path: str, source: str, owner: str
) -> None:
    _write_adapter_source(tmp_path, relative_path, source)

    with pytest.raises(ValueError, match=rf"no inventoried terminal: .*\|{owner}"):
        discover_adapter_json_sinks(tmp_path)


def test_registered_mcp_error_only_handler_with_reviewed_funnel_is_inventoried(
    tmp_path: Path,
) -> None:
    _write_adapter_source(
        tmp_path,
        "mcp/tools/error_funnel.py",
        """
from notebooklm.types import Source

@mcp.tool
def error_only(source: Source):
    with mcp_errors():
        raise LookupError(source.id)
""",
    )

    sinks = discover_adapter_json_sinks(tmp_path)
    assert [(sink.owner, sink.kind, sink.site_role) for sink in sinks] == [
        ("error_only", "tool-error-funnel", "error-projection")
    ]


def test_direct_cli_json_bypass_is_discovered_and_rejected(tmp_path: Path) -> None:
    _write_adapter_source(
        tmp_path,
        "cli/example.py",
        """
import json
import click

def command(payload):
    click.echo(json.dumps(payload))
""",
    )

    sinks = discover_adapter_json_sinks(tmp_path)

    assert len(sinks) == 1
    assert sinks[0].kind == "direct-json-emission"
    with pytest.raises(ValueError, match="unreviewed direct CLI JSON emissions"):
        assert_no_unreviewed_direct_json_emissions(sinks)


@pytest.mark.parametrize(
    "source",
    [
        """
import json
import click

def command(payload):
    encoded = json.dumps(payload)
    click.echo(encoded)
""",
        """
from json import dump as emit_json
import sys

def command(payload):
    emit_json(payload, sys.stdout)
""",
        """
import json as codec
import click

def command(payload):
    serialize: object = lambda value: codec.dumps(value)
    encoded = serialize(payload)
    click.echo(encoded)
""",
        """
import json
import click

def command(payload):
    encoded = json.JSONEncoder().encode(payload)
    click.echo(encoded)
""",
        """
import json
import click

def command(payload):
    serialize = getattr(json, "dumps")
    click.echo(serialize(payload))
""",
        """
from json import JSONEncoder as Encoder
import click

def command(payload):
    encoder = Encoder()
    serialize = getattr(encoder, "iterencode")
    click.echo("".join(serialize(payload)))
""",
    ],
)
def test_cli_json_serialization_cannot_be_separated_from_stdout_sink(
    tmp_path: Path, source: str
) -> None:
    _write_adapter_source(tmp_path, "cli/example.py", source)

    with pytest.raises(ValueError, match="notebooklm/cli/example.py"):
        assert_no_unreviewed_cli_json_serialization(tmp_path)


@pytest.mark.parametrize(
    ("relative_path", "source", "channel"),
    [
        (
            "server/routes/dotted.py",
            """
from fastapi import FastAPI
holder.api = FastAPI()
@holder.api.get("/hidden")
def hidden():
    return {"visible": True}
""",
            "rest response",
        ),
        (
            "server/routes/subscript.py",
            """
from fastapi import APIRouter
holders["router"] = APIRouter()
@holders["router"].post("/hidden")
def hidden():
    return {"visible": True}
""",
            "rest response",
        ),
        (
            "mcp/tools/dotted.py",
            """
from fastmcp import FastMCP
holder.server = FastMCP()
@holder.server.tool
def hidden():
    return {"visible": True}
""",
            "mcp tool result",
        ),
    ],
)
def test_dotted_and_subscript_framework_owner_aliases_are_discovered(
    tmp_path: Path, relative_path: str, source: str, channel: str
) -> None:
    _write_adapter_source(tmp_path, relative_path, source)

    assert_supported_adapter_registrations(tmp_path)
    sinks = discover_adapter_json_sinks(tmp_path)
    assert [(sink.channel, sink.owner) for sink in sinks] == [(channel, "hidden")]


@pytest.mark.parametrize(
    ("relative_path", "source"),
    [
        (
            "mcp/dynamic.py",
            """
def register(mcp):
    def hidden_tool():
        return {"hidden": True}
    mcp.add_tool(hidden_tool)
""",
        ),
        (
            "server/routes/dynamic.py",
            """
def hidden_route():
    return {"hidden": True}
router.add_api_route("/hidden", hidden_route)
""",
        ),
    ],
)
def test_dynamic_adapter_registration_is_rejected(
    tmp_path: Path, relative_path: str, source: str
) -> None:
    _write_adapter_source(tmp_path, relative_path, source)

    with pytest.raises(ValueError, match="unsupported dynamic adapter registrations"):
        assert_supported_adapter_registrations(tmp_path)


def test_decorated_mcp_tool_registration_is_supported(tmp_path: Path) -> None:
    _write_adapter_source(
        tmp_path,
        "mcp/tools/example.py",
        """
def register(mcp):
    @mcp.tool(annotations={"readOnlyHint": True})
    def visible_tool():
        return {"visible": True}
""",
    )

    assert_supported_adapter_registrations(tmp_path)
    assert len(discover_adapter_json_sinks(tmp_path)) == 1


def test_mcp_tool_error_boundary_is_a_separate_terminal(tmp_path: Path) -> None:
    _write_adapter_source(
        tmp_path,
        "mcp/tools/example.py",
        """
def register(mcp):
    @mcp.tool
    def visible_tool():
        with mcp_errors():
            return {"visible": True}
""",
    )

    sinks = discover_adapter_json_sinks(tmp_path)
    assert [(sink.kind, sink.site_role) for sink in sinks] == [
        ("return", "projection"),
        ("tool-error-funnel", "error-projection"),
    ]


def test_qualified_owner_includes_class_scope(tmp_path: Path) -> None:
    _write_adapter_source(
        tmp_path,
        "cli/example.py",
        """
class First:
    def render(self):
        json_output_response({"first": True})

class Second:
    def render(self):
        json_output_response({"second": True})
""",
    )

    sinks = discover_adapter_json_sinks(tmp_path)

    assert [sink.owner for sink in sinks] == ["First.render", "Second.render"]


def test_error_projection_tracks_extra_shape_and_marks_facade_forwarding(tmp_path: Path) -> None:
    _write_adapter_source(
        tmp_path,
        "cli/example.py",
        """
def command(result):
    output_error("failed", "FAILED", True, 1, extra={"status": result})
""",
    )
    before = discover_adapter_json_sinks(tmp_path)
    assert before[0].site_role == "error-projection"

    _write_adapter_source(
        tmp_path,
        "cli/example.py",
        """
def command(result):
    output_error("failed", "FAILED", True, 1, extra={"transition": result})
""",
    )
    after = discover_adapter_json_sinks(tmp_path)
    assert before[0].id != after[0].id


def test_dispositions_fail_closed_for_missing_and_stale_sites(tmp_path: Path) -> None:
    _write_adapter_source(
        tmp_path,
        "cli/example.py",
        """
def command():
    json_output_response(payload)
""",
    )
    sinks = discover_adapter_json_sinks(tmp_path)

    with pytest.raises(ValueError, match="missing="):
        assert_exact_sink_dispositions(sinks, {})
    with pytest.raises(ValueError, match="stale="):
        assert_exact_sink_dispositions(sinks, {sinks[0].id: {}, "removed": {}})

    assert_exact_sink_dispositions(
        sinks,
        {sinks[0].id: {"projection_ids": ["cli-source-row"]}},
        known_projection_ids={"cli-source-row"},
    )


def test_dispositions_require_one_reviewed_reachability_case(tmp_path: Path) -> None:
    _write_adapter_source(
        tmp_path,
        "cli/example.py",
        """
def command():
    json_output_response(payload)
""",
    )
    sink = discover_adapter_json_sinks(tmp_path)[0]

    with pytest.raises(ValueError, match="expected one discriminator"):
        assert_exact_sink_dispositions(
            [sink],
            {
                sink.id: {
                    "projection_ids": ["row"],
                    "non_public_model_reason": "plain scalars",
                }
            },
        )
    with pytest.raises(ValueError, match="unknown compatibility projection ids"):
        assert_exact_sink_dispositions(
            [sink],
            {sink.id: {"projection_ids": ["missing"]}},
            known_projection_ids={"row"},
        )


@pytest.mark.parametrize(
    "import_line,dump_call",
    [
        ("import json as j", "j.dumps(payload)"),
        ("from json import dumps as encode_json", "encode_json(payload)"),
    ],
)
def test_direct_cli_json_bypass_resolves_stdlib_import_aliases(
    tmp_path: Path, import_line: str, dump_call: str
) -> None:
    _write_adapter_source(
        tmp_path,
        "cli/example.py",
        f"""
{import_line}
import click

def command(payload):
    click.echo({dump_call})
""",
    )

    sinks = discover_adapter_json_sinks(tmp_path)
    assert [sink.kind for sink in sinks] == ["direct-json-emission"]
    with pytest.raises(ValueError, match="unreviewed direct CLI JSON emissions"):
        assert_no_unreviewed_direct_json_emissions(sinks)


def test_mcp_custom_route_inventories_json_and_non_json_returns(tmp_path: Path) -> None:
    _write_adapter_source(
        tmp_path,
        "mcp/routes.py",
        """
@mcp.custom_route("/example", methods=["GET"])
def example(flag):
    if flag:
        return JSONResponse(status_code=200, content={"source_id": source.id})
    return PlainTextResponse("not json")
""",
    )

    sinks = discover_adapter_json_sinks(tmp_path)
    assert len(sinks) == 2
    assert {sink.channel for sink in sinks} == {"mcp auxiliary response"}
    assert {sink.kind for sink in sinks} == {"return"}
    assert_supported_adapter_registrations(tmp_path)


def test_rest_app_routes_and_exception_handlers_are_discovered(tmp_path: Path) -> None:
    _write_adapter_source(
        tmp_path,
        "server/app.py",
        """
@app.get("/healthz")
def health():
    return {"ok": True}

@app.exception_handler(Exception)
def errors(request, exc):
    return error_response(exc)
""",
    )

    sinks = discover_adapter_json_sinks(tmp_path)
    assert [(sink.channel, sink.owner) for sink in sinks] == [
        ("rest response", "errors"),
        ("rest response", "health"),
    ]


def test_rest_central_json_response_extracts_keyword_content(tmp_path: Path) -> None:
    _write_adapter_source(
        tmp_path,
        "server/_errors.py",
        """
def error_response(code):
    return JSONResponse(status_code=400, content={"error": {"code": code}})
""",
    )
    before = discover_adapter_json_sinks(tmp_path)

    _write_adapter_source(
        tmp_path,
        "server/_errors.py",
        """
def error_response(code):
    return JSONResponse(status_code=400, content={"problem": {"code": code}})
""",
    )
    after = discover_adapter_json_sinks(tmp_path)

    assert len(before) == len(after) == 1
    assert before[0].kind == "json-response-return"
    assert before[0].site_role == "forwarding-infrastructure"
    assert before[0].id != after[0].id


@pytest.mark.parametrize(
    ("relative_path", "source"),
    [
        (
            "mcp/dynamic_custom.py",
            """
def handler():
    return {"hidden": True}
mcp.custom_route("/hidden", methods=["GET"])(handler)
""",
        ),
        (
            "server/routes/dynamic_verb.py",
            """
def handler():
    return {"hidden": True}
router.get("/hidden")(handler)
""",
        ),
        (
            "server/dynamic_exception.py",
            """
def handler(request, exc):
    return {"hidden": True}
app.add_exception_handler(Exception, handler)
""",
        ),
        (
            "server/app.py",
            """
from fastapi import FastAPI
service = FastAPI()
service.include_router(hidden_router)
""",
        ),
        (
            "server/starlette_alias.py",
            """
from starlette.routing import Route as HiddenRoute
routes = [HiddenRoute("/hidden", hidden_handler)]
""",
        ),
        (
            "mcp/_oauth.py",
            """
from starlette.routing import Route as HiddenRoute
def routes(self):
    return [HiddenRoute("/login", self._login, methods=["GET", "POST"])]
""",
        ),
        (
            "mcp/bound_alias.py",
            """
from fastmcp import FastMCP
server = FastMCP()
register_tool = server.tool
@register_tool
def hidden():
    return {"hidden": True}
""",
        ),
    ],
)
def test_non_decorator_registration_forms_are_rejected(
    tmp_path: Path, relative_path: str, source: str
) -> None:
    _write_adapter_source(tmp_path, relative_path, source)
    with pytest.raises(ValueError, match="unsupported dynamic adapter registrations"):
        assert_supported_adapter_registrations(tmp_path)


def test_reviewed_oauth_route_exception_rejects_same_path_different_handler(
    tmp_path: Path,
) -> None:
    _write_adapter_source(
        tmp_path,
        "mcp/_oauth.py",
        """
def routes(self):
    return [Route("/login", self._json_login, methods=["GET", "POST"])]
""",
    )
    with pytest.raises(ValueError, match="unsupported dynamic adapter registrations"):
        assert_supported_adapter_registrations(tmp_path)


def _allocation_projection_ids() -> set[str]:
    rows = reachability._load_reviewed_allocations()
    return {
        projection_id
        for allocation in rows.values()
        for projection_id in allocation.get("projection_ids", [])
    }


def test_checked_in_reachability_allocations_are_exact() -> None:
    contract = reachability.derive_adapter_sink_reachability_contract(
        _source_root(), known_projection_ids=_allocation_projection_ids()
    )
    assert contract["site_count"] == 350
    private_paths = contract["private_dataclass_projection_paths"]
    assert len(private_paths) == 37  # see the R6.4 note above
    provider_auth_paths = [
        path
        for path in private_paths
        if str(path["private_model"]).startswith("notebooklm._auth.web_provider_")
    ]
    assert len(provider_auth_paths) == 2
    assert {path["allocation"]["unreachable_category"] for path in provider_auth_paths} == {
        "internal-runtime-auth-capability"
    }
    assert all("projection_ids" not in path["allocation"] for path in provider_auth_paths)
    assert all("terminal_locators" not in path["allocation"] for path in provider_auth_paths)
    assert contract["transitive_helper_graph"] == {
        "schema_version": 1,
        "root_count": 210,
        "node_count": 534,
        "edge_count": 1259,
        "aggregate_fingerprint": contract["transitive_helper_graph"]["aggregate_fingerprint"],
    }
    assert str(contract["transitive_helper_graph"]["aggregate_fingerprint"]).startswith("sha256:")


def test_mcp_mind_map_union_projections_are_on_value_carrying_branches() -> None:
    allocations = reachability._load_reviewed_allocations()
    rename_id = "mcp.MindMap.transitive-resolver-rename-final-wrapper"
    delete_id = "mcp.MindMap.transitive-resolver-delete-final-wrapper"

    def projection_ids(owner: str, ordinal: int) -> set[str]:
        locator = (
            f"mcp tool result|notebooklm/mcp/tools/studio.py|register.{owner}|return|{ordinal}"
        )
        return set(allocations[locator].get("projection_ids", []))

    assert rename_id in projection_ids("studio_rename", 1)
    assert rename_id in projection_ids("studio_rename", 3)
    artifact_rename_id = "mcp.Artifact.transitive-studio-rename-final-projection"
    assert artifact_rename_id not in projection_ids("studio_rename", 1)
    assert artifact_rename_id in projection_ids("studio_rename", 3)
    assert delete_id in projection_ids("studio_delete", 2)
    assert delete_id not in projection_ids("studio_delete", 3)
    assert delete_id in projection_ids("studio_delete", 5)


def test_status_derived_contributions_are_on_exact_terminal_or_error_funnel() -> None:
    allocations = reachability._load_reviewed_allocations()
    expected = {
        (
            "cli --json|notebooklm/cli/artifact_cmd.py|artifact_retry._run|json_output_response|3",
            "cli.GenerationStatus.transitive-retry-timeout-task-id-contribution",
        ),
        (
            "rest response|notebooklm/server/routes/sources.py|get_source_content|return|2",
            "rest.Source.transitive-source-content-readiness-contribution",
        ),
        (
            "mcp tool result|notebooklm/mcp/tools/studio.py|"
            "register.studio_download|tool-error-funnel|1",
            "mcp.Artifact.transitive-download-incomplete-status-error-text-contribution",
        ),
        (
            "mcp tool result|notebooklm/mcp/tools/studio.py|"
            "register.studio_retry|tool-error-funnel|1",
            "mcp.Artifact.transitive-retry-wrong-state-status-error-text-contribution",
        ),
    }
    for locator, projection_id in expected:
        assert projection_id in allocations[locator]["projection_ids"]


def test_mcp_source_add_projections_follow_exact_return_branches() -> None:
    allocations = reachability._load_reviewed_allocations()
    expected_source_ids = {
        1: {"mcp.Source.transitive-batch-added-item-final-wrapper"},
        2: {"mcp.Source.app-view-source-wait-final-wrapper"},
        3: {"mcp.Source.app-view-source-add-final-wrapper"},
        4: set(),
        5: {"mcp.Source.app-view-source-wait-final-wrapper"},
        6: {"mcp.Source.app-view-source-add-drive-final-wrapper"},
        7: {"mcp.Source.app-view-source-wait-final-wrapper"},
        8: {"mcp.Source.app-view-source-add-final-wrapper"},
    }
    for ordinal, expected in expected_source_ids.items():
        locator = (
            f"mcp tool result|notebooklm/mcp/tools/sources.py|register.source_add|return|{ordinal}"
        )
        actual = {
            projection_id
            for projection_id in allocations[locator]["projection_ids"]
            if projection_id.startswith("mcp.Source.")
        }
        assert actual == expected

    rest_batch = allocations[
        "rest response|notebooklm/server/routes/sources.py|add_batch|return|1"
    ]["projection_ids"]
    assert rest_batch == ["rest.Source.transitive-batch-added-item-final-wrapper"]
    expected_variants = {
        "mcp tool result|notebooklm/mcp/tools/sources.py|register.source_add|return|1": (
            "the notebook uses the canonical-id fast path and all batch inputs fail before any "
            "Source instance is returned"
        ),
        "rest response|notebooklm/server/routes/sources.py|add_batch|return|1": (
            "all batch inputs fail before any Source instance is returned"
        ),
    }
    for locator, condition in expected_variants.items():
        variants = allocations[locator]["non_public_variants"]
        assert [variant["condition"] for variant in variants] == [condition]


def test_source_wait_no_source_variants_are_explicit() -> None:
    allocations = reachability._load_reviewed_allocations()
    expected_conditions = {
        "mcp tool result|notebooklm/mcp/tools/sources.py|register.source_wait|return|1": {
            "the notebook and every explicit subset ref use canonical-id fast paths and every outcome is non-ready"
        },
        "mcp tool result|notebooklm/mcp/tools/sources.py|register.source_wait|return|2": {
            "the notebook and single ref use canonical-id fast paths and the outcome is non-ready"
        },
        "mcp tool result|notebooklm/mcp/tools/sources.py|register.source_wait|return|3": {
            "the notebook uses the canonical-id fast path and wait-all lists zero Source instances"
        },
        "rest response|notebooklm/server/routes/sources.py|wait_sources|return|1": {
            "explicit canonical source_ids all produce non-ready outcomes",
            "wait-all lists zero Source instances",
        },
    }
    for locator, conditions in expected_conditions.items():
        variants = allocations[locator]["non_public_variants"]
        assert {variant["condition"] for variant in variants} == conditions

    wait_all_ids = allocations[
        "mcp tool result|notebooklm/mcp/tools/sources.py|register.source_wait|return|3"
    ]["projection_ids"]
    assert "mcp.Source.conditional-noncanonical-resolver-id-contribution" not in wait_all_ids


def test_download_no_model_variants_are_explicit() -> None:
    allocations = reachability._load_reviewed_allocations()
    expected_conditions = {
        "cli --json|notebooklm/cli/download_cmd.py|_run_artifact_download|json_output_response|1": (
            "artifact listing is empty and the download result is NO_ARTIFACTS"
        ),
        "mcp tool result|notebooklm/mcp/tools/studio.py|register.studio_download|return|1": (
            "the notebook uses the canonical-id fast path, artifact_type latest mode omits "
            "artifact and artifact_id, and the kind is non-inline"
        ),
        "mcp tool result|notebooklm/mcp/tools/studio.py|register.studio_download|return|2": (
            "the notebook uses the canonical-id fast path and the artifact listing is empty"
        ),
    }
    for locator, condition in expected_conditions.items():
        variants = allocations[locator]["non_public_variants"]
        assert [variant["condition"] for variant in variants] == [condition]


def test_metadata_and_mind_map_contributions_use_exact_terminals() -> None:
    allocations = reachability._load_reviewed_allocations()
    expected = {
        "cli --json|notebooklm/cli/notebook_cmd.py|"
        "register_notebook_commands.metadata_cmd._run|json_output_response|1": {
            "cli.Source.transitive-notebook-metadata-source-summary-final-wrapper"
        },
        "mcp tool result|notebooklm/mcp/tools/notebooks.py|register.notebook_describe|return|1": {
            "mcp.Source.transitive-notebook-describe-metadata-source-summary-final-wrapper",
            "mcp.NotebookMetadata.transitive-notebook-describe-final-with-metadata-null-description",
            "mcp.NotebookDescription.nested-notebook-describe-final",
        },
        "cli --json|notebooklm/cli/generate_cmd.py|"
        "_output_mind_map_result|json_output_response|1": {
            "cli.Note.transitive-note-backed-mind-map-generation-final-contribution",
            "cli.Artifact.transitive-interactive-mind-map-generation-final-contribution",
        },
        "mcp tool result|notebooklm/mcp/tools/studio.py|register.studio_generate|return|1": {
            "mcp.Note.transitive-note-backed-mind-map-generation-final-contribution",
            "mcp.Artifact.transitive-interactive-mind-map-generation-final-contribution",
        },
        "rest response|notebooklm/server/routes/artifacts.py|generate|return|1": {
            "rest.Note.transitive-note-backed-mind-map-generation-final-contribution",
            "rest.Artifact.transitive-interactive-mind-map-generation-final-contribution",
        },
        "rest response|notebooklm/server/routes/artifacts.py|rename|return|1": {
            "rest.Artifact.transitive-mind-map-rename-membership-final-contribution"
        },
    }
    for locator, expected_ids in expected.items():
        assert expected_ids <= set(allocations[locator]["projection_ids"])

    default_describe = allocations[
        "mcp tool result|notebooklm/mcp/tools/notebooks.py|register.notebook_describe|return|2"
    ]["projection_ids"]
    assert (
        "mcp.Source.transitive-notebook-describe-metadata-source-summary-final-wrapper"
        not in default_describe
    )
    private_paths = reachability._load_private_path_allocations()
    metadata_path = private_paths[
        (
            "notebooklm._app.notebooks.NotebookMetadataResult",
            "metadata",
            "notebooklm.types.NotebookMetadata",
        )
    ]
    assert (
        "mcp tool result|notebooklm/mcp/tools/notebooks.py|"
        "register.notebook_describe|return|1" in metadata_path["terminal_locators"]
    )
    assert (
        "mcp tool result|notebooklm/mcp/tools/notebooks.py|"
        "register.notebook_describe|return|2" not in metadata_path["terminal_locators"]
    )
    assert (
        "mcp.NotebookMetadata.transitive-notebook-describe-final-with-metadata-null-description"
        in metadata_path["projection_ids"]
    )


def test_cli_research_wait_source_projection_covers_failed_and_completed_results() -> None:
    allocations = reachability._load_reviewed_allocations()
    expected = {
        3: "cli.ResearchSource.transitive-wait-failed-final-wrapper",
        4: "cli.ResearchSource.transitive-wait-completed-final-wrapper",
    }
    for ordinal, projection_id in expected.items():
        locator = (
            "cli --json|notebooklm/cli/research_cmd.py|"
            f"_render_wait_result|json_output_response|{ordinal}"
        )
        assert projection_id in allocations[locator]["projection_ids"]
        assert (
            "cli.ResearchSource.nested-to-public-dict-projection"
            not in allocations[locator]["projection_ids"]
        )

    import_ids = allocations[
        "cli --json|notebooklm/cli/research_cmd.py|_render_import_result|json_output_response|1"
    ]["projection_ids"]
    assert {
        "cli.ResearchTask.transitive-import-success-final-wrapper",
        "cli.ResearchSource.transitive-import-selection-final-contribution",
    } <= set(import_ids)


def test_chat_document_contributions_use_only_ask_terminals() -> None:
    allocations = reachability._load_reviewed_allocations()
    expected = {
        "cli --json|notebooklm/cli/chat_cmd.py|"
        "register_chat_commands.ask_cmd._run|json_output_response|1": {
            "cli.StructuredDocument.transitive-chat-reference-full-contribution",
            "cli.DocumentAnnotation.transitive-chat-reference-full-contribution",
            "cli.DocumentBlock.transitive-chat-reference-full-contribution",
            "cli.TextSpan.transitive-chat-reference-full-contribution",
        },
        "mcp tool result|notebooklm/mcp/tools/chat.py|register.chat_ask|return|1": {
            "mcp.StructuredDocument.transitive-chat-reference-full-contribution",
            "mcp.DocumentAnnotation.transitive-chat-reference-full-contribution",
            "mcp.DocumentBlock.transitive-chat-reference-full-contribution",
            "mcp.TextSpan.transitive-chat-reference-full-contribution",
            "mcp.DocumentBlock.transitive-chat-reference-lite-fragment-contribution",
            "mcp.TextSpan.transitive-chat-reference-lite-fragment-contribution",
        },
        "rest response|notebooklm/server/routes/chat.py|ask|return|1": {
            "rest.StructuredDocument.transitive-chat-reference-full-contribution",
            "rest.DocumentAnnotation.transitive-chat-reference-full-contribution",
            "rest.DocumentBlock.transitive-chat-reference-full-contribution",
            "rest.TextSpan.transitive-chat-reference-full-contribution",
        },
    }
    for locator, expected_ids in expected.items():
        assert expected_ids <= set(allocations[locator]["projection_ids"])


def test_account_rename_and_http_error_contributions_use_exact_terminals() -> None:
    allocations = reachability._load_reviewed_allocations()
    server_info_ids = allocations[
        "mcp tool result|notebooklm/mcp/tools/meta.py|register.server_info|return|1"
    ]["projection_ids"]
    assert "mcp.AuthTokens.redacted-server-info-account-identity-contribution" in server_info_ids
    rest_server_info_ids = allocations[
        "rest response|notebooklm/server/routes/meta.py|server_info|return|1"
    ]["projection_ids"]
    assert (
        "rest.AuthTokens.redacted-server-info-account-identity-contribution" in rest_server_info_ids
    )

    rename_id = "mcp.Artifact.transitive-mind-map-rename-membership-final-contribution"
    for ordinal in (1, 3):
        locator = (
            "mcp tool result|notebooklm/mcp/tools/studio.py|"
            f"register.studio_rename|return|{ordinal}"
        )
        assert rename_id in allocations[locator]["projection_ids"]
    note_branch = allocations[
        "mcp tool result|notebooklm/mcp/tools/studio.py|register.studio_rename|return|2"
    ]["projection_ids"]
    assert rename_id not in note_branch

    http_handler = allocations[
        "rest response|notebooklm/server/_errors.py|"
        "install_exception_handlers._handle_http|return|1"
    ]
    assert (
        "rest.Artifact.transitive-download-no-artifacts-409-error-contribution"
        in http_handler["projection_ids"]
    )
    assert http_handler["non_public_variants"]


def test_research_error_contributions_use_exact_shared_funnels() -> None:
    allocations = reachability._load_reviewed_allocations()
    expected = {
        "cli --json|notebooklm/cli/error_handler.py|handle_errors.emit|_output_error|1": {
            "cli.ResearchTask.transitive-import-refusal-error-contribution"
        },
        "mcp tool result|notebooklm/mcp/tools/research.py|"
        "register.research_import|tool-error-funnel|1": {
            "mcp.ResearchTask.transitive-import-refusal-error-contribution"
        },
        "mcp tool result|notebooklm/mcp/tools/research.py|"
        "register.research_start|tool-error-funnel|1": {
            "mcp.ResearchStart.transitive-start-missing-report-id-error-contribution"
        },
        "rest response|notebooklm/server/_errors.py|"
        "install_exception_handlers._handle_library|return|1": {
            "rest.ResearchTask.transitive-import-refusal-error-contribution",
            "rest.ResearchStart.transitive-start-missing-poll-id-error-contribution",
        },
    }
    for locator, expected_ids in expected.items():
        projection_ids = set(allocations[locator]["projection_ids"])
        assert expected_ids <= projection_ids
        assert allocations[locator]["non_public_variants"]


def test_share_status_get_omits_view_level_projection() -> None:
    allocations = reachability._load_reviewed_allocations()
    get_ids = allocations[
        "mcp tool result|notebooklm/mcp/tools/sharing.py|register.share_status|return|1"
    ]["projection_ids"]
    mutation_ids = allocations[
        "mcp tool result|notebooklm/mcp/tools/sharing.py|register.share_set_access|return|2"
    ]["projection_ids"]
    assert "mcp.ShareStatus.app-view-share-status-view-view-level" not in get_ids
    assert "mcp.ShareStatus.app-view-mutation-final-updated-view-level" in mutation_ids


def test_reachability_rejects_unallocated_known_projection_id() -> None:
    with pytest.raises(ValueError, match="no adapter terminal allocation"):
        reachability.derive_adapter_sink_reachability_contract(
            _source_root(),
            known_projection_ids={*_allocation_projection_ids(), "cli.Source.new-unallocated"},
        )


def test_private_path_dispositions_fail_when_new_path_is_unallocated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = discover_private_dataclass_projection_paths(_source_root())
    added = PrivateDataclassProjectionPath(
        private_model="notebooklm._app.future.Result",
        field_path="source",
        public_model="notebooklm.types.Source",
    )
    monkeypatch.setattr(
        reachability,
        "discover_private_dataclass_projection_paths",
        lambda _root: [*original, added],
    )
    with pytest.raises(ValueError, match="private-path allocations are not exact"):
        reachability.derive_adapter_sink_reachability_contract(
            _source_root(), known_projection_ids=_allocation_projection_ids()
        )


def _write_allocation_copy(tmp_path: Path, name: str, payload: object) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_sink_allocations_reject_duplicate_and_generic_review_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = json.loads(reachability._ALLOCATION_PATH.read_text(encoding="utf-8"))
    duplicated = copy.deepcopy(raw)
    duplicated["allocations"].append(copy.deepcopy(duplicated["allocations"][0]))
    monkeypatch.setattr(
        reachability,
        "_ALLOCATION_PATH",
        _write_allocation_copy(tmp_path, "duplicate.json", duplicated),
    )
    with pytest.raises(ValueError, match="duplicate reviewed adapter sink allocation"):
        reachability.derive_adapter_sink_reachability_contract(
            _source_root(), known_projection_ids=_allocation_projection_ids()
        )

    generic = copy.deepcopy(raw)
    reviewed = next(row for row in generic["allocations"] if "non_public_category" in row)
    reviewed["review_note"] = "no public dataclass"
    monkeypatch.setattr(
        reachability,
        "_ALLOCATION_PATH",
        _write_allocation_copy(tmp_path, "generic.json", generic),
    )
    with pytest.raises(ValueError, match="generic review_note"):
        reachability.derive_adapter_sink_reachability_contract(
            _source_root(), known_projection_ids=_allocation_projection_ids()
        )

    invalid_variant = copy.deepcopy(raw)
    variant_row = next(
        row for row in invalid_variant["allocations"] if "non_public_variants" in row
    )
    variant_row["non_public_variants"][0]["category"] = "generic"
    monkeypatch.setattr(
        reachability,
        "_ALLOCATION_PATH",
        _write_allocation_copy(tmp_path, "invalid-variant.json", invalid_variant),
    )
    with pytest.raises(ValueError, match="invalid non-public projection variant"):
        reachability.derive_adapter_sink_reachability_contract(
            _source_root(), known_projection_ids=_allocation_projection_ids()
        )


def test_sink_allocations_reject_known_cross_channel_projection_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = json.loads(reachability._ALLOCATION_PATH.read_text(encoding="utf-8"))
    mutated = copy.deepcopy(raw)
    row = next(
        item
        for item in mutated["allocations"]
        if item["locator"].startswith("cli --json|") and "projection_ids" in item
    )
    row["projection_ids"].append("rest.Note.dataclass-full")
    monkeypatch.setattr(
        reachability,
        "_ALLOCATION_PATH",
        _write_allocation_copy(tmp_path, "cross-channel.json", mutated),
    )
    with pytest.raises(ValueError, match="cross-channel projection ids"):
        reachability.derive_adapter_sink_reachability_contract(
            _source_root(),
            known_projection_ids={*_allocation_projection_ids(), "rest.Note.dataclass-full"},
        )


def test_private_path_allocations_reject_wrong_model_and_unrelated_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = json.loads(reachability._PRIVATE_PATH_ALLOCATION_PATH.read_text(encoding="utf-8"))
    wrong_model = copy.deepcopy(raw)
    wrong_model["allocations"][0]["projection_ids"].append("cli.Label.manual-list-projection")
    monkeypatch.setattr(
        reachability,
        "_PRIVATE_PATH_ALLOCATION_PATH",
        _write_allocation_copy(tmp_path, "wrong-model.json", wrong_model),
    )
    with pytest.raises(ValueError, match="wrong-model private-path projection ids"):
        reachability.derive_adapter_sink_reachability_contract(
            _source_root(), known_projection_ids=_allocation_projection_ids()
        )

    unrelated = copy.deepcopy(raw)
    non_public_locator = next(
        locator
        for locator, allocation in reachability._load_reviewed_allocations().items()
        if "non_public_category" in allocation
    )
    unrelated["allocations"][0]["terminal_locators"].append(non_public_locator)
    monkeypatch.setattr(
        reachability,
        "_PRIVATE_PATH_ALLOCATION_PATH",
        _write_allocation_copy(tmp_path, "unrelated.json", unrelated),
    )
    with pytest.raises(ValueError, match="do not intersect projection ids"):
        reachability.derive_adapter_sink_reachability_contract(
            _source_root(), known_projection_ids=_allocation_projection_ids()
        )
