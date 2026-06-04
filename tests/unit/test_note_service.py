"""Unit tests for the private ``NoteService`` primitives.

``NoteService`` owns the raw note-row fetch + classify + CRUD
primitives shared by ``NotesAPI`` (Phase 6 retypes it) and
``NoteBackedMindMapService`` (the mind-map adapter the artifact
download path uses).

The classifier behaviour, CRUD wire payloads, and the audit §28
cancel-shielded ``create_note`` are all exercised here; Phase 6
(refactor-history.md Step 9, ADR-0013) retired the legacy
``test_mind_map_service.py`` tests because the underlying
``MindMapService`` class is gone.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, call

import pytest

from _fixtures.fake_core import FakeSession, make_fake_core
from notebooklm._note_service import NoteRowKind, NoteService
from notebooklm.exceptions import RPCError
from notebooklm.rpc import RPCMethod
from notebooklm.types import Note


@pytest.fixture
def mock_session() -> FakeSession:
    # ``make_fake_core`` is the ADR-0007 sanctioned substrate. We inject a
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
        mock_session.rpc_executor.rpc_call.return_value = [
            [
                ["note_1", "Content"],
                [None, ["note_3", "Nested body", None, None, "Nested Title"]],
                [],
                "not-a-row",
                [123, "Non-string ID"],
                [None, "Non-nested note payload"],
                ["note_2", "Content"],
            ]
        ]

        rows = await service.fetch_note_rows("nb_123")

        assert rows == [
            ["note_1", "Content"],
            ["note_3", ["note_3", "Nested body", None, None, "Nested Title"]],
            ["note_2", "Content"],
        ]
        mock_session.rpc_executor.rpc_call.assert_awaited_once_with(
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
        mock_session.rpc_executor.rpc_call.return_value = payload
        assert await service.fetch_note_rows("nb_123") == []

    @pytest.mark.asyncio
    async def test_fetch_note_rows_accepts_flat_row_container(
        self, service: NoteService, mock_session: FakeSession
    ) -> None:
        mock_session.rpc_executor.rpc_call.return_value = [
            ["note_1", "Content"],
            ["deleted_note", None, 2],
        ]

        assert await service.fetch_note_rows("nb_123") == [
            ["note_1", "Content"],
            ["deleted_note", None, 2],
        ]


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
        """Per refactor-history.md §Risks: when saved-chat metadata is not
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
        mock_session.rpc_executor.rpc_call.side_effect = [[["note_123"]], None]

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
        assert mock_session.rpc_executor.rpc_call.await_args_list == [
            call(
                RPCMethod.CREATE_NOTE,
                ["nb_123", "", [1], None, "Mind Map"],
                source_path="/notebook/nb_123",
                operation_variant="plain",
            ),
            call(
                RPCMethod.UPDATE_NOTE,
                ["nb_123", "note_123", [[['{"children":[]}', "Mind Map", [], 0]]]],
                source_path="/notebook/nb_123",
                allow_null=True,
            ),
        ]

    @pytest.mark.asyncio
    async def test_create_note_raises_when_server_omits_id(
        self, service: NoteService, mock_session: FakeSession
    ) -> None:
        mock_session.rpc_executor.rpc_call.return_value = None

        # An unparseable CREATE_NOTE payload must surface as an error
        # rather than a success-shaped ``Note(id="")`` (issue #1162).
        with pytest.raises(RPCError, match="no usable note id"):
            await service.create_note("nb_123", title="T", content="body")

        # Only CREATE_NOTE should fire; bailing before UPDATE_NOTE avoids
        # poisoning a non-existent row.
        assert mock_session.rpc_executor.rpc_call.await_count == 1

    @pytest.mark.asyncio
    async def test_create_note_accepts_operation_variant_kwarg(
        self, service: NoteService, mock_session: FakeSession
    ) -> None:
        mock_session.rpc_executor.rpc_call.side_effect = [[["note_123"]], None]

        await service.create_note(
            "nb_123",
            title="T",
            content="body",
            operation_variant="plain",
        )

        create_call = mock_session.rpc_executor.rpc_call.await_args_list[0]
        assert create_call.kwargs["operation_variant"] == "plain"

    @pytest.mark.asyncio
    async def test_update_note_sends_existing_payload(
        self, service: NoteService, mock_session: FakeSession
    ) -> None:
        await service.update_note("nb_123", "note_123", "Body", "Title")

        mock_session.rpc_executor.rpc_call.assert_awaited_once_with(
            RPCMethod.UPDATE_NOTE,
            ["nb_123", "note_123", [[["Body", "Title", [], 0]]]],
            source_path="/notebook/nb_123",
            allow_null=True,
        )

    @pytest.mark.asyncio
    async def test_delete_note_returns_none_and_sends_soft_delete(
        self, service: NoteService, mock_session: FakeSession
    ) -> None:
        # v0.7.0: delete_note returns None (issue #1211).
        assert await service.delete_note("nb_123", "note_123") is None

        mock_session.rpc_executor.rpc_call.assert_awaited_once_with(
            RPCMethod.DELETE_NOTE,
            ["nb_123", None, ["note_123"]],
            source_path="/notebook/nb_123",
            allow_null=True,
        )


class TestCreateNoteCancellation:
    """Audit item §28: cancel mid-UPDATE_NOTE must not leave an orphan row.

    Moved to ``NoteService`` in Phase 6 (refactor-history.md Step 9, ADR-0013).
    The legacy ``_mind_map.MindMapService.create_note`` path that
    previously owned the shield + best-effort cleanup contract was
    retired in the same phase; the contract itself lives here now.
    """

    @pytest.mark.asyncio
    async def test_cancellation_schedules_best_effort_cleanup(
        self,
        mock_session: FakeSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        service = NoteService(mock_session)
        mock_session.rpc_executor.rpc_call.return_value = [["note_123"]]
        update_started = asyncio.Event()
        update_can_finish = asyncio.Event()
        update_finished = asyncio.Event()
        cleanup_started = asyncio.Event()
        cleanup_can_finish = asyncio.Event()
        cleanup_finished = asyncio.Event()

        async def fake_update_note(
            notebook_id: str,
            note_id: str,
            content: str,
            title: str,
        ) -> None:
            assert (notebook_id, note_id, content, title) == (
                "nb_123",
                "note_123",
                "body",
                "Title",
            )
            update_started.set()
            try:
                await update_can_finish.wait()
            finally:
                update_finished.set()

        async def fake_delete_note_best_effort(notebook_id: str, note_id: str) -> None:
            assert (notebook_id, note_id) == ("nb_123", "note_123")
            cleanup_started.set()
            try:
                await cleanup_can_finish.wait()
            finally:
                cleanup_finished.set()

        monkeypatch.setattr(service, "update_note", fake_update_note)
        monkeypatch.setattr(service, "_delete_note_best_effort", fake_delete_note_best_effort)

        task = asyncio.create_task(service.create_note("nb_123", title="Title", content="body"))
        await asyncio.wait_for(update_started.wait(), timeout=1)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)

        # Ordered cleanup (coderabbit feedback on PR #875): the cleanup
        # wrapper task is scheduled at cancel time but DELETE_NOTE only
        # fires AFTER the shielded UPDATE_NOTE finishes. Before
        # releasing UPDATE_NOTE, neither should have run — update is
        # still suspended on its event and delete is gated on the
        # update completing.
        assert not update_finished.is_set()
        assert not cleanup_started.is_set()
        assert not cleanup_finished.is_set()

        # Release the shielded UPDATE_NOTE; the cleanup task then
        # observes update_task completion and proceeds to DELETE_NOTE.
        update_can_finish.set()
        await asyncio.wait_for(update_finished.wait(), timeout=1)
        await asyncio.wait_for(cleanup_started.wait(), timeout=1)
        assert not cleanup_finished.is_set()

        cleanup_can_finish.set()
        await asyncio.wait_for(cleanup_finished.wait(), timeout=1)

    @pytest.mark.asyncio
    async def test_cancellation_cleanup_runs_even_when_update_raises(
        self,
        mock_session: FakeSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If the shielded UPDATE_NOTE raises after cancellation, the
        best-effort DELETE_NOTE cleanup still fires.

        Coderabbit feedback on PR #875 (the ordered-cleanup change)
        added this guard: the cleanup wrapper logs and swallows any
        UPDATE_NOTE exception so the DELETE_NOTE half always runs.
        Without it, an update-side error would leave the orphan row
        the shield was supposed to protect against.
        """
        service = NoteService(mock_session)
        mock_session.rpc_executor.rpc_call.return_value = [["note_456"]]
        update_started = asyncio.Event()
        update_can_finish = asyncio.Event()
        cleanup_started = asyncio.Event()

        async def failing_update_note(
            notebook_id: str,
            note_id: str,
            content: str,
            title: str,
        ) -> None:
            update_started.set()
            await update_can_finish.wait()
            raise RuntimeError("simulated UPDATE_NOTE failure after shield")

        async def fake_delete_note_best_effort(notebook_id: str, note_id: str) -> None:
            assert (notebook_id, note_id) == ("nb_456", "note_456")
            cleanup_started.set()

        monkeypatch.setattr(service, "update_note", failing_update_note)
        monkeypatch.setattr(service, "_delete_note_best_effort", fake_delete_note_best_effort)

        task = asyncio.create_task(service.create_note("nb_456", title="T", content="b"))
        await asyncio.wait_for(update_started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)

        # Ordered-cleanup guarantee on the failing-update path: even
        # though we're about to make UPDATE_NOTE raise, DELETE_NOTE
        # must still wait for that update to complete before firing —
        # cleanup must NOT have started while UPDATE_NOTE is still in
        # flight. (Mirrors the pre-release assertion in the
        # success-path test above.)
        assert not cleanup_started.is_set()

        # Release the shielded UPDATE_NOTE; it will raise — the
        # cleanup wrapper must catch+log and still issue the
        # DELETE_NOTE side.
        update_can_finish.set()
        await asyncio.wait_for(cleanup_started.wait(), timeout=1)


class TestPrivacy:
    """``NoteRowKind`` is intentionally not part of the public surface."""

    def test_note_row_kind_not_in_public_exports(self) -> None:
        import notebooklm
        import notebooklm.types

        assert "NoteRowKind" not in dir(notebooklm)
        assert "NoteRowKind" not in dir(notebooklm.types)
