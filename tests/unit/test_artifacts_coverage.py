"""Additional unit tests to improve _artifacts.py coverage.

These tests target specific uncovered lines identified by coverage analysis.
"""

import asyncio
import warnings
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from notebooklm._artifacts import ArtifactsAPI
from notebooklm.rpc.decoder import RPCError
from notebooklm.types import ArtifactDownloadError


@pytest.fixture
def mock_artifacts_api():
    """Create an ArtifactsAPI with mocked core and notes API."""
    mock_core = MagicMock()
    mock_core.rpc_call = AsyncMock()
    mock_core.get_source_ids = AsyncMock(return_value=[])
    mock_notes = MagicMock()
    mock_notes.list_mind_maps = AsyncMock(return_value=[])
    mock_note = MagicMock()
    mock_note.id = "created_note_123"
    mock_notes.create = AsyncMock(return_value=mock_note)
    api = ArtifactsAPI(mock_core, notes_api=mock_notes)
    return api, mock_core


# =============================================================================
# TIER 1: _download_urls_batch tests (lines 1360-1390)
# =============================================================================


class TestDownloadUrlsBatch:
    """Test _download_urls_batch method for batch downloading."""

    @pytest.mark.asyncio
    async def test_batch_download_success(self, mock_artifacts_api, tmp_path):
        """Test successful batch download of multiple files."""
        api, _ = mock_artifacts_api

        # Create mock response with binary content
        mock_response = MagicMock()
        mock_response.content = b"binary media content"
        mock_response.headers = {"content-type": "video/mp4"}
        mock_response.raise_for_status = MagicMock()

        with (
            patch("notebooklm._artifacts.load_httpx_cookies", return_value={}),
            patch("httpx.AsyncClient") as mock_client_cls,
        ):
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            urls_and_paths = [
                ("https://example.com/file1.mp4", str(tmp_path / "file1.mp4")),
                ("https://example.com/file2.mp4", str(tmp_path / "file2.mp4")),
            ]

            result = await api._download_urls_batch(urls_and_paths)

        assert len(result) == 2
        assert str(tmp_path / "file1.mp4") in result
        assert str(tmp_path / "file2.mp4") in result

    @pytest.mark.asyncio
    async def test_batch_download_html_response_rejected(self, mock_artifacts_api, tmp_path):
        """Test that HTML responses raise ArtifactDownloadError (auth expired)."""
        api, _ = mock_artifacts_api

        # Mock response returning HTML instead of media
        mock_response = MagicMock()
        mock_response.content = b"<html>Login page</html>"
        mock_response.headers = {"content-type": "text/html"}
        mock_response.raise_for_status = MagicMock()

        with (
            patch("notebooklm._artifacts.load_httpx_cookies", return_value={}),
            patch("httpx.AsyncClient") as mock_client_cls,
        ):
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            urls_and_paths = [
                ("https://example.com/file.mp4", str(tmp_path / "file.mp4")),
            ]

            # HTML response should raise ArtifactDownloadError
            with pytest.raises(ArtifactDownloadError, match="Received HTML instead of media"):
                await api._download_urls_batch(urls_and_paths)

    @pytest.mark.asyncio
    async def test_batch_download_partial_failure(self, mock_artifacts_api, tmp_path):
        """Test batch download with one success and one failure."""
        api, _ = mock_artifacts_api

        success_response = MagicMock()
        success_response.content = b"valid content"
        success_response.headers = {"content-type": "video/mp4"}
        success_response.raise_for_status = MagicMock()

        with (
            patch("notebooklm._artifacts.load_httpx_cookies", return_value={}),
            patch("httpx.AsyncClient") as mock_client_cls,
        ):
            mock_client = AsyncMock()
            mock_client.get.side_effect = [success_response, httpx.HTTPError("Network error")]
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            urls_and_paths = [
                ("https://example.com/file1.mp4", str(tmp_path / "file1.mp4")),
                ("https://example.com/file2.mp4", str(tmp_path / "file2.mp4")),
            ]

            result = await api._download_urls_batch(urls_and_paths)

        # Only first file should succeed
        assert len(result) == 1
        assert str(tmp_path / "file1.mp4") in result


# =============================================================================
# TIER 1: _call_generate rate limit tests (lines 1326-1334)
# =============================================================================


class TestCallGenerateRateLimit:
    """Test _call_generate handling of rate limit errors."""

    @pytest.mark.asyncio
    async def test_rate_limit_returns_failed_status(self, mock_artifacts_api):
        """Test that USER_DISPLAYABLE_ERROR returns failed status."""
        api, mock_core = mock_artifacts_api

        # Simulate rate limit error from RPC
        mock_core.rpc_call.side_effect = RPCError(
            "Rate limit exceeded", code="USER_DISPLAYABLE_ERROR"
        )

        result = await api.generate_video("nb_123")

        assert result.status == "failed"
        assert result.error is not None
        assert "Rate limit" in result.error
        assert result.error_code == "USER_DISPLAYABLE_ERROR"

    @pytest.mark.asyncio
    async def test_other_rpc_error_propagates(self, mock_artifacts_api):
        """Test that non-rate-limit RPC errors propagate."""
        api, mock_core = mock_artifacts_api

        mock_core.rpc_call.side_effect = RPCError("Server error", code="INTERNAL_ERROR")

        with pytest.raises(RPCError, match="Server error"):
            await api.generate_video("nb_123")


# =============================================================================
# TIER 1: wait_for_completion timeout tests (lines 1085-1157)
# =============================================================================


class TestWaitForCompletion:
    """Test wait_for_completion timeout and backoff logic."""

    @pytest.mark.asyncio
    async def test_timeout_raises_error(self, mock_artifacts_api):
        """Test that timeout is raised after max wait time."""
        api, mock_core = mock_artifacts_api

        # Always return in_progress status
        mock_core.rpc_call.return_value = ["task_123", "in_progress", None, None]

        # Patch the event loop time to simulate time passing
        loop = asyncio.get_running_loop()

        time_values = iter([0, 0.1, 0.2, 0.5, 1.0, 2.0])

        def mock_time():
            try:
                return next(time_values)
            except StopIteration:
                return 10.0  # Exceed timeout

        with (
            patch.object(loop, "time", mock_time),
            patch("asyncio.sleep", new_callable=AsyncMock),
            pytest.raises(TimeoutError, match="timed out"),
        ):
            await api.wait_for_completion("nb_123", "task_123", timeout=1.5)

    @pytest.mark.asyncio
    async def test_wait_completes_successfully(self, mock_artifacts_api):
        """Test successful completion without timeout."""
        api, mock_core = mock_artifacts_api

        # Return completed on second poll
        mock_core.rpc_call.side_effect = [
            ["task_123", "in_progress", None, None],
            ["task_123", "completed", "http://url", None],
        ]

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await api.wait_for_completion("nb_123", "task_123", timeout=60.0)

        assert result.status == "completed"
        assert result.url == "http://url"

    @pytest.mark.asyncio
    async def test_poll_returns_none_uses_fallback(self, mock_artifacts_api):
        """Test fallback to _list_raw when poll_status returns None."""
        api, mock_core = mock_artifacts_api

        # First poll returns None, triggering fallback path
        # Then complete on second poll
        # Note: For audio artifacts (type=1), we need URL at art[6][5] to report completed
        mock_core.rpc_call.side_effect = [
            None,  # First call (poll_status) returns None, triggering fallback
            [
                [
                    [  # Second call (_list_raw) succeeds with URL data
                        "task_123",
                        "Title",
                        1,  # AUDIO
                        None,
                        3,  # COMPLETED
                        None,
                        [
                            None,
                            None,
                            None,
                            None,
                            None,
                            [["https://audio.url/file.mp4", None, "audio/mp4"]],
                        ],
                    ]
                ]
            ],
        ]

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await api.wait_for_completion("nb_123", "task_123", timeout=60.0)

        assert result.status == "completed"


# =============================================================================
# TIER 1: _parse_generation_result tests (lines 1423-1457)
# =============================================================================


class TestParseGenerationResult:
    """Test _parse_generation_result parsing logic."""

    def test_parse_null_result(self, mock_artifacts_api):
        """Test parsing None result returns failed status."""
        api, _ = mock_artifacts_api

        result = api._parse_generation_result(None)

        assert result.status == "failed"
        assert result.task_id == ""
        assert "no artifact_id" in result.error.lower()

    def test_parse_empty_list_result(self, mock_artifacts_api):
        """Test parsing empty list returns failed status."""
        api, _ = mock_artifacts_api

        result = api._parse_generation_result([])

        assert result.status == "failed"
        assert result.task_id == ""
        assert "no artifact_id" in result.error.lower()

    def test_parse_valid_in_progress(self, mock_artifacts_api):
        """Test parsing valid in_progress status (code 1)."""
        api, _ = mock_artifacts_api

        # Valid result with status code 1 (in_progress)
        result = api._parse_generation_result([["artifact_001", "Title", 1, None, 1]])

        assert result.task_id == "artifact_001"
        assert result.status == "in_progress"

    def test_parse_valid_completed(self, mock_artifacts_api):
        """Test parsing valid completed status (code 3)."""
        api, _ = mock_artifacts_api

        result = api._parse_generation_result([["artifact_002", "Title", 1, None, 3]])

        assert result.task_id == "artifact_002"
        assert result.status == "completed"

    def test_parse_unknown_status_code(self, mock_artifacts_api):
        """Test parsing unknown status code returns unknown."""
        api, _ = mock_artifacts_api

        result = api._parse_generation_result([["artifact_003", "Title", 1, None, 99]])

        assert result.task_id == "artifact_003"
        assert result.status == "unknown"  # Unknown codes return "unknown"


# =============================================================================
# TIER 2: Deprecation warning test (lines 1127-1135)
# =============================================================================


class TestDeprecationWarnings:
    """Test deprecation warnings."""

    @pytest.mark.asyncio
    async def test_poll_interval_deprecation_warning(self, mock_artifacts_api):
        """Test that poll_interval parameter triggers deprecation warning."""
        api, mock_core = mock_artifacts_api

        # Return completed immediately
        mock_core.rpc_call.return_value = ["task_123", "completed", "http://url", None]

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            await api.wait_for_completion(
                "nb_123",
                "task_123",
                poll_interval=5.0,  # Deprecated parameter
            )

        assert len(w) == 1
        assert issubclass(w[0].category, DeprecationWarning)
        assert "poll_interval is deprecated" in str(w[0].message)


# =============================================================================
# MEDIA READINESS TESTS (Issue #21 fix)
# =============================================================================


class TestIsValidMediaUrl:
    """Test _is_valid_media_url helper method."""

    def test_valid_https_url(self, mock_artifacts_api):
        """Test that valid HTTPS URL returns True."""
        api, _ = mock_artifacts_api
        assert api._is_valid_media_url("https://example.com/audio.mp3") is True

    def test_valid_http_url(self, mock_artifacts_api):
        """Test that valid HTTP URL returns True."""
        api, _ = mock_artifacts_api
        assert api._is_valid_media_url("http://example.com/video.mp4") is True

    def test_invalid_string_no_protocol(self, mock_artifacts_api):
        """Test that string without http(s) returns False."""
        api, _ = mock_artifacts_api
        assert api._is_valid_media_url("example.com/audio.mp3") is False

    def test_invalid_ftp_url(self, mock_artifacts_api):
        """Test that FTP URL returns False."""
        api, _ = mock_artifacts_api
        assert api._is_valid_media_url("ftp://example.com/file.mp3") is False

    def test_empty_string(self, mock_artifacts_api):
        """Test that empty string returns False."""
        api, _ = mock_artifacts_api
        assert api._is_valid_media_url("") is False

    def test_none_value(self, mock_artifacts_api):
        """Test that None returns False."""
        api, _ = mock_artifacts_api
        assert api._is_valid_media_url(None) is False

    def test_integer_value(self, mock_artifacts_api):
        """Test that integer returns False."""
        api, _ = mock_artifacts_api
        assert api._is_valid_media_url(123) is False

    def test_list_value(self, mock_artifacts_api):
        """Test that list returns False."""
        api, _ = mock_artifacts_api
        assert api._is_valid_media_url(["https://example.com"]) is False


class TestIsMediaReady:
    """Test _is_media_ready helper method."""

    def test_audio_with_valid_url(self, mock_artifacts_api):
        """Test audio artifact with valid URL returns True."""
        api, _ = mock_artifacts_api
        # Audio URL is at art[6][5][0][0]
        art = [
            "artifact_id",  # 0
            "title",  # 1
            1,  # 2: StudioContentType.AUDIO
            None,  # 3
            3,  # 4: ArtifactStatus.COMPLETED
            None,  # 5
            [
                None,
                None,
                None,
                None,
                None,
                [["https://audio.url/file.mp4", None, "audio/mp4"]],
            ],  # 6
        ]
        assert api._is_media_ready(art, 1) is True

    def test_audio_without_url(self, mock_artifacts_api):
        """Test audio artifact without URL returns False."""
        api, _ = mock_artifacts_api
        art = [
            "artifact_id",
            "title",
            1,  # AUDIO
            None,
            3,  # COMPLETED
            None,
            [None, None, None, None, None, []],  # Empty media list
        ]
        assert api._is_media_ready(art, 1) is False

    def test_audio_with_empty_media_list(self, mock_artifacts_api):
        """Test audio artifact with empty media list returns False."""
        api, _ = mock_artifacts_api
        art = [
            "artifact_id",
            "title",
            1,
            None,
            3,
            None,
            [None, None, None, None, None, None],  # media_list is None
        ]
        assert api._is_media_ready(art, 1) is False

    def test_audio_truncated_structure(self, mock_artifacts_api):
        """Test audio artifact with truncated structure returns False."""
        api, _ = mock_artifacts_api
        art = ["artifact_id", "title", 1, None, 3]  # Too short
        assert api._is_media_ready(art, 1) is False

    def test_video_with_valid_url(self, mock_artifacts_api):
        """Test video artifact with valid URL returns True."""
        api, _ = mock_artifacts_api
        art = [
            "artifact_id",
            "title",
            3,  # VIDEO
            None,
            3,  # COMPLETED
            None,
            None,
            None,
            [["https://video.url/file.mp4", None, "video/mp4"]],  # art[8]
        ]
        assert api._is_media_ready(art, 3) is True

    def test_video_without_url(self, mock_artifacts_api):
        """Test video artifact without URL returns False."""
        api, _ = mock_artifacts_api
        art = [
            "artifact_id",
            "title",
            3,
            None,
            3,
            None,
            None,
            None,
            [],  # Empty video metadata
        ]
        assert api._is_media_ready(art, 3) is False

    def test_video_truncated_structure(self, mock_artifacts_api):
        """Test video artifact with truncated structure returns False."""
        api, _ = mock_artifacts_api
        art = ["artifact_id", "title", 3, None, 3, None, None]  # Too short (no art[8])
        assert api._is_media_ready(art, 3) is False

    def test_slide_deck_with_valid_url(self, mock_artifacts_api):
        """Test slide deck artifact with valid URL returns True."""
        api, _ = mock_artifacts_api
        # Create array with 17+ elements, PDF URL at art[16][3]
        art = (
            ["artifact_id", "title", 8]
            + [None] * 13
            + [[None, None, None, "https://slides.url/deck.pdf"]]
        )
        assert api._is_media_ready(art, 8) is True

    def test_slide_deck_without_url(self, mock_artifacts_api):
        """Test slide deck artifact without URL returns False."""
        api, _ = mock_artifacts_api
        art = ["artifact_id", "title", 8] + [None] * 13 + [[None, None, None, None]]
        assert api._is_media_ready(art, 8) is False

    def test_slide_deck_truncated_structure(self, mock_artifacts_api):
        """Test slide deck artifact with truncated structure returns False."""
        api, _ = mock_artifacts_api
        art = ["artifact_id", "title", 8] + [None] * 10  # Too short
        assert api._is_media_ready(art, 8) is False

    def test_infographic_with_valid_url(self, mock_artifacts_api):
        """Test infographic artifact with valid URL returns True.

        The _find_infographic_url method iterates backwards through art, looking for:
        - item[2] = non-empty list (content)
        - item[2][0] = list with len > 1 (first_content)
        - item[2][0][1] = non-empty list (img_data)
        - item[2][0][1][0] = URL string
        """
        api, _ = mock_artifacts_api
        # Build correct structure: item with item[2][0][1][0] = URL
        # item = [None, None, [[dummy, [URL]]]]
        #        item[0]=None, item[1]=None, item[2]=[[dummy, [URL]]]
        #        item[2][0] = [dummy, [URL]]  (len=2, > 1)
        #        item[2][0][1] = [URL]
        #        item[2][0][1][0] = URL
        art = [
            "artifact_id",
            "title",
            7,  # INFOGRAPHIC
            None,
            3,  # COMPLETED
            None,
            None,
            None,
            None,
            [None, None, [["dummy", ["https://infographic.url/image.png"]]]],  # Valid structure
        ]
        assert api._is_media_ready(art, 7) is True

    def test_infographic_without_url(self, mock_artifacts_api):
        """Test infographic artifact without URL returns False."""
        api, _ = mock_artifacts_api
        # Structure without valid URL
        art = [
            "artifact_id",
            "title",
            7,  # INFOGRAPHIC
            None,
            3,  # COMPLETED
            None,
            None,
            None,
            None,
            [None, None, [[[None, []]]]],  # Empty img_data list
        ]
        assert api._is_media_ready(art, 7) is False

    def test_infographic_malformed_structure(self, mock_artifacts_api):
        """Test infographic with malformed structure returns False."""
        api, _ = mock_artifacts_api
        # Malformed - item[2][0] is not a list
        art = [
            "artifact_id",
            "title",
            7,  # INFOGRAPHIC
            None,
            3,  # COMPLETED
            None,
            None,
            None,
            None,
            [None, None, "not a list"],  # item[2] is not a list
        ]
        assert api._is_media_ready(art, 7) is False

    def test_infographic_truncated_structure(self, mock_artifacts_api):
        """Test infographic artifact with truncated structure returns False."""
        api, _ = mock_artifacts_api
        art = ["artifact_id", "title", 7, None, 3]  # Too short
        assert api._is_media_ready(art, 7) is False

    def test_non_media_artifact_returns_true(self, mock_artifacts_api):
        """Test non-media artifacts (Quiz, Report, etc.) always return True."""
        api, _ = mock_artifacts_api
        # Quiz (type 4) - no URL needed
        art = ["artifact_id", "title", 4, None, 3]
        assert api._is_media_ready(art, 4) is True

        # Report (type 2) - no URL needed
        art = ["artifact_id", "title", 2, None, 3]
        assert api._is_media_ready(art, 2) is True

        # Data Table (type 9) - no URL needed
        art = ["artifact_id", "title", 9, None, 3]
        assert api._is_media_ready(art, 9) is True

    def test_unexpected_structure_returns_false_for_media_types(self, mock_artifacts_api):
        """Test that malformed structure returns False for media types (not ready)."""
        api, _ = mock_artifacts_api
        # Malformed structure - doesn't have the expected nested structure
        art = "not a list"
        # Should return False because URLs can't be found
        assert api._is_media_ready(art, 1) is False  # AUDIO
        assert api._is_media_ready(art, 3) is False  # VIDEO
        assert api._is_media_ready(art, 7) is False  # INFOGRAPHIC
        assert api._is_media_ready(art, 8) is False  # SLIDE_DECK

    def test_unexpected_structure_returns_true_for_non_media_types(self, mock_artifacts_api):
        """Test that malformed structure returns True for non-media types."""
        api, _ = mock_artifacts_api
        # Malformed structure - but non-media types don't need URLs
        art = "not a list"
        # Should return True because non-media types only need status code
        assert api._is_media_ready(art, 2) is True  # REPORT
        assert api._is_media_ready(art, 4) is True  # QUIZ
        assert api._is_media_ready(art, 5) is True  # FLASHCARD
        assert api._is_media_ready(art, 9) is True  # DATA_TABLE

    def test_graceful_handling_non_subscriptable(self, mock_artifacts_api):
        """Test that non-subscriptable elements don't raise exceptions."""
        api, _ = mock_artifacts_api
        # art[6] is an int, not a list - should handle gracefully
        art = [
            "artifact_id",
            "title",
            1,  # AUDIO
            None,
            3,  # COMPLETED
            None,
            123,  # art[6] is an int, not a list
        ]
        # Should return False gracefully (isinstance check prevents access)
        assert api._is_media_ready(art, 1) is False


class TestPollStatusMediaReadiness:
    """Test poll_status with media readiness checking."""

    @pytest.mark.asyncio
    async def test_poll_status_audio_completed_with_url(self, mock_artifacts_api):
        """Test poll_status returns completed when audio URL is present."""
        api, mock_core = mock_artifacts_api

        # POLL_STUDIO returns None, triggering fallback
        mock_core.rpc_call.side_effect = [
            None,  # POLL_STUDIO
            [
                [
                    [  # LIST_ARTIFACTS response
                        "task_123",
                        "Audio Overview",
                        1,  # AUDIO
                        None,
                        3,  # COMPLETED
                        None,
                        [
                            None,
                            None,
                            None,
                            None,
                            None,
                            [["https://audio.url/file.mp4", None, "audio/mp4"]],
                        ],
                    ]
                ]
            ],
        ]

        status = await api.poll_status("nb_123", "task_123")
        assert status.status == "completed"

    @pytest.mark.asyncio
    async def test_poll_status_audio_completed_without_url(self, mock_artifacts_api):
        """Test poll_status returns in_progress when audio URL is missing."""
        api, mock_core = mock_artifacts_api

        # POLL_STUDIO returns None, triggering fallback
        mock_core.rpc_call.side_effect = [
            None,  # POLL_STUDIO
            [
                [
                    [  # LIST_ARTIFACTS response - status=COMPLETED but no URL
                        "task_123",
                        "Audio Overview",
                        1,  # AUDIO
                        None,
                        3,  # COMPLETED
                        None,
                        [None, None, None, None, None, []],  # Empty media list
                    ]
                ]
            ],
        ]

        status = await api.poll_status("nb_123", "task_123")
        # Should downgrade to in_progress because URL is missing
        assert status.status == "in_progress"

    @pytest.mark.asyncio
    async def test_poll_status_video_completed_without_url(self, mock_artifacts_api):
        """Test poll_status returns in_progress when video URL is missing."""
        api, mock_core = mock_artifacts_api

        mock_core.rpc_call.side_effect = [
            None,  # POLL_STUDIO
            [
                [
                    [  # LIST_ARTIFACTS - video with status=COMPLETED but no URL
                        "task_123",
                        "Video Overview",
                        3,  # VIDEO
                        None,
                        3,  # COMPLETED
                        None,
                        None,
                        None,
                        [],  # Empty video metadata
                    ]
                ]
            ],
        ]

        status = await api.poll_status("nb_123", "task_123")
        assert status.status == "in_progress"

    @pytest.mark.asyncio
    async def test_poll_status_quiz_completed_without_url_check(self, mock_artifacts_api):
        """Test poll_status returns completed for quiz (no URL check needed)."""
        api, mock_core = mock_artifacts_api

        mock_core.rpc_call.side_effect = [
            None,  # POLL_STUDIO
            [
                [
                    [  # LIST_ARTIFACTS - quiz
                        "task_123",
                        "Quiz",
                        4,  # QUIZ
                        None,
                        3,  # COMPLETED
                    ]
                ]
            ],
        ]

        status = await api.poll_status("nb_123", "task_123")
        # Quiz doesn't need URL check, should return completed
        assert status.status == "completed"

    @pytest.mark.asyncio
    async def test_poll_status_processing_status_unchanged(self, mock_artifacts_api):
        """Test poll_status returns in_progress for PROCESSING status (no URL check)."""
        api, mock_core = mock_artifacts_api

        mock_core.rpc_call.side_effect = [
            None,  # POLL_STUDIO
            [
                [
                    [  # LIST_ARTIFACTS - audio still processing
                        "task_123",
                        "Audio Overview",
                        1,  # AUDIO
                        None,
                        1,  # PROCESSING (not COMPLETED)
                        None,
                        [None, None, None, None, None, []],
                    ]
                ]
            ],
        ]

        status = await api.poll_status("nb_123", "task_123")
        # Should remain in_progress (original status)
        assert status.status == "in_progress"
