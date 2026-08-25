"""P9.3 notebook reads: six leaf handlers became codec rows that dispatch as before.

The rows live in ``_web/bindings/notebooks.py``.  These tests pin the
conversion oracles: the identical keyword set reaches the runtime for every
converted operation (including explicit ``False``/``None`` values and the
route), the payloads are byte-for-byte the handlers' params, the non-uniform
``NOTEBOOK_LIST`` decoder still accepts its three payload shapes, the
``NOTEBOOK_GET`` decoder still branches on the input, the ``NOTEBOOK_PATCH``
primitive preserves the property-mask payload, the ``notebook.create``
composite still lists through a helper under its own attribution, failure
projection is what ``invoke()`` produced for handler rows, and the
``dispatched`` marker reaches the neutral error.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from notebooklm._backend import (
    BackendDeadlineExceededError,
    BackendError,
    BackendErrorReason,
    may_have_committed,
)
from notebooklm._binding import CodecBinding, CodecPayload, DeadlineMode
from notebooklm._deadline import RuntimeDeadline
from notebooklm._notebook_payloads import build_get_notebook_params
from notebooklm._operations import CallPolicy, Operation
from notebooklm._records import (
    NOTEBOOK_CREATE_DEF,
    NOTEBOOK_DELETE_DEF,
    NOTEBOOK_DESCRIBE_DEF,
    NOTEBOOK_GET_DEF,
    NOTEBOOK_LIST_DEF,
    NOTEBOOK_PATCH_DEF,
    NOTEBOOK_REMOVE_RECENT_DEF,
    NOTEBOOK_SUMMARIZE_DEF,
    NotebookCreateInput,
    NotebookDeleteInput,
    NotebookDeleteResult,
    NotebookGetInput,
    NotebookGuideInput,
    NotebookListInput,
    NotebookPatchInput,
    NotebookRemoveRecentInput,
    NotebookRemoveRecentResult,
)
from notebooklm._web.backend import WebRpcBackend
from notebooklm._web.bindings import WEB_BINDING_ROWS
from notebooklm._web.bindings import notebooks as notebook_rows
from notebooklm._web.codec import notebooks as notebooks_codec
from notebooklm._web.registry import WEB_OPERATION_REGISTRY
from notebooklm.exceptions import DecodingError, RPCTimeoutError, ServerError
from notebooklm.rpc import RPCMethod
from tests._fixtures.web_backend import build_web_backend

_NOTEBOOK_ROW: list[Any] = ["Title", [["src-1"], ["src-2"]], "nb_123", None, None, [1]]
_LIST_RESPONSE: list[Any] = [[_NOTEBOOK_ROW]]
_GET_RESPONSE: list[Any] = [_NOTEBOOK_ROW]
_GUIDE_RESPONSE: list[Any] = [[["A summary"], [[["Q?", "prompt"]]]]]

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


_CONVERTED: dict[Operation, CodecBinding[Any, Any, RPCMethod]] = {
    Operation.NOTEBOOK_LIST: notebook_rows.NOTEBOOK_LIST,
    Operation.NOTEBOOK_GET: notebook_rows.NOTEBOOK_GET,
    Operation.NOTEBOOK_PATCH: notebook_rows.NOTEBOOK_PATCH,
    Operation.NOTEBOOK_DELETE: notebook_rows.NOTEBOOK_DELETE,
    Operation.NOTEBOOK_REMOVE_RECENT: notebook_rows.NOTEBOOK_REMOVE_RECENT,
    Operation.NOTEBOOK_SUMMARIZE: notebook_rows.NOTEBOOK_SUMMARIZE,
    Operation.NOTEBOOK_DESCRIBE: notebook_rows.NOTEBOOK_DESCRIBE,
}

_EXPECTED_NATIVES = {
    Operation.NOTEBOOK_LIST: RPCMethod.LIST_NOTEBOOKS,
    Operation.NOTEBOOK_GET: RPCMethod.GET_NOTEBOOK,
    Operation.NOTEBOOK_PATCH: RPCMethod.RENAME_NOTEBOOK,
    Operation.NOTEBOOK_DELETE: RPCMethod.DELETE_NOTEBOOK,
    Operation.NOTEBOOK_REMOVE_RECENT: RPCMethod.REMOVE_RECENTLY_VIEWED,
    Operation.NOTEBOOK_SUMMARIZE: RPCMethod.SUMMARIZE,
    Operation.NOTEBOOK_DESCRIBE: RPCMethod.SUMMARIZE,
}


# --- registry partition ------------------------------------------------------


def test_notebook_rows_replace_their_handlers_and_composites_stay() -> None:
    assert {op: WEB_BINDING_ROWS[op] for op in _CONVERTED} == _CONVERTED
    # P9.4b keeps NOTEBOOK_CREATE as the remaining custom row in this table.
    assert {op: row for op, row in notebook_rows.NOTEBOOK_ROWS.items() if op in _CONVERTED} == (
        _CONVERTED
    )
    for operation, row in _CONVERTED.items():
        binding = WEB_OPERATION_REGISTRY[operation]
        assert binding.is_supported
        assert binding.handler_name is None
        assert binding.row is row
        assert isinstance(row, CodecBinding)
        assert row.definition is binding.definition
        assert row.deadline is DeadlineMode.INHERIT
        assert row.native.is_constant
        choice = row.native.select(None)
        assert (choice.method, choice.variant) == (_EXPECTED_NATIVES[operation], None)
        assert row.forward_disable_internal_retries is False
        assert row.map_error is (
            notebook_rows._map_required_get_not_found
            if operation is Operation.NOTEBOOK_GET
            else None
        )
    for name in (
        "_notebook_list",
        "_notebook_get",
        "_notebook_delete",
        "_notebook_remove_recent",
        "_notebook_guide",
        "_notebook_summarize",
        "_notebook_describe",
    ):
        assert not hasattr(WebRpcBackend, name)
    create = WEB_OPERATION_REGISTRY[Operation.NOTEBOOK_CREATE]
    assert create.handler_name is None
    assert create.row is notebook_rows.NOTEBOOK_CREATE
    update = WEB_OPERATION_REGISTRY[Operation.NOTEBOOK_UPDATE]
    assert update.handler_name is None and update.row is None
    assert update.service_owned is True
    for name in ("_list_notebooks", "_notebook_create", "_notebook_update"):
        assert not hasattr(WebRpcBackend, name)
    backend = build_web_backend(_RecordingExecutor())
    for operation, row in _CONVERTED.items():
        assert backend._bindings[operation] is row


# --- payload goldens -----------------------------------------------------------


def test_notebook_payload_goldens() -> None:
    assert notebooks_codec.encode_notebook_list(NotebookListInput()) == CodecPayload(
        params=[None, 1, None, [2]], source_path="/"
    )
    get = notebooks_codec.encode_notebook_get(NotebookGetInput("nb_123"))
    assert get == CodecPayload(
        params=build_get_notebook_params("nb_123"), source_path="/notebook/nb_123"
    )
    assert notebooks_codec.encode_notebook_delete(NotebookDeleteInput("nb_123")) == CodecPayload(
        params=[["nb_123"], [2]], source_path="/"
    )
    recent = notebooks_codec.encode_notebook_remove_recent(NotebookRemoveRecentInput("nb_123"))
    assert recent == CodecPayload(params=["nb_123"], source_path="/", allow_null=True)
    patch = notebooks_codec.encode_notebook_patch(
        NotebookPatchInput("nb_123", title="New", emoji=None)
    )
    assert patch == CodecPayload(
        params=[["nb_123", "New", None], [["title"]]], source_path="/", allow_null=True
    )
    guide = notebooks_codec.encode_notebook_guide_request(NotebookGuideInput("nb_123"))
    assert guide == CodecPayload(params=["nb_123", [2]], source_path="/notebook/nb_123")
    for payload in (get, guide):
        assert payload.allow_null is False
        assert payload.raise_on_null_status is False
        assert payload.attempt_timeout is None


# --- decoders ------------------------------------------------------------------


@pytest.mark.parametrize("raw", [None, [], [None]])
def test_notebook_list_decoder_accepts_the_empty_shapes(raw: Any) -> None:
    assert notebooks_codec.decode_notebook_list(NotebookListInput(), raw).notebooks == ()


def test_notebook_list_decoder_decodes_rows_and_rejects_unknown_shapes() -> None:
    (notebook,) = notebooks_codec.decode_notebook_list(
        NotebookListInput(), _LIST_RESPONSE
    ).notebooks
    assert (notebook.id, notebook.title, notebook.sources_count) == ("nb_123", "Title", 2)
    with pytest.raises(DecodingError, match="Unrecognized LIST_NOTEBOOKS payload shape"):
        notebooks_codec.decode_notebook_list(NotebookListInput(), [["not", "a", "row"]][0][0])


def test_notebook_get_decoder_branches_on_include_notebook() -> None:
    full = notebooks_codec.decode_notebook_get(NotebookGetInput("nb_123"), _GET_RESPONSE)
    assert full.notebook is not None
    assert full.notebook.id == "nb_123"
    assert full.source_ids == ("src-1", "src-2")
    ids_only = notebooks_codec.decode_notebook_get(
        NotebookGetInput("nb_123", include_notebook=False), _GET_RESPONSE
    )
    assert ids_only.notebook is None
    assert ids_only.source_ids == ("src-1", "src-2")
    missing = notebooks_codec.decode_notebook_get(NotebookGetInput("nb_123"), [None])
    assert missing.notebook is None
    assert missing.source_ids == ()
    blank = notebooks_codec.decode_notebook_get(NotebookGetInput("nb_123"), [["", [], ""]])
    assert blank.notebook is None


def test_notebook_guide_decoder_projects_summary_and_topics() -> None:
    result = notebooks_codec.decode_notebook_guide(NotebookGuideInput("nb_123"), _GUIDE_RESPONSE)
    assert result.description.summary == "A summary"
    assert [(t.question, t.prompt) for t in result.description.suggested_topics] == [
        ("Q?", "prompt")
    ]


# --- dispatch oracles ------------------------------------------------------------


@pytest.mark.asyncio
async def test_notebook_rows_forward_the_identical_keyword_set() -> None:
    executor = _RecordingExecutor(
        _LIST_RESPONSE, _GET_RESPONSE, None, None, _GUIDE_RESPONSE, _GUIDE_RESPONSE
    )
    backend = build_web_backend(executor)

    listed = await backend.invoke(NOTEBOOK_LIST_DEF, NotebookListInput(), deadline=None)
    got = await backend.invoke(NOTEBOOK_GET_DEF, NotebookGetInput("nb_123"), deadline=None)
    deleted = await backend.invoke(
        NOTEBOOK_DELETE_DEF, NotebookDeleteInput("nb_123"), deadline=None
    )
    removed = await backend.invoke(
        NOTEBOOK_REMOVE_RECENT_DEF, NotebookRemoveRecentInput("nb_123"), deadline=None
    )
    summary = await backend.invoke(
        NOTEBOOK_SUMMARIZE_DEF, NotebookGuideInput("nb_123"), deadline=None
    )
    description = await backend.invoke(
        NOTEBOOK_DESCRIBE_DEF, NotebookGuideInput("nb_123"), deadline=None
    )

    assert [notebook.id for notebook in listed.notebooks] == ["nb_123"]
    assert got.notebook is not None and got.notebook.id == "nb_123"
    assert deleted == NotebookDeleteResult()
    assert removed == NotebookRemoveRecentResult()
    assert summary.description.summary == "A summary"
    assert description.description.summary == "A summary"

    list_call, get_call, delete_call, recent_call, summarize_call, describe_call = executor.calls
    assert list_call.method is RPCMethod.LIST_NOTEBOOKS
    assert list_call.params == [None, 1, None, [2]]
    assert list_call.kwargs == {**_BASE_KWARGS, "source_path": "/"}
    assert get_call.method is RPCMethod.GET_NOTEBOOK
    assert get_call.params == build_get_notebook_params("nb_123")
    assert get_call.kwargs == {**_BASE_KWARGS, "source_path": "/notebook/nb_123"}
    assert delete_call.method is RPCMethod.DELETE_NOTEBOOK
    assert delete_call.params == [["nb_123"], [2]]
    assert delete_call.kwargs == {**_BASE_KWARGS, "source_path": "/"}
    assert recent_call.method is RPCMethod.REMOVE_RECENTLY_VIEWED
    assert recent_call.params == ["nb_123"]
    assert recent_call.kwargs == {**_BASE_KWARGS, "source_path": "/", "allow_null": True}
    for call in (summarize_call, describe_call):
        assert call.method is RPCMethod.SUMMARIZE
        assert call.params == ["nb_123", [2]]
        assert call.kwargs == {**_BASE_KWARGS, "source_path": "/notebook/nb_123"}


@pytest.mark.asyncio
async def test_notebook_create_composite_still_lists_through_the_helper() -> None:
    created_row: list[Any] = ["New", [], "nb_new", None, None, [1]]
    executor = _RecordingExecutor([[]], created_row)
    backend = build_web_backend(executor)

    result = await backend.invoke(NOTEBOOK_CREATE_DEF, NotebookCreateInput("New"), deadline=None)

    assert result.notebook.id == "nb_new"
    baseline, create = executor.calls
    assert baseline.method is RPCMethod.LIST_NOTEBOOKS
    assert baseline.params == [None, 1, None, [2]]
    assert baseline.kwargs == {**_BASE_KWARGS, "source_path": "/"}
    assert create.method is RPCMethod.CREATE_NOTEBOOK
    assert create.kwargs["disable_internal_retries"] is True


@pytest.mark.asyncio
async def test_codec_row_read_timeout_is_clamped_to_the_shared_deadline() -> None:
    executor = _RecordingExecutor(_LIST_RESPONSE)
    backend = build_web_backend(executor)
    deadline = RuntimeDeadline(timeout=5.0, started_at=10.0, monotonic=lambda: 11.0)

    await backend.invoke(NOTEBOOK_LIST_DEF, NotebookListInput(), deadline=deadline)

    (call,) = executor.calls
    assert call.kwargs["read_timeout"] == pytest.approx(4.0)
    assert call.kwargs["_retry_deadline"] is deadline


@pytest.mark.asyncio
async def test_codec_row_server_error_translates_like_a_handler_and_is_dispatched() -> None:
    executor = _RecordingExecutor(ServerError("boom", method_id=RPCMethod.DELETE_NOTEBOOK.value))
    backend = build_web_backend(executor)

    with pytest.raises(BackendError) as caught:
        await backend.invoke(NOTEBOOK_DELETE_DEF, NotebookDeleteInput("nb_123"), deadline=None)

    error = caught.value
    assert type(error) is BackendError
    assert error.operation is Operation.NOTEBOOK_DELETE
    assert error.reason is BackendErrorReason.SERVER
    assert error.message == "boom"
    assert error.outcome_unknown is False
    assert error.diagnostics is not None
    assert error.diagnostics["method_id"] == RPCMethod.DELETE_NOTEBOOK.value
    assert "public_error_failure" in error.diagnostics
    assert error.dispatched is True
    assert may_have_committed(error) is True
    assert isinstance(error.__cause__, ServerError)


@pytest.mark.asyncio
async def test_codec_row_timeout_after_expiry_becomes_a_dispatched_deadline_error() -> None:
    clock = [11.0]
    executor = _RecordingExecutor(RPCTimeoutError("slow", method_id=RPCMethod.GET_NOTEBOOK.value))
    backend = build_web_backend(executor)
    deadline = RuntimeDeadline(timeout=5.0, started_at=10.0, monotonic=lambda: clock[0])

    async def rpc_call(method: RPCMethod, params: list[Any], **kwargs: Any) -> Any:
        clock[0] = 16.0
        return await _RecordingExecutor.rpc_call(executor, method, params, **kwargs)

    backend._runtime = type("Runtime", (), {"rpc_call": staticmethod(rpc_call)})()  # type: ignore[assignment]

    with pytest.raises(BackendDeadlineExceededError) as caught:
        await backend.invoke(NOTEBOOK_GET_DEF, NotebookGetInput("nb_123"), deadline=deadline)

    error = caught.value
    assert error.operation is Operation.NOTEBOOK_GET
    assert error.reason is BackendErrorReason.TIMEOUT
    # ``notebook.get`` is not a READ (recency side effect), so uncertainty is reported
    # exactly as ``invoke()`` reported it for the handler: derived from the policy.
    assert error.outcome_unknown is (NOTEBOOK_GET_DEF.policy is not CallPolicy.READ)
    assert error.outcome_unknown is True
    assert error.dispatched is True
    assert may_have_committed(error) is True
    assert error.diagnostics is not None
    assert error.diagnostics["timeout"] == 5.0
    assert error.diagnostics["method_id"] == RPCMethod.GET_NOTEBOOK.value
    assert "public_error_failure" in error.diagnostics
    assert isinstance(error.__cause__, RPCTimeoutError)


@pytest.mark.asyncio
async def test_codec_row_pre_dispatch_expiry_is_not_dispatched() -> None:
    executor = _RecordingExecutor()
    backend = build_web_backend(executor)
    deadline = RuntimeDeadline(timeout=5.0, started_at=10.0, monotonic=lambda: 16.0)

    with pytest.raises(BackendDeadlineExceededError) as caught:
        await backend.invoke(NOTEBOOK_DELETE_DEF, NotebookDeleteInput("nb_123"), deadline=deadline)

    assert executor.calls == []
    assert caught.value.outcome_unknown is False
    assert caught.value.dispatched is False
    assert may_have_committed(caught.value) is False
