"""Focused P5.6 mind-map, data-table, and Drive-export compatibility tests."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest

from notebooklm._backend import BackendError, BackendErrorReason
from notebooklm._backend_compat import project_backend_error
from notebooklm._deadline import RuntimeDeadline
from notebooklm._operations import Operation
from notebooklm._read_services import NotebookReadService
from notebooklm._records import (
    ARTIFACT_EXPORT_DEF,
    ARTIFACT_GENERATE_DATA_TABLE_DEF,
    DataTableGenerateInput,
    DataTableGenerateRequest,
    DriveExportInput,
    MindMapGenerateInput,
)
from notebooklm._studio import (
    DataTableFamilyService,
    NoteBackedMindMapFamilyService,
    StudioCatalog,
    StudioGenerationInputs,
)
from notebooklm._web.backend import WebRpcBackend
from notebooklm._web.codec.artifact_payloads import (
    build_data_table_artifact_params,
    build_mind_map_params,
)
from notebooklm.exceptions import ArtifactFeatureUnavailableError, ServerError
from notebooklm.rpc import RPCMethod


@dataclass(frozen=True)
class _Call:
    method: RPCMethod
    params: list[Any]
    kwargs: dict[str, Any]


class _Executor:
    def __init__(self, *responses: object) -> None:
        self.responses = list(responses)
        self.calls: list[_Call] = []

    async def rpc_call(self, method: RPCMethod, params: list[Any], **kwargs: Any) -> Any:
        self.calls.append(_Call(method, params, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def _backend(executor: _Executor) -> WebRpcBackend:
    return WebRpcBackend(executor)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_data_table_generate_preserves_payload_and_generation_status() -> None:
    executor = _Executor([["task-table", None, None, None, 1]])
    deadline = RuntimeDeadline(timeout=5.0, started_at=10.0, monotonic=lambda: 11.0)
    result = await _backend(executor).invoke(
        ARTIFACT_GENERATE_DATA_TABLE_DEF,
        DataTableGenerateInput("nb", ("src-a",), "fr", "compare"),
        deadline=deadline,
    )

    assert (result.status.task_id, result.status.status) == ("task-table", "pending")
    assert executor.calls[0].method is RPCMethod.CREATE_ARTIFACT
    assert executor.calls[0].params == build_data_table_artifact_params(
        "nb", ["src-a"], language="fr", instructions="compare"
    )
    assert executor.calls[0].kwargs["raise_on_null_status"] is True
    assert executor.calls[0].kwargs["_retry_deadline"] is deadline


@pytest.mark.asyncio
async def test_omitted_generation_sources_perform_one_recency_read_and_preserve_drive_ids() -> None:
    notebook = [
        [
            "nb",
            [
                [["src-a"], "Plain"],
                [[None, True, ["drive-b"]], "Drive"],
            ],
        ]
    ]
    executor = _Executor(notebook, [["task-table", None, None, None, 1]])
    backend = _backend(executor)
    # R5.1a: the service resolves the omitted source set, so the read is above
    # the port and the row itself dispatches only the kickoff.
    service = DataTableFamilyService(
        backend, StudioCatalog(backend), StudioGenerationInputs(NotebookReadService(backend))
    )
    await service.generate(DataTableGenerateRequest("nb"), deadline=None)

    assert [call.method for call in executor.calls] == [
        RPCMethod.GET_NOTEBOOK,
        RPCMethod.CREATE_ARTIFACT,
    ]
    assert executor.calls[1].params == build_data_table_artifact_params(
        "nb", ["src-a", "drive-b"], language="en", instructions=None
    )


@pytest.mark.asyncio
async def test_data_table_null_reconstructs_public_feature_error() -> None:
    executor = _Executor(None)
    backend = _backend(executor)
    with pytest.raises(BackendError) as caught:
        await backend.invoke(
            ARTIFACT_GENERATE_DATA_TABLE_DEF,
            DataTableGenerateInput("nb", ("src-a",), "en"),
            deadline=None,
        )

    projected = project_backend_error(caught.value)
    assert isinstance(projected, ArtifactFeatureUnavailableError)
    assert projected.artifact_type == "data table"
    assert projected.method_id == RPCMethod.CREATE_ARTIFACT.value


def _mind_map_family(executor: _Executor) -> NoteBackedMindMapFamilyService:
    """The service that owns ``artifact.generate_mind_map`` since P10 R4.2."""

    backend = _backend(executor)
    return NoteBackedMindMapFamilyService(backend, StudioCatalog(backend))


@pytest.mark.asyncio
async def test_mind_map_generate_persists_json_through_plain_note_variant() -> None:
    tree = {"name": "Roadmap", "children": [{"name": "Milestone"}]}
    executor = _Executor(
        [[tree]],
        [["note-1"]],
        None,
    )
    deadline = RuntimeDeadline(timeout=5.0, started_at=10.0, monotonic=lambda: 11.0)
    result = await _mind_map_family(executor).generate(
        MindMapGenerateInput("nb", ("src-a",), "en", "focus"),
        deadline=deadline,
    )

    assert result.mind_map == tree
    assert result.note_id == "note-1"
    assert [call.method for call in executor.calls] == [
        RPCMethod.GENERATE_MIND_MAP,
        RPCMethod.CREATE_NOTE,
        RPCMethod.UPDATE_NOTE,
    ]
    assert executor.calls[0].params == build_mind_map_params(
        ["src-a"], language="en", instructions="focus"
    )
    assert executor.calls[1].kwargs["operation_variant"] == "plain"
    assert executor.calls[2].params[1] == "note-1"
    # One budget for the whole workflow, as the composite row had for its natives.
    assert all(call.kwargs["_retry_deadline"] is deadline for call in executor.calls)


@pytest.mark.asyncio
async def test_mind_map_absent_leaf_preserves_empty_success() -> None:
    executor = _Executor(None)
    result = await _mind_map_family(executor).generate(
        MindMapGenerateInput("nb", ()),
        deadline=None,
    )
    assert result.mind_map is None
    assert result.note_id is None
    assert len(executor.calls) == 1


@pytest.mark.asyncio
async def test_mind_map_generate_names_the_workflow_not_the_failing_leaf() -> None:
    """A leaf failure keeps the composite's public identity (P10 R4.2)."""

    executor = _Executor(ServerError("boom", method_id=RPCMethod.GENERATE_MIND_MAP.value))

    with pytest.raises(BackendError) as caught:
        await _mind_map_family(executor).generate(MindMapGenerateInput("nb", ("src-a",)))

    assert caught.value.operation is Operation.ARTIFACT_GENERATE_MIND_MAP
    assert caught.value.diagnostics["leaf_operation"] is Operation.MIND_MAP_GENERATE_NOTE
    assert caught.value.reason is BackendErrorReason.SERVER


@pytest.mark.asyncio
async def test_mind_map_generate_cancellation_schedules_orphan_cleanup() -> None:
    """The cancel-safety gate for the persistence choreography (audit §28).

    ``artifact.generate_mind_map`` sequences ``NoteService.create_note_record``,
    so a cancel arriving while the finalizing ``note.update`` is in flight must
    still leave the shielded update to complete and then issue the best-effort
    ``note.delete`` — a generated mind map is never half-persisted.
    """

    update_started = asyncio.Event()
    update_can_finish = asyncio.Event()
    delete_started = asyncio.Event()
    methods: list[RPCMethod] = []

    class _GatedExecutor:
        async def rpc_call(self, method: RPCMethod, params: list[Any], **_: Any) -> Any:
            methods.append(method)
            if method is RPCMethod.GENERATE_MIND_MAP:
                return [[{"name": "Roadmap", "children": []}]]
            if method is RPCMethod.CREATE_NOTE:
                return [["note-orphan"]]
            if method is RPCMethod.UPDATE_NOTE:
                update_started.set()
                await update_can_finish.wait()
                return None
            if method is RPCMethod.DELETE_NOTE:
                assert params == ["nb", None, ["note-orphan"]]
                delete_started.set()
                return None
            return None

    backend = WebRpcBackend(_GatedExecutor())  # type: ignore[arg-type]
    service = NoteBackedMindMapFamilyService(backend, StudioCatalog(backend))

    task = asyncio.create_task(service.generate(MindMapGenerateInput("nb", ("src-a",))))
    await asyncio.wait_for(update_started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1)

    # Ordered cleanup: delete may not race the still-shielded update.
    assert not delete_started.is_set()
    update_can_finish.set()
    await asyncio.wait_for(delete_started.wait(), timeout=1)
    assert methods == [
        RPCMethod.GENERATE_MIND_MAP,
        RPCMethod.CREATE_NOTE,
        RPCMethod.UPDATE_NOTE,
        RPCMethod.DELETE_NOTE,
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("destination", "code"),
    [("docs", 1), ("sheets", 2)],
)
async def test_drive_export_preserves_opaque_response_and_exact_destination(
    destination: str,
    code: int,
) -> None:
    response = [f"https://drive.example/{destination}"]
    executor = _Executor(response)
    result = await _backend(executor).invoke(
        ARTIFACT_EXPORT_DEF,
        DriveExportInput("nb", "artifact", None, "Title", destination),
        deadline=None,
    )

    assert result.value is response
    assert executor.calls[0].method is RPCMethod.EXPORT_ARTIFACT
    assert executor.calls[0].params == [None, "artifact", None, "Title", code]
    assert executor.calls[0].kwargs["allow_null"] is True


def test_sensitive_generation_export_payloads_are_absent_from_record_reprs() -> None:
    assert "secret instructions" not in repr(
        DataTableGenerateInput("nb", (), "en", instructions="secret instructions")
    )
    assert "secret instructions" not in repr(
        MindMapGenerateInput("nb", instructions="secret instructions")
    )
    assert "private content" not in repr(DriveExportInput("nb", content="private content"))
