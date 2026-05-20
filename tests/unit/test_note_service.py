"""Unit tests for the private ``NoteService`` primitives.

``NoteService`` owns the raw note-row fetch + classify + CRUD
primitives shared by ``NotesAPI`` (Phase 6 retypes it) and
``NoteBackedMindMapService`` (the mind-map adapter the artifact
download path uses).

The classifier behavior is exercised here because it is the only
new logic introduced in Phase 5 (the CRUD methods mirror the existing
``MindMapService`` wire payloads, which already have dedicated tests
in ``test_mind_map_service.py``).
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, call

import pytest

from _fixtures.fake_core import FakeSession, make_fake_core
from notebooklm._note_service import NoteRowKind, NoteService
from notebooklm.rpc import RPCMethod
from notebooklm.types import Note


@pytest.fixture
def mock_session() -> FakeSession:
    # ``make_fake_core`` is the ADR-007 sanctioned substrate. We inject a
    # fresh ``AsyncMock`` for ``rpc_call`` at construction time so per-test
    # ``.return_value`` / ``.side_effect`` assignment still works.
    return make_fake_core(rpc_call=AsyncMock(return_value=None))


@pytest.fixture
def service(mock_session: FakeSession) -> NoteService:
    return NoteService(mock_session)


class TestFetchNoteRows:
    """``fetch_note_rows`` returns raw rows or ``[]`` for malformed payloads."""

    @pytest.mark.asyncio
    async def test_fetch_note_rows_filters_invalid_rows(
        self, service: NoteService, mock_session: FakeSession
    ) -> None:
        mock_session.rpc_call.return_value = [
            [
                ["note_1", "Content"],
                [],
                "not-a-row",
                [123, "Non-string ID"],
                ["note_2", "Content"],
            ]
        ]

        rows = await service.fetch_note_rows("nb_123")

        assert rows == [["note_1", "Content"], ["note_2", "Content"]]
        mock_session.rpc_call.assert_awaited_once_with(
            RPCMethod.GET_NOTES_AND_MIND_MAPS,
            ["nb_123"],
            source_path="/notebook/nb_123",
            allow_null=True,
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("payload", [None, [], ["not-a-list"], [[]]])
    async def test_fetch_note_rows_returns_empty_for_malformed_payload(
        self, service: NoteService, mock_session: FakeSession, payload: object
    ) -> None:
        mock_session.rpc_call.return_value = payload
        assert await service.fetch_note_rows("nb_123") == []


class TestClassifyRow:
    """The classifier maps raw rows to ``NoteRowKind`` values."""

    def test_deleted_row_classifies_as_deleted(self, service: NoteService) -> None:
        assert service.classify_row(["row_1", None, 2]) == NoteRowKind.DELETED

    def test_mind_map_row_via_children_key(self, service: NoteService) -> None:
        row = ["mm_1", json.dumps({"children": []})]
        assert service.classify_row(row) == NoteRowKind.MIND_MAP

    def test_mind_map_row_via_nodes_key(self, service: NoteService) -> None:
        row = ["mm_2", ["mm_2", json.dumps({"nodes": []}), None, None, "Title"]]
        assert service.classify_row(row) == NoteRowKind.MIND_MAP

    def test_plain_note_row(self, service: NoteService) -> None:
        row = ["note_1", "This is a regular note body."]
        assert service.classify_row(row) == NoteRowKind.NOTE

    def test_nested_note_shape_classifies_as_note(self, service: NoteService) -> None:
        row = ["note_2", ["note_2", "Nested body", None, None, "Nested Title"]]
        assert service.classify_row(row) == NoteRowKind.NOTE

    def test_unknown_row_with_missing_content(self, service: NoteService) -> None:
        # Row with an ID but no extractable content (and not soft-deleted)
        # is intentionally classified as UNKNOWN rather than NOTE so the
        # caller can distinguish "not a real note" from "empty note".
        assert service.classify_row(["row_3", 123]) == NoteRowKind.UNKNOWN

    def test_empty_row_classifies_as_unknown(self, service: NoteService) -> None:
        assert service.classify_row([]) == NoteRowKind.UNKNOWN

    def test_saved_chat_with_unrecognized_metadata_falls_back_to_note(
        self, service: NoteService
    ) -> None:
        """Per refactor.md §Risks: when saved-chat metadata is not
        positively detectable, the classifier must default to NOTE so
        the row never silently drops out of ``NotesAPI.list()``.
        """
        row = ["chat_note_1", "Saved chat answer body without explicit chat flag."]
        # No chat-mode metadata on the wire — classifier should still
        # surface the row as a (plain) note rather than UNKNOWN.
        assert service.classify_row(row) == NoteRowKind.NOTE


class TestExtractContent:
    """``extract_content`` handles legacy and current wire shapes."""

    def test_extract_content_from_legacy_shape(self, service: NoteService) -> None:
        assert service.extract_content(["row_1", "legacy"]) == "legacy"

    def test_extract_content_from_nested_shape(self, service: NoteService) -> None:
        assert (
            service.extract_content(["row_1", ["row_1", "nested", None, None, "Title"]]) == "nested"
        )

    def test_extract_content_returns_none_for_unknown_shape(self, service: NoteService) -> None:
        assert service.extract_content(["row_1", 123]) is None
        assert service.extract_content(["row_1", ["row_1"]]) is None
        assert service.extract_content([]) is None


class TestCrud:
    """CRUD methods send the expected wire payloads."""

    @pytest.mark.asyncio
    async def test_create_note_does_create_then_update(
        self, service: NoteService, mock_session: FakeSession
    ) -> None:
        mock_session.rpc_call.side_effect = [[["note_123"]], None]

        note = await service.create_note(
            "nb_123",
            title="Mind Map",
            content='{"children":[]}',
        )

        assert note == Note(
            id="note_123",
            notebook_id="nb_123",
            title="Mind Map",
            content='{"children":[]}',
        )
        assert mock_session.rpc_call.await_args_list == [
            call(
                RPCMethod.CREATE_NOTE,
                ["nb_123", "", [1], None, "Mind Map"],
                source_path="/notebook/nb_123",
            ),
            call(
                RPCMethod.UPDATE_NOTE,
                ["nb_123", "note_123", [[['{"children":[]}', "Mind Map", [], 0]]]],
                source_path="/notebook/nb_123",
                allow_null=True,
            ),
        ]

    @pytest.mark.asyncio
    async def test_create_note_returns_empty_id_when_server_omits_id(
        self, service: NoteService, mock_session: FakeSession
    ) -> None:
        mock_session.rpc_call.return_value = None

        note = await service.create_note("nb_123", title="T", content="body")

        assert note.id == ""
        # Only CREATE_NOTE should fire; the UPDATE_NOTE skip avoids
        # poisoning a non-existent row.
        assert mock_session.rpc_call.await_count == 1

    @pytest.mark.asyncio
    async def test_update_note_sends_existing_payload(
        self, service: NoteService, mock_session: FakeSession
    ) -> None:
        await service.update_note("nb_123", "note_123", "Body", "Title")

        mock_session.rpc_call.assert_awaited_once_with(
            RPCMethod.UPDATE_NOTE,
            ["nb_123", "note_123", [[["Body", "Title", [], 0]]]],
            source_path="/notebook/nb_123",
            allow_null=True,
        )

    @pytest.mark.asyncio
    async def test_delete_note_returns_true_and_sends_soft_delete(
        self, service: NoteService, mock_session: FakeSession
    ) -> None:
        assert await service.delete_note("nb_123", "note_123") is True

        mock_session.rpc_call.assert_awaited_once_with(
            RPCMethod.DELETE_NOTE,
            ["nb_123", None, ["note_123"]],
            source_path="/notebook/nb_123",
            allow_null=True,
        )


class TestPrivacy:
    """``NoteRowKind`` is intentionally not part of the public surface."""

    def test_note_row_kind_not_in_public_exports(self) -> None:
        import notebooklm
        import notebooklm.types

        assert "NoteRowKind" not in dir(notebooklm)
        assert "NoteRowKind" not in dir(notebooklm.types)
