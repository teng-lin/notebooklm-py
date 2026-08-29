"""Exact/local protobuf contract tests for B9 organization methods."""

from __future__ import annotations

from typing import Any

from google.protobuf.descriptor import FieldDescriptor

from notebooklm._android.proto.google.internal.labs.tailwind.orchestration.v1 import (
    organization_pb2,
    read_pb2,
)
from notebooklm._android.proto.notebooklm.android.wire.v1 import (
    organization_mutations_pb2,
)

ORCHESTRATION_PACKAGE = "google.internal.labs.tailwind.orchestration.v1"
LOCAL_PACKAGE = "notebooklm.android.wire.v1"


def _fields(message_type: type[Any]) -> dict[str, tuple[int, bool, int, str | None]]:
    return {
        field.name: (
            field.number,
            field.is_repeated,
            field.type,
            None if field.message_type is None else field.message_type.full_name,
        )
        for field in message_type.DESCRIPTOR.fields
    }


def test_exact_get_labels_package_fields_and_service_types_are_minimal() -> None:
    singular = False
    repeated = True
    string = FieldDescriptor.TYPE_STRING
    int32 = FieldDescriptor.TYPE_INT32
    message = FieldDescriptor.TYPE_MESSAGE

    assert organization_pb2.DESCRIPTOR.package == ORCHESTRATION_PACKAGE
    assert organization_pb2.DESCRIPTOR.services_by_name == {}
    assert [dependency.name for dependency in organization_pb2.DESCRIPTOR.dependencies] == [
        "google/internal/labs/tailwind/orchestration/v1/read.proto"
    ]
    assert _fields(organization_pb2.GetLabelsRequest) == {
        "project_id": (2, singular, string, None),
        "label_type": (3, singular, int32, None),
    }
    assert _fields(organization_pb2.LabelAndSources) == {
        "label": (1, singular, string, None),
        "source_ids": (2, repeated, message, f"{ORCHESTRATION_PACKAGE}.SourceId"),
        "label_id": (3, singular, string, None),
        "emoji": (4, singular, string, None),
    }
    assert _fields(organization_pb2.GetLabelsResponse) == {
        "label_and_sources": (
            1,
            repeated,
            message,
            f"{ORCHESTRATION_PACKAGE}.LabelAndSources",
        ),
        "notebook_collections": (
            2,
            repeated,
            message,
            f"{ORCHESTRATION_PACKAGE}.LabelAndSources",
        ),
    }


def test_repository_local_overlay_fields_are_exhaustive_and_visibly_local() -> None:
    proto = organization_mutations_pb2
    singular = False
    repeated = True
    string = FieldDescriptor.TYPE_STRING
    bytes_ = FieldDescriptor.TYPE_BYTES
    int32 = FieldDescriptor.TYPE_INT32
    message = FieldDescriptor.TYPE_MESSAGE

    assert proto.DESCRIPTOR.package == LOCAL_PACKAGE
    assert proto.DESCRIPTOR.services_by_name == {}
    assert _fields(proto.OrganizationRecordWire) == {
        "name": (1, singular, string, None),
        "member_ids": (2, repeated, bytes_, None),
        "id": (3, singular, string, None),
        "emoji": (4, singular, string, None),
    }
    assert _fields(proto.LabelPropertiesWire) == {
        "name": (1, singular, string, None),
        "emoji": (2, singular, string, None),
    }
    assert _fields(proto.ManualCreateLabelWire) == {
        "properties": (1, singular, message, f"{LOCAL_PACKAGE}.LabelPropertiesWire"),
        "source_ids": (2, repeated, string, None),
        "notebook_ids": (3, repeated, string, None),
    }
    assert _fields(proto.CreateLabelWireRequest) == {
        "project_id": (2, singular, string, None),
        "manual_create": (6, singular, message, f"{LOCAL_PACKAGE}.ManualCreateLabelWire"),
        "label_type": (7, singular, int32, None),
    }
    assert _fields(proto.MemberMutationWire) == {"member_ids": (1, repeated, string, None)}
    assert _fields(proto.LabelMutationWire) == {
        "properties": (1, singular, message, f"{LOCAL_PACKAGE}.LabelPropertiesWire"),
        "add_sources": (2, singular, message, f"{LOCAL_PACKAGE}.MemberMutationWire"),
        "remove_sources": (3, singular, message, f"{LOCAL_PACKAGE}.MemberMutationWire"),
        "add_notebooks": (4, singular, message, f"{LOCAL_PACKAGE}.MemberMutationWire"),
        "remove_notebooks": (5, singular, message, f"{LOCAL_PACKAGE}.MemberMutationWire"),
    }
    assert _fields(proto.MutateLabelWireRequest) == {
        "project_id": (2, singular, string, None),
        "label_id": (3, singular, string, None),
        "mutations": (4, repeated, message, f"{LOCAL_PACKAGE}.LabelMutationWire"),
        "label_type": (5, singular, int32, None),
    }
    assert _fields(proto.DeleteLabelsWireRequest) == {
        "project_id": (2, singular, string, None),
        "label_ids": (3, repeated, string, None),
        "label_type": (4, singular, int32, None),
    }
    assert _fields(proto.OrganizationMutationWireResponse) == {}


def test_request_wire_bytes_pin_both_resource_modes_and_one_member_operations() -> None:
    proto = organization_mutations_pb2
    assert organization_pb2.GetLabelsRequest(project_id="nb").SerializeToString().hex() == (
        "12026e62"
    )
    assert organization_pb2.GetLabelsRequest(label_type=3).SerializeToString().hex() == "1803"
    assert (
        proto.CreateLabelWireRequest(
            project_id="nb",
            manual_create=proto.ManualCreateLabelWire(
                properties=proto.LabelPropertiesWire(name="A", emoji="x")
            ),
        )
        .SerializeToString()
        .hex()
        == "12026e6232080a060a0141120178"
    )
    assert (
        proto.CreateLabelWireRequest(
            manual_create=proto.ManualCreateLabelWire(
                properties=proto.LabelPropertiesWire(name="A")
            ),
            label_type=3,
        )
        .SerializeToString()
        .hex()
        == "32050a030a01413803"
    )
    assert (
        proto.MutateLabelWireRequest(
            project_id="nb",
            label_id="l1",
            mutations=[
                proto.LabelMutationWire(add_sources=proto.MemberMutationWire(member_ids=["s1"]))
            ],
        )
        .SerializeToString()
        .hex()
        == "12026e621a026c31220612040a027331"
    )
    assert (
        proto.MutateLabelWireRequest(
            label_id="c1",
            mutations=[
                proto.LabelMutationWire(
                    remove_notebooks=proto.MemberMutationWire(member_ids=["n1"])
                )
            ],
            label_type=3,
        )
        .SerializeToString()
        .hex()
        == "1a02633122062a040a026e312803"
    )
    assert (
        proto.DeleteLabelsWireRequest(project_id="nb", label_ids=["l1", "l2"])
        .SerializeToString()
        .hex()
        == "12026e621a026c311a026c32"
    )
    assert (
        proto.DeleteLabelsWireRequest(label_ids=["c1"], label_type=3).SerializeToString().hex()
        == "1a0263312003"
    )


def test_local_read_overlay_preserves_heterogeneous_member_bytes_and_empty_presence() -> None:
    proto = organization_mutations_pb2
    source_id = "00000000-0000-4000-8000-000000000101"
    notebook_id = "00000000-0000-4000-8000-000000000201"
    response = proto.GetLabelsWireResponse(
        labels=[
            proto.OrganizationRecordWire(
                member_ids=[read_pb2.SourceId(id=source_id).SerializeToString()]
            )
        ],
        collections=[proto.OrganizationRecordWire(member_ids=[notebook_id.encode()])],
    )
    reparsed = proto.GetLabelsWireResponse.FromString(response.SerializeToString())
    assert read_pb2.SourceId.FromString(reparsed.labels[0].member_ids[0]).id == source_id
    assert reparsed.collections[0].member_ids == [notebook_id.encode()]

    properties = proto.LabelPropertiesWire(name="A", emoji="")
    assert properties.HasField("name")
    assert properties.HasField("emoji")
    assert properties.SerializeToString().hex() == "0a01411200"
