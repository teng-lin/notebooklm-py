"""P9.3 notebook reads: six leaf handlers became codec rows that dispatch as before.

The rows live in ``_web/bindings/notebooks.py``.  These tests pin the
conversion oracles: the identical keyword set reaches the runtime for every
converted operation (including explicit ``False``/``None`` values and the
route), the payloads are byte-for-byte the handlers' params, the non-uniform
``NOTEBOOK_LIST`` decoder still accepts its three payload shapes, the
``NOTEBOOK_GET`` decoder still branches on the input, guarded notebook
allocation disables internal retries, failure projection is what ``invoke()``
produced for handler rows, and the ``dispatched`` marker reaches the neutral
error.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from notebooklm._deadline import RuntimeDeadline
from notebooklm._notebook_payloads import (
    build_create_notebook_params,
    build_get_notebook_params,
    build_update_notebook_params,
)
from notebooklm._semantic.backend import (
    BackendDeadlineExceededError,
    BackendError,
    BackendErrorReason,
    may_have_committed,
)
from notebooklm._semantic.binding import CodecBinding, CodecPayload, DeadlineMode
from notebooklm._semantic.operations import CallPolicy, Operation
from notebooklm._semantic.records import (
    NOTEBOOK_ALLOCATE_DEF,
    NOTEBOOK_DELETE_DEF,
    NOTEBOOK_DESCRIBE_DEF,
    NOTEBOOK_GET_DEF,
    NOTEBOOK_LIST_DEF,
    NOTEBOOK_PATCH_DEF,
    NOTEBOOK_REMOVE_RECENT_DEF,
    NOTEBOOK_SUMMARIZE_DEF,
    NotebookAllocateInput,
    NotebookDeleteInput,
    NotebookDeleteResult,
    NotebookGetInput,
    NotebookGetResult,
    NotebookGuideInput,
    NotebookListInput,
    NotebookPatchInput,
    NotebookPatchResult,
    NotebookRemoveRecentInput,
    NotebookRemoveRecentResult,
)
from notebooklm._web.backend import WebRpcBackend
from notebooklm._web.bindings import WEB_BINDING_ROWS
from notebooklm._web.bindings import notebooks as notebook_rows
from notebooklm._web.codec import notebooks as notebooks_codec
from notebooklm._web.registry import WEB_OPERATION_REGISTRY, WEB_SERVICE_OWNED_OPERATIONS
from notebooklm.exceptions import ClientError, DecodingError, RPCError, RPCTimeoutError, ServerError
from notebooklm.rpc import RPCMethod
from notebooklm.rpc.encoder import build_request_body, encode_rpc_request
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
    Operation.NOTEBOOK_ALLOCATE: notebook_rows.NOTEBOOK_ALLOCATE,
    Operation.NOTEBOOK_PATCH: notebook_rows.NOTEBOOK_PATCH,
    Operation.NOTEBOOK_DELETE: notebook_rows.NOTEBOOK_DELETE,
    Operation.NOTEBOOK_REMOVE_RECENT: notebook_rows.NOTEBOOK_REMOVE_RECENT,
    Operation.NOTEBOOK_SUMMARIZE: notebook_rows.NOTEBOOK_SUMMARIZE,
    Operation.NOTEBOOK_DESCRIBE: notebook_rows.NOTEBOOK_DESCRIBE,
}

_EXPECTED_NATIVES = {
    Operation.NOTEBOOK_LIST: RPCMethod.LIST_NOTEBOOKS,
    Operation.NOTEBOOK_GET: RPCMethod.GET_NOTEBOOK,
    Operation.NOTEBOOK_ALLOCATE: RPCMethod.CREATE_NOTEBOOK,
    Operation.NOTEBOOK_PATCH: RPCMethod.RENAME_NOTEBOOK,
    Operation.NOTEBOOK_DELETE: RPCMethod.DELETE_NOTEBOOK,
    Operation.NOTEBOOK_REMOVE_RECENT: RPCMethod.REMOVE_RECENTLY_VIEWED,
    Operation.NOTEBOOK_SUMMARIZE: RPCMethod.SUMMARIZE,
    Operation.NOTEBOOK_DESCRIBE: RPCMethod.SUMMARIZE,
}


# --- registry partition ------------------------------------------------------


def test_notebook_rows_replace_their_handlers_and_composites_stay() -> None:
    assert {op: WEB_BINDING_ROWS[op] for op in _CONVERTED} == _CONVERTED
    assert dict(notebook_rows.NOTEBOOK_ROWS) == _CONVERTED
    for operation, row in _CONVERTED.items():
        binding = WEB_OPERATION_REGISTRY[operation]
        assert binding.is_supported
        assert binding.row is row
        assert isinstance(row, CodecBinding)
        assert row.definition is binding.definition
        assert row.deadline is DeadlineMode.INHERIT
        assert row.native.is_constant
        choice = row.native.select(None)
        assert (choice.method, choice.variant) == (_EXPECTED_NATIVES[operation], None)
        assert row.forward_disable_internal_retries is (operation is Operation.NOTEBOOK_ALLOCATE)
        mapped = operation in {
            Operation.NOTEBOOK_GET,
            Operation.NOTEBOOK_ALLOCATE,
        }
        assert (row.map_error is not None) is mapped
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
    for operation in (Operation.NOTEBOOK_CREATE, Operation.NOTEBOOK_UPDATE):
        workflow = WEB_OPERATION_REGISTRY[operation]
        assert workflow.service_owned is True
        assert workflow.row is None
        assert operation in WEB_SERVICE_OWNED_OPERATIONS
    for name in ("_notebook_create", "_notebook_update", "_list_notebooks"):
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
    assert notebooks_codec.encode_notebook_patch(
        NotebookPatchInput("nb_123", title="Renamed", emoji="📖")
    ) == CodecPayload(
        params=build_update_notebook_params("nb_123", title="Renamed", emoji="📖"),
        source_path="/",
        allow_null=True,
    )
    assert notebooks_codec.encode_notebook_allocate(
        NotebookAllocateInput("Daily News")
    ) == CodecPayload(
        params=build_create_notebook_params("Daily News"),
        source_path="/",
    )
    assert notebooks_codec.encode_notebook_delete(NotebookDeleteInput("nb_123")) == CodecPayload(
        params=[["nb_123"], [2]], source_path="/"
    )
    recent = notebooks_codec.encode_notebook_remove_recent(NotebookRemoveRecentInput("nb_123"))
    assert recent == CodecPayload(params=["nb_123"], source_path="/", allow_null=True)
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


def test_raw_notebook_get_request_is_byte_identical_to_the_retired_raw_call() -> None:
    """R6.2: routing ``notebooks.get_raw`` through the row changes no request byte.

    The retired facade helper issued
    ``rpc_call(GET_NOTEBOOK, build_get_notebook_params(id),
    source_path=f"/notebook/{id}")`` and left every other option at its
    ``rpc_call`` default. The one default that differs from the row's is
    ``raise_on_null_status``, and it is unreachable here: it only applies to a
    null result an ``allow_null=True`` caller would otherwise tolerate, and
    both call shapes send ``allow_null=False``. The integration cassettes match
    on the decoded ``f.req`` form body, so this equality is what keeps
    ``notebooks_get_raw.yaml`` replaying.
    """
    retired = CodecPayload(
        params=build_get_notebook_params("nb_123"),
        source_path="/notebook/nb_123",
    )
    raw = notebooks_codec.encode_notebook_get(
        NotebookGetInput("nb_123", include_notebook=False, include_raw=True)
    )

    assert raw == retired
    # The decoded branches share the request; only the decode differs.
    assert raw == notebooks_codec.encode_notebook_get(NotebookGetInput("nb_123"))
    assert build_request_body(
        encode_rpc_request(RPCMethod.GET_NOTEBOOK, raw.params), csrf_token="tok"
    ) == build_request_body(
        encode_rpc_request(RPCMethod.GET_NOTEBOOK, retired.params), csrf_token="tok"
    )


def test_raw_notebook_get_decoder_returns_the_payload_untouched() -> None:
    """The raw branch runs no positional decode, so any shape survives."""
    value = NotebookGetInput("nb_123", include_notebook=False, include_raw=True)

    assert notebooks_codec.decode_notebook_get(value, _GET_RESPONSE) == NotebookGetResult(
        notebook=None, source_ids=(), raw=_GET_RESPONSE
    )
    # Shapes the decoded branches reject or normalise still come back verbatim.
    for payload in (None, [], "not-a-row", [["", [], ""]]):
        assert notebooks_codec.decode_notebook_get(value, payload).raw == payload


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


@pytest.mark.parametrize("raw", [None, [], [None], [["", [], ""]]])
def test_required_notebook_get_decoder_raises_neutral_leaf_miss(raw: Any) -> None:
    value = NotebookGetInput("nb_123", require_notebook=True)
    with pytest.raises(BackendError) as caught:
        notebooks_codec.decode_notebook_get(value, raw)
    assert caught.value.operation is Operation.NOTEBOOK_GET
    assert caught.value.reason is BackendErrorReason.NOT_FOUND
    assert dict(caught.value.diagnostics or {}) == {
        "notebook_id": "nb_123",
        "method_id": RPCMethod.GET_NOTEBOOK.value,
    }


def test_notebook_patch_decoder_accepts_null_success() -> None:
    assert (
        notebooks_codec.decode_notebook_patch(NotebookPatchInput("nb_123", title="Renamed"), None)
        == NotebookPatchResult()
    )


def test_notebook_allocate_decoder_returns_the_created_record() -> None:
    raw = ["New", [], "nb_new", None, None, [1]]
    result = notebooks_codec.decode_notebook_allocate(NotebookAllocateInput("New"), raw)
    assert (result.notebook.id, result.notebook.title) == ("nb_new", "New")


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
        _LIST_RESPONSE, _GET_RESPONSE, None, None, None, _GUIDE_RESPONSE, _GUIDE_RESPONSE
    )
    backend = build_web_backend(executor)

    listed = await backend.invoke(NOTEBOOK_LIST_DEF, NotebookListInput(), deadline=None)
    got = await backend.invoke(NOTEBOOK_GET_DEF, NotebookGetInput("nb_123"), deadline=None)
    patched = await backend.invoke(
        NOTEBOOK_PATCH_DEF,
        NotebookPatchInput("nb_123", title="Renamed", emoji="📖"),
        deadline=None,
    )
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
    assert patched == NotebookPatchResult()
    assert deleted == NotebookDeleteResult()
    assert removed == NotebookRemoveRecentResult()
    assert summary.description.summary == "A summary"
    assert description.description.summary == "A summary"

    list_call, get_call, patch_call, delete_call, recent_call, summarize_call, describe_call = (
        executor.calls
    )
    assert list_call.method is RPCMethod.LIST_NOTEBOOKS
    assert list_call.params == [None, 1, None, [2]]
    assert list_call.kwargs == {**_BASE_KWARGS, "source_path": "/"}
    assert get_call.method is RPCMethod.GET_NOTEBOOK
    assert get_call.params == build_get_notebook_params("nb_123")
    assert get_call.kwargs == {**_BASE_KWARGS, "source_path": "/notebook/nb_123"}
    assert patch_call.method is RPCMethod.RENAME_NOTEBOOK
    assert patch_call.params == build_update_notebook_params("nb_123", title="Renamed", emoji="📖")
    assert patch_call.kwargs == {**_BASE_KWARGS, "source_path": "/", "allow_null": True}
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
async def test_notebook_allocate_is_one_guarded_call_with_byte_identical_kwargs() -> None:
    created_row: list[Any] = ["New", [], "nb_new", None, None, [1]]
    executor = _RecordingExecutor(created_row)
    backend = build_web_backend(executor)

    result = await backend.invoke(
        NOTEBOOK_ALLOCATE_DEF,
        NotebookAllocateInput("New"),
        deadline=None,
    )

    assert result.notebook.id == "nb_new"
    (create,) = executor.calls
    assert create.method is RPCMethod.CREATE_NOTEBOOK
    assert create.params == build_create_notebook_params("New")
    assert create.kwargs == {
        **_BASE_KWARGS,
        "source_path": "/",
        "disable_internal_retries": True,
    }


@pytest.mark.asyncio
async def test_allocate_maps_only_guarded_code_three_to_quota_rejection() -> None:
    original = RPCError(
        "invalid argument",
        method_id=RPCMethod.CREATE_NOTEBOOK.value,
        rpc_code=3,
    )
    backend = build_web_backend(_RecordingExecutor(original))

    with pytest.raises(BackendError) as caught:
        await backend.invoke(
            NOTEBOOK_ALLOCATE_DEF,
            NotebookAllocateInput("New"),
            deadline=None,
        )

    error = caught.value
    assert error.operation is Operation.NOTEBOOK_ALLOCATE
    assert error.reason is BackendErrorReason.RPC
    assert error.__cause__ is original
    assert error.diagnostics is not None
    assert error.diagnostics["quota_rejection"] is True
    assert error.diagnostics["method_id"] == RPCMethod.CREATE_NOTEBOOK.value
    assert error.diagnostics["rpc_code"] == 3


@pytest.mark.parametrize(
    "original",
    [
        RPCError("other code", method_id=RPCMethod.CREATE_NOTEBOOK.value, rpc_code=13),
        RPCError("other method", method_id=RPCMethod.GET_NOTEBOOK.value, rpc_code=3),
    ],
)
@pytest.mark.asyncio
async def test_allocate_does_not_tag_non_quota_rpc_rejections(original: RPCError) -> None:
    backend = build_web_backend(_RecordingExecutor(original))
    with pytest.raises(BackendError) as caught:
        await backend.invoke(
            NOTEBOOK_ALLOCATE_DEF,
            NotebookAllocateInput("New"),
            deadline=None,
        )
    assert caught.value.reason is BackendErrorReason.RPC
    assert "quota_rejection" not in (caught.value.diagnostics or {})


@pytest.mark.asyncio
async def test_allocate_server_error_is_dispatched_and_commit_uncertain() -> None:
    original = ServerError("boom", method_id=RPCMethod.CREATE_NOTEBOOK.value)
    backend = build_web_backend(_RecordingExecutor(original))
    with pytest.raises(BackendError) as caught:
        await backend.invoke(
            NOTEBOOK_ALLOCATE_DEF,
            NotebookAllocateInput("New"),
            deadline=None,
        )
    assert caught.value.operation is Operation.NOTEBOOK_ALLOCATE
    assert caught.value.reason is BackendErrorReason.SERVER
    assert caught.value.dispatched is True
    assert caught.value.outcome_unknown is False
    assert may_have_committed(caught.value) is True
    assert caught.value.__cause__ is original


@pytest.mark.asyncio
async def test_allocate_timeout_after_dispatch_uses_mutation_uncertainty() -> None:
    clock = [11.0]
    original = RPCTimeoutError("slow", method_id=RPCMethod.CREATE_NOTEBOOK.value)
    executor = _RecordingExecutor(original)
    backend = build_web_backend(executor)
    deadline = RuntimeDeadline(timeout=5.0, started_at=10.0, monotonic=lambda: clock[0])

    async def rpc_call(method: RPCMethod, params: list[Any], **kwargs: Any) -> Any:
        clock[0] = 16.0
        return await _RecordingExecutor.rpc_call(executor, method, params, **kwargs)

    backend._runtime = type("Runtime", (), {"rpc_call": staticmethod(rpc_call)})()  # type: ignore[assignment]
    with pytest.raises(BackendDeadlineExceededError) as caught:
        await backend.invoke(
            NOTEBOOK_ALLOCATE_DEF,
            NotebookAllocateInput("New"),
            deadline=deadline,
        )
    assert caught.value.operation is Operation.NOTEBOOK_ALLOCATE
    assert caught.value.dispatched is True
    assert caught.value.outcome_unknown is True
    assert may_have_committed(caught.value) is True


@pytest.mark.asyncio
async def test_allocate_predispatch_expiry_never_enters_the_runtime() -> None:
    executor = _RecordingExecutor()
    backend = build_web_backend(executor)
    deadline = RuntimeDeadline(timeout=5.0, started_at=10.0, monotonic=lambda: 16.0)
    with pytest.raises(BackendDeadlineExceededError) as caught:
        await backend.invoke(
            NOTEBOOK_ALLOCATE_DEF,
            NotebookAllocateInput("New"),
            deadline=deadline,
        )
    assert executor.calls == []
    assert caught.value.operation is Operation.NOTEBOOK_ALLOCATE
    assert caught.value.dispatched is False
    assert caught.value.outcome_unknown is False


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
async def test_required_get_maps_only_status_five_to_neutral_not_found() -> None:
    original = ClientError(
        "not found",
        status_code=404,
        method_id=RPCMethod.GET_NOTEBOOK.value,
        raw_response="scrubbed response",
        rpc_code=5,
    )
    backend = build_web_backend(_RecordingExecutor(original))

    with pytest.raises(BackendError) as caught:
        await backend.invoke(
            NOTEBOOK_GET_DEF,
            NotebookGetInput("nb_123", require_notebook=True),
            deadline=None,
        )

    error = caught.value
    assert error.reason is BackendErrorReason.NOT_FOUND
    assert error.operation is Operation.NOTEBOOK_GET
    assert error.__cause__ is original
    assert error.diagnostics is not None
    assert error.diagnostics["status_code"] == 404
    assert error.diagnostics["rpc_code"] == 5
    assert error.diagnostics["raw_response"] == "scrubbed response"
    assert error.diagnostics["original_message"] == "not found"


@pytest.mark.asyncio
async def test_ordinary_get_keeps_status_five_on_the_legacy_client_error_path() -> None:
    original = ClientError(
        "not found",
        status_code=404,
        method_id=RPCMethod.GET_NOTEBOOK.value,
        rpc_code=5,
    )
    backend = build_web_backend(_RecordingExecutor(original))

    with pytest.raises(BackendError) as caught:
        await backend.invoke(
            NOTEBOOK_GET_DEF,
            NotebookGetInput("nb_123"),
            deadline=None,
        )

    assert caught.value.reason is BackendErrorReason.CLIENT
    assert caught.value.__cause__ is original


@pytest.mark.asyncio
async def test_patch_server_error_is_dispatched_without_inventing_outcome_unknown() -> None:
    original = ServerError("boom", method_id=RPCMethod.RENAME_NOTEBOOK.value)
    backend = build_web_backend(_RecordingExecutor(original))

    with pytest.raises(BackendError) as caught:
        await backend.invoke(
            NOTEBOOK_PATCH_DEF,
            NotebookPatchInput("nb_123", title="Renamed"),
            deadline=None,
        )

    assert caught.value.operation is Operation.NOTEBOOK_PATCH
    assert caught.value.reason is BackendErrorReason.SERVER
    assert caught.value.dispatched is True
    assert caught.value.outcome_unknown is False
    assert may_have_committed(caught.value) is True
    assert caught.value.__cause__ is original


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
