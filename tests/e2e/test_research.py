"""E2E tests for Research API.

Tests the research functionality including starting web/drive research,
polling for results, and importing discovered sources.
"""

import asyncio
import pytest
from .conftest import requires_auth, POLL_INTERVAL, POLL_TIMEOUT


@requires_auth
@pytest.mark.e2e
class TestResearchStart:
    """Test starting research sessions."""

    @pytest.mark.asyncio
    @pytest.mark.slow
    @pytest.mark.stable
    async def test_start_fast_web_research(self, client, temp_notebook):
        """Test starting fast web research."""
        result = await client.research.start(
            temp_notebook.id,
            query="artificial intelligence basics",
            source="web",
            mode="fast",
        )

        assert result is not None, "Research start should return a result"
        assert "task_id" in result, "Result should contain task_id"
        assert result["task_id"] is not None, "task_id should not be None"
        assert result["notebook_id"] == temp_notebook.id
        assert result["query"] == "artificial intelligence basics"
        assert result["mode"] == "fast"

    @pytest.mark.asyncio
    @pytest.mark.slow
    @pytest.mark.unstable  # Deep research may take longer/rate limited
    async def test_start_deep_web_research(self, client, temp_notebook):
        """Test starting deep web research."""
        result = await client.research.start(
            temp_notebook.id,
            query="machine learning algorithms",
            source="web",
            mode="deep",
        )

        assert result is not None, "Deep research start should return a result"
        assert "task_id" in result, "Result should contain task_id"
        assert result["task_id"] is not None, "task_id should not be None"
        assert result["mode"] == "deep"

    @pytest.mark.asyncio
    async def test_start_research_invalid_source(self, client, temp_notebook):
        """Test that invalid source raises ValueError."""
        with pytest.raises(ValueError, match="Invalid source"):
            await client.research.start(
                temp_notebook.id,
                query="test query",
                source="invalid",
            )

    @pytest.mark.asyncio
    async def test_start_research_invalid_mode(self, client, temp_notebook):
        """Test that invalid mode raises ValueError."""
        with pytest.raises(ValueError, match="Invalid mode"):
            await client.research.start(
                temp_notebook.id,
                query="test query",
                mode="invalid",
            )

    @pytest.mark.asyncio
    async def test_start_deep_drive_research_invalid(self, client, temp_notebook):
        """Test that deep research with drive source raises ValueError."""
        with pytest.raises(ValueError, match="Deep Research only supports Web"):
            await client.research.start(
                temp_notebook.id,
                query="test query",
                source="drive",
                mode="deep",
            )


@requires_auth
@pytest.mark.e2e
class TestResearchPoll:
    """Test polling for research results."""

    @pytest.mark.asyncio
    @pytest.mark.stable
    async def test_poll_no_research(self, client, temp_notebook):
        """Test polling when no research has been started."""
        result = await client.research.poll(temp_notebook.id)

        assert result is not None
        status = result.get("status")
        assert status == "no_research", f"Expected 'no_research', got {status}"

    @pytest.mark.asyncio
    @pytest.mark.slow
    @pytest.mark.stable
    async def test_poll_after_start(self, client, temp_notebook):
        """Test polling after starting research."""
        # Start research
        start_result = await client.research.start(
            temp_notebook.id,
            query="python programming",
            source="web",
            mode="fast",
        )
        assert start_result is not None

        # Wait a bit for research to start processing
        await asyncio.sleep(POLL_INTERVAL)

        # Poll for results
        poll_result = await client.research.poll(temp_notebook.id)

        assert poll_result is not None
        status = poll_result.get("status")
        assert status is not None, f"Invalid poll response: {poll_result}"
        # Should be either in_progress or completed
        assert status in ("in_progress", "completed", "no_research")

        if status != "no_research":
            assert "task_id" in poll_result
            assert "query" in poll_result

    @pytest.mark.asyncio
    @pytest.mark.slow
    @pytest.mark.unstable  # Research behavior can be unpredictable
    async def test_poll_until_complete(self, client, temp_notebook, rate_limit_aware):
        """Test polling until research completes."""
        # Start research
        start_result = await client.research.start(
            temp_notebook.id,
            query="data science introduction",
            source="web",
            mode="fast",
        )
        assert start_result is not None

        # Poll until complete or timeout
        max_attempts = int(POLL_TIMEOUT / POLL_INTERVAL)
        for _ in range(max_attempts):
            poll_result = await client.research.poll(temp_notebook.id)
            status = poll_result.get("status")

            if status is None:
                pytest.fail(f"Invalid poll response (missing status): {poll_result}")

            if status == "completed":
                # Verify completed result structure
                assert "sources" in poll_result
                assert isinstance(poll_result.get("sources"), list)
                # Research may or may not find sources
                sources = poll_result.get("sources", [])
                if sources:
                    assert "url" in sources[0] or "title" in sources[0]
                return  # Test passed

            if status == "no_research":
                # Research may complete and disappear quickly - this is acceptable
                pytest.skip("Research completed too quickly to poll")

            await asyncio.sleep(POLL_INTERVAL)

        # If we reach here, research didn't complete in time
        pytest.skip(f"Research did not complete within {POLL_TIMEOUT}s timeout")


@requires_auth
@pytest.mark.e2e
class TestResearchImport:
    """Test importing research sources."""

    @pytest.mark.asyncio
    @pytest.mark.slow
    @pytest.mark.stable
    async def test_import_empty_sources(self, client, temp_notebook):
        """Test importing empty sources list returns empty list."""
        result = await client.research.import_sources(
            temp_notebook.id,
            task_id="fake_task_id",
            sources=[],
        )

        assert result == []

    @pytest.mark.asyncio
    @pytest.mark.slow
    @pytest.mark.unstable  # Research behavior can be unpredictable
    async def test_full_research_workflow(self, client, temp_notebook, rate_limit_aware):
        """Test complete research workflow: start -> poll -> import.

        This is a critical path test for the Research API, but marked unstable
        because research behavior can vary (fast completion, rate limiting, etc).
        """
        # Step 1: Start research
        start_result = await client.research.start(
            temp_notebook.id,
            query="software engineering best practices",
            source="web",
            mode="fast",
        )
        assert start_result is not None, "Failed to start research"
        task_id = start_result.get("task_id")
        assert task_id is not None, "start_result missing task_id"

        # Step 2: Poll until complete
        poll_result = None
        max_attempts = int(POLL_TIMEOUT / POLL_INTERVAL)
        for _ in range(max_attempts):
            poll_result = await client.research.poll(temp_notebook.id)
            status = poll_result.get("status")

            if status is None:
                pytest.fail(f"Invalid poll response: {poll_result}")

            if status == "completed":
                break

            if status == "no_research":
                # Research may complete and disappear quickly
                pytest.skip("Research completed too quickly to poll")

            await asyncio.sleep(POLL_INTERVAL)

        if poll_result is None or poll_result.get("status") != "completed":
            pytest.skip(f"Research did not complete within {POLL_TIMEOUT}s")

        # Step 3: Import sources (if any found)
        sources = poll_result.get("sources", [])
        if not sources:
            pytest.skip("No sources found by research - cannot test import")

        # Import first 2 sources
        sources_to_import = sources[:2]
        imported = await client.research.import_sources(
            temp_notebook.id,
            task_id,
            sources_to_import,
        )

        # Verify import
        assert isinstance(imported, list)
        # Import may succeed or fail depending on source validity
        # If sources were imported, they should have id and title
        for src in imported:
            if src:  # May be empty dict if import failed
                assert "id" in src or "title" in src


@requires_auth
@pytest.mark.e2e
class TestResearchDriveSource:
    """Test research with Google Drive sources."""

    @pytest.mark.asyncio
    @pytest.mark.slow
    @pytest.mark.unstable  # Requires Drive access
    async def test_start_drive_research(self, client, temp_notebook):
        """Test starting Drive research (fast mode only)."""
        result = await client.research.start(
            temp_notebook.id,
            query="documents about testing",
            source="drive",
            mode="fast",
        )

        # May return None if no Drive access or no matching documents
        if result is None:
            pytest.skip("Drive research returned no results - may need Drive access")

        assert "task_id" in result
        assert result["mode"] == "fast"
