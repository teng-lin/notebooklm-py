"""Unit tests for artifact download methods."""

import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from notebooklm._artifacts import ArtifactsAPI
from notebooklm.auth import AuthTokens


@pytest.fixture
def auth_tokens():
    return AuthTokens(
        cookies={"SID": "test"},
        csrf_token="csrf",
        session_id="session",
    )


@pytest.fixture
def mock_artifacts_api():
    """Create an ArtifactsAPI with mocked core and notes API."""
    mock_core = MagicMock()
    mock_core.rpc_call = AsyncMock()
    mock_core.get_source_ids = AsyncMock(return_value=[])
    mock_notes = MagicMock()
    mock_notes.list_mind_maps = AsyncMock(return_value=[])
    # Mock create to return a Note-like object with an id
    mock_note = MagicMock()
    mock_note.id = "created_note_123"
    mock_notes.create = AsyncMock(return_value=mock_note)
    api = ArtifactsAPI(mock_core, notes_api=mock_notes)
    return api, mock_core


class TestDownloadAudio:
    """Test download_audio method."""

    @pytest.mark.asyncio
    async def test_download_audio_success(self, mock_artifacts_api):
        """Test successful audio download."""
        api, mock_core = mock_artifacts_api
        # Mock artifact list response - type 1 (audio), status 3 (completed)
        mock_core.rpc_call.return_value = [
            [
                [
                    "audio_001",  # id
                    "Audio Title",  # title
                    1,  # type (audio)
                    None,  # ?
                    3,  # status (completed)
                    None,  # ?
                    [
                        None,
                        None,
                        None,
                        None,
                        None,
                        [  # metadata[6][5] = media list
                            ["https://example.com/audio.mp4", None, "audio/mp4"]
                        ],
                    ],
                ]
            ]
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = os.path.join(tmpdir, "audio.mp4")

            with patch.object(
                api, "_download_url", new_callable=AsyncMock, return_value=output_path
            ):
                result = await api.download_audio("nb_123", output_path)

            assert result == output_path

    @pytest.mark.asyncio
    async def test_download_audio_no_audio_found(self, mock_artifacts_api):
        """Test error when no audio artifact exists."""
        api, mock_core = mock_artifacts_api
        mock_core.rpc_call.return_value = [[]]  # Empty list

        with pytest.raises(ValueError, match="No completed audio"):
            await api.download_audio("nb_123", "/tmp/audio.mp4")

    @pytest.mark.asyncio
    async def test_download_audio_specific_id_not_found(self, mock_artifacts_api):
        """Test error when specific audio ID not found."""
        api, mock_core = mock_artifacts_api
        mock_core.rpc_call.return_value = [[["other_id", "Audio", 1, None, 3, None, [None] * 6]]]

        with pytest.raises(ValueError, match="Audio artifact audio_001 not found"):
            await api.download_audio("nb_123", "/tmp/audio.mp4", artifact_id="audio_001")

    @pytest.mark.asyncio
    async def test_download_audio_invalid_metadata(self, mock_artifacts_api):
        """Test error on invalid metadata structure."""
        api, mock_core = mock_artifacts_api
        mock_core.rpc_call.return_value = [
            [
                ["audio_001", "Audio", 1, None, 3, None, "not_a_list"]  # metadata should be list
            ]
        ]

        with pytest.raises(ValueError, match="Invalid audio metadata|Failed to parse"):
            await api.download_audio("nb_123", "/tmp/audio.mp4")


class TestDownloadVideo:
    """Test download_video method."""

    @pytest.mark.asyncio
    async def test_download_video_success(self, mock_artifacts_api):
        """Test successful video download."""
        api, mock_core = mock_artifacts_api

        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = os.path.join(tmpdir, "video.mp4")

            # Patch _list_raw to return video artifact data
            with patch.object(api, "_list_raw", new_callable=AsyncMock) as mock_list:
                # Type 3 (video), status 3 (completed), metadata at index 8
                mock_list.return_value = [
                    [
                        "video_001",
                        "Video Title",
                        3,
                        None,
                        3,
                        None,
                        None,
                        None,
                        [[["https://example.com/video.mp4", 4, "video/mp4"]]],
                    ]
                ]

                with patch.object(
                    api, "_download_url", new_callable=AsyncMock, return_value=output_path
                ):
                    result = await api.download_video("nb_123", output_path)

            assert result == output_path

    @pytest.mark.asyncio
    async def test_download_video_no_video_found(self, mock_artifacts_api):
        """Test error when no video artifact exists."""
        api, mock_core = mock_artifacts_api

        with patch.object(api, "_list_raw", new_callable=AsyncMock) as mock_list:
            mock_list.return_value = []

            with pytest.raises(ValueError, match="No completed video"):
                await api.download_video("nb_123", "/tmp/video.mp4")

    @pytest.mark.asyncio
    async def test_download_video_specific_id_not_found(self, mock_artifacts_api):
        """Test error when specific video ID not found."""
        api, mock_core = mock_artifacts_api

        with patch.object(api, "_list_raw", new_callable=AsyncMock) as mock_list:
            mock_list.return_value = [["other_id", "Video", 3, None, 3, None, None, None, []]]

            with pytest.raises(ValueError, match="Video artifact video_001 not found"):
                await api.download_video("nb_123", "/tmp/video.mp4", artifact_id="video_001")


class TestDownloadInfographic:
    """Test download_infographic method."""

    @pytest.mark.asyncio
    async def test_download_infographic_success(self, mock_artifacts_api):
        """Test successful infographic download."""
        api, mock_core = mock_artifacts_api

        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = os.path.join(tmpdir, "infographic.png")

            # Patch _list_raw to return infographic data
            with patch.object(api, "_list_raw", new_callable=AsyncMock) as mock_list:
                # Type 7 (infographic), status 3, metadata with nested URL structure
                mock_list.return_value = [
                    [
                        "infographic_001",
                        "Infographic Title",
                        7,
                        None,
                        3,
                        None,
                        None,
                        None,
                        None,
                        [[], [], [[None, ["https://example.com/infographic.png"]]]],
                    ]
                ]

                with patch.object(
                    api, "_download_url", new_callable=AsyncMock, return_value=output_path
                ):
                    result = await api.download_infographic("nb_123", output_path)

            assert result == output_path

    @pytest.mark.asyncio
    async def test_download_infographic_no_infographic_found(self, mock_artifacts_api):
        """Test error when no infographic artifact exists."""
        api, mock_core = mock_artifacts_api

        with patch.object(api, "_list_raw", new_callable=AsyncMock) as mock_list:
            mock_list.return_value = []

            with pytest.raises(ValueError, match="No completed infographic"):
                await api.download_infographic("nb_123", "/tmp/info.png")


class TestDownloadSlideDeck:
    """Test download_slide_deck method."""

    @pytest.mark.asyncio
    async def test_download_slide_deck_success(self, mock_artifacts_api):
        """Test successful slide deck PDF download."""
        api, mock_core = mock_artifacts_api

        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = os.path.join(tmpdir, "slides.pdf")

            # Patch _list_raw to return slide deck artifact data
            # Structure: artifact[16] = [config, title, slides_list, pdf_url]
            with patch.object(api, "_list_raw", new_callable=AsyncMock) as mock_list:
                # Create artifact with 17+ elements, type 8 (slide deck), status 3
                artifact = ["slide_001", "Slide Deck Title", 8, None, 3]
                # Pad to index 16
                artifact.extend([None] * 11)
                # Index 16: metadata with PDF URL at position 3
                artifact.append(
                    [
                        ["config"],
                        "Slide Deck Title",
                        [["slide1"], ["slide2"]],  # slides_list
                        "https://contribution.usercontent.google.com/download?filename=test.pdf",
                    ]
                )
                mock_list.return_value = [artifact]

                with patch.object(
                    api, "_download_url", new_callable=AsyncMock, return_value=output_path
                ):
                    result = await api.download_slide_deck("nb_123", output_path)

            assert result == output_path

    @pytest.mark.asyncio
    async def test_download_slide_deck_no_slides_found(self, mock_artifacts_api):
        """Test error when no slide deck artifact exists."""
        api, mock_core = mock_artifacts_api

        with patch.object(api, "_list_raw", new_callable=AsyncMock) as mock_list:
            mock_list.return_value = []

            with pytest.raises(ValueError, match="No completed slide"):
                await api.download_slide_deck("nb_123", "/tmp/slides.pdf")

    @pytest.mark.asyncio
    async def test_download_slide_deck_specific_id_not_found(self, mock_artifacts_api):
        """Test error when specific slide deck ID not found."""
        api, mock_core = mock_artifacts_api

        with patch.object(api, "_list_raw", new_callable=AsyncMock) as mock_list:
            # Need at least 17 elements for valid structure
            artifact = ["other_id", "Slides", 8, None, 3]
            artifact.extend([None] * 11)
            artifact.append([["config"], "title", [], "http://example.com/test.pdf"])
            mock_list.return_value = [artifact]

            with pytest.raises(ValueError, match="Slide deck slides_001 not found"):
                await api.download_slide_deck("nb_123", "/tmp/slides.pdf", artifact_id="slides_001")

    @pytest.mark.asyncio
    async def test_download_slide_deck_invalid_metadata(self, mock_artifacts_api):
        """Test error on invalid metadata structure."""
        api, mock_core = mock_artifacts_api

        with patch.object(api, "_list_raw", new_callable=AsyncMock) as mock_list:
            # Create artifact with invalid metadata (less than 4 elements)
            artifact = ["slide_001", "Slides", 8, None, 3]
            artifact.extend([None] * 11)
            artifact.append(["only", "two"])  # Invalid: needs 4 elements
            mock_list.return_value = [artifact]

            with pytest.raises(ValueError, match="Invalid slide deck metadata"):
                await api.download_slide_deck("nb_123", "/tmp/slides.pdf")


class TestMindMapGeneration:
    """Test mind map generation result parsing."""

    @pytest.mark.asyncio
    async def test_generate_mind_map_with_json_string(self, mock_artifacts_api):
        """Test parsing mind map response with JSON string."""
        api, mock_core = mock_artifacts_api
        # Mock get_source_ids for source ID fetching
        mock_core.get_source_ids.return_value = ["src_001"]
        # Mock the actual mind map generation RPC call
        mock_core.rpc_call.return_value = [
            [
                '{"nodes": [{"id": "1", "text": "Root"}]}',  # JSON string
                None,
                ["note_123"],  # note info (not used anymore, note is created explicitly)
            ]
        ]

        result = await api.generate_mind_map("nb_123")

        assert result is not None
        assert "mind_map" in result
        # note_id is now from the explicitly created note
        assert result["note_id"] == "created_note_123"

    @pytest.mark.asyncio
    async def test_generate_mind_map_with_dict(self, mock_artifacts_api):
        """Test parsing mind map response with dict."""
        api, mock_core = mock_artifacts_api
        # Mock get_source_ids for source ID fetching
        mock_core.get_source_ids.return_value = ["src_001"]
        # Mock the actual mind map generation RPC call
        mock_core.rpc_call.return_value = [
            [
                {"nodes": [{"id": "1"}]},  # Already a dict
                None,
                ["note_456"],  # note info (not used anymore)
            ]
        ]

        result = await api.generate_mind_map("nb_123")

        assert result is not None
        assert result["mind_map"]["nodes"][0]["id"] == "1"
        # note_id is now from the explicitly created note
        assert result["note_id"] == "created_note_123"

    @pytest.mark.asyncio
    async def test_generate_mind_map_empty_result(self, mock_artifacts_api):
        """Test mind map with empty/null result."""
        api, mock_core = mock_artifacts_api
        # Mock get_source_ids for source ID fetching
        mock_core.get_source_ids.return_value = ["src_001"]
        # Mock the actual mind map generation with empty response
        mock_core.rpc_call.return_value = None

        result = await api.generate_mind_map("nb_123")

        assert result["mind_map"] is None
        assert result["note_id"] is None


class TestDownloadUrl:
    """Test _download_url helper method."""

    @pytest.mark.asyncio
    async def test_download_url_direct(self, mock_artifacts_api):
        """Test direct URL download (not Google content domain)."""
        api, mock_core = mock_artifacts_api

        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = os.path.join(tmpdir, "file.mp4")

            # Create a mock httpx module
            import httpx as real_httpx

            mock_response = MagicMock()
            mock_response.headers = {"content-type": "video/mp4"}
            mock_response.content = b"fake video content"
            mock_response.raise_for_status = MagicMock()

            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)

            # Mock load_httpx_cookies to avoid requiring real auth files
            mock_cookies = MagicMock()
            with (
                patch.object(real_httpx, "AsyncClient", return_value=mock_client),
                patch("notebooklm._artifacts.load_httpx_cookies", return_value=mock_cookies),
            ):
                result = await api._download_url("https://other.example.com/file.mp4", output_path)

            assert result == output_path
