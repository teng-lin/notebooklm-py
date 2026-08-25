"""Focused P5.6 mind-map, data-table, and Drive-export compatibility tests."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from notebooklm._artifact.payloads import build_data_table_artifact_params, build_mind_map_params
from notebooklm._backend import BackendError
from notebooklm._backend_compat import project_backend_error
from notebooklm._deadline import RuntimeDeadline
from notebooklm._records import (
    ARTIFACT_EXPORT_DEF,
    ARTIFACT_GENERATE_DATA_TABLE_DEF,
    ARTIFACT_GENERATE_MIND_MAP_DEF,
    DataTableGenerateInput,
    DriveExportInput,
    MindMapGenerateInput,
)
from notebooklm._web.backend import WebRpcBackend
from notebooklm.exceptions import ArtifactFeatureUnavailableError
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
    await _backend(executor).invoke(
        ARTIFACT_GENERATE_DATA_TABLE_DEF,
        DataTableGenerateInput("nb"),
        deadline=None,
    )

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
            DataTableGenerateInput("nb", ("src-a",)),
            deadline=None,
        )

    projected = project_backend_error(caught.value)
    assert isinstance(projected, ArtifactFeatureUnavailableError)
    assert projected.artifact_type == "data table"
    assert projected.method_id == RPCMethod.CREATE_ARTIFACT.value


@pytest.mark.asyncio
async def test_mind_map_generate_persists_json_through_plain_note_variant() -> None:
    tree = {"name": "Roadmap", "children": [{"name": "Milestone"}]}
    executor = _Executor(
        [[tree]],
        [["note-1"]],
        None,
    )
    deadline = RuntimeDeadline(timeout=5.0, started_at=10.0, monotonic=lambda: 11.0)
    result = await _backend(executor).invoke(
        ARTIFACT_GENERATE_MIND_MAP_DEF,
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
    assert all(call.kwargs["_retry_deadline"] is deadline for call in executor.calls)


@pytest.mark.asyncio
async def test_mind_map_absent_leaf_preserves_empty_success() -> None:
    executor = _Executor(None)
    result = await _backend(executor).invoke(
        ARTIFACT_GENERATE_MIND_MAP_DEF,
        MindMapGenerateInput("nb", ()),
        deadline=None,
    )
    assert result.mind_map is None
    assert result.note_id is None
    assert len(executor.calls) == 1


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
        DataTableGenerateInput("nb", instructions="secret instructions")
    )
    assert "secret instructions" not in repr(
        MindMapGenerateInput("nb", instructions="secret instructions")
    )
    assert "private content" not in repr(DriveExportInput("nb", content="private content"))
