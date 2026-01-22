import asyncio

import pytest

from notebooklm import Source, SourceStatus

from .conftest import requires_auth


@requires_auth
class TestSourceOperations:
    """Tests for source creation operations.

    Note: Source creation requires an OWNED notebook. The test notebook
    is read-only - use temp_notebook fixture instead.
    """

    @pytest.mark.asyncio
    async def test_add_text_source(self, client, temp_notebook):
        """Test adding a text source to an owned notebook."""
        source = await client.sources.add_text(
            temp_notebook.id,
            "E2E Test Text Source",
            "This is test content for E2E testing. It contains enough text for NotebookLM to process.",
        )
        assert isinstance(source, Source)
        assert source.id is not None
        assert source.title == "E2E Test Text Source"

    @pytest.mark.asyncio
    async def test_add_url_source(self, client, temp_notebook):
        """Test adding a URL source to an owned notebook."""
        source = await client.sources.add_url(temp_notebook.id, "https://httpbin.org/html")
        assert isinstance(source, Source)
        assert source.id is not None
        # URL may or may not be returned in response
        # assert source.url == "https://httpbin.org/html"

    @pytest.mark.asyncio
    async def test_add_youtube_source(self, client, temp_notebook):
        """Test adding a YouTube source to an owned notebook."""
        source = await client.sources.add_url(
            temp_notebook.id, "https://www.youtube.com/watch?v=jNQXAC9IVRw"
        )
        assert isinstance(source, Source)
        assert source.id is not None
        # Title is returned for YouTube videos
        assert source.title is not None

    @pytest.mark.asyncio
    async def test_rename_source(self, client, temp_notebook):
        """Test renaming a source in an owned notebook."""
        # List sources (temp_notebook has one source from fixture)
        sources = await client.sources.list(temp_notebook.id)
        assert isinstance(sources, list)
        assert len(sources) > 0, "temp_notebook should have at least one source"

        # Get first source
        source = sources[0]
        assert isinstance(source, Source)

        # Rename
        renamed = await client.sources.rename(temp_notebook.id, source.id, "Renamed Test Source")
        assert isinstance(renamed, Source)
        assert renamed.title == "Renamed Test Source"
        # No need to restore - temp_notebook is deleted after test


@requires_auth
class TestSourceRetrieval:
    @pytest.mark.asyncio
    async def test_list_sources(self, client, read_only_notebook_id):
        sources = await client.sources.list(read_only_notebook_id)
        assert isinstance(sources, list)
        assert all(isinstance(src, Source) for src in sources)

    @pytest.mark.asyncio
    async def test_get_source(self, client, read_only_notebook_id):
        """Test getting a specific source by ID."""
        sources = await client.sources.list(read_only_notebook_id)
        if not sources:
            pytest.skip("No sources available to get")

        source = await client.sources.get(read_only_notebook_id, sources[0].id)
        assert source is not None
        assert isinstance(source, Source)
        assert source.id == sources[0].id

    @pytest.mark.asyncio
    async def test_get_source_not_found(self, client, read_only_notebook_id):
        """Test getting a non-existent source returns None."""
        source = await client.sources.get(read_only_notebook_id, "nonexistent_source_id")
        assert source is None

    @pytest.mark.asyncio
    async def test_get_guide(self, client, read_only_notebook_id):
        """Test getting source guide/summary."""
        sources = await client.sources.list(read_only_notebook_id)
        if not sources:
            pytest.skip("No sources available for guide")

        guide = await client.sources.get_guide(read_only_notebook_id, sources[0].id)
        # get_guide returns dict with summary and keywords
        assert isinstance(guide, dict)
        assert "summary" in guide
        assert "keywords" in guide
        # Verify values are actually populated (not empty due to parsing bugs)
        assert guide["summary"], "Expected non-empty summary from source guide"
        assert isinstance(guide["keywords"], list)
        assert len(guide["keywords"]) > 0, "Expected non-empty keywords from source guide"


@requires_auth
class TestSourceMutations:
    """Tests that create/delete sources - use temp_notebook to avoid affecting test notebook."""

    @pytest.mark.asyncio
    async def test_delete_source(self, client, temp_notebook):
        """Test deleting a source."""
        # Create a source to delete
        source = await client.sources.add_text(
            temp_notebook.id,
            "Source to Delete",
            "This source will be deleted as part of the E2E test.",
        )
        assert source.id is not None

        # Delete it
        deleted = await client.sources.delete(temp_notebook.id, source.id)
        assert deleted is True

        # Verify it's gone
        sources = await client.sources.list(temp_notebook.id)
        source_ids = [s.id for s in sources]
        assert source.id not in source_ids

    @pytest.mark.asyncio
    async def test_refresh_source(self, client, temp_notebook):
        """Test refreshing a URL source."""
        # Add a URL source
        source = await client.sources.add_url(temp_notebook.id, "https://httpbin.org/html")
        assert source.id is not None

        # Refresh it
        await asyncio.sleep(2)  # Wait for initial processing

        result = await client.sources.refresh(temp_notebook.id, source.id)
        # refresh() always returns True if successful
        assert result is True

    @pytest.mark.asyncio
    async def test_check_freshness(self, client, temp_notebook):
        """Test checking source freshness."""
        # Add a URL source
        source = await client.sources.add_url(temp_notebook.id, "https://httpbin.org/html")
        assert source.id is not None

        await asyncio.sleep(2)  # Wait for processing

        # Check freshness
        freshness = await client.sources.check_freshness(temp_notebook.id, source.id)
        # check_freshness() returns bool: True if fresh, False if stale
        assert isinstance(freshness, bool)


@requires_auth
class TestSourceStatus:
    """Tests for source status and readiness polling."""

    @pytest.mark.asyncio
    async def test_source_has_status_field(self, client, read_only_notebook_id):
        """Test that sources have a status field."""
        sources = await client.sources.list(read_only_notebook_id)
        if not sources:
            pytest.skip("No sources available to check status")

        source = sources[0]
        assert hasattr(source, "status")
        assert source.status in (
            SourceStatus.PROCESSING,
            SourceStatus.READY,
            SourceStatus.ERROR,
        )

    @pytest.mark.asyncio
    async def test_source_is_ready_property(self, client, read_only_notebook_id):
        """Test that is_ready property works correctly."""
        sources = await client.sources.list(read_only_notebook_id)
        if not sources:
            pytest.skip("No sources available to check")

        # At least one source in an existing notebook should be ready
        ready_sources = [s for s in sources if s.is_ready]
        assert len(ready_sources) > 0, "Expected at least one ready source in test notebook"

    @pytest.mark.asyncio
    async def test_add_text_with_wait(self, client, temp_notebook):
        """Test adding a text source with wait=True."""
        source = await client.sources.add_text(
            temp_notebook.id,
            "Wait Test Source",
            "Content for testing wait functionality. " * 10,
            wait=True,
            wait_timeout=60.0,
        )
        assert isinstance(source, Source)
        assert source.is_ready, "Source should be ready after wait=True"

    @pytest.mark.asyncio
    async def test_wait_until_ready(self, client, temp_notebook):
        """Test wait_until_ready() method."""
        # Add source without waiting
        source = await client.sources.add_text(
            temp_notebook.id,
            "Polling Test Source",
            "Content for testing polling functionality. " * 10,
        )
        assert source.id is not None

        # Wait for it to be ready
        ready_source = await client.sources.wait_until_ready(
            temp_notebook.id,
            source.id,
            timeout=60.0,
        )
        assert ready_source.is_ready

    @pytest.mark.asyncio
    async def test_wait_for_multiple_sources(self, client, temp_notebook):
        """Test wait_for_sources() for batch operations."""
        # Add multiple sources without waiting
        source1 = await client.sources.add_text(
            temp_notebook.id,
            "Batch Test 1",
            "First batch test content. " * 10,
        )
        source2 = await client.sources.add_text(
            temp_notebook.id,
            "Batch Test 2",
            "Second batch test content. " * 10,
        )

        # Wait for all to be ready
        ready_sources = await client.sources.wait_for_sources(
            temp_notebook.id,
            [source1.id, source2.id],
            timeout=60.0,
        )

        assert len(ready_sources) == 2
        assert all(s.is_ready for s in ready_sources)
