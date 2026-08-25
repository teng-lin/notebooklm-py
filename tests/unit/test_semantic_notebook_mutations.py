"""Focused tests for the inert transport-neutral notebook mutation service."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from notebooklm._deadline import RuntimeDeadline
from notebooklm._notebook_mutation_service import NotebookMutationService
from notebooklm._operations import Operation
from notebooklm._records import (
    NOTEBOOK_ALLOCATE_DEF,
    NOTEBOOK_DELETE_DEF,
    NOTEBOOK_GET_DEF,
    NOTEBOOK_LIST_DEF,
    NOTEBOOK_PATCH_DEF,
    NOTEBOOK_REMOVE_RECENT_DEF,
    SETTINGS_GET_LIMITS_DEF,
    AccountLimitsRecord,
    NotebookAllocateInput,
    NotebookAllocateResult,
    NotebookDeleteInput,
    NotebookDeleteResult,
    NotebookGetInput,
    NotebookGetResult,
    NotebookListInput,
    NotebookListResult,
    NotebookPatchInput,
    NotebookPatchResult,
    NotebookRecord,
    NotebookRemoveRecentInput,
    NotebookRemoveRecentResult,
    SettingsGetLimitsResult,
)
from notebooklm.exceptions import ValidationError
from tests._fixtures.recording_backend import BackendInvocation, RecordingBackend


@pytest.mark.asyncio
async def test_mutation_service_records_typed_calls_deadline_and_projects_results() -> None:
    backend = RecordingBackend()
    created = NotebookRecord("nb-created", "Created")
    updated = NotebookRecord("nb-created", "Renamed")
    backend.set_result(NOTEBOOK_LIST_DEF, NotebookListResult(()))
    backend.set_result(NOTEBOOK_ALLOCATE_DEF, NotebookAllocateResult(created))
    backend.set_result(
        SETTINGS_GET_LIMITS_DEF,
        SettingsGetLimitsResult(AccountLimitsRecord()),
    )
    backend.set_result(NOTEBOOK_PATCH_DEF, NotebookPatchResult())
    backend.set_result(NOTEBOOK_GET_DEF, NotebookGetResult(updated))
    backend.set_result(NOTEBOOK_DELETE_DEF, NotebookDeleteResult())
    backend.set_result(NOTEBOOK_REMOVE_RECENT_DEF, NotebookRemoveRecentResult())
    service = NotebookMutationService(backend)
    deadline = RuntimeDeadline(timeout=5.0, started_at=10.0, monotonic=lambda: 11.0)

    created_model = await service.create("Created", deadline=deadline)
    updated_model = await service.update_title("nb-created", "Renamed", deadline=deadline)
    assert await service.delete("nb-created", deadline=deadline) is None
    assert await service.remove_from_recent("nb-created", deadline=deadline) is None

    assert (created_model.id, created_model.title) == ("nb-created", "Created")
    assert (updated_model.id, updated_model.title) == ("nb-created", "Renamed")
    assert backend.invocations == [
        BackendInvocation(Operation.NOTEBOOK_LIST, NotebookListInput(), deadline),
        BackendInvocation(
            Operation.NOTEBOOK_ALLOCATE,
            NotebookAllocateInput("Created"),
            deadline,
        ),
        BackendInvocation(
            Operation.NOTEBOOK_PATCH,
            NotebookPatchInput("nb-created", title="Renamed"),
            deadline,
        ),
        BackendInvocation(
            Operation.NOTEBOOK_GET,
            NotebookGetInput("nb-created", require_notebook=True),
            deadline,
        ),
        BackendInvocation(Operation.NOTEBOOK_DELETE, NotebookDeleteInput("nb-created"), deadline),
        BackendInvocation(
            Operation.NOTEBOOK_REMOVE_RECENT,
            NotebookRemoveRecentInput("nb-created"),
            deadline,
        ),
    ]


@pytest.mark.asyncio
async def test_empty_change_fails_before_backend_invocation() -> None:
    backend = RecordingBackend()
    backend.set_result(NOTEBOOK_PATCH_DEF, NotebookPatchResult())
    backend.set_result(NOTEBOOK_GET_DEF, NotebookGetResult(NotebookRecord("nb", "unused")))

    with pytest.raises(ValidationError, match="At least one"):
        await NotebookMutationService(backend).update("nb")

    assert backend.invocations == []


@pytest.mark.asyncio
async def test_empty_title_remains_a_valid_legacy_mutation_value() -> None:
    backend = RecordingBackend()
    backend.set_result(NOTEBOOK_PATCH_DEF, NotebookPatchResult())
    backend.set_result(NOTEBOOK_GET_DEF, NotebookGetResult(NotebookRecord("nb", "")))

    updated = await NotebookMutationService(backend).update_title("nb", "")

    assert updated.title == ""
    assert backend.invocations == [
        BackendInvocation(
            Operation.NOTEBOOK_PATCH,
            NotebookPatchInput("nb", title=""),
            None,
        ),
        BackendInvocation(
            Operation.NOTEBOOK_GET,
            NotebookGetInput("nb", require_notebook=True),
            None,
        ),
    ]


def test_mutation_service_is_transport_neutral_and_never_descends_raw_rows() -> None:
    path = (
        Path(__file__).resolve().parents[2] / "src" / "notebooklm" / "_notebook_mutation_service.py"
    )
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported_modules = {
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}

    assert not any(
        forbidden in module
        for module in imported_modules
        for forbidden in ("httpx", "rpc", "cli", "mcp", "server", "_row_adapters")
    )
    assert names.isdisjoint({"RPCMethod", "RpcCaller", "NotebookLMClient"})
    assert not any(isinstance(node, ast.Subscript) for node in ast.walk(tree))
