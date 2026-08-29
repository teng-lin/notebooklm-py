"""Exact generated-service coverage for implemented private Android RPC paths."""

from __future__ import annotations

import importlib
import json
import re
from pathlib import Path
from typing import Any

from notebooklm._android import (
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
    orchestration_service_pb2,
    orchestration_service_pb2_grpc,
    read_pb2,
    read_pb2_grpc,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
EXCEPTION_MANIFEST = REPO_ROOT / "docs" / "android" / "grpc-service-signature-exceptions.json"
ORCHESTRATION_PACKAGE = "google.internal.labs.tailwind.orchestration.v1"
ORCHESTRATION_SERVICE = f"{ORCHESTRATION_PACKAGE}.LabsTailwindOrchestrationService"
SHARING_SERVICE = "labs.language.tailwind.sharing.LabsTailwindSharingService"

_ADAPTER_MODULES = (notebooks, sources, artifacts, chat, notes, research, organization, sharing)
_EXPECTED_SIGNATURES = {
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
    "DeleteSources": (
        f"{ORCHESTRATION_PACKAGE}.DeleteSourcesRequest",
        "google.protobuf.Empty",
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
    "DeleteArtifact": (
        f"{ORCHESTRATION_PACKAGE}.DeleteArtifactRequest",
        "google.protobuf.Empty",
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
    "CancelDiscoverSourcesJob": (
        f"{ORCHESTRATION_PACKAGE}.CancelDiscoverSourcesJobRequest",
        "google.protobuf.Empty",
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


def _adapter_paths() -> set[str]:
    return {
        value
        for module in _ADAPTER_MODULES
        for name, value in vars(module).items()
        if name.endswith("_METHOD") and isinstance(value, str) and value.startswith("/")
    }


def _descriptor_paths() -> set[str]:
    service = orchestration_service_pb2.DESCRIPTOR.services_by_name[
        "LabsTailwindOrchestrationService"
    ]
    return {f"/{service.full_name}/{method.name}" for method in service.methods}


def _manifest_entries() -> list[dict[str, Any]]:
    payload = json.loads(EXCEPTION_MANIFEST.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    return payload["exceptions"]


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
    assert orchestration_service_pb2.DESCRIPTOR.package == ORCHESTRATION_PACKAGE
    assert list(orchestration_service_pb2.DESCRIPTOR.services_by_name) == [
        "LabsTailwindOrchestrationService"
    ]

    service = orchestration_service_pb2.DESCRIPTOR.services_by_name[
        "LabsTailwindOrchestrationService"
    ]
    assert service.full_name == ORCHESTRATION_SERVICE
    assert {
        method.name: (
            method.input_type.full_name,
            method.output_type.full_name,
            method.server_streaming,
        )
        for method in service.methods
    } == _EXPECTED_SIGNATURES
    assert all(not method.client_streaming for method in service.methods)

    channel = _RecordingChannel()
    stub = orchestration_service_pb2_grpc.LabsTailwindOrchestrationServiceStub(channel)
    assert set(channel.calls) == _descriptor_paths()
    assert set(vars(stub)) == set(_EXPECTED_SIGNATURES)
    assert channel.calls[f"/{ORCHESTRATION_SERVICE}/GenerateFreeFormStreamed"] == "unary_stream"
    assert set(channel.calls.values()) == {"unary_unary", "unary_stream"}


def test_adapter_paths_equal_exact_descriptor_plus_machine_readable_exceptions() -> None:
    entries = _manifest_entries()
    assert len(entries) == 14
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
    assert _descriptor_paths().isdisjoint(exception_paths)
    assert _adapter_paths() == _descriptor_paths() | exception_paths
    assert len(_adapter_paths()) == 39
    assert len(_descriptor_paths()) == 25

    for entry in entries:
        module_name, constant_name = entry["adapter_constant"].rsplit(".", 1)
        assert getattr(importlib.import_module(module_name), constant_name) == entry["path"]

    sharing_paths = {path for path in _adapter_paths() if path.startswith(f"/{SHARING_SERVICE}/")}
    assert sharing_paths == {
        f"/{SHARING_SERVICE}/GetProjectDetails",
        f"/{SHARING_SERVICE}/ShareProject",
    }
    assert sharing_paths <= exception_paths


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
