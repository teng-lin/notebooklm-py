"""Focused contracts for the migrated backend-neutral plain-note service."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from notebooklm._semantic.operations import Operation
from notebooklm._semantic.records import (
    NOTE_CREATE_DEF,
    NOTE_DELETE_DEF,
    NOTE_GET_DEF,
    NOTE_LIST_DEF,
    NOTE_UPDATE_DEF,
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
)
from notebooklm._semantic.services.note import NoteService
from tests._fixtures.recording_backend import BackendInvocation, RecordingBackend


@pytest.mark.asyncio
async def test_note_service_uses_only_typed_operations_and_returns_note_records() -> None:
    backend = RecordingBackend()
    record = NoteRecord("note", "notebook", "Title", "Body")
    backend.set_result(NOTE_LIST_DEF, NoteListResult((record,)))
    backend.set_result(NOTE_GET_DEF, NoteGetResult(record))
    backend.set_result(NOTE_CREATE_DEF, NoteCreateResult(record))
    backend.set_result(NOTE_UPDATE_DEF, NoteUpdateResult())
    backend.set_result(NOTE_DELETE_DEF, NoteDeleteResult())
    service = NoteService(backend)

    listed = await service.list_notes("notebook")
    assert [(note.id, note.notebook_id, note.title, note.content) for note in listed] == [
        ("note", "notebook", "Title", "Body")
    ]
    selected = await service.get_note_or_none("notebook", "note")
    assert selected is not None and selected.id == "note"
    created = await service.create_note_record("notebook", "Title", "Body")
    assert created.id == "note"
    assert await service.update_note("notebook", "note", "New body", "New title") is None
    assert await service.delete_note("notebook", "note") is None

    assert backend.invocations == [
        BackendInvocation(Operation.NOTE_LIST, NoteListInput("notebook"), None),
        BackendInvocation(Operation.NOTE_GET, NoteGetInput("notebook", "note"), None),
        BackendInvocation(
            Operation.NOTE_CREATE,
            NoteCreateInput("notebook", "Title", "Body"),
            None,
        ),
        BackendInvocation(
            Operation.NOTE_UPDATE,
            NoteUpdateInput("notebook", "note", "Body", "Title"),
            None,
        ),
        BackendInvocation(
            Operation.NOTE_UPDATE,
            NoteUpdateInput("notebook", "note", "New body", "New title"),
            None,
        ),
        BackendInvocation(Operation.NOTE_DELETE, NoteDeleteInput("notebook", "note"), None),
    ]


def test_semantic_note_service_class_is_transport_neutral() -> None:
    path = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "notebooklm"
        / "_semantic"
        / "services"
        / "note.py"
    )
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    service = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "NoteService"
    )
    names = {node.id for node in ast.walk(service) if isinstance(node, ast.Name)}
    attributes = {node.attr for node in ast.walk(service) if isinstance(node, ast.Attribute)}

    assert names.isdisjoint({"RPCMethod", "RpcCaller", "NoteRow", "safe_index"})
    assert attributes.isdisjoint({"rpc_call"})
    assert not any(
        isinstance(node, ast.Subscript)
        and isinstance(node.slice, ast.Constant)
        and isinstance(node.slice.value, int)
        for node in ast.walk(service)
    )
