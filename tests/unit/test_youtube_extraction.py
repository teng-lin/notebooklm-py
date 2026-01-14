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


class TestYouTubeVideoIdQueryParamOrder:
    """Test that video ID extraction works regardless of query param order.

    YouTube URLs can have query params in any order (e.g., ?si=...&v=... or ?list=...&v=...).
    The old regex-based implementation only worked when v= was the first param.
    """

    @pytest.fixture
    def client(self):
        """Create a client instance for testing the extraction method."""
        mock_auth = MagicMock()
        mock_auth.cookies = {}
        mock_auth.csrf_token = "test"
        mock_auth.session_id = "test"
        return NotebookLMClient(mock_auth)

    def test_watch_url_with_timestamp(self, client):
        """Test watch URL with timestamp param after video ID."""
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=120"
        assert client.sources._extract_youtube_video_id(url) == "dQw4w9WgXcQ"

    def test_watch_url_with_si_param_first(self, client):
        """Test watch URL with si= (share tracking) before v=.

        This is YouTube's new share format and was a common cause of misindexing.
        """
        url = "https://www.youtube.com/watch?si=ABC123&v=dQw4w9WgXcQ"
        assert client.sources._extract_youtube_video_id(url) == "dQw4w9WgXcQ"

    def test_watch_url_with_list_param_first(self, client):
        """Test watch URL with list= (playlist) before v=."""
        url = "https://www.youtube.com/watch?list=PLrAXtmErZgOeiKm4sgNOknGvNjby9efdf&v=dQw4w9WgXcQ"
        assert client.sources._extract_youtube_video_id(url) == "dQw4w9WgXcQ"

    def test_watch_url_with_feature_param_first(self, client):
        """Test watch URL with feature=share before v=."""
        url = "https://www.youtube.com/watch?feature=share&v=dQw4w9WgXcQ"
        assert client.sources._extract_youtube_video_id(url) == "dQw4w9WgXcQ"

    def test_watch_url_with_multiple_params(self, client):
        """Test watch URL with multiple params in various orders."""
        url = "https://www.youtube.com/watch?list=PL123&v=dQw4w9WgXcQ&t=30&si=ABC"
        assert client.sources._extract_youtube_video_id(url) == "dQw4w9WgXcQ"


class TestYouTubeVideoIdSubdomains:
    """Test that video ID extraction works with YouTube subdomains.

    YouTube has multiple subdomains: m.youtube.com (mobile), music.youtube.com, etc.
    """

    @pytest.fixture
    def client(self):
        """Create a client instance for testing the extraction method."""
        mock_auth = MagicMock()
        mock_auth.cookies = {}
        mock_auth.csrf_token = "test"
        mock_auth.session_id = "test"
        return NotebookLMClient(mock_auth)

    def test_mobile_youtube(self, client):
        """Test m.youtube.com URLs (mobile)."""
        url = "https://m.youtube.com/watch?v=dQw4w9WgXcQ"
        assert client.sources._extract_youtube_video_id(url) == "dQw4w9WgXcQ"

    def test_youtube_music(self, client):
        """Test music.youtube.com URLs."""
        url = "https://music.youtube.com/watch?v=dQw4w9WgXcQ"
        assert client.sources._extract_youtube_video_id(url) == "dQw4w9WgXcQ"

    def test_mobile_youtube_with_params(self, client):
        """Test mobile YouTube URL with additional params."""
        url = "https://m.youtube.com/watch?si=ABC&v=dQw4w9WgXcQ&t=60"
        assert client.sources._extract_youtube_video_id(url) == "dQw4w9WgXcQ"


class TestYouTubeVideoIdPathFormats:
    """Test that video ID extraction works with different YouTube path formats.

    YouTube uses several path formats: /embed/, /live/, /v/ (legacy).
    """

    @pytest.fixture
    def client(self):
        """Create a client instance for testing the extraction method."""
        mock_auth = MagicMock()
        mock_auth.cookies = {}
        mock_auth.csrf_token = "test"
        mock_auth.session_id = "test"
        return NotebookLMClient(mock_auth)

    def test_embed_url(self, client):
        """Test embed URLs (/embed/VIDEO_ID)."""
        url = "https://www.youtube.com/embed/dQw4w9WgXcQ"
        assert client.sources._extract_youtube_video_id(url) == "dQw4w9WgXcQ"

    def test_live_url(self, client):
        """Test live URLs (/live/VIDEO_ID)."""
        url = "https://www.youtube.com/live/dQw4w9WgXcQ"
        assert client.sources._extract_youtube_video_id(url) == "dQw4w9WgXcQ"

    def test_legacy_v_url(self, client):
        """Test legacy /v/ URLs."""
        url = "https://www.youtube.com/v/dQw4w9WgXcQ"
        assert client.sources._extract_youtube_video_id(url) == "dQw4w9WgXcQ"

    def test_embed_with_subdomain(self, client):
        """Test embed URL with subdomain."""
        url = "https://m.youtube.com/embed/dQw4w9WgXcQ"
        assert client.sources._extract_youtube_video_id(url) == "dQw4w9WgXcQ"

    def test_shorts_with_query_params(self, client):
        """Test shorts URL with query params."""
        url = "https://www.youtube.com/shorts/NZdU4m72QeI?feature=share"
        assert client.sources._extract_youtube_video_id(url) == "NZdU4m72QeI"


class TestYouTubeVideoIdEdgeCases:
    """Test edge cases and invalid URLs."""

    @pytest.fixture
    def client(self):
        """Create a client instance for testing the extraction method."""
        mock_auth = MagicMock()
        mock_auth.cookies = {}
        mock_auth.csrf_token = "test"
        mock_auth.session_id = "test"
        return NotebookLMClient(mock_auth)

    def test_empty_youtu_be_path(self, client):
        """Test youtu.be with no video ID."""
        url = "https://youtu.be/"
        assert client.sources._extract_youtube_video_id(url) is None

    def test_watch_without_v_param(self, client):
        """Test /watch URL without v= param."""
        url = "https://www.youtube.com/watch"
        assert client.sources._extract_youtube_video_id(url) is None

    def test_watch_with_only_list_param(self, client):
        """Test /watch URL with only list= (no v=)."""
        url = "https://www.youtube.com/watch?list=PLrAXtmErZgOeiKm4sgNOknGvNjby9efdf"
        assert client.sources._extract_youtube_video_id(url) is None

    def test_non_youtube_domain(self, client):
        """Test non-YouTube domain with similar path."""
        url = "https://example.com/watch?v=dQw4w9WgXcQ"
        assert client.sources._extract_youtube_video_id(url) is None

    def test_fake_youtube_subdomain(self, client):
        """Test fake domain that looks like YouTube subdomain."""
        url = "https://not-youtube.com/watch?v=dQw4w9WgXcQ"
        assert client.sources._extract_youtube_video_id(url) is None

    def test_channel_url_returns_none(self, client):
        """Test channel URLs don't return a video ID."""
        url = "https://www.youtube.com/channel/UCuAXFkgsw1L7xaCfnd5JJOw"
        assert client.sources._extract_youtube_video_id(url) is None

    def test_playlist_url_returns_none(self, client):
        """Test playlist URLs (without v=) don't return a video ID."""
        url = "https://www.youtube.com/playlist?list=PLrAXtmErZgOeiKm4sgNOknGvNjby9efdf"
        assert client.sources._extract_youtube_video_id(url) is None
