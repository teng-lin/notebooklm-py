"""Exact generated-service coverage for implemented private Android RPC paths."""

from __future__ import annotations

import csv
import hashlib
import importlib
import json
import re
from pathlib import Path
from typing import Any

from notebooklm._android import (
    account,
    artifacts,
    chat,
    notebooks,
    notes,
    organization,
    research,
    sharing,
    sources,
)
from notebooklm._android.proto.google.internal.labs.tailwind.orchestration.v1 import (
    notebooks_pb2,
    notebooks_pb2_grpc,
    orchestration_service_pb2,
    orchestration_service_pb2_grpc,
    read_pb2,
    read_pb2_grpc,
)
from notebooklm._android.proto.labs.language.tailwind.sharing import (
    sharing_pb2 as exact_sharing_pb2,
)
from notebooklm._android.proto.labs.language.tailwind.sharing import (
    sharing_pb2_grpc as exact_sharing_pb2_grpc,
)
from notebooklm._android.proto.notebooklm.android.wire.v1 import (
    organization_mutations_pb2,
)
from notebooklm._android.proto.notebooklm.android.wire.v1 import (
    sharing_pb2 as wire_sharing_pb2,
)
from notebooklm._android.proto.notebooklm.internal.android.wire.v1 import (
    notebooks_pb2 as wire_notebooks_pb2,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
EXCEPTION_MANIFEST = REPO_ROOT / "docs" / "android" / "grpc-service-signature-exceptions.json"
PARSER_OVERRIDE_MANIFEST = REPO_ROOT / "docs" / "android" / "grpc-runtime-parser-overrides.json"
EXTERNAL_METHOD_MANIFEST = (
    REPO_ROOT / "tests" / "fixtures" / "android" / "external_method_manifest.csv"
)
EXTERNAL_METHOD_MANIFEST_SHA256 = "c2cf4bf2e6cdefd35232f01572070fbe07d11ef9bad99b556f76b5e3748f38a3"
ORCHESTRATION_PACKAGE = "google.internal.labs.tailwind.orchestration.v1"
ORCHESTRATION_SERVICE = f"{ORCHESTRATION_PACKAGE}.LabsTailwindOrchestrationService"
SHARING_SERVICE = "labs.language.tailwind.sharing.LabsTailwindSharingService"

_ADAPTER_MODULES = (
    account,
    notebooks,
    sources,
    artifacts,
    chat,
    notes,
    research,
    organization,
    sharing,
)
_EXPECTED_ORCHESTRATION_SIGNATURES = {
    "GetOrCreateAccount": (
        f"{ORCHESTRATION_PACKAGE}.GetOrCreateAccountRequest",
        f"{ORCHESTRATION_PACKAGE}.GetOrCreateAccountResponse",
        False,
    ),
    "GetProject": (
        f"{ORCHESTRATION_PACKAGE}.GetProjectRequest",
        f"{ORCHESTRATION_PACKAGE}.GetProjectResponse",
        False,
    ),
    "ListRecentlyViewedProjects": (
        f"{ORCHESTRATION_PACKAGE}.ListRecentlyViewedProjectsRequest",
        f"{ORCHESTRATION_PACKAGE}.ListRecentlyViewedProjectsResponse",
        False,
    ),
    "CreateProject": (
        f"{ORCHESTRATION_PACKAGE}.CreateProjectRequest",
        f"{ORCHESTRATION_PACKAGE}.Project",
        False,
    ),
    "MutateProject": (
        f"{ORCHESTRATION_PACKAGE}.MutateProjectRequest",
        f"{ORCHESTRATION_PACKAGE}.Project",
        False,
    ),
    "GenerateNotebookGuide": (
        f"{ORCHESTRATION_PACKAGE}.GenerateNotebookGuideRequest",
        f"{ORCHESTRATION_PACKAGE}.GenerateNotebookGuideResponse",
        False,
    ),
    "AddTentativeSources": (
        f"{ORCHESTRATION_PACKAGE}.AddTentativeSourcesRequest",
        f"{ORCHESTRATION_PACKAGE}.AddTentativeSourcesResponse",
        False,
    ),
    "AddSources": (
        f"{ORCHESTRATION_PACKAGE}.AddSourcesRequest",
        f"{ORCHESTRATION_PACKAGE}.AddSourcesResponse",
        False,
    ),
    "GenerateDocumentGuides": (
        f"{ORCHESTRATION_PACKAGE}.GenerateDocumentGuidesRequest",
        f"{ORCHESTRATION_PACKAGE}.GenerateDocumentGuidesResponse",
        False,
    ),
    "LoadSource": (
        f"{ORCHESTRATION_PACKAGE}.LoadSourceRequest",
        f"{ORCHESTRATION_PACKAGE}.LoadSourceResponse",
        False,
    ),
    "ListArtifacts": (
        f"{ORCHESTRATION_PACKAGE}.ListArtifactsRequest",
        f"{ORCHESTRATION_PACKAGE}.ListArtifactsResponse",
        False,
    ),
    "GetArtifact": (
        f"{ORCHESTRATION_PACKAGE}.GetArtifactRequest",
        f"{ORCHESTRATION_PACKAGE}.GetArtifactResponse",
        False,
    ),
    "CreateArtifact": (
        f"{ORCHESTRATION_PACKAGE}.CreateArtifactRequest",
        f"{ORCHESTRATION_PACKAGE}.CreateArtifactResponse",
        False,
    ),
    "UpdateArtifact": (
        f"{ORCHESTRATION_PACKAGE}.UpdateArtifactRequest",
        f"{ORCHESTRATION_PACKAGE}.Artifact",
        False,
    ),
    "ListChatSessions": (
        f"{ORCHESTRATION_PACKAGE}.ListChatSessionsRequest",
        f"{ORCHESTRATION_PACKAGE}.ListChatSessionsResponse",
        False,
    ),
    "ListChatTurns": (
        f"{ORCHESTRATION_PACKAGE}.ListChatTurnsRequest",
        f"{ORCHESTRATION_PACKAGE}.ListChatTurnsResponse",
        False,
    ),
    "GenerateFreeFormStreamed": (
        f"{ORCHESTRATION_PACKAGE}.GenerateFreeFormStreamedRequest",
        f"{ORCHESTRATION_PACKAGE}.GenerateFreeFormStreamedResponse",
        True,
    ),
    "GetNotes": (
        f"{ORCHESTRATION_PACKAGE}.GetNotesRequest",
        f"{ORCHESTRATION_PACKAGE}.GetNotesResponse",
        False,
    ),
    "CreateNote": (
        f"{ORCHESTRATION_PACKAGE}.CreateNoteRequest",
        f"{ORCHESTRATION_PACKAGE}.CreateNoteResponse",
        False,
    ),
    "MutateNote": (
        f"{ORCHESTRATION_PACKAGE}.MutateNoteRequest",
        f"{ORCHESTRATION_PACKAGE}.MutateNoteResponse",
        False,
    ),
    "DiscoverSources": (
        f"{ORCHESTRATION_PACKAGE}.DiscoverSourcesRequest",
        f"{ORCHESTRATION_PACKAGE}.DiscoverSourcesResponse",
        False,
    ),
    "DiscoverSourcesManifold": (
        f"{ORCHESTRATION_PACKAGE}.DiscoverSourcesManifoldRequest",
        f"{ORCHESTRATION_PACKAGE}.DiscoverSourcesManifoldResponse",
        False,
    ),
    "DiscoverSourcesAsync": (
        f"{ORCHESTRATION_PACKAGE}.DiscoverSourcesAsyncRequest",
        f"{ORCHESTRATION_PACKAGE}.DiscoverSourcesAsyncResponse",
        False,
    ),
    "ListDiscoverSourcesJob": (
        f"{ORCHESTRATION_PACKAGE}.ListDiscoverSourcesJobRequest",
        f"{ORCHESTRATION_PACKAGE}.ListDiscoverSourcesJobResponse",
        False,
    ),
    "FinishDiscoverSourcesRun": (
        f"{ORCHESTRATION_PACKAGE}.FinishDiscoverSourcesRunRequest",
        f"{ORCHESTRATION_PACKAGE}.FinishDiscoverSourcesRunResponse",
        False,
    ),
    "GetLabels": (
        f"{ORCHESTRATION_PACKAGE}.GetLabelsRequest",
        f"{ORCHESTRATION_PACKAGE}.GetLabelsResponse",
        False,
    ),
}
_EXPECTED_SHARING_SIGNATURES = {
    "GetProjectDetails": (
        "labs.language.tailwind.sharing.GetProjectDetailsRequest",
        "labs.language.tailwind.sharing.GetProjectDetailsResponse",
        False,
    )
}
_LOCAL_PARSER_TYPES = {
    wire_notebooks_pb2.WireMutateProjectRequest.DESCRIPTOR.full_name,
    wire_notebooks_pb2.WireGenerateNotebookGuideResponse.DESCRIPTOR.full_name,
    organization_mutations_pb2.GetLabelsWireResponse.DESCRIPTOR.full_name,
    wire_sharing_pb2.GetProjectDetailsResponse.DESCRIPTOR.full_name,
}


def _adapter_paths() -> set[str]:
    return {
        value
        for module in _ADAPTER_MODULES
        for name, value in vars(module).items()
        if name.endswith("_METHOD") and isinstance(value, str) and value.startswith("/")
    }


def _exact_services() -> tuple[Any, Any]:
    return (
        orchestration_service_pb2.DESCRIPTOR.services_by_name["LabsTailwindOrchestrationService"],
        exact_sharing_pb2.DESCRIPTOR.services_by_name["LabsTailwindSharingService"],
    )


def _descriptor_paths() -> set[str]:
    return {
        f"/{service.full_name}/{method.name}"
        for service in _exact_services()
        for method in service.methods
    }


def _descriptor_signatures() -> dict[str, tuple[str, str, str]]:
    return {
        f"/{service.full_name}/{method.name}": (
            method.input_type.full_name,
            method.output_type.full_name,
            "unary_stream" if method.server_streaming else "unary_unary",
        )
        for service in _exact_services()
        for method in service.methods
    }


def _manifest_entries() -> list[dict[str, Any]]:
    payload = json.loads(EXCEPTION_MANIFEST.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    return payload["exceptions"]


def _parser_override_entries() -> list[dict[str, Any]]:
    payload = json.loads(PARSER_OVERRIDE_MANIFEST.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    return payload["overrides"]


def _external_method_entries() -> dict[str, dict[str, str]]:
    with EXTERNAL_METHOD_MANIFEST.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 53
    entries = {row["path"]: row for row in rows}
    assert len(entries) == len(rows)
    return entries


def _markdown_anchors(path: Path) -> set[str]:
    anchors: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("#"):
            continue
        heading = line.lstrip("#").strip().lower()
        anchors.add(re.sub(r"[^a-z0-9 -]", "", heading).replace(" ", "-"))
    return anchors


class _RecordingChannel:
    def __init__(self) -> None:
        self.calls: dict[str, str] = {}

    def unary_unary(self, path: str, **_kwargs: Any) -> object:
        self.calls[path] = "unary_unary"
        return object()

    def unary_stream(self, path: str, **_kwargs: Any) -> object:
        self.calls[path] = "unary_stream"
        return object()


def test_exact_service_descriptor_and_generated_stub_expose_all_admitted_paths() -> None:
    assert read_pb2.DESCRIPTOR.services_by_name == {}
    assert not hasattr(read_pb2_grpc, "LabsTailwindOrchestrationServiceStub")
    assert notebooks_pb2.DESCRIPTOR.services_by_name == {}
    assert not hasattr(notebooks_pb2_grpc, "LabsTailwindOrchestrationServiceStub")
    assert orchestration_service_pb2.DESCRIPTOR.package == ORCHESTRATION_PACKAGE
    assert list(orchestration_service_pb2.DESCRIPTOR.services_by_name) == [
        "LabsTailwindOrchestrationService"
    ]

    orchestration_service = orchestration_service_pb2.DESCRIPTOR.services_by_name[
        "LabsTailwindOrchestrationService"
    ]
    assert orchestration_service.full_name == ORCHESTRATION_SERVICE
    assert {
        method.name: (
            method.input_type.full_name,
            method.output_type.full_name,
            method.server_streaming,
        )
        for method in orchestration_service.methods
    } == _EXPECTED_ORCHESTRATION_SIGNATURES
    assert all(not method.client_streaming for method in orchestration_service.methods)

    sharing_service = exact_sharing_pb2.DESCRIPTOR.services_by_name["LabsTailwindSharingService"]
    assert sharing_service.full_name == SHARING_SERVICE
    assert {
        method.name: (
            method.input_type.full_name,
            method.output_type.full_name,
            method.server_streaming,
        )
        for method in sharing_service.methods
    } == _EXPECTED_SHARING_SIGNATURES
    assert all(not method.client_streaming for method in sharing_service.methods)

    channel = _RecordingChannel()
    stub = orchestration_service_pb2_grpc.LabsTailwindOrchestrationServiceStub(channel)
    sharing_stub = exact_sharing_pb2_grpc.LabsTailwindSharingServiceStub(channel)
    assert set(channel.calls) == _descriptor_paths()
    assert set(vars(stub)) == set(_EXPECTED_ORCHESTRATION_SIGNATURES)
    assert set(vars(sharing_stub)) == set(_EXPECTED_SHARING_SIGNATURES)
    assert channel.calls[f"/{ORCHESTRATION_SERVICE}/GenerateFreeFormStreamed"] == "unary_stream"
    assert set(channel.calls.values()) == {"unary_unary", "unary_stream"}


def test_adapter_paths_equal_exact_descriptor_plus_machine_readable_exceptions() -> None:
    entries = _manifest_entries()
    assert len(entries) == 13
    assert all(
        set(entry)
        == {
            "path",
            "adapter_constant",
            "request_parser",
            "response_parser",
            "reason_code",
            "evidence",
        }
        for entry in entries
    )
    assert {entry["reason_code"] for entry in entries} == {
        "request_fqn_unproven",
        "request_response_fqns_unproven",
        "response_fqn_unproven",
    }
    assert all(entry["evidence"].startswith("docs/android/") for entry in entries)

    for entry in entries:
        document, separator, anchor = entry["evidence"].partition("#")
        assert separator and anchor
        evidence_path = REPO_ROOT / document
        assert evidence_path.is_file()
        assert anchor in _markdown_anchors(evidence_path)

    exception_paths = {entry["path"] for entry in entries}
    assert len(exception_paths) == len(entries)
    entries_by_path = {entry["path"]: entry for entry in entries}
    assert entries_by_path[f"/{ORCHESTRATION_SERVICE}/DeleteProjects"]["reason_code"] == (
        "response_fqn_unproven"
    )
    for method in (
        "DeleteSources",
        "DeleteArtifact",
        "DeleteChatTurns",
        "CancelDiscoverSourcesJob",
    ):
        assert entries_by_path[f"/{ORCHESTRATION_SERVICE}/{method}"]["reason_code"] == (
            "response_fqn_unproven"
        )
    assert _descriptor_paths().isdisjoint(exception_paths)
    assert _adapter_paths() == _descriptor_paths() | exception_paths
    assert len(_adapter_paths()) == 40
    assert len(_descriptor_paths()) == 27
    assert sum(path.startswith(f"/{ORCHESTRATION_SERVICE}/") for path in _descriptor_paths()) == 26
    assert sum(path.startswith(f"/{SHARING_SERVICE}/") for path in _descriptor_paths()) == 1

    for entry in entries:
        module_name, constant_name = entry["adapter_constant"].rsplit(".", 1)
        assert getattr(importlib.import_module(module_name), constant_name) == entry["path"]

    sharing_paths = {path for path in _adapter_paths() if path.startswith(f"/{SHARING_SERVICE}/")}
    assert sharing_paths == {
        f"/{SHARING_SERVICE}/GetProjectDetails",
        f"/{SHARING_SERVICE}/ShareProject",
    }
    assert f"/{SHARING_SERVICE}/GetProjectDetails" in _descriptor_paths()
    assert f"/{SHARING_SERVICE}/ShareProject" in exception_paths


def test_external_manifest_and_implemented_signature_inventory_are_bidirectional() -> None:
    """Admit every implemented exact signature and reject normalized or unresolved responses."""

    assert hashlib.sha256(EXTERNAL_METHOD_MANIFEST.read_bytes()).hexdigest() == (
        EXTERNAL_METHOD_MANIFEST_SHA256
    )
    external = _external_method_entries()
    signatures = _descriptor_signatures()
    exceptions = {entry["path"]: entry for entry in _manifest_entries()}

    for path, (request_type, response_type, cardinality) in signatures.items():
        row = external[path]
        assert row["request_type"].removeprefix(".") == request_type
        assert row["response_type"] not in {"NORMALIZED_EMPTY", "UNRESOLVED_PRIVATE"}
        assert row["response_type"].removeprefix(".") == response_type
        assert row["cardinality"] == cardinality

    for path in _adapter_paths():
        row = external.get(path)
        if row is None:
            assert path in exceptions
            continue
        if row["response_type"] in {"NORMALIZED_EMPTY", "UNRESOLVED_PRIVATE"}:
            assert path in exceptions
            assert exceptions[path]["reason_code"] == "response_fqn_unproven"
            continue
        assert path in signatures

    for path, entry in exceptions.items():
        row = external.get(path)
        if row is not None:
            assert row["response_type"] in {"NORMALIZED_EMPTY", "UNRESOLVED_PRIVATE"}
            assert entry["reason_code"] == "response_fqn_unproven"

    normalized_empty_paths = {
        path for path, row in external.items() if row["response_type"] == "NORMALIZED_EMPTY"
    }
    assert normalized_empty_paths & _adapter_paths() == {
        f"/{ORCHESTRATION_SERVICE}/DeleteProjects",
        f"/{ORCHESTRATION_SERVICE}/DeleteSources",
        f"/{ORCHESTRATION_SERVICE}/DeleteArtifact",
        f"/{ORCHESTRATION_SERVICE}/DeleteChatTurns",
        f"/{ORCHESTRATION_SERVICE}/CancelDiscoverSourcesJob",
        f"/{SHARING_SERVICE}/ShareProject",
    }
    assert _descriptor_paths().isdisjoint(normalized_empty_paths)


def test_runtime_local_parser_overrides_are_explicit_exact_path_exceptions() -> None:
    entries = _parser_override_entries()
    assert len(entries) == 4
    assert all(
        set(entry)
        == {
            "path",
            "side",
            "remote_type",
            "adapter_parser",
            "reason_code",
            "evidence",
        }
        for entry in entries
    )
    assert {entry["adapter_parser"] for entry in entries} == _LOCAL_PARSER_TYPES
    assert {entry["reason_code"] for entry in entries} == {
        "heterogeneous_member_wire",
        "live_only_field",
        "presence_semantics",
    }

    signatures = _descriptor_signatures()
    for entry in entries:
        assert entry["path"] in signatures
        side_index = 0 if entry["side"] == "request" else 1
        assert entry["remote_type"] == signatures[entry["path"]][side_index]
        assert entry["adapter_parser"] != entry["remote_type"]
        document, separator, anchor = entry["evidence"].partition("#")
        assert separator and anchor
        evidence_path = REPO_ROOT / document
        assert evidence_path.is_file()
        assert anchor in _markdown_anchors(evidence_path)


def test_cumulative_descriptor_fixture_contains_the_exact_service_source() -> None:
    from google.protobuf import descriptor_pb2

    fixture = REPO_ROOT / "tests" / "fixtures" / "android" / "android_descriptor_set.pb"
    descriptor_set = descriptor_pb2.FileDescriptorSet.FromString(fixture.read_bytes())
    files = {file.name: file for file in descriptor_set.file}
    service_file = "google/internal/labs/tailwind/orchestration/v1/orchestration_service.proto"
    assert service_file in files
    assert [service.name for service in files[service_file].service] == [
        "LabsTailwindOrchestrationService"
    ]
    sharing_file = "labs/language/tailwind/sharing/sharing.proto"
    assert sharing_file in files
    assert [service.name for service in files[sharing_file].service] == [
        "LabsTailwindSharingService"
    ]
