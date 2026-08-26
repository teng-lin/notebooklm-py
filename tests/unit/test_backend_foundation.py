"""Focused contract tests for the private semantic backend foundation."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone
from typing import cast

import pytest

from notebooklm._backend import (
    BackendAdapter,
    BackendCapabilities,
    BackendContractError,
    BackendDeadlineExceededError,
    BackendError,
    BackendErrorReason,
    BackendKind,
    UnsupportedOperationError,
)
from notebooklm._deadline import RuntimeDeadline
from notebooklm._operations import CallPolicy, Operation, OperationDef
from notebooklm._semantic.records import (
    MIND_MAP_DELETE_DEF,
    MIND_MAP_GENERATE_INTERACTIVE_DEF,
    MIND_MAP_GENERATE_NOTE_DEF,
    MIND_MAP_GET_DEF,
    MIND_MAP_LIST_DEF,
    MIND_MAP_UPDATE_DEF,
    NOTE_CREATE_DEF,
    NOTE_DELETE_DEF,
    NOTE_GET_DEF,
    NOTE_LIST_DEF,
    NOTE_UPDATE_DEF,
    NOTEBOOK_GET_DEF,
    NOTEBOOK_LIST_DEF,
    SOURCE_GET_DEF,
    SOURCE_LIST_DEF,
    MindMapDeleteInput,
    MindMapDeleteResult,
    MindMapGenerateInteractiveInput,
    MindMapGenerateInteractiveResult,
    MindMapGenerateNoteInput,
    MindMapGenerateNoteResult,
    MindMapGetInput,
    MindMapGetResult,
    MindMapListInput,
    MindMapListResult,
    MindMapRecord,
    MindMapUpdateInput,
    MindMapUpdateResult,
    NotebookChatSessionRecord,
    NotebookChatSettingsRecord,
    NotebookGetInput,
    NotebookGetResult,
    NotebookListInput,
    NotebookListResult,
    NotebookPremiumFeaturesRecord,
    NotebookRecord,
    NoteCreateInput,
    NoteCreateResult,
    NoteDeleteInput,
    NoteDeleteResult,
    NoteGetInput,
    NoteGetResult,
    NoteListInput,
    NoteListResult,
    NoteRecord,
    NoteUpdateInput,
    NoteUpdateResult,
    SourceGetInput,
    SourceGetResult,
    SourceListInput,
    SourceListResult,
    SourceRecord,
)
from tests._fixtures.recording_backend import RecordingBackend


def test_backend_vocabulary_is_closed_hashable_and_runtime_checkable() -> None:
    capabilities = BackendCapabilities(
        frozenset({Operation.NOTEBOOK_LIST, Operation.SOURCE_GET}),
        workflows=frozenset({Operation.NOTEBOOK_CREATE}),
    )
    backend = RecordingBackend(kind=BackendKind.WEB)

    assert {kind.value for kind in BackendKind} == {"web", "mobile"}
    assert capabilities.supports(Operation.NOTEBOOK_LIST)
    assert not capabilities.supports(Operation.NOTEBOOK_GET)
    # A service-owned workflow is available through the client but never
    # directly invokable; see tests/unit/test_semantic_capabilities.py.
    assert not capabilities.supports(Operation.NOTEBOOK_CREATE)
    assert capabilities.available(Operation.NOTEBOOK_CREATE)
    assert capabilities.available(Operation.NOTEBOOK_LIST)
    assert not capabilities.available(Operation.NOTEBOOK_GET)
    assert capabilities == replace(capabilities)
    assert hash(capabilities) == hash(replace(capabilities))
    assert isinstance(backend, BackendAdapter)


def test_read_slice_records_are_frozen_slotted_values_with_typed_definitions() -> None:
    timestamp = datetime(2026, 8, 23, tzinfo=timezone.utc)
    notebook = NotebookRecord(
        id="notebook-id",
        title="Notebook",
        created_at=timestamp,
        role="owner",
        premium_features=NotebookPremiumFeaturesRecord(True, False, None),
        chat_sessions=(NotebookChatSessionRecord("session-id"),),
        chat_settings=NotebookChatSettingsRecord("custom", "long", "Be precise"),
    )
    source = SourceRecord(
        id="source-id",
        title="Source",
        kind="pdf",
        status="ready",
        created_at=timestamp,
    )
    note = NoteRecord("note-id", "notebook-id", "Note", "Body", timestamp)
    mind_map = MindMapRecord(
        "mind-map-id",
        "notebook-id",
        "Mind Map",
        "note_backed",
        timestamp,
        '{"name":"Mind Map","children":[]}',
    )
    values = (
        NotebookListInput(),
        NotebookListResult((notebook,)),
        NotebookGetInput("notebook-id"),
        NotebookGetResult(notebook),
        SourceListInput(
            "notebook-id",
            strict=True,
            statuses=frozenset({"ready"}),
            kinds=frozenset({"pdf"}),
        ),
        SourceListResult((source,)),
        SourceGetInput("notebook-id", "source-id"),
        SourceGetResult(source),
        NoteListInput("notebook-id"),
        NoteListResult((note,)),
        NoteGetInput("notebook-id", "note-id"),
        NoteGetResult(note),
        NoteCreateInput("notebook-id", "Note", "Body"),
        NoteCreateResult(note),
        NoteUpdateInput("notebook-id", "note-id", "New body", "New title"),
        NoteUpdateResult(),
        NoteDeleteInput("notebook-id", "note-id"),
        NoteDeleteResult(),
        MindMapListInput("notebook-id"),
        MindMapListResult((mind_map,)),
        MindMapGetInput("notebook-id", "mind-map-id"),
        MindMapGetResult(mind_map.tree_json),
        MindMapGenerateNoteInput("notebook-id", ("source-id",), "en", "focus"),
        MindMapGenerateNoteResult(mind_map.tree_json),
        MindMapGenerateInteractiveInput("notebook-id", ("source-id",), "focus"),
        MindMapGenerateInteractiveResult("mind-map-id"),
        MindMapUpdateInput("notebook-id", "mind-map-id", "Renamed"),
        MindMapUpdateResult(),
        MindMapDeleteInput("notebook-id", "mind-map-id"),
        MindMapDeleteResult(),
    )

    assert all(not hasattr(value, "__dict__") for value in values)
    assert all(value == replace(value) for value in values)
    assert all(hash(value) == hash(replace(value)) for value in values)
    with pytest.raises(FrozenInstanceError):
        notebook.__setattr__("title", "changed")

    definitions = {
        NOTEBOOK_LIST_DEF: (
            Operation.NOTEBOOK_LIST,
            CallPolicy.READ,
            NotebookListInput,
            NotebookListResult,
        ),
        NOTEBOOK_GET_DEF: (
            Operation.NOTEBOOK_GET,
            CallPolicy.MUTATION,
            NotebookGetInput,
            NotebookGetResult,
        ),
        SOURCE_LIST_DEF: (
            Operation.SOURCE_LIST,
            CallPolicy.MUTATION,
            SourceListInput,
            SourceListResult,
        ),
        SOURCE_GET_DEF: (
            Operation.SOURCE_GET,
            CallPolicy.MUTATION,
            SourceGetInput,
            SourceGetResult,
        ),
        NOTE_LIST_DEF: (Operation.NOTE_LIST, CallPolicy.READ, NoteListInput, NoteListResult),
        NOTE_GET_DEF: (Operation.NOTE_GET, CallPolicy.READ, NoteGetInput, NoteGetResult),
        NOTE_CREATE_DEF: (
            Operation.NOTE_CREATE,
            CallPolicy.MUTATION,
            NoteCreateInput,
            NoteCreateResult,
        ),
        NOTE_UPDATE_DEF: (
            Operation.NOTE_UPDATE,
            CallPolicy.MUTATION,
            NoteUpdateInput,
            NoteUpdateResult,
        ),
        NOTE_DELETE_DEF: (
            Operation.NOTE_DELETE,
            CallPolicy.MUTATION,
            NoteDeleteInput,
            NoteDeleteResult,
        ),
        MIND_MAP_LIST_DEF: (
            Operation.MIND_MAP_LIST,
            CallPolicy.READ,
            MindMapListInput,
            MindMapListResult,
        ),
        MIND_MAP_GET_DEF: (
            Operation.MIND_MAP_GET,
            CallPolicy.READ,
            MindMapGetInput,
            MindMapGetResult,
        ),
        MIND_MAP_GENERATE_NOTE_DEF: (
            Operation.MIND_MAP_GENERATE_NOTE,
            CallPolicy.STATEFUL_START,
            MindMapGenerateNoteInput,
            MindMapGenerateNoteResult,
        ),
        MIND_MAP_GENERATE_INTERACTIVE_DEF: (
            Operation.MIND_MAP_GENERATE_INTERACTIVE,
            CallPolicy.STATEFUL_START,
            MindMapGenerateInteractiveInput,
            MindMapGenerateInteractiveResult,
        ),
        MIND_MAP_UPDATE_DEF: (
            Operation.MIND_MAP_UPDATE,
            CallPolicy.MUTATION,
            MindMapUpdateInput,
            MindMapUpdateResult,
        ),
        MIND_MAP_DELETE_DEF: (
            Operation.MIND_MAP_DELETE,
            CallPolicy.MUTATION,
            MindMapDeleteInput,
            MindMapDeleteResult,
        ),
    }
    for definition, (key, policy, input_type, output_type) in definitions.items():
        assert definition.key is key
        assert definition.policy is policy
        assert definition.input_type is input_type
        assert definition.output_type is output_type


@pytest.mark.asyncio
async def test_recording_backend_validates_and_records_typed_calls_and_deadline() -> None:
    result = NotebookListResult((NotebookRecord("notebook-id", "Notebook"),))
    deadline = RuntimeDeadline(timeout=5.0, started_at=10.0, monotonic=lambda: 11.0)
    backend = RecordingBackend()
    backend.set_result(NOTEBOOK_LIST_DEF, result)

    assert await backend.invoke(NOTEBOOK_LIST_DEF, NotebookListInput(), deadline=deadline) == result
    assert backend.invocations[0].operation is Operation.NOTEBOOK_LIST
    assert backend.invocations[0].value == NotebookListInput()
    assert backend.invocations[0].deadline is deadline
    assert backend.capabilities == BackendCapabilities(frozenset({Operation.NOTEBOOK_LIST}))

    await backend.close()
    assert backend.closed


@pytest.mark.asyncio
async def test_recording_backend_rejects_unsupported_and_invalid_calls_before_recording() -> None:
    backend = RecordingBackend(kind=BackendKind.MOBILE)

    with pytest.raises(UnsupportedOperationError) as unsupported:
        await backend.invoke(NOTEBOOK_LIST_DEF, NotebookListInput(), deadline=None)
    assert unsupported.value.operation is Operation.NOTEBOOK_LIST
    assert unsupported.value.backend_kind is BackendKind.MOBILE
    assert backend.invocations == []

    result = NotebookListResult(())
    backend.set_result(NOTEBOOK_LIST_DEF, result)
    with pytest.raises(BackendContractError, match="input must be NotebookListInput"):
        await backend.invoke(
            NOTEBOOK_LIST_DEF,
            cast(NotebookListInput, "wrong input"),
            deadline=None,
        )
    assert backend.invocations == []

    forged = OperationDef(
        Operation.NOTEBOOK_LIST,
        CallPolicy.READ,
        NotebookListInput,
        NotebookGetResult,
    )
    with pytest.raises(BackendContractError, match="unregistered operation definition"):
        await backend.invoke(forged, NotebookListInput(), deadline=None)
    assert backend.invocations == []

    with pytest.raises(TypeError, match="result must be NotebookListResult"):
        backend.set_result(
            NOTEBOOK_LIST_DEF,
            cast(NotebookListResult, SourceListResult(())),
        )


def test_backend_error_record_preserves_scrubbed_diagnostics_and_timeout_semantics() -> None:
    diagnostics: dict[str, object] = {
        "method_id": "opaque-method",
        "rpc_code": 5,
        "found_ids": ("safe-id",),
        "raw_response": "scrubbed",
    }
    error = BackendError(
        "backend failed",
        operation=Operation.NOTEBOOK_GET,
        diagnostics=diagnostics,
    )
    deadline_error = BackendDeadlineExceededError(
        Operation.SOURCE_GET,
        outcome_unknown=True,
        diagnostics=diagnostics,
    )

    assert str(error) == "backend failed"
    assert error.args == ("backend failed",)
    assert error.diagnostics is diagnostics
    assert error == replace(error)
    assert hash(error) == hash(replace(error))
    assert deadline_error.operation is Operation.SOURCE_GET
    assert deadline_error.reason is BackendErrorReason.TIMEOUT
    assert deadline_error.outcome_unknown
    assert deadline_error.diagnostics is diagnostics
    assert "source.get" in str(deadline_error)
