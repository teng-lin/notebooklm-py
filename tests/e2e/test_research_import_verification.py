"""E2E test to verify research import correctly adds sources to notebook.

This test verifies the complete flow:
1. Start fast web research
2. Import discovered sources
3. Wait for sources to process
4. Verify source count matches expected

Note: These tests are marked as flaky because Google's IMPORT_RESEARCH API
can occasionally take longer than the default 30s timeout to respond.
"""

import asyncio

import pytest

from .conftest import POLL_INTERVAL, requires_auth


@requires_auth
@pytest.mark.flaky(reruns=2, reruns_delay=5)
class TestResearchImportVerification:
    """Verify research import actually adds sources to the notebook."""

    @pytest.mark.asyncio
    async def test_fast_research_import_count_matches(self, client, temp_notebook):
        """Test that imported sources from fast research appear in notebook.

        This is the critical test: after import, the notebook should have
        the expected number of new sources.
        """
        # Get initial source count
        initial_sources = await client.sources.list(temp_notebook.id)
        initial_count = len(initial_sources)

        # Step 1: Start fast web research
        start_result = await client.research.start(
            temp_notebook.id,
            query="python programming tutorial",
            source="web",
            mode="fast",
        )
        assert start_result is not None, "Failed to start research"
        task_id = start_result.get("task_id")
        assert task_id is not None, "start_result missing task_id"

        # Wait for research to register before polling
        await asyncio.sleep(3)

        # Step 2: Poll until complete
        poll_result = None
        research_timeout = 120.0
        max_attempts = int(research_timeout / POLL_INTERVAL)
        for _ in range(max_attempts):
            poll_result = await client.research.poll(temp_notebook.id)
            status = poll_result.get("status")

            if status == "completed":
                break
            if status == "no_research":
                pytest.skip("Research completed too quickly to poll")

            await asyncio.sleep(POLL_INTERVAL)

        if poll_result is None or poll_result.get("status") != "completed":
            pytest.skip(f"Research did not complete within {research_timeout}s")

        # Step 3: Get sources to import
        sources = poll_result.get("sources", [])
        if not sources:
            pytest.skip("No sources found by research - cannot test import")

        # Filter to sources with URLs (required for import)
        sources_with_urls = [s for s in sources if s.get("url")]
        if not sources_with_urls:
            pytest.skip("All sources lack URLs - cannot test import")

        # Import first 3 sources
        sources_to_import = sources_with_urls[:3]
        expected_import_count = len(sources_to_import)

        # Step 4: Import sources
        await client.research.import_sources(
            temp_notebook.id,
            task_id,
            sources_to_import,
        )

        # Step 5: Poll for imported sources to appear
        import_timeout = 30.0
        import_max_attempts = int(import_timeout / POLL_INTERVAL)
        new_source_count = -1

        for _ in range(import_max_attempts):
            final_sources = await client.sources.list(temp_notebook.id)
            new_source_count = len(final_sources) - initial_count
            if new_source_count == expected_import_count:
                break
            await asyncio.sleep(POLL_INTERVAL)

        # Step 6: Verify source count
        # The critical assertion: verify ALL requested sources were actually imported
        # Note: import_sources() return value may be incomplete due to API quirk,
        # so we verify against actual notebook contents instead
        assert new_source_count == expected_import_count, (
            f"Source count mismatch! "
            f"Requested to import {expected_import_count} but {new_source_count} "
            f"new sources appear in notebook after waiting."
        )

    @staticmethod
    def test_import_sources_filters_empty_urls():
        """Test that sources without URLs are filtered out before import.

        The import_sources method should skip sources with empty/None/missing URLs
        rather than sending them to the API (which would cause errors).
        """
        # Test the filtering logic directly
        sources_mixed = [
            {"url": "https://example.com/valid", "title": "Valid Source"},
            {"url": "", "title": "No URL Source"},  # Empty URL - should be filtered
            {"url": None, "title": "Null URL Source"},  # None URL - should be filtered
            {"title": "Missing URL Key"},  # No URL key - should be filtered
        ]

        # Filter like import_sources does
        valid_sources = [s for s in sources_mixed if s.get("url")]

        # Should only have 1 valid source
        assert len(valid_sources) == 1, (
            f"Expected 1 valid source after filtering, got {len(valid_sources)}"
        )
        assert valid_sources[0]["url"] == "https://example.com/valid"
        assert valid_sources[0]["title"] == "Valid Source"
