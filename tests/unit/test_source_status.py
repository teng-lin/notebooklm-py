"""Unit tests for source status and polling functionality."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from notebooklm._sources import SourcesAPI
from notebooklm.types import (
    Source,
    SourceError,
    SourceNotFoundError,
    SourceProcessingError,
    SourceStatus,
    SourceTimeoutError,
)


class TestSourceStatus:
    """Tests for SourceStatus enum."""

    def test_status_values(self):
        """Test SourceStatus enum values."""
        assert SourceStatus.PROCESSING == 1
        assert SourceStatus.READY == 2
        assert SourceStatus.ERROR == 3

    def test_status_is_int_enum(self):
        """Test that SourceStatus values can be compared with ints."""
        assert SourceStatus.READY == 2
        assert SourceStatus.READY == 2


class TestSourceStatusProperties:
    """Tests for Source status properties."""

    def test_is_ready_when_ready(self):
        """Test is_ready returns True for READY status."""
        source = Source(id="src_1", status=SourceStatus.READY)
        assert source.is_ready is True
        assert source.is_processing is False
        assert source.is_error is False

    def test_is_processing_when_processing(self):
        """Test is_processing returns True for PROCESSING status."""
        source = Source(id="src_1", status=SourceStatus.PROCESSING)
        assert source.is_ready is False
        assert source.is_processing is True
        assert source.is_error is False

    def test_is_error_when_error(self):
        """Test is_error returns True for ERROR status."""
        source = Source(id="src_1", status=SourceStatus.ERROR)
        assert source.is_ready is False
        assert source.is_processing is False
        assert source.is_error is True

    def test_default_status_is_ready(self):
        """Test that default status is READY."""
        source = Source(id="src_1")
        assert source.status == SourceStatus.READY
        assert source.is_ready is True


class TestSourceExceptions:
    """Tests for source exception classes."""

    def test_source_error_is_base(self):
        """Test SourceError is the base exception."""
        assert issubclass(SourceProcessingError, SourceError)
        assert issubclass(SourceTimeoutError, SourceError)
        assert issubclass(SourceNotFoundError, SourceError)

    def test_source_processing_error(self):
        """Test SourceProcessingError attributes."""
        error = SourceProcessingError("src_123", status=3)
        assert error.source_id == "src_123"
        assert error.status == 3
        assert "src_123" in str(error)

    def test_source_timeout_error(self):
        """Test SourceTimeoutError attributes."""
        error = SourceTimeoutError("src_123", timeout=60.0, last_status=1)
        assert error.source_id == "src_123"
        assert error.timeout == 60.0
        assert error.last_status == 1
        assert "60.0" in str(error)
        assert "src_123" in str(error)

    def test_source_not_found_error(self):
        """Test SourceNotFoundError attributes."""
        error = SourceNotFoundError("src_123")
        assert error.source_id == "src_123"
        assert "src_123" in str(error)


class TestWaitUntilReady:
    """Tests for wait_until_ready method."""

    @pytest.fixture
    def sources_api(self):
        """Create a SourcesAPI with mocked core."""
        core = MagicMock()
        return SourcesAPI(core)

    @pytest.mark.asyncio
    async def test_returns_immediately_if_ready(self, sources_api):
        """Test that wait_until_ready returns immediately if source is ready."""
        ready_source = Source(id="src_1", title="Test", status=SourceStatus.READY)

        with patch.object(sources_api, "get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = ready_source

            result = await sources_api.wait_until_ready("nb_1", "src_1", timeout=10.0)

            assert result.is_ready
            assert mock_get.call_count == 1

    @pytest.mark.asyncio
    async def test_polls_until_ready(self, sources_api):
        """Test that wait_until_ready polls until source becomes ready."""
        processing_source = Source(id="src_1", status=SourceStatus.PROCESSING)
        ready_source = Source(id="src_1", status=SourceStatus.READY)

        call_count = 0

        async def mock_get(notebook_id, source_id):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                return processing_source
            return ready_source

        with patch.object(sources_api, "get", side_effect=mock_get):
            result = await sources_api.wait_until_ready(
                "nb_1", "src_1", timeout=10.0, initial_interval=0.01
            )

            assert result.is_ready
            assert call_count == 3

    @pytest.mark.asyncio
    async def test_raises_processing_error_on_error_status(self, sources_api):
        """Test that wait_until_ready raises SourceProcessingError on ERROR status."""
        error_source = Source(id="src_1", status=SourceStatus.ERROR)

        with patch.object(sources_api, "get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = error_source

            with pytest.raises(SourceProcessingError) as exc_info:
                await sources_api.wait_until_ready("nb_1", "src_1", timeout=10.0)

            assert exc_info.value.source_id == "src_1"
            assert exc_info.value.status == SourceStatus.ERROR

    @pytest.mark.asyncio
    async def test_raises_not_found_error_when_source_missing(self, sources_api):
        """Test that wait_until_ready raises SourceNotFoundError when source not found."""
        with patch.object(sources_api, "get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = None

            with pytest.raises(SourceNotFoundError) as exc_info:
                await sources_api.wait_until_ready("nb_1", "src_1", timeout=10.0)

            assert exc_info.value.source_id == "src_1"

    @pytest.mark.asyncio
    async def test_raises_timeout_error(self, sources_api):
        """Test that wait_until_ready raises SourceTimeoutError on timeout."""
        processing_source = Source(id="src_1", status=SourceStatus.PROCESSING)

        with patch.object(sources_api, "get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = processing_source

            with pytest.raises(SourceTimeoutError) as exc_info:
                await sources_api.wait_until_ready(
                    "nb_1", "src_1", timeout=0.05, initial_interval=0.02
                )

            assert exc_info.value.source_id == "src_1"
            assert exc_info.value.last_status == SourceStatus.PROCESSING

    @pytest.mark.asyncio
    async def test_exponential_backoff(self, sources_api):
        """Test that polling uses exponential backoff."""
        processing_source = Source(id="src_1", status=SourceStatus.PROCESSING)
        ready_source = Source(id="src_1", status=SourceStatus.READY)

        call_times = []

        async def mock_get(notebook_id, source_id):
            call_times.append(asyncio.get_event_loop().time())
            if len(call_times) < 4:
                return processing_source
            return ready_source

        with patch.object(sources_api, "get", side_effect=mock_get):
            await sources_api.wait_until_ready(
                "nb_1",
                "src_1",
                timeout=10.0,
                initial_interval=0.05,
                backoff_factor=2.0,
                max_interval=1.0,
            )

        # Check that intervals increase (with some tolerance)
        if len(call_times) >= 3:
            interval1 = call_times[2] - call_times[1]
            interval0 = call_times[1] - call_times[0]
            # Second interval should be larger than first (backoff)
            assert interval1 >= interval0 * 1.5


class TestWaitForSources:
    """Tests for wait_for_sources method."""

    @pytest.fixture
    def sources_api(self):
        """Create a SourcesAPI with mocked core."""
        core = MagicMock()
        return SourcesAPI(core)

    @pytest.mark.asyncio
    async def test_waits_for_multiple_sources(self, sources_api):
        """Test wait_for_sources waits for all sources in parallel."""
        ready_sources = [
            Source(id="src_1", status=SourceStatus.READY),
            Source(id="src_2", status=SourceStatus.READY),
        ]

        async def mock_wait(notebook_id, source_id, **kwargs):
            for s in ready_sources:
                if s.id == source_id:
                    return s
            raise SourceNotFoundError(source_id)

        with patch.object(sources_api, "wait_until_ready", side_effect=mock_wait):
            results = await sources_api.wait_for_sources("nb_1", ["src_1", "src_2"], timeout=10.0)

            assert len(results) == 2
            assert all(s.is_ready for s in results)

    @pytest.mark.asyncio
    async def test_raises_on_any_failure(self, sources_api):
        """Test wait_for_sources raises if any source fails."""

        async def mock_wait(notebook_id, source_id, **kwargs):
            if source_id == "src_2":
                raise SourceProcessingError(source_id, status=3)
            return Source(id=source_id, status=SourceStatus.READY)

        with patch.object(sources_api, "wait_until_ready", side_effect=mock_wait):
            with pytest.raises(SourceProcessingError):
                await sources_api.wait_for_sources("nb_1", ["src_1", "src_2"], timeout=10.0)
