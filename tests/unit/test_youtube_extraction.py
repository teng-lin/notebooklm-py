"""Unit tests for YouTube URL extraction."""

from unittest.mock import MagicMock

import pytest

from notebooklm import NotebookLMClient


class TestYouTubeVideoIdExtraction:
    """Test _extract_youtube_video_id handles various YouTube URL formats."""

    @pytest.fixture
    def client(self):
        """Create a client instance for testing the extraction method."""
        # Create client with mock auth (we only need the method, not network calls)
        mock_auth = MagicMock()
        mock_auth.cookies = {}
        mock_auth.csrf_token = "test"
        mock_auth.session_id = "test"
        return NotebookLMClient(mock_auth)

    def test_standard_watch_url(self, client):
        """Test standard youtube.com/watch?v= URLs."""
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        assert client.sources._extract_youtube_video_id(url) == "dQw4w9WgXcQ"

    def test_standard_watch_url_without_www(self, client):
        """Test youtube.com/watch?v= URLs without www."""
        url = "https://youtube.com/watch?v=dQw4w9WgXcQ"
        assert client.sources._extract_youtube_video_id(url) == "dQw4w9WgXcQ"

    def test_short_url(self, client):
        """Test youtu.be short URLs."""
        url = "https://youtu.be/dQw4w9WgXcQ"
        assert client.sources._extract_youtube_video_id(url) == "dQw4w9WgXcQ"

    def test_shorts_url(self, client):
        """Test YouTube Shorts URLs."""
        url = "https://www.youtube.com/shorts/NZdU4m72QeI"
        assert client.sources._extract_youtube_video_id(url) == "NZdU4m72QeI"

    def test_shorts_url_without_www(self, client):
        """Test YouTube Shorts URLs without www."""
        url = "https://youtube.com/shorts/NZdU4m72QeI"
        assert client.sources._extract_youtube_video_id(url) == "NZdU4m72QeI"

    def test_http_urls(self, client):
        """Test HTTP (non-HTTPS) URLs still work."""
        url = "http://www.youtube.com/watch?v=dQw4w9WgXcQ"
        assert client.sources._extract_youtube_video_id(url) == "dQw4w9WgXcQ"

    def test_non_youtube_url_returns_none(self, client):
        """Test non-YouTube URLs return None."""
        url = "https://example.com/video"
        assert client.sources._extract_youtube_video_id(url) is None

    def test_invalid_youtube_url_returns_none(self, client):
        """Test invalid YouTube URLs return None."""
        url = "https://www.youtube.com/channel/abc123"
        assert client.sources._extract_youtube_video_id(url) is None

    def test_video_id_with_hyphens_and_underscores(self, client):
        """Test video IDs with hyphens and underscores."""
        url = "https://www.youtube.com/shorts/NZdU4m72QeI"
        assert client.sources._extract_youtube_video_id(url) == "NZdU4m72QeI"

        url = "https://youtu.be/abc-123_XYZ"
        assert client.sources._extract_youtube_video_id(url) == "abc-123_XYZ"

    def test_query_param_order_independence(self, client):
        """Test that v= parameter is found regardless of position in query string.

        This was a bug where ?si=...&v=... failed because the regex expected
        v= to be the first query parameter.
        """
        # v= is second param (common when copied from YouTube share)
        url = "https://www.youtube.com/watch?si=abc123&v=dQw4w9WgXcQ"
        assert client.sources._extract_youtube_video_id(url) == "dQw4w9WgXcQ"

        # Multiple params with v= in middle
        url = "https://www.youtube.com/watch?list=PLabc&v=dQw4w9WgXcQ&t=123"
        assert client.sources._extract_youtube_video_id(url) == "dQw4w9WgXcQ"

    def test_mobile_subdomain(self, client):
        """Test m.youtube.com mobile URLs."""
        url = "https://m.youtube.com/watch?v=dQw4w9WgXcQ"
        assert client.sources._extract_youtube_video_id(url) == "dQw4w9WgXcQ"

    def test_music_subdomain(self, client):
        """Test music.youtube.com URLs."""
        url = "https://music.youtube.com/watch?v=dQw4w9WgXcQ"
        assert client.sources._extract_youtube_video_id(url) == "dQw4w9WgXcQ"

    def test_embed_url(self, client):
        """Test YouTube embed URLs."""
        url = "https://www.youtube.com/embed/dQw4w9WgXcQ"
        assert client.sources._extract_youtube_video_id(url) == "dQw4w9WgXcQ"

    def test_live_url(self, client):
        """Test YouTube live stream URLs."""
        url = "https://www.youtube.com/live/dQw4w9WgXcQ"
        assert client.sources._extract_youtube_video_id(url) == "dQw4w9WgXcQ"

    def test_legacy_v_url(self, client):
        """Test legacy /v/ format URLs."""
        url = "https://www.youtube.com/v/dQw4w9WgXcQ"
        assert client.sources._extract_youtube_video_id(url) == "dQw4w9WgXcQ"

    def test_trailing_whitespace(self, client):
        """Test URLs with trailing whitespace are handled correctly.

        This was a bug where trailing whitespace in the video ID caused
        validation to fail.
        """
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ  "
        assert client.sources._extract_youtube_video_id(url) == "dQw4w9WgXcQ"

        url = "  https://youtu.be/dQw4w9WgXcQ  "
        assert client.sources._extract_youtube_video_id(url) == "dQw4w9WgXcQ"

    def test_youtu_be_with_params(self, client):
        """Test youtu.be short URLs with query parameters."""
        url = "https://youtu.be/dQw4w9WgXcQ?t=120"
        assert client.sources._extract_youtube_video_id(url) == "dQw4w9WgXcQ"

        url = "https://youtu.be/dQw4w9WgXcQ?si=abc123"
        assert client.sources._extract_youtube_video_id(url) == "dQw4w9WgXcQ"

    def test_shorts_with_query_params(self, client):
        """Test shorts URLs with query parameters (like tracking params)."""
        url = "https://www.youtube.com/shorts/NZdU4m72QeI?feature=share"
        assert client.sources._extract_youtube_video_id(url) == "NZdU4m72QeI"

    def test_uppercase_path_segments(self, client):
        """Test URLs with uppercase path segments are handled correctly.

        URL paths are case-insensitive for path type detection (shorts, embed, etc.)
        but the video ID itself preserves its original case.
        """
        # Uppercase SHORTS
        url = "https://www.youtube.com/SHORTS/NZdU4m72QeI"
        assert client.sources._extract_youtube_video_id(url) == "NZdU4m72QeI"

        # Mixed case Embed
        url = "https://www.youtube.com/Embed/dQw4w9WgXcQ"
        assert client.sources._extract_youtube_video_id(url) == "dQw4w9WgXcQ"

        # Uppercase LIVE
        url = "https://www.youtube.com/LIVE/abc123XYZ"
        assert client.sources._extract_youtube_video_id(url) == "abc123XYZ"

    def test_unsupported_subdomains_return_none(self, client):
        """Test that unsupported YouTube subdomains return None.

        Only www, m, and music subdomains are supported for video extraction.
        Other subdomains (gaming, studio, tv) fall back to web page indexing.
        """
        # gaming.youtube.com - not in supported domain list
        url = "https://gaming.youtube.com/watch?v=dQw4w9WgXcQ"
        assert client.sources._extract_youtube_video_id(url) is None

        # studio.youtube.com - not in supported domain list
        url = "https://studio.youtube.com/watch?v=dQw4w9WgXcQ"
        assert client.sources._extract_youtube_video_id(url) is None
