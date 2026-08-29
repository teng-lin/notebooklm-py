"""Shared Android organization transport seam and evidenced wire builders."""

from __future__ import annotations

import builtins
from typing import Any, Literal, cast

from ..exceptions import NotebookNotFoundError, RPCError
from ..types import Collection, Label
from .codecs.organization import decode_collections, decode_labels
from .session import AndroidSession

_SERVICE = "google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService"
GET_LABELS_METHOD = f"/{_SERVICE}/GetLabels"
CREATE_LABEL_METHOD = f"/{_SERVICE}/CreateLabel"
MUTATE_LABEL_METHOD = f"/{_SERVICE}/MutateLabel"
DELETE_LABELS_METHOD = f"/{_SERVICE}/DeleteLabels"

COLLECTION_TYPE = 3
OrganizationKind = Literal["label", "collection"]
MemberOperation = Literal["add_sources", "remove_sources", "add_notebooks", "remove_notebooks"]


def _exact_proto() -> Any:
    from .proto.google.internal.labs.tailwind.orchestration.v1 import organization_pb2

    return cast(Any, organization_pb2)


def _wire_proto() -> Any:
    from .proto.notebooklm.android.wire.v1 import organization_mutations_pb2

    return cast(Any, organization_mutations_pb2)


def _map_notebook_error(notebook_id: str, error: RPCError) -> RPCError:
    if error.rpc_code != 5:
        return error
    return NotebookNotFoundError(
        notebook_id,
        method_id=GET_LABELS_METHOD,
        raw_response=error.raw_response,
        rpc_code=error.rpc_code,
        found_ids=error.found_ids,
        detail=str(error),
    )


async def list_labels(
    transport: AndroidSession,
    notebook_id: str,
    *,
    expected_epoch: int,
) -> builtins.list[Label]:
    exact = _exact_proto()
    wire = _wire_proto()
    try:
        response = await transport.unary(
            GET_LABELS_METHOD,
            exact.GetLabelsRequest(project_id=notebook_id),
            replay_safe=True,
            response_type=wire.GetLabelsWireResponse,
            expected_epoch=expected_epoch,
        )
    except RPCError as exc:
        mapped = _map_notebook_error(notebook_id, exc)
        if mapped is exc:
            raise
        raise mapped from exc
    return decode_labels(response, notebook_id, method_id=GET_LABELS_METHOD)


async def list_collections(
    transport: AndroidSession,
    *,
    expected_epoch: int,
) -> builtins.list[Collection]:
    exact = _exact_proto()
    wire = _wire_proto()
    response = await transport.unary(
        GET_LABELS_METHOD,
        exact.GetLabelsRequest(label_type=COLLECTION_TYPE),
        replay_safe=True,
        response_type=wire.GetLabelsWireResponse,
        expected_epoch=expected_epoch,
    )
    return decode_collections(response, method_id=GET_LABELS_METHOD)


async def create_manual(
    transport: AndroidSession,
    *,
    kind: OrganizationKind,
    name: str,
    emoji: str,
    notebook_id: str | None,
    expected_epoch: int,
) -> None:
    wire = _wire_proto()
    properties = wire.LabelPropertiesWire(name=name)
    if emoji:
        properties.emoji = emoji
    manual = wire.ManualCreateLabelWire(properties=properties)
    request = wire.CreateLabelWireRequest(manual_create=manual)
    if kind == "label":
        assert notebook_id is not None
        request.project_id = notebook_id
    else:
        request.label_type = COLLECTION_TYPE
    await transport.unary(
        CREATE_LABEL_METHOD,
        request,
        replay_safe=False,
        response_type=wire.OrganizationMutationWireResponse,
        expected_epoch=expected_epoch,
    )


async def mutate_properties(
    transport: AndroidSession,
    *,
    kind: OrganizationKind,
    resource_id: str,
    name: str,
    emoji: str,
    notebook_id: str | None,
    expected_epoch: int,
) -> None:
    wire = _wire_proto()
    request = wire.MutateLabelWireRequest(
        label_id=resource_id,
        mutations=[
            wire.LabelMutationWire(properties=wire.LabelPropertiesWire(name=name, emoji=emoji))
        ],
    )
    if kind == "label":
        assert notebook_id is not None
        request.project_id = notebook_id
    else:
        request.label_type = COLLECTION_TYPE
    await transport.unary(
        MUTATE_LABEL_METHOD,
        request,
        replay_safe=False,
        response_type=wire.OrganizationMutationWireResponse,
        expected_epoch=expected_epoch,
    )


async def mutate_member(
    transport: AndroidSession,
    *,
    kind: OrganizationKind,
    resource_id: str,
    member_id: str,
    operation: MemberOperation,
    notebook_id: str | None,
    expected_epoch: int,
) -> None:
    wire = _wire_proto()
    mutation = wire.LabelMutationWire()
    getattr(mutation, operation).member_ids.append(member_id)
    request = wire.MutateLabelWireRequest(label_id=resource_id, mutations=[mutation])
    if kind == "label":
        assert notebook_id is not None
        request.project_id = notebook_id
    else:
        request.label_type = COLLECTION_TYPE
    await transport.unary(
        MUTATE_LABEL_METHOD,
        request,
        replay_safe=False,
        response_type=wire.OrganizationMutationWireResponse,
        expected_epoch=expected_epoch,
    )


async def delete_resources(
    transport: AndroidSession,
    *,
    kind: OrganizationKind,
    resource_ids: builtins.list[str],
    notebook_id: str | None,
    expected_epoch: int,
) -> None:
    wire = _wire_proto()
    request = wire.DeleteLabelsWireRequest(label_ids=resource_ids)
    if kind == "label":
        assert notebook_id is not None
        request.project_id = notebook_id
    else:
        request.label_type = COLLECTION_TYPE
    await transport.unary(
        DELETE_LABELS_METHOD,
        request,
        replay_safe=False,
        response_type=wire.OrganizationMutationWireResponse,
        expected_epoch=expected_epoch,
    )


__all__ = [
    "COLLECTION_TYPE",
    "CREATE_LABEL_METHOD",
    "DELETE_LABELS_METHOD",
    "GET_LABELS_METHOD",
    "MUTATE_LABEL_METHOD",
    "MemberOperation",
    "create_manual",
    "delete_resources",
    "list_collections",
    "list_labels",
    "mutate_member",
    "mutate_properties",
]
