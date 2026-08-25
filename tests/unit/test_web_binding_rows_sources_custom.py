"""P9.4b: the source-add family dispatches as ``CustomBinding`` rows exactly as the handlers did.

``SOURCE_ADD_URL``, ``SOURCE_ADD_URL_BATCH``, ``SOURCE_ADD_TEXT``,
``SOURCE_ADD_DRIVE`` and ``SOURCE_ADD_FILE`` declare their natives under spec
keys and sequence them through the row-scoped invoker.  These tests pin the
conversion oracles: the partition and categories, the identical keyword set per
phase (including explicit ``False``/``None`` values, ``disable_internal_retries``
on the guarded creates, ``allow_null`` on the Drive create and the rename), the
raw passthrough of the established public leaves, the ``dispatched`` marker on
the one translated row, failure tagging with the selected spec, the deadline
projection, and — plan open item 1 — that the upload pipeline's callbacks run
through the ``SOURCE_ADD_FILE`` row's invoker for the invocation.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from notebooklm._backend import (
    BackendContractError,
    BackendDeadlineExceededError,
    BackendError,
    BackendErrorReason,
    may_have_committed,
)
from notebooklm._binding import CustomBinding, ErrorMode
from notebooklm._deadline import RuntimeDeadline
from notebooklm._operations import Operation
from notebooklm._records import (
    SOURCE_ADD_DRIVE_DEF,
    SOURCE_ADD_FILE_DEF,
    SOURCE_ADD_TEXT_DEF,
    SOURCE_ADD_URL_BATCH_DEF,
    SOURCE_ADD_URL_DEF,
    SourceAddCommitState,
    SourceAddDriveInput,
    SourceAddFileInput,
    SourceAddTextInput,
    SourceAddTitleState,
    SourceAddUrlBatchInput,
    SourceAddUrlInput,
    SourceFileInputKind,
)
from notebooklm._source_upload_port import SourceUploadBackend
from notebooklm._web.backend import ROW_COLLABORATOR_NAMES, WebRpcBackend, _row_error_projection
from notebooklm._web.bindings import WEB_BINDING_ROWS
from notebooklm._web.bindings import sources as source_rows
from notebooklm._web.registry import WEB_OPERATION_REGISTRY
from notebooklm.exceptions import RPCTimeoutError, ServerError
from notebooklm.rpc import RPCMethod
from notebooklm.types import Source, SourceStatus
from tests._fixtures.web_backend import build_web_backend

_NB = "nb"
_ROUTE = "/notebook/nb"

_BASE_KWARGS = {
    "allow_null": False,
    "_is_retry": False,
    "disable_internal_retries": False,
    "operation_variant": None,
    "read_timeout": None,
    "raise_on_null_status": False,
    "_retry_deadline": None,
}


def _source_entry(
    source_id: str,
    *,
    title: str,
    url: str | None = None,
    status: int = 2,
    kind: int = 5,
) -> list[Any]:
    metadata = [None, 11, [1704067200, 0], None, kind, None, None, [url] if url else None]
    return [[source_id], title, metadata, [None, status]]


def _snapshot(*entries: list[Any]) -> list[Any]:
    return [["Notebook", list(entries), _NB]]


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


class _Uploader:
    """A fake pipeline recording the per-invocation backend the row binds."""

    def __init__(self) -> None:
        self.bound: list[SourceUploadBackend] = []
        self.active: SourceUploadBackend | None = None
        self.limit_lookup: object | None = None
        self.source_backend: dict[str, object] | None = None
        self.calls: list[tuple[object, ...]] = []

    def configure_source_limit_lookup(self, callback: object) -> None:
        self.limit_lookup = callback

    def configure_source_backend(self, **callbacks: object) -> None:
        self.source_backend = callbacks

    @contextmanager
    def bind_backend(self, backend: SourceUploadBackend) -> Iterator[None]:
        self.bound.append(backend)
        self.active = backend
        try:
            yield
        finally:
            self.active = None

    async def _add_file_result(
        self, notebook_id: str, file_path: str | Path, **kwargs: Any
    ) -> object:
        assert self.active is not None, "upload ran outside the row's bound backend"
        self.calls.append((notebook_id, file_path, kwargs))
        # The pipeline's registration, listing and limit lookup all go through
        # the bound backend during the upload.
        registration = await self.active.register_file_source(notebook_id, "file.txt")
        listed = await self.active.list_sources(notebook_id)
        limit = await self.active.get_source_limit() if self.active.get_source_limit else None
        self.calls.append(("registered", registration.source_id, [s.id for s in listed], limit))
        source = Source(id="uploaded", title="file.txt", status=SourceStatus.PROCESSING)
        return type("UploadResult", (), {"source": source, "transient_error_types": ()})()


# --- partition -----------------------------------------------------------------------


def test_source_add_rows_replace_their_handlers_with_declared_specs() -> None:
    expected = {
        Operation.SOURCE_ADD_URL: (
            source_rows.SOURCE_ADD_URL,
            "protocol",
            ErrorMode.TRANSLATE,
            {
                ("snapshot", RPCMethod.GET_NOTEBOOK, None),
                ("create", RPCMethod.ADD_SOURCE, "url"),
                ("rename", RPCMethod.UPDATE_SOURCE, None),
            },
            ("capture_public_failure",),
        ),
        Operation.SOURCE_ADD_URL_BATCH: (
            source_rows.SOURCE_ADD_URL_BATCH,
            "protocol",
            ErrorMode.RAW_PASSTHROUGH,
            {("create", RPCMethod.ADD_SOURCE, "url"), ("snapshot", RPCMethod.GET_NOTEBOOK, None)},
            ("capture_public_failure",),
        ),
        Operation.SOURCE_ADD_TEXT: (
            source_rows.SOURCE_ADD_TEXT,
            "compatibility",
            ErrorMode.RAW_PASSTHROUGH,
            {("create", RPCMethod.ADD_SOURCE, "text")},
            (),
        ),
        Operation.SOURCE_ADD_DRIVE: (
            source_rows.SOURCE_ADD_DRIVE,
            "protocol",
            ErrorMode.RAW_PASSTHROUGH,
            {
                ("create", RPCMethod.ADD_SOURCE, "drive"),
                ("snapshot", RPCMethod.GET_NOTEBOOK, None),
                ("rename", RPCMethod.UPDATE_SOURCE, None),
            },
            (),
        ),
        Operation.SOURCE_ADD_FILE: (
            source_rows.SOURCE_ADD_FILE,
            "protocol",
            ErrorMode.RAW_PASSTHROUGH,
            {
                ("register", RPCMethod.ADD_SOURCE_FILE, None),
                ("snapshot", RPCMethod.GET_NOTEBOOK, None),
                ("limits", RPCMethod.GET_USER_SETTINGS, None),
                ("rename", RPCMethod.UPDATE_SOURCE, None),
            },
            ("source_uploader",),
        ),
    }
    for operation, (row, category, error_mode, specs, collaborators) in expected.items():
        assert WEB_BINDING_ROWS[operation] is row
        binding = WEB_OPERATION_REGISTRY[operation]
        assert binding.is_supported and binding.row is row
        assert isinstance(row, CustomBinding)
        assert row.definition is binding.definition
        assert row.category == category
        assert row.justification.strip()
        assert row.error_mode is error_mode
        assert row.collaborators == collaborators
        assert set(collaborators) <= ROW_COLLABORATOR_NAMES
        assert {
            (spec.key, choice.method, choice.variant)
            for spec in row.native
            for choice in spec.choices
        } == specs
        raw, scrub = _row_error_projection(row, operation)
        assert raw is (error_mode is ErrorMode.RAW_PASSTHROUGH)
        assert scrub is False
    for name in (
        "_source_add_url",
        "_source_add_url_batch",
        "_source_add_text",
        "_source_add_drive",
        "_source_add_file",
        "_source_public_snapshot",
        "_source_upload_list",
        "_source_register_file",
        "_source_upload_rename",
        "_source_file_limit",
        "_rename_source_public",
        "_create_url_source",
    ):
        assert not hasattr(WebRpcBackend, name)
    backend = build_web_backend(_RecordingExecutor())
    for operation, (row, *_rest) in expected.items():
        assert backend._bindings[operation] is row


# --- phase sequences and identical kwargs -----------------------------------------------


@pytest.mark.asyncio
async def test_add_url_probe_create_and_rename_phases_forward_identical_kwargs() -> None:
    url = "https://example.com/doc"
    created = _source_entry("src", title="Upstream", url=url)
    executor = _RecordingExecutor(
        _snapshot(),  # baseline snapshot
        [[created]],  # guarded create
        [_source_entry("src", title="Requested", url=url)],  # rename echo
    )
    backend = build_web_backend(executor)
    deadline = RuntimeDeadline(timeout=30.0, started_at=10.0, monotonic=lambda: 12.0)

    result = await backend.invoke(
        SOURCE_ADD_URL_DEF,
        SourceAddUrlInput(_NB, url, requested_title="Requested"),
        deadline=deadline,
    )

    assert result.source.id == "src"
    assert result.receipt.commit_state is SourceAddCommitState.CREATED
    assert result.receipt.title_state is SourceAddTitleState.RENAMED
    snapshot, create, rename = executor.calls
    assert snapshot.method is RPCMethod.GET_NOTEBOOK
    assert snapshot.kwargs == {
        **_BASE_KWARGS,
        "source_path": _ROUTE,
        "read_timeout": 28.0,
        "_retry_deadline": deadline,
    }
    assert create.method is RPCMethod.ADD_SOURCE
    assert create.kwargs == {
        **_BASE_KWARGS,
        "source_path": _ROUTE,
        "disable_internal_retries": True,
        "operation_variant": "url",
        "read_timeout": 28.0,
        "_retry_deadline": deadline,
    }
    assert rename.method is RPCMethod.UPDATE_SOURCE
    assert rename.kwargs == {
        **_BASE_KWARGS,
        "source_path": _ROUTE,
        "allow_null": True,
        "read_timeout": 28.0,
        "_retry_deadline": deadline,
    }


@pytest.mark.asyncio
async def test_add_url_batch_create_then_reconciliation_snapshot() -> None:
    good = "https://good.example/"
    missing = "https://missing.example/"
    executor = _RecordingExecutor(
        [_source_entry("good", title="Good", url=good)],
        _snapshot(_source_entry("ghost", title="Ghost", url=missing, status=3)),
    )

    result = await build_web_backend(executor).invoke(
        SOURCE_ADD_URL_BATCH_DEF, SourceAddUrlBatchInput(_NB, (good, missing)), deadline=None
    )

    create, snapshot = executor.calls
    assert create.method is RPCMethod.ADD_SOURCE
    assert create.kwargs == {
        **_BASE_KWARGS,
        "source_path": _ROUTE,
        "disable_internal_retries": True,
        "operation_variant": "url",
    }
    assert snapshot.method is RPCMethod.GET_NOTEBOOK
    assert snapshot.kwargs == {**_BASE_KWARGS, "source_path": _ROUTE}
    assert result.items[0].source is not None and result.items[0].source.id == "good"
    assert result.items[1].error is not None


@pytest.mark.asyncio
async def test_add_text_is_one_unguarded_text_create() -> None:
    executor = _RecordingExecutor([[_source_entry("txt", title="Pasted")]])

    result = await build_web_backend(executor).invoke(
        SOURCE_ADD_TEXT_DEF, SourceAddTextInput(_NB, "Pasted", "body"), deadline=None
    )

    (create,) = executor.calls
    assert result.source.id == "txt"
    assert create.method is RPCMethod.ADD_SOURCE
    assert create.kwargs == {**_BASE_KWARGS, "source_path": _ROUTE, "operation_variant": "text"}


@pytest.mark.asyncio
async def test_add_drive_guarded_null_tolerant_create_then_rename_without_hydration() -> None:
    executor = _RecordingExecutor(
        _snapshot(),
        [[_source_entry("drv", title="Upstream")]],
        None,  # rename echoes nothing: no hydration on the Drive path
    )

    result = await build_web_backend(executor).invoke(
        SOURCE_ADD_DRIVE_DEF,
        SourceAddDriveInput(_NB, "file-id", "Requested", "application/pdf"),
        deadline=None,
    )

    snapshot, create, rename = executor.calls
    assert result.source.id == "drv"
    assert snapshot.method is RPCMethod.GET_NOTEBOOK
    assert create.method is RPCMethod.ADD_SOURCE
    assert create.kwargs == {
        **_BASE_KWARGS,
        "source_path": _ROUTE,
        "allow_null": True,
        "disable_internal_retries": True,
        "operation_variant": "drive",
    }
    assert rename.method is RPCMethod.UPDATE_SOURCE
    assert rename.kwargs == {**_BASE_KWARGS, "source_path": _ROUTE, "allow_null": True}


# --- open item 1: the upload pipeline runs through the row's invoker ------------------------


@pytest.mark.asyncio
async def test_add_file_binds_its_invoker_backed_callbacks_for_the_invocation(
    tmp_path: Path,
) -> None:
    uploader = _Uploader()
    executor = _RecordingExecutor(
        [[["registered-id"]]],  # ADD_SOURCE_FILE registration
        _snapshot(_source_entry("registered-id", title="file.txt")),  # listing
        [[[None, None, None, None, None, None, None, 300]]],  # GET_USER_SETTINGS (limit lookup)
    )
    backend = build_web_backend(executor, source_uploader=uploader)
    # Construction wires the pipeline's default callbacks under the same row.
    assert uploader.limit_lookup is not None
    assert uploader.source_backend is not None and set(uploader.source_backend) == {
        "list_sources",
        "register_file_source",
        "rename_source",
    }
    path = tmp_path / "file.txt"

    result = await backend.invoke(
        SOURCE_ADD_FILE_DEF,
        SourceAddFileInput(_NB, SourceFileInputKind.LOCAL, file_path=path, mime_type="text/plain"),
        deadline=RuntimeDeadline(timeout=5.0, started_at=10.0, monotonic=lambda: 11.0),
    )

    assert result.source.id == "uploaded"
    assert len(uploader.bound) == 1 and uploader.active is None  # bound for exactly one invocation
    assert uploader.calls[0] == (
        _NB,
        path,
        {
            "mime_type": "text/plain",
            "wait": False,
            "wait_timeout": 120.0,
            "title": None,
            "on_progress": None,
        },
    )
    assert uploader.calls[1][:3] == ("registered", "registered-id", ["registered-id"])
    register, listing, limits = executor.calls
    # Upload legs keep their own windows: the row's deadline never reaches the callbacks.
    assert register.method is RPCMethod.ADD_SOURCE_FILE
    assert register.kwargs == {
        **_BASE_KWARGS,
        "source_path": _ROUTE,
        "disable_internal_retries": True,
    }
    assert listing.method is RPCMethod.GET_NOTEBOOK
    assert listing.kwargs == {**_BASE_KWARGS, "source_path": _ROUTE}
    assert limits.method is RPCMethod.GET_USER_SETTINGS
    assert limits.kwargs == {**_BASE_KWARGS, "source_path": "/"}


@pytest.mark.asyncio
async def test_add_file_callback_failures_are_tagged_with_the_selected_spec(
    tmp_path: Path,
) -> None:
    uploader = _Uploader()
    error = ServerError("boom", method_id=RPCMethod.ADD_SOURCE_FILE.value)
    backend = build_web_backend(_RecordingExecutor(error), source_uploader=uploader)

    with pytest.raises(ServerError) as caught:
        await backend.invoke(
            SOURCE_ADD_FILE_DEF,
            SourceAddFileInput(_NB, SourceFileInputKind.LOCAL, file_path=tmp_path / "f.txt"),
            deadline=None,
        )

    # Raw passthrough: the native error itself, tagged with the register spec.
    assert caught.value is error
    assert caught.value.binding_native.method is RPCMethod.ADD_SOURCE_FILE  # type: ignore[attr-defined]
    assert caught.value.dispatched is True  # type: ignore[attr-defined]
    assert uploader.active is None


@pytest.mark.asyncio
async def test_add_file_without_a_pipeline_is_a_contract_error_before_any_call() -> None:
    executor = _RecordingExecutor()
    backend = build_web_backend(executor, source_uploader=None)

    with pytest.raises(BackendContractError, match="composition-root upload pipeline"):
        await backend.invoke(
            SOURCE_ADD_FILE_DEF,
            SourceAddFileInput(_NB, SourceFileInputKind.LOCAL, file_path="f.txt"),
            deadline=None,
        )
    assert executor.calls == []


# --- failure projection --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_url_translates_the_public_leaf_and_carries_the_dispatched_marker() -> None:
    executor = _RecordingExecutor(
        _snapshot(),
        ServerError("boom", method_id=RPCMethod.ADD_SOURCE.value),
        _snapshot(),  # the probe after the transport failure finds nothing
        ServerError("boom again", method_id=RPCMethod.ADD_SOURCE.value),
        _snapshot(),
    )

    with pytest.raises(BackendError) as caught:
        await build_web_backend(executor).invoke(
            SOURCE_ADD_URL_DEF, SourceAddUrlInput(_NB, "https://example.com/x"), deadline=None
        )

    error = caught.value
    assert type(error) is BackendError
    assert error.operation is Operation.SOURCE_ADD_URL
    assert error.reason is BackendErrorReason.SOURCE_ADD
    assert error.diagnostics is not None
    assert error.diagnostics["receipt"].commit_state is SourceAddCommitState.FAILED
    assert "source_add_failure" in error.diagnostics
    # The transport error escapes SourceAddService raw; the row keeps it as the
    # cause, tagged with the create spec and marked dispatched by the transport.
    assert isinstance(error.__cause__, ServerError)
    assert error.__cause__.dispatched is True  # type: ignore[attr-defined]
    assert error.__cause__.binding_native.method is RPCMethod.ADD_SOURCE  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_add_text_passes_the_native_failure_through_raw() -> None:
    error = ServerError("down", method_id=RPCMethod.ADD_SOURCE.value)
    executor = _RecordingExecutor(error)

    with pytest.raises(ServerError) as caught:
        await build_web_backend(executor).invoke(
            SOURCE_ADD_TEXT_DEF, SourceAddTextInput(_NB, "T", "body"), deadline=None
        )

    # RAW_PASSTHROUGH: the very object the runtime raised, never a BackendError.
    assert caught.value is error
    assert error.dispatched is True  # type: ignore[attr-defined]
    assert error.binding_native.method is RPCMethod.ADD_SOURCE  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_add_url_batch_rename_free_timeout_after_expiry_passes_through_raw() -> None:
    clock = [11.0]
    executor = _RecordingExecutor(
        RPCTimeoutError("slow", method_id=RPCMethod.ADD_SOURCE.value),
    )
    backend = build_web_backend(executor)
    deadline = RuntimeDeadline(timeout=5.0, started_at=10.0, monotonic=lambda: clock[0])

    async def rpc_call(method: RPCMethod, params: list[Any], **kwargs: Any) -> Any:
        clock[0] = 16.0
        return await _RecordingExecutor.rpc_call(executor, method, params, **kwargs)

    backend._runtime = type("Runtime", (), {"rpc_call": staticmethod(rpc_call)})()  # type: ignore[assignment]

    # RAW_PASSTHROUGH: the batch service marks the whole write unconfirmed and
    # re-raises the native timeout; the head never projects it as a deadline error.
    with pytest.raises(RPCTimeoutError) as caught:
        await backend.invoke(
            SOURCE_ADD_URL_BATCH_DEF,
            SourceAddUrlBatchInput(_NB, ("https://a.example/",)),
            deadline=deadline,
        )
    assert getattr(caught.value, "unconfirmed", False) is True
    assert caught.value.dispatched is True  # type: ignore[attr-defined]
    assert caught.value.binding_native.method is RPCMethod.ADD_SOURCE  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_add_drive_pre_dispatch_expiry_is_not_dispatched() -> None:
    executor = _RecordingExecutor()
    deadline = RuntimeDeadline(timeout=5.0, started_at=10.0, monotonic=lambda: 16.0)

    with pytest.raises(BackendDeadlineExceededError) as caught:
        await build_web_backend(executor).invoke(
            SOURCE_ADD_DRIVE_DEF,
            SourceAddDriveInput(_NB, "file-id", "T", "application/pdf"),
            deadline=deadline,
        )

    assert executor.calls == []
    assert caught.value.dispatched is False
    assert may_have_committed(caught.value) is False
