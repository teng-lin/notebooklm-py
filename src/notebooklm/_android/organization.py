"""Shared Android organization transport seam and evidenced wire builders."""

from __future__ import annotations

import builtins
from typing import Any, Literal, cast

from .._idempotency import mark_unconfirmed
from ..exceptions import NotebookNotFoundError, RPCError
from ..types import Collection, Label
from .codecs.organization import decode_collections, decode_labels
from .session import AndroidSession
from .write_safety import call_unconfirmed_on_transport_loss

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


def _request_context() -> Any:
    # Keep protobuf imports deferred until an async operation, matching the
    # organization adapter's optional-runtime construction contract.
    from .upload import android_request_context

    return android_request_context()


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
) -> Any:
    exact = _exact_proto()
    properties = exact.LabelProperties(name=name)
    if emoji:
        properties.emoji = emoji
    manual = exact.ManualCreateLabel(properties=properties)
    request = exact.CreateLabelRequest(
        request_context=_request_context(),
        manual_create=manual,
    )
    if kind == "label":
        assert notebook_id is not None
        request.project_id = notebook_id
    else:
        request.label_type = COLLECTION_TYPE
    return await call_unconfirmed_on_transport_loss(
        lambda: transport.unary(
            CREATE_LABEL_METHOD,
            request,
            replay_safe=False,
            response_type=exact.CreateLabelResponse,
            expected_epoch=expected_epoch,
        )
    )


async def generate_labels(
    transport: AndroidSession,
    notebook_id: str,
    *,
    regenerate_all: bool,
    expected_epoch: int,
) -> builtins.list[Label]:
    """Generate source labels through CreateLabel's evidenced auto-create branch."""
    exact = _exact_proto()
    request = exact.CreateLabelRequest(
        request_context=_request_context(),
        project_id=notebook_id,
        auto_create=exact.AutoCreateLabel(regenerate_all=regenerate_all),
    )
    await call_unconfirmed_on_transport_loss(
        lambda: transport.unary(
            CREATE_LABEL_METHOD,
            request,
            replay_safe=False,
            response_type=exact.CreateLabelResponse,
            expected_epoch=expected_epoch,
        )
    )
    # Return the complete post-write set through the canonical heterogeneous
    # label decoder; CreateLabelResponse may contain only created rows.
    try:
        return await list_labels(transport, notebook_id, expected_epoch=expected_epoch)
    except Exception as error:
        # CreateLabel already returned successfully. Any ordinary failure in
        # the required GetLabels projection leaves the generated set unknown,
        # so a retry could repeat the mutation even when the read failed for a
        # non-transport reason. Cancellation and process-control exceptions
        # remain BaseException and therefore propagate untouched.
        raise mark_unconfirmed(error) from None


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
    exact = _exact_proto()
    request = exact.MutateLabelRequest(
        request_context=_request_context(),
        label_id=resource_id,
        mutations=[
            exact.LabelMutation(properties=exact.MutateLabelProperties(name=name, emoji=emoji))
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
        response_type=exact.MutateLabelResponse,
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
    exact = _exact_proto()
    mutation = exact.LabelMutation()
    getattr(mutation, operation).member_ids.append(member_id)
    request = exact.MutateLabelRequest(
        request_context=_request_context(),
        label_id=resource_id,
        mutations=[mutation],
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
        response_type=exact.MutateLabelResponse,
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
    exact = _exact_proto()
    request = exact.DeleteLabelsRequest(
        request_context=_request_context(),
        label_ids=resource_ids,
    )
    if kind == "label":
        assert notebook_id is not None
        request.project_id = notebook_id
    else:
        request.label_type = COLLECTION_TYPE
    await transport.unary(
        DELETE_LABELS_METHOD,
        request,
        replay_safe=False,
        response_type=exact.DeleteLabelsResponse,
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
    "generate_labels",
    "list_collections",
    "list_labels",
    "mutate_member",
    "mutate_properties",
]
