"""P9.3/P9.2 Studio leaves dispatch as codec rows exactly as their handlers did.

``ARTIFACT_DOWNLOAD`` is the input-keyed row (one of three natives chosen from
``value.action``); ``ARTIFACT_WAIT`` inherits the caller's deadline; the other
the remaining rows are constant. These tests pin the conversion oracles: the identical
keyword set reaches the runtime (including explicit ``False``/``None`` values,
``allow_null`` and ``raise_on_null_status``), the payload builders are
unchanged, the closed ``ARTIFACT_FEATURE_UNAVAILABLE`` error keeps its shape,
failure projection is what ``invoke()`` produced for handler rows, and the
``dispatched`` marker reaches the neutral ``BackendError``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from notebooklm._backend import (
    BackendContractError,
    BackendDeadlineExceededError,
    BackendError,
    BackendErrorReason,
    may_have_committed,
)
from notebooklm._binding import CodecBinding, DeadlineMode, NativeChoice
from notebooklm._deadline import RuntimeDeadline
from notebooklm._operations import Operation
from notebooklm._records import (
    ARTIFACT_CATALOG_DEF,
    ARTIFACT_DELETE_DEF,
    ARTIFACT_DOWNLOAD_DEF,
    ARTIFACT_EXPORT_DEF,
    ARTIFACT_PATCH_TITLE_DEF,
    ARTIFACT_RETRY_DEF,
    ARTIFACT_REVISE_SLIDE_DEF,
    ARTIFACT_WAIT_DEF,
    ArtifactCatalogInput,
    ArtifactCatalogResult,
    ArtifactDeleteInput,
    ArtifactDeleteResult,
    ArtifactDownloadInput,
    ArtifactPatchTitleInput,
    ArtifactPatchTitleResult,
    ArtifactPollInput,
    ArtifactRetryInput,
    ArtifactReviseSlideInput,
    DriveExportInput,
)
from notebooklm._web.backend import WebRpcBackend
from notebooklm._web.bindings import WEB_BINDING_ROWS
from notebooklm._web.bindings import studio as studio_rows
from notebooklm._web.codec.artifact_payloads import (
    build_retry_artifact_params,
    build_revise_slide_params,
)
from notebooklm._web.registry import WEB_OPERATION_REGISTRY
from notebooklm.exceptions import RPCTimeoutError, ServerError
from notebooklm.rpc import ARTIFACT_STATUS_SUGGESTED_WIRE_NAME, RPCMethod
from tests._fixtures.web_backend import build_web_backend

_CATALOG_PARAMS = [[2], "nb", f'NOT artifact.status = "{ARTIFACT_STATUS_SUGGESTED_WIRE_NAME}"']
_BASE_KWARGS = {
    "source_path": "/notebook/nb",
    "allow_null": True,
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


# --- registry partition ------------------------------------------------------


def test_studio_leaves_are_rows_and_rename_is_service_owned() -> None:
    converted = {
        Operation.ARTIFACT_CATALOG: studio_rows.ARTIFACT_CATALOG,
        Operation.ARTIFACT_EXPORT: studio_rows.ARTIFACT_EXPORT,
        Operation.ARTIFACT_REVISE_SLIDE: studio_rows.ARTIFACT_REVISE_SLIDE,
        Operation.ARTIFACT_RETRY: studio_rows.ARTIFACT_RETRY,
        Operation.ARTIFACT_DELETE: studio_rows.ARTIFACT_DELETE,
        Operation.ARTIFACT_PATCH_TITLE: studio_rows.ARTIFACT_PATCH_TITLE,
        Operation.ARTIFACT_WAIT: studio_rows.ARTIFACT_WAIT,
        Operation.ARTIFACT_DOWNLOAD: studio_rows.ARTIFACT_DOWNLOAD,
    }
    # Codec-row slice only: the P9.4b custom rows share this domain module.
    assert {op: studio_rows.STUDIO_ROWS[op] for op in converted} == converted
    for operation, row in converted.items():
        assert WEB_BINDING_ROWS[operation] is row
        binding = WEB_OPERATION_REGISTRY[operation]
        assert binding.is_supported
        assert binding.row is row
        assert isinstance(row, CodecBinding)
        assert row.definition is binding.definition
        assert row.deadline is DeadlineMode.INHERIT
        assert row.forward_disable_internal_retries is False
        assert row.map_error is None
    for row, method in (
        (studio_rows.ARTIFACT_EXPORT, RPCMethod.EXPORT_ARTIFACT),
        (studio_rows.ARTIFACT_REVISE_SLIDE, RPCMethod.REVISE_SLIDE),
        (studio_rows.ARTIFACT_RETRY, RPCMethod.RETRY_ARTIFACT),
        (studio_rows.ARTIFACT_DELETE, RPCMethod.DELETE_ARTIFACT),
        (studio_rows.ARTIFACT_PATCH_TITLE, RPCMethod.RENAME_ARTIFACT),
        (studio_rows.ARTIFACT_CATALOG, RPCMethod.LIST_ARTIFACTS),
        (studio_rows.ARTIFACT_WAIT, RPCMethod.LIST_ARTIFACTS),
    ):
        assert row.native.is_constant
        assert row.native.select(None).method is method
    # The download row is input-keyed over exactly the ledger's three natives.
    download = studio_rows.ARTIFACT_DOWNLOAD.native
    assert not download.is_constant
    assert set(download.choices) == {
        NativeChoice(RPCMethod.LIST_ARTIFACTS),
        NativeChoice(RPCMethod.GET_NOTES_AND_MIND_MAPS),
        NativeChoice(RPCMethod.GET_INTERACTIVE_HTML),
    }
    for name in (
        "_artifact_export",
        "_artifact_revise_slide",
        "_artifact_retry",
        "_artifact_delete",
        "_artifact_wait",
        "_artifact_download",
        "_artifact_rename",
        "_studio_rows",
        "_feature_unavailable",
    ):
        assert not hasattr(WebRpcBackend, name)
    # P9.4b generation and mind-map composites remain custom rows.
    assert WEB_OPERATION_REGISTRY[Operation.ARTIFACT_GENERATE_DATA_TABLE].row is (
        studio_rows.ARTIFACT_GENERATE_DATA_TABLE
    )
    assert WEB_OPERATION_REGISTRY[Operation.ARTIFACT_GENERATE_MIND_MAP].row is not None
    # P10 R4.2: the two catalog composites join rename as service-owned
    # workflows sequenced from artifact.catalog and mind_map.list.
    for operation in (
        Operation.ARTIFACT_RENAME,
        Operation.ARTIFACT_LIST,
        Operation.ARTIFACT_GET,
    ):
        binding = WEB_OPERATION_REGISTRY[operation]
        assert binding.service_owned is True
        assert binding.row is None
    backend = build_web_backend(_RecordingExecutor())
    assert backend._bindings[Operation.ARTIFACT_DOWNLOAD] is studio_rows.ARTIFACT_DOWNLOAD


@pytest.mark.parametrize(
    ("action", "expected"),
    [
        ("catalog", NativeChoice(RPCMethod.LIST_ARTIFACTS)),
        ("mind_maps", NativeChoice(RPCMethod.GET_NOTES_AND_MIND_MAPS)),
        ("interactive_html", NativeChoice(RPCMethod.GET_INTERACTIVE_HTML)),
        ("mind_map_tree", NativeChoice(RPCMethod.GET_INTERACTIVE_HTML)),
    ],
)
def test_download_selector_picks_one_native_per_action(
    action: str, expected: NativeChoice[RPCMethod]
) -> None:
    value = ArtifactDownloadInput("nb", action, "artifact-id")
    assert studio_rows.ARTIFACT_DOWNLOAD.native.select(value) == expected


def test_download_selector_rejects_unknown_actions_before_dispatch() -> None:
    with pytest.raises(BackendContractError, match="unrecognized artifact.download action"):
        studio_rows.ARTIFACT_DOWNLOAD.native.select(ArtifactDownloadInput("nb", "wire"))


# --- dispatch oracles ------------------------------------------------------------


@pytest.mark.asyncio
async def test_artifact_rename_primitives_preserve_byte_identical_web_kwargs() -> None:
    executor = _RecordingExecutor(None, [])
    backend = build_web_backend(executor)

    patched = await backend.invoke(
        ARTIFACT_PATCH_TITLE_DEF,
        ArtifactPatchTitleInput("nb", "artifact-id", "Renamed"),
        deadline=None,
    )
    catalog = await backend.invoke(
        ARTIFACT_CATALOG_DEF,
        ArtifactCatalogInput("nb"),
        deadline=None,
    )

    assert patched == ArtifactPatchTitleResult()
    assert catalog == ArtifactCatalogResult(artifacts=())
    patch_call, catalog_call = executor.calls
    assert patch_call.method is RPCMethod.RENAME_ARTIFACT
    assert patch_call.params == [["artifact-id", "Renamed"], [["title"]]]
    assert patch_call.kwargs == _BASE_KWARGS
    assert catalog_call.method is RPCMethod.LIST_ARTIFACTS
    assert catalog_call.params == _CATALOG_PARAMS
    assert catalog_call.kwargs == _BASE_KWARGS


@pytest.mark.asyncio
async def test_studio_rows_forward_the_identical_keyword_set() -> None:
    executor = _RecordingExecutor(
        {"exported": True},
        [["deck", None, None, None, 1]],
        [["retry-id", None, None, None, 1]],
        None,
        [["task-id", "Deck", 8, None, 3]],
    )
    backend = build_web_backend(executor)

    exported = await backend.invoke(
        ARTIFACT_EXPORT_DEF,
        DriveExportInput("nb", "artifact-id", "body", "Title", "sheets"),
        deadline=None,
    )
    revised = await backend.invoke(
        ARTIFACT_REVISE_SLIDE_DEF,
        ArtifactReviseSlideInput("nb", "deck", 2, "Improve"),
        deadline=None,
    )
    retried = await backend.invoke(
        ARTIFACT_RETRY_DEF, ArtifactRetryInput("nb", "retry-id"), deadline=None
    )
    deleted = await backend.invoke(
        ARTIFACT_DELETE_DEF, ArtifactDeleteInput("nb", "artifact-id"), deadline=None
    )
    observed = await backend.invoke(
        ARTIFACT_WAIT_DEF, ArtifactPollInput("nb", "task-id"), deadline=None
    )

    assert exported.value == {"exported": True}
    assert revised.status.task_id == "deck"
    assert retried.status.task_id == "retry-id"
    assert deleted == ArtifactDeleteResult()
    assert observed.status.task_id == "task-id"
    assert observed.status.status == "in_progress"

    export, revise, retry, delete, wait = executor.calls
    assert export.method is RPCMethod.EXPORT_ARTIFACT
    assert export.params == [None, "artifact-id", "body", "Title", 2]
    assert export.kwargs == _BASE_KWARGS
    assert revise.method is RPCMethod.REVISE_SLIDE
    assert revise.params == build_revise_slide_params("deck", 2, "Improve")
    assert revise.kwargs == {**_BASE_KWARGS, "raise_on_null_status": True}
    assert retry.method is RPCMethod.RETRY_ARTIFACT
    assert retry.params == build_retry_artifact_params("retry-id")
    assert retry.kwargs == {**_BASE_KWARGS, "raise_on_null_status": True}
    assert delete.method is RPCMethod.DELETE_ARTIFACT
    assert delete.params == [[2], "artifact-id"]
    assert delete.kwargs == _BASE_KWARGS
    assert wait.method is RPCMethod.LIST_ARTIFACTS
    assert wait.params == _CATALOG_PARAMS
    assert wait.kwargs == _BASE_KWARGS


@pytest.mark.asyncio
async def test_download_row_dispatches_every_branch_with_the_handler_shapes() -> None:
    executor = _RecordingExecutor(
        [],
        [],
        [[None] * 9 + [["<html>"]]],
        [[None] * 9 + [[None, None, None, '{"tree": 1}']]],
    )
    backend = build_web_backend(executor)

    catalog = await backend.invoke(
        ARTIFACT_DOWNLOAD_DEF, ArtifactDownloadInput("nb", "catalog"), deadline=None
    )
    maps = await backend.invoke(
        ARTIFACT_DOWNLOAD_DEF, ArtifactDownloadInput("nb", "mind_maps"), deadline=None
    )
    html = await backend.invoke(
        ARTIFACT_DOWNLOAD_DEF,
        ArtifactDownloadInput("nb", "interactive_html", "artifact-id"),
        deadline=None,
    )
    tree = await backend.invoke(
        ARTIFACT_DOWNLOAD_DEF,
        ArtifactDownloadInput("nb", "mind_map_tree", "artifact-id"),
        deadline=None,
    )

    assert catalog.representations == ()
    assert maps.mind_maps == ()
    assert html.content == "<html>"
    assert tree.content == '{"tree": 1}'
    catalog_call, maps_call, html_call, tree_call = executor.calls
    assert catalog_call.method is RPCMethod.LIST_ARTIFACTS
    assert catalog_call.params == _CATALOG_PARAMS
    assert maps_call.method is RPCMethod.GET_NOTES_AND_MIND_MAPS
    assert maps_call.params == ["nb"]
    assert html_call.method is RPCMethod.GET_INTERACTIVE_HTML
    assert html_call.params == ["artifact-id"]
    assert tree_call.method is RPCMethod.GET_INTERACTIVE_HTML
    assert tree_call.params == ["artifact-id"]
    for call in executor.calls:
        assert call.kwargs == _BASE_KWARGS


@pytest.mark.asyncio
async def test_download_contract_errors_are_raised_before_any_native_call() -> None:
    executor = _RecordingExecutor()
    backend = build_web_backend(executor)

    with pytest.raises(BackendContractError, match="unrecognized artifact.download action"):
        await backend.invoke(
            ARTIFACT_DOWNLOAD_DEF, ArtifactDownloadInput("nb", "wire_passthrough"), deadline=None
        )
    with pytest.raises(BackendContractError, match="requires artifact_id"):
        await backend.invoke(
            ARTIFACT_DOWNLOAD_DEF, ArtifactDownloadInput("nb", "interactive_html"), deadline=None
        )
    with pytest.raises(BackendContractError, match="unrecognized Drive export destination"):
        await backend.invoke(
            ARTIFACT_EXPORT_DEF,
            DriveExportInput("nb", "artifact-id", None, "Title", "slides"),
            deadline=None,
        )
    assert executor.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("definition", "value", "artifact_type", "method"),
    [
        (
            ARTIFACT_REVISE_SLIDE_DEF,
            ArtifactReviseSlideInput("nb", "deck", 2, "Improve"),
            "slide revision",
            RPCMethod.REVISE_SLIDE,
        ),
        (
            ARTIFACT_RETRY_DEF,
            ArtifactRetryInput("nb", "retry-id"),
            "retry",
            RPCMethod.RETRY_ARTIFACT,
        ),
    ],
)
async def test_null_kickoff_keeps_the_closed_feature_unavailable_error(
    definition: Any, value: Any, artifact_type: str, method: RPCMethod
) -> None:
    backend = build_web_backend(_RecordingExecutor(None))

    with pytest.raises(BackendError) as caught:
        await backend.invoke(definition, value, deadline=None)

    error = caught.value
    assert type(error) is BackendError
    assert error.reason is BackendErrorReason.ARTIFACT_FEATURE_UNAVAILABLE
    assert error.operation is definition.key
    assert error.message == f"{artifact_type.capitalize()} generation is unavailable"
    assert error.outcome_unknown is False
    assert dict(error.diagnostics or {}) == {
        "artifact_type": artifact_type,
        "method_id": method.value,
        "raw_response": None,
    }


@pytest.mark.asyncio
async def test_codec_row_read_timeout_is_clamped_to_the_shared_deadline() -> None:
    executor = _RecordingExecutor([["task-id", "Deck", 8, None, 3]])
    backend = build_web_backend(executor)
    deadline = RuntimeDeadline(timeout=5.0, started_at=10.0, monotonic=lambda: 11.0)

    await backend.invoke(ARTIFACT_WAIT_DEF, ArtifactPollInput("nb", "task-id"), deadline=deadline)

    (call,) = executor.calls
    assert call.kwargs["read_timeout"] == pytest.approx(4.0)
    assert call.kwargs["_retry_deadline"] is deadline


@pytest.mark.asyncio
async def test_codec_row_server_error_translates_like_a_handler_and_is_dispatched() -> None:
    server_error = ServerError("boom", method_id=RPCMethod.DELETE_ARTIFACT.value)
    backend = build_web_backend(_RecordingExecutor(server_error))

    with pytest.raises(BackendError) as caught:
        await backend.invoke(
            ARTIFACT_DELETE_DEF, ArtifactDeleteInput("nb", "artifact-id"), deadline=None
        )

    error = caught.value
    assert type(error) is BackendError
    assert error.operation is Operation.ARTIFACT_DELETE
    assert error.reason is BackendErrorReason.SERVER
    assert error.message == "boom"
    assert error.outcome_unknown is False
    assert error.diagnostics is not None
    assert error.diagnostics["method_id"] == RPCMethod.DELETE_ARTIFACT.value
    assert "public_error_failure" in error.diagnostics
    assert error.dispatched is True
    assert may_have_committed(error) is True
    assert error.__cause__ is server_error


@pytest.mark.asyncio
async def test_codec_row_timeout_after_expiry_becomes_a_dispatched_deadline_error() -> None:
    clock = [11.0]
    executor = _RecordingExecutor(RPCTimeoutError("slow", method_id=RPCMethod.LIST_ARTIFACTS.value))
    backend = build_web_backend(executor)
    deadline = RuntimeDeadline(timeout=5.0, started_at=10.0, monotonic=lambda: clock[0])

    async def rpc_call(method: RPCMethod, params: list[Any], **kwargs: Any) -> Any:
        clock[0] = 16.0
        return await _RecordingExecutor.rpc_call(executor, method, params, **kwargs)

    backend._runtime = type("Runtime", (), {"rpc_call": staticmethod(rpc_call)})()  # type: ignore[assignment]

    with pytest.raises(BackendDeadlineExceededError) as caught:
        await backend.invoke(
            ARTIFACT_WAIT_DEF, ArtifactPollInput("nb", "task-id"), deadline=deadline
        )

    error = caught.value
    assert error.operation is Operation.ARTIFACT_WAIT
    assert error.reason is BackendErrorReason.TIMEOUT
    assert error.outcome_unknown is False  # READ policy
    assert error.dispatched is True
    assert error.diagnostics is not None
    assert error.diagnostics["timeout"] == 5.0
    assert error.diagnostics["method_id"] == RPCMethod.LIST_ARTIFACTS.value
    assert "public_error_failure" in error.diagnostics
    assert isinstance(error.__cause__, RPCTimeoutError)


@pytest.mark.asyncio
async def test_codec_row_pre_dispatch_expiry_is_not_dispatched() -> None:
    executor = _RecordingExecutor()
    backend = build_web_backend(executor)
    deadline = RuntimeDeadline(timeout=5.0, started_at=10.0, monotonic=lambda: 16.0)

    with pytest.raises(BackendDeadlineExceededError) as caught:
        await backend.invoke(
            ARTIFACT_DELETE_DEF, ArtifactDeleteInput("nb", "artifact-id"), deadline=deadline
        )

    assert executor.calls == []
    assert caught.value.operation is Operation.ARTIFACT_DELETE
    assert caught.value.outcome_unknown is False
    assert caught.value.dispatched is False
    assert may_have_committed(caught.value) is False
