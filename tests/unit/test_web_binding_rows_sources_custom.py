"""P9.4b: the source-add family dispatches as ``CustomBinding`` rows exactly as the handlers did.

``SOURCE_ADD_FILE`` declares its natives under spec
keys and sequence them through the row-scoped invoker.  These tests pin the
conversion oracles: the partition and categories, the identical keyword set per
phase (including explicit ``False``/``None`` values, ``disable_internal_retries``
on the guarded create and ``allow_null`` on the rename), the
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

import httpx
import pytest

from notebooklm._backend import (
    BackendContractError,
    BackendError,
    BackendErrorReason,
)
from notebooklm._backend_compat import project_backend_error
from notebooklm._binding import CustomBinding, ErrorMode
from notebooklm._deadline import RuntimeDeadline
from notebooklm._idempotency import mark_unconfirmed
from notebooklm._operations import Operation
from notebooklm._records import (
    SOURCE_ADD_FILE_DEF,
    SourceAddFailureRecord,
    SourceAddFileInput,
    SourceFileInputKind,
)
from notebooklm._source._upload_decode import raise_partial_upload_failure
from notebooklm._source_upload_port import SourceUploadBackend
from notebooklm._web.backend import ROW_COLLABORATOR_NAMES, WebRpcBackend, _row_error_projection
from notebooklm._web.bindings import WEB_BINDING_ROWS
from notebooklm._web.bindings import sources as source_rows
from notebooklm._web.registry import WEB_OPERATION_REGISTRY
from notebooklm.exceptions import (
    NetworkError,
    ServerError,
    ValidationError,
)
from notebooklm.rpc import RPCMethod
from notebooklm.types import Source, SourceStatus
from tests._fixtures.source_add_replay import assert_replays as _assert_replays
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
        Operation.SOURCE_ADD_FILE: (
            source_rows.SOURCE_ADD_FILE,
            "protocol",
            ErrorMode.TRANSLATE,
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
        # P10 invariant I8: no row asks the head to pass a native failure through.
        assert _row_error_projection(row, operation) is False
    for name in (
        "_source_add_url",
        "_source_add_url_batch",
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

    with pytest.raises(BackendError) as caught:
        await backend.invoke(
            SOURCE_ADD_FILE_DEF,
            SourceAddFileInput(_NB, SourceFileInputKind.LOCAL, file_path=tmp_path / "f.txt"),
            deadline=None,
        )

    # I8: the callback failure leaves as neutral evidence, still tagged with the
    # register spec on the private cause, and replays field-for-field at the facade.
    _assert_neutral_source_add_failure(
        caught.value,
        operation=Operation.SOURCE_ADD_FILE,
        native=error,
    )
    assert error.binding_native.method is RPCMethod.ADD_SOURCE_FILE  # type: ignore[attr-defined]
    assert error.dispatched is True  # type: ignore[attr-defined]
    assert caught.value.dispatched is True
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


# --- the replay oracle -----------------------------------------------------------------
#
# The value contract itself lives in ``tests/_fixtures/source_add_replay.py``:
# both the surviving custom rows and the workflows P10 hoists above the port
# are held to the same "equal, never identical" oracle.


def _assert_neutral_source_add_failure(
    error: BackendError,
    *,
    operation: Operation,
    native: BaseException,
    outcome_unknown: bool = False,
) -> None:
    """The row reported one neutral SOURCE_ADD reason that replays ``native``."""
    assert type(error) is BackendError
    assert error.operation is operation
    assert error.reason is BackendErrorReason.SOURCE_ADD
    assert error.outcome_unknown is outcome_unknown
    assert error.diagnostics is not None
    record = error.diagnostics["source_add_failure"]
    assert isinstance(record, SourceAddFailureRecord)
    # The native object itself remains reachable as the private cause, still
    # carrying the row's spec tagging; nothing above the port reads it.
    assert error.__cause__ is native
    _assert_replays(project_backend_error(error), native)


# --- failure projection --------------------------------------------------------------------


class _FailingUploader(_Uploader):
    """A pipeline double whose upload leg fails after registration."""

    def __init__(self, error: BaseException) -> None:
        super().__init__()
        self.error = error

    async def _add_file_result(
        self, notebook_id: str, file_path: str | Path, **kwargs: Any
    ) -> object:
        assert self.active is not None, "upload ran outside the row's bound backend"
        self.calls.append((notebook_id, file_path, kwargs))
        raise self.error


def _partial_upload_failure() -> NetworkError:
    """The exact post-registration graph ``raise_partial_upload_failure`` builds.

    ``httpx.RequestError`` mid-body → a ``NetworkError`` normalisation that keeps
    the httpx leaf as both ``original_error`` and ``__cause__``, tagged with the
    retained ``source_id``/``stage`` and marked unconfirmed by the pipeline.
    """
    request = httpx.Request("POST", "https://upload.example/x?upload_id=secret")
    leaf = httpx.ReadError("connection reset", request=request)
    try:
        raise_partial_upload_failure(
            leaf, "f.txt", source_id="retained-id", stage="upload_finalize"
        )
    except NetworkError as exc:
        mark_unconfirmed(exc)
        return exc
    raise AssertionError("raise_partial_upload_failure did not raise")


@pytest.mark.asyncio
async def test_add_file_replays_the_post_registration_upload_graph(tmp_path: Path) -> None:
    """The permanent D4 row: every field of the Scotty failure graph survives.

    This is the parity evidence R3.1 owes for deleting ``RAW_PASSTHROUGH`` on
    ``source.add_file``: the exception the facade raises after the neutral
    round trip is field-for-field the exception the pipeline raised, including
    the retained ``source_id``/``stage``, the unconfirmed marker, the httpx
    ``original_error`` and the causal chain.
    """
    native = _partial_upload_failure()
    uploader = _FailingUploader(native)
    backend = build_web_backend(_RecordingExecutor(), source_uploader=uploader)

    with pytest.raises(BackendError) as caught:
        await backend.invoke(
            SOURCE_ADD_FILE_DEF,
            SourceAddFileInput(_NB, SourceFileInputKind.LOCAL, file_path=tmp_path / "f.txt"),
            deadline=None,
        )

    _assert_neutral_source_add_failure(
        caught.value,
        operation=Operation.SOURCE_ADD_FILE,
        native=native,
        outcome_unknown=True,
    )
    replayed = project_backend_error(caught.value)
    # Spelled out, not just asserted through the oracle: these are the fields the
    # upload recovery contract documents for a partial upload.
    assert type(replayed) is NetworkError
    assert replayed.source_id == "retained-id"  # type: ignore[attr-defined]
    assert replayed.stage == "upload_finalize"  # type: ignore[attr-defined]
    assert replayed.unconfirmed is True  # type: ignore[attr-defined]
    assert isinstance(replayed.original_error, httpx.ReadError)
    assert replayed.__cause__ is replayed.original_error
    assert replayed.__suppress_context__ is True
    assert uploader.active is None


@pytest.mark.asyncio
async def test_add_file_replays_a_rejected_title_as_validation_error(tmp_path: Path) -> None:
    """``ValidationError`` is outside the four reviewed families and still survives."""
    backend = build_web_backend(_RecordingExecutor(), source_uploader=_Uploader())

    with pytest.raises(BackendError) as caught:
        await backend.invoke(
            SOURCE_ADD_FILE_DEF,
            SourceAddFileInput(
                _NB,
                SourceFileInputKind.LOCAL,
                file_path=tmp_path / "f.txt",
                title="   ",
                wait=True,
            ),
            deadline=None,
        )

    replayed = project_backend_error(caught.value)
    assert type(replayed) is ValidationError
    assert str(replayed) == "Title cannot be empty or whitespace-only"
