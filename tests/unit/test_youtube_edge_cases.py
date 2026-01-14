"""Test edge cases that could cause YouTube URL misclassification.

These tests investigate potential bugs where valid YouTube URLs might
be incorrectly classified as web pages, causing the misindexing issue.
"""

from unittest.mock import MagicMock

import pytest

from notebooklm import NotebookLMClient
from notebooklm._url_utils import is_youtube_url


@pytest.fixture
def client():
    """Create a client instance for testing the extraction method."""
    mock_auth = MagicMock()
    mock_auth.cookies = {}
    mock_auth.csrf_token = "test"
    mock_auth.session_id = "test"
    return NotebookLMClient(mock_auth)


class TestURLEncodingEdgeCases:
    """Test URL encoding edge cases that could cause misclassification."""

    def test_url_encoded_equals_in_param(self, client):
        """Test URL with encoded equals sign (%3D) in v parameter."""
        # This shouldn't happen in practice but let's verify
        url = "https://www.youtube.com/watch?v%3DdQw4w9WgXcQ"
        # urlparse won't parse %3D as =, so this should fail
        result = client.sources._extract_youtube_video_id(url)
        # This is expected to fail - the v= param won't be found
        assert result is None  # Expected behavior - malformed URL

    def test_url_encoded_video_id(self, client):
        """Test URL with encoded characters in video ID."""
        # Video IDs with special characters that got URL encoded
        url = "https://www.youtube.com/watch?v=abc%2D123"  # %2D = hyphen
        result = client.sources._extract_youtube_video_id(url)
        # parse_qs should decode %2D back to -
        assert result == "abc-123"

    def test_double_encoded_url(self, client):
        """Test double-encoded URL (common copy-paste issue)."""
        # Double encoded: %253D = %3D = =
        url = "https://www.youtube.com/watch?v%253DdQw4w9WgXcQ"
        result = client.sources._extract_youtube_video_id(url)
        # Double encoding should cause failure
        assert result is None  # Expected - double encoding breaks parsing

    def test_url_with_plus_sign_in_video_id(self, client):
        """Test URL with + sign (sometimes used in base64-like IDs)."""
        # YouTube video IDs don't actually contain +, but let's verify
        url = "https://www.youtube.com/watch?v=abc+123"
        result = client.sources._extract_youtube_video_id(url)
        # + in query params is interpreted as space by parse_qs
        # So this should fail validation (space is not valid in video ID)
        assert result is None  # Expected - + is converted to space

    def test_url_with_encoded_plus(self, client):
        """Test URL with %2B (encoded plus sign)."""
        url = "https://www.youtube.com/watch?v=abc%2B123"
        result = client.sources._extract_youtube_video_id(url)
        # %2B decodes to +, which is not a valid video ID character
        assert result is None  # Expected - + is not valid


class TestUnicodeEdgeCases:
    """Test Unicode and internationalized URL edge cases."""

    def test_unicode_in_hostname(self, client):
        """Test IDN (internationalized domain name) that looks like YouTube."""
        # Homograph attack: using similar-looking Unicode characters
        # Note: This shouldn't match youtube.com
        url = "https://уoutube.com/watch?v=dQw4w9WgXcQ"  # Cyrillic у
        result = client.sources._extract_youtube_video_id(url)
        assert result is None  # Should not match - different hostname

    def test_punycode_youtube(self, client):
        """Test punycode-encoded YouTube URL."""
        # youtube.com in punycode (if someone maliciously encodes it)
        url = "https://xn--youtube-cua.com/watch?v=dQw4w9WgXcQ"
        result = client.sources._extract_youtube_video_id(url)
        assert result is None  # Should not match - different hostname


class TestFragmentEdgeCases:
    """Test URL fragment edge cases."""

    def test_url_with_fragment(self, client):
        """Test YouTube URL with fragment (hash)."""
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ#t=120"
        result = client.sources._extract_youtube_video_id(url)
        assert result == "dQw4w9WgXcQ"  # Fragment should be ignored

    def test_fragment_containing_v_param(self, client):
        """Test URL where v= is in fragment not query."""
        # Some SPAs use fragment-based routing
        url = "https://www.youtube.com/watch#v=dQw4w9WgXcQ"
        result = client.sources._extract_youtube_video_id(url)
        # This should fail - v= is in fragment, not query
        assert result is None


class TestWhitespaceEdgeCases:
    """Test whitespace handling in URLs."""

    def test_leading_whitespace(self, client):
        """Test URL with leading whitespace."""
        url = "  https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        result = client.sources._extract_youtube_video_id(url)
        # urlparse handles leading whitespace poorly
        # This might fail - let's see what happens
        # If this fails, we need to add .strip() before parsing
        assert result == "dQw4w9WgXcQ"

    def test_trailing_whitespace(self, client):
        """Test URL with trailing whitespace."""
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ  "
        result = client.sources._extract_youtube_video_id(url)
        assert result == "dQw4w9WgXcQ"

    def test_newline_in_url(self, client):
        """Test URL with embedded newline (copy-paste issue).

        Python's urlparse strips newlines from query strings, so the
        video ID is correctly extracted despite the newline.
        """
        url = "https://www.youtube.com/watch?v=dQw4w\n9WgXcQ"
        result = client.sources._extract_youtube_video_id(url)
        # urlparse strips newlines, so this works
        assert result == "dQw4w9WgXcQ"


class TestAuthCredentialEdgeCases:
    """Test URLs with authentication credentials."""

    def test_url_with_credentials(self, client):
        """Test URL with username:password (shouldn't happen but verify)."""
        url = "https://user:pass@www.youtube.com/watch?v=dQw4w9WgXcQ"
        result = client.sources._extract_youtube_video_id(url)
        # urlparse should still extract hostname correctly
        assert result == "dQw4w9WgXcQ"


class TestMalformedURLEdgeCases:
    """Test malformed URL edge cases."""

    def test_missing_protocol(self, client):
        """Test URL without protocol."""
        url = "www.youtube.com/watch?v=dQw4w9WgXcQ"
        result = client.sources._extract_youtube_video_id(url)
        # urlparse without protocol treats it as path
        assert result is None  # Expected - no hostname parsed

    def test_protocol_only(self, client):
        """Test URL with only protocol."""
        url = "https://"
        result = client.sources._extract_youtube_video_id(url)
        assert result is None

    def test_empty_string(self, client):
        """Test empty string."""
        url = ""
        result = client.sources._extract_youtube_video_id(url)
        assert result is None

    def test_just_hostname(self, client):
        """Test just hostname without path."""
        url = "https://www.youtube.com"
        result = client.sources._extract_youtube_video_id(url)
        assert result is None

    def test_triple_slash(self, client):
        """Test URL with triple slash."""
        url = "https:///www.youtube.com/watch?v=dQw4w9WgXcQ"
        result = client.sources._extract_youtube_video_id(url)
        # Triple slash means empty hostname, www.youtube.com becomes path
        assert result is None

    def test_double_question_mark(self, client):
        """Test URL with double question mark."""
        url = "https://www.youtube.com/watch??v=dQw4w9WgXcQ"
        result = client.sources._extract_youtube_video_id(url)
        # Double ? - first ? starts query, second ? is part of query string
        # parse_qs treats '?v=...' as the key, not 'v=...'
        assert result is None  # v param not found due to extra ?


class TestIsYouTubeUrlConsistency:
    """Test consistency between is_youtube_url() and _extract_youtube_video_id().

    The core bug risk: is_youtube_url() returns True but _extract_youtube_video_id()
    returns None, causing the URL to be added as web page but displayed as YouTube.
    """

    def test_channel_url_consistency(self, client):
        """Test YouTube channel URL - should NOT be detected as video."""
        url = "https://www.youtube.com/channel/UCuAXFkgsw1L7xaCfnd5JJOw"
        # is_youtube_url checks hostname only
        assert is_youtube_url(url) is True
        # _extract_youtube_video_id checks for video ID
        assert client.sources._extract_youtube_video_id(url) is None
        # MISMATCH: URL is "YouTube" but not a video
        # This is intentional - channel URLs aren't videos

    def test_playlist_url_consistency(self, client):
        """Test YouTube playlist URL (without v=) - should NOT be detected as video."""
        url = "https://www.youtube.com/playlist?list=PLrAXtmErZgOeiKm4sgNOknGvNjby9efdf"
        assert is_youtube_url(url) is True
        assert client.sources._extract_youtube_video_id(url) is None
        # MISMATCH: This would be added as web page, but is_youtube_url returns True

    def test_search_url_consistency(self, client):
        """Test YouTube search URL - should NOT be detected as video."""
        url = "https://www.youtube.com/results?search_query=python"
        assert is_youtube_url(url) is True
        assert client.sources._extract_youtube_video_id(url) is None
        # MISMATCH: Search results page

    def test_homepage_consistency(self, client):
        """Test YouTube homepage - should NOT be detected as video."""
        url = "https://www.youtube.com/"
        assert is_youtube_url(url) is True
        assert client.sources._extract_youtube_video_id(url) is None
        # MISMATCH: Homepage

    def test_feed_url_consistency(self, client):
        """Test YouTube feed URL - should NOT be detected as video."""
        url = "https://www.youtube.com/feed/subscriptions"
        assert is_youtube_url(url) is True
        assert client.sources._extract_youtube_video_id(url) is None
        # MISMATCH: Feed page

    def test_studio_url_consistency(self, client):
        """Test YouTube Studio URL - should NOT be detected as video."""
        url = "https://studio.youtube.com/channel/abc123"
        assert is_youtube_url(url) is True  # studio.youtube.com matches
        assert client.sources._extract_youtube_video_id(url) is None
        # MISMATCH: Studio page


class TestPotentialBugVectors:
    """Test specific scenarios that could cause the misindexing bug."""

    def test_youtube_url_with_only_list_param(self, client):
        """Test playlist watch URL without v= param.

        This is a real bug scenario: user copies a playlist video URL
        but the video ID gets stripped somehow.
        """
        url = "https://www.youtube.com/watch?list=PLrAXtmErZgOeiKm4sg"
        assert is_youtube_url(url) is True
        assert client.sources._extract_youtube_video_id(url) is None
        # BUG VECTOR: This URL would be added as web page!

    def test_youtube_url_with_empty_v_param(self, client):
        """Test URL with empty v= parameter."""
        url = "https://www.youtube.com/watch?v="
        assert is_youtube_url(url) is True
        result = client.sources._extract_youtube_video_id(url)
        # Empty video ID should fail validation
        assert result is None
        # BUG VECTOR: Empty v= causes web page indexing

    def test_youtube_url_with_invalid_video_id(self, client):
        """Test URL with invalid video ID characters."""
        url = "https://www.youtube.com/watch?v=abc!@#$%"
        assert is_youtube_url(url) is True
        result = client.sources._extract_youtube_video_id(url)
        assert result is None
        # BUG VECTOR: Invalid characters cause web page indexing
