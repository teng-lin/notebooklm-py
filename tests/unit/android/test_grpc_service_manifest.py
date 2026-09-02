"""Generated-service coverage for implemented Android RPC paths."""

from __future__ import annotations

import csv
import hashlib
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
    settings,
    sharing,
    source_search,
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
from notebooklm._android.proto.notebooklm.internal.android.wire.v1 import source_content_pb2

REPO_ROOT = Path(__file__).resolve().parents[3]
EXCEPTION_MANIFEST = REPO_ROOT / "docs" / "android" / "grpc-service-signature-exceptions.json"
INFERENCE_MANIFEST = REPO_ROOT / "docs" / "android" / "grpc-service-signature-inferences.json"
PARSER_OVERRIDE_MANIFEST = REPO_ROOT / "docs" / "android" / "grpc-runtime-parser-overrides.json"
EXTERNAL_METHOD_MANIFEST = (
    REPO_ROOT / "tests" / "fixtures" / "android" / "external_method_manifest.csv"
)
EXTERNAL_METHOD_MANIFEST_SHA256 = "411129064d2528b7ea108571ab382bd786055ed434209d6e733e13f130d9ebbd"
LATEST_APK_GRPC_SIGNATURES = (
    REPO_ROOT / "tests" / "fixtures" / "android" / "latest_apk_grpc_signatures.csv"
)
LATEST_APK_GRPC_SIGNATURES_SHA256 = (
    "6381163929c18d51eb654bc677846061ea65e9d501b9beb9db3952b749b32b7c"
)
LATEST_APK_GRPC_PATHS = REPO_ROOT / "tests" / "fixtures" / "android" / "latest_apk_grpc_paths.txt"
LATEST_APK_GRPC_PATHS_SHA256 = "b5df4996f271e71ccc14e0ae0f8eaa13e1e337b4bc726b54a487a0c4f6d31697"
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
    settings,
    sharing,
    source_search,
)
_EXPECTED_ORCHESTRATION_SIGNATURES = {
    "GetOrCreateAccount": (
        f"{ORCHESTRATION_PACKAGE}.GetOrCreateAccountRequest",
        f"{ORCHESTRATION_PACKAGE}.GetOrCreateAccountResponse",
        False,
    ),
    "MutateAccount": (
        f"{ORCHESTRATION_PACKAGE}.MutateAccountRequest",
        f"{ORCHESTRATION_PACKAGE}.Account",
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
    "DeleteProjects": (
        f"{ORCHESTRATION_PACKAGE}.DeleteProjectsRequest",
        "google.protobuf.Empty",
        False,
    ),
    "RemoveRecentlyViewedProject": (
        f"{ORCHESTRATION_PACKAGE}.RemoveRecentlyViewedProjectRequest",
        "google.protobuf.Empty",
        False,
    ),
    "MutateProject": (
        f"{ORCHESTRATION_PACKAGE}.MutateProjectRequest",
        f"{ORCHESTRATION_PACKAGE}.Project",
        False,
    ),
    "CopyProject": (
        f"{ORCHESTRATION_PACKAGE}.CopyProjectRequest",
        f"{ORCHESTRATION_PACKAGE}.Project",
        False,
    ),
    "GenerateNotebookGuide": (
        f"{ORCHESTRATION_PACKAGE}.GenerateNotebookGuideRequest",
        f"{ORCHESTRATION_PACKAGE}.GenerateNotebookGuideResponse",
        False,
    ),
    "GeneratePromptSuggestions": (
        f"{ORCHESTRATION_PACKAGE}.GeneratePromptSuggestionsRequest",
        f"{ORCHESTRATION_PACKAGE}.GeneratePromptSuggestionsResponse",
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
    "ListExpertIntelligenceContent": (
        f"{ORCHESTRATION_PACKAGE}.ListExpertIntelligenceContentRequest",
        f"{ORCHESTRATION_PACKAGE}.ListExpertIntelligenceContentResponse",
        False,
    ),
    "DeleteSources": (
        f"{ORCHESTRATION_PACKAGE}.DeleteSourcesRequest",
        "google.protobuf.Empty",
        False,
    ),
    "MutateSource": (
        f"{ORCHESTRATION_PACKAGE}.MutateSourceRequest",
        f"{ORCHESTRATION_PACKAGE}.MutateSourceResponse",
        False,
    ),
    "CheckSourceFreshness": (
        f"{ORCHESTRATION_PACKAGE}.CheckSourceFreshnessRequest",
        f"{ORCHESTRATION_PACKAGE}.CheckSourceFreshnessResponse",
        False,
    ),
    "RefreshSource": (
        f"{ORCHESTRATION_PACKAGE}.RefreshSourceRequest",
        f"{ORCHESTRATION_PACKAGE}.RefreshSourceResponse",
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
    "RetrieveRelevantChunks": (
        f"{ORCHESTRATION_PACKAGE}.RetrieveRelevantChunksRequest",
        f"{ORCHESTRATION_PACKAGE}.RetrieveRelevantChunksResponse",
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
    "DeriveArtifact": (
        f"{ORCHESTRATION_PACKAGE}.DeriveArtifactRequest",
        f"{ORCHESTRATION_PACKAGE}.DeriveArtifactResponse",
        False,
    ),
    "DeleteArtifact": (
        f"{ORCHESTRATION_PACKAGE}.DeleteArtifactRequest",
        "google.protobuf.Empty",
        False,
    ),
    "UpdateArtifact": (
        f"{ORCHESTRATION_PACKAGE}.UpdateArtifactRequest",
        f"{ORCHESTRATION_PACKAGE}.Artifact",
        False,
    ),
    "GenerateReportSuggestions": (
        f"{ORCHESTRATION_PACKAGE}.GenerateReportSuggestionsRequest",
        f"{ORCHESTRATION_PACKAGE}.GenerateReportSuggestionsResponse",
        False,
    ),
    "GenerateArtifact": (
        f"{ORCHESTRATION_PACKAGE}.GenerateArtifactRequest",
        f"{ORCHESTRATION_PACKAGE}.GenerateArtifactResponse",
        False,
    ),
    "ExportToDrive": (
        f"{ORCHESTRATION_PACKAGE}.ExportToDriveRequest",
        f"{ORCHESTRATION_PACKAGE}.ExportToDriveResponse",
        False,
    ),
    "ListChatSessions": (
        f"{ORCHESTRATION_PACKAGE}.ListChatSessionsRequest",
        f"{ORCHESTRATION_PACKAGE}.ListChatSessionsResponse",
        False,
    ),
    "GetChatSessionStatus": (
        f"{ORCHESTRATION_PACKAGE}.GetChatSessionStatusRequest",
        f"{ORCHESTRATION_PACKAGE}.GetChatSessionStatusResponse",
        False,
    ),
    "CancelGeneration": (
        f"{ORCHESTRATION_PACKAGE}.CancelGenerationRequest",
        f"{ORCHESTRATION_PACKAGE}.CancelGenerationResponse",
        False,
    ),
    "ListChatTurns": (
        f"{ORCHESTRATION_PACKAGE}.ListChatTurnsRequest",
        f"{ORCHESTRATION_PACKAGE}.ListChatTurnsResponse",
        False,
    ),
    "DeleteChatTurns": (
        f"{ORCHESTRATION_PACKAGE}.DeleteChatTurnsRequest",
        "google.protobuf.Empty",
        False,
    ),
    "GenerateFreeFormStreamed": (
        f"{ORCHESTRATION_PACKAGE}.GenerateFreeFormStreamedRequest",
        f"{ORCHESTRATION_PACKAGE}.GenerateFreeFormStreamedResponse",
        True,
    ),
    "ActOnSources": (
        f"{ORCHESTRATION_PACKAGE}.ActOnSourcesRequest",
        f"{ORCHESTRATION_PACKAGE}.ActOnSourcesResponse",
        False,
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
    "DeleteNotes": (
        f"{ORCHESTRATION_PACKAGE}.DeleteNotesRequest",
        f"{ORCHESTRATION_PACKAGE}.DeleteNotesResponse",
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
    "CreateLabel": (
        f"{ORCHESTRATION_PACKAGE}.CreateLabelRequest",
        f"{ORCHESTRATION_PACKAGE}.CreateLabelResponse",
        False,
    ),
    "MutateLabel": (
        f"{ORCHESTRATION_PACKAGE}.MutateLabelRequest",
        f"{ORCHESTRATION_PACKAGE}.MutateLabelResponse",
        False,
    ),
    "DeleteLabels": (
        f"{ORCHESTRATION_PACKAGE}.DeleteLabelsRequest",
        f"{ORCHESTRATION_PACKAGE}.DeleteLabelsResponse",
        False,
    ),
    # #2283 transfer / suggestion family (docs/android/copy-append-suggestion-evidence.md)
    "AddSourcesAsync": (
        f"{ORCHESTRATION_PACKAGE}.AddSourcesRequest",
        f"{ORCHESTRATION_PACKAGE}.AddSourcesAsyncResponse",
        False,
    ),
    "AppendSource": (
        f"{ORCHESTRATION_PACKAGE}.AppendSourceRequest",
        "google.protobuf.Empty",
        False,
    ),
    "CopySourcesAsync": (
        f"{ORCHESTRATION_PACKAGE}.CopySourcesAsyncRequest",
        f"{ORCHESTRATION_PACKAGE}.CopySourcesAsyncResponse",
        False,
    ),
    "CopyArtifactsAsync": (
        f"{ORCHESTRATION_PACKAGE}.CopyArtifactsAsyncRequest",
        f"{ORCHESTRATION_PACKAGE}.CopyArtifactsAsyncResponse",
        False,
    ),
    "NextStepSuggestions": (
        f"{ORCHESTRATION_PACKAGE}.NextStepSuggestionsRequest",
        f"{ORCHESTRATION_PACKAGE}.NextStepSuggestions",
        False,
    ),
    "GetArtifactCustomizationChoices": (
        f"{ORCHESTRATION_PACKAGE}.GetArtifactCustomizationChoicesRequest",
        f"{ORCHESTRATION_PACKAGE}.GetArtifactCustomizationChoicesResponse",
        False,
    ),
}
_EXPECTED_SHARING_SIGNATURES = {
    "GetProjectDetails": (
        "labs.language.tailwind.sharing.GetProjectDetailsRequest",
        "labs.language.tailwind.sharing.GetProjectDetailsResponse",
        False,
    ),
    "ShareProject": (
        "labs.language.tailwind.sharing.ShareProjectRequest",
        "labs.language.tailwind.sharing.ShareProjectResponse",
        False,
    ),
}
_LOCAL_PARSER_TYPES = {
    wire_notebooks_pb2.WireMutateProjectRequest.DESCRIPTOR.full_name,
    wire_notebooks_pb2.WireGenerateNotebookGuideResponse.DESCRIPTOR.full_name,
    wire_notebooks_pb2.WireGetProjectResponse.DESCRIPTOR.full_name,
    source_content_pb2.WireLoadSourceResponse.DESCRIPTOR.full_name,
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


def _inference_entries() -> list[dict[str, Any]]:
    payload = json.loads(INFERENCE_MANIFEST.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["bundle_sha256"] == (
        "8cc2569196b28083ba58a33319df79af97ec1832f442c4a182289894edf5eaef"
    )
    return payload["inferences"]


def _external_method_entries() -> dict[str, dict[str, str]]:
    with EXTERNAL_METHOD_MANIFEST.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 71
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


def test_service_descriptor_and_generated_stub_expose_all_admitted_paths() -> None:
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


def test_adapter_paths_equal_generated_descriptor_with_no_omitted_exceptions() -> None:
    entries = _manifest_entries()
    assert entries == []
    assert _adapter_paths() == _descriptor_paths()
    assert len(_adapter_paths()) == 59
    assert len(_descriptor_paths()) == 59
    assert sum(path.startswith(f"/{ORCHESTRATION_SERVICE}/") for path in _descriptor_paths()) == 57
    assert sum(path.startswith(f"/{SHARING_SERVICE}/") for path in _descriptor_paths()) == 2

    sharing_paths = {path for path in _adapter_paths() if path.startswith(f"/{SHARING_SERVICE}/")}
    assert sharing_paths == {
        f"/{SHARING_SERVICE}/GetProjectDetails",
        f"/{SHARING_SERVICE}/ShareProject",
    }
    assert sharing_paths <= _descriptor_paths()


def test_web_derived_signature_inferences_are_explicit_and_generated() -> None:
    entries = _inference_entries()
    assert len(entries) == 17
    assert all(
        set(entry) == {"path", "request_type", "response_type", "confidence", "evidence"}
        for entry in entries
    )
    paths = {entry["path"] for entry in entries}
    assert len(paths) == len(entries)
    assert paths < _descriptor_paths()
    assert f"/{ORCHESTRATION_SERVICE}/CancelDiscoverSourcesJob" not in paths
    signatures = _descriptor_signatures()
    for entry in entries:
        request_type, response_type, cardinality = signatures[entry["path"]]
        assert (request_type, response_type, cardinality) == (
            entry["request_type"],
            entry["response_type"],
            "unary_unary",
        )
        document, separator, anchor = entry["evidence"].partition("#")
        assert separator and anchor
        evidence_path = REPO_ROOT / document
        assert evidence_path.is_file()
        assert anchor in _markdown_anchors(evidence_path)


def test_external_manifest_and_implemented_signature_inventory_are_bidirectional() -> None:
    """Admit every implemented generated signature, including qualified web inferences."""

    assert hashlib.sha256(EXTERNAL_METHOD_MANIFEST.read_bytes()).hexdigest() == (
        EXTERNAL_METHOD_MANIFEST_SHA256
    )
    external = _external_method_entries()
    signatures = _descriptor_signatures()
    assert len(external) == 71

    for path, (request_type, response_type, cardinality) in signatures.items():
        row = external[path]
        assert row["request_type"].removeprefix(".") == request_type
        assert row["response_type"] not in {"NORMALIZED_EMPTY", "UNRESOLVED_PRIVATE"}
        assert row["response_type"].removeprefix(".") == response_type
        assert row["cardinality"] == cardinality

    for path in _adapter_paths():
        row = external[path]
        assert row["response_type"] not in {"NORMALIZED_EMPTY", "UNRESOLVED_PRIVATE"}
        assert path in signatures

    normalized_empty_paths = {
        path for path, row in external.items() if row["response_type"] == "NORMALIZED_EMPTY"
    }
    assert normalized_empty_paths & _adapter_paths() == set()
    assert _descriptor_paths().isdisjoint(normalized_empty_paths)


def test_latest_signed_apk_inventory_is_complete_exact_and_version_scoped() -> None:
    assert hashlib.sha256(LATEST_APK_GRPC_SIGNATURES.read_bytes()).hexdigest() == (
        LATEST_APK_GRPC_SIGNATURES_SHA256
    )
    assert hashlib.sha256(LATEST_APK_GRPC_PATHS.read_bytes()).hexdigest() == (
        LATEST_APK_GRPC_PATHS_SHA256
    )

    raw_paths = set(LATEST_APK_GRPC_PATHS.read_text(encoding="utf-8").splitlines())
    with LATEST_APK_GRPC_SIGNATURES.open(newline="", encoding="utf-8") as stream:
        exact = {row["path"]: row for row in csv.DictReader(stream)}

    upsert_path = f"/{ORCHESTRATION_SERVICE}/UpsertArtifactUserState"
    assert len(raw_paths) == 53
    assert len(exact) == 52
    assert raw_paths - exact.keys() == {upsert_path}
    assert exact.keys() <= raw_paths
    inferred_or_web_proven = {entry["path"] for entry in _inference_entries()} | {
        f"/{ORCHESTRATION_SERVICE}/CancelDiscoverSourcesJob"
    }
    assert inferred_or_web_proven.isdisjoint(raw_paths)

    new_paths = {
        f"/{ORCHESTRATION_SERVICE}/CancelGeneration",
        f"/{ORCHESTRATION_SERVICE}/ListArtifactScheduledNotificationConfigs",
        f"/{ORCHESTRATION_SERVICE}/UpdateArtifactScheduledNotificationConfig",
        "/google.internal.labs.tailwind.api.v1.DiscoveryService/BatchSearchNotebooks",
        "/google.internal.labs.tailwind.api.v1.DiscoveryService/SearchNotebooks",
    }
    assert new_paths <= exact.keys()
    assert (
        "/google.internal.labs.tailwind.discovery.v1."
        "LabsTailwindDiscoveryService/PrototypeNotebookSearch" not in raw_paths
    )


def test_runtime_local_parser_overrides_are_explicit_exact_path_exceptions() -> None:
    entries = _parser_override_entries()
    assert len(entries) == 6
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
        "import_cycle_overlay",
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
