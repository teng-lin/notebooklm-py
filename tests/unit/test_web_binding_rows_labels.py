"""P9.3 labels/collections: the seven leaf rows dispatch exactly as the handlers did.

``LABEL_LIST``/``LABEL_GET``/``LABEL_GENERATE``/``LABEL_DELETE`` and
``COLLECTION_LIST``/``COLLECTION_GET``/``COLLECTION_DELETE`` are
``encode → one native call → decode`` rows in ``_web/bindings/labels.py``.
These tests pin the conversion oracles: the identical keyword set reaches the
runtime for both dialects (route, ``allow_null``, explicit ``False``/``None``
values), the dialect and scope contract errors still fire before any wire
call, the get rows select by exact id inside ``decode``, and failure projection
is what ``invoke()`` produced for handler rows. Both create workflows now live
above the port in ``LabelSetService``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from notebooklm._deadline import RuntimeDeadline
from notebooklm._semantic.backend import (
    BackendContractError,
    BackendDeadlineExceededError,
    BackendError,
    BackendErrorReason,
    may_have_committed,
)
from notebooklm._semantic.binding import CodecBinding, DeadlineMode
from notebooklm._semantic.operations import Operation
from notebooklm._semantic.records import (
    COLLECTION_DELETE_DEF,
    COLLECTION_GET_DEF,
    COLLECTION_LIST_DEF,
    LABEL_DELETE_DEF,
    LABEL_GENERATE_DEF,
    LABEL_GET_DEF,
    LABEL_LIST_DEF,
    LabelDeleteInput,
    LabelGenerateInput,
    LabelGetInput,
    LabelKind,
    LabelListInput,
)
from notebooklm._web.backend import WebRpcBackend
from notebooklm._web.bindings import WEB_BINDING_ROWS
from notebooklm._web.bindings import labels as label_rows
from notebooklm._web.registry import WEB_OPERATION_REGISTRY
from notebooklm.exceptions import RPCTimeoutError, ServerError
from notebooklm.rpc import RPCMethod
from tests._fixtures.web_backend import build_web_backend

_NB = "nb_1"
_OPTS = [2, None, None, [1, None, None, None, None, None, None, None, None, None, [1]]]
_COLLECTION_OPTS = [
    2,
    None,
    None,
    [1, None, None, None, None, None, None, None, None, None, [1, 3]],
]


def _label(name: str, label_id: str, *sources: str) -> list[Any]:
    return [name, [[source] for source in sources] or None, label_id, ""]


def _collection(name: str, collection_id: str, *notebooks: str) -> list[Any]:
    return [name, list(notebooks) or None, collection_id, ""]


_LABEL_SET = [[_label("A", "l1", "s1"), _label("B", "l2")]]
_COLLECTION_SET = [None, [_collection("C", "c1", "nb_1"), _collection("D", "c2")]]


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


_BASE_KWARGS = {
    "_is_retry": False,
    "disable_internal_retries": False,
    "operation_variant": None,
    "read_timeout": None,
    "raise_on_null_status": False,
    "_retry_deadline": None,
}


def test_label_rows_replace_their_handlers_in_the_registry_and_table() -> None:
    converted = {
        Operation.LABEL_LIST: label_rows.LABEL_LIST,
        Operation.LABEL_GET: label_rows.LABEL_GET,
        Operation.LABEL_GENERATE: label_rows.LABEL_GENERATE,
        Operation.LABEL_DELETE: label_rows.LABEL_DELETE,
        Operation.COLLECTION_LIST: label_rows.COLLECTION_LIST,
        Operation.COLLECTION_GET: label_rows.COLLECTION_GET,
        Operation.COLLECTION_DELETE: label_rows.COLLECTION_DELETE,
    }
    assert {op: WEB_BINDING_ROWS[op] for op in converted} == converted
    for operation, row in converted.items():
        binding = WEB_OPERATION_REGISTRY[operation]
        assert binding.is_supported
        assert binding.row is row
        assert isinstance(row, CodecBinding)
        assert row.definition is binding.definition
        assert row.deadline is DeadlineMode.INHERIT
        assert row.native.is_constant
        assert row.forward_disable_internal_retries is False
    for name in (
        "_label_list",
        "_label_get",
        "_label_generate",
        "_label_delete",
        "_collection_list",
        "_collection_get",
        "_collection_delete",
        "_label_set_delete",
    ):
        assert not hasattr(WebRpcBackend, name)
    # All four create/update workflows are service-owned (no handler, no row,
    # not invokable); the emptied label mixin is gone from the backend chain.
    for operation in (
        Operation.LABEL_CREATE,
        Operation.LABEL_UPDATE,
        Operation.COLLECTION_CREATE,
        Operation.COLLECTION_UPDATE,
    ):
        assert WEB_OPERATION_REGISTRY[operation].row is None
        assert WEB_OPERATION_REGISTRY[operation].service_owned is True
    for name in ("_label_create", "_label_update", "_collection_create", "_collection_update"):
        assert not hasattr(WebRpcBackend, name)
    assert WebRpcBackend.__mro__ == (WebRpcBackend, object)
    backend = build_web_backend(_RecordingExecutor())
    assert backend._bindings[Operation.LABEL_LIST] is label_rows.LABEL_LIST


@pytest.mark.asyncio
async def test_source_label_rows_forward_the_identical_keyword_set() -> None:
    executor = _RecordingExecutor(_LABEL_SET, _LABEL_SET, [None, [_label("A", "l1")]], [])
    backend = build_web_backend(executor)

    listed = await backend.invoke(
        LABEL_LIST_DEF, LabelListInput(LabelKind.SOURCE_LABEL, _NB), deadline=None
    )
    got = await backend.invoke(
        LABEL_GET_DEF, LabelGetInput(LabelKind.SOURCE_LABEL, "l2", _NB), deadline=None
    )
    generated = await backend.invoke(
        LABEL_GENERATE_DEF, LabelGenerateInput(_NB, replace_existing=True), deadline=None
    )
    await backend.invoke(
        LABEL_DELETE_DEF,
        LabelDeleteInput(LabelKind.SOURCE_LABEL, ("l1", "l2"), _NB),
        deadline=None,
    )

    assert [label.id for label in listed.labels] == ["l1", "l2"]
    assert listed.labels[0].member_ids == ("s1",)
    assert listed.labels[0].kind is LabelKind.SOURCE_LABEL
    assert listed.labels[0].notebook_id == _NB
    assert got.label is not None
    assert got.label.id == "l2"
    assert [label.id for label in generated.labels] == ["l1"]

    list_call, get_call, generate_call, delete_call = executor.calls
    assert list_call.method is RPCMethod.LIST_LABELS
    assert list_call.params == [_OPTS, _NB]
    assert get_call.method is RPCMethod.LIST_LABELS
    assert get_call.params == [_OPTS, _NB]
    assert generate_call.method is RPCMethod.CREATE_LABEL
    assert generate_call.params == [_OPTS, _NB, None, None, []]
    assert delete_call.method is RPCMethod.DELETE_LABEL
    assert delete_call.params == [_OPTS, _NB, ["l1", "l2"]]
    notebook_path = f"/notebook/{_NB}"
    for call in (list_call, get_call):
        assert call.kwargs == {"source_path": notebook_path, "allow_null": False, **_BASE_KWARGS}
    for call in (generate_call, delete_call):
        assert call.kwargs == {"source_path": notebook_path, "allow_null": True, **_BASE_KWARGS}


@pytest.mark.asyncio
async def test_collection_rows_forward_the_account_route_and_allow_null() -> None:
    executor = _RecordingExecutor(_COLLECTION_SET, None, _COLLECTION_SET, [])
    backend = build_web_backend(executor)

    listed = await backend.invoke(
        COLLECTION_LIST_DEF, LabelListInput(LabelKind.COLLECTION), deadline=None
    )
    empty = await backend.invoke(
        COLLECTION_LIST_DEF, LabelListInput(LabelKind.COLLECTION), deadline=None
    )
    missing = await backend.invoke(
        COLLECTION_GET_DEF, LabelGetInput(LabelKind.COLLECTION, "nope"), deadline=None
    )
    await backend.invoke(
        COLLECTION_DELETE_DEF, LabelDeleteInput(LabelKind.COLLECTION, ("c1",)), deadline=None
    )

    assert [label.id for label in listed.labels] == ["c1", "c2"]
    assert listed.labels[0].kind is LabelKind.COLLECTION
    assert listed.labels[0].notebook_id is None
    assert listed.labels[0].member_ids == ("nb_1",)
    assert empty.labels == ()
    assert missing.label is None

    list_call, empty_call, get_call, delete_call = executor.calls
    for call in (list_call, empty_call, get_call):
        assert call.method is RPCMethod.LIST_LABELS
        assert call.params == [_COLLECTION_OPTS, None, 3]
    assert delete_call.method is RPCMethod.DELETE_LABEL
    assert delete_call.params == [_COLLECTION_OPTS, None, ["c1"], 3]
    for call in executor.calls:
        assert call.kwargs == {"source_path": "/", "allow_null": True, **_BASE_KWARGS}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("definition", "value", "fragment"),
    [
        (LABEL_LIST_DEF, LabelListInput(LabelKind.COLLECTION), "requires a source_label request"),
        (
            COLLECTION_GET_DEF,
            LabelGetInput(LabelKind.SOURCE_LABEL, "l1", _NB),
            "requires a collection request",
        ),
        (LABEL_LIST_DEF, LabelListInput(LabelKind.SOURCE_LABEL), "requires a notebook scope"),
        (
            LABEL_DELETE_DEF,
            LabelDeleteInput(LabelKind.SOURCE_LABEL, ("l1",)),
            "requires a notebook scope",
        ),
    ],
)
async def test_dialect_and_scope_contract_errors_fire_before_any_wire_call(
    definition: Any, value: Any, fragment: str
) -> None:
    executor = _RecordingExecutor()
    backend = build_web_backend(executor)

    with pytest.raises(BackendContractError) as caught:
        await backend.invoke(definition, value, deadline=None)

    assert caught.value.operation is definition.key
    assert fragment in caught.value.message
    assert executor.calls == []


@pytest.mark.asyncio
async def test_codec_row_server_error_translates_like_a_handler_and_is_dispatched() -> None:
    executor = _RecordingExecutor(ServerError("boom", method_id=RPCMethod.DELETE_LABEL.value))
    backend = build_web_backend(executor)

    with pytest.raises(BackendError) as caught:
        await backend.invoke(
            LABEL_DELETE_DEF,
            LabelDeleteInput(LabelKind.SOURCE_LABEL, ("l1",), _NB),
            deadline=None,
        )

    error = caught.value
    assert type(error) is BackendError
    assert error.operation is Operation.LABEL_DELETE
    assert error.reason is BackendErrorReason.SERVER
    assert error.message == "boom"
    assert error.outcome_unknown is False
    assert error.diagnostics is not None
    assert error.diagnostics["method_id"] == RPCMethod.DELETE_LABEL.value
    assert "public_error_failure" in error.diagnostics
    assert error.dispatched is True
    assert may_have_committed(error) is True
    assert isinstance(error.__cause__, ServerError)


@pytest.mark.asyncio
async def test_codec_row_timeout_after_expiry_becomes_a_dispatched_deadline_error() -> None:
    clock = [11.0]
    executor = _RecordingExecutor(RPCTimeoutError("slow", method_id=RPCMethod.LIST_LABELS.value))
    backend = build_web_backend(executor)
    deadline = RuntimeDeadline(timeout=5.0, started_at=10.0, monotonic=lambda: clock[0])

    async def rpc_call(method: RPCMethod, params: list[Any], **kwargs: Any) -> Any:
        clock[0] = 16.0
        return await _RecordingExecutor.rpc_call(executor, method, params, **kwargs)

    backend._runtime = type("Runtime", (), {"rpc_call": staticmethod(rpc_call)})()  # type: ignore[assignment]

    with pytest.raises(BackendDeadlineExceededError) as caught:
        await backend.invoke(
            COLLECTION_LIST_DEF, LabelListInput(LabelKind.COLLECTION), deadline=deadline
        )

    error = caught.value
    assert error.operation is Operation.COLLECTION_LIST
    assert error.reason is BackendErrorReason.TIMEOUT
    assert error.outcome_unknown is False  # READ policy
    assert error.dispatched is True
    assert error.diagnostics is not None
    assert error.diagnostics["timeout"] == 5.0
    assert error.diagnostics["method_id"] == RPCMethod.LIST_LABELS.value
    assert isinstance(error.__cause__, RPCTimeoutError)


@pytest.mark.asyncio
async def test_codec_row_pre_dispatch_expiry_is_not_dispatched() -> None:
    executor = _RecordingExecutor()
    backend = build_web_backend(executor)
    deadline = RuntimeDeadline(timeout=5.0, started_at=10.0, monotonic=lambda: 16.0)

    with pytest.raises(BackendDeadlineExceededError) as caught:
        await backend.invoke(LABEL_GENERATE_DEF, LabelGenerateInput(_NB), deadline=deadline)

    assert executor.calls == []
    assert caught.value.dispatched is False
    assert may_have_committed(caught.value) is False
