"""P9.4b: the notebook and mind-map/catalog composites dispatch as ``CustomBinding`` rows.

``NOTEBOOK_CREATE``, ``NOTEBOOK_UPDATE``, ``MIND_MAP_GENERATE_NOTE``,
``MIND_MAP_GENERATE_INTERACTIVE``, ``ARTIFACT_GENERATE_MIND_MAP``,
``ARTIFACT_LIST`` and ``ARTIFACT_GET`` declare their natives as keyed specs and
sequence them through the row-scoped invoker.  These tests pin the conversion
oracles: the identical keyword set reaches the runtime for every phase
(including explicit ``False``/``None`` values, ``disable_internal_retries`` on
the guarded create, ``operation_variant="plain"`` on the legacy note
allocation), the closed error identities (quota limit, not-found, feature
unavailable), the raw partial-availability swallow the catalog rows keep,
failure tagging with the selected spec, the deadline projection, and the
``InvokerRpcCaller`` contract that replaced ``DeadlineRpcCaller``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx
import pytest

from notebooklm._backend import (
    BackendContractError,
    BackendDeadlineExceededError,
    BackendError,
    BackendErrorReason,
    may_have_committed,
)
from notebooklm._binding import CodecPayload, CustomBinding, NativeChoice
from notebooklm._deadline import RuntimeDeadline
from notebooklm._operations import Operation
from notebooklm._records import (
    ARTIFACT_GENERATE_MIND_MAP_DEF,
    ARTIFACT_GET_DEF,
    ARTIFACT_LIST_DEF,
    MIND_MAP_GENERATE_INTERACTIVE_DEF,
    MIND_MAP_GENERATE_NOTE_DEF,
    NOTEBOOK_CREATE_DEF,
    NOTEBOOK_UPDATE_DEF,
    ArtifactGetInput,
    ArtifactListInput,
    MindMapGenerateInput,
    MindMapGenerateInteractiveInput,
    MindMapGenerateNoteInput,
    NotebookCreateInput,
    NotebookUpdateInput,
)
from notebooklm._web.backend import WebRpcBackend
from notebooklm._web.bindings import WEB_BINDING_ROWS
from notebooklm._web.bindings import mind_maps as mind_map_rows
from notebooklm._web.bindings import notebooks as notebook_rows
from notebooklm._web.bindings._invoker_caller import InvokerRpcCaller
from notebooklm._web.registry import WEB_OPERATION_REGISTRY
from notebooklm.exceptions import ClientError, RPCError, RPCTimeoutError, ServerError
from notebooklm.rpc import RPCMethod
from tests._fixtures.web_backend import build_web_backend

_BASE_KWARGS = {
    "allow_null": False,
    "_is_retry": False,
    "disable_internal_retries": False,
    "operation_variant": None,
    "read_timeout": None,
    "raise_on_null_status": False,
    "_retry_deadline": None,
}
_CREATED_ROW = [
    "Daily News",
    None,
    "nb-new",
    None,
    None,
    [None, False, None, None, None, [1704067200, 0]],
]


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


def _kwargs(source_path: str, **overrides: Any) -> dict[str, Any]:
    return {**_BASE_KWARGS, "source_path": source_path, **overrides}


# --- registry partition ----------------------------------------------------------


def test_composites_are_custom_rows_with_their_categories_and_specs() -> None:
    expected = {
        Operation.NOTEBOOK_CREATE: (notebook_rows.NOTEBOOK_CREATE, "deferred-product"),
        Operation.NOTEBOOK_UPDATE: (notebook_rows.NOTEBOOK_UPDATE, "deferred-product"),
        Operation.MIND_MAP_GENERATE_NOTE: (
            mind_map_rows.MIND_MAP_GENERATE_NOTE,
            "deferred-product",
        ),
        Operation.MIND_MAP_GENERATE_INTERACTIVE: (
            mind_map_rows.MIND_MAP_GENERATE_INTERACTIVE,
            "deferred-product",
        ),
        Operation.ARTIFACT_GENERATE_MIND_MAP: (
            mind_map_rows.ARTIFACT_GENERATE_MIND_MAP,
            "compatibility",
        ),
        Operation.ARTIFACT_LIST: (mind_map_rows.ARTIFACT_LIST, "compatibility"),
        Operation.ARTIFACT_GET: (mind_map_rows.ARTIFACT_GET, "compatibility"),
    }
    for operation, (row, category) in expected.items():
        assert isinstance(row, CustomBinding)
        assert WEB_BINDING_ROWS[operation] is row
        binding = WEB_OPERATION_REGISTRY[operation]
        assert binding.handler_name is None and binding.row is row
        assert row.category == category
        assert row.justification.strip()
        assert row.collaborators == ()
    assert notebook_rows.NOTEBOOK_CREATE.spec("create").select(None) == NativeChoice(
        RPCMethod.CREATE_NOTEBOOK
    )
    assert notebook_rows.NOTEBOOK_UPDATE.spec("readback").select(None) == NativeChoice(
        RPCMethod.GET_NOTEBOOK
    )
    assert mind_map_rows.ARTIFACT_GENERATE_MIND_MAP.spec("note_create").select(None) == (
        NativeChoice(RPCMethod.CREATE_NOTE, "plain")
    )
    for name in (
        "_notebook_create",
        "_notebook_update",
        "_notebook_limit_error",
        "_list_notebooks",
        "_mind_map_generate_note",
        "_mind_map_generate_interactive",
        "_mind_map_generate",
        "_persist_generated_mind_map",
        "_artifact_list",
        "_artifact_get",
    ):
        assert not hasattr(WebRpcBackend, name)
    with pytest.raises(ModuleNotFoundError):
        __import__("notebooklm._web.deadline_rpc")


# --- notebook.create --------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_snapshots_then_creates_with_retries_disabled() -> None:
    executor = _RecordingExecutor([], _CREATED_ROW)
    backend = build_web_backend(executor)

    result = await backend.invoke(
        NOTEBOOK_CREATE_DEF, NotebookCreateInput("Daily News"), deadline=None
    )

    assert (result.notebook.id, result.notebook.title) == ("nb-new", "Daily News")
    snapshot, create = executor.calls
    assert snapshot.method is RPCMethod.LIST_NOTEBOOKS
    assert snapshot.params == [None, 1, None, [2]]
    assert snapshot.kwargs == _kwargs("/")
    assert create.method is RPCMethod.CREATE_NOTEBOOK
    assert create.kwargs == _kwargs("/", disable_internal_retries=True)


@pytest.mark.asyncio
async def test_create_probes_after_a_dispatched_transport_loss() -> None:
    old_row = ["Daily News", [], "nb-old"]
    new_row = ["Daily News", [], "nb-landed"]
    executor = _RecordingExecutor(
        [[old_row]], ServerError("bad gateway", status_code=502), [[old_row, new_row]]
    )
    backend = build_web_backend(executor)

    result = await backend.invoke(
        NOTEBOOK_CREATE_DEF, NotebookCreateInput("Daily News"), deadline=None
    )

    assert result.notebook.id == "nb-landed"
    assert [call.method for call in executor.calls] == [
        RPCMethod.LIST_NOTEBOOKS,
        RPCMethod.CREATE_NOTEBOOK,
        RPCMethod.LIST_NOTEBOOKS,
    ]


@pytest.mark.asyncio
async def test_create_quota_rejection_reads_limits_then_the_catalog() -> None:
    quota = RPCError("quota", method_id=RPCMethod.CREATE_NOTEBOOK.value, rpc_code=3)
    executor = _RecordingExecutor([], quota, [[None, [None, 1]]], [])
    backend = build_web_backend(executor)

    with pytest.raises(BackendError) as caught:
        await backend.invoke(NOTEBOOK_CREATE_DEF, NotebookCreateInput("Daily News"), deadline=None)

    error = caught.value
    assert error.reason is BackendErrorReason.NOTEBOOK_LIMIT
    assert error.message == "notebook limit reached"
    assert error.operation is Operation.NOTEBOOK_CREATE
    assert error.diagnostics is not None
    assert error.diagnostics["limit"] == 1
    assert error.diagnostics["current_count"] == 0
    assert error.diagnostics["original_reason"] == BackendErrorReason.RPC.value
    assert [call.method for call in executor.calls] == [
        RPCMethod.LIST_NOTEBOOKS,
        RPCMethod.CREATE_NOTEBOOK,
        RPCMethod.GET_USER_SETTINGS,
        RPCMethod.LIST_NOTEBOOKS,
    ]
    assert executor.calls[2].kwargs == _kwargs("/")


@pytest.mark.asyncio
async def test_create_reconciliation_timeout_keeps_parent_attribution() -> None:
    clock = [0.0]
    executor = _RecordingExecutor(
        [],
        ServerError("create response lost", status_code=502),
        RPCTimeoutError("slow", method_id=RPCMethod.LIST_NOTEBOOKS.value, timeout_seconds=5.0),
    )
    backend = build_web_backend(executor)
    deadline = RuntimeDeadline(timeout=5.0, started_at=0.0, monotonic=lambda: clock[0])

    async def rpc_call(method: RPCMethod, params: list[Any], **kwargs: Any) -> Any:
        if len(executor.calls) == 2:
            clock[0] = 6.0
        return await _RecordingExecutor.rpc_call(executor, method, params, **kwargs)

    backend._runtime = type("Runtime", (), {"rpc_call": staticmethod(rpc_call)})()  # type: ignore[assignment]

    with pytest.raises(BackendDeadlineExceededError) as caught:
        await backend.invoke(
            NOTEBOOK_CREATE_DEF, NotebookCreateInput("Daily News"), deadline=deadline
        )

    assert caught.value.operation is Operation.NOTEBOOK_CREATE
    assert caught.value.outcome_unknown is True
    assert caught.value.dispatched is True
    assert all(call.kwargs["_retry_deadline"] is deadline for call in executor.calls)


# --- notebook.update --------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_mutates_then_reads_back_with_identical_kwargs() -> None:
    executor = _RecordingExecutor(None, [["Renamed", [], "nb-1"]])
    backend = build_web_backend(executor)

    result = await backend.invoke(
        NOTEBOOK_UPDATE_DEF, NotebookUpdateInput("nb-1", title="Renamed"), deadline=None
    )

    assert result.notebook.title == "Renamed"
    mutate, readback = executor.calls
    assert mutate.method is RPCMethod.RENAME_NOTEBOOK
    assert mutate.kwargs == _kwargs("/", allow_null=True)
    assert readback.method is RPCMethod.GET_NOTEBOOK
    assert readback.kwargs == _kwargs("/notebook/nb-1")


@pytest.mark.asyncio
async def test_update_not_found_readback_keeps_its_closed_identity() -> None:
    missing = ClientError("gone", method_id=RPCMethod.GET_NOTEBOOK.value, rpc_code=5)
    executor = _RecordingExecutor(None, missing)
    backend = build_web_backend(executor)

    with pytest.raises(BackendError) as caught:
        await backend.invoke(
            NOTEBOOK_UPDATE_DEF, NotebookUpdateInput("nb-1", title="Renamed"), deadline=None
        )

    error = caught.value
    assert error.reason is BackendErrorReason.NOTEBOOK_NOT_FOUND
    assert error.message == "Notebook not found: nb-1"
    assert error.diagnostics is not None
    assert error.diagnostics["notebook_id"] == "nb-1"
    assert error.diagnostics["method_id"] == RPCMethod.GET_NOTEBOOK.value
    assert error.diagnostics["detail"] == str(missing)
    assert isinstance(error.__cause__, ClientError)


@pytest.mark.asyncio
async def test_update_empty_readback_is_not_found() -> None:
    executor = _RecordingExecutor(None, [])
    backend = build_web_backend(executor)

    with pytest.raises(BackendError) as caught:
        await backend.invoke(
            NOTEBOOK_UPDATE_DEF, NotebookUpdateInput("nb-1", title="Renamed"), deadline=None
        )

    assert caught.value.reason is BackendErrorReason.NOTEBOOK_NOT_FOUND
    assert caught.value.diagnostics == {
        "notebook_id": "nb-1",
        "method_id": RPCMethod.GET_NOTEBOOK.value,
    }


@pytest.mark.asyncio
async def test_update_readback_pre_dispatch_expiry_is_commit_uncertain() -> None:
    clock = [11.0]
    executor = _RecordingExecutor(None)
    backend = build_web_backend(executor)
    deadline = RuntimeDeadline(timeout=5.0, started_at=10.0, monotonic=lambda: clock[0])

    async def rpc_call(method: RPCMethod, params: list[Any], **kwargs: Any) -> Any:
        clock[0] = 16.0
        return await _RecordingExecutor.rpc_call(executor, method, params, **kwargs)

    backend._runtime = type("Runtime", (), {"rpc_call": staticmethod(rpc_call)})()  # type: ignore[assignment]

    with pytest.raises(BackendDeadlineExceededError) as caught:
        await backend.invoke(
            NOTEBOOK_UPDATE_DEF, NotebookUpdateInput("nb-1", title="Renamed"), deadline=deadline
        )

    assert caught.value.outcome_unknown is True
    assert caught.value.dispatched is False
    assert may_have_committed(caught.value) is False
    assert caught.value.diagnostics is not None
    assert caught.value.diagnostics["method_id"] == RPCMethod.GET_NOTEBOOK.value
    assert len(executor.calls) == 1


# --- mind-map generate rows ---------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_note_defaults_sources_through_get_notebook() -> None:
    executor = _RecordingExecutor([[None, []]], [['{"name": "Tree"}']])
    backend = build_web_backend(executor)

    result = await backend.invoke(
        MIND_MAP_GENERATE_NOTE_DEF, MindMapGenerateNoteInput("nb-1"), deadline=None
    )

    assert result.tree_json == '{"name": "Tree"}'
    sources, generate = executor.calls
    assert sources.method is RPCMethod.GET_NOTEBOOK
    assert sources.kwargs == _kwargs("/notebook/nb-1")
    assert generate.method is RPCMethod.GENERATE_MIND_MAP
    assert generate.kwargs == _kwargs("/notebook/nb-1", allow_null=True)


@pytest.mark.asyncio
async def test_generate_note_with_sources_issues_one_call() -> None:
    executor = _RecordingExecutor([["tree"]])
    backend = build_web_backend(executor)

    await backend.invoke(
        MIND_MAP_GENERATE_NOTE_DEF,
        MindMapGenerateNoteInput("nb-1", source_ids=("s1",)),
        deadline=None,
    )

    assert [call.method for call in executor.calls] == [RPCMethod.GENERATE_MIND_MAP]


@pytest.mark.asyncio
async def test_generate_interactive_creates_an_artifact_or_reports_unavailable() -> None:
    executor = _RecordingExecutor([["mm-1"]])
    backend = build_web_backend(executor)

    result = await backend.invoke(
        MIND_MAP_GENERATE_INTERACTIVE_DEF,
        MindMapGenerateInteractiveInput("nb-1", source_ids=("s1",)),
        deadline=None,
    )
    assert result.mind_map_id == "mm-1"
    (create,) = executor.calls
    assert create.method is RPCMethod.CREATE_ARTIFACT
    assert create.kwargs == _kwargs("/notebook/nb-1", allow_null=True)

    executor = _RecordingExecutor(None)
    backend = build_web_backend(executor)
    with pytest.raises(BackendError) as caught:
        await backend.invoke(
            MIND_MAP_GENERATE_INTERACTIVE_DEF,
            MindMapGenerateInteractiveInput("nb-1", source_ids=("s1",)),
            deadline=None,
        )
    assert caught.value.reason is BackendErrorReason.ARTIFACT_FEATURE_UNAVAILABLE
    assert caught.value.diagnostics is not None
    assert caught.value.diagnostics["method_id"] == RPCMethod.CREATE_ARTIFACT.value


@pytest.mark.asyncio
async def test_artifact_mind_map_generation_persists_through_the_legacy_note_family() -> None:
    executor = _RecordingExecutor([['{"name": "Tree", "children": []}']], [["note-1"]], None)
    backend = build_web_backend(executor)

    result = await backend.invoke(
        ARTIFACT_GENERATE_MIND_MAP_DEF,
        MindMapGenerateInput("nb-1", source_ids=("s1",)),
        deadline=None,
    )

    assert result.note_id == "note-1"
    assert result.mind_map == {"name": "Tree", "children": []}
    generate, create, update = executor.calls
    assert generate.method is RPCMethod.GENERATE_MIND_MAP
    assert generate.kwargs == _kwargs("/notebook/nb-1", allow_null=True)
    assert create.method is RPCMethod.CREATE_NOTE
    assert create.params == ["nb-1", "", [1], None, "Tree"]
    assert create.kwargs == _kwargs("/notebook/nb-1", operation_variant="plain")
    assert update.method is RPCMethod.UPDATE_NOTE
    assert update.params == [
        "nb-1",
        "note-1",
        [[['{"name": "Tree", "children": []}', "Tree", [], 0]]],
    ]
    assert update.kwargs == _kwargs("/notebook/nb-1", allow_null=True)


@pytest.mark.asyncio
async def test_artifact_mind_map_generation_with_an_absent_leaf_persists_nothing() -> None:
    executor = _RecordingExecutor([])
    backend = build_web_backend(executor)

    result = await backend.invoke(
        ARTIFACT_GENERATE_MIND_MAP_DEF,
        MindMapGenerateInput("nb-1", source_ids=("s1",)),
        deadline=None,
    )

    assert (result.mind_map, result.note_id, result.created_at) == (None, None, None)
    assert [call.method for call in executor.calls] == [RPCMethod.GENERATE_MIND_MAP]


# --- catalog rows -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_artifact_list_merges_note_backed_mind_maps_with_identical_kwargs() -> None:
    executor = _RecordingExecutor([], [])
    backend = build_web_backend(executor)

    result = await backend.invoke(ARTIFACT_LIST_DEF, ArtifactListInput("nb-1"), deadline=None)

    assert result.artifacts == ()
    catalog, notes = executor.calls
    assert catalog.method is RPCMethod.LIST_ARTIFACTS
    assert catalog.kwargs == _kwargs("/notebook/nb-1", allow_null=True)
    assert notes.method is RPCMethod.GET_NOTES_AND_MIND_MAPS
    assert notes.params == ["nb-1"]
    assert notes.kwargs == _kwargs("/notebook/nb-1", allow_null=True)


@pytest.mark.asyncio
async def test_artifact_list_for_another_family_skips_the_merge() -> None:
    executor = _RecordingExecutor([])
    backend = build_web_backend(executor)

    await backend.invoke(
        ARTIFACT_LIST_DEF, ArtifactListInput("nb-1", family="audio"), deadline=None
    )

    assert [call.method for call in executor.calls] == [RPCMethod.LIST_ARTIFACTS]


@pytest.mark.asyncio
async def test_artifact_get_swallows_a_raw_merge_failure_into_partial_availability(
    caplog: pytest.LogCaptureFixture,
) -> None:
    request = httpx.Request("POST", "https://notebooklm.google.com/_/rpc")
    failure = httpx.HTTPStatusError(
        "authentication expired", request=request, response=httpx.Response(401, request=request)
    )
    executor = _RecordingExecutor([], failure)
    backend = build_web_backend(executor)

    with caplog.at_level(logging.WARNING, logger="notebooklm._artifact.listing"):
        result = await backend.invoke(
            ARTIFACT_GET_DEF, ArtifactGetInput("nb-1", "art-1"), deadline=None
        )

    assert result.artifact is None
    assert "Failed to fetch mind maps" in caplog.text
    assert len(executor.calls) == 2


@pytest.mark.asyncio
async def test_a_failed_catalog_read_is_translated_dispatched_and_tagged() -> None:
    executor = _RecordingExecutor(ServerError("boom", method_id=RPCMethod.LIST_ARTIFACTS.value))
    backend = build_web_backend(executor)

    with pytest.raises(BackendError) as caught:
        await backend.invoke(ARTIFACT_LIST_DEF, ArtifactListInput("nb-1"), deadline=None)

    error = caught.value
    assert error.operation is Operation.ARTIFACT_LIST
    assert error.reason is BackendErrorReason.SERVER
    assert error.dispatched is True
    assert error.__cause__.binding_native == NativeChoice(RPCMethod.LIST_ARTIFACTS)  # type: ignore[union-attr]
    assert len(executor.calls) == 1


# --- InvokerRpcCaller --------------------------------------------------------------


class _FakeInvoker:
    def __init__(self, response: object) -> None:
        self.response = response
        self.calls: list[tuple[str, CodecPayload, dict[str, Any]]] = []

    async def call(self, spec_key: str, payload: CodecPayload, **kwargs: Any) -> Any:
        self.calls.append((spec_key, payload, kwargs))
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response

    async def stream(self, spec_key: str, payload: CodecPayload, **kwargs: Any) -> Any:
        raise AssertionError("not used")

    def collaborator(self, name: str) -> Any:
        raise AssertionError("not used")


_SPECS = {
    RPCMethod.GET_NOTES_AND_MIND_MAPS: ("note_rows", None),
    RPCMethod.CREATE_NOTE: ("note_create", "plain"),
}


@pytest.mark.asyncio
async def test_invoker_caller_maps_methods_onto_declared_spec_keys() -> None:
    invoker = _FakeInvoker([])
    caller = InvokerRpcCaller(invoker, None, operation=Operation.ARTIFACT_LIST, spec_keys=_SPECS)

    result = await caller.rpc_call(
        RPCMethod.GET_NOTES_AND_MIND_MAPS, ["nb-1"], source_path="/notebook/nb-1", allow_null=True
    )

    assert result == []
    ((key, payload, kwargs),) = invoker.calls
    assert key == "note_rows"
    assert payload == CodecPayload(params=["nb-1"], source_path="/notebook/nb-1", allow_null=True)
    assert kwargs == {"deadline": None, "disable_internal_retries": False}


@pytest.mark.asyncio
async def test_invoker_caller_rejects_undeclared_methods_and_variant_drift() -> None:
    caller = InvokerRpcCaller(
        _FakeInvoker(None), None, operation=Operation.ARTIFACT_LIST, spec_keys=_SPECS
    )
    with pytest.raises(BackendContractError, match="declares no native spec for DELETE_NOTE"):
        await caller.rpc_call(RPCMethod.DELETE_NOTE, [])
    with pytest.raises(BackendContractError, match="variant 'plain', not None"):
        await caller.rpc_call(RPCMethod.CREATE_NOTE, [])


@pytest.mark.asyncio
async def test_invoker_caller_surfaces_expiry_as_a_timeout_outside_the_private_frame() -> None:
    expiry = BackendDeadlineExceededError(Operation.ARTIFACT_LIST)
    deadline = RuntimeDeadline(timeout=5.0, started_at=0.0, monotonic=lambda: 0.0)
    caller = InvokerRpcCaller(
        _FakeInvoker(expiry), deadline, operation=Operation.ARTIFACT_LIST, spec_keys=_SPECS
    )

    with pytest.raises(RPCTimeoutError) as caught:
        await caller.rpc_call(RPCMethod.GET_NOTES_AND_MIND_MAPS, ["nb-1"])

    error = caught.value
    assert error.method_id == RPCMethod.GET_NOTES_AND_MIND_MAPS.value
    assert error.timeout_seconds == 5.0
    # The public failure projector fails closed on a BackendError in the chain,
    # so the private deadline frame is deliberately not chained (the P6
    # ``DeadlineRpcCaller`` contract).
    assert error.__cause__ is None and error.__context__ is None


@pytest.mark.asyncio
async def test_invoker_caller_copies_a_native_tag_onto_the_timeout() -> None:
    class _Expiry(BackendDeadlineExceededError):
        __slots__ = ("binding_native",)

        def __init__(self) -> None:
            BackendDeadlineExceededError.__init__(self, Operation.ARTIFACT_LIST)
            object.__setattr__(
                self, "binding_native", NativeChoice(RPCMethod.GET_NOTES_AND_MIND_MAPS)
            )

    caller = InvokerRpcCaller(
        _FakeInvoker(_Expiry()), None, operation=Operation.ARTIFACT_LIST, spec_keys=_SPECS
    )
    with pytest.raises(RPCTimeoutError) as caught:
        await caller.rpc_call(RPCMethod.GET_NOTES_AND_MIND_MAPS, ["nb-1"])
    assert caught.value.binding_native == NativeChoice(RPCMethod.GET_NOTES_AND_MIND_MAPS)  # type: ignore[attr-defined]
    assert caught.value.timeout_seconds is None
