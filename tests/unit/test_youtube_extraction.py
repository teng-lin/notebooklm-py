"""Unit tests for YouTube URL extraction."""

import pytest
from unittest.mock import MagicMock


class TestYouTubeVideoIdExtraction:
    """Test _extract_youtube_video_id handles various YouTube URL formats."""

    @pytest.fixture
    def client(self):
        """Create a client instance for testing the extraction method."""
        from notebooklm.api_client import NotebookLMClient

        # Create client with mock auth (we only need the method, not network calls)
        mock_auth = MagicMock()
        mock_auth.cookies = {}
        mock_auth.csrf_token = "test"
        mock_auth.session_id = "test"
        return NotebookLMClient(mock_auth)

    def test_standard_watch_url(self, client):
        """Test standard youtube.com/watch?v= URLs."""
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        assert client._extract_youtube_video_id(url) == "dQw4w9WgXcQ"

    def test_standard_watch_url_without_www(self, client):
        """Test youtube.com/watch?v= URLs without www."""
        url = "https://youtube.com/watch?v=dQw4w9WgXcQ"
        assert client._extract_youtube_video_id(url) == "dQw4w9WgXcQ"

    def test_short_url(self, client):
        """Test youtu.be short URLs."""
        url = "https://youtu.be/dQw4w9WgXcQ"
        assert client._extract_youtube_video_id(url) == "dQw4w9WgXcQ"

    def test_shorts_url(self, client):
        """Test YouTube Shorts URLs."""
        url = "https://www.youtube.com/shorts/NZdU4m72QeI"
        assert client._extract_youtube_video_id(url) == "NZdU4m72QeI"

    def test_shorts_url_without_www(self, client):
        """Test YouTube Shorts URLs without www."""
        url = "https://youtube.com/shorts/NZdU4m72QeI"
        assert client._extract_youtube_video_id(url) == "NZdU4m72QeI"

    def test_http_urls(self, client):
        """Test HTTP (non-HTTPS) URLs still work."""
        url = "http://www.youtube.com/watch?v=dQw4w9WgXcQ"
        assert client._extract_youtube_video_id(url) == "dQw4w9WgXcQ"

    def test_non_youtube_url_returns_none(self, client):
        """Test non-YouTube URLs return None."""
        url = "https://example.com/video"
        assert client._extract_youtube_video_id(url) is None

    def test_invalid_youtube_url_returns_none(self, client):
        """Test invalid YouTube URLs return None."""
        url = "https://www.youtube.com/channel/abc123"
        assert client._extract_youtube_video_id(url) is None

    def test_video_id_with_hyphens_and_underscores(self, client):
        """Test video IDs with hyphens and underscores."""
        url = "https://www.youtube.com/shorts/NZdU4m72QeI"
        assert client._extract_youtube_video_id(url) == "NZdU4m72QeI"

        url = "https://youtu.be/abc-123_XYZ"
        assert client._extract_youtube_video_id(url) == "abc-123_XYZ"
