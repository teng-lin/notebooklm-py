"""P9.4a: the sharing composites dispatch as ``CustomBinding`` rows exactly as the handlers did.

``SHARING_SET_PUBLIC``, ``SHARING_SET_VIEW_LEVEL`` and ``SHARING_UPDATE_USERS``
each declare their ``mutate`` and ``readback`` specs and sequence them through
the row-scoped invoker.  These tests pin the conversion oracles: the identical
keyword set reaches the runtime for both phases (including explicit
``False``/``None`` values, ``allow_null`` on the guarded mutation and
``outcome_unknown_on_expiry`` on the readback), failures stay tagged with the
selected spec, the head honours each ``error_mode``, collaborators are a
closed declared set audited at construction, and the deadline projection is the
handler's (a pre-dispatch expiry on the readback is commit-uncertain).
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

import pytest

from notebooklm._backend import (
    BackendContractError,
    BackendDeadlineExceededError,
    BackendError,
    BackendErrorReason,
    may_have_committed,
)
from notebooklm._binding import (
    BindingTable,
    CodecPayload,
    CustomBinding,
    ErrorMode,
    NativeCallSpec,
    NativeChoice,
    invoke_binding,
)
from notebooklm._deadline import RuntimeDeadline
from notebooklm._operations import Operation
from notebooklm._records import (
    SHARING_SET_PUBLIC_DEF,
    SHARING_SET_VIEW_LEVEL_DEF,
    SHARING_UPDATE_USERS_DEF,
    SharePermissionLevel,
    ShareViewScope,
    SharingSetPublicInput,
    SharingSetViewLevelInput,
    SharingUpdateUsersInput,
    SharingUserGrant,
)
from notebooklm._web.backend import (
    ROW_COLLABORATOR_NAMES,
    _resolve_handler_bindings,
    _row_error_projection,
)
from notebooklm._web.bindings import WEB_BINDING_ROWS
from notebooklm._web.bindings import sharing as sharing_rows
from notebooklm._web.registry import (
    WEB_OPERATION_REGISTRY,
    WEB_SUPPORTED_OPERATIONS,
    WebOperationBinding,
)
from notebooklm.exceptions import RPCTimeoutError, ServerError
from notebooklm.rpc import RPCMethod
from tests._fixtures.web_backend import build_web_backend

_SHARE_STATUS_PAYLOAD: list[Any] = [
    [["owner@example.com", 1, None, ["Owner", None]]],
    [True],
    1000,
    True,
]

_BASE_KWARGS = {
    "allow_null": False,
    "_is_retry": False,
    "disable_internal_retries": False,
    "operation_variant": None,
    "read_timeout": None,
    "raise_on_null_status": False,
    "_retry_deadline": None,
}


@dataclass
class _Call:
    method: RPCMethod
    params: list[Any]
    kwargs: dict[str, Any]


class _RecordingExecutor:
    def __init__(self, *responses: object) -> None:
        self.responses = list(responses)
        self.calls: list[_Call] = []

    async def rpc_call(self, method: RPCMethod, params: list[Any], **kwargs: Any) -> Any:
        self.calls.append(_Call(method=method, params=params, kwargs=kwargs))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class _FakeTransport:
    def __init__(self, *responses: object) -> None:
        self.responses = list(responses)
        self.requests: list[tuple[Any, ...]] = []

    def assemble(
        self, definition, native, payload, *, retry_flag, deadline, outcome_unknown_on_expiry=False
    ):
        return (definition.key, native, payload, retry_flag, outcome_unknown_on_expiry)

    async def call(self, request, *, deadline):
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    async def stream(self, request, *, deadline):
        return await self.call(request, deadline=deadline)


# --- registry partition ----------------------------------------------------------


def test_sharing_composites_are_deferred_product_custom_rows() -> None:
    rows = {
        Operation.SHARING_SET_PUBLIC: (sharing_rows.SHARING_SET_PUBLIC, RPCMethod.SHARE_NOTEBOOK),
        Operation.SHARING_SET_VIEW_LEVEL: (
            sharing_rows.SHARING_SET_VIEW_LEVEL,
            RPCMethod.RENAME_NOTEBOOK,
        ),
        Operation.SHARING_UPDATE_USERS: (
            sharing_rows.SHARING_UPDATE_USERS,
            RPCMethod.SHARE_NOTEBOOK,
        ),
    }
    for operation, (row, mutate_method) in rows.items():
        assert isinstance(row, CustomBinding)
        assert WEB_BINDING_ROWS[operation] is row
        binding = WEB_OPERATION_REGISTRY[operation]
        assert binding.handler_name is None and binding.row is row
        assert row.category == "deferred-product"
        assert "stop/go" in row.justification
        assert row.error_mode is ErrorMode.TRANSLATE
        assert row.collaborators == ()
        assert row.spec("mutate").select(None) == NativeChoice(mutate_method)
        assert row.spec("readback").select(None) == NativeChoice(RPCMethod.GET_SHARE_STATUS)
    backend = build_web_backend(_RecordingExecutor())
    assert backend._bindings.custom_count == sum(
        1 for row in WEB_BINDING_ROWS.values() if isinstance(row, CustomBinding)
    )
    assert backend._bindings.custom_count_by_category()["deferred-product"] >= 3


# --- sequence and kwargs ----------------------------------------------------------


@pytest.mark.asyncio
async def test_set_public_mutates_then_reads_back_with_identical_kwargs() -> None:
    executor = _RecordingExecutor([], _SHARE_STATUS_PAYLOAD)
    backend = build_web_backend(executor)

    result = await backend.invoke(
        SHARING_SET_PUBLIC_DEF, SharingSetPublicInput("nb_123", public=True), deadline=None
    )

    assert result.status.is_public is True
    assert result.status.view_level is ShareViewScope.FULL_NOTEBOOK
    mutate, readback = executor.calls
    assert mutate.method is RPCMethod.SHARE_NOTEBOOK
    assert mutate.params == [[["nb_123", None, [1], [1, ""]]], 1, None, [2]]
    assert mutate.kwargs == {**_BASE_KWARGS, "source_path": "/notebook/nb_123", "allow_null": True}
    assert readback.method is RPCMethod.GET_SHARE_STATUS
    assert readback.params == ["nb_123", [2]]
    assert readback.kwargs == {**_BASE_KWARGS, "source_path": "/notebook/nb_123"}


@pytest.mark.asyncio
async def test_set_view_level_reports_the_level_it_just_set() -> None:
    executor = _RecordingExecutor([], _SHARE_STATUS_PAYLOAD)
    backend = build_web_backend(executor)

    result = await backend.invoke(
        SHARING_SET_VIEW_LEVEL_DEF,
        SharingSetViewLevelInput("nb_123", view_level=ShareViewScope.CHAT_ONLY),
        deadline=None,
    )

    assert result.status.view_level is ShareViewScope.CHAT_ONLY
    mutate, readback = executor.calls
    assert mutate.method is RPCMethod.RENAME_NOTEBOOK
    assert mutate.params[0] == "nb_123"
    assert mutate.kwargs == {**_BASE_KWARGS, "source_path": "/notebook/nb_123", "allow_null": True}
    assert readback.method is RPCMethod.GET_SHARE_STATUS
    assert readback.kwargs == {**_BASE_KWARGS, "source_path": "/notebook/nb_123"}


@pytest.mark.asyncio
async def test_update_users_sends_one_grant_envelope_then_reads_back() -> None:
    executor = _RecordingExecutor([], _SHARE_STATUS_PAYLOAD)
    backend = build_web_backend(executor)
    grants = (SharingUserGrant("viewer@example.com", SharePermissionLevel.VIEWER),)

    result = await backend.invoke(
        SHARING_UPDATE_USERS_DEF,
        SharingUpdateUsersInput("nb_123", grants=grants, notify=False, welcome_message=""),
        deadline=None,
    )

    assert [user.email for user in result.status.shared_users] == ["owner@example.com"]
    mutate, readback = executor.calls
    assert mutate.method is RPCMethod.SHARE_NOTEBOOK
    assert mutate.params == [
        [["nb_123", [["viewer@example.com", None, 3]], None, [1, ""]]],
        0,
        None,
        [2],
    ]
    assert mutate.kwargs == {**_BASE_KWARGS, "source_path": "/notebook/nb_123", "allow_null": True}
    assert readback.method is RPCMethod.GET_SHARE_STATUS


# --- failure projection -----------------------------------------------------------


@pytest.mark.asyncio
async def test_a_failed_readback_is_translated_dispatched_and_tagged_with_its_spec() -> None:
    executor = _RecordingExecutor(
        [], ServerError("boom", method_id=RPCMethod.GET_SHARE_STATUS.value)
    )
    backend = build_web_backend(executor)

    with pytest.raises(BackendError) as caught:
        await backend.invoke(
            SHARING_SET_PUBLIC_DEF, SharingSetPublicInput("nb_123", public=True), deadline=None
        )

    error = caught.value
    assert type(error) is BackendError
    assert error.operation is Operation.SHARING_SET_PUBLIC
    assert error.reason is BackendErrorReason.SERVER
    assert error.message == "boom"
    assert error.dispatched is True
    assert may_have_committed(error) is True
    assert isinstance(error.__cause__, ServerError)
    assert error.__cause__.binding_native == NativeChoice(RPCMethod.GET_SHARE_STATUS)  # type: ignore[attr-defined]
    assert len(executor.calls) == 2


@pytest.mark.asyncio
async def test_a_failed_mutation_never_reads_back() -> None:
    executor = _RecordingExecutor(ServerError("boom", method_id=RPCMethod.SHARE_NOTEBOOK.value))
    backend = build_web_backend(executor)

    with pytest.raises(BackendError) as caught:
        await backend.invoke(
            SHARING_SET_PUBLIC_DEF, SharingSetPublicInput("nb_123", public=True), deadline=None
        )

    assert caught.value.__cause__.binding_native == NativeChoice(RPCMethod.SHARE_NOTEBOOK)  # type: ignore[union-attr]
    assert len(executor.calls) == 1


def _row(mode: ErrorMode) -> CustomBinding[Any, Any, str]:
    async def handler(value, deadline, invoke):
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
        (None, (False, None)),
        (_row(ErrorMode.TRANSLATE), (False, False)),
        (_row(ErrorMode.TRANSLATE_SCRUBBED), (False, True)),
        (_row(ErrorMode.RAW_PASSTHROUGH), (True, False)),
    ],
    ids=["handler-backed", "translate", "translate-scrubbed", "raw-passthrough"],
)
def test_the_head_projects_each_error_mode(row: Any, expected: tuple[bool, bool | None]) -> None:
    assert _row_error_projection(row, Operation.SHARING_SET_PUBLIC) == expected


def test_handler_backed_source_add_operations_keep_the_raw_passthrough_set() -> None:
    for operation in (
        Operation.SOURCE_ADD_URL_BATCH,
        Operation.SOURCE_ADD_TEXT,
        Operation.SOURCE_ADD_DRIVE,
        Operation.SOURCE_ADD_FILE,
    ):
        assert _row_error_projection(None, operation) == (True, None)


# --- collaborators ------------------------------------------------------------------


def _custom_row_with_collaborators(*names: str) -> CustomBinding[Any, Any, Any]:
    async def handler(value, deadline, invoke):
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
    backend = build_web_backend(_RecordingExecutor())
    registry = dict(WEB_OPERATION_REGISTRY)
    canonical = registry[Operation.SHARING_SET_PUBLIC]
    registry[Operation.SHARING_SET_PUBLIC] = WebOperationBinding(
        definition=canonical.definition,
        handler_name=None,
        unsupported_reason=None,
        row=_custom_row_with_collaborators("not_a_collaborator"),
    )
    with pytest.raises(BackendContractError, match="does not provide: not_a_collaborator"):
        _resolve_handler_bindings(
            backend, registry=MappingProxyType(registry), supported=WEB_SUPPORTED_OPERATIONS
        )
    # Every provided name is accepted.
    registry[Operation.SHARING_SET_PUBLIC] = WebOperationBinding(
        definition=canonical.definition,
        handler_name=None,
        unsupported_reason=None,
        row=_custom_row_with_collaborators(*sorted(ROW_COLLABORATOR_NAMES)),
    )
    table = _resolve_handler_bindings(
        backend, registry=MappingProxyType(registry), supported=WEB_SUPPORTED_OPERATIONS
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

    async def undeclared(value, deadline, invoke):
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

    async def handler(value, deadline, invoke):
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


# --- deadline projection ----------------------------------------------------------


@pytest.mark.asyncio
async def test_readback_pre_dispatch_expiry_is_commit_uncertain_but_not_dispatched() -> None:
    clock = [11.0]
    executor = _RecordingExecutor([])
    backend = build_web_backend(executor)
    deadline = RuntimeDeadline(timeout=5.0, started_at=10.0, monotonic=lambda: clock[0])

    async def rpc_call(method: RPCMethod, params: list[Any], **kwargs: Any) -> Any:
        clock[0] = 16.0  # the mutation lands, then the budget is gone before the readback
        return await _RecordingExecutor.rpc_call(executor, method, params, **kwargs)

    backend._runtime = type("Runtime", (), {"rpc_call": staticmethod(rpc_call)})()  # type: ignore[assignment]

    with pytest.raises(BackendDeadlineExceededError) as caught:
        await backend.invoke(
            SHARING_SET_PUBLIC_DEF, SharingSetPublicInput("nb_123", public=True), deadline=deadline
        )

    error = caught.value
    assert error.operation is Operation.SHARING_SET_PUBLIC
    assert error.outcome_unknown is True  # a write was dispatched before the readback expired
    assert error.dispatched is False  # the readback itself never entered the runtime
    assert may_have_committed(error) is False
    assert error.diagnostics is not None
    assert error.diagnostics["method_id"] == RPCMethod.GET_SHARE_STATUS.value
    assert len(executor.calls) == 1


@pytest.mark.asyncio
async def test_readback_timeout_after_expiry_is_a_dispatched_deadline_error() -> None:
    clock = [11.0]
    executor = _RecordingExecutor(
        [], RPCTimeoutError("slow", method_id=RPCMethod.GET_SHARE_STATUS.value)
    )
    backend = build_web_backend(executor)
    deadline = RuntimeDeadline(timeout=5.0, started_at=10.0, monotonic=lambda: clock[0])

    async def rpc_call(method: RPCMethod, params: list[Any], **kwargs: Any) -> Any:
        if method is RPCMethod.GET_SHARE_STATUS:
            clock[0] = 16.0
        return await _RecordingExecutor.rpc_call(executor, method, params, **kwargs)

    backend._runtime = type("Runtime", (), {"rpc_call": staticmethod(rpc_call)})()  # type: ignore[assignment]

    with pytest.raises(BackendDeadlineExceededError) as caught:
        await backend.invoke(
            SHARING_SET_PUBLIC_DEF, SharingSetPublicInput("nb_123", public=True), deadline=deadline
        )

    error = caught.value
    assert error.outcome_unknown is True  # MUTATION policy
    assert error.dispatched is True
    assert may_have_committed(error) is True
    assert isinstance(error.__cause__, RPCTimeoutError)
