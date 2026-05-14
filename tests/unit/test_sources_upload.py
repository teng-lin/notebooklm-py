"""Unit tests for SourcesAPI file upload pipeline and YouTube detection."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from notebooklm._sources import SourcesAPI
from notebooklm.exceptions import NetworkError, RPCError, ValidationError
from notebooklm.rpc.types import SourceStatus
from notebooklm.types import Source


@pytest.fixture
def mock_core():
    """Create a mocked ClientCore for SourcesAPI."""
    core = MagicMock()
    core.rpc_call = AsyncMock()
    core.auth = MagicMock()
    # Upload paths pass the live http client's cookie jar to httpx so cookies
    # are scoped by Domain attribute (#373). The mock makes auth.cookie_jar and
    # get_http_client().cookies the same sentinel so existing assertions still
    # work against either reference.
    cookie_jar = MagicMock(name="cookie_jar")
    core.auth.cookie_jar = cookie_jar
    core.get_http_client.return_value.cookies = cookie_jar
    return core


@pytest.fixture
def sources_api(mock_core):
    """Create SourcesAPI with mocked core."""
    return SourcesAPI(mock_core)


# =============================================================================
# _extract_youtube_video_id() tests
# =============================================================================


class TestExtractYoutubeVideoId:
    """Tests for YouTube video ID extraction."""

    def test_extract_youtube_short_url(self, sources_api):
        """Test extraction from youtu.be short URLs."""
        url = "https://youtu.be/dQw4w9WgXcQ"
        result = sources_api._extract_youtube_video_id(url)
        assert result == "dQw4w9WgXcQ"

    def test_extract_youtube_short_url_http(self, sources_api):
        """Test extraction from HTTP youtu.be URLs."""
        url = "http://youtu.be/abc123_XYZ"
        result = sources_api._extract_youtube_video_id(url)
        assert result == "abc123_XYZ"

    def test_extract_youtube_standard_watch_url(self, sources_api):
        """Test extraction from standard watch URLs."""
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        result = sources_api._extract_youtube_video_id(url)
        assert result == "dQw4w9WgXcQ"

    def test_extract_youtube_watch_url_no_www(self, sources_api):
        """Test extraction from watch URLs without www."""
        url = "https://youtube.com/watch?v=abc123-_XY"
        result = sources_api._extract_youtube_video_id(url)
        assert result == "abc123-_XY"

    def test_extract_youtube_shorts_url(self, sources_api):
        """Test extraction from shorts URLs."""
        url = "https://www.youtube.com/shorts/abc123DEF"
        result = sources_api._extract_youtube_video_id(url)
        assert result == "abc123DEF"

    def test_extract_youtube_shorts_url_no_www(self, sources_api):
        """Test extraction from shorts URLs without www."""
        url = "https://youtube.com/shorts/xyz789"
        result = sources_api._extract_youtube_video_id(url)
        assert result == "xyz789"

    def test_extract_youtube_returns_none_for_non_youtube(self, sources_api):
        """Test that non-YouTube URLs return None."""
        url = "https://example.com/video"
        result = sources_api._extract_youtube_video_id(url)
        assert result is None

    def test_extract_youtube_returns_none_for_invalid_format(self, sources_api):
        """Test that invalid YouTube URLs return None."""
        url = "https://youtube.com/invalid/format"
        result = sources_api._extract_youtube_video_id(url)
        assert result is None

    def test_extract_youtube_with_hyphen_underscore_in_id(self, sources_api):
        """Test extraction with hyphens and underscores in video ID."""
        url = "https://youtu.be/a-b_c-D_E-f"
        result = sources_api._extract_youtube_video_id(url)
        assert result == "a-b_c-D_E-f"


# =============================================================================
# _register_file_source() tests
# =============================================================================


class TestRegisterFileSource:
    """Tests for file source registration."""

    @pytest.mark.asyncio
    async def test_register_file_source_success(self, sources_api, mock_core):
        """Test successful file source registration."""
        # Response structure: [[[["source_id_123"]]]] - 4 levels with string at deepest
        mock_core.rpc_call.return_value = [[[["source_id_abc"]]]]

        result = await sources_api._register_file_source("nb_123", "test.pdf")

        assert result == "source_id_abc"
        mock_core.rpc_call.assert_called_once()

    @pytest.mark.asyncio
    async def test_register_file_source_parses_deeply_nested(self, sources_api, mock_core):
        """Test parsing deeply nested response."""
        mock_core.rpc_call.return_value = [[[["my_source_id"]]]]

        result = await sources_api._register_file_source("nb_123", "doc.docx")

        assert result == "my_source_id"

    @pytest.mark.asyncio
    async def test_register_file_source_raises_on_null_response(self, sources_api, mock_core):
        """Test that null response raises SourceAddError."""
        from notebooklm.exceptions import SourceAddError

        mock_core.rpc_call.return_value = None

        with pytest.raises(SourceAddError, match="Failed to get SOURCE_ID"):
            await sources_api._register_file_source("nb_123", "test.pdf")

    @pytest.mark.asyncio
    async def test_register_file_source_raises_on_empty_response(self, sources_api, mock_core):
        """Test that empty response raises SourceAddError."""
        from notebooklm.exceptions import SourceAddError

        mock_core.rpc_call.return_value = []

        with pytest.raises(SourceAddError, match="Failed to get SOURCE_ID"):
            await sources_api._register_file_source("nb_123", "test.pdf")

    @pytest.mark.asyncio
    async def test_register_file_source_extracts_id_from_nested_lists(self, sources_api, mock_core):
        """Test that ID is extracted from arbitrarily nested lists."""
        # The flexible parser should extract "source_id_123" from any nesting depth
        mock_core.rpc_call.return_value = [[["source_id_123"]]]

        result = await sources_api._register_file_source("nb_123", "test.pdf")
        assert result == "source_id_123"

    @pytest.mark.asyncio
    async def test_register_file_source_raises_on_non_string_id(self, sources_api, mock_core):
        """Test that non-string source ID raises SourceAddError."""
        from notebooklm.exceptions import SourceAddError

        mock_core.rpc_call.return_value = [[[[[[12345]]]]]]

        with pytest.raises(SourceAddError, match="Failed to get SOURCE_ID"):
            await sources_api._register_file_source("nb_123", "test.pdf")

    @pytest.mark.asyncio
    async def test_register_file_source_handles_leading_none_shape(self, sources_api, mock_core):
        """Shape drift (#474): the new wrb.fr result_data starts with a None
        element, so the legacy position-0 walk lands on None. The full-tree
        scan should still find the UUID-shaped SOURCE_ID elsewhere.
        """
        uuid = "dc84ca28-2629-49ac-aec3-de45f0ec93e4"
        mock_core.rpc_call.return_value = [None, [[[uuid]]]]

        result = await sources_api._register_file_source("nb_123", "report.pdf")
        assert result == uuid

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "filename,response,expected",
        [
            # Filename echoed next to the SOURCE_ID (sibling).
            (
                "report.pdf",
                [[[["report.pdf", ["11111111-2222-3333-4444-555555555555"]]]]],
                "11111111-2222-3333-4444-555555555555",
            ),
            # Filename at the first leaf position — the legacy data[0] walker
            # would have returned 'doc.pdf' and broken downstream upload (#474).
            (
                "doc.pdf",
                [[["doc.pdf"], [["aabbccdd-eeff-1122-3344-556677889900"]]]],
                "aabbccdd-eeff-1122-3344-556677889900",
            ),
        ],
        ids=["filename-sibling-of-uuid", "filename-at-position-zero"],
    )
    async def test_register_file_source_prefers_uuid_over_echoed_filename(
        self, sources_api, mock_core, filename, response, expected
    ):
        """The extractor must skip the echoed filename and return the UUID,
        regardless of where the filename sits in the structure (#474).
        """
        mock_core.rpc_call.return_value = response

        result = await sources_api._register_file_source("nb_123", filename)
        assert result == expected

    @pytest.mark.asyncio
    async def test_register_file_source_falls_back_to_non_uuid_string(self, sources_api, mock_core):
        """Existing tests pass non-UUID IDs like 'src_pdf' — when no UUID
        candidate is present, the extractor falls back to the first non-
        filename string. Preserves backward compatibility with prior shapes.
        """
        mock_core.rpc_call.return_value = [[[["src_pdf"]]]]

        result = await sources_api._register_file_source("nb_123", "doc.pdf")
        assert result == "src_pdf"

    @pytest.mark.asyncio
    async def test_register_file_source_error_message_includes_shape_preview(
        self, sources_api, mock_core
    ):
        """Future shape drift should surface a structural preview in the error
        so users can file actionable bug reports (#474).
        """
        from notebooklm.exceptions import SourceAddError

        # Pure-numeric response — no string leaves → no candidates → raises.
        mock_core.rpc_call.return_value = [[[1, 2, 3]]]

        with pytest.raises(SourceAddError, match=r"Response shape:.*\[\[\[1, 2, 3\]\]\]"):
            await sources_api._register_file_source("nb_123", "test.pdf")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status_token", ["OK", "DONE", "true", "null"])
    async def test_register_file_source_rejects_all_alpha_status_tokens(
        self, sources_api, mock_core, status_token
    ):
        """Fallback must reject all-alpha status tokens — ``OK``/``DONE``/``true``
        in the response position would otherwise be picked up as a bogus
        SOURCE_ID and break the downstream upload silently.
        """
        from notebooklm.exceptions import SourceAddError

        mock_core.rpc_call.return_value = [[[status_token]]]

        with pytest.raises(SourceAddError, match="Failed to get SOURCE_ID"):
            await sources_api._register_file_source("nb_123", "test.pdf")

    @pytest.mark.asyncio
    async def test_register_file_source_wraps_rpc_error_with_status_code(
        self, sources_api, mock_core
    ):
        """When the decoder raises RPCError (e.g. server returned null result
        with a status code at wrb.fr[5] — the suspected #474 mode for account-
        routing mismatches), wrap the rich diagnostic into SourceAddError so
        the user sees actionable detail instead of a generic "Failed to get
        SOURCE_ID" with no context.
        """
        from notebooklm.exceptions import ClientError, SourceAddError

        # ClientError is what the decoder raises for status codes 5/7 (NOT_FOUND
        # / PERMISSION_DENIED) with an account-routing hint attached — exactly
        # the #114/#294 pattern we suspect for #474.
        mock_core.rpc_call.side_effect = ClientError(
            "RPC o4cbdc returned null result with status code 7 (Permission denied). "
            "If you have multiple Google accounts signed in...",
            method_id="o4cbdc",
            rpc_code=7,
        )

        with pytest.raises(SourceAddError, match="Permission denied") as exc_info:
            await sources_api._register_file_source("nb_123", "test.pdf")

        # The original RPCError is preserved as the cause so debuggers can
        # inspect the full decoder context.
        assert isinstance(exc_info.value.cause, ClientError)
        assert exc_info.value.cause.rpc_code == 7

    @pytest.mark.asyncio
    async def test_register_file_source_lets_auth_error_propagate(self, sources_api, mock_core):
        """AuthError must NOT be wrapped — the core auth-refresh retry path
        relies on the exception bubbling up unchanged.
        """
        from notebooklm.exceptions import AuthError

        mock_core.rpc_call.side_effect = AuthError("session expired")

        with pytest.raises(AuthError, match="session expired"):
            await sources_api._register_file_source("nb_123", "test.pdf")

    @pytest.mark.asyncio
    async def test_register_file_source_lets_rate_limit_error_propagate(
        self, sources_api, mock_core
    ):
        """RateLimitError must propagate so callers implementing back-off keep
        their specific exception type (and ``retry_after``) without having to
        unwrap ``SourceAddError.cause``.
        """
        from notebooklm.exceptions import RateLimitError

        mock_core.rpc_call.side_effect = RateLimitError(
            "API rate limit exceeded",
            method_id="o4cbdc",
            rpc_code="USER_DISPLAYABLE_ERROR",
        )

        with pytest.raises(RateLimitError, match="rate limit"):
            await sources_api._register_file_source("nb_123", "test.pdf")

    @pytest.mark.asyncio
    async def test_register_file_source_lets_server_error_propagate(self, sources_api, mock_core):
        """ServerError must propagate so callers handling transient 5xx
        backend errors keep the specific exception type for retry logic.
        """
        from notebooklm.exceptions import ServerError

        mock_core.rpc_call.side_effect = ServerError(
            "Backend unavailable",
            method_id="o4cbdc",
            rpc_code=500,
        )

        with pytest.raises(ServerError, match="Backend unavailable"):
            await sources_api._register_file_source("nb_123", "test.pdf")

    @pytest.mark.asyncio
    async def test_register_file_source_walker_has_recursion_guard(self, sources_api, mock_core):
        """A pathological deeply-nested response must not trigger
        RecursionError — the depth guard caps recursion at ``max_depth=50``.
        """
        # 200-deep nest with a UUID at the bottom. Past the depth guard the
        # walker stops, so the UUID is unreachable and we raise — but we don't
        # crash with RecursionError.
        from notebooklm.exceptions import SourceAddError

        deep: list = ["dc84ca28-2629-49ac-aec3-de45f0ec93e4"]
        for _ in range(200):
            deep = [deep]
        mock_core.rpc_call.return_value = deep

        with pytest.raises(SourceAddError, match="Failed to get SOURCE_ID"):
            await sources_api._register_file_source("nb_123", "test.pdf")


# =============================================================================
# _start_resumable_upload() tests
# =============================================================================


class TestStartResumableUpload:
    """Tests for starting resumable upload."""

    @pytest.mark.asyncio
    async def test_start_resumable_upload_success(self, sources_api, mock_core):
        """Test successful upload start."""
        mock_response = MagicMock()
        mock_response.headers = {"x-goog-upload-url": "https://upload.example.com/session123"}

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client.post.return_value = mock_response
            mock_client_cls.return_value = mock_client

            result = await sources_api._start_resumable_upload(
                "nb_123", "test.pdf", 1024, "src_456"
            )

        assert result == "https://upload.example.com/session123"

    @pytest.mark.asyncio
    async def test_start_resumable_upload_includes_correct_headers(self, sources_api, mock_core):
        """Test that upload start includes correct headers."""
        mock_response = MagicMock()
        mock_response.headers = {"x-goog-upload-url": "https://upload.example.com"}

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client.post.return_value = mock_response
            mock_client_cls.return_value = mock_client

            await sources_api._start_resumable_upload("nb_123", "test.pdf", 2048, "src_789")

            call_kwargs = mock_client.post.call_args[1]
            headers = call_kwargs["headers"]

            assert headers["x-goog-upload-command"] == "start"
            assert headers["x-goog-upload-header-content-length"] == "2048"
            assert headers["x-goog-upload-protocol"] == "resumable"
            # Cookie header is no longer set manually; httpx scopes cookies
            # by Domain attribute via the cookie_jar kwarg (#373).
            assert "Cookie" not in headers
            assert mock_client_cls.call_args.kwargs["cookies"] is mock_core.auth.cookie_jar

    @pytest.mark.asyncio
    async def test_start_resumable_upload_includes_json_body(self, sources_api, mock_core):
        """Test that upload start includes correct JSON body."""
        import json

        mock_response = MagicMock()
        mock_response.headers = {"x-goog-upload-url": "https://upload.example.com"}

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client.post.return_value = mock_response
            mock_client_cls.return_value = mock_client

            await sources_api._start_resumable_upload("nb_test", "myfile.pdf", 1000, "src_abc")

            call_kwargs = mock_client.post.call_args[1]
            body = json.loads(call_kwargs["content"])

            assert body["PROJECT_ID"] == "nb_test"
            assert body["SOURCE_NAME"] == "myfile.pdf"
            assert body["SOURCE_ID"] == "src_abc"

    @pytest.mark.asyncio
    async def test_start_resumable_upload_raises_on_missing_url_header(
        self, sources_api, mock_core
    ):
        """Test that missing upload URL header raises SourceAddError."""
        from notebooklm.exceptions import SourceAddError

        mock_response = MagicMock()
        mock_response.headers = {}  # No x-goog-upload-url

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client.post.return_value = mock_response
            mock_client_cls.return_value = mock_client

            with pytest.raises(SourceAddError, match="Failed to get upload URL"):
                await sources_api._start_resumable_upload("nb_123", "test.pdf", 1024, "src_456")

    @pytest.mark.asyncio
    async def test_start_resumable_upload_raises_on_http_error(self, sources_api, mock_core):
        """Test that HTTP error raises exception."""
        import httpx

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client.post.side_effect = httpx.HTTPStatusError(
                "Server Error", request=MagicMock(), response=MagicMock()
            )
            mock_client_cls.return_value = mock_client

            with pytest.raises(httpx.HTTPStatusError):
                await sources_api._start_resumable_upload("nb_123", "test.pdf", 1024, "src_456")


# =============================================================================
# _upload_file_streaming() tests
# =============================================================================


class TestUploadFileStreaming:
    """Tests for streaming file upload."""

    @pytest.mark.asyncio
    async def test_upload_file_streaming_success(self, sources_api, mock_core, tmp_path):
        """Test successful streaming file upload."""
        test_file = tmp_path / "test.txt"
        test_file.write_bytes(b"file content here")
        mock_response = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client.post.return_value = mock_response
            mock_client_cls.return_value = mock_client

            # Should not raise
            await sources_api._upload_file_streaming(
                "https://upload.example.com/session", test_file
            )

            mock_client.post.assert_called_once()

    @pytest.mark.asyncio
    async def test_upload_file_streaming_includes_correct_headers(
        self, sources_api, mock_core, tmp_path
    ):
        """Test that streaming upload includes correct headers."""
        test_file = tmp_path / "test.txt"
        test_file.write_bytes(b"content")
        mock_response = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client.post.return_value = mock_response
            mock_client_cls.return_value = mock_client

            await sources_api._upload_file_streaming(
                "https://upload.example.com/session", test_file
            )

            call_kwargs = mock_client.post.call_args[1]
            headers = call_kwargs["headers"]

            assert headers["x-goog-upload-command"] == "upload, finalize"
            assert headers["x-goog-upload-offset"] == "0"
            # Cookie header is no longer set manually; httpx scopes cookies
            # by Domain attribute via the cookie_jar kwarg (#373).
            assert "Cookie" not in headers
            assert mock_client_cls.call_args.kwargs["cookies"] is mock_core.auth.cookie_jar

    @pytest.mark.asyncio
    async def test_upload_file_streaming_uses_generator(self, sources_api, mock_core, tmp_path):
        """Test that file content is streamed via generator."""
        test_file = tmp_path / "test.txt"
        test_content = b"This is my file content"
        test_file.write_bytes(test_content)
        mock_response = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client.post.return_value = mock_response
            mock_client_cls.return_value = mock_client

            await sources_api._upload_file_streaming("https://upload.example.com", test_file)

            call_kwargs = mock_client.post.call_args[1]
            # Content should be a generator, not bytes
            content = call_kwargs["content"]
            # Consume the generator to verify it yields the file content
            chunks = [chunk async for chunk in content]
            assert b"".join(chunks) == test_content

    @pytest.mark.asyncio
    async def test_upload_file_streaming_raises_on_http_error(
        self, sources_api, mock_core, tmp_path
    ):
        """Test that HTTP error raises exception."""
        import httpx

        test_file = tmp_path / "test.txt"
        test_file.write_bytes(b"content")

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client.post.side_effect = httpx.HTTPStatusError(
                "Upload Failed", request=MagicMock(), response=MagicMock()
            )
            mock_client_cls.return_value = mock_client

            with pytest.raises(httpx.HTTPStatusError):
                await sources_api._upload_file_streaming("https://upload.example.com", test_file)


# =============================================================================
# add_file() tests
# =============================================================================


class TestAddFile:
    """Tests for the add_file() public method."""

    @pytest.mark.asyncio
    async def test_add_file_complete_flow(self, sources_api, mock_core, tmp_path):
        """Test complete file upload flow."""
        # Create a temp file
        test_file = tmp_path / "test.pdf"
        test_file.write_bytes(b"fake pdf content")

        # Mock the registration response - 4 levels with string at deepest
        mock_core.rpc_call.return_value = [[[["src_new_123"]]]]

        # Mock HTTP calls
        mock_start_response = MagicMock()
        mock_start_response.headers = {"x-goog-upload-url": "https://upload.example.com/session"}

        mock_upload_response = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client.post.side_effect = [mock_start_response, mock_upload_response]
            mock_client_cls.return_value = mock_client

            result = await sources_api.add_file("nb_123", str(test_file))

        assert result.id == "src_new_123"
        assert result.title == "test.pdf"
        assert result.kind == "unknown"
        assert mock_core.rpc_call.call_count == 1

    @pytest.mark.asyncio
    async def test_add_file_raises_file_not_found(self, sources_api, mock_core):
        """Test that non-existent file raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError, match="File not found"):
            await sources_api.add_file("nb_123", "/nonexistent/path/file.pdf")

    @pytest.mark.asyncio
    async def test_add_file_with_path_object(self, sources_api, mock_core, tmp_path):
        """Test add_file accepts Path objects."""
        test_file = tmp_path / "doc.txt"
        test_file.write_bytes(b"text content")

        mock_core.rpc_call.return_value = [[[["src_txt"]]]]

        mock_start_response = MagicMock()
        mock_start_response.headers = {"x-goog-upload-url": "https://upload.example.com"}
        mock_upload_response = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client.post.side_effect = [mock_start_response, mock_upload_response]
            mock_client_cls.return_value = mock_client

            result = await sources_api.add_file("nb_123", test_file)  # Path object

        assert result.id == "src_txt"
        assert result.title == "doc.txt"

    @pytest.mark.asyncio
    async def test_add_file_preserves_positional_wait_args(self, sources_api, mock_core, tmp_path):
        """Existing positional wait/wait_timeout callers remain compatible."""
        test_file = tmp_path / "report.pdf"
        test_file.write_bytes(b"fake pdf content")

        mock_core.rpc_call.return_value = [[[["src_pdf"]]]]
        sources_api.wait_until_ready = AsyncMock(
            return_value=MagicMock(id="src_pdf", title="report.pdf")
        )

        mock_start_response = MagicMock()
        mock_start_response.headers = {"x-goog-upload-url": "https://upload.example.com"}
        mock_upload_response = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client.post.side_effect = [mock_start_response, mock_upload_response]
            mock_client_cls.return_value = mock_client

            result = await sources_api.add_file("nb_123", str(test_file), None, True, 45.0)

        assert result.id == "src_pdf"
        sources_api.wait_until_ready.assert_awaited_once_with("nb_123", "src_pdf", timeout=45.0)

    @pytest.mark.asyncio
    async def test_add_file_with_custom_title_renames_after_upload(
        self, sources_api, mock_core, tmp_path
    ):
        """A non-null title that differs from the filename should trigger a rename
        and surface in the returned Source (regression test for #313).

        Per #388, supplying a title forces a brief wait before the rename so the
        UPDATE_SOURCE RPC reaches a registered source instead of silently
        no-opping.
        """
        test_file = tmp_path / "boring-filename.md"
        test_file.write_bytes(b"# content\n")

        # First rpc_call serves the file registration; the second serves rename().
        mock_core.rpc_call.side_effect = [
            [[[["src_md"]]]],
            [[[["src_md"], "Real Intended Title", [None, None, None, None, 8]]]],
        ]
        # The forced pre-rename registration wait is mocked — we don't want
        # this test to depend on the polling implementation. It returns the
        # registered source so add_file then issues the rename RPC.
        sources_api.wait_until_registered = AsyncMock(
            return_value=Source(id="src_md", title="boring-filename.md", _type_code=8)
        )
        sources_api.wait_until_ready = AsyncMock()

        mock_start_response = MagicMock()
        mock_start_response.headers = {"x-goog-upload-url": "https://upload.example.com"}
        mock_upload_response = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client.post.side_effect = [mock_start_response, mock_upload_response]
            mock_client_cls.return_value = mock_client

            result = await sources_api.add_file(
                "nb_123", str(test_file), title="  Real Intended Title  "
            )

        assert result.id == "src_md"
        assert result.title == "Real Intended Title"
        # 1 register + 1 rename
        assert mock_core.rpc_call.call_count == 2
        rename_params = mock_core.rpc_call.call_args_list[1].args[1]
        assert rename_params == [None, ["src_md"], [[["Real Intended Title"]]]]
        # Narrow wait uses the caller's wait_timeout (default 120s) — not the
        # full wait_until_ready. wait_until_registered returns on first
        # PROCESSING/READY status so the bound stays cheap in practice.
        sources_api.wait_until_registered.assert_awaited_once_with(
            "nb_123", "src_md", timeout=120.0
        )
        sources_api.wait_until_ready.assert_not_called()

    @pytest.mark.asyncio
    async def test_add_file_with_custom_title_renames_after_wait(
        self, sources_api, mock_core, tmp_path
    ):
        """The custom-title rename must run AFTER the source is fully registered
        server-side, otherwise the UPDATE_SOURCE RPC silently no-ops (#388).
        With wait=True the order is: register -> upload -> wait -> rename.
        """
        test_file = tmp_path / "boring-filename.md"
        test_file.write_bytes(b"# content\n")

        mock_core.rpc_call.side_effect = [
            [[[["src_md"]]]],
            [[[["src_md"], "Real Intended Title"]]],
        ]

        async def wait_side_effect(notebook_id, source_id, *, timeout):
            assert notebook_id == "nb_123"
            assert source_id == "src_md"
            assert timeout == 120.0
            # Wait must happen BEFORE the rename: at this point only the
            # registration RPC has been issued.
            assert mock_core.rpc_call.call_count == 1
            return Source(id=source_id, title="boring-filename.md", _type_code=8)

        sources_api.wait_until_ready = AsyncMock(side_effect=wait_side_effect)

        mock_start_response = MagicMock()
        mock_start_response.headers = {"x-goog-upload-url": "https://upload.example.com"}
        mock_upload_response = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client.post.side_effect = [mock_start_response, mock_upload_response]
            mock_client_cls.return_value = mock_client

            result = await sources_api.add_file(
                "nb_123",
                str(test_file),
                title="Real Intended Title",
                wait=True,
            )

        assert result.title == "Real Intended Title"
        sources_api.wait_until_ready.assert_awaited_once_with("nb_123", "src_md", timeout=120.0)
        # After the wait, the rename RPC fires — so the final RPC count is 2.
        assert mock_core.rpc_call.call_count == 2

    @pytest.mark.asyncio
    async def test_add_file_with_title_forces_wait_when_wait_false(
        self, sources_api, mock_core, tmp_path
    ):
        """Even with wait=False, supplying a custom title must force a brief wait
        so the rename hits a registered source instead of silently no-opping (#388).
        """
        test_file = tmp_path / "boring-filename.md"
        test_file.write_bytes(b"# content\n")

        mock_core.rpc_call.side_effect = [
            [[[["src_md"]]]],
            [[[["src_md"], "Real Intended Title", [None, None, None, None, 8]]]],
        ]

        async def wait_side_effect(notebook_id, source_id, *, timeout):
            # Narrow registration wait — wait_timeout default (120s) is forwarded
            # verbatim. wait_until_registered returns on the first non-PREPARING
            # status, so the bound stays cheap.
            assert timeout == 120.0
            # Wait runs BEFORE the rename: at this point only the register
            # RPC has been issued.
            assert mock_core.rpc_call.call_count == 1
            return Source(id=source_id, title="boring-filename.md", _type_code=8)

        sources_api.wait_until_registered = AsyncMock(side_effect=wait_side_effect)
        sources_api.wait_until_ready = AsyncMock()

        mock_start_response = MagicMock()
        mock_start_response.headers = {"x-goog-upload-url": "https://upload.example.com"}
        mock_upload_response = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client.post.side_effect = [mock_start_response, mock_upload_response]
            mock_client_cls.return_value = mock_client

            result = await sources_api.add_file(
                "nb_123",
                str(test_file),
                title="Real Intended Title",
            )

        assert result.title == "Real Intended Title"
        sources_api.wait_until_registered.assert_awaited_once_with(
            "nb_123", "src_md", timeout=120.0
        )
        sources_api.wait_until_ready.assert_not_called()

    @pytest.mark.asyncio
    async def test_add_file_with_title_forwards_wait_timeout(
        self, sources_api, mock_core, tmp_path
    ):
        """The caller's wait_timeout must be forwarded verbatim to the narrow
        registration wait. The wait_until_registered helper polls and returns
        on first PROCESSING/READY response, so generous bounds (e.g. long
        audio uploads passing wait_timeout=600) stay cheap in practice while
        still honoring the caller's intent if registration is unusually slow.
        """
        test_file = tmp_path / "podcast.mp3"
        test_file.write_bytes(b"fake audio")

        mock_core.rpc_call.side_effect = [
            [[[["src_audio"]]]],
            [[[["src_audio"], "Episode 1", [None, None, None, None, 10]]]],
        ]

        sources_api.wait_until_registered = AsyncMock(
            return_value=Source(id="src_audio", title="podcast.mp3", _type_code=10)
        )
        sources_api.wait_until_ready = AsyncMock()

        mock_start_response = MagicMock()
        mock_start_response.headers = {"x-goog-upload-url": "https://upload.example.com"}
        mock_upload_response = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client.post.side_effect = [mock_start_response, mock_upload_response]
            mock_client_cls.return_value = mock_client

            await sources_api.add_file(
                "nb_123",
                str(test_file),
                title="Episode 1",
                wait_timeout=600.0,
            )

        # wait_timeout is forwarded directly — no min() cap.
        sources_api.wait_until_registered.assert_awaited_once_with(
            "nb_123", "src_audio", timeout=600.0
        )
        sources_api.wait_until_ready.assert_not_called()

    @pytest.mark.asyncio
    async def test_add_file_no_title_no_wait_does_not_wait(self, sources_api, mock_core, tmp_path):
        """Back-compat: wait=False with no custom title must NOT call
        wait_until_ready — preserves the existing fast-return semantics.
        """
        test_file = tmp_path / "test.pdf"
        test_file.write_bytes(b"fake pdf content")

        mock_core.rpc_call.return_value = [[[["src_pdf"]]]]
        sources_api.wait_until_ready = AsyncMock()

        mock_start_response = MagicMock()
        mock_start_response.headers = {"x-goog-upload-url": "https://upload.example.com"}
        mock_upload_response = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client.post.side_effect = [mock_start_response, mock_upload_response]
            mock_client_cls.return_value = mock_client

            result = await sources_api.add_file("nb_123", str(test_file))

        assert result.id == "src_pdf"
        assert result.title == "test.pdf"
        sources_api.wait_until_ready.assert_not_called()

    @pytest.mark.asyncio
    async def test_add_file_no_title_no_wait_returns_processing_status(
        self, sources_api, mock_core, tmp_path
    ):
        """The fire-and-forget placeholder Source returned when wait=False and
        no custom title is supplied must carry status=PROCESSING. Previously
        it defaulted to READY, so callers saw is_ready=True on a source that
        had only just been registered. Regression guard for the CodeRabbit
        comment on #396.
        """
        test_file = tmp_path / "test.pdf"
        test_file.write_bytes(b"fake pdf content")

        mock_core.rpc_call.return_value = [[[["src_pdf"]]]]
        sources_api.wait_until_ready = AsyncMock()

        mock_start_response = MagicMock()
        mock_start_response.headers = {"x-goog-upload-url": "https://upload.example.com"}
        mock_upload_response = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client.post.side_effect = [mock_start_response, mock_upload_response]
            mock_client_cls.return_value = mock_client

            result = await sources_api.add_file("nb_123", str(test_file))

        assert result.status == SourceStatus.PROCESSING
        assert result.is_processing is True
        assert result.is_ready is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize("title", ["", "   "])
    async def test_add_file_rejects_blank_custom_title(
        self, sources_api, mock_core, tmp_path, title
    ):
        """Blank custom titles should fail before upload or rename starts."""
        test_file = tmp_path / "notes.md"
        test_file.write_bytes(b"# notes\n")

        with pytest.raises(ValidationError, match="Title cannot be empty"):
            await sources_api.add_file("nb_123", str(test_file), title=title)

        mock_core.rpc_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_add_file_skips_rename_when_title_matches_filename(
        self, sources_api, mock_core, tmp_path
    ):
        """If the caller passes a title equal to the on-disk filename, no rename
        RPC should be issued — saves a round-trip and avoids no-op writes.
        """
        test_file = tmp_path / "report.pdf"
        test_file.write_bytes(b"fake pdf content")

        mock_core.rpc_call.return_value = [[[["src_pdf"]]]]

        mock_start_response = MagicMock()
        mock_start_response.headers = {"x-goog-upload-url": "https://upload.example.com"}
        mock_upload_response = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client.post.side_effect = [mock_start_response, mock_upload_response]
            mock_client_cls.return_value = mock_client

            result = await sources_api.add_file("nb_123", str(test_file), title="report.pdf")

        assert result.id == "src_pdf"
        assert result.title == "report.pdf"
        # Only the registration call — no rename.
        assert mock_core.rpc_call.call_count == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "rename_error",
        [
            RPCError("rename rpc blew up"),
            NetworkError("rename network blew up"),
        ],
        ids=["rpc", "network"],
    )
    async def test_add_file_rename_failure_keeps_upload(
        self, sources_api, mock_core, tmp_path, caplog, rename_error
    ):
        """A failing rename must not lose the already-uploaded file; the
        registered source is returned and a warning is logged.
        """
        import logging

        test_file = tmp_path / "doc.txt"
        test_file.write_bytes(b"content")

        # Registration succeeds; rename raises a library-level expected error
        # (representative of what `self.rename` actually raises in the wild).
        # The forced wait between register and rename is mocked separately.
        mock_core.rpc_call.side_effect = [
            [[[["src_doc"]]]],
            rename_error,
        ]
        sources_api.wait_until_registered = AsyncMock(
            return_value=Source(id="src_doc", title="doc.txt", _type_code=4)
        )

        mock_start_response = MagicMock()
        mock_start_response.headers = {"x-goog-upload-url": "https://upload.example.com"}
        mock_upload_response = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client.post.side_effect = [mock_start_response, mock_upload_response]
            mock_client_cls.return_value = mock_client

            with caplog.at_level(logging.WARNING, logger="notebooklm._sources"):
                result = await sources_api.add_file("nb_123", str(test_file), title="Custom")

        # The already-registered source is preserved.
        assert result.id == "src_doc"
        assert result.title == "doc.txt"
        warning_records = [
            rec for rec in caplog.records if "rename to 'Custom' failed" in rec.message
        ]
        assert warning_records
        assert warning_records[0].exc_info is not None

    @pytest.mark.asyncio
    async def test_add_file_with_title_preserves_waited_metadata(
        self, sources_api, mock_core, tmp_path
    ):
        """Renaming after the forced wait must not null out _type_code/url/created_at.

        UPDATE_SOURCE's response shape can be sparse (see the rename() fallback
        for ``result is None`` in _sources.py). If add_file naively overwrites
        the fully-populated Source from wait_until_ready() with rename()'s
        sparse return value, the source loses its real type code, URL, and
        timestamp. Only the title should be taken from the rename response.
        """
        from datetime import datetime, timezone

        test_file = tmp_path / "podcast.mp3"
        test_file.write_bytes(b"fake audio")

        created_at = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

        # First rpc_call serves file registration. Second serves rename() —
        # which returns a sparse Source (only id + new title) so we can verify
        # the merge preserves type_code/url/created_at from the waited source.
        mock_core.rpc_call.side_effect = [
            [[[["src_audio"]]]],
            None,  # Triggers rename()'s Source(id=source_id, title=new_title) fallback
        ]
        sources_api.wait_until_registered = AsyncMock(
            return_value=Source(
                id="src_audio",
                title="podcast.mp3",
                _type_code=10,
                url="https://example.com/audio",
                created_at=created_at,
            )
        )

        mock_start_response = MagicMock()
        mock_start_response.headers = {"x-goog-upload-url": "https://upload.example.com"}
        mock_upload_response = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client.post.side_effect = [mock_start_response, mock_upload_response]
            mock_client_cls.return_value = mock_client

            result = await sources_api.add_file("nb_123", str(test_file), title="Episode 1")

        # New title is applied...
        assert result.title == "Episode 1"
        # ...but the metadata populated by wait_until_ready() survives.
        assert result._type_code == 10
        assert result.url == "https://example.com/audio"
        assert result.created_at == created_at

    @pytest.mark.asyncio
    async def test_add_file_title_uses_narrow_wait_not_full_wait(
        self, sources_api, mock_core, tmp_path
    ):
        """With wait=False but a custom title, add_file must call the narrow
        wait_until_registered helper, NOT the full wait_until_ready. This is
        the regression guard for the HIGH gemini-code-assist comment on #396:
        callers that pass wait=False should not be blocked on full processing
        just because they wanted to set a title.
        """
        test_file = tmp_path / "long-audio.mp3"
        test_file.write_bytes(b"fake audio")

        mock_core.rpc_call.side_effect = [
            [[[["src_audio"]]]],
            [[[["src_audio"], "My Title", [None, None, None, None, 10]]]],
        ]

        sources_api.wait_until_registered = AsyncMock(
            return_value=Source(id="src_audio", title="long-audio.mp3", _type_code=10)
        )
        sources_api.wait_until_ready = AsyncMock()

        mock_start_response = MagicMock()
        mock_start_response.headers = {"x-goog-upload-url": "https://upload.example.com"}
        mock_upload_response = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client.post.side_effect = [mock_start_response, mock_upload_response]
            mock_client_cls.return_value = mock_client

            await sources_api.add_file(
                "nb_123",
                str(test_file),
                title="My Title",
                wait=False,
            )

        # Narrow wait was used...
        sources_api.wait_until_registered.assert_awaited_once()
        # ...and the full wait was NOT.
        sources_api.wait_until_ready.assert_not_called()

    @pytest.mark.asyncio
    async def test_add_file_rename_failure_still_waits(self, sources_api, mock_core, tmp_path):
        """A failed custom-title rename should not prevent wait=True polling.
        With the new ordering, wait runs BEFORE rename, so this verifies the
        wait still happens and the upload is preserved when the rename fails.
        """
        test_file = tmp_path / "doc.txt"
        test_file.write_bytes(b"content")

        mock_core.rpc_call.side_effect = [
            [[[["src_doc"]]]],
            RPCError("rename rpc blew up"),
        ]

        async def wait_side_effect(notebook_id, source_id, *, timeout):
            assert notebook_id == "nb_123"
            assert source_id == "src_doc"
            assert timeout == 120.0
            # Wait runs BEFORE rename — only the register RPC has fired.
            assert mock_core.rpc_call.call_count == 1
            return Source(id=source_id, title="doc.txt", _type_code=4)

        sources_api.wait_until_ready = AsyncMock(side_effect=wait_side_effect)

        mock_start_response = MagicMock()
        mock_start_response.headers = {"x-goog-upload-url": "https://upload.example.com"}
        mock_upload_response = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__.return_value = mock_client
            mock_client.__aexit__.return_value = None
            mock_client.post.side_effect = [mock_start_response, mock_upload_response]
            mock_client_cls.return_value = mock_client

            result = await sources_api.add_file(
                "nb_123",
                str(test_file),
                title="Custom",
                wait=True,
            )

        assert result.title == "doc.txt"
        sources_api.wait_until_ready.assert_awaited_once_with("nb_123", "src_doc", timeout=120.0)


# =============================================================================
# add_url() with YouTube detection tests
# =============================================================================


class TestAddUrlWithYouTube:
    """Tests for add_url() with YouTube auto-detection."""

    @pytest.mark.asyncio
    async def test_add_url_detects_youtube_and_uses_youtube_method(self, sources_api, mock_core):
        """Test that YouTube URLs are detected and routed correctly."""
        mock_core.rpc_call.return_value = [[["src_yt"], "YouTube Video"]]

        await sources_api.add_url("nb_123", "https://youtu.be/dQw4w9WgXcQ")

        # Check that the RPC was called with YouTube-specific params
        call_args = mock_core.rpc_call.call_args
        params = call_args[0][1]
        # YouTube params have the URL at position [0][0][7]
        assert params[0][0][7] == ["https://youtu.be/dQw4w9WgXcQ"]

    @pytest.mark.asyncio
    async def test_add_url_uses_regular_method_for_non_youtube(self, sources_api, mock_core):
        """Test that non-YouTube URLs use regular add method."""
        mock_core.rpc_call.return_value = [[["src_url"], "Example Site"]]

        await sources_api.add_url("nb_123", "https://example.com/article")

        # Check that the RPC was called with regular URL params
        call_args = mock_core.rpc_call.call_args
        params = call_args[0][1]
        # Regular URL params have the URL at position [0][0][2] (different from YouTube's [7])
        assert params[0][0][2] == ["https://example.com/article"]

    @pytest.mark.asyncio
    async def test_add_url_wraps_rpc_error_with_status_code(self, sources_api, mock_core):
        """When ADD_SOURCE returns null with a status code at wrb.fr[5] —
        the #407 mode shared with #474 — the decoder raises ClientError /
        RPCError and add_url wraps it into SourceAddError with the rich
        diagnostic preserved as .cause. Previously allow_null=True on
        _add_youtube_source swallowed the status code silently.
        """
        from notebooklm.exceptions import ClientError, SourceAddError

        mock_core.rpc_call.side_effect = ClientError(
            "RPC <id> returned null result with status code 7 (Permission denied). ...",
            method_id="<id>",
            rpc_code=7,
        )

        with pytest.raises(SourceAddError) as exc_info:
            await sources_api.add_url("nb_123", "https://youtu.be/dQw4w9WgXcQ")

        assert isinstance(exc_info.value.cause, ClientError)
        assert exc_info.value.cause.rpc_code == 7

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "exc_factory",
        [
            lambda: __import__("notebooklm.exceptions", fromlist=["AuthError"]).AuthError(
                "session expired"
            ),
            lambda: __import__("notebooklm.exceptions", fromlist=["RateLimitError"]).RateLimitError(
                "rate limited", method_id="<id>"
            ),
            lambda: __import__("notebooklm.exceptions", fromlist=["ServerError"]).ServerError(
                "backend down", method_id="<id>", rpc_code=503
            ),
        ],
        ids=["auth", "rate-limit", "server"],
    )
    async def test_add_url_lets_transport_errors_propagate(
        self, sources_api, mock_core, exc_factory
    ):
        """AuthError, RateLimitError, and ServerError must NOT be wrapped —
        callers rely on the specific exception type for re-login / back-off
        / transient retry. Mirrors the propagation contract on
        _register_file_source for #474.
        """
        exc = exc_factory()
        mock_core.rpc_call.side_effect = exc

        with pytest.raises(type(exc)):
            await sources_api.add_url("nb_123", "https://example.com/article")


# =============================================================================
# _add_youtube_source() tests
# =============================================================================


class TestAddYoutubeSource:
    """Tests for _add_youtube_source() helper."""

    @pytest.mark.asyncio
    async def test_add_youtube_source_structure(self, sources_api, mock_core):
        """Test YouTube source params structure."""
        mock_core.rpc_call.return_value = [[["src_123"]]]

        await sources_api._add_youtube_source("nb_123", "https://youtu.be/abc123")

        call_args = mock_core.rpc_call.call_args
        params = call_args[0][1]

        # Verify structure: [[None, None, None, ..., [url], None, None, 1]]
        assert params[0][0][7] == ["https://youtu.be/abc123"]
        assert params[0][0][10] == 1  # YouTube indicator
        assert params[1] == "nb_123"


# =============================================================================
# _add_url_source() tests
# =============================================================================


class TestAddUrlSource:
    """Tests for _add_url_source() helper."""

    @pytest.mark.asyncio
    async def test_add_url_source_structure(self, sources_api, mock_core):
        """Test regular URL source params structure."""
        mock_core.rpc_call.return_value = [[["src_123"]]]

        await sources_api._add_url_source("nb_123", "https://example.com/page")

        call_args = mock_core.rpc_call.call_args
        params = call_args[0][1]

        # Verify structure: URL at position 2 (different from YouTube which uses position 7)
        assert params[0][0][2] == ["https://example.com/page"]
        assert params[1] == "nb_123"
        assert params[2] == [2]
        assert params[3] is None
        assert params[4] is None
