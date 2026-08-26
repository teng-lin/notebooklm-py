"""P9.3 sources: the eight source leaves dispatch as codec rows exactly as the handlers did.

``SOURCE_LIST``/``SOURCE_GET``/``SOURCE_WAIT`` (the recency-writing snapshot),
``SOURCE_DELETE``/``SOURCE_REFRESH``/``SOURCE_CHECK_FRESHNESS``/``SOURCE_GET_GUIDE``/
``SOURCE_GET_FULLTEXT`` are ``encode → one native call → decode`` rows in
``_web/bindings/sources.py``.  These tests pin the conversion oracles: the
identical keyword set reaches the runtime (route, ``allow_null``, explicit
``False``/``None`` values), ``SOURCE_GET`` selects by exact id inside ``decode``,
``SOURCE_WAIT`` ignores the caller's deadline as the handler did, the fulltext
not-found identity is unchanged, failure projection is what ``invoke()``
produced for handler rows, and the source-add composites remain custom rows.
``SOURCE_UPDATE`` is service-owned and hydrates through the ``SOURCE_GET`` row.
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
from notebooklm._binding import CodecBinding, CodecPayload, CustomBinding, DeadlineMode
from notebooklm._deadline import RuntimeDeadline
from notebooklm._operations import Operation
from notebooklm._records import (
    SOURCE_CHECK_FRESHNESS_DEF,
    SOURCE_DELETE_DEF,
    SOURCE_GET_DEF,
    SOURCE_GET_FULLTEXT_DEF,
    SOURCE_GET_GUIDE_DEF,
    SOURCE_LIST_DEF,
    SOURCE_REFRESH_DEF,
    SOURCE_WAIT_DEF,
    SourceDeleteInput,
    SourceDeleteResult,
    SourceFreshnessInput,
    SourceFulltextInput,
    SourceGetInput,
    SourceGuideInput,
    SourceListInput,
    SourceRefreshInput,
    SourceRefreshResult,
    SourceWaitSnapshotInput,
)
from notebooklm._source_service import SourceService
from notebooklm._web.backend import WebRpcBackend
from notebooklm._web.bindings import WEB_BINDING_ROWS
from notebooklm._web.bindings import sources as source_rows
from notebooklm._web.codec import sources as sources_codec
from notebooklm._web.registry import WEB_OPERATION_REGISTRY
from notebooklm.exceptions import RPCTimeoutError, ServerError
from notebooklm.rpc import RPCMethod
from tests._fixtures.web_backend import build_web_backend

_NB = "nb_1"
_ROUTE = "/notebook/nb_1"
_SNAPSHOT_PARAMS: list[Any] = [
    _NB,
    None,
    [2, None, None, [1, None, None, None, None, None, None, None, None, None, [1]]],
    None,
    0,
]


def _source_entry(
    source_id: str,
    *,
    title: str | None = None,
    url: str = "https://example.com",
    status: int = 1,
    kind: int = 5,
) -> list[Any]:
    return [
        [source_id],
        title or f"Source {source_id}",
        [None, 11, [1704067200, 0], None, kind, None, None, [url]],
        [None, status],
    ]


def _snapshot(*rows: list[Any]) -> list[Any]:
    return [["Notebook", list(rows), _NB]]


_ROWS = [_source_entry("src-web"), _source_entry("src-pdf", status=2, kind=3)]

_BASE_KWARGS = {
    "allow_null": False,
    "_is_retry": False,
    "disable_internal_retries": False,
    "operation_variant": None,
    "read_timeout": None,
    "raise_on_null_status": False,
    "_retry_deadline": None,
    "source_path": _ROUTE,
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


# --- registry partition ------------------------------------------------------


def test_source_leaves_are_rows_and_composites_stay_handlers() -> None:
    converted = {
        Operation.SOURCE_LIST: source_rows.SOURCE_LIST,
        Operation.SOURCE_GET: source_rows.SOURCE_GET,
        Operation.SOURCE_WAIT: source_rows.SOURCE_WAIT,
        Operation.SOURCE_DELETE: source_rows.SOURCE_DELETE,
        Operation.SOURCE_REFRESH: source_rows.SOURCE_REFRESH,
        Operation.SOURCE_CHECK_FRESHNESS: source_rows.SOURCE_CHECK_FRESHNESS,
        Operation.SOURCE_GET_GUIDE: source_rows.SOURCE_GET_GUIDE,
        Operation.SOURCE_GET_FULLTEXT: source_rows.SOURCE_GET_FULLTEXT,
    }
    # The codec leaves; the source-add family are custom rows in the same table (P9.4b).
    assert {
        operation: row
        for operation, row in source_rows.SOURCE_ROWS.items()
        if isinstance(row, CodecBinding)
    } == converted
    for operation, row in converted.items():
        assert WEB_BINDING_ROWS[operation] is row
        binding = WEB_OPERATION_REGISTRY[operation]
        assert binding.is_supported
        assert binding.row is row
        assert isinstance(row, CodecBinding)
        assert row.definition is binding.definition
        assert row.native.is_constant
        assert row.forward_disable_internal_retries is False
        assert row.map_error is None
    assert source_rows.SOURCE_WAIT.deadline is DeadlineMode.IGNORE
    assert all(
        row.deadline is DeadlineMode.INHERIT
        for operation, row in converted.items()
        if operation is not Operation.SOURCE_WAIT
    )
    natives = {operation: row.native.select(None).method for operation, row in converted.items()}
    assert natives == {
        Operation.SOURCE_LIST: RPCMethod.GET_NOTEBOOK,
        Operation.SOURCE_GET: RPCMethod.GET_NOTEBOOK,
        Operation.SOURCE_WAIT: RPCMethod.GET_NOTEBOOK,
        Operation.SOURCE_DELETE: RPCMethod.DELETE_SOURCE,
        Operation.SOURCE_REFRESH: RPCMethod.REFRESH_SOURCE,
        Operation.SOURCE_CHECK_FRESHNESS: RPCMethod.CHECK_SOURCE_FRESHNESS,
        Operation.SOURCE_GET_GUIDE: RPCMethod.GET_SOURCE_GUIDE,
        Operation.SOURCE_GET_FULLTEXT: RPCMethod.GET_SOURCE,
    }
    for name in (
        "_source_list",
        "_source_get",
        "_source_wait",
        "_source_delete",
        "_source_refresh",
        "_source_check_freshness",
        "_source_get_guide",
        "_source_get_fulltext",
    ):
        assert not hasattr(WebRpcBackend, name)
    # P9.4b: the remaining source-add family are custom rows in the same module.
    for operation in (
        Operation.SOURCE_ADD_URL_BATCH,
        Operation.SOURCE_ADD_DRIVE,
        Operation.SOURCE_ADD_FILE,
    ):
        binding = WEB_OPERATION_REGISTRY[operation]
        assert isinstance(binding.row, CustomBinding)
    # P9.2-4 / P10 R3.2 / P10 R3.3: these are sequenced above the port instead.
    for service_owned in (
        Operation.SOURCE_UPDATE,
        Operation.SOURCE_ADD_TEXT,
        Operation.SOURCE_ADD_URL,
    ):
        assert WEB_OPERATION_REGISTRY[service_owned].service_owned is True
        assert WEB_OPERATION_REGISTRY[service_owned].row is None
    backend = build_web_backend(_RecordingExecutor())
    for operation, row in converted.items():
        assert backend._bindings[operation] is row


# --- payload goldens -----------------------------------------------------------


@pytest.mark.parametrize(
    ("encoder", "value"),
    [
        (sources_codec.encode_source_list, SourceListInput(_NB)),
        (sources_codec.encode_source_get, SourceGetInput(_NB, "src-pdf")),
        (sources_codec.encode_source_wait, SourceWaitSnapshotInput(_NB)),
    ],
)
def test_snapshot_payload_golden(encoder: Any, value: Any) -> None:
    assert encoder(value) == CodecPayload(params=_SNAPSHOT_PARAMS, source_path=_ROUTE)


def test_single_native_payload_goldens() -> None:
    assert sources_codec.encode_source_delete(SourceDeleteInput(_NB, "s1")) == CodecPayload(
        params=[[["s1"]]], source_path=_ROUTE, allow_null=True
    )
    assert sources_codec.encode_source_refresh(SourceRefreshInput(_NB, "s1")) == CodecPayload(
        params=[None, ["s1"], [2]], source_path=_ROUTE, allow_null=True
    )
    assert sources_codec.encode_source_check_freshness(
        SourceFreshnessInput(_NB, "s1")
    ) == CodecPayload(params=[None, ["s1"], [2]], source_path=_ROUTE, allow_null=True)
    assert sources_codec.encode_source_get_guide(SourceGuideInput(_NB, "s1")) == CodecPayload(
        params=[[[["s1"]]]], source_path=_ROUTE, allow_null=True
    )
    assert sources_codec.encode_source_get_fulltext(
        SourceFulltextInput(_NB, "s1", output_format="text")
    ) == CodecPayload(params=[["s1"], [2], [2]], source_path=_ROUTE, allow_null=True)
    assert sources_codec.encode_source_get_fulltext(
        SourceFulltextInput(_NB, "s1", output_format="markdown")
    ) == CodecPayload(params=[["s1"], [3], [3]], source_path=_ROUTE, allow_null=True)


def test_fulltext_encoder_validates_the_format_before_any_wire_call() -> None:
    with pytest.raises(ValueError, match="Invalid format: 'html'"):
        sources_codec.encode_source_get_fulltext(
            SourceFulltextInput(_NB, "s1", output_format="html")
        )


# --- dispatch oracles ------------------------------------------------------------


@pytest.mark.asyncio
async def test_snapshot_rows_forward_the_identical_keyword_set_and_decode() -> None:
    executor = _RecordingExecutor(_snapshot(*_ROWS), _snapshot(*_ROWS), _snapshot(*_ROWS))
    backend = build_web_backend(executor)

    listed = await backend.invoke(
        SOURCE_LIST_DEF,
        SourceListInput(_NB, statuses=frozenset({"processing"}), kinds=frozenset({"web_page"})),
        deadline=None,
    )
    fetched = await backend.invoke(SOURCE_GET_DEF, SourceGetInput(_NB, "src-pdf"), deadline=None)
    waited = await backend.invoke(SOURCE_WAIT_DEF, SourceWaitSnapshotInput(_NB), deadline=None)

    assert [(item.id, item.status, item.kind) for item in listed.sources] == [
        ("src-web", "processing", "web_page")
    ]
    assert fetched.source is not None
    assert fetched.source.id == "src-pdf"
    assert fetched.source.kind == "pdf"
    assert [item.id for item in waited.sources] == ["src-web", "src-pdf"]
    assert len(executor.calls) == 3
    for call in executor.calls:
        assert call.method is RPCMethod.GET_NOTEBOOK
        assert call.params == _SNAPSHOT_PARAMS
        assert call.kwargs == _BASE_KWARGS


@pytest.mark.asyncio
async def test_source_get_filters_in_decode_and_yields_none_for_a_missing_id() -> None:
    executor = _RecordingExecutor(_snapshot(*_ROWS))
    backend = build_web_backend(executor)

    fetched = await backend.invoke(SOURCE_GET_DEF, SourceGetInput(_NB, "absent"), deadline=None)

    assert fetched.source is None
    (call,) = executor.calls
    assert call.method is RPCMethod.GET_NOTEBOOK


@pytest.mark.asyncio
async def test_single_native_rows_forward_allow_null_and_decode() -> None:
    executor = _RecordingExecutor(None, None, [], [])
    backend = build_web_backend(executor)

    deleted = await backend.invoke(SOURCE_DELETE_DEF, SourceDeleteInput(_NB, "s1"), deadline=None)
    refreshed = await backend.invoke(
        SOURCE_REFRESH_DEF, SourceRefreshInput(_NB, "s1"), deadline=None
    )
    fresh = await backend.invoke(
        SOURCE_CHECK_FRESHNESS_DEF, SourceFreshnessInput(_NB, "s1"), deadline=None
    )
    guide = await backend.invoke(SOURCE_GET_GUIDE_DEF, SourceGuideInput(_NB, "s1"), deadline=None)

    assert deleted == SourceDeleteResult()
    assert refreshed == SourceRefreshResult()
    assert fresh.fresh is True
    assert guide.guide.summary == ""
    assert guide.guide.keywords == ()
    methods = [call.method for call in executor.calls]
    assert methods == [
        RPCMethod.DELETE_SOURCE,
        RPCMethod.REFRESH_SOURCE,
        RPCMethod.CHECK_SOURCE_FRESHNESS,
        RPCMethod.GET_SOURCE_GUIDE,
    ]
    assert [call.params for call in executor.calls] == [
        [[["s1"]]],
        [None, ["s1"], [2]],
        [None, ["s1"], [2]],
        [[[["s1"]]]],
    ]
    for call in executor.calls:
        assert call.kwargs == {**_BASE_KWARGS, "allow_null": True}


@pytest.mark.asyncio
async def test_fulltext_row_keeps_the_legacy_not_found_identity() -> None:
    executor = _RecordingExecutor([])
    backend = build_web_backend(executor)

    with pytest.raises(BackendError) as caught:
        await backend.invoke(SOURCE_GET_FULLTEXT_DEF, SourceFulltextInput(_NB, "s1"), deadline=None)

    error = caught.value
    assert type(error) is BackendError
    assert error.operation is Operation.SOURCE_GET_FULLTEXT
    assert error.reason is BackendErrorReason.SOURCE_NOT_FOUND
    assert error.message == "Source not found: Source s1 not found in notebook nb_1"
    assert error.diagnostics == {
        "source_id": "Source s1 not found in notebook nb_1",
        "method_id": None,
        "raw_response": None,
    }
    assert error.dispatched is False
    (call,) = executor.calls
    assert call.method is RPCMethod.GET_SOURCE
    assert call.kwargs == {**_BASE_KWARGS, "allow_null": True}


@pytest.mark.asyncio
async def test_source_wait_ignores_the_caller_deadline_like_the_handler_did() -> None:
    executor = _RecordingExecutor(_snapshot(*_ROWS))
    backend = build_web_backend(executor)
    deadline = RuntimeDeadline(timeout=5.0, started_at=10.0, monotonic=lambda: 11.0)

    await backend.invoke(SOURCE_WAIT_DEF, SourceWaitSnapshotInput(_NB), deadline=deadline)

    (call,) = executor.calls
    assert call.kwargs["read_timeout"] is None
    assert call.kwargs["_retry_deadline"] is None


@pytest.mark.asyncio
async def test_source_wait_dispatches_after_expiry_because_its_row_ignores_the_deadline() -> None:
    """The one ``DeadlineMode.IGNORE`` row never meets the pre-dispatch expiry check."""
    executor = _RecordingExecutor(_snapshot(*_ROWS))
    backend = build_web_backend(executor)
    deadline = RuntimeDeadline(timeout=5.0, started_at=10.0, monotonic=lambda: 16.0)

    waited = await backend.invoke(SOURCE_WAIT_DEF, SourceWaitSnapshotInput(_NB), deadline=deadline)

    assert [item.id for item in waited.sources] == ["src-web", "src-pdf"]
    (call,) = executor.calls
    assert call.method is RPCMethod.GET_NOTEBOOK
    assert call.kwargs == _BASE_KWARGS


@pytest.mark.asyncio
async def test_inheriting_rows_clamp_read_timeout_to_the_shared_deadline() -> None:
    executor = _RecordingExecutor(_snapshot(*_ROWS))
    backend = build_web_backend(executor)
    deadline = RuntimeDeadline(timeout=5.0, started_at=10.0, monotonic=lambda: 11.0)

    await backend.invoke(SOURCE_LIST_DEF, SourceListInput(_NB), deadline=deadline)

    (call,) = executor.calls
    assert call.kwargs["read_timeout"] == pytest.approx(4.0)
    assert call.kwargs["_retry_deadline"] is deadline


@pytest.mark.asyncio
async def test_source_update_hydration_reads_through_the_source_get_row() -> None:
    executor = _RecordingExecutor(None, _snapshot(*_ROWS))
    backend = build_web_backend(executor)

    result = await SourceService(backend).update(_NB, "src-pdf", "Renamed", return_object=True)

    assert result.source is not None
    assert result.source.id == "src-pdf"
    update, readback = executor.calls
    assert update.method is RPCMethod.UPDATE_SOURCE
    assert update.kwargs["allow_null"] is True
    assert readback.method is RPCMethod.GET_NOTEBOOK
    assert readback.params == _SNAPSHOT_PARAMS
    assert readback.kwargs == _BASE_KWARGS


@pytest.mark.asyncio
async def test_codec_row_server_error_translates_like_a_handler_and_is_dispatched() -> None:
    executor = _RecordingExecutor(ServerError("boom", method_id=RPCMethod.DELETE_SOURCE.value))
    backend = build_web_backend(executor)

    with pytest.raises(BackendError) as caught:
        await backend.invoke(SOURCE_DELETE_DEF, SourceDeleteInput(_NB, "s1"), deadline=None)

    error = caught.value
    assert type(error) is BackendError
    assert error.operation is Operation.SOURCE_DELETE
    assert error.reason is BackendErrorReason.SERVER
    assert error.message == "boom"
    assert error.outcome_unknown is False
    assert error.diagnostics is not None
    assert error.diagnostics["method_id"] == RPCMethod.DELETE_SOURCE.value
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
        await backend.invoke(SOURCE_LIST_DEF, SourceListInput(_NB), deadline=deadline)

    error = caught.value
    assert error.operation is Operation.SOURCE_LIST
    assert error.reason is BackendErrorReason.TIMEOUT
    # ``source.list`` is a MUTATION-policy read (recency side effect), so the
    # post-dispatch expiry is projected exactly as the handler's was.
    assert error.outcome_unknown is True
    assert error.dispatched is True
    assert may_have_committed(error) is True
    assert error.diagnostics is not None
    assert error.diagnostics["timeout"] == 5.0
    assert error.diagnostics["method_id"] == RPCMethod.GET_NOTEBOOK.value
    assert isinstance(error.__cause__, RPCTimeoutError)


@pytest.mark.asyncio
async def test_codec_row_pre_dispatch_expiry_is_not_dispatched() -> None:
    executor = _RecordingExecutor()
    backend = build_web_backend(executor)
    deadline = RuntimeDeadline(timeout=5.0, started_at=10.0, monotonic=lambda: 16.0)

    with pytest.raises(BackendDeadlineExceededError) as caught:
        await backend.invoke(SOURCE_DELETE_DEF, SourceDeleteInput(_NB, "s1"), deadline=deadline)

    assert executor.calls == []
    assert caught.value.outcome_unknown is False
    assert caught.value.dispatched is False
    assert may_have_committed(caught.value) is False
