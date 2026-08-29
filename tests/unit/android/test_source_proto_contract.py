"""Descriptor and deterministic-wire gates for the B3 source overlays."""

from __future__ import annotations

from typing import Any

from google.protobuf.descriptor import FieldDescriptor

from notebooklm._android.proto.google.internal.labs.tailwind.orchestration.v1 import (
    read_pb2,
    sources_pb2,
)
from notebooklm._android.proto.labs.language.tailwind.common.protos import (
    metadata_pb2,
    provenance_pb2,
)
from notebooklm._android.proto.notebooklm.internal.android.wire import (
    source_mutation_wire_pb2,
)
from notebooklm._android.upload import android_provenance, android_request_context

ORCHESTRATION_PACKAGE = "google.internal.labs.tailwind.orchestration.v1"
LOCAL_WIRE_PACKAGE = "notebooklm.internal.android.wire"
COMMON_PACKAGE = "labs.language.tailwind.common.protos"


def _shape(message: Any) -> dict[str, tuple[int, bool, int, str | None]]:
    result: dict[str, tuple[int, bool, int, str | None]] = {}
    for field in message.DESCRIPTOR.fields:
        target = None
        if field.message_type is not None:
            target = field.message_type.full_name
        elif field.enum_type is not None:
            target = field.enum_type.full_name
        result[field.name] = (field.number, field.is_repeated, field.type, target)
    return result


def test_b3_exact_package_overlay_is_minimal_and_has_no_service_guess() -> None:
    assert sources_pb2.DESCRIPTOR.name == (
        "google/internal/labs/tailwind/orchestration/v1/sources.proto"
    )
    assert sources_pb2.DESCRIPTOR.package == ORCHESTRATION_PACKAGE
    assert [dependency.name for dependency in sources_pb2.DESCRIPTOR.dependencies] == [
        "google/internal/labs/tailwind/orchestration/v1/read.proto",
        "labs/language/tailwind/common/protos/metadata.proto",
        "labs/language/tailwind/common/protos/provenance.proto",
    ]
    assert sources_pb2.DESCRIPTOR.services_by_name == {}
    assert set(sources_pb2.DESCRIPTOR.message_types_by_name) == {
        "AddSourcesRequest",
        "AddSourcesResponse",
        "AddTentativeSourcesRequest",
        "AddTentativeSourcesResponse",
        "DeleteSourcesRequest",
        "DocumentGuide",
        "GenerateDocumentGuidesRequest",
        "GenerateDocumentGuidesResponse",
        "InputSource",
        "LoadSourceRequest",
        "LoadSourceResponse",
        "MainIdeas",
        "PlainTextSourceContent",
        "Snippet",
        "TentativeSourceMetadata",
        "UploadFileRequest",
        "UserContent",
        "WebContent",
    }
    assert _shape(sources_pb2.WebContent) == {
        "url": (1, False, FieldDescriptor.TYPE_STRING, None)
    }
    assert _shape(sources_pb2.UserContent) == {
        "web_content": (
            3,
            False,
            FieldDescriptor.TYPE_MESSAGE,
            f"{ORCHESTRATION_PACKAGE}.WebContent",
        ),
        "tentative_source_id": (
            9,
            False,
            FieldDescriptor.TYPE_MESSAGE,
            f"{ORCHESTRATION_PACKAGE}.SourceId",
        ),
    }
    assert _shape(sources_pb2.AddTentativeSourcesRequest) == {
        "tentative_sources_metadata": (
            1,
            True,
            FieldDescriptor.TYPE_MESSAGE,
            f"{ORCHESTRATION_PACKAGE}.TentativeSourceMetadata",
        ),
        "project_id": (2, False, FieldDescriptor.TYPE_STRING, None),
        "request_context": (
            3,
            False,
            FieldDescriptor.TYPE_MESSAGE,
            f"{COMMON_PACKAGE}.RequestContext",
        ),
        "provenance": (
            4,
            False,
            FieldDescriptor.TYPE_MESSAGE,
            f"{COMMON_PACKAGE}.Provenance",
        ),
    }
    assert _shape(sources_pb2.UploadFileRequest) == {
        "project_id": (3, False, FieldDescriptor.TYPE_STRING, None),
        "request_context": (
            4,
            False,
            FieldDescriptor.TYPE_MESSAGE,
            f"{COMMON_PACKAGE}.RequestContext",
        ),
        "source_id": (5, False, FieldDescriptor.TYPE_STRING, None),
        "provenance": (
            6,
            False,
            FieldDescriptor.TYPE_MESSAGE,
            f"{COMMON_PACKAGE}.Provenance",
        ),
    }


def test_b3b_context_and_provenance_packages_are_exact_minimal_closures() -> None:
    assert provenance_pb2.DESCRIPTOR.name == (
        "labs/language/tailwind/common/protos/provenance.proto"
    )
    assert provenance_pb2.DESCRIPTOR.package == COMMON_PACKAGE
    assert provenance_pb2.DESCRIPTOR.services_by_name == {}
    assert set(provenance_pb2.DESCRIPTOR.message_types_by_name) == {"ClientInfo", "Provenance"}
    assert _shape(provenance_pb2.ClientInfo) == {
        "application_platform": (
            1,
            False,
            FieldDescriptor.TYPE_ENUM,
            f"{COMMON_PACKAGE}.ClientInfo.ApplicationPlatform",
        ),
        "device": (
            2,
            False,
            FieldDescriptor.TYPE_ENUM,
            f"{COMMON_PACKAGE}.ClientInfo.Device",
        ),
        "application_version": (3, False, FieldDescriptor.TYPE_STRING, None),
    }
    assert _shape(provenance_pb2.Provenance) == {
        "origin_product_type": (
            1,
            False,
            FieldDescriptor.TYPE_ENUM,
            f"{COMMON_PACKAGE}.Provenance.OriginProductType",
        ),
        "client_info": (
            11,
            False,
            FieldDescriptor.TYPE_MESSAGE,
            f"{COMMON_PACKAGE}.ClientInfo",
        ),
    }
    assert metadata_pb2.DESCRIPTOR.package == COMMON_PACKAGE
    assert metadata_pb2.DESCRIPTOR.name == "labs/language/tailwind/common/protos/metadata.proto"
    assert [dependency.name for dependency in metadata_pb2.DESCRIPTOR.dependencies] == [
        "labs/language/tailwind/common/protos/provenance.proto"
    ]
    assert set(metadata_pb2.DESCRIPTOR.message_types_by_name) == {
        "ClientMetadata",
        "RequestContext",
    }
    assert set(metadata_pb2.DESCRIPTOR.enum_types_by_name) == {"ClientType"}


def test_b3_local_mutation_overlay_does_not_claim_the_remote_request_fqn() -> None:
    assert source_mutation_wire_pb2.DESCRIPTOR.package == LOCAL_WIRE_PACKAGE
    assert source_mutation_wire_pb2.DESCRIPTOR.services_by_name == {}
    assert _shape(source_mutation_wire_pb2.MutateSourceWireRequest) == {
        "source_id": (
            2,
            False,
            FieldDescriptor.TYPE_MESSAGE,
            f"{ORCHESTRATION_PACKAGE}.SourceId",
        ),
        "mutations": (
            3,
            True,
            FieldDescriptor.TYPE_MESSAGE,
            f"{LOCAL_WIRE_PACKAGE}.SourceMutation",
        ),
    }


def test_b3_request_bytes_are_pinned_without_context_or_unexercised_title() -> None:
    registration = sources_pb2.AddTentativeSourcesRequest(
        tentative_sources_metadata=[sources_pb2.TentativeSourceMetadata(name="corr")],
        project_id="project",
    )
    commit = sources_pb2.AddSourcesRequest(
        user_content=[
            sources_pb2.UserContent(
                web_content=sources_pb2.WebContent(url=" raw "),
                tentative_source_id=read_pb2.SourceId(id="source"),
            )
        ],
        project_id="project",
    )
    mutation = source_mutation_wire_pb2.MutateSourceWireRequest(
        source_id=read_pb2.SourceId(id="source"),
        mutations=[
            source_mutation_wire_pb2.SourceMutation(
                change_title=source_mutation_wire_pb2.ChangeTitle(title="new")
            )
        ],
    )

    assert registration.SerializeToString(deterministic=True).hex() == (
        "0a060a04636f7272120770726f6a656374"
    )
    assert commit.SerializeToString(deterministic=True).hex() == (
        "0a131a070a0520726177204a080a06736f75726365120770726f6a656374"
    )
    assert mutation.SerializeToString(deterministic=True).hex() == (
        "12080a06736f757263651a070a050a036e6577"
    )


def test_b3b_registration_and_upload_file_request_bytes_are_pinned() -> None:
    registration = sources_pb2.AddTentativeSourcesRequest(
        tentative_sources_metadata=[
            sources_pb2.TentativeSourceMetadata(name="document.pdf")
        ],
        project_id="project",
        request_context=android_request_context(),
        provenance=android_provenance(),
    )
    upload = sources_pb2.UploadFileRequest(
        project_id="project",
        request_context=android_request_context(),
        source_id="source",
        provenance=android_provenance(),
    )
    assert registration.SerializeToString(deterministic=True).hex() == (
        "0a0e0a0c646f63756d656e742e706466120770726f6a6563741a32080312120a10312e34362e"
        "372e393430393435343230221a08015a16080210011a10312e34362e372e393430393435343230"
        "221a08015a16080210011a10312e34362e372e393430393435343230"
    )
    assert upload.SerializeToString(deterministic=True).hex() == (
        "1a0770726f6a6563742232080312120a10312e34362e372e393430393435343230221a08015a16"
        "080210011a10312e34362e372e3934303934353432302a06736f75726365321a08015a16080210"
        "011a10312e34362e372e393430393435343230"
    )
