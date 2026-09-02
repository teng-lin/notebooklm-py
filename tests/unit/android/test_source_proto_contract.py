"""Descriptor and deterministic-wire gates for the source overlays."""

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
from notebooklm._android.proto.notebooklm.internal.android.wire.v1 import source_content_pb2
from notebooklm._android.upload import android_provenance, android_request_context

ORCHESTRATION_PACKAGE = "google.internal.labs.tailwind.orchestration.v1"
COMMON_PACKAGE = "labs.language.tailwind.common.protos"
LOCAL_WIRE_PACKAGE = "notebooklm.internal.android.wire.v1"


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


def test_source_generated_package_overlay_is_complete_and_service_free() -> None:
    assert sources_pb2.DESCRIPTOR.name == (
        "google/internal/labs/tailwind/orchestration/v1/sources.proto"
    )
    assert sources_pb2.DESCRIPTOR.package == ORCHESTRATION_PACKAGE
    assert [dependency.name for dependency in sources_pb2.DESCRIPTOR.dependencies] == [
        "google/internal/labs/tailwind/orchestration/v1/read.proto",
        "google/protobuf/timestamp.proto",
        "labs/language/tailwind/common/protos/metadata.proto",
        "labs/language/tailwind/common/protos/provenance.proto",
    ]
    assert sources_pb2.DESCRIPTOR.services_by_name == {}
    assert set(sources_pb2.DESCRIPTOR.message_types_by_name) == {
        "AddSourcesRequest",
        "AddSourcesResponse",
        "ExpertIntelligenceContent",
        "ExpertIntelligenceContentItem",
        "ListExpertIntelligenceContentRequest",
        "ListExpertIntelligenceContentResponse",
        "AddTentativeSourcesRequest",
        "AddTentativeSourcesResponse",
        "CheckSourceFreshnessRequest",
        "CheckSourceFreshnessResponse",
        "DeleteSourcesRequest",
        "DocumentGuide",
        "GenerateDocumentGuidesRequest",
        "GenerateDocumentGuidesResponse",
        "GoogleDriveContent",
        "InputSource",
        "LoadSourceRequest",
        "LoadSourceResponse",
        "MainIdeas",
        "MutateSourceRequest",
        "MutateSourceResponse",
        "PlainTextSourceContent",
        "RefreshSourceRequest",
        "RefreshSourceResponse",
        "RelevantChunk",
        "RelevantChunkContent",
        "RelevantChunkSpan",
        "RelevantChunkText",
        "RetrieveRelevantChunksOptions",
        "RetrieveRelevantChunksRequest",
        "RetrieveRelevantChunksResponse",
        "SourceIdFilter",
        "SourceRelevantChunks",
        "ChangeTitle",
        "Snippet",
        "SourceMutation",
        "SourceFreshness",
        "TentativeSourceMetadata",
        "TextContent",
        "UploadFileRequest",
        "UserContent",
        "VideoContent",
        "WebContent",
        # #2283 transfer family (docs/android/copy-append-suggestion-evidence.md)
        "AddSourcesAsyncResponse",
        "AppendSourceRequest",
        "CopiedSource",
        "CopySourcesAsyncRequest",
        "CopySourcesAsyncResponse",
        "SourceAcknowledgement",
        "SourceContent",
    }
    assert _shape(sources_pb2.WebContent) == {
        "url": (1, False, FieldDescriptor.TYPE_STRING, None),
        "source_name": (2, False, FieldDescriptor.TYPE_STRING, None),
    }
    assert _shape(sources_pb2.TextContent) == {
        "source_name": (1, False, FieldDescriptor.TYPE_STRING, None),
        "content": (2, False, FieldDescriptor.TYPE_STRING, None),
    }
    assert _shape(sources_pb2.GoogleDriveContent) == {
        "document_id": (1, False, FieldDescriptor.TYPE_STRING, None),
        "mime_type": (2, False, FieldDescriptor.TYPE_STRING, None),
        "can_download": (3, False, FieldDescriptor.TYPE_BOOL, None),
        "source_name": (4, False, FieldDescriptor.TYPE_STRING, None),
    }
    assert _shape(sources_pb2.VideoContent) == {
        "youtube_url": (1, False, FieldDescriptor.TYPE_STRING, None),
    }
    assert _shape(sources_pb2.UserContent) == {
        "google_drive_content": (
            1,
            False,
            FieldDescriptor.TYPE_MESSAGE,
            f"{ORCHESTRATION_PACKAGE}.GoogleDriveContent",
        ),
        "text_content": (
            2,
            False,
            FieldDescriptor.TYPE_MESSAGE,
            f"{ORCHESTRATION_PACKAGE}.TextContent",
        ),
        "web_content": (
            3,
            False,
            FieldDescriptor.TYPE_MESSAGE,
            f"{ORCHESTRATION_PACKAGE}.WebContent",
        ),
        "text_content_type": (
            4,
            False,
            FieldDescriptor.TYPE_ENUM,
            f"{ORCHESTRATION_PACKAGE}.UserContent.TextContentType",
        ),
        "video_content": (
            8,
            False,
            FieldDescriptor.TYPE_MESSAGE,
            f"{ORCHESTRATION_PACKAGE}.VideoContent",
        ),
        "tentative_source_id": (
            9,
            False,
            FieldDescriptor.TYPE_MESSAGE,
            f"{ORCHESTRATION_PACKAGE}.SourceId",
        ),
        "expert_intelligence_content": (
            16,
            False,
            FieldDescriptor.TYPE_MESSAGE,
            f"{ORCHESTRATION_PACKAGE}.ExpertIntelligenceContent",
        ),
    }
    assert _shape(sources_pb2.ExpertIntelligenceContent) == {
        "provider": (1, False, FieldDescriptor.TYPE_INT32, None),
        "content_id": (2, False, FieldDescriptor.TYPE_STRING, None),
        "title": (3, False, FieldDescriptor.TYPE_STRING, None),
        "description": (4, False, FieldDescriptor.TYPE_STRING, None),
        "thumbnail_image_url": (5, False, FieldDescriptor.TYPE_STRING, None),
        "field_type": (6, False, FieldDescriptor.TYPE_DOUBLE, None),
        "authors": (7, True, FieldDescriptor.TYPE_STRING, None),
    }
    assert _shape(sources_pb2.ExpertIntelligenceContentItem) == {
        "content_id": (1, False, FieldDescriptor.TYPE_STRING, None),
        "provider": (2, False, FieldDescriptor.TYPE_INT32, None),
        "title": (3, False, FieldDescriptor.TYPE_STRING, None),
        "description": (4, False, FieldDescriptor.TYPE_STRING, None),
        "thumbnail_image_url": (5, False, FieldDescriptor.TYPE_STRING, None),
        "export_disabled": (6, False, FieldDescriptor.TYPE_BOOL, None),
        "export_reason": (7, False, FieldDescriptor.TYPE_INT32, None),
        "authors": (8, True, FieldDescriptor.TYPE_STRING, None),
        "field_type": (9, False, FieldDescriptor.TYPE_DOUBLE, None),
        "updated_timestamp": (
            10,
            False,
            FieldDescriptor.TYPE_MESSAGE,
            "google.protobuf.Timestamp",
        ),
    }
    assert _shape(sources_pb2.ListExpertIntelligenceContentRequest) == {
        "request_context": (
            1,
            False,
            FieldDescriptor.TYPE_MESSAGE,
            f"{COMMON_PACKAGE}.RequestContext",
        ),
        "source_class": (2, False, FieldDescriptor.TYPE_INT32, None),
    }
    assert _shape(sources_pb2.ListExpertIntelligenceContentResponse) == {
        "items": (
            1,
            True,
            FieldDescriptor.TYPE_MESSAGE,
            f"{ORCHESTRATION_PACKAGE}.ExpertIntelligenceContentItem",
        ),
    }
    assert _shape(sources_pb2.AddSourcesRequest) == {
        "user_content": (
            1,
            True,
            FieldDescriptor.TYPE_MESSAGE,
            f"{ORCHESTRATION_PACKAGE}.UserContent",
        ),
        "project_id": (2, False, FieldDescriptor.TYPE_STRING, None),
        "request_context": (
            3,
            False,
            FieldDescriptor.TYPE_MESSAGE,
            f"{COMMON_PACKAGE}.RequestContext",
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


def test_retrieve_relevant_chunks_shapes_are_live_pinned() -> None:
    """#2283: the Web layout and native Android reply agree field-for-field."""
    message = FieldDescriptor.TYPE_MESSAGE
    string = FieldDescriptor.TYPE_STRING
    integer = FieldDescriptor.TYPE_INT32
    assert _shape(sources_pb2.RetrieveRelevantChunksOptions) == {
        "mode": (1, False, integer, None),
    }
    assert _shape(sources_pb2.SourceIdFilter) == {
        "source_ids": (1, True, message, f"{ORCHESTRATION_PACKAGE}.SourceId"),
    }
    assert _shape(sources_pb2.RetrieveRelevantChunksRequest) == {
        "project_id": (1, False, string, None),
        "query": (2, False, string, None),
        "options": (
            4,
            False,
            message,
            f"{ORCHESTRATION_PACKAGE}.RetrieveRelevantChunksOptions",
        ),
        "source_filter": (
            5,
            False,
            message,
            f"{ORCHESTRATION_PACKAGE}.SourceIdFilter",
        ),
    }
    assert _shape(sources_pb2.RelevantChunkText) == {
        "parts": (1, True, string, None),
    }
    assert _shape(sources_pb2.RelevantChunkContent) == {
        "text": (1, False, message, f"{ORCHESTRATION_PACKAGE}.RelevantChunkText"),
    }
    assert _shape(sources_pb2.RelevantChunkSpan) == {
        "start": (2, False, integer, None),
        "end": (3, False, integer, None),
    }
    assert _shape(sources_pb2.RelevantChunk) == {
        "content": (1, False, message, f"{ORCHESTRATION_PACKAGE}.RelevantChunkContent"),
        "rank": (2, False, integer, None),
        "spans": (3, True, message, f"{ORCHESTRATION_PACKAGE}.RelevantChunkSpan"),
    }
    assert _shape(sources_pb2.SourceRelevantChunks) == {
        "source_id": (1, False, string, None),
        "chunks": (2, True, message, f"{ORCHESTRATION_PACKAGE}.RelevantChunk"),
    }
    assert _shape(sources_pb2.RetrieveRelevantChunksResponse) == {
        "source_chunks": (1, True, message, f"{ORCHESTRATION_PACKAGE}.SourceRelevantChunks"),
    }


def test_load_source_local_overlay_admits_exact_tailwind_doc_field() -> None:
    assert source_content_pb2.DESCRIPTOR.name == (
        "notebooklm/internal/android/wire/v1/source_content.proto"
    )
    assert source_content_pb2.DESCRIPTOR.package == LOCAL_WIRE_PACKAGE
    assert source_content_pb2.DESCRIPTOR.services_by_name == {}
    assert _shape(source_content_pb2.WireLoadSourceResponse) == {
        "source": (
            1,
            False,
            FieldDescriptor.TYPE_MESSAGE,
            f"{ORCHESTRATION_PACKAGE}.Source",
        ),
        "plain_text": (
            2,
            False,
            FieldDescriptor.TYPE_MESSAGE,
            f"{ORCHESTRATION_PACKAGE}.PlainTextSourceContent",
        ),
        "markdown_string": (3, False, FieldDescriptor.TYPE_STRING, None),
        "tailwind_doc": (
            4,
            False,
            FieldDescriptor.TYPE_MESSAGE,
            f"{ORCHESTRATION_PACKAGE}.TailwindDoc",
        ),
    }


def test_source_context_and_provenance_packages_are_exact_minimal_closures() -> None:
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


def test_source_web_derived_mutation_request_and_response_shapes_are_pinned() -> None:
    assert _shape(sources_pb2.MutateSourceRequest) == {
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
            f"{ORCHESTRATION_PACKAGE}.SourceMutation",
        ),
        "request_context": (
            4,
            False,
            FieldDescriptor.TYPE_MESSAGE,
            "labs.language.tailwind.common.protos.RequestContext",
        ),
    }
    assert _shape(sources_pb2.SourceMutation) == {
        "change_title": (
            1,
            False,
            FieldDescriptor.TYPE_MESSAGE,
            f"{ORCHESTRATION_PACKAGE}.ChangeTitle",
        )
    }
    assert list(sources_pb2.SourceMutation.DESCRIPTOR.oneofs_by_name) == ["mutation"]
    assert [
        field.name
        for field in sources_pb2.SourceMutation.DESCRIPTOR.oneofs_by_name["mutation"].fields
    ] == ["change_title"]
    assert _shape(sources_pb2.MutateSourceResponse) == {
        "source": (
            1,
            False,
            FieldDescriptor.TYPE_MESSAGE,
            f"{ORCHESTRATION_PACKAGE}.Source",
        )
    }


def test_web_derived_freshness_shapes_are_pinned() -> None:
    assert _shape(sources_pb2.CheckSourceFreshnessRequest) == {
        "source_id": (
            2,
            False,
            FieldDescriptor.TYPE_MESSAGE,
            f"{ORCHESTRATION_PACKAGE}.SourceId",
        ),
        "request_context": (
            3,
            False,
            FieldDescriptor.TYPE_MESSAGE,
            f"{COMMON_PACKAGE}.RequestContext",
        ),
    }
    assert _shape(sources_pb2.SourceFreshness) == {
        "is_fresh": (2, False, FieldDescriptor.TYPE_BOOL, None),
        "source_id": (
            3,
            False,
            FieldDescriptor.TYPE_MESSAGE,
            f"{ORCHESTRATION_PACKAGE}.SourceId",
        ),
    }
    assert sources_pb2.SourceFreshness.DESCRIPTOR.fields_by_name["is_fresh"].has_presence
    assert _shape(sources_pb2.CheckSourceFreshnessResponse) == {
        "source_freshness": (
            1,
            False,
            FieldDescriptor.TYPE_MESSAGE,
            f"{ORCHESTRATION_PACKAGE}.SourceFreshness",
        )
    }
    assert _shape(sources_pb2.RefreshSourceRequest) == {
        "source_id": (
            2,
            False,
            FieldDescriptor.TYPE_MESSAGE,
            f"{ORCHESTRATION_PACKAGE}.SourceId",
        ),
        "request_context": (
            3,
            False,
            FieldDescriptor.TYPE_MESSAGE,
            f"{COMMON_PACKAGE}.RequestContext",
        ),
    }
    assert _shape(sources_pb2.RefreshSourceResponse) == {
        "source": (
            1,
            False,
            FieldDescriptor.TYPE_MESSAGE,
            f"{ORCHESTRATION_PACKAGE}.Source",
        )
    }


def test_source_request_bytes_are_pinned_without_context_or_unexercised_title() -> None:
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
    mutation = sources_pb2.MutateSourceRequest(
        source_id=read_pb2.SourceId(id="source"),
        mutations=[sources_pb2.SourceMutation(change_title=sources_pb2.ChangeTitle(title="new"))],
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


def test_source_registration_and_upload_file_request_bytes_are_pinned() -> None:
    registration = sources_pb2.AddTentativeSourcesRequest(
        tentative_sources_metadata=[sources_pb2.TentativeSourceMetadata(name="document.pdf")],
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


def test_transfer_family_request_and_response_shapes_are_pinned() -> None:
    """#2283: AddSourcesAsync / AppendSource / CopySourcesAsync (web-derived, live-pinned)."""
    message = FieldDescriptor.TYPE_MESSAGE
    string = FieldDescriptor.TYPE_STRING
    integer = FieldDescriptor.TYPE_INT32
    assert _shape(sources_pb2.SourceContent) == {
        "plain_text": (2, False, message, f"{ORCHESTRATION_PACKAGE}.PlainTextSourceContent"),
    }
    assert _shape(sources_pb2.AppendSourceRequest) == {
        "source_id": (2, False, message, f"{ORCHESTRATION_PACKAGE}.SourceId"),
        "content": (4, False, message, f"{ORCHESTRATION_PACKAGE}.SourceContent"),
    }
    assert _shape(sources_pb2.CopySourcesAsyncRequest) == {
        "source_ids": (3, True, message, f"{ORCHESTRATION_PACKAGE}.SourceId"),
        "target_project_id": (4, False, string, None),
    }
    assert _shape(sources_pb2.CopiedSource) == {
        "source_id": (1, False, message, f"{ORCHESTRATION_PACKAGE}.SourceId"),
        "source": (2, False, message, f"{ORCHESTRATION_PACKAGE}.Source"),
    }
    assert _shape(sources_pb2.CopySourcesAsyncResponse) == {
        "copied_sources": (1, True, message, f"{ORCHESTRATION_PACKAGE}.CopiedSource"),
    }
    assert _shape(sources_pb2.SourceAcknowledgement) == {
        "source": (1, False, message, f"{ORCHESTRATION_PACKAGE}.Source"),
        "status": (2, False, integer, None),
    }
    assert _shape(sources_pb2.AddSourcesAsyncResponse) == {
        "sources": (1, True, message, f"{ORCHESTRATION_PACKAGE}.Source"),
        "acknowledgements": (3, True, message, f"{ORCHESTRATION_PACKAGE}.SourceAcknowledgement"),
    }
