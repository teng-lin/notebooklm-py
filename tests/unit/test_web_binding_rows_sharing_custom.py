"""P9.4 custom-row core checks after the P9.2 sharing hoists.

The three sharing composites formerly provided convenient concrete rows for
testing error projection and collaborator plumbing. They are service-owned as
of P9.2-5/6/7, so this module now pins their absence while retaining the
provider-wide custom-row mechanics with synthetic rows.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Any

import pytest

from notebooklm._backend import BackendContractError
from notebooklm._binding import (
    BindingTable,
    CodecPayload,
    CustomBinding,
    ErrorMode,
    NativeCallSpec,
    NativeChoice,
    invoke_binding,
)
from notebooklm._operations import Operation
from notebooklm._records import SHARING_SET_PUBLIC_DEF, SharingSetPublicInput
from notebooklm._web.backend import (
    ROW_COLLABORATOR_NAMES,
    _build_binding_table,
    _row_error_projection,
)
from notebooklm._web.bindings import WEB_BINDING_ROWS
from notebooklm._web.registry import (
    WEB_OPERATION_REGISTRY,
    WEB_SUPPORTED_OPERATIONS,
    WebOperationBinding,
)
from notebooklm.rpc import RPCMethod


class _RecordingExecutor:
    async def rpc_call(self, *_args: object, **_kwargs: object) -> object:
        raise AssertionError("unexpected RPC call")


class _FakeTransport:
    def __init__(self, *responses: object) -> None:
        self.responses = list(responses)
        self.requests: list[tuple[Any, ...]] = []

    def assemble(
        self,
        definition: Any,
        native: Any,
        payload: Any,
        *,
        retry_flag: bool,
        deadline: Any,
        outcome_unknown_on_expiry: bool = False,
    ) -> tuple[Any, ...]:
        del deadline
        return (
            definition.key,
            native,
            payload,
            retry_flag,
            outcome_unknown_on_expiry,
        )

    async def call(self, request: tuple[Any, ...], *, deadline: Any) -> object:
        del deadline
        self.requests.append(request)
        return self.responses.pop(0)

    async def stream(self, request: tuple[Any, ...], *, deadline: Any) -> object:
        return await self.call(request, deadline=deadline)


def test_sharing_composites_are_service_owned_not_custom_rows() -> None:
    for operation in (
        Operation.SHARING_SET_PUBLIC,
        Operation.SHARING_SET_VIEW_LEVEL,
        Operation.SHARING_UPDATE_USERS,
    ):
        binding = WEB_OPERATION_REGISTRY[operation]
        assert binding.service_owned is True
        assert binding.row is None
        assert operation not in WEB_BINDING_ROWS
        assert operation not in WEB_SUPPORTED_OPERATIONS


def _row(mode: ErrorMode) -> CustomBinding[Any, Any, str]:
    async def handler(value: Any, deadline: Any, invoke: Any) -> object:
        del value
        return await invoke.call("only", CodecPayload(params=[]), deadline=deadline)

    return CustomBinding(
        definition=SHARING_SET_PUBLIC_DEF,
        handler=handler,
        native=(NativeCallSpec.constant("ONLY", key="only"),),
        justification="fake row for the projection table",
        category="protocol",
        error_mode=mode,
    )


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        (None, None),
        (_row(ErrorMode.TRANSLATE), False),
        (_row(ErrorMode.TRANSLATE_SCRUBBED), True),
    ],
    ids=["handler-backed", "translate", "translate-scrubbed"],
)
def test_the_head_projects_each_error_mode(
    row: CustomBinding[Any, Any, str] | None,
    expected: bool | None,
) -> None:
    assert _row_error_projection(row, Operation.SHARING_SET_PUBLIC) == expected


def test_the_source_add_rows_all_translate() -> None:
    """P10 invariant I8: every source-add row owns its public compatibility leaf.

    The head no longer has a raw-passthrough branch, so ``_row_error_projection``
    answers only "scrub request URLs?" — ``False`` for every one of these rows.
    """
    for operation in (
        Operation.SOURCE_ADD_URL,
        Operation.SOURCE_ADD_URL_BATCH,
        Operation.SOURCE_ADD_TEXT,
        Operation.SOURCE_ADD_DRIVE,
        Operation.SOURCE_ADD_FILE,
    ):
        assert _row_error_projection(WEB_BINDING_ROWS[operation], operation) is False
        assert _row_error_projection(None, operation) is None


def _custom_row_with_collaborators(*names: str) -> CustomBinding[Any, Any, Any]:
    async def handler(value: Any, deadline: Any, invoke: Any) -> object:
        del value, deadline
        return invoke.collaborator(names[0]) if names else None

    return CustomBinding(
        definition=SHARING_SET_PUBLIC_DEF,
        handler=handler,
        native=(NativeCallSpec.constant(RPCMethod.SHARE_NOTEBOOK, key="mutate"),),
        justification="fake row exercising collaborators",
        category="protocol",
        collaborators=names,
    )


def test_an_undeclared_or_unprovided_collaborator_fails_at_construction() -> None:
    registry = dict(WEB_OPERATION_REGISTRY)
    registry[Operation.SHARING_SET_PUBLIC] = WebOperationBinding(
        definition=SHARING_SET_PUBLIC_DEF,
        unsupported_reason=None,
        row=_custom_row_with_collaborators("not_a_collaborator"),
    )
    supported = WEB_SUPPORTED_OPERATIONS | {Operation.SHARING_SET_PUBLIC}
    with pytest.raises(BackendContractError, match="does not provide: not_a_collaborator"):
        _build_binding_table(registry=MappingProxyType(registry), supported=supported)

    registry[Operation.SHARING_SET_PUBLIC] = WebOperationBinding(
        definition=SHARING_SET_PUBLIC_DEF,
        unsupported_reason=None,
        row=_custom_row_with_collaborators(*sorted(ROW_COLLABORATOR_NAMES)),
    )
    table = _build_binding_table(
        registry=MappingProxyType(registry),
        supported=supported,
    )
    assert isinstance(table[Operation.SHARING_SET_PUBLIC], CustomBinding)


@pytest.mark.asyncio
async def test_the_invoker_exposes_only_declared_and_provided_collaborators() -> None:
    uploader = object()
    row = _custom_row_with_collaborators("source_uploader")
    table = BindingTable({Operation.SHARING_SET_PUBLIC: row})

    got = await invoke_binding(
        table,
        _FakeTransport(),
        None,
        SHARING_SET_PUBLIC_DEF,
        SharingSetPublicInput("nb", public=True),
        deadline=None,
        collaborators={"source_uploader": uploader, "deadline_factory": None},
    )
    assert got is uploader

    async def undeclared(value: Any, deadline: Any, invoke: Any) -> object:
        del value, deadline
        return invoke.collaborator("deadline_factory")

    row_undeclared = CustomBinding(
        definition=SHARING_SET_PUBLIC_DEF,
        handler=undeclared,
        native=row.native,
        justification="fake",
        category="protocol",
        collaborators=("source_uploader",),
    )
    with pytest.raises(BackendContractError, match="declares no collaborator 'deadline_factory'"):
        await invoke_binding(
            BindingTable({Operation.SHARING_SET_PUBLIC: row_undeclared}),
            _FakeTransport(),
            None,
            SHARING_SET_PUBLIC_DEF,
            SharingSetPublicInput("nb", public=True),
            deadline=None,
            collaborators={"source_uploader": uploader, "deadline_factory": None},
        )
    with pytest.raises(BackendContractError, match="was not provided"):
        await invoke_binding(
            table,
            _FakeTransport(),
            None,
            SHARING_SET_PUBLIC_DEF,
            SharingSetPublicInput("nb", public=True),
            deadline=None,
            collaborators={},
        )


@pytest.mark.asyncio
async def test_invoker_options_reach_the_assembled_request() -> None:
    transport = _FakeTransport("ok")

    async def handler(value: Any, deadline: Any, invoke: Any) -> object:
        del value
        return await invoke.call(
            "mutate",
            CodecPayload(params=["p"]),
            deadline=deadline,
            disable_internal_retries=True,
            outcome_unknown_on_expiry=True,
        )

    row = CustomBinding(
        definition=SHARING_SET_PUBLIC_DEF,
        handler=handler,
        native=(NativeCallSpec.constant(RPCMethod.SHARE_NOTEBOOK, key="mutate"),),
        justification="fake",
        category="protocol",
    )
    await invoke_binding(
        BindingTable({Operation.SHARING_SET_PUBLIC: row}),
        transport,
        None,
        SHARING_SET_PUBLIC_DEF,
        SharingSetPublicInput("nb", public=True),
        deadline=None,
    )
    assert transport.requests == [
        (
            Operation.SHARING_SET_PUBLIC,
            NativeChoice(RPCMethod.SHARE_NOTEBOOK),
            CodecPayload(params=["p"]),
            True,
            True,
        )
    ]
