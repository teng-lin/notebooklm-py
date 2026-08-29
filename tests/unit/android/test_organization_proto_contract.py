"""Exact/local protobuf contract tests for organization methods."""

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


def test_generated_organization_package_fields_are_pinned() -> None:
    singular = False
    repeated = True
    string = FieldDescriptor.TYPE_STRING
    bool_ = FieldDescriptor.TYPE_BOOL
    int32 = FieldDescriptor.TYPE_INT32
    message = FieldDescriptor.TYPE_MESSAGE

    assert organization_pb2.DESCRIPTOR.package == ORCHESTRATION_PACKAGE
    assert organization_pb2.DESCRIPTOR.services_by_name == {}
    assert [dependency.name for dependency in organization_pb2.DESCRIPTOR.dependencies] == [
        "google/internal/labs/tailwind/orchestration/v1/read.proto",
        "labs/language/tailwind/common/protos/metadata.proto",
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
            f"{ORCHESTRATION_PACKAGE}.NotebookCollection",
        ),
    }
    assert _fields(organization_pb2.LabelProperties) == {
        "name": (1, singular, string, None),
        "emoji": (2, singular, string, None),
    }
    assert _fields(organization_pb2.ManualCreateLabel) == {
        "properties": (
            1,
            singular,
            message,
            f"{ORCHESTRATION_PACKAGE}.LabelProperties",
        ),
        "source_ids": (2, repeated, string, None),
        "notebook_ids": (3, repeated, string, None),
    }
    assert _fields(organization_pb2.AutoCreateLabel) == {
        "regenerate_all": (1, singular, bool_, None),
    }
    assert _fields(organization_pb2.CreateLabelRequest) == {
        "request_context": (
            1,
            singular,
            message,
            "labs.language.tailwind.common.protos.RequestContext",
        ),
        "project_id": (2, singular, string, None),
        "auto_create": (
            5,
            singular,
            message,
            f"{ORCHESTRATION_PACKAGE}.AutoCreateLabel",
        ),
        "manual_create": (
            6,
            singular,
            message,
            f"{ORCHESTRATION_PACKAGE}.ManualCreateLabel",
        ),
        "label_type": (7, singular, int32, None),
    }
    assert list(organization_pb2.CreateLabelRequest.DESCRIPTOR.oneofs_by_name) == ["create_mode"]
    assert {
        field.name
        for field in organization_pb2.CreateLabelRequest.DESCRIPTOR.oneofs_by_name[
            "create_mode"
        ].fields
    } == {"auto_create", "manual_create"}
    assert _fields(organization_pb2.CreateLabelResponse) == {
        "label_and_sources": (
            2,
            repeated,
            message,
            f"{ORCHESTRATION_PACKAGE}.LabelAndSources",
        ),
        "notebook_collections": (
            3,
            repeated,
            message,
            f"{ORCHESTRATION_PACKAGE}.NotebookCollection",
        ),
    }
    assert _fields(organization_pb2.NotebookCollection) == {
        "name": (1, singular, string, None),
        "notebook_ids": (2, repeated, string, None),
        "id": (3, singular, string, None),
        "emoji": (4, singular, string, None),
    }
    assert _fields(organization_pb2.MutateLabelProperties) == {
        "name": (1, singular, string, None),
        "emoji": (2, singular, string, None),
    }
    assert _fields(organization_pb2.AddSourcesMutation) == {
        "member_ids": (1, repeated, string, None)
    }
    assert _fields(organization_pb2.RemoveSourcesMutation) == {
        "member_ids": (1, repeated, string, None)
    }
    assert _fields(organization_pb2.AddNotebooksMutation) == {
        "member_ids": (1, repeated, string, None)
    }
    assert _fields(organization_pb2.RemoveNotebooksMutation) == {
        "member_ids": (1, repeated, string, None)
    }
    assert _fields(organization_pb2.LabelMutation) == {
        "properties": (
            1,
            singular,
            message,
            f"{ORCHESTRATION_PACKAGE}.MutateLabelProperties",
        ),
        "add_sources": (
            2,
            singular,
            message,
            f"{ORCHESTRATION_PACKAGE}.AddSourcesMutation",
        ),
        "remove_sources": (
            3,
            singular,
            message,
            f"{ORCHESTRATION_PACKAGE}.RemoveSourcesMutation",
        ),
        "add_notebooks": (
            4,
            singular,
            message,
            f"{ORCHESTRATION_PACKAGE}.AddNotebooksMutation",
        ),
        "remove_notebooks": (
            5,
            singular,
            message,
            f"{ORCHESTRATION_PACKAGE}.RemoveNotebooksMutation",
        ),
    }
    assert list(organization_pb2.LabelMutation.DESCRIPTOR.oneofs_by_name) == ["mutation"]
    assert {
        field.name
        for field in organization_pb2.LabelMutation.DESCRIPTOR.oneofs_by_name["mutation"].fields
    } == {
        "properties",
        "add_sources",
        "remove_sources",
        "add_notebooks",
        "remove_notebooks",
    }
    assert _fields(organization_pb2.MutateLabelRequest) == {
        "request_context": (
            1,
            singular,
            message,
            "labs.language.tailwind.common.protos.RequestContext",
        ),
        "project_id": (2, singular, string, None),
        "label_id": (3, singular, string, None),
        "mutations": (
            4,
            repeated,
            message,
            f"{ORCHESTRATION_PACKAGE}.LabelMutation",
        ),
        "label_type": (5, singular, int32, None),
    }
    assert _fields(organization_pb2.MutateLabelResponse) == {}
    assert _fields(organization_pb2.DeleteLabelsRequest) == {
        "request_context": (
            1,
            singular,
            message,
            "labs.language.tailwind.common.protos.RequestContext",
        ),
        "project_id": (2, singular, string, None),
        "label_ids": (3, repeated, string, None),
        "label_type": (4, singular, int32, None),
    }
    assert _fields(organization_pb2.DeleteLabelsResponse) == {}


def test_get_labels_collection_round_trips_raw_notebook_ids() -> None:
    response = organization_pb2.GetLabelsResponse(
        notebook_collections=[
            organization_pb2.NotebookCollection(
                name="Reading",
                notebook_ids=["00000000-0000-4000-8000-000000000001"],
                id="00000000-0000-4000-8000-000000000002",
                emoji="📚",
            )
        ]
    )

    decoded = organization_pb2.GetLabelsResponse.FromString(response.SerializeToString())

    assert decoded.notebook_collections[0].notebook_ids == ["00000000-0000-4000-8000-000000000001"]


def test_repository_local_overlay_fields_are_exhaustive_and_visibly_local() -> None:
    proto = organization_mutations_pb2
    singular = False
    repeated = True
    bytes_ = FieldDescriptor.TYPE_BYTES
    message = FieldDescriptor.TYPE_MESSAGE

    assert proto.DESCRIPTOR.package == LOCAL_PACKAGE
    assert proto.DESCRIPTOR.services_by_name == {}
    assert _fields(proto.OrganizationRecordWire) == {
        "name": (1, singular, FieldDescriptor.TYPE_STRING, None),
        "member_ids": (2, repeated, bytes_, None),
        "id": (3, singular, FieldDescriptor.TYPE_STRING, None),
        "emoji": (4, singular, FieldDescriptor.TYPE_STRING, None),
    }
    assert _fields(proto.GetLabelsWireResponse) == {
        "labels": (1, repeated, message, f"{LOCAL_PACKAGE}.OrganizationRecordWire"),
        "collections": (2, repeated, message, f"{LOCAL_PACKAGE}.OrganizationRecordWire"),
    }


def test_request_wire_bytes_pin_both_resource_modes_and_one_member_operations() -> None:
    proto = organization_pb2
    assert organization_pb2.GetLabelsRequest(project_id="nb").SerializeToString().hex() == (
        "12026e62"
    )
    assert organization_pb2.GetLabelsRequest(label_type=3).SerializeToString().hex() == "1803"
    unlabeled = proto.AutoCreateLabel(regenerate_all=False)
    assert unlabeled.HasField("regenerate_all")
    assert (
        proto.CreateLabelRequest(project_id="nb", auto_create=unlabeled).SerializeToString().hex()
        == "12026e622a020800"
    )
    regenerate_all = proto.AutoCreateLabel(regenerate_all=True)
    assert regenerate_all.HasField("regenerate_all")
    assert (
        proto.CreateLabelRequest(project_id="nb", auto_create=regenerate_all)
        .SerializeToString()
        .hex()
        == "12026e622a020801"
    )
    assert (
        proto.CreateLabelRequest(project_id="nb", auto_create=proto.AutoCreateLabel())
        .SerializeToString()
        .hex()
        == "12026e622a00"
    )
    assert (
        proto.CreateLabelRequest(
            project_id="nb",
            manual_create=proto.ManualCreateLabel(
                properties=proto.LabelProperties(name="A", emoji="x")
            ),
        )
        .SerializeToString()
        .hex()
        == "12026e6232080a060a0141120178"
    )
    assert (
        proto.CreateLabelRequest(
            manual_create=proto.ManualCreateLabel(properties=proto.LabelProperties(name="A")),
            label_type=3,
        )
        .SerializeToString()
        .hex()
        == "32050a030a01413803"
    )
    assert (
        proto.MutateLabelRequest(
            project_id="nb",
            label_id="l1",
            mutations=[
                proto.LabelMutation(add_sources=proto.AddSourcesMutation(member_ids=["s1"]))
            ],
        )
        .SerializeToString()
        .hex()
        == "12026e621a026c31220612040a027331"
    )
    assert (
        proto.MutateLabelRequest(
            label_id="c1",
            mutations=[
                proto.LabelMutation(
                    remove_notebooks=proto.RemoveNotebooksMutation(member_ids=["n1"])
                )
            ],
            label_type=3,
        )
        .SerializeToString()
        .hex()
        == "1a02633122062a040a026e312803"
    )
    assert (
        proto.DeleteLabelsRequest(project_id="nb", label_ids=["l1", "l2"]).SerializeToString().hex()
        == "12026e621a026c311a026c32"
    )
    assert (
        proto.DeleteLabelsRequest(label_ids=["c1"], label_type=3).SerializeToString().hex()
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

    properties = organization_pb2.LabelProperties(name="A", emoji="")
    assert properties.HasField("name")
    assert properties.HasField("emoji")
    assert properties.SerializeToString().hex() == "0a01411200"


def test_create_collection_response_wire_bytes_pin_live_field_three_row() -> None:
    response = organization_pb2.CreateLabelResponse(
        notebook_collections=[
            organization_pb2.NotebookCollection(
                name="Research",
                notebook_ids=["n1", "n2"],
                id="c1",
                emoji="x",
            )
        ]
    )
    assert (
        response.SerializeToString().hex()
        == "1a190a08526573656172636812026e3112026e321a026331220178"
    )
    assert organization_pb2.CreateLabelResponse.FromString(response.SerializeToString()) == response
