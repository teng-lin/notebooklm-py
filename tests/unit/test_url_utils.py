"""Unit tests for URL validation utilities.

These tests verify that URL validation functions correctly prevent
substring-based bypass attacks (CodeQL: py/incomplete-url-substring-sanitization).
"""

import pytest

from notebooklm._url_utils import (
    contains_google_auth_redirect,
    is_google_auth_redirect,
    is_notebooklm_unavailable_redirect,
    is_youtube_url,
    notebooklm_unavailable_location,
    pdf_url_display_title,
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


class TestUrlParsingExceptionPaths:
    """Cover the defensive ``except`` branches in the URL parsers.

    ``urlparse(...).hostname`` raises ``ValueError`` for malformed inputs
    such as an unterminated IPv6 literal. Both validators must swallow that
    and report a non-match rather than propagating the exception.
    """

    # ``http://[::1`` has an unterminated IPv6 host; ``.hostname`` raises
    # ``ValueError: Invalid IPv6 URL`` in CPython's urllib.
    MALFORMED_IPV6 = "http://[::1"

    def test_is_youtube_url_swallows_parse_error(self):
        assert is_youtube_url(self.MALFORMED_IPV6) is False

    def test_is_google_auth_redirect_swallows_parse_error(self):
        assert is_google_auth_redirect(self.MALFORMED_IPV6) is False

    def test_contains_google_auth_redirect_swallows_parse_error(self):
        # The malformed URL is extracted by the regex (it stops at the
        # space, leaving the unterminated ``http://[::1``), then routed
        # through ``is_google_auth_redirect`` where the ValueError is
        # swallowed.
        text = f"redirecting to {self.MALFORMED_IPV6} signin"
        assert contains_google_auth_redirect(text) is False

    def test_is_notebooklm_unavailable_redirect_swallows_parse_error(self):
        assert is_notebooklm_unavailable_redirect(self.MALFORMED_IPV6) is False

    def test_notebooklm_unavailable_location_swallows_parse_error(self):
        assert notebooklm_unavailable_location(self.MALFORMED_IPV6) is None


class TestIsNotebookLMUnavailableRedirect:
    """Tests for is_notebooklm_unavailable_redirect() — the region/anti-abuse gate (#1630)."""

    @pytest.mark.parametrize(
        "url",
        [
            "https://notebooklm.google",
            "https://notebooklm.google/",
            "https://notebooklm.google/?location=unsupported",
            "https://www.notebooklm.google/?location=unsupported",
            "http://notebooklm.google",
        ],
    )
    def test_marketing_host_is_gate(self, url: str):
        assert is_notebooklm_unavailable_redirect(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            # The APP host (with .com) is NOT the gate — the whole point.
            "https://notebooklm.google.com",
            "https://notebooklm.google.com/",
            "https://notebooklm.google.com/notebook/abc",
            "https://accounts.google.com/ServiceLogin",
            # Spoofed lookalikes must not match.
            "https://notebooklm.google.evil.com/",
            "https://evil.com/notebooklm.google",
            "https://fakenotebooklm.google/",
            "",
        ],
    )
    def test_non_gate_urls(self, url: str):
        assert is_notebooklm_unavailable_redirect(url) is False


class TestNotebookLMUnavailableLocation:
    """Tests for notebooklm_unavailable_location() — surfaces the ?location= diagnostic."""

    def test_extracts_location(self):
        assert (
            notebooklm_unavailable_location("https://notebooklm.google/?location=unsupported")
            == "unsupported"
        )

    @pytest.mark.parametrize(
        "url",
        [
            "https://notebooklm.google/",
            "https://notebooklm.google",
            "https://notebooklm.google/?foo=bar",
        ],
    )
    def test_no_location_returns_none(self, url: str):
        assert notebooklm_unavailable_location(url) is None

    def test_sanitizes_injected_value(self):
        # The value lands in a user-facing error string: control chars / spaces /
        # URL-shaped content are stripped, and the result is length-bounded.
        assert (
            notebooklm_unavailable_location(
                "https://notebooklm.google/?location=un%0Asupported%20hi"
            )
            == "unsupportedhi"
        )
        assert notebooklm_unavailable_location("https://notebooklm.google/?location=%0A%20") is None
        long = notebooklm_unavailable_location("https://notebooklm.google/?location=" + "a" * 200)
        assert long is not None and len(long) == 64


class TestPdfUrlDisplayTitle:
    """Tests for pdf_url_display_title() — the #1850 direct-PDF-URL title fallback."""

    @pytest.mark.parametrize(
        "url,expected",
        [
            # Happy path: basename stem, extension stripped.
            ("https://example.com/papers/SomePaper.pdf", "SomePaper"),
            # Query and fragment are ignored.
            ("https://example.com/papers/SomePaper.pdf?v=2#page=3", "SomePaper"),
            # Trailing slash is stripped before taking the basename.
            ("https://example.com/papers/SomePaper.pdf/", "SomePaper"),
            # Only the trailing .pdf is stripped; inner dots are preserved.
            ("https://example.com/paper.v2.pdf", "paper.v2"),
            # Case-insensitive extension match.
            ("https://example.com/SomePaper.PDF", "SomePaper"),
            # Percent-encoding is decoded (after the path is split into segments).
            ("https://example.com/%E6%97%A5%E6%9C%AC.pdf", "日本"),
            ("https://example.com/Some%20Paper.pdf", "Some Paper"),
        ],
    )
    def test_derives_clean_title(self, url: str, expected: str):
        assert pdf_url_display_title(url) == expected

    @pytest.mark.parametrize(
        "url",
        [
            # Root URL / no path segment → nothing to derive.
            "https://example.com/",
            "https://example.com",
            # Basename has no .pdf extension — the ".pdf" lives in the query, so
            # deriving "download" would be worse than keeping the URL.
            "https://example.com/download?file=x.pdf",
            # Directory-style URL whose leaf is not a .pdf file.
            "https://example.com/papers/",
            # Non-http(s) schemes are out of scope (also neutralizes data: noise).
            "data:application/pdf;base64,JVBERi0xLjQK",
            "ftp://example.com/paper.pdf",
            # Degenerate leaf: the whole basename is the extension.
            "https://example.com/.pdf",
            # A segment that is only control chars once the .pdf is stripped.
            "https://example.com/%00%01.pdf",
            # Percent-encoded path separators must not slip into the title.
            "https://example.com/%2Fa%2Fb.pdf",
            "https://example.com/..%2F.pdf",
        ],
    )
    def test_keeps_url_when_no_clean_title(self, url: str):
        assert pdf_url_display_title(url) is None

    def test_strips_control_chars_from_stem(self):
        # unquote can reintroduce control chars; they must never reach a title.
        assert pdf_url_display_title("https://example.com/a%0Ab.pdf") == "ab"

    def test_bounds_length(self):
        long_stem = "a" * 500
        result = pdf_url_display_title(f"https://example.com/{long_stem}.pdf")
        assert result is not None and len(result) == 200

    @pytest.mark.parametrize("bad", [None, 123, ["x"]])
    def test_non_string_input_returns_none(self, bad):
        assert pdf_url_display_title(bad) is None
