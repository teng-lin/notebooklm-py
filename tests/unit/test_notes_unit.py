"""Unit tests for NotesAPI private helpers and edge cases."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from notebooklm._notes import NotesAPI


@pytest.fixture
def mock_core():
    """Create a mocked ClientCore for NotesAPI."""
    core = MagicMock()
    core.rpc_call = AsyncMock()
    return core


@pytest.fixture
def notes_api(mock_core):
    """Create NotesAPI with mocked core."""
    return NotesAPI(mock_core)


# =============================================================================
# _is_deleted() tests
# =============================================================================


class TestIsDeleted:
    """Tests for the _is_deleted private helper."""

    def test_is_deleted_standard_deleted_item(self, notes_api):
        """Test detecting standard deleted item: ['id', None, 2]."""
        item = ["note_123", None, 2]
        assert notes_api._is_deleted(item) is True

    def test_is_deleted_with_extra_elements(self, notes_api):
        """Test deleted item with additional elements."""
        item = ["note_123", None, 2, "extra", "data"]
        assert notes_api._is_deleted(item) is True

    def test_is_deleted_active_note_string_content(self, notes_api):
        """Test active note with string content is not deleted."""
        item = ["note_123", "This is content"]
        assert notes_api._is_deleted(item) is False

    def test_is_deleted_active_note_nested_format(self, notes_api):
        """Test active note with nested format is not deleted."""
        item = ["note_123", ["note_123", "Content", None, None, "Title"], 1]
        assert notes_api._is_deleted(item) is False

    def test_is_deleted_status_not_2(self, notes_api):
        """Test item with None content but status != 2."""
        item = ["note_123", None, 1]
        assert notes_api._is_deleted(item) is False

    def test_is_deleted_status_zero(self, notes_api):
        """Test item with None content and status 0."""
        item = ["note_123", None, 0]
        assert notes_api._is_deleted(item) is False

    def test_is_deleted_content_not_none(self, notes_api):
        """Test item with content and status 2 is not deleted."""
        # The actual deleted pattern requires item[1] to be None
        item = ["note_123", "content", 2]
        assert notes_api._is_deleted(item) is False

    def test_is_deleted_empty_list(self, notes_api):
        """Test empty list is not deleted."""
        item = []
        assert notes_api._is_deleted(item) is False

    def test_is_deleted_single_element(self, notes_api):
        """Test single element list is not deleted."""
        item = ["note_123"]
        assert notes_api._is_deleted(item) is False

    def test_is_deleted_two_elements(self, notes_api):
        """Test two element list is not deleted (less than 3)."""
        item = ["note_123", None]
        assert notes_api._is_deleted(item) is False

    def test_is_deleted_non_list_string(self, notes_api):
        """Test string input is not deleted."""
        item = "not_a_list"
        assert notes_api._is_deleted(item) is False

    def test_is_deleted_non_list_none(self, notes_api):
        """Test None input is not deleted."""
        item = None
        assert notes_api._is_deleted(item) is False

    def test_is_deleted_non_list_dict(self, notes_api):
        """Test dict input is not deleted."""
        item = {"id": "note_123", "content": None, "status": 2}
        assert notes_api._is_deleted(item) is False

    def test_is_deleted_nested_content_with_status_2(self, notes_api):
        """Test nested content format with status 2 is not deleted."""
        # Nested content at [1] means it's not None, so not deleted
        item = ["note_123", ["note_123", "content"], 2]
        assert notes_api._is_deleted(item) is False

    def test_is_deleted_empty_string_content(self, notes_api):
        """Test empty string content is not considered deleted."""
        item = ["note_123", "", 2]
        assert notes_api._is_deleted(item) is False

    def test_is_deleted_empty_list_content(self, notes_api):
        """Test empty list content is not considered deleted."""
        item = ["note_123", [], 2]
        assert notes_api._is_deleted(item) is False


# =============================================================================
# _extract_content() tests
# =============================================================================


class TestExtractContent:
    """Tests for the _extract_content private helper."""

    def test_extract_content_string_at_index_1(self, notes_api):
        """Test extracting content when item[1] is a string."""
        item = ["note_id", "This is the content"]
        result = notes_api._extract_content(item)
        assert result == "This is the content"

    def test_extract_content_nested_list_format(self, notes_api):
        """Test extracting content from nested list format."""
        item = ["note_id", ["note_id", "Nested content", None, None, "Title"]]
        result = notes_api._extract_content(item)
        assert result == "Nested content"

    def test_extract_content_empty_item(self, notes_api):
        """Test extracting content from empty item."""
        item = []
        result = notes_api._extract_content(item)
        assert result is None

    def test_extract_content_single_element(self, notes_api):
        """Test extracting content from single-element item."""
        item = ["note_id"]
        result = notes_api._extract_content(item)
        assert result is None

    def test_extract_content_nested_list_missing_content(self, notes_api):
        """Test extracting content when nested list has no content string."""
        item = ["note_id", ["note_id"]]
        result = notes_api._extract_content(item)
        assert result is None

    def test_extract_content_nested_list_non_string_content(self, notes_api):
        """Test extracting content when nested content is not a string."""
        item = ["note_id", ["note_id", 12345]]
        result = notes_api._extract_content(item)
        assert result is None

    def test_extract_content_non_string_non_list_at_index_1(self, notes_api):
        """Test extracting content when item[1] is neither string nor list."""
        item = ["note_id", 12345]
        result = notes_api._extract_content(item)
        assert result is None

    def test_extract_content_empty_nested_list(self, notes_api):
        """Test extracting content when nested list is empty."""
        item = ["note_id", []]
        result = notes_api._extract_content(item)
        assert result is None


# =============================================================================
# _parse_note() tests
# =============================================================================


class TestParseNote:
    """Tests for the _parse_note private helper."""

    def test_parse_note_old_format(self, notes_api):
        """Test parsing old format: [note_id, content]."""
        item = ["note_123", "Old format content"]
        result = notes_api._parse_note(item, "nb_456")

        assert result.id == "note_123"
        assert result.notebook_id == "nb_456"
        assert result.content == "Old format content"
        assert result.title == ""

    def test_parse_note_new_format(self, notes_api):
        """Test parsing new format: [note_id, [note_id, content, meta, None, title]]."""
        item = ["note_123", ["note_123", "New format content", None, None, "My Title"]]
        result = notes_api._parse_note(item, "nb_456")

        assert result.id == "note_123"
        assert result.notebook_id == "nb_456"
        assert result.content == "New format content"
        assert result.title == "My Title"

    def test_parse_note_new_format_missing_title(self, notes_api):
        """Test parsing new format when title is missing."""
        item = ["note_123", ["note_123", "Content only"]]
        result = notes_api._parse_note(item, "nb_456")

        assert result.id == "note_123"
        assert result.content == "Content only"
        assert result.title == ""

    def test_parse_note_empty_item(self, notes_api):
        """Test parsing empty item."""
        item = []
        result = notes_api._parse_note(item, "nb_456")

        assert result.id == ""
        assert result.notebook_id == "nb_456"
        assert result.content == ""
        assert result.title == ""

    def test_parse_note_id_only(self, notes_api):
        """Test parsing item with only ID."""
        item = ["note_123"]
        result = notes_api._parse_note(item, "nb_456")

        assert result.id == "note_123"
        assert result.content == ""
        assert result.title == ""

    def test_parse_note_nested_non_string_content(self, notes_api):
        """Test parsing when nested content is not a string."""
        item = ["note_123", ["note_123", None, None, None, "Title"]]
        result = notes_api._parse_note(item, "nb_456")

        assert result.id == "note_123"
        assert result.content == ""
        assert result.title == "Title"

    def test_parse_note_nested_non_string_title(self, notes_api):
        """Test parsing when nested title is not a string."""
        item = ["note_123", ["note_123", "Content", None, None, 12345]]
        result = notes_api._parse_note(item, "nb_456")

        assert result.content == "Content"
        assert result.title == ""

    def test_parse_note_converts_id_to_string(self, notes_api):
        """Test that note ID is converted to string."""
        item = [123, "Content"]
        result = notes_api._parse_note(item, "nb_456")

        assert result.id == "123"
        assert isinstance(result.id, str)


# =============================================================================
# _get_all_notes_and_mind_maps() tests
# =============================================================================


class TestGetAllNotesAndMindMaps:
    """Tests for the _get_all_notes_and_mind_maps private helper."""

    @pytest.mark.asyncio
    async def test_get_all_notes_valid_response(self, notes_api, mock_core):
        """Test with valid response structure."""
        mock_core.rpc_call.return_value = [
            [
                ["note_1", "Content 1"],
                ["note_2", "Content 2"],
            ]
        ]

        result = await notes_api._get_all_notes_and_mind_maps("nb_123")

        assert len(result) == 2
        assert result[0][0] == "note_1"
        assert result[1][0] == "note_2"

    @pytest.mark.asyncio
    async def test_get_all_notes_null_response(self, notes_api, mock_core):
        """Test with null response."""
        mock_core.rpc_call.return_value = None

        result = await notes_api._get_all_notes_and_mind_maps("nb_123")

        assert result == []

    @pytest.mark.asyncio
    async def test_get_all_notes_empty_list_response(self, notes_api, mock_core):
        """Test with empty list response."""
        mock_core.rpc_call.return_value = []

        result = await notes_api._get_all_notes_and_mind_maps("nb_123")

        assert result == []

    @pytest.mark.asyncio
    async def test_get_all_notes_first_element_not_list(self, notes_api, mock_core):
        """Test when first element is not a list."""
        mock_core.rpc_call.return_value = ["not_a_list"]

        result = await notes_api._get_all_notes_and_mind_maps("nb_123")

        assert result == []

    @pytest.mark.asyncio
    async def test_get_all_notes_filters_invalid_items(self, notes_api, mock_core):
        """Test that invalid items are filtered out."""
        mock_core.rpc_call.return_value = [
            [
                ["valid_note", "Content"],
                "not_a_list",
                [],
                [123, "Content"],  # Non-string ID
                ["valid_note_2", "Content 2"],
            ]
        ]

        result = await notes_api._get_all_notes_and_mind_maps("nb_123")

        assert len(result) == 2
        assert result[0][0] == "valid_note"
        assert result[1][0] == "valid_note_2"

    @pytest.mark.asyncio
    async def test_get_all_notes_empty_inner_list(self, notes_api, mock_core):
        """Test with empty inner notes list."""
        mock_core.rpc_call.return_value = [[]]

        result = await notes_api._get_all_notes_and_mind_maps("nb_123")

        assert result == []


# =============================================================================
# list() edge cases
# =============================================================================


class TestListNotes:
    """Edge case tests for list() method."""

    @pytest.mark.asyncio
    async def test_list_detects_mind_map_with_children_key(self, notes_api, mock_core):
        """Test that items with 'children' key are detected as mind maps."""
        mock_core.rpc_call.return_value = [
            [
                ["note_1", '{"children": []}'],
                ["note_2", "Regular content"],
            ]
        ]

        result = await notes_api.list("nb_123")

        assert len(result) == 1
        assert result[0].id == "note_2"

    @pytest.mark.asyncio
    async def test_list_detects_mind_map_with_nodes_key(self, notes_api, mock_core):
        """Test that items with 'nodes' key are detected as mind maps."""
        mock_core.rpc_call.return_value = [
            [
                ["note_1", '{"nodes": []}'],
                ["note_2", "Regular content"],
            ]
        ]

        result = await notes_api.list("nb_123")

        assert len(result) == 1
        assert result[0].id == "note_2"

    @pytest.mark.asyncio
    async def test_list_nested_format_mind_map_detection(self, notes_api, mock_core):
        """Test mind map detection in nested format."""
        mock_core.rpc_call.return_value = [
            [
                ["mm_1", ["mm_1", '{"children": [], "title": "Mind Map"}', None, None, "MM"]],
                ["note_1", ["note_1", "Just text", None, None, "Note"]],
            ]
        ]

        result = await notes_api.list("nb_123")

        assert len(result) == 1
        assert result[0].id == "note_1"

    @pytest.mark.asyncio
    async def test_list_returns_empty_for_null_content(self, notes_api, mock_core):
        """Test that notes with null content are still included."""
        mock_core.rpc_call.return_value = [
            [
                ["note_1", None],
            ]
        ]

        result = await notes_api.list("nb_123")

        # Note should be included because content is None (not a mind map)
        assert len(result) == 1


# =============================================================================
# get() edge cases
# =============================================================================


class TestGetNote:
    """Edge case tests for get() method."""

    @pytest.mark.asyncio
    async def test_get_returns_none_for_empty_list(self, notes_api, mock_core):
        """Test get() returns None when notes list is empty."""
        mock_core.rpc_call.return_value = [[]]

        result = await notes_api.get("nb_123", "note_1")

        assert result is None

    @pytest.mark.asyncio
    async def test_get_matches_first_element(self, notes_api, mock_core):
        """Test that get() matches on item[0]."""
        mock_core.rpc_call.return_value = [
            [
                ["note_1", "Content 1"],
                ["note_2", "Content 2"],
            ]
        ]

        result = await notes_api.get("nb_123", "note_2")

        assert result is not None
        assert result.id == "note_2"
        assert result.content == "Content 2"


# =============================================================================
# create() edge cases
# =============================================================================


class TestCreateNote:
    """Edge case tests for create() method."""

    @pytest.mark.asyncio
    async def test_create_with_nested_result(self, notes_api, mock_core):
        """Test create() with nested result [[note_id]]."""
        mock_core.rpc_call.side_effect = [
            [["new_note_123"]],  # CREATE_NOTE response
            None,  # UPDATE_NOTE response
        ]

        result = await notes_api.create("nb_123", "Title", "Content")

        assert result.id == "new_note_123"
        assert result.title == "Title"
        assert result.content == "Content"

    @pytest.mark.asyncio
    async def test_create_with_flat_result(self, notes_api, mock_core):
        """Test create() with flat result [note_id] (string at index 0)."""
        mock_core.rpc_call.side_effect = [
            ["new_note_456"],  # CREATE_NOTE response
            None,  # UPDATE_NOTE response
        ]

        result = await notes_api.create("nb_123", "Title", "Content")

        assert result.id == "new_note_456"

    @pytest.mark.asyncio
    async def test_create_with_null_result(self, notes_api, mock_core):
        """Test create() when RPC returns None."""
        mock_core.rpc_call.return_value = None

        result = await notes_api.create("nb_123", "Title", "Content")

        assert result.id == ""
        assert result.title == "Title"
        assert result.content == "Content"

    @pytest.mark.asyncio
    async def test_create_with_empty_result(self, notes_api, mock_core):
        """Test create() when RPC returns empty list."""
        mock_core.rpc_call.return_value = []

        result = await notes_api.create("nb_123", "Title", "Content")

        assert result.id == ""

    @pytest.mark.asyncio
    async def test_create_calls_update_after_create(self, notes_api, mock_core):
        """Test that create() calls update() to set title."""
        mock_core.rpc_call.side_effect = [
            [["note_id"]],
            None,
        ]

        await notes_api.create("nb_123", "My Title", "My Content")

        # Should have 2 RPC calls: CREATE_NOTE then UPDATE_NOTE
        assert mock_core.rpc_call.call_count == 2

    @pytest.mark.asyncio
    async def test_create_skips_update_when_no_id(self, notes_api, mock_core):
        """Test that create() skips update when no note_id returned."""
        mock_core.rpc_call.return_value = None

        await notes_api.create("nb_123", "Title", "Content")

        # Should only have 1 RPC call (CREATE_NOTE)
        assert mock_core.rpc_call.call_count == 1


# =============================================================================
# update() tests
# =============================================================================


class TestUpdateNote:
    """Tests for update() method."""

    @pytest.mark.asyncio
    async def test_update_calls_rpc_with_correct_params(self, notes_api, mock_core):
        """Test that update() passes correct parameters."""
        mock_core.rpc_call.return_value = None

        await notes_api.update("nb_123", "note_456", "New content", "New title")

        mock_core.rpc_call.assert_called_once()
        call_args = mock_core.rpc_call.call_args
        params = call_args[0][1]

        assert params[0] == "nb_123"
        assert params[1] == "note_456"
        assert params[2] == [[["New content", "New title", [], 0]]]


# =============================================================================
# delete() tests
# =============================================================================


class TestDeleteNote:
    """Tests for delete() method."""

    @pytest.mark.asyncio
    async def test_delete_returns_true(self, notes_api, mock_core):
        """Test that delete() always returns True."""
        mock_core.rpc_call.return_value = None

        result = await notes_api.delete("nb_123", "note_456")

        assert result is True

    @pytest.mark.asyncio
    async def test_delete_calls_rpc_with_correct_params(self, notes_api, mock_core):
        """Test that delete() passes correct parameters."""
        mock_core.rpc_call.return_value = None

        await notes_api.delete("nb_123", "note_456")

        call_args = mock_core.rpc_call.call_args
        params = call_args[0][1]

        assert params[0] == "nb_123"
        assert params[1] is None
        assert params[2] == ["note_456"]


# =============================================================================
# list_mind_maps() tests
# =============================================================================


class TestListMindMaps:
    """Tests for list_mind_maps() method."""

    @pytest.mark.asyncio
    async def test_list_mind_maps_filters_regular_notes(self, notes_api, mock_core):
        """Test that list_mind_maps() excludes regular notes."""
        mock_core.rpc_call.return_value = [
            [
                ["note_1", "Regular note"],
                ["mm_1", '{"children": []}'],
            ]
        ]

        result = await notes_api.list_mind_maps("nb_123")

        assert len(result) == 1
        assert result[0][0] == "mm_1"

    @pytest.mark.asyncio
    async def test_list_mind_maps_returns_raw_data(self, notes_api, mock_core):
        """Test that list_mind_maps() returns raw items, not Note objects."""
        mock_core.rpc_call.return_value = [
            [
                ["mm_1", '{"children": []}'],
            ]
        ]

        result = await notes_api.list_mind_maps("nb_123")

        assert isinstance(result[0], list)
        assert result[0][0] == "mm_1"


# =============================================================================
# delete_mind_map() tests
# =============================================================================


class TestDeleteMindMap:
    """Tests for delete_mind_map() method."""

    @pytest.mark.asyncio
    async def test_delete_mind_map_returns_true(self, notes_api, mock_core):
        """Test that delete_mind_map() always returns True."""
        mock_core.rpc_call.return_value = None

        result = await notes_api.delete_mind_map("nb_123", "mm_456")

        assert result is True

    @pytest.mark.asyncio
    async def test_delete_mind_map_uses_same_rpc_as_delete(self, notes_api, mock_core):
        """Test that delete_mind_map() uses DELETE_NOTE RPC."""
        mock_core.rpc_call.return_value = None

        await notes_api.delete_mind_map("nb_123", "mm_456")

        call_args = mock_core.rpc_call.call_args
        params = call_args[0][1]

        assert params[0] == "nb_123"
        assert params[1] is None
        assert params[2] == ["mm_456"]
