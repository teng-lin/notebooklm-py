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
        """Test that HTML responses are rejected (auth expired)."""
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

            result = await api._download_urls_batch(urls_and_paths)

        # HTML response should be rejected, returning empty list
        assert result == []

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
        mock_core.rpc_call.side_effect = [
            None,  # First call (poll_status) returns None, triggering fallback
            [[["task_123", "Title", 1, None, 3]]],  # Second call (_list_raw) succeeds
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
        """Test parsing unknown status code returns pending."""
        api, _ = mock_artifacts_api

        result = api._parse_generation_result([["artifact_003", "Title", 1, None, 99]])

        assert result.task_id == "artifact_003"
        assert result.status == "pending"  # Unknown codes default to pending


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
