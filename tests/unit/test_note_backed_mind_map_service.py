"""Unit tests for the ``NoteBackedMindMapService`` facade.

This service is the adapter that knows mind maps share storage with
plain notes. It delegates everything to a wrapped :class:`NoteService`
so the artifact download path doesn't have to know about the note row
shape, and the Phase 6 NotesAPI retype gets a clean ``mind_maps``
parameter to wire.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from notebooklm._mind_map import NoteBackedMindMapService
from notebooklm._note_service import NoteRowKind, NoteService


@pytest.fixture
def mock_notes() -> MagicMock:
    notes = MagicMock(spec=NoteService)
    return notes


@pytest.fixture
def service(mock_notes: MagicMock) -> NoteBackedMindMapService:
    return NoteBackedMindMapService(mock_notes)


class TestListMindMaps:
    @pytest.mark.asyncio
    async def test_list_mind_maps_filters_to_mind_map_rows(
        self, service: NoteBackedMindMapService, mock_notes: MagicMock
    ) -> None:
        mind_map_row = ["mm_1", json.dumps({"nodes": []})]
        plain_note = ["note_1", "plain body"]
        deleted = ["del_1", None, 2]
        mock_notes.fetch_note_rows = AsyncMock(return_value=[plain_note, mind_map_row, deleted])

        def fake_classify(row: list[object]) -> NoteRowKind:
            if row is mind_map_row:
                return NoteRowKind.MIND_MAP
            if row is deleted:
                return NoteRowKind.DELETED
            return NoteRowKind.NOTE

        mock_notes.classify_row = MagicMock(side_effect=fake_classify)

        result = await service.list_mind_maps("nb_abc")

        assert result == [mind_map_row]
        mock_notes.fetch_note_rows.assert_awaited_once_with("nb_abc")

    @pytest.mark.asyncio
    async def test_list_mind_maps_returns_empty_when_no_rows(
        self, service: NoteBackedMindMapService, mock_notes: MagicMock
    ) -> None:
        mock_notes.fetch_note_rows = AsyncMock(return_value=[])
        mock_notes.classify_row = MagicMock()
        assert await service.list_mind_maps("nb_abc") == []
        mock_notes.classify_row.assert_not_called()


class TestExtractContent:
    def test_extract_content_delegates_to_note_service(
        self, service: NoteBackedMindMapService, mock_notes: MagicMock
    ) -> None:
        mock_notes.extract_content = MagicMock(return_value="payload")
        row = ["mm_1", "payload"]

        result = service.extract_content(row)

        assert result == "payload"
        mock_notes.extract_content.assert_called_once_with(row)


class TestDeleteMindMap:
    @pytest.mark.asyncio
    async def test_delete_mind_map_delegates_and_returns_none(
        self, service: NoteBackedMindMapService, mock_notes: MagicMock
    ) -> None:
        mock_notes.delete_note = AsyncMock(return_value=None)

        # v0.7.0: delete now returns None (issue #1211).
        assert await service.delete_mind_map("nb_abc", "mm_1") is None

        mock_notes.delete_note.assert_awaited_once_with("nb_abc", "mm_1")


class TestEndToEndWithRealNoteService:
    """Integration check: NoteBackedMindMapService backed by a real
    :class:`NoteService` must still return mind-map rows correctly."""

    @pytest.mark.asyncio
    async def test_real_note_service_round_trip(self) -> None:
        from _fixtures.fake_core import make_fake_core
        from notebooklm._note_service import NoteService as RealNoteService

        mind_map_payload = json.dumps({"children": [{"name": "c"}]})
        session = make_fake_core(
            rpc_call=AsyncMock(
                return_value=[
                    [
                        ["note_1", "plain"],
                        ["mm_1", mind_map_payload],
                        ["del_1", None, 2],
                    ]
                ]
            )
        )

        notes = RealNoteService(session)
        svc = NoteBackedMindMapService(notes)

        rows = await svc.list_mind_maps("nb_x")

        assert rows == [["mm_1", mind_map_payload]]
        assert svc.extract_content(rows[0]) == mind_map_payload
