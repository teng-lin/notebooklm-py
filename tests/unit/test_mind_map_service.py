"""Unit tests for the private note-backed mind-map service."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, call

import pytest

from notebooklm import _mind_map
from notebooklm._mind_map import MindMapService
from notebooklm._notes import NotesAPI
from notebooklm.rpc import RPCMethod
from notebooklm.types import Note


@pytest.fixture
def mock_core() -> MagicMock:
    core = MagicMock()
    core.rpc_call = AsyncMock()
    return core


@pytest.fixture
def service(mock_core: MagicMock) -> MindMapService:
    return MindMapService(mock_core)


class TestMindMapServiceRows:
    """Raw row fetch/filter behavior."""

    @pytest.mark.asyncio
    async def test_fetch_all_notes_and_mind_maps_filters_invalid_rows(
        self,
        service: MindMapService,
        mock_core: MagicMock,
    ) -> None:
        mock_core.rpc_call.return_value = [
            [
                ["note_1", "Content"],
                [],
                "not-a-row",
                [123, "Non-string ID"],
                ["note_2", "Content"],
            ]
        ]

        result = await service.fetch_all_notes_and_mind_maps("nb_123")

        assert result == [["note_1", "Content"], ["note_2", "Content"]]
        mock_core.rpc_call.assert_awaited_once_with(
            RPCMethod.GET_NOTES_AND_MIND_MAPS,
            ["nb_123"],
            source_path="/notebook/nb_123",
            allow_null=True,
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("payload", [None, [], ["not-a-list"], [[]]])
    async def test_fetch_all_notes_and_mind_maps_handles_empty_or_malformed_payload(
        self,
        service: MindMapService,
        mock_core: MagicMock,
        payload: object,
    ) -> None:
        mock_core.rpc_call.return_value = payload

        assert await service.fetch_all_notes_and_mind_maps("nb_123") == []

    def test_is_deleted_detects_only_soft_deleted_shape(self) -> None:
        assert MindMapService.is_deleted(["row_1", None, 2]) is True
        assert MindMapService.is_deleted(["row_1", "content", 2]) is False
        assert MindMapService.is_deleted(["row_1", None, 1]) is False
        assert MindMapService.is_deleted([]) is False
        assert MindMapService.is_deleted("not-a-row") is False

    def test_extract_content_supports_legacy_and_nested_shapes(self) -> None:
        assert MindMapService.extract_content(["row_1", "legacy"]) == "legacy"
        assert (
            MindMapService.extract_content(["row_1", ["row_1", "nested", None, None, "Title"]])
            == "nested"
        )
        assert MindMapService.extract_content(["row_1", ["row_1"]]) is None
        assert MindMapService.extract_content(["row_1", 123]) is None
        assert MindMapService.extract_content([]) is None

    def test_is_mind_map_content_detects_supported_tree_keys(self) -> None:
        assert MindMapService.is_mind_map_content('{"children": []}') is True
        assert MindMapService.is_mind_map_content('{"nodes": []}') is True
        assert MindMapService.is_mind_map_content("Regular note") is False
        assert MindMapService.is_mind_map_content(None) is False

    @pytest.mark.asyncio
    async def test_list_mind_maps_filters_deleted_notes_and_detects_children_or_nodes(
        self,
        service: MindMapService,
        mock_core: MagicMock,
    ) -> None:
        children_row = ["mm_children", '{"children": []}']
        nodes_row = ["mm_nodes", ["mm_nodes", '{"nodes": []}', None, None, "Nodes"]]
        mock_core.rpc_call.return_value = [
            [
                ["note_1", "Regular note"],
                children_row,
                nodes_row,
                ["deleted", None, 2],
            ]
        ]

        assert await service.list_mind_maps("nb_123") == [children_row, nodes_row]


class TestMindMapServiceMutations:
    """Create/update/delete service behavior."""

    @pytest.mark.asyncio
    async def test_update_note_sends_existing_payload(
        self,
        service: MindMapService,
        mock_core: MagicMock,
    ) -> None:
        await service.update_note("nb_123", "note_123", "Body", "Title")

        mock_core.rpc_call.assert_awaited_once_with(
            RPCMethod.UPDATE_NOTE,
            ["nb_123", "note_123", [[["Body", "Title", [], 0]]]],
            source_path="/notebook/nb_123",
            allow_null=True,
        )

    @pytest.mark.asyncio
    async def test_delete_note_sends_soft_delete_payload(
        self,
        service: MindMapService,
        mock_core: MagicMock,
    ) -> None:
        assert await service.delete_note("nb_123", "note_123") is True

        mock_core.rpc_call.assert_awaited_once_with(
            RPCMethod.DELETE_NOTE,
            ["nb_123", None, ["note_123"]],
            source_path="/notebook/nb_123",
            allow_null=True,
        )

    @pytest.mark.asyncio
    async def test_create_note_uses_create_then_update_and_returns_note(
        self,
        service: MindMapService,
        mock_core: MagicMock,
    ) -> None:
        mock_core.rpc_call.side_effect = [[["note_123"]], None]

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
        assert mock_core.rpc_call.await_args_list == [
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
    async def test_create_note_cancellation_schedules_best_effort_cleanup(
        self,
        service: MindMapService,
        mock_core: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        mock_core.rpc_call.return_value = [["note_123"]]
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

        await asyncio.wait_for(cleanup_started.wait(), timeout=1)
        assert not cleanup_finished.is_set()
        assert not update_finished.is_set()

        update_can_finish.set()
        await asyncio.wait_for(update_finished.wait(), timeout=1)
        cleanup_can_finish.set()
        await asyncio.wait_for(cleanup_finished.wait(), timeout=1)


class TestModuleCompatibility:
    """Module-level functions remain patchable compatibility seams."""

    @pytest.mark.asyncio
    async def test_module_create_note_still_uses_module_update_patch(
        self,
        mock_core: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        mock_core.rpc_call.return_value = [["note_123"]]
        calls: list[tuple[str, str, str, str]] = []

        async def fake_update_note(
            core: object,
            notebook_id: str,
            note_id: str,
            content: str,
            title: str,
        ) -> None:
            assert core is mock_core
            calls.append((notebook_id, note_id, content, title))

        monkeypatch.setattr(_mind_map, "update_note", fake_update_note)

        note = await _mind_map.create_note(
            mock_core,
            "nb_123",
            title="Title",
            content="body",
        )

        assert note.id == "note_123"
        assert calls == [("nb_123", "note_123", "body", "Title")]


class TestNotesAPIServiceInjection:
    """NotesAPI delegates note-backed behavior through the injected service."""

    @pytest.mark.asyncio
    async def test_create_update_list_and_delete_mind_map_use_injected_service(
        self,
        mock_core: MagicMock,
    ) -> None:
        mind_map_service = MagicMock()
        mind_map_service.create_note = AsyncMock(
            return_value=Note("note_123", "nb_123", "Title", "Body")
        )
        mind_map_service.update_note = AsyncMock(return_value=None)
        mind_map_service.list_mind_maps = AsyncMock(return_value=[["mm_1", '{"nodes": []}']])
        mind_map_service.delete_note = AsyncMock(return_value=True)
        api = NotesAPI(mock_core, mind_map_service=mind_map_service)

        assert await api.create("nb_123", "Title", "Body") == Note(
            "note_123",
            "nb_123",
            "Title",
            "Body",
        )
        await api.update("nb_123", "note_123", "New Body", "New Title")
        assert await api.list_mind_maps("nb_123") == [["mm_1", '{"nodes": []}']]
        assert await api.delete("nb_123", "note_123") is True
        assert await api.delete_mind_map("nb_123", "mm_1") is True

        mind_map_service.create_note.assert_awaited_once_with(
            "nb_123",
            title="Title",
            content="Body",
        )
        mind_map_service.update_note.assert_awaited_once_with(
            "nb_123",
            "note_123",
            "New Body",
            "New Title",
        )
        mind_map_service.list_mind_maps.assert_awaited_once_with("nb_123")
        mind_map_service.delete_note.assert_has_awaits(
            [
                call("nb_123", "note_123"),
                call("nb_123", "mm_1"),
            ]
        )

    @pytest.mark.asyncio
    async def test_private_helpers_use_injected_service(self, mock_core: MagicMock) -> None:
        mind_map_service = MagicMock()
        mind_map_service.fetch_all_notes_and_mind_maps = AsyncMock(return_value=[["note_1"]])
        mind_map_service.is_deleted = MagicMock(return_value=False)
        mind_map_service.extract_content = MagicMock(return_value="Body")
        api = NotesAPI(mock_core, mind_map_service=mind_map_service)

        assert await api._get_all_notes_and_mind_maps("nb_123") == [["note_1"]]
        assert api._is_deleted(["note_1"]) is False
        assert api._extract_content(["note_1", "Body"]) == "Body"

        mind_map_service.fetch_all_notes_and_mind_maps.assert_awaited_once_with("nb_123")
        mind_map_service.is_deleted.assert_called_once_with(["note_1"])
        mind_map_service.extract_content.assert_called_once_with(["note_1", "Body"])
