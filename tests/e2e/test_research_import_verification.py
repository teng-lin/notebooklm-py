"""E2E test to verify research import correctly adds sources to notebook.

This test verifies the complete flow:
1. Start fast / deep web research
2. Import discovered sources via ``import_sources_with_verification``
3. Wait for sources to process
4. Verify source count matches expected

The ``@pytest.mark.flaky`` decorator below historically covered occasional
``IMPORT_RESEARCH`` 30 s client-side timeouts. As of #315, the import path
is verification-aware and recovers from those automatically, so the
decorator now only guards against unrelated transient failures
(snapshot/probe blips, upstream rate limits).
"""

import asyncio

import pytest

from notebooklm.exceptions import RateLimitError

from .conftest import POLL_INTERVAL, requires_auth


@requires_auth
@pytest.mark.e2e
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
        task_id = start_result.task_id
        assert task_id is not None, "start_result missing task_id"

        # Wait for research to register before polling
        await asyncio.sleep(3)

        # Step 2: Poll until complete
        poll_result = None
        research_timeout = 120.0
        max_attempts = int(research_timeout / POLL_INTERVAL)
        for _ in range(max_attempts):
            poll_result = await client.research.poll(temp_notebook.id)
            status = poll_result.status

            if status == "completed":
                break
            if status == "no_research":
                pytest.skip("Research completed too quickly to poll")

            await asyncio.sleep(POLL_INTERVAL)

        if poll_result is None or poll_result.status != "completed":
            pytest.skip(f"Research did not complete within {research_timeout}s")

        # Step 3: Get sources to import
        sources = poll_result.sources
        if not sources:
            pytest.skip("No sources found by research - cannot test import")

        # Filter to sources with URLs (required for import)
        sources_with_urls = [s for s in sources if s.url]
        if not sources_with_urls:
            pytest.skip("All sources lack URLs - cannot test import")

        # Import first 3 sources
        sources_to_import = sources_with_urls[:3]
        expected_import_count = len(sources_to_import)

        # Step 4: Import sources. Use the verification-aware variant so an
        # occasional 30 s IMPORT_RESEARCH timeout doesn't crash the test —
        # that was the original reason this class was marked
        # ``@pytest.mark.flaky(reruns=2)``. The fix that landed for #315
        # makes this path robust for fast-mode imports too.
        await client.research.import_sources_with_verification(
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
        # The critical assertion: verify ALL requested sources were actually
        # imported. We poll sources.list rather than trust the import return
        # value because the underlying RPC response is sometimes incomplete.
        assert new_source_count == expected_import_count, (
            f"Source count mismatch! "
            f"Requested to import {expected_import_count} but {new_source_count} "
            f"new sources appear in notebook after waiting."
        )

    @pytest.mark.timeout(2400)
    @pytest.mark.asyncio
    async def test_deep_research_import_count_matches(self, client, temp_notebook):
        """Regression for #315: deep research import actually adds sources.

        Deep research differs from fast research on the wire: ``IMPORT_RESEARCH``
        receives one report entry (``_build_report_import_entry``) alongside
        the web-source entries (``_build_web_import_entry``). The NotebookLM
        web UI surfaces an "Add sources?" modal when a deep-research task has
        completed server-side but no client has acknowledged it via
        ``IMPORT_RESEARCH``. This test exercises the full lifecycle with the
        long-poll budget the CLI now exposes via ``--timeout``, and asserts
        the report + web sources actually appear in ``sources.list``.

        Deep research routinely takes 10-20+ minutes server-side, so this is
        nightly-only and may skip when the run window is too short.
        """
        initial_sources = await client.sources.list(temp_notebook.id)
        initial_count = len(initial_sources)

        try:
            start_result = await client.research.start(
                temp_notebook.id,
                query="distributed systems consensus algorithms overview",
                source="web",
                mode="deep",
            )
        except RateLimitError as e:
            pytest.skip(f"START_DEEP_RESEARCH rate limited by NotebookLM: {e}")
        assert start_result is not None, "Failed to start deep research"
        start_task_id = start_result.task_id
        assert start_task_id is not None, "start_result missing task_id"

        await asyncio.sleep(3)

        # Deep research's polled task_id can differ from start()'s task_id
        # (see docs/rpc-reference.md §IMPORT_RESEARCH note about the
        # "later polled deep-research task ID"). Pin on the polled id once we
        # have it so our IMPORT call lands on the right task.
        poll_result = None
        polled_task_id: str | None = None
        research_timeout = 1800.0
        max_attempts = int(research_timeout / POLL_INTERVAL)
        for _ in range(max_attempts):
            poll_result = await client.research.poll(temp_notebook.id, task_id=polled_task_id)
            status = poll_result.status

            if polled_task_id is None:
                polled_task_id = poll_result.task_id

            if status == "completed":
                break
            if status == "no_research":
                pytest.skip("Deep research disappeared from poll — flaky upstream")

            await asyncio.sleep(POLL_INTERVAL)

        if poll_result is None or poll_result.status != "completed":
            pytest.skip(f"Deep research did not complete within {research_timeout}s")

        sources = poll_result.sources
        if not sources:
            pytest.skip("Deep research returned no sources — cannot test import")

        # Count importable entries: report rows (is_report with markdown) +
        # web rows with non-empty URLs. ``import_sources`` skips everything
        # else, so this is the upper bound we expect to land in sources.list.
        report_entries = [src for src in sources if src.is_report and src.report_markdown]
        web_entries = [src for src in sources if src.url]
        expected_import_count = len(report_entries) + len(web_entries)
        if expected_import_count == 0:
            pytest.skip("Deep research returned sources but none are importable")

        # Use the library's verification-aware import (#315). The raw
        # ``client.research.import_sources`` call frequently times out at 30 s
        # on deep research, and the executor refuses to retry under
        # IDEMPOTENCY_REGISTRY's NON_IDEMPOTENT_NO_RETRY policy (#808).
        # ``import_sources_with_verification`` snapshots baseline source IDs
        # before the call and, on timeout, probes ``sources.list`` to confirm
        # the requested URLs appear among *new* sources — disambiguating
        # commit-lost-response from genuine failure without duplicating.
        imported = await client.research.import_sources_with_verification(
            temp_notebook.id,
            polled_task_id or start_task_id,
            sources,
            max_elapsed=1800,
        )

        # Allow the backend to process imports and surface them in the
        # notebook's source list. Deep-research imports include a report
        # entry which may take a beat longer than a plain URL.
        import_timeout = 60.0
        import_max_attempts = int(import_timeout / POLL_INTERVAL)
        new_source_count = -1
        for _ in range(import_max_attempts):
            final_sources = await client.sources.list(temp_notebook.id)
            new_source_count = len(final_sources) - initial_count
            if new_source_count >= expected_import_count:
                break
            await asyncio.sleep(POLL_INTERVAL)

        # Deep research can return 30-50+ sources, and NotebookLM silently
        # de-dupes or rejects a subset (paywalled, malformed, duplicates of
        # existing notebook content). Asserting exact equality with the
        # request count is too brittle for deep research — what #315 is
        # actually about is whether *some* of the requested sources land
        # without the user having to click the "Add sources?" modal. So we
        # check (a) the import returned a non-empty result and (b) most of
        # the requested sources actually appear in the notebook (>=50%).
        # If the bug regresses, ``imported`` would be empty (raw timeout
        # propagated) and/or ``new_source_count`` would be 0 (modal would
        # be hanging open server-side).
        assert imported, (
            f"import_sources_with_verification returned no entries for "
            f"{expected_import_count} requested sources — IMPORT_RESEARCH "
            f"never committed and the NotebookLM web UI will keep an "
            f"'Add sources?' modal open in this state."
        )
        assert new_source_count > 0, (
            f"No new sources appeared in notebook after import "
            f"({expected_import_count} requested) — the modal is hanging."
        )
        # Soft check: at least half of requested sources should land. If
        # this is consistently low, that's a separate upstream-rejection
        # issue, not #315.
        assert new_source_count >= expected_import_count // 2, (
            f"Deep research committed only {new_source_count} of "
            f"{expected_import_count} requested sources — investigate "
            f"whether IMPORT_RESEARCH is silently rejecting or whether "
            f"retry-budget is exhausting before all batches commit."
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
