"""Focused contracts for the two backend-neutral mind-map services."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from notebooklm._semantic.operations import Operation
from notebooklm._semantic.records import (
    MIND_MAP_DELETE_DEF,
    MIND_MAP_GENERATE_INTERACTIVE_DEF,
    MIND_MAP_GENERATE_NOTE_DEF,
    MIND_MAP_GET_DEF,
    MIND_MAP_LIST_DEF,
    MIND_MAP_UPDATE_DEF,
    NOTE_CREATE_DEF,
    NOTE_DELETE_DEF,
    NOTE_UPDATE_DEF,
    ArtifactRecord,
    MindMapDeleteInput,
    MindMapDeleteResult,
    MindMapGenerateInteractiveInput,
    MindMapGenerateInteractiveResult,
    MindMapGenerateNoteInput,
    MindMapGenerateNoteResult,
    MindMapGenerateOutcomeRecord,
    MindMapGetInput,
    MindMapGetResult,
    MindMapListInput,
    MindMapListResult,
    MindMapRecord,
    MindMapUpdateInput,
    MindMapUpdateResult,
    NoteCreateInput,
    NoteCreateResult,
    NoteDeleteInput,
    NoteDeleteResult,
    NoteRecord,
    NoteUpdateInput,
    NoteUpdateResult,
)
from notebooklm._semantic.services.note import NoteService
from notebooklm._studio import MindMapFamilyService, StudioCatalog
from notebooklm.types import MindMapKind
from tests._fixtures.recording_backend import (
    BackendInvocation,
    RecordingBackend,
    set_studio_catalog,
)


@pytest.mark.asyncio
async def test_note_service_generates_and_persists_exact_mind_map_shape() -> None:
    created_at = datetime(2026, 8, 24, tzinfo=timezone.utc)
    tree_json = '{"name":"Generated Map","children":[{"name":"Leaf"}]}'
    backend = RecordingBackend()
    backend.set_result(MIND_MAP_GENERATE_NOTE_DEF, MindMapGenerateNoteResult(tree_json))
    backend.set_result(
        NOTE_CREATE_DEF,
        NoteCreateResult(
            NoteRecord("note-id", "notebook-id", "Generated Map", tree_json, created_at)
        ),
    )
    backend.set_result(NOTE_UPDATE_DEF, NoteUpdateResult())

    result = await NoteService(backend).generate_mind_map(
        "notebook-id",
        ["source-id"],
        "en",
        "focus",
    )

    # R6.6: the service returns the neutral record carrying the exact persisted
    # JSON; ``MindMapsAPI.generate`` projects it, and
    # ``test_mind_maps_api.py::test_generate_note_backed_delegates`` asserts the
    # public ``MindMap`` (including the parsed ``tree``) that comes out.
    assert (
        result.id,
        result.notebook_id,
        result.title,
        result.kind,
        result.created_at,
        result.tree_json,
    ) == (
        "note-id",
        "notebook-id",
        "Generated Map",
        MindMapKind.NOTE_BACKED.value,
        created_at,
        tree_json,
    )
    assert backend.invocations == [
        BackendInvocation(
            Operation.MIND_MAP_GENERATE_NOTE,
            MindMapGenerateNoteInput("notebook-id", ("source-id",), "en", "focus"),
            None,
        ),
        BackendInvocation(
            Operation.NOTE_CREATE,
            NoteCreateInput("notebook-id", "Generated Map", tree_json),
            None,
        ),
        BackendInvocation(
            Operation.NOTE_UPDATE,
            NoteUpdateInput("notebook-id", "note-id", tree_json, "Generated Map"),
            None,
        ),
    ]


@pytest.mark.asyncio
async def test_note_service_renames_without_reencoding_tree_and_deletes_idempotently() -> None:
    tree_json = '{ "name": "Original", "children": [] }'
    record = MindMapRecord(
        "mind-map-id",
        "notebook-id",
        "Original",
        "note_backed",
        tree_json=tree_json,
    )
    backend = RecordingBackend()
    backend.set_result(MIND_MAP_LIST_DEF, MindMapListResult((record,)))
    backend.set_result(NOTE_UPDATE_DEF, NoteUpdateResult())
    backend.set_result(NOTE_DELETE_DEF, NoteDeleteResult())
    service = NoteService(backend)

    await service.rename_mind_map("notebook-id", "mind-map-id", "Renamed")
    await service.delete_mind_map("notebook-id", "mind-map-id")

    assert backend.invocations == [
        BackendInvocation(Operation.MIND_MAP_LIST, MindMapListInput("notebook-id"), None),
        BackendInvocation(
            Operation.NOTE_UPDATE,
            NoteUpdateInput("notebook-id", "mind-map-id", tree_json, "Renamed"),
            None,
        ),
        BackendInvocation(
            Operation.NOTE_DELETE,
            NoteDeleteInput("notebook-id", "mind-map-id"),
            None,
        ),
    ]


@pytest.mark.asyncio
async def test_interactive_service_uses_studio_catalog_and_typed_family_operations() -> None:
    created_at = datetime(2026, 8, 24, tzinfo=timezone.utc)
    record = ArtifactRecord(
        "mind-map-id",
        "Generated Map",
        "mind_map",
        "completed",
        variant="interactive_mind_map",
        created_at=created_at,
    )
    backend = RecordingBackend()
    set_studio_catalog(backend, (record,))
    backend.set_result(
        MIND_MAP_GENERATE_INTERACTIVE_DEF,
        MindMapGenerateInteractiveResult("mind-map-id"),
    )

    backend.set_result(
        MIND_MAP_GET_DEF,
        MindMapGetResult('{"name":"Generated Map","children":[]}'),
    )
    backend.set_result(MIND_MAP_UPDATE_DEF, MindMapUpdateResult())
    backend.set_result(MIND_MAP_DELETE_DEF, MindMapDeleteResult())
    wait_for_completion = AsyncMock()
    service = MindMapFamilyService(
        backend,
        StudioCatalog(backend),
        wait_for_completion=wait_for_completion,
    )

    listed = await service.list_mind_maps("notebook-id")
    generated = await service.generate(
        "notebook-id",
        ["source-id"],
        "focus",
        wait=True,
    )
    await service.rename("notebook-id", "mind-map-id", "Renamed")
    await service.delete("notebook-id", "mind-map-id")

    # ``list_mind_maps``/``generate`` stay record-only here (P10 I1); public
    # ``MindMap`` projection is ``MindMapsAPI``'s job — see
    # ``tests/unit/test_mind_maps_api.py`` for the projected assertions this
    # test previously carried.
    assert [(item.id, item.variant) for item in listed] == [("mind-map-id", "interactive_mind_map")]
    assert generated == MindMapGenerateOutcomeRecord(
        mind_map_id="mind-map-id",
        record=record,
        tree={"name": "Generated Map", "children": []},
    )
    wait_for_completion.assert_awaited_once_with("notebook-id", "mind-map-id")
    # The listing above is two invocations: the catalog read and its merge.
    assert backend.invocations[2].value == MindMapGenerateInteractiveInput(
        "notebook-id", ("source-id",), "focus"
    )
    assert backend.invocations[-3:] == [
        BackendInvocation(
            Operation.MIND_MAP_GET,
            MindMapGetInput("notebook-id", "mind-map-id"),
            None,
        ),
        BackendInvocation(
            Operation.MIND_MAP_UPDATE,
            MindMapUpdateInput("notebook-id", "mind-map-id", "Renamed"),
            None,
        ),
        BackendInvocation(
            Operation.MIND_MAP_DELETE,
            MindMapDeleteInput("notebook-id", "mind-map-id"),
            None,
        ),
    ]
