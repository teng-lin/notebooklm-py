"""Descriptor and deterministic-wire gates for the B3 source overlays."""

from __future__ import annotations

from typing import Any

from google.protobuf.descriptor import FieldDescriptor

from notebooklm._android.proto.google.internal.labs.tailwind.orchestration.v1 import (
    b1_read_pb2,
    b3_sources_pb2,
)
from notebooklm._android.proto.notebooklm.internal.android.wire import (
    source_mutation_wire_pb2,
)

ORCHESTRATION_PACKAGE = "google.internal.labs.tailwind.orchestration.v1"
LOCAL_WIRE_PACKAGE = "notebooklm.internal.android.wire"


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
    assert b3_sources_pb2.DESCRIPTOR.package == ORCHESTRATION_PACKAGE
    assert [dependency.name for dependency in b3_sources_pb2.DESCRIPTOR.dependencies] == [
        "google/internal/labs/tailwind/orchestration/v1/b1_read.proto"
    ]
    assert b3_sources_pb2.DESCRIPTOR.services_by_name == {}
    assert set(b3_sources_pb2.DESCRIPTOR.message_types_by_name) == {
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
        "UserContent",
        "WebContent",
    }
    assert _shape(b3_sources_pb2.WebContent) == {
        "url": (1, False, FieldDescriptor.TYPE_STRING, None)
    }
    assert _shape(b3_sources_pb2.UserContent) == {
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
    assert _shape(b3_sources_pb2.AddTentativeSourcesRequest) == {
        "tentative_sources_metadata": (
            1,
            True,
            FieldDescriptor.TYPE_MESSAGE,
            f"{ORCHESTRATION_PACKAGE}.TentativeSourceMetadata",
        ),
        "project_id": (2, False, FieldDescriptor.TYPE_STRING, None),
    }
    assert 3 not in b3_sources_pb2.AddTentativeSourcesRequest.DESCRIPTOR.fields_by_number
    assert 4 not in b3_sources_pb2.AddTentativeSourcesRequest.DESCRIPTOR.fields_by_number


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
    registration = b3_sources_pb2.AddTentativeSourcesRequest(
        tentative_sources_metadata=[b3_sources_pb2.TentativeSourceMetadata(name="corr")],
        project_id="project",
    )
    commit = b3_sources_pb2.AddSourcesRequest(
        user_content=[
            b3_sources_pb2.UserContent(
                web_content=b3_sources_pb2.WebContent(url=" raw "),
                tentative_source_id=b1_read_pb2.SourceId(id="source"),
            )
        ],
        project_id="project",
    )
    mutation = source_mutation_wire_pb2.MutateSourceWireRequest(
        source_id=b1_read_pb2.SourceId(id="source"),
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
