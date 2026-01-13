"""Unit tests for URL validation utilities.

These tests verify that URL validation functions correctly prevent
substring-based bypass attacks (CodeQL: py/incomplete-url-substring-sanitization).
"""

import pytest

from notebooklm._url_utils import (
    contains_google_auth_redirect,
    is_google_auth_redirect,
    is_youtube_url,
)


class TestIsYoutubeUrl:
    """Tests for is_youtube_url() function."""

    @pytest.mark.parametrize(
        "url",
        [
            "https://youtube.com/watch?v=abc123",
            "https://www.youtube.com/watch?v=abc123",
            "https://m.youtube.com/watch?v=abc123",
            "https://music.youtube.com/watch?v=abc123",
            "http://youtube.com/watch?v=abc123",
            "https://youtu.be/abc123",
            "https://YOUTUBE.COM/watch?v=abc123",  # Case insensitive
        ],
    )
    def test_valid_youtube_urls(self, url: str):
        """Should return True for legitimate YouTube URLs."""
        assert is_youtube_url(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            # Path-based bypass attacks
            "https://evil.com/youtube.com/watch?v=abc123",
            "https://evil.com/www.youtube.com/video",
            "https://evil.com/path?redirect=youtube.com",
            # Subdomain spoofing attacks
            "https://youtube.com.evil.com/watch?v=abc123",
            "https://fake-youtube.com/watch?v=abc123",
            "https://notyoutube.com/watch?v=abc123",
            "https://evilyoutube.com/watch?v=abc123",
            # Other domains
            "https://vimeo.com/123456",
            "https://example.com/video",
            "https://google.com/youtube",
            # Malformed or empty
            "not-a-url",
            "",
            "javascript:alert('youtube.com')",
            "file:///etc/passwd?youtube.com",
        ],
    )
    def test_invalid_youtube_urls(self, url: str):
        """Should return False for non-YouTube or malicious URLs."""
        assert is_youtube_url(url) is False


class TestIsGoogleAuthRedirect:
    """Tests for is_google_auth_redirect() function."""

    @pytest.mark.parametrize(
        "url",
        [
            "https://accounts.google.com",
            "https://accounts.google.com/",
            "https://accounts.google.com/signin",
            "https://accounts.google.com/ServiceLogin",
            "http://accounts.google.com/login",
            "https://ACCOUNTS.GOOGLE.COM/signin",  # Case insensitive
        ],
    )
    def test_valid_google_auth_urls(self, url: str):
        """Should return True for Google accounts URLs."""
        assert is_google_auth_redirect(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            # Path-based bypass attacks
            "https://evil.com/accounts.google.com/signin",
            "https://evil.com?redirect=accounts.google.com",
            # Subdomain spoofing attacks
            "https://accounts.google.com.evil.com/signin",
            "https://fake-accounts.google.com/signin",
            "https://notaccounts.google.com/signin",
            "https://evilaccounts.google.com/signin",
            # Other Google domains (not auth)
            "https://google.com",
            "https://mail.google.com",
            "https://notebooklm.google.com",
            "https://www.google.com/accounts",
            # Malformed or empty
            "not-a-url",
            "",
            "javascript:alert('accounts.google.com')",
        ],
    )
    def test_invalid_google_auth_urls(self, url: str):
        """Should return False for non-auth or malicious URLs."""
        assert is_google_auth_redirect(url) is False


class TestContainsGoogleAuthRedirect:
    """Tests for contains_google_auth_redirect() function."""

    @pytest.mark.parametrize(
        "html",
        [
            '<a href="https://accounts.google.com/signin">Login</a>',
            'window.location = "https://accounts.google.com/ServiceLogin"',
            '{"redirect_url": "https://accounts.google.com/"}',
            "Redirecting to https://accounts.google.com/signin...",
        ],
    )
    def test_html_with_auth_redirect(self, html: str):
        """Should return True when HTML contains Google auth URL."""
        assert contains_google_auth_redirect(html) is True

    @pytest.mark.parametrize(
        "html",
        [
            '<a href="https://notebooklm.google.com">NotebookLM</a>',
            '<a href="https://example.com">Example</a>',
            # Should NOT match spoofed URLs
            '<a href="https://accounts.google.com.evil.com/">Fake</a>',
            '<a href="https://evil.com/accounts.google.com/">Path Bypass</a>',
            "No URLs here",
            "",
        ],
    )
    def test_html_without_auth_redirect(self, html: str):
        """Should return False when HTML doesn't contain Google auth URL."""
        assert contains_google_auth_redirect(html) is False

    def test_multiple_urls_one_is_auth(self):
        """Should return True if any URL is a Google auth redirect."""
        html = """
        <a href="https://example.com">Example</a>
        <a href="https://accounts.google.com/signin">Login Required</a>
        <a href="https://google.com">Google</a>
        """
        assert contains_google_auth_redirect(html) is True

    def test_multiple_urls_none_is_auth(self):
        """Should return False if no URL is a Google auth redirect."""
        html = """
        <a href="https://example.com">Example</a>
        <a href="https://notebooklm.google.com">NotebookLM</a>
        <a href="https://google.com">Google</a>
        """
        assert contains_google_auth_redirect(html) is False
