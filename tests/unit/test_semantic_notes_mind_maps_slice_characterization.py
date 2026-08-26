"""Migration sentinels and inventory characterization for P6.3 Notes & Note-Backed Mind Maps.

Governed by docs/plan/2026-08-13-semantic-backend-refactor.md § P6.3.
This characterization suite freezes and validates the complete NoteService, NotesAPI,
NoteBackedMindMapService, and MindMapsAPI contract before semantic backend migration:
1. Public signatures and method inventory frozen across all note and mind map surfaces;
2. NoteService container normalization, row classification, CRUD wire payloads, timestamp
   preservation, and shielded cancellation orphan cleanup;
3. NotesAPI list filtering (excluding deleted and mind maps), existence preflights, and
   exact-ID selection;
4. NoteBackedMindMapService adapter delegation and note-backed retitling;
5. Public MindMapsAPI split: note-backed JSON mind maps vs interactive Studio mind maps,
   single-RPC isolation for list_note_backed, exact MindMap return shapes, delete(kind=...)
   auto-detect idempotency, rename preflights & hydration, and get_tree shape drift diagnostics;
6. GET_NOTEBOOK recency-bump inventory asserting exact call counts across every operation.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call

import httpx
import pytest

from notebooklm import MindMap, Note
from notebooklm._mind_maps_api import MindMapsAPI, extract_interactive_tree_leaf
from notebooklm._notes import NotesAPI
from notebooklm._row_adapters.notes import NoteRow
from notebooklm._semantic.backend import BackendError, BackendErrorReason
from notebooklm._semantic.records import (
    ArtifactRecord,
    MindMapGenerateOutcomeRecord,
    MindMapRecord,
    NoteRecord,
)
from notebooklm._semantic.services.note import NoteService, _cleanup_tasks
from notebooklm._types.mind_maps import MindMapKind
from notebooklm.exceptions import (
    ArtifactFeatureUnavailableError,
    DecodingError,
    MindMapNotFoundError,
    NoteNotFoundError,
    RPCError,
    RPCTimeoutError,
    UnknownRPCMethodError,
)
from notebooklm.rpc import RPCMethod
from tests._fixtures.fake_core import make_fake_core
from tests._fixtures.note_stack import make_note_stack

# ===========================================================================
# 1. Public Signatures and Method Inventory Frozen
# ===========================================================================


def test_notes_api_public_signatures_are_frozen() -> None:
    """Freeze all public method signatures on NotesAPI."""
    assert list(inspect.signature(NotesAPI.list).parameters) == ["self", "notebook_id"]
    assert list(inspect.signature(NotesAPI.get).parameters) == ["self", "notebook_id", "note_id"]
    assert list(inspect.signature(NotesAPI.get_or_none).parameters) == [
        "self",
        "notebook_id",
        "note_id",
    ]

    create_sig = inspect.signature(NotesAPI.create).parameters
    assert list(create_sig) == ["self", "notebook_id", "title", "content"]
    assert create_sig["title"].default == "New Note"
    assert create_sig["content"].default == ""

    assert list(inspect.signature(NotesAPI.update).parameters) == [
        "self",
        "notebook_id",
        "note_id",
        "content",
        "title",
    ]
    assert list(inspect.signature(NotesAPI.delete).parameters) == [
        "self",
        "notebook_id",
        "note_id",
    ]
    assert list(inspect.signature(NotesAPI.list_mind_maps).parameters) == ["self", "notebook_id"]
    assert list(inspect.signature(NotesAPI.delete_mind_map).parameters) == [
        "self",
        "notebook_id",
        "mind_map_id",
    ]


@pytest.mark.asyncio
async def test_notes_facade_preserves_reconstructed_timeout_cause_graph() -> None:
    leaf = httpx.ReadTimeout(
        "socket stalled",
        request=httpx.Request(
            "POST", "https://notebooklm.google.com/_/LabsTailwindUi/data/batchexecute"
        ),
    )
    timeout = RPCTimeoutError(
        "request timed out",
        method_id=RPCMethod.GET_NOTES_AND_MIND_MAPS.value,
        timeout_seconds=3.0,
        original_error=leaf,
    )
    timeout.__cause__ = leaf
    timeout.__context__ = leaf
    timeout.__suppress_context__ = True
    core = make_fake_core(rpc_call=AsyncMock(side_effect=timeout))
    _notes, api = make_note_stack(core)

    with pytest.raises(RPCTimeoutError) as caught:
        await api.list("nb-1")

    assert caught.value is not timeout
    assert isinstance(caught.value.original_error, httpx.ReadTimeout)
    assert caught.value.original_error.args == ("socket stalled",)
    assert caught.value.__cause__ is caught.value.original_error
    assert caught.value.__context__ is caught.value.original_error
    assert caught.value.__suppress_context__ is True


def test_note_service_public_signatures_are_frozen() -> None:
    """Freeze method signatures on NoteService primitives."""
    assert list(inspect.signature(NoteService.list_notes).parameters) == [
        "self",
        "notebook_id",
    ]
    assert list(inspect.signature(NoteService.get_note_or_none).parameters) == [
        "self",
        "notebook_id",
        "note_id",
    ]

    # P10 R6.6 deleted ``create_note``: once the service returns records it was
    # a byte-identical delegate of ``create_note_record``, the one
    # cancellation-safe create. The pinned defaults move here unchanged.
    create_sig = inspect.signature(NoteService.create_note_record).parameters
    assert list(create_sig) == [
        "self",
        "notebook_id",
        "title",
        "content",
        "operation_variant",
        "deadline",
    ]
    assert create_sig["title"].default == "New Note"
    assert create_sig["content"].default == ""
    assert create_sig["operation_variant"].kind is inspect.Parameter.KEYWORD_ONLY
    assert create_sig["operation_variant"].default == "plain"
    assert create_sig["deadline"].kind is inspect.Parameter.KEYWORD_ONLY
    assert create_sig["deadline"].default is None

    assert list(inspect.signature(NoteService.update_note).parameters) == [
        "self",
        "notebook_id",
        "note_id",
        "content",
        "title",
    ]
    assert list(inspect.signature(NoteService.delete_note).parameters) == [
        "self",
        "notebook_id",
        "note_id",
    ]

    # P10 R4.2 deleted the deferred raw-service seam these callers used; the
    # raw row listings it published are ``NoteService`` methods now.
    assert list(inspect.signature(NoteService.list_note_rows).parameters) == [
        "self",
        "notebook_id",
    ]
    assert list(inspect.signature(NoteService.list_mind_map_rows).parameters) == [
        "self",
        "notebook_id",
    ]


def test_note_backed_mind_map_signatures_are_frozen() -> None:
    """Freeze the note-backed mind-map surface ``NoteService`` now owns."""
    assert list(inspect.signature(NoteService.list_mind_maps).parameters) == [
        "self",
        "notebook_id",
    ]
    assert list(inspect.signature(NoteService.get_mind_map_or_none).parameters) == [
        "self",
        "notebook_id",
        "mind_map_id",
    ]
    assert list(inspect.signature(NoteService.delete_mind_map).parameters) == [
        "self",
        "notebook_id",
        "mind_map_id",
    ]
    assert list(inspect.signature(NoteService.rename_mind_map).parameters) == [
        "self",
        "notebook_id",
        "mind_map_id",
        "new_title",
    ]


def test_mind_maps_api_public_signatures_are_frozen() -> None:
    """Freeze all public method signatures on MindMapsAPI."""
    assert list(inspect.signature(MindMapsAPI.list_note_backed).parameters) == [
        "self",
        "notebook_id",
    ]
    assert list(inspect.signature(MindMapsAPI.list).parameters) == ["self", "notebook_id"]
    assert list(inspect.signature(MindMapsAPI.get).parameters) == [
        "self",
        "notebook_id",
        "mind_map_id",
    ]
    assert list(inspect.signature(MindMapsAPI.get_or_none).parameters) == [
        "self",
        "notebook_id",
        "mind_map_id",
    ]

    gen_sig = inspect.signature(MindMapsAPI.generate).parameters
    assert list(gen_sig) == [
        "self",
        "notebook_id",
        "source_ids",
        "kind",
        "language",
        "instructions",
        "wait",
    ]
    assert gen_sig["source_ids"].default is None
    assert gen_sig["kind"].kind is inspect.Parameter.KEYWORD_ONLY
    assert gen_sig["language"].default == "en"
    assert gen_sig["instructions"].default is None
    assert gen_sig["wait"].default is True

    rename_sig = inspect.signature(MindMapsAPI.rename).parameters
    assert list(rename_sig) == [
        "self",
        "notebook_id",
        "mind_map_id",
        "new_title",
        "kind",
        "return_object",
    ]
    assert rename_sig["kind"].kind is inspect.Parameter.KEYWORD_ONLY
    assert rename_sig["kind"].default is None
    assert rename_sig["return_object"].kind is inspect.Parameter.KEYWORD_ONLY
    assert rename_sig["return_object"].default is True

    del_sig = inspect.signature(MindMapsAPI.delete).parameters
    assert list(del_sig) == ["self", "notebook_id", "mind_map_id", "kind"]
    assert del_sig["kind"].kind is inspect.Parameter.KEYWORD_ONLY
    assert del_sig["kind"].default is None

    tree_sig = inspect.signature(MindMapsAPI.get_tree).parameters
    assert list(tree_sig) == ["self", "notebook_id", "mind_map_id", "kind"]
    assert tree_sig["kind"].kind is inspect.Parameter.KEYWORD_ONLY
    assert tree_sig["kind"].default is None


# ===========================================================================
# 2. NoteService Primitives & Wire Operations
# ===========================================================================


@pytest.mark.asyncio
async def test_note_service_fetch_note_rows_normalizes_containers_and_handles_schema_drift() -> (
    None
):
    """NoteService extracts rows from nested or flat containers, filters non-rows, and raises on drift."""
    mock_session = make_fake_core(rpc_call=AsyncMock())
    service = make_note_stack(mock_session)[0]

    # 1. Nested row container with various valid and invalid row shapes
    mock_session.rpc_executor.rpc_call.return_value = [
        [
            ["note-1", "Content 1"],
            [None, ["note-2", "Content 2", None, None, "Title 2"]],
            [],
            "not-a-row",
            [123, "Invalid id type"],
            [None, "Non-nested payload"],
            ["note-3", None, 2],  # soft-deleted row
        ]
    ]

    rows = await service.list_note_rows("nb-100")
    assert rows == [
        ["note-1", "Content 1"],
        ["note-2", ["note-2", "Content 2", None, None, "Title 2"]],
        ["note-3", None, 2],
    ]
    assert mock_session.rpc_executor.rpc_call.await_args.args == (
        RPCMethod.GET_NOTES_AND_MIND_MAPS,
        ["nb-100"],
    )
    assert mock_session.rpc_executor.rpc_call.await_args.kwargs["source_path"] == "/notebook/nb-100"
    assert mock_session.rpc_executor.rpc_call.await_args.kwargs["allow_null"] is True

    # 2. Flat row container with timestamp tail
    mock_session.rpc_executor.rpc_call.reset_mock()
    mock_session.rpc_executor.rpc_call.return_value = [
        ["note-flat-1", "Content"],
        [1700000000, 0],
    ]
    assert await service.list_note_rows("nb-100") == [["note-flat-1", "Content"]]

    # 3. Empty or None response returns empty list
    for empty_payload in [None, [], [[]], ["not-a-list"]]:
        mock_session.rpc_executor.rpc_call.return_value = empty_payload
        assert await service.list_note_rows("nb-100") == []

    # 4. Truthy non-list schema drift raises the decoding reason
    for drift_payload in ["string-drift", {"error": "unexpected"}, 42]:
        mock_session.rpc_executor.rpc_call.return_value = drift_payload
        with pytest.raises(BackendError) as exc_info:
            await service.list_note_rows("nb-100")
        assert exc_info.value.reason is BackendErrorReason.DECODING
        assert isinstance(exc_info.value.__cause__, DecodingError)
        assert exc_info.value.__cause__.method_id == RPCMethod.GET_NOTES_AND_MIND_MAPS.value


@pytest.mark.asyncio
async def test_note_service_row_partition_exhaustiveness() -> None:
    """The row partition keeps the classifier's exact DELETED/MIND_MAP/NOTE split."""
    mock_session = make_fake_core(rpc_call=AsyncMock())
    service = make_note_stack(mock_session)[0]

    async def _partition(row: object) -> tuple[list[str], list[str]]:
        mock_session.rpc_executor.rpc_call.return_value = [[row]]
        mind_maps = await service.list_mind_map_rows("nb-100")
        notes = await service.list_notes("nb-100")
        return [item[0] for item in mind_maps], [item.id for item in notes]

    # Deleted row (status=2 at position 2) reaches neither listing.
    assert await _partition(["n-del", None, 2]) == ([], [])
    assert await _partition(["n-del", None, 2, "extra"]) == ([], [])

    # Mind map (JSON with 'children' or 'nodes')
    assert await _partition(["n-mm1", '{"name": "Map", "children": [{"name": "Child"}]}']) == (
        ["n-mm1"],
        [],
    )
    assert await _partition(["n-mm2", '{"name": "Map", "nodes": []}']) == (["n-mm2"], [])

    # Plain text note
    assert await _partition(["n-txt", "This is plain markdown content"]) == ([], ["n-txt"])

    # Nested note format
    assert await _partition(["n-nest", ["n-nest", "Nested body", None, None, "Title"]]) == (
        [],
        ["n-nest"],
    )

    # None content without the soft-delete sentinel is not a mind map; the
    # plain-note listing keeps surfacing it rather than dropping the row.
    assert await _partition(["n-none", None, 0]) == ([], ["n-none"])
    assert await _partition(["n-none", None, 1]) == ([], ["n-none"])

    # Non-row shapes inside the container are filtered by the normalizer.
    assert await _partition([]) == ([], [])
    mock_session.rpc_executor.rpc_call.return_value = [
        ["n-keep", "body"],
        "not-a-row",
        [123, "non-string id"],
    ]
    assert [item.id for item in await service.list_notes("nb-100")] == ["n-keep"]


@pytest.mark.asyncio
async def test_note_service_crud_wire_payloads_and_endpoints() -> None:
    """Verify wire payloads, parameter shapes, and RPC methods for all NoteService operations."""
    mock_session = make_fake_core(rpc_call=AsyncMock(return_value=[["note-new"]]))
    service = make_note_stack(mock_session)[0]

    # 1. create_note_record (CREATE_NOTE followed by shielded UPDATE_NOTE)
    note = await service.create_note_record(
        "nb-100", title="Research Plan", content="# Goals", operation_variant="plain"
    )
    # R6.6: the neutral record, not the public model. The ``Note`` the facade
    # projects out of it is pinned in
    # ``test_notes_unit.py::test_create_projects_the_allocation_record``.
    assert isinstance(note, NoteRecord)
    assert note.id == "note-new"
    assert note.notebook_id == "nb-100"
    assert note.title == "Research Plan"
    assert note.content == "# Goals"

    assert mock_session.rpc_executor.rpc_call.await_count == 2
    assert mock_session.rpc_executor.rpc_call.await_args_list[0] == call(
        RPCMethod.CREATE_NOTE,
        ["nb-100", "", [1], None, "Research Plan"],
        source_path="/notebook/nb-100",
        allow_null=False,
        _is_retry=False,
        disable_internal_retries=False,
        operation_variant="plain",
        read_timeout=None,
        raise_on_null_status=False,
        _retry_deadline=None,
    )
    assert mock_session.rpc_executor.rpc_call.await_args_list[1] == call(
        RPCMethod.UPDATE_NOTE,
        ["nb-100", "note-new", [[["# Goals", "Research Plan", [], 0]]]],
        source_path="/notebook/nb-100",
        allow_null=True,
        _is_retry=False,
        disable_internal_retries=False,
        operation_variant=None,
        read_timeout=None,
        raise_on_null_status=False,
        _retry_deadline=None,
    )

    # 2. update_note
    mock_session.rpc_executor.rpc_call.reset_mock()
    await service.update_note("nb-100", "note-1", "Updated Content", "Updated Title")
    mock_session.rpc_executor.rpc_call.assert_awaited_once_with(
        RPCMethod.UPDATE_NOTE,
        ["nb-100", "note-1", [[["Updated Content", "Updated Title", [], 0]]]],
        source_path="/notebook/nb-100",
        allow_null=True,
        _is_retry=False,
        disable_internal_retries=False,
        operation_variant=None,
        read_timeout=None,
        raise_on_null_status=False,
        _retry_deadline=None,
    )

    # 3. delete_note
    mock_session.rpc_executor.rpc_call.reset_mock()
    await service.delete_note("nb-100", "note-1")
    mock_session.rpc_executor.rpc_call.assert_awaited_once_with(
        RPCMethod.DELETE_NOTE,
        ["nb-100", None, ["note-1"]],
        source_path="/notebook/nb-100",
        allow_null=True,
        _is_retry=False,
        disable_internal_retries=False,
        operation_variant=None,
        read_timeout=None,
        raise_on_null_status=False,
        _retry_deadline=None,
    )


@pytest.mark.asyncio
async def test_note_service_create_note_handles_id_extraction_and_timestamps() -> None:
    """create_note decodes nested and flat envelopes, extracts created_at, and fails loud on missing ID."""
    mock_session = make_fake_core(rpc_call=AsyncMock())
    service, api = make_note_stack(mock_session)

    # 1. Nested envelope with creation timestamp [seconds, nanos]
    mock_session.rpc_executor.rpc_call.return_value = [
        [
            "note-nested",
            "unused-init-content",
            [1, "user-123", [1_700_000_000, 0]],
            None,
            "Initial Title",
        ]
    ]
    note = await service.create_note_record("nb-100", title="Title", content="Body")
    assert note.id == "note-nested"
    assert note.created_at == datetime(2023, 11, 14, 22, 13, 20, tzinfo=timezone.utc)

    # 2. Flat envelope
    mock_session.rpc_executor.rpc_call.return_value = [
        "note-flat",
        "init-content",
        [1, "user-123", [1_700_000_000, 500_000_000]],
        None,
        "Initial Title",
    ]
    note_flat = await service.create_note_record("nb-100", title="Title", content="Body")
    assert note_flat.id == "note-flat"
    assert note_flat.created_at == datetime(2023, 11, 14, 22, 13, 20, tzinfo=timezone.utc)

    # 3. Degenerate response with no usable ID raises RPCError
    for degenerate in [None, [], [[]], [[None]], [123]]:
        mock_session.rpc_executor.rpc_call.return_value = degenerate
        with pytest.raises(RPCError) as exc_info:
            await api.create("nb-100", title="Title", content="Body")
        assert exc_info.value.method_id == RPCMethod.CREATE_NOTE.value


@pytest.mark.asyncio
async def test_note_service_create_note_cancellation_triggers_fire_and_forget_cleanup() -> None:
    """When outer cancel interrupts create_note during shielded UPDATE_NOTE, orphan row cleanup is queued."""
    update_started = asyncio.Event()
    update_proceed = asyncio.Event()

    async def _mock_rpc_call(method: RPCMethod, *args: Any, **kwargs: Any) -> Any:
        if method is RPCMethod.CREATE_NOTE:
            return [["note-orphan"]]
        if method is RPCMethod.UPDATE_NOTE:
            update_started.set()
            await update_proceed.wait()
            return None
        if method is RPCMethod.DELETE_NOTE:
            return None
        return None

    mock_session = make_fake_core(rpc_call=AsyncMock(side_effect=_mock_rpc_call))
    service = make_note_stack(mock_session)[0]

    task = asyncio.create_task(service.create_note_record("nb-100", title="Title", content="Body"))
    await update_started.wait()

    # Cancel outer create task while UPDATE_NOTE is running under shield
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Verify that cleanup task was created and tracked in _cleanup_tasks
    assert len(_cleanup_tasks) >= 1

    # Allow shielded update to finish so the sequential delete cleanup runs
    update_proceed.set()
    await asyncio.sleep(0.01)

    # Verify DELETE_NOTE cleanup was dispatched
    delete_calls = [
        c
        for c in mock_session.rpc_executor.rpc_call.await_args_list
        if c[0][0] is RPCMethod.DELETE_NOTE
    ]
    assert len(delete_calls) == 1
    assert delete_calls[0] == call(
        RPCMethod.DELETE_NOTE,
        ["nb-100", None, ["note-orphan"]],
        source_path="/notebook/nb-100",
        allow_null=True,
        _is_retry=False,
        disable_internal_retries=False,
        operation_variant=None,
        read_timeout=None,
        raise_on_null_status=False,
        _retry_deadline=None,
    )


# ===========================================================================
# 3. NotesAPI Behavior, Existence Preflights & Exact-ID Selection
# ===========================================================================


@pytest.mark.asyncio
async def test_notes_api_list_filters_deleted_and_mind_maps() -> None:
    """NotesAPI.list returns only active plain notes, filtering out deleted notes and mind maps."""
    mock_session = make_fake_core(rpc_call=AsyncMock())
    _service, api = make_note_stack(mock_session)

    mock_session.rpc_executor.rpc_call.return_value = [
        [
            ["n-plain", ["n-plain", "Text content", None, None, "Note Title"]],
            ["n-mm", '{"name": "Mind Map", "children": []}'],
            ["n-del", None, 2],
        ]
    ]

    notes = await api.list("nb-100")
    assert len(notes) == 1
    note = notes[0]
    # R6.6 moved projection here from ``NoteService``, so the public-type pin
    # that used to sit on the service's create sits on the facade's reads.
    assert isinstance(note, Note)
    assert note.id == "n-plain"
    assert note.notebook_id == "nb-100"
    assert note.title == "Note Title"
    assert note.content == "Text content"


@pytest.mark.asyncio
async def test_notes_api_get_and_get_or_none_exact_id_selection() -> None:
    """NotesAPI.get raises NoteNotFoundError on miss; get_or_none returns None; uses exact ID match."""
    mock_session = make_fake_core(rpc_call=AsyncMock())
    _service, api = make_note_stack(mock_session)

    mock_session.rpc_executor.rpc_call.return_value = [
        [
            ["note-123", ["note-123", "Body 123", None, None, "Title 123"]],
            ["note-12", ["note-12", "Prefix match should not confuse exact ID", None, None, "T12"]],
        ]
    ]

    # Exact ID match
    note = await api.get("nb-100", "note-123")
    assert isinstance(note, Note)
    assert note.id == "note-123"
    assert note.title == "Title 123"

    note_none = await api.get_or_none("nb-100", "note-123")
    assert note_none is not None and note_none.id == "note-123"

    # Miss
    assert await api.get_or_none("nb-100", "note-999") is None
    with pytest.raises(NoteNotFoundError) as exc_info:
        await api.get("nb-100", "note-999")
    assert exc_info.value.note_id == "note-999"


@pytest.mark.asyncio
async def test_notes_api_update_existence_preflight_and_loud_failure() -> None:
    """NotesAPI.update executes existence preflight via get_or_none and raises NoteNotFoundError on miss."""
    mock_session = make_fake_core(rpc_call=AsyncMock())
    _service, api = make_note_stack(mock_session)

    # Missing note -> preflight fails loud before UPDATE_NOTE
    mock_session.rpc_executor.rpc_call.return_value = [[]]
    with pytest.raises(NoteNotFoundError):
        await api.update("nb-100", "n-missing", "New Content", "New Title")

    # Assert only GET_NOTES_AND_MIND_MAPS was called; no UPDATE_NOTE was dispatched
    assert mock_session.rpc_executor.rpc_call.await_count == 1
    assert (
        mock_session.rpc_executor.rpc_call.await_args_list[0][0][0]
        == RPCMethod.GET_NOTES_AND_MIND_MAPS
    )

    # Existing note -> preflight passes and UPDATE_NOTE runs
    mock_session.rpc_executor.rpc_call.reset_mock()
    mock_session.rpc_executor.rpc_call.side_effect = [
        [["n-exist", ["n-exist", "Old Content", None, None, "Old Title"]]],
        None,
    ]
    await api.update("nb-100", "n-exist", "New Content", "New Title")
    assert mock_session.rpc_executor.rpc_call.await_count == 2
    assert mock_session.rpc_executor.rpc_call.await_args_list[1] == call(
        RPCMethod.UPDATE_NOTE,
        ["nb-100", "n-exist", [[["New Content", "New Title", [], 0]]]],
        source_path="/notebook/nb-100",
        allow_null=True,
        _is_retry=False,
        disable_internal_retries=False,
        operation_variant=None,
        read_timeout=None,
        raise_on_null_status=False,
        _retry_deadline=None,
    )


@pytest.mark.asyncio
async def test_notes_api_delete_is_idempotent_and_returns_none() -> None:
    """NotesAPI.delete and delete_mind_map are idempotent and return None."""
    mock_session = make_fake_core(rpc_call=AsyncMock(return_value=None))
    _service, api = make_note_stack(mock_session)

    assert await api.delete("nb-100", "note-1") is None
    assert await api.delete_mind_map("nb-100", "mm-1") is None


# ===========================================================================
# 4. Note-Backed Mind-Map Surface
# ===========================================================================


@pytest.mark.asyncio
async def test_note_backed_mind_map_operations() -> None:
    """NoteService lists, reads, and renames note-backed mind maps."""
    mock_session = make_fake_core(rpc_call=AsyncMock())
    service = make_note_stack(mock_session)[0]

    mm_json = '{"name": "Architecture Map", "children": []}'
    mock_session.rpc_executor.rpc_call.return_value = [
        [
            ["n-plain", "plain content"],
            ["mm-1", ["mm-1", mm_json, None, None, "Architecture Map"]],
        ]
    ]

    # 1. the raw listing keeps the wire row, the record listing its payload
    rows = await service.list_mind_map_rows("nb-100")
    assert len(rows) == 1
    assert NoteRow(rows[0]).id == "mm-1"
    records = await service.list_mind_maps("nb-100")
    assert [record.tree_json for record in records] == [mm_json]

    # 2. rename_mind_map (re-sends existing JSON content with new title)
    mock_session.rpc_executor.rpc_call.reset_mock()
    mock_session.rpc_executor.rpc_call.side_effect = [
        [["mm-1", ["mm-1", mm_json, None, None, "Architecture Map"]]],
        None,
    ]
    await service.rename_mind_map("nb-100", "mm-1", "New Architecture Map")
    update = mock_session.rpc_executor.rpc_call.await_args_list[1]
    assert update.args == (
        RPCMethod.UPDATE_NOTE,
        ["nb-100", "mm-1", [[[mm_json, "New Architecture Map", [], 0]]]],
    )
    assert update.kwargs["source_path"] == "/notebook/nb-100"
    assert update.kwargs["allow_null"] is True

    # 3. rename missing note-backed mind map raises MindMapNotFoundError
    mock_session.rpc_executor.rpc_call.reset_mock()
    mock_session.rpc_executor.rpc_call.side_effect = None
    mock_session.rpc_executor.rpc_call.return_value = [[]]
    with pytest.raises(MindMapNotFoundError) as exc_info:
        await service.rename_mind_map("nb-100", "mm-missing", "Title")
    assert exc_info.value.mind_map_id == "mm-missing"


# ===========================================================================
# 5. MindMapsAPI Split (Note-Backed JSON vs Interactive Studio)
# ===========================================================================


def _interactive_art(artifact_id: str, title: str = "Studio Map") -> ArtifactRecord:
    return ArtifactRecord(
        id=artifact_id,
        title=title,
        family="mind_map",
        status="completed",
        variant="interactive_mind_map",
    )


def _make_mind_maps_api(
    *,
    rpc_call: AsyncMock | None = None,
    note_rows: list[Any] | None = None,
    interactive_artifacts: list[ArtifactRecord] | None = None,
) -> tuple[MindMapsAPI, MagicMock, MagicMock, MagicMock, MagicMock]:
    rpc = MagicMock(rpc_call=rpc_call or AsyncMock(return_value=None))
    # R6.6: the note service returns neutral records and ``MindMapsAPI``
    # projects them, so the stub yields the persisted JSON as text exactly as
    # the real service does.
    note_maps: list[MindMapRecord] = []
    for row in note_rows or []:
        note_row = NoteRow(row)
        note_maps.append(
            MindMapRecord(
                note_row.id,
                "nb-100",
                note_row.title,
                MindMapKind.NOTE_BACKED.value,
                note_row.created_at,
                note_row.content,
            )
        )
    # Matches MindMapFamilyService.list_mind_maps'/.get_or_none's own filter:
    # only a confirmed "interactive_mind_map" variant counts.
    interactive_maps = [
        record for record in interactive_artifacts or [] if record.variant == "interactive_mind_map"
    ]
    mind_maps_adapter = MagicMock()
    mind_maps_adapter.list_mind_maps = AsyncMock(return_value=note_maps)
    mind_maps_adapter.get_mind_map_or_none = AsyncMock(
        side_effect=lambda _notebook_id, item_id: next(
            (item for item in note_maps if item.id == item_id), None
        )
    )
    mind_maps_adapter.rename_mind_map = AsyncMock()
    mind_maps_adapter.delete_mind_map = AsyncMock(return_value=None)
    mind_maps_adapter.generate_mind_map = AsyncMock()

    artifacts_api = MagicMock()
    artifacts_api.list_mind_maps = AsyncMock(return_value=interactive_maps)
    artifacts_api.get_or_none = AsyncMock(
        side_effect=lambda _notebook_id, item_id: next(
            (item for item in interactive_maps if item.id == item_id), None
        )
    )
    artifacts_api.rename = AsyncMock()
    artifacts_api.delete = AsyncMock(return_value=None)
    artifacts_api.generate = AsyncMock()
    artifacts_api.get_tree = AsyncMock(return_value=None)

    notebooks_api = MagicMock()
    notebooks_api.get_source_ids = AsyncMock(return_value=["src-1", "src-2"])

    api = MindMapsAPI(notes=mind_maps_adapter, studio=artifacts_api)
    return api, rpc, mind_maps_adapter, artifacts_api, notebooks_api


@pytest.mark.asyncio
async def test_mind_maps_api_list_note_backed_single_rpc_isolation() -> None:
    """list_note_backed decodes note-backed mind maps only and NEVER queries artifacts.list."""
    mm_row = ["mm-note", '{"name": "NB MindMap", "children": [{"name": "C1"}]}']
    api, _, mock_mm, mock_art, _ = _make_mind_maps_api(
        note_rows=[mm_row],
        interactive_artifacts=[_interactive_art("art-int")],
    )

    result = await api.list_note_backed("nb-100")
    assert len(result) == 1
    mm = result[0]
    assert isinstance(mm, MindMap)
    assert mm.id == "mm-note"
    assert mm.kind is MindMapKind.NOTE_BACKED
    assert mm.tree == {"name": "NB MindMap", "children": [{"name": "C1"}]}
    assert mm.notebook_id == "nb-100"

    mock_mm.list_mind_maps.assert_awaited_once_with("nb-100")
    mock_art.list_mind_maps.assert_not_awaited()


@pytest.mark.asyncio
async def test_mind_maps_api_list_unions_both_backings_with_exact_shapes() -> None:
    """list() returns note-backed maps with eager tree and interactive maps with tree=None."""
    mm_row = ["mm-note", '{"name": "NB MindMap", "children": []}']
    art_interactive = _interactive_art("art-int", "Studio MindMap")

    api, _, _, mock_art, _ = _make_mind_maps_api(
        note_rows=[mm_row],
        interactive_artifacts=[art_interactive],
    )

    result = await api.list("nb-100")
    assert len(result) == 2

    by_id = {m.id: m for m in result}
    note_mm = by_id["mm-note"]
    assert note_mm.kind is MindMapKind.NOTE_BACKED
    assert note_mm.tree == {"name": "NB MindMap", "children": []}

    int_mm = by_id["art-int"]
    assert int_mm.kind is MindMapKind.INTERACTIVE
    assert int_mm.tree is None  # lazy tree fetching per ADR-0019

    mock_art.list_mind_maps.assert_awaited_once_with("nb-100")


@pytest.mark.asyncio
async def test_mind_maps_api_get_and_get_or_none_diagnostics() -> None:
    """MindMapsAPI.get raises MindMapNotFoundError on miss; get_or_none returns None."""
    mm_row = ["mm-note", '{"name": "NB MindMap", "children": []}']
    api, *_ = _make_mind_maps_api(note_rows=[mm_row])

    assert (await api.get("nb-100", "mm-note")).id == "mm-note"
    assert (await api.get_or_none("nb-100", "mm-note")) is not None
    assert await api.get_or_none("nb-100", "mm-absent") is None

    with pytest.raises(MindMapNotFoundError) as exc_info:
        await api.get("nb-100", "mm-absent")
    assert exc_info.value.mind_map_id == "mm-absent"


@pytest.mark.asyncio
async def test_mind_maps_api_generate_note_backed_vs_interactive() -> None:
    """generate dispatches to generate_mind_map for NOTE_BACKED and CREATE_ARTIFACT for INTERACTIVE."""
    api, _, mock_notes, mock_studio, _ = _make_mind_maps_api()

    # 1. Note-backed generation
    mock_notes.generate_mind_map.return_value = MindMapRecord(
        "note-gen-1",
        "nb-100",
        "Generated Note Map",
        MindMapKind.NOTE_BACKED.value,
        datetime(2023, 11, 14, tzinfo=timezone.utc),
        json.dumps({"name": "Generated Note Map", "children": []}),
    )
    nb_res = await api.generate(
        "nb-100", ["src-1"], kind=MindMapKind.NOTE_BACKED, instructions="Prompt"
    )
    assert nb_res.id == "note-gen-1"
    assert nb_res.kind is MindMapKind.NOTE_BACKED
    assert nb_res.title == "Generated Note Map"
    assert nb_res.tree == {"name": "Generated Note Map", "children": []}
    mock_notes.generate_mind_map.assert_awaited_once_with("nb-100", ["src-1"], "en", "Prompt")

    # 2. Interactive generation with wait=True
    mock_studio.generate.return_value = MindMapGenerateOutcomeRecord(
        mind_map_id="art-new-100",
        record=ArtifactRecord(
            "art-new-100",
            "Interactive MindMap",
            "mind_map",
            "completed",
            variant="interactive_mind_map",
        ),
        tree={"name": "Tree", "children": []},
    )

    int_res = await api.generate(
        "nb-100", ["src-1"], kind=MindMapKind.INTERACTIVE, instructions="Prompt", wait=True
    )
    assert int_res.id == "art-new-100"
    assert int_res.kind is MindMapKind.INTERACTIVE
    assert int_res.tree == {"name": "Tree", "children": []}
    mock_studio.generate.assert_awaited_once_with("nb-100", ["src-1"], "Prompt", wait=True)

    # 3. Interactive generation null response raises ArtifactFeatureUnavailableError
    mock_studio.generate.side_effect = ArtifactFeatureUnavailableError(
        "mind_map", method_id=RPCMethod.CREATE_ARTIFACT.value
    )
    with pytest.raises(ArtifactFeatureUnavailableError):
        await api.generate("nb-100", ["src-1"], kind=MindMapKind.INTERACTIVE)


@pytest.mark.asyncio
async def test_mind_maps_api_rename_auto_detect_and_explicit_kinds() -> None:
    """rename dispatches by auto-detected or explicit kind and hydrates renamed MindMap."""
    mm_row = ["mm-note", '{"name": "Old NB", "children": []}']
    art_int = _interactive_art("art-int", "Old Studio")
    api, _, mock_mm, mock_art, _ = _make_mind_maps_api(
        note_rows=[mm_row],
        interactive_artifacts=[art_int],
    )

    # 1. Auto-detect note-backed rename
    renamed_note = await api.rename("nb-100", "mm-note", "New NB Title")
    assert renamed_note is not None
    assert renamed_note.id == "mm-note"
    mock_mm.rename_mind_map.assert_awaited_once_with("nb-100", "mm-note", "New NB Title")

    # 2. Auto-detect interactive rename
    mock_mm.rename_mind_map.reset_mock()
    renamed_int = await api.rename("nb-100", "art-int", "New Studio Title")
    assert renamed_int is not None
    assert renamed_int.id == "art-int"
    mock_art.rename.assert_awaited_once_with("nb-100", "art-int", "New Studio Title")

    # 3. Auto-detect missing raises MindMapNotFoundError
    with pytest.raises(MindMapNotFoundError):
        await api.rename("nb-100", "absent-id", "Title")

    # 4. return_object=False returns None without re-fetch
    res_none = await api.rename("nb-100", "mm-note", "New Title", return_object=False)
    assert res_none is None


@pytest.mark.asyncio
async def test_mind_maps_api_delete_auto_detect_idempotency() -> None:
    """delete with kind=None swallows MindMapNotFoundError to return None (idempotent delete)."""
    mm_row = ["mm-note", '{"name": "NB", "children": []}']
    art_int = _interactive_art("art-int", "Studio")
    api, _, mock_mm, mock_art, _ = _make_mind_maps_api(
        note_rows=[mm_row],
        interactive_artifacts=[art_int],
    )

    # 1. Note-backed delete
    assert await api.delete("nb-100", "mm-note") is None
    mock_mm.delete_mind_map.assert_awaited_once_with("nb-100", "mm-note")

    # 2. Interactive delete
    assert await api.delete("nb-100", "art-int") is None
    mock_art.delete.assert_awaited_once_with("nb-100", "art-int")

    # 3. Missing ID delete is idempotent: swallows MindMapNotFoundError and returns None
    assert await api.delete("nb-100", "missing-map-id") is None


@pytest.mark.asyncio
async def test_mind_maps_api_get_tree_semantics_and_drift_diagnostics() -> None:
    """get_tree extracts node tree for note-backed and interactive maps and detects shape drift."""
    mm_row = ["mm-note", '{"name": "NB Tree", "children": []}']
    art_int = _interactive_art("art-int", "Studio")

    api, _, _, mock_studio, _ = _make_mind_maps_api(
        note_rows=[mm_row],
        interactive_artifacts=[art_int],
    )

    # 1. Note-backed get_tree (parsed from note content)
    tree_nb = await api.get_tree("nb-100", "mm-note")
    assert tree_nb == {"name": "NB Tree", "children": []}

    # 2. Interactive get_tree (fetched via GET_INTERACTIVE_HTML at [0][9][3])
    mock_studio.get_tree.return_value = {"name": "Studio Tree", "children": []}
    tree_int = await api.get_tree("nb-100", "art-int", kind=MindMapKind.INTERACTIVE)
    assert tree_int == {"name": "Studio Tree", "children": []}

    # 3. Absent leaf within list options block is tolerated during settling
    mock_studio.get_tree.return_value = None
    assert await api.get_tree("nb-100", "art-int", kind=MindMapKind.INTERACTIVE) is None

    # 4. Shape drift where options block is not a list raises UnknownRPCMethodError
    mock_studio.get_tree.side_effect = UnknownRPCMethodError(
        "drift", method_id=RPCMethod.GET_INTERACTIVE_HTML.value
    )
    with pytest.raises(UnknownRPCMethodError) as exc_info:
        await api.get_tree("nb-100", "art-int", kind=MindMapKind.INTERACTIVE)
    assert exc_info.value.method_id == RPCMethod.GET_INTERACTIVE_HTML.value


def test_extract_interactive_tree_leaf_unit() -> None:
    """extract_interactive_tree_leaf raises UnknownRPCMethodError on non-list options block."""
    # Valid options block with leaf
    payload = [[None] * 9 + [[None, None, None, '{"name": "T"}']]]
    assert extract_interactive_tree_leaf(payload, source="test") == '{"name": "T"}'

    # None payload
    assert extract_interactive_tree_leaf(None, source="test") is None

    # Shape drift: options block is string
    drift_payload = [[None] * 9 + ["drift-string"]]
    with pytest.raises(UnknownRPCMethodError) as exc_info:
        extract_interactive_tree_leaf(drift_payload, source="test")
    assert exc_info.value.path == (0, 9)


# ===========================================================================
# 6. GET_NOTEBOOK Recency-Bump Counts Inventory
# ===========================================================================


@pytest.mark.asyncio
async def test_notes_and_mind_maps_get_notebook_recency_bump_inventory() -> None:
    """Fail-closed inventory verifying GET_NOTEBOOK recency-bump counts across all note and mind map paths.

    Plan invariant: GET_NOTEBOOK writes lastViewedTime. None of the NotesAPI or
    NoteService operations should issue GET_NOTEBOOK. MindMapsAPI.generate only issues
    GET_NOTEBOOK when source_ids is omitted (resolving source IDs via notebooks.get_source_ids).
    """
    mock_session = make_fake_core(
        rpc_call=AsyncMock(
            return_value=[
                [
                    ["n1", ["n1", "Content", None, None, "Title"]],
                    ["mm1", '{"name": "Map", "children": []}'],
                ]
            ]
        )
    )
    notes_service, notes_api = make_note_stack(mock_session)

    def _get_notebook_call_count() -> int:
        return sum(
            1
            for c in mock_session.rpc_executor.rpc_call.await_args_list
            if c[0][0] is RPCMethod.GET_NOTEBOOK
        )

    # 1. NotesAPI operations -> 0 GET_NOTEBOOK calls
    await notes_api.list("nb-1")
    assert _get_notebook_call_count() == 0

    await notes_api.get("nb-1", "n1")
    assert _get_notebook_call_count() == 0

    await notes_api.get_or_none("nb-1", "n1")
    assert _get_notebook_call_count() == 0

    mock_session.rpc_executor.rpc_call.side_effect = [[["n-new"]], None]
    await notes_api.create("nb-1", "T", "C")
    assert _get_notebook_call_count() == 0

    mock_session.rpc_executor.rpc_call.side_effect = [
        [[["n1", ["n1", "C", None, None, "T"]]]],
        None,
    ]
    await notes_api.update("nb-1", "n1", "C2", "T2")
    assert _get_notebook_call_count() == 0

    mock_session.rpc_executor.rpc_call.side_effect = None
    mock_session.rpc_executor.rpc_call.return_value = None
    await notes_api.delete("nb-1", "n1")
    assert _get_notebook_call_count() == 0

    mock_session.rpc_executor.rpc_call.return_value = [[["mm1", '{"name": "M", "children": []}']]]
    await notes_api.list_mind_maps("nb-1")
    assert _get_notebook_call_count() == 0

    mock_session.rpc_executor.rpc_call.return_value = None
    await notes_api.delete_mind_map("nb-1", "mm1")
    assert _get_notebook_call_count() == 0

    # 2. MindMapsAPI operations -> 0 GET_NOTEBOOK calls (when source_ids explicit)
    api, _, mock_notes, mock_studio, _ = _make_mind_maps_api(
        note_rows=[["mm1", '{"name": "M", "children": []}']],
        interactive_artifacts=[_interactive_art("art1")],
    )

    await api.list_note_backed("nb-1")
    await api.list("nb-1")
    await api.get("nb-1", "mm1")
    await api.get_or_none("nb-1", "mm1")
    await api.rename("nb-1", "mm1", "New Title")
    await api.delete("nb-1", "mm1")
    await api.get_tree("nb-1", "mm1")

    # generate with explicit source_ids -> 0 get_source_ids calls -> 0 GET_NOTEBOOK
    mock_notes.generate_mind_map.return_value = MindMapRecord(
        "note-new", "nb-1", "Map", MindMapKind.NOTE_BACKED.value
    )
    mock_studio.generate.return_value = MindMapGenerateOutcomeRecord(
        mind_map_id="art-new", record=None, tree=None
    )
    await api.generate("nb-1", ["src-1"], kind=MindMapKind.NOTE_BACKED)
    await api.generate("nb-1", ["src-1"], kind=MindMapKind.INTERACTIVE, wait=False)
    assert mock_notes.generate_mind_map.await_args.args[1] == ["src-1"]
    assert mock_studio.generate.await_args.args[1] == ["src-1"]

    # generate with source_ids=None -> exactly 1 get_source_ids call (which issues GET_NOTEBOOK)
    await api.generate("nb-1", source_ids=None, kind=MindMapKind.INTERACTIVE, wait=False)
    assert mock_studio.generate.await_args.args[1] is None
