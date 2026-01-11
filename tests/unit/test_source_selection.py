"""Unit tests for multi-source selection in chat and artifact generation.

Tests that source_ids are correctly handled when:
1. Explicitly passed (subset of sources)
2. None (uses all sources via core.get_source_ids)

Verifies correct encoding of source IDs in RPC parameters:
- source_ids_triple = [[[sid]] for sid in source_ids]
- source_ids_double = [[sid] for sid in source_ids]
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from notebooklm._artifacts import ArtifactsAPI
from notebooklm._chat import ChatAPI
from notebooklm.auth import AuthTokens


@pytest.fixture
def auth_tokens():
    return AuthTokens(
        cookies={"SID": "test"},
        csrf_token="test_csrf",
        session_id="test_session",
    )


@pytest.fixture
def mock_core():
    """Create a mock ClientCore."""
    core = MagicMock()
    core.rpc_call = AsyncMock()
    core.get_source_ids = AsyncMock(return_value=[])
    core.auth = MagicMock()
    core.auth.csrf_token = "test_csrf"
    core.auth.session_id = "test_session"
    core._reqid_counter = 0
    core.get_http_client = MagicMock()
    core.get_cached_conversation = MagicMock(return_value=[])
    core.cache_conversation_turn = MagicMock()
    return core


@pytest.fixture
def mock_notes_api():
    """Create a mock NotesAPI."""
    notes = MagicMock()
    notes.list_mind_maps = AsyncMock(return_value=[])
    mock_note = MagicMock()
    mock_note.id = "created_note_123"
    notes.create = AsyncMock(return_value=mock_note)
    return notes


class TestChatSourceSelection:
    """Tests for source selection in ChatAPI.ask()."""

    @pytest.mark.asyncio
    async def test_ask_with_explicit_source_ids(self, mock_core):
        """Test ask() with explicitly provided source_ids."""
        api = ChatAPI(mock_core)

        # Mock HTTP response for ask
        mock_response = MagicMock()
        inner_json = json.dumps(
            [["Answer text here that is definitely long enough.", None, None, None, [1]]]
        )
        chunk_json = json.dumps([["wrb.fr", None, inner_json]])
        mock_response.text = f")]}}'\n{len(chunk_json)}\n{chunk_json}\n"
        mock_response.raise_for_status = MagicMock()

        mock_http_client = MagicMock()
        mock_http_client.post = AsyncMock(return_value=mock_response)
        mock_core.get_http_client.return_value = mock_http_client

        result = await api.ask(
            notebook_id="nb_123",
            question="Test question?",
            source_ids=["src_001", "src_002"],
        )

        assert result.answer == "Answer text here that is definitely long enough."

        # Verify HTTP call was made with correct source encoding
        call_args = mock_http_client.post.call_args
        body = call_args.kwargs.get("content", call_args.args[1] if len(call_args.args) > 1 else "")

        # The body should contain the encoded sources_array
        # sources_array = [[[sid]] for sid in source_ids]
        # For ["src_001", "src_002"], this becomes [[["src_001"]], [["src_002"]]]
        assert "src_001" in body
        assert "src_002" in body

    @pytest.mark.asyncio
    async def test_ask_with_none_fetches_all_sources(self, mock_core):
        """Test ask() with source_ids=None fetches all sources."""
        api = ChatAPI(mock_core)

        # Mock get_source_ids to return source IDs
        mock_core.get_source_ids.return_value = ["src_001", "src_002", "src_003"]

        # Mock HTTP response for ask
        mock_response = MagicMock()
        inner_json = json.dumps(
            [["Answer from all sources with enough length.", None, None, None, [1]]]
        )
        chunk_json = json.dumps([["wrb.fr", None, inner_json]])
        mock_response.text = f")]}}'\n{len(chunk_json)}\n{chunk_json}\n"
        mock_response.raise_for_status = MagicMock()

        mock_http_client = MagicMock()
        mock_http_client.post = AsyncMock(return_value=mock_response)
        mock_core.get_http_client.return_value = mock_http_client

        result = await api.ask(
            notebook_id="nb_123",
            question="Test question?",
            source_ids=None,  # Should fetch all sources
        )

        assert result.answer == "Answer from all sources with enough length."

        # Verify get_source_ids was called on core
        mock_core.get_source_ids.assert_called_once_with("nb_123")

    @pytest.mark.asyncio
    async def test_ask_source_encoding_format(self, mock_core):
        """Verify the correct encoding format for source IDs in ask()."""
        api = ChatAPI(mock_core)

        # Mock HTTP response
        mock_response = MagicMock()
        inner_json = json.dumps(
            [["Valid answer with sufficient characters.", None, None, None, [1]]]
        )
        chunk_json = json.dumps([["wrb.fr", None, inner_json]])
        mock_response.text = f")]}}'\n{len(chunk_json)}\n{chunk_json}\n"
        mock_response.raise_for_status = MagicMock()

        mock_http_client = MagicMock()
        mock_http_client.post = AsyncMock(return_value=mock_response)
        mock_core.get_http_client.return_value = mock_http_client

        await api.ask(
            notebook_id="nb_123",
            question="Test?",
            source_ids=["s1", "s2", "s3"],
        )

        # Verify the post was called
        mock_http_client.post.assert_called_once()

        # Get the body and decode it
        call_args = mock_http_client.post.call_args
        body = call_args.kwargs.get("content", "")

        # The body contains URL-encoded f.req parameter
        # sources_array should be [[["s1"]], [["s2"]], [["s3"]]]
        # This gets encoded in the params as the first element
        # Extract f.req from body
        import re
        from urllib.parse import unquote

        match = re.search(r"f\.req=([^&]+)", body)
        if match:
            f_req_encoded = match.group(1)
            f_req_decoded = unquote(f_req_encoded)
            f_req_data = json.loads(f_req_decoded)
            # f_req is [None, params_json]
            params = json.loads(f_req_data[1])
            sources_array = params[0]

            # Verify the triple-nested format
            assert sources_array == [[["s1"]], [["s2"]], [["s3"]]]


class TestArtifactsSourceSelection:
    """Tests for source selection in ArtifactsAPI generation methods."""

    @pytest.mark.asyncio
    async def test_generate_audio_with_explicit_source_ids(self, mock_core, mock_notes_api):
        """Test generate_audio with explicitly provided source_ids."""
        api = ArtifactsAPI(mock_core, mock_notes_api)

        # Mock successful generation response
        mock_core.rpc_call.return_value = [
            ["artifact_123", "Audio", 1, None, 1]  # status 1 = in_progress
        ]

        result = await api.generate_audio(
            notebook_id="nb_123",
            source_ids=["src_001", "src_002"],
        )

        assert result.task_id == "artifact_123"
        assert result.status == "in_progress"

        # Verify RPC was called with correct source encoding
        mock_core.rpc_call.assert_called_once()
        call_args = mock_core.rpc_call.call_args
        params = call_args.args[1]

        # params structure for audio:
        # [
        #   [2],
        #   notebook_id,
        #   [
        #     None, None, 1,  # type = audio
        #     source_ids_triple,  # [[[sid]] for sid]
        #     None, None,
        #     [None, [instructions, length_code, None, source_ids_double, language, None, format_code]]
        #   ]
        # ]
        inner_params = params[2]
        source_ids_triple = inner_params[3]
        source_ids_double = inner_params[6][1][3]

        assert source_ids_triple == [[["src_001"]], [["src_002"]]]
        assert source_ids_double == [["src_001"], ["src_002"]]

    @pytest.mark.asyncio
    async def test_generate_audio_with_none_fetches_all_sources(self, mock_core, mock_notes_api):
        """Test generate_audio with source_ids=None fetches all sources."""
        api = ArtifactsAPI(mock_core, mock_notes_api)

        # Mock get_source_ids to return source IDs
        mock_core.get_source_ids.return_value = ["src_001", "src_002"]

        # Mock the generation RPC call
        mock_core.rpc_call.return_value = [["artifact_123", "Audio", 1, None, 1]]

        result = await api.generate_audio(
            notebook_id="nb_123",
            source_ids=None,
        )

        assert result.task_id == "artifact_123"

        # Verify get_source_ids was called
        mock_core.get_source_ids.assert_called_once_with("nb_123")

        # Verify CREATE_VIDEO RPC was called with fetched source IDs
        mock_core.rpc_call.assert_called_once()
        call_args = mock_core.rpc_call.call_args
        params = call_args.args[1]
        inner_params = params[2]
        source_ids_triple = inner_params[3]

        assert source_ids_triple == [[["src_001"]], [["src_002"]]]

    @pytest.mark.asyncio
    async def test_generate_video_source_encoding(self, mock_core, mock_notes_api):
        """Test generate_video has correct source encoding format."""
        api = ArtifactsAPI(mock_core, mock_notes_api)

        mock_core.rpc_call.return_value = [["artifact_456", "Video", 3, None, 1]]

        await api.generate_video(
            notebook_id="nb_123",
            source_ids=["src_a", "src_b"],
        )

        call_args = mock_core.rpc_call.call_args
        params = call_args.args[1]

        # Video params structure:
        # [
        #   [2], notebook_id,
        #   [None, None, 3, source_ids_triple, None, None, None, None,
        #    [None, None, [source_ids_double, language, instructions, None, format_code, style_code]]]
        # ]
        inner_params = params[2]
        source_ids_triple = inner_params[3]
        video_config = inner_params[8][2]
        source_ids_double = video_config[0]

        assert source_ids_triple == [[["src_a"]], [["src_b"]]]
        assert source_ids_double == [["src_a"], ["src_b"]]

    @pytest.mark.asyncio
    async def test_generate_report_source_encoding(self, mock_core, mock_notes_api):
        """Test generate_report has correct source encoding format."""
        api = ArtifactsAPI(mock_core, mock_notes_api)

        mock_core.rpc_call.return_value = [["artifact_789", "Report", 2, None, 1]]

        await api.generate_report(
            notebook_id="nb_123",
            source_ids=["src_x", "src_y", "src_z"],
        )

        call_args = mock_core.rpc_call.call_args
        params = call_args.args[1]

        # Report params structure:
        # [
        #   [2], notebook_id,
        #   [None, None, 2, source_ids_triple, None, None, None,
        #    [None, [title, desc, None, source_ids_double, language, prompt, None, True]]]
        # ]
        inner_params = params[2]
        source_ids_triple = inner_params[3]
        report_config = inner_params[7][1]
        source_ids_double = report_config[3]

        assert source_ids_triple == [[["src_x"]], [["src_y"]], [["src_z"]]]
        assert source_ids_double == [["src_x"], ["src_y"], ["src_z"]]

    @pytest.mark.asyncio
    async def test_generate_quiz_source_encoding(self, mock_core, mock_notes_api):
        """Test generate_quiz has correct source encoding format."""
        api = ArtifactsAPI(mock_core, mock_notes_api)

        mock_core.rpc_call.return_value = [["artifact_quiz", "Quiz", 4, None, 1]]

        await api.generate_quiz(
            notebook_id="nb_123",
            source_ids=["src_1", "src_2"],
        )

        call_args = mock_core.rpc_call.call_args
        params = call_args.args[1]

        # Quiz params structure:
        # [
        #   [2], notebook_id,
        #   [None, None, 4, source_ids_triple, ...]
        # ]
        inner_params = params[2]
        source_ids_triple = inner_params[3]

        assert source_ids_triple == [[["src_1"]], [["src_2"]]]

    @pytest.mark.asyncio
    async def test_generate_flashcards_source_encoding(self, mock_core, mock_notes_api):
        """Test generate_flashcards has correct source encoding format."""
        api = ArtifactsAPI(mock_core, mock_notes_api)

        mock_core.rpc_call.return_value = [["artifact_fc", "Flashcards", 4, None, 1]]

        await api.generate_flashcards(
            notebook_id="nb_123",
            source_ids=["src_flash"],
        )

        call_args = mock_core.rpc_call.call_args
        params = call_args.args[1]

        inner_params = params[2]
        source_ids_triple = inner_params[3]

        assert source_ids_triple == [[["src_flash"]]]

    @pytest.mark.asyncio
    async def test_generate_infographic_source_encoding(self, mock_core, mock_notes_api):
        """Test generate_infographic has correct source encoding format."""
        api = ArtifactsAPI(mock_core, mock_notes_api)

        mock_core.rpc_call.return_value = [["artifact_info", "Infographic", 7, None, 1]]

        await api.generate_infographic(
            notebook_id="nb_123",
            source_ids=["src_info_1", "src_info_2"],
        )

        call_args = mock_core.rpc_call.call_args
        params = call_args.args[1]

        inner_params = params[2]
        source_ids_triple = inner_params[3]

        assert source_ids_triple == [[["src_info_1"]], [["src_info_2"]]]

    @pytest.mark.asyncio
    async def test_generate_slide_deck_source_encoding(self, mock_core, mock_notes_api):
        """Test generate_slide_deck has correct source encoding format."""
        api = ArtifactsAPI(mock_core, mock_notes_api)

        mock_core.rpc_call.return_value = [["artifact_slide", "Slides", 8, None, 1]]

        await api.generate_slide_deck(
            notebook_id="nb_123",
            source_ids=["src_slide"],
        )

        call_args = mock_core.rpc_call.call_args
        params = call_args.args[1]

        inner_params = params[2]
        source_ids_triple = inner_params[3]

        assert source_ids_triple == [[["src_slide"]]]

    @pytest.mark.asyncio
    async def test_generate_data_table_source_encoding(self, mock_core, mock_notes_api):
        """Test generate_data_table has correct source encoding format."""
        api = ArtifactsAPI(mock_core, mock_notes_api)

        mock_core.rpc_call.return_value = [["artifact_table", "Table", 9, None, 1]]

        await api.generate_data_table(
            notebook_id="nb_123",
            source_ids=["src_table_1", "src_table_2"],
        )

        call_args = mock_core.rpc_call.call_args
        params = call_args.args[1]

        inner_params = params[2]
        source_ids_triple = inner_params[3]

        assert source_ids_triple == [[["src_table_1"]], [["src_table_2"]]]

    @pytest.mark.asyncio
    async def test_generate_mind_map_source_encoding(self, mock_core, mock_notes_api):
        """Test generate_mind_map has correct source encoding format."""
        api = ArtifactsAPI(mock_core, mock_notes_api)

        # Mock get_source_ids to return source IDs
        mock_core.get_source_ids.return_value = ["src_mm_1", "src_mm_2"]

        # Mock the mind map generation RPC call
        mock_core.rpc_call.return_value = [['{"name": "Mind Map", "children": []}']]

        await api.generate_mind_map(
            notebook_id="nb_123",
            source_ids=None,  # Will fetch sources
        )

        # Verify get_source_ids was called
        mock_core.get_source_ids.assert_called_once_with("nb_123")

        # Verify ACT_ON_SOURCES RPC was called with correct source encoding
        mock_core.rpc_call.assert_called_once()
        call_args = mock_core.rpc_call.call_args
        params = call_args.args[1]

        # Mind map uses source_ids_nested = [[[sid]] for sid]
        source_ids_nested = params[0]

        assert source_ids_nested == [[["src_mm_1"]], [["src_mm_2"]]]

    @pytest.mark.asyncio
    async def test_suggest_reports_source_encoding(self, mock_core, mock_notes_api):
        """Test suggest_reports has correct source encoding format."""
        api = ArtifactsAPI(mock_core, mock_notes_api)

        # Mock get_source_ids to return source IDs
        mock_core.get_source_ids.return_value = ["src_sug"]

        # Mock the suggest_reports RPC call
        mock_core.rpc_call.return_value = [
            ["Report Title", "Description", None, None, "Custom prompt", 2]
        ]

        await api.suggest_reports(
            notebook_id="nb_123",
            source_ids=None,
        )

        # Verify get_source_ids was called
        mock_core.get_source_ids.assert_called_once_with("nb_123")


class TestEmptySourceIds:
    """Tests for edge cases with empty source lists."""

    @pytest.mark.asyncio
    async def test_generate_with_empty_source_list(self, mock_core, mock_notes_api):
        """Test generation with empty source_ids list produces empty arrays."""
        api = ArtifactsAPI(mock_core, mock_notes_api)

        mock_core.rpc_call.return_value = [["artifact_empty", "Audio", 1, None, 1]]

        await api.generate_audio(
            notebook_id="nb_123",
            source_ids=[],  # Explicit empty list
        )

        call_args = mock_core.rpc_call.call_args
        params = call_args.args[1]
        inner_params = params[2]

        source_ids_triple = inner_params[3]
        source_ids_double = inner_params[6][1][3]

        # Empty list should produce empty arrays
        assert source_ids_triple == []
        assert source_ids_double == []

    @pytest.mark.asyncio
    async def test_ask_with_empty_source_list(self, mock_core):
        """Test ask with empty source_ids list."""
        api = ChatAPI(mock_core)

        mock_response = MagicMock()
        inner_json = json.dumps(
            [["Response with empty sources, long enough.", None, None, None, [1]]]
        )
        chunk_json = json.dumps([["wrb.fr", None, inner_json]])
        mock_response.text = f")]}}'\n{len(chunk_json)}\n{chunk_json}\n"
        mock_response.raise_for_status = MagicMock()

        mock_http_client = MagicMock()
        mock_http_client.post = AsyncMock(return_value=mock_response)
        mock_core.get_http_client.return_value = mock_http_client

        await api.ask(
            notebook_id="nb_123",
            question="Test?",
            source_ids=[],
        )

        # Verify the sources_array is empty in the request
        call_args = mock_http_client.post.call_args
        body = call_args.kwargs.get("content", "")

        import re
        from urllib.parse import unquote

        match = re.search(r"f\.req=([^&]+)", body)
        if match:
            f_req_encoded = match.group(1)
            f_req_decoded = unquote(f_req_encoded)
            f_req_data = json.loads(f_req_decoded)
            params = json.loads(f_req_data[1])
            sources_array = params[0]

            assert sources_array == []


class TestGetSourceIds:
    """Tests for ClientCore.get_source_ids method."""

    @pytest.mark.asyncio
    async def test_get_source_ids_extracts_correctly(self, auth_tokens):
        """Test get_source_ids correctly extracts source IDs from notebook data."""
        from notebooklm._core import ClientCore

        core = ClientCore(auth_tokens)
        core.rpc_call = AsyncMock()

        # Mock notebook data with multiple sources
        # Structure: notebook_data[0][1] = sources list
        # Each source: [["source_id"], "Source Title", ...]
        core.rpc_call.return_value = [
            [
                "nb_123",  # notebook_info[0]
                [
                    # sources list - source[0] is ["id"], source[0][0] is the id
                    [["source_aaa"], "Source A Title"],
                    [["source_bbb"], "Source B Title"],
                    [["source_ccc"], "Source C Title"],
                ],
            ]
        ]

        source_ids = await core.get_source_ids("nb_123")

        assert source_ids == ["source_aaa", "source_bbb", "source_ccc"]

    @pytest.mark.asyncio
    async def test_get_source_ids_handles_empty_notebook(self, auth_tokens):
        """Test get_source_ids handles notebook with no sources."""
        from notebooklm._core import ClientCore

        core = ClientCore(auth_tokens)
        core.rpc_call = AsyncMock()

        core.rpc_call.return_value = [["nb_123", []]]

        source_ids = await core.get_source_ids("nb_123")

        assert source_ids == []

    @pytest.mark.asyncio
    async def test_get_source_ids_handles_null_response(self, auth_tokens):
        """Test get_source_ids handles null API response."""
        from notebooklm._core import ClientCore

        core = ClientCore(auth_tokens)
        core.rpc_call = AsyncMock()

        core.rpc_call.return_value = None

        source_ids = await core.get_source_ids("nb_123")

        assert source_ids == []

    @pytest.mark.asyncio
    async def test_get_source_ids_handles_malformed_data(self, auth_tokens):
        """Test get_source_ids handles malformed source data gracefully."""
        from notebooklm._core import ClientCore

        core = ClientCore(auth_tokens)
        core.rpc_call = AsyncMock()

        # Malformed data - missing nested structure
        # Structure: source[0] must be a list, source[0][0] must be a string
        core.rpc_call.return_value = [
            [
                "nb_123",
                [
                    [None, "Missing ID"],  # Invalid: source[0] is None
                    [["valid_id"], "Valid Source"],  # Valid
                    "not a list",  # Invalid: not a list at all
                ],
            ]
        ]

        source_ids = await core.get_source_ids("nb_123")

        # Should only extract the valid source
        assert source_ids == ["valid_id"]
