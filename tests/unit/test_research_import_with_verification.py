"""Tests for ``ResearchAPI.import_sources_with_verification``.

The retry-with-verification logic for ``IMPORT_RESEARCH`` timeouts lives
on ``ResearchAPI`` as of issue #315. These tests were originally in
``tests/unit/cli/test_helpers.py::TestImportWithRetry`` (the logic used to
live in ``cli/research_import.py``); they were moved here when the policy
became a library-layer concern so Python API users get the same fix the
CLI does.

The CLI wrapper ``cli.research_import.import_with_retry`` is now a thin
delegate — its tests cover only the wiring (still in ``test_helpers.py``).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import notebooklm._research as _research_mod
from notebooklm._research import ResearchAPI
from notebooklm.exceptions import NetworkError, RPCError, RPCTimeoutError


def _make_research() -> tuple[ResearchAPI, MagicMock, MagicMock]:
    """Build a ``ResearchAPI`` with a mocked source-lister seam.

    Returns ``(research, mock_rpc, mock_source_lister)``. Override
    ``research.import_sources`` / ``mock_source_lister.list`` per test.

    ResearchAPI now mirrors ``NotebooksAPI``'s default-builder pattern, so
    injecting a mock lister bypasses the cross-API dependency entirely —
    the test does not need a SourcesAPI handle.
    """
    mock_rpc = MagicMock()
    mock_source_lister = MagicMock()
    research = ResearchAPI(mock_rpc, source_lister=mock_source_lister)
    return research, mock_rpc, mock_source_lister


class TestImportSourcesWithVerification:
    @pytest.mark.asyncio
    async def test_empty_sources_returns_empty_without_calling_rpc(self):
        research, _, mock_source_lister = _make_research()
        mock_source_lister.list = AsyncMock()
        research.import_sources = AsyncMock()

        imported = await research.import_sources_with_verification("nb_123", "task_123", [])

        assert imported == []
        research.import_sources.assert_not_awaited()
        mock_source_lister.list.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_retries_rpc_timeout_then_succeeds(self):
        # Empty baseline + empty post-timeout probe → verification fails →
        # falls through to legacy retry. This exercises the retry path
        # explicitly rather than relying on a snapshot exception.
        research, _, mock_source_lister = _make_research()
        mock_source_lister.list = AsyncMock(return_value=[])
        research.import_sources = AsyncMock(
            side_effect=[
                RPCTimeoutError("Timed out", timeout_seconds=30.0),
                [{"id": "src_1", "title": "Source 1"}],
            ]
        )

        with patch.object(_research_mod.asyncio, "sleep", new_callable=AsyncMock) as mock_sleep:
            imported = await research.import_sources_with_verification(
                "nb_123",
                "task_123",
                [{"url": "https://example.com", "title": "Source 1"}],
                initial_delay=5,
                max_delay=60,
            )

        assert imported == [{"id": "src_1", "title": "Source 1"}]
        assert research.import_sources.await_count == 2
        mock_sleep.assert_awaited_once_with(5)

    @pytest.mark.asyncio
    async def test_raises_after_elapsed_budget(self):
        research, _, mock_source_lister = _make_research()
        mock_source_lister.list = AsyncMock(return_value=[])
        research.import_sources = AsyncMock(
            side_effect=RPCTimeoutError("Timed out", timeout_seconds=30.0)
        )

        # time.monotonic is read once at start, then on each timeout. Two values
        # cover the snapshot path plus the timeout-handling path (elapsed
        # check). Past-budget on the second read forces the raise.
        with (
            patch.object(
                _research_mod.time,
                "monotonic",
                side_effect=[0.0, 1801.0],
            ),
            patch.object(_research_mod.asyncio, "sleep", new_callable=AsyncMock) as mock_sleep,
            pytest.raises(RPCTimeoutError),
        ):
            await research.import_sources_with_verification(
                "nb_123",
                "task_123",
                [{"url": "https://example.com", "title": "Source 1"}],
                max_elapsed=1800,
            )

        mock_sleep.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_does_not_retry_non_timeout_error(self):
        research, _, mock_source_lister = _make_research()
        mock_source_lister.list = AsyncMock(return_value=[])
        research.import_sources = AsyncMock(side_effect=ValueError("boom"))

        with (
            patch.object(_research_mod.asyncio, "sleep", new_callable=AsyncMock) as mock_sleep,
            pytest.raises(ValueError, match="boom"),
        ):
            await research.import_sources_with_verification(
                "nb_123",
                "task_123",
                [{"url": "https://example.com", "title": "Source 1"}],
            )

        assert research.import_sources.await_count == 1
        mock_sleep.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_retry_when_server_state_shows_import_succeeded(self):
        """If the import RPC times out but sources.list shows our URLs were
        added server-side, treat it as success and skip retry. This avoids
        the duplicate-on-retry inflation that otherwise multiplies sources
        by the retry count.
        """
        baseline_src = MagicMock(id="src_pre", title="Pre-existing", url="https://pre.example.com")
        new_src = MagicMock(id="src_new", title="Source 1", url="https://example.com")
        research, _, mock_source_lister = _make_research()
        mock_source_lister.list = AsyncMock(
            side_effect=[
                [baseline_src],  # snapshot before import
                [baseline_src, new_src],  # probe after timeout — URL is now there
            ]
        )
        research.import_sources = AsyncMock(
            side_effect=RPCTimeoutError("Timed out", timeout_seconds=30.0)
        )

        with patch.object(_research_mod.asyncio, "sleep", new_callable=AsyncMock) as mock_sleep:
            imported = await research.import_sources_with_verification(
                "nb_123",
                "task_123",
                [{"url": "https://example.com", "title": "Source 1"}],
            )

        assert imported == [{"id": "src_new", "title": "Source 1"}]
        # Single import attempt — no retry.
        assert research.import_sources.await_count == 1
        # Snapshot + post-timeout probe — exactly two sources.list calls.
        assert mock_source_lister.list.await_count == 2
        # No sleep, no retry — straight to verified-success exit.
        mock_sleep.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_retry_when_url_normalization_matches(self):
        """Server-side URL normalization (case folding, trailing-slash strip)
        is handled by normalizing both sides before the subset check, so a
        cosmetic difference between request and stored URL doesn't force a
        duplicating retry.
        """
        new_src = MagicMock(id="src_new", title="Source 1", url="https://example.com")
        research, _, mock_source_lister = _make_research()
        mock_source_lister.list = AsyncMock(side_effect=[[], [new_src]])
        research.import_sources = AsyncMock(
            side_effect=RPCTimeoutError("Timed out", timeout_seconds=30.0)
        )

        with patch.object(_research_mod.asyncio, "sleep", new_callable=AsyncMock) as mock_sleep:
            imported = await research.import_sources_with_verification(
                "nb_123",
                "task_123",
                # Trailing slash + uppercase host differ from server-normalized form.
                [{"url": "https://Example.com/", "title": "Source 1"}],
            )

        assert imported == [{"id": "src_new", "title": "Source 1"}]
        assert research.import_sources.await_count == 1
        mock_sleep.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_retry_when_only_url_fragment_differs(self):
        new_src = MagicMock(id="src_new", title="Source 1", url="https://example.com/a")
        research, _, mock_source_lister = _make_research()
        mock_source_lister.list = AsyncMock(side_effect=[[], [new_src]])
        research.import_sources = AsyncMock(
            side_effect=RPCTimeoutError("Timed out", timeout_seconds=30.0)
        )

        with patch.object(_research_mod.asyncio, "sleep", new_callable=AsyncMock) as mock_sleep:
            imported = await research.import_sources_with_verification(
                "nb_123",
                "task_123",
                [{"url": "https://example.com/a#top", "title": "Source 1"}],
            )

        assert imported == [{"id": "src_new", "title": "Source 1"}]
        assert research.import_sources.await_count == 1
        mock_sleep.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_retries_when_server_state_shows_no_progress(self):
        """If sources.list shows the requested URLs were NOT imported, fall
        back to the original retry behavior.
        """
        research, _, mock_source_lister = _make_research()
        mock_source_lister.list = AsyncMock(return_value=[])  # always empty
        research.import_sources = AsyncMock(
            side_effect=[
                RPCTimeoutError("Timed out", timeout_seconds=30.0),
                [{"id": "src_1", "title": "Source 1"}],
            ]
        )

        with patch.object(_research_mod.asyncio, "sleep", new_callable=AsyncMock) as mock_sleep:
            imported = await research.import_sources_with_verification(
                "nb_123",
                "task_123",
                [{"url": "https://example.com", "title": "Source 1"}],
                initial_delay=5,
            )

        assert imported == [{"id": "src_1", "title": "Source 1"}]
        assert research.import_sources.await_count == 2
        mock_sleep.assert_awaited_once_with(5)

    @pytest.mark.asyncio
    async def test_partial_timeout_retries_only_missing_urls(self):
        """If a timed-out import partially committed URLs, the retry payload
        must drop already-visible URLs to avoid duplicating them.
        """
        imported_src = MagicMock(id="src_1", title="Source 1", url="https://one.example.com")
        sources = [
            {"url": "https://one.example.com", "title": "Source 1"},
            {"url": "https://two.example.com", "title": "Source 2"},
            {"url": "https://three.example.com", "title": "Source 3"},
        ]
        research, _, mock_source_lister = _make_research()
        mock_source_lister.list = AsyncMock(
            side_effect=[
                [],  # baseline
                [imported_src],  # post-timeout probe — 1 of 3 is visible
            ]
        )
        research.import_sources = AsyncMock(
            side_effect=[
                RPCTimeoutError("Timed out", timeout_seconds=30.0),
                [{"id": "src_2", "title": "Source 2"}, {"id": "src_3", "title": "Source 3"}],
            ]
        )

        with patch.object(_research_mod.asyncio, "sleep", new_callable=AsyncMock) as mock_sleep:
            imported = await research.import_sources_with_verification(
                "nb_123",
                "task_123",
                sources,
                initial_delay=5,
            )

        assert imported == [
            {"id": "src_1", "title": "Source 1"},
            {"id": "src_2", "title": "Source 2"},
            {"id": "src_3", "title": "Source 3"},
        ]
        assert research.import_sources.await_count == 2
        first_call_sources = research.import_sources.await_args_list[0].args[2]
        retry_call_sources = research.import_sources.await_args_list[1].args[2]
        assert first_call_sources == sources
        assert retry_call_sources == [
            {"url": "https://two.example.com", "title": "Source 2"},
            {"url": "https://three.example.com", "title": "Source 3"},
        ]
        mock_sleep.assert_awaited_once_with(5)

    @pytest.mark.asyncio
    async def test_partial_timeout_drops_report_entries_when_any_url_committed(self):
        """When the partial-success probe shows at least one requested URL
        already in the notebook, no-URL entries (deep-research reports) MUST
        be dropped from the retry batch.

        Reports are appended first in the IMPORT_RESEARCH payload (see
        ``_build_report_import_entry`` usage in ``ResearchAPI.import_sources``),
        so a verified URL implies the report committed too. Retrying the
        report on each subsequent timeout would create duplicate report
        sources server-side (gemini-code-assist review on PR #882).
        """
        imported_src = MagicMock(id="src_1", title="Source 1", url="https://one.example.com")
        report_entry = {
            "title": "Research Report",
            "report_markdown": "# Findings\n...",
            "result_type": 5,
        }
        sources = [
            {"url": "https://one.example.com", "title": "Source 1"},
            {"url": "https://two.example.com", "title": "Source 2"},
            report_entry,
        ]
        research, _, mock_source_lister = _make_research()
        mock_source_lister.list = AsyncMock(
            side_effect=[
                [],  # baseline
                [imported_src],  # post-timeout probe — URL 1 is visible
            ]
        )
        research.import_sources = AsyncMock(
            side_effect=[
                RPCTimeoutError("Timed out", timeout_seconds=30.0),
                [{"id": "src_2", "title": "Source 2"}],
            ]
        )

        with patch.object(_research_mod.asyncio, "sleep", new_callable=AsyncMock):
            imported = await research.import_sources_with_verification(
                "nb_123",
                "task_123",
                sources,
                initial_delay=5,
            )

        # The retry batch must NOT include the report entry.
        retry_call_sources = research.import_sources.await_args_list[1].args[2]
        assert retry_call_sources == [
            {"url": "https://two.example.com", "title": "Source 2"},
        ], "Report entry should be dropped from retry batch once any URL is verified committed"

        # Returned set: URL 1 (verified during partial probe) + URL 2 (from
        # the retry's successful response). The report is not in the
        # return list because the function has no reliable way to attribute
        # a no-URL source to this call vs. concurrent activity once the
        # report was already committed under the timed-out RPC.
        assert imported == [
            {"id": "src_1", "title": "Source 1"},
            {"id": "src_2", "title": "Source 2"},
        ]

    @pytest.mark.asyncio
    async def test_partial_timeout_keeps_report_entry_when_no_url_committed(self):
        """When the partial-success probe shows NO requested URLs in the
        notebook, no-URL report entries stay in the retry batch — their
        fate is unknown and dropping them would lose the report.

        The report-only retry path is then bounded by the no-URL attempt
        cap below (``test_report_only_import_bounded_retries_on_persistent_timeout``).
        """
        report_entry = {
            "title": "Research Report",
            "report_markdown": "# Findings\n...",
            "result_type": 5,
        }
        sources = [
            {"url": "https://one.example.com", "title": "Source 1"},
            report_entry,
        ]
        research, _, mock_source_lister = _make_research()
        mock_source_lister.list = AsyncMock(
            side_effect=[
                [],  # baseline
                [],  # post-timeout probe — nothing committed yet
            ]
        )
        research.import_sources = AsyncMock(
            side_effect=[
                RPCTimeoutError("Timed out", timeout_seconds=30.0),
                [{"id": "src_1", "title": "Source 1"}, {"id": "src_report", "title": "Report"}],
            ]
        )

        with patch.object(_research_mod.asyncio, "sleep", new_callable=AsyncMock):
            await research.import_sources_with_verification(
                "nb_123",
                "task_123",
                sources,
                initial_delay=5,
            )

        # No URL was verified committed → keep the report in the retry.
        retry_call_sources = research.import_sources.await_args_list[1].args[2]
        assert retry_call_sources == sources, (
            "Report must remain in retry batch when nothing was verified committed"
        )

    @pytest.mark.asyncio
    async def test_partial_timeout_merges_prior_verified_sources_on_later_verified_success(self):
        """When multiple timeouts happen, later verified-success returns must
        include sources verified during earlier partial probes.
        """
        source_1 = MagicMock(id="src_1", title="Source 1", url="https://one.example.com")
        source_2 = MagicMock(id="src_2", title="Source 2", url="https://two.example.com")
        sources = [
            {"url": "https://one.example.com", "title": "Source 1"},
            {"url": "https://two.example.com", "title": "Source 2"},
        ]
        research, _, mock_source_lister = _make_research()
        mock_source_lister.list = AsyncMock(
            side_effect=[
                [],  # baseline
                [source_1],  # first timeout — only URL 1 is visible, so retry URL 2
                [source_1, source_2],  # second timeout — URL 2 is now visible
            ]
        )
        research.import_sources = AsyncMock(
            side_effect=[
                RPCTimeoutError("Timed out", timeout_seconds=30.0),
                RPCTimeoutError("Timed out", timeout_seconds=30.0),
            ]
        )

        with patch.object(_research_mod.asyncio, "sleep", new_callable=AsyncMock) as mock_sleep:
            imported = await research.import_sources_with_verification(
                "nb_123",
                "task_123",
                sources,
                initial_delay=5,
            )

        assert imported == [
            {"id": "src_1", "title": "Source 1"},
            {"id": "src_2", "title": "Source 2"},
        ]
        assert research.import_sources.await_count == 2
        retry_call_sources = research.import_sources.await_args_list[1].args[2]
        assert retry_call_sources == [{"url": "https://two.example.com", "title": "Source 2"}]
        mock_sleep.assert_awaited_once_with(5)

    @pytest.mark.asyncio
    async def test_snapshot_failure_deduplicates_retries_without_verified_success(self):
        """A malformed pre-import snapshot must not masquerade as an empty
        notebook. Without a reliable baseline we can still drop URLs already
        visible after a timeout, but we must not classify all current rows
        as newly imported by this call.
        """
        source_1 = MagicMock(id="src_1", title="Source 1", url="https://one.example.com")
        source_2 = MagicMock(id="src_2", title="Source 2", url="https://two.example.com")
        source_3 = MagicMock(id="src_3", title="Source 3", url="https://three.example.com")
        sources = [
            {"url": "https://one.example.com", "title": "Source 1"},
            {"url": "https://two.example.com", "title": "Source 2"},
            {"url": "https://three.example.com", "title": "Source 3"},
        ]
        research, _, mock_source_lister = _make_research()
        mock_source_lister.list = AsyncMock(
            side_effect=[
                RPCError("snapshot unavailable"),
                [source_1],
                [source_1, source_2, source_3],
            ]
        )
        research.import_sources = AsyncMock(
            side_effect=[
                RPCTimeoutError("Timed out", timeout_seconds=30.0),
                RPCTimeoutError("Timed out", timeout_seconds=30.0),
            ]
        )

        with patch.object(_research_mod.asyncio, "sleep", new_callable=AsyncMock) as mock_sleep:
            imported = await research.import_sources_with_verification(
                "nb_123",
                "task_123",
                sources,
                initial_delay=5,
            )

        assert imported == []
        assert research.import_sources.await_count == 2
        assert mock_source_lister.list.await_count == 3
        assert all(
            awaited_call.kwargs.get("strict") is True
            for awaited_call in mock_source_lister.list.await_args_list
        )
        assert research.import_sources.await_args_list[0].args[2] == sources
        assert research.import_sources.await_args_list[1].args[2] == [
            {"url": "https://two.example.com", "title": "Source 2"},
            {"url": "https://three.example.com", "title": "Source 3"},
        ]
        mock_sleep.assert_awaited_once_with(5)

    @pytest.mark.asyncio
    async def test_all_requested_urls_already_present_skips_import_entirely(self):
        """If every requested URL is already in the notebook, the up-front
        idempotency filter (#1961) removes them all — no import RPC is issued
        and the already-present set is reported. (Previously this URL was only
        dropped after a timeout probe; now it never reaches the import path.)
        """
        existing_src = MagicMock(id="src_existing", title="Old", url="https://example.com")
        research, _, mock_source_lister = _make_research()
        mock_source_lister.list = AsyncMock(return_value=[existing_src])
        research.import_sources = AsyncMock(
            side_effect=AssertionError("import_sources must not be called")
        )

        with patch.object(_research_mod.asyncio, "sleep", new_callable=AsyncMock) as mock_sleep:
            imported = await research.import_sources_with_verification(
                "nb_123",
                "task_123",
                [{"url": "https://example.com", "title": "Old (request)"}],
                initial_delay=5,
            )

        assert list(imported) == []
        assert imported.already_present == [
            {"id": "src_existing", "title": "Old", "url": "https://example.com"}
        ]
        research.import_sources.assert_not_awaited()
        mock_sleep.assert_not_awaited()
        # Only the baseline snapshot — no post-timeout probe (path never entered).
        assert mock_source_lister.list.await_count == 1

    @pytest.mark.asyncio
    async def test_unrelated_concurrent_addition_does_not_falsely_verify_new_url(
        self,
    ):
        """The verified-success branch must NOT fire when the post-timeout probe
        shows only an unrelated concurrent addition — not our requested URL.

        A pre-existing requested URL is filtered up front (#1961); a genuinely
        NEW requested URL that never appears in the probe still forces a retry
        (a concurrent unrelated addition is not proof our import wrote anything).
        """
        existing_src = MagicMock(id="src_existing", title="Old", url="https://example.com")
        unrelated_src = MagicMock(
            id="src_unrelated",
            title="Unrelated (concurrent)",
            url="https://other.example.com",
        )
        research, _, mock_source_lister = _make_research()
        mock_source_lister.list = AsyncMock(
            side_effect=[
                [existing_src],  # baseline already has example.com
                # post-timeout: pre-existing + unrelated concurrent addition,
                # but no truly-new source matching the requested NEW URL.
                [existing_src, unrelated_src],
            ]
        )
        research.import_sources = AsyncMock(
            side_effect=[
                RPCTimeoutError("Timed out", timeout_seconds=30.0),
                [{"id": "src_new", "title": "New"}],
            ]
        )

        with patch.object(_research_mod.asyncio, "sleep", new_callable=AsyncMock) as mock_sleep:
            imported = await research.import_sources_with_verification(
                "nb_123",
                "task_123",
                [
                    # already present → filtered up front
                    {"url": "https://example.com", "title": "Old (request)"},
                    # genuinely new → imported, never appears in probe → retried
                    {"url": "https://new.example.com", "title": "New"},
                ],
                initial_delay=5,
            )

        assert list(imported) == [{"id": "src_new", "title": "New"}]
        assert imported.already_present == [
            {"id": "src_existing", "title": "Old", "url": "https://example.com"}
        ]
        # example.com filtered up front → only new.example.com was ever sent,
        # and the false-verified-success guard held (it retried).
        assert research.import_sources.await_count == 2
        assert research.import_sources.await_args_list[0].args[2] == [
            {"url": "https://new.example.com", "title": "New"}
        ]
        mock_sleep.assert_awaited_once_with(5)

    @pytest.mark.asyncio
    async def test_pre_existing_url_does_not_prove_report_entry_committed(self):
        """Pre-existing URLs de-dupe URL entries but must not drop no-URL reports.

        A URL visible before the timed-out request is not proof that this
        request committed the preceding report entry. Only a requested URL
        newly observed after the attempt may suppress no-URL report retries.
        """
        existing_src = MagicMock(id="src_existing", title="Old", url="https://example.com")
        report_entry = {
            "title": "Research Report",
            "report_markdown": "# Findings\n...",
            "result_type": 5,
        }
        research, _, mock_source_lister = _make_research()
        mock_source_lister.list = AsyncMock(
            side_effect=[
                [existing_src],  # baseline already has the requested URL
                [existing_src],  # post-timeout: no newly committed URL
            ]
        )
        research.import_sources = AsyncMock(
            side_effect=[
                RPCTimeoutError("Timed out", timeout_seconds=30.0),
                [{"id": "src_report", "title": "Research Report"}],
            ]
        )

        with patch.object(_research_mod.asyncio, "sleep", new_callable=AsyncMock) as mock_sleep:
            imported = await research.import_sources_with_verification(
                "nb_123",
                "task_123",
                [
                    report_entry,
                    {"url": "https://example.com", "title": "Old (request)"},
                ],
                initial_delay=5,
            )

        assert imported == [{"id": "src_report", "title": "Research Report"}]
        assert research.import_sources.await_count == 2
        assert research.import_sources.await_args_list[1].args[2] == [report_entry]
        mock_sleep.assert_awaited_once_with(5)

    @pytest.mark.asyncio
    async def test_returned_list_includes_non_url_sources_like_research_reports(self):
        """When the request includes a research-report entry (no URL, only
        title + ``report_markdown``), the verified-success return value must
        surface the matching new no-URL source so callers can count it as
        imported.
        """
        report_src = MagicMock(id="src_report", title="Research Report", url=None)
        new_src = MagicMock(id="src_new", title="Source 1", url="https://example.com")
        research, _, mock_source_lister = _make_research()
        mock_source_lister.list = AsyncMock(
            side_effect=[
                [],  # empty baseline
                [report_src, new_src],  # both new after the timeout
            ]
        )
        research.import_sources = AsyncMock(
            side_effect=RPCTimeoutError("Timed out", timeout_seconds=30.0)
        )

        with patch.object(_research_mod.asyncio, "sleep", new_callable=AsyncMock):
            imported = await research.import_sources_with_verification(
                "nb_123",
                "task_123",
                [
                    # Mixed request: one URL + one report entry.
                    {"url": "https://example.com", "title": "Source 1"},
                    {
                        "title": "Research Report",
                        "report_markdown": "# Findings\n...",
                        "result_type": 5,
                    },
                ],
            )

        ids_returned = {entry["id"] for entry in imported}
        assert ids_returned == {"src_report", "src_new"}

    @pytest.mark.asyncio
    async def test_no_url_verified_success_is_capped_to_requested_no_url_count(self):
        """Concurrent no-URL rows must not inflate the synthesized import count."""
        requested_report = MagicMock(id="src_report", title="Research Report", url=None)
        concurrent_report = MagicMock(id="src_concurrent", title="Concurrent Report", url=None)
        new_src = MagicMock(id="src_new", title="Source 1", url="https://example.com")
        research, _, mock_source_lister = _make_research()
        mock_source_lister.list = AsyncMock(
            side_effect=[[], [requested_report, concurrent_report, new_src]]
        )
        research.import_sources = AsyncMock(
            side_effect=RPCTimeoutError("Timed out", timeout_seconds=30.0)
        )

        with patch.object(_research_mod.asyncio, "sleep", new_callable=AsyncMock):
            imported = await research.import_sources_with_verification(
                "nb_123",
                "task_123",
                [
                    {"url": "https://example.com", "title": "Source 1"},
                    {
                        "title": "Research Report",
                        "report_markdown": "# Findings\n...",
                        "result_type": 5,
                    },
                ],
            )

        ids_returned = {entry["id"] for entry in imported}
        assert ids_returned == {"src_report", "src_new"}

    @pytest.mark.asyncio
    async def test_does_not_over_report_concurrent_no_url_source(self):
        """When the request has NO no-URL entries (URLs only), a concurrent
        no-URL source added during the timeout window must NOT be reported
        as imported — even if the requested URL itself was successfully
        written. Otherwise the caller's ``len(imported)`` overstates what
        this call actually added.
        """
        new_src = MagicMock(id="src_new", title="Source 1", url="https://example.com")
        concurrent_report = MagicMock(
            id="src_concurrent_report", title="Unrelated Report", url=None
        )
        research, _, mock_source_lister = _make_research()
        mock_source_lister.list = AsyncMock(
            side_effect=[
                [],  # empty baseline
                [new_src, concurrent_report],
            ]
        )
        research.import_sources = AsyncMock(
            side_effect=RPCTimeoutError("Timed out", timeout_seconds=30.0)
        )

        with patch.object(_research_mod.asyncio, "sleep", new_callable=AsyncMock):
            imported = await research.import_sources_with_verification(
                "nb_123",
                "task_123",
                [{"url": "https://example.com", "title": "Source 1"}],
            )

        assert imported == [{"id": "src_new", "title": "Source 1"}]

    @pytest.mark.asyncio
    async def test_does_not_falsely_succeed_on_unrelated_concurrent_source(self):
        """Concurrent activity from another session (e.g. web UI, parallel
        CLI) can add unrelated sources during the import window. The
        verification condition must NOT fire on those — success must require
        the *requested* URLs to actually appear among the new sources, not
        just that the post-timeout source count grew.

        Without this guard, a real timeout coinciding with any concurrent
        addition would skip the retry and return the unrelated source as
        "imported" — silently losing the user's import.
        """
        unrelated_src = MagicMock(
            id="src_unrelated",
            title="Unrelated",
            url="https://other.example.com",
        )
        research, _, mock_source_lister = _make_research()
        mock_source_lister.list = AsyncMock(
            side_effect=[
                [],  # baseline: empty
                # Post-timeout: only the unrelated concurrent addition is
                # visible; our requested URL is NOT there.
                [unrelated_src],
            ]
        )
        research.import_sources = AsyncMock(
            side_effect=[
                RPCTimeoutError("Timed out", timeout_seconds=30.0),
                [{"id": "src_new", "title": "Source 1"}],
            ]
        )

        with patch.object(_research_mod.asyncio, "sleep", new_callable=AsyncMock) as mock_sleep:
            imported = await research.import_sources_with_verification(
                "nb_123",
                "task_123",
                [{"url": "https://example.com", "title": "Source 1"}],
                initial_delay=5,
            )

        # Must retry, not falsely return the unrelated source.
        assert imported == [{"id": "src_new", "title": "Source 1"}]
        assert research.import_sources.await_count == 2
        mock_sleep.assert_awaited_once_with(5)

    @pytest.mark.asyncio
    async def test_does_not_readd_pre_existing_requested_url_on_happy_path(self):
        """A requested URL already in the notebook is filtered up front (#1961)
        and never re-added, while a genuinely-new requested URL imports normally.

        Guards the classic false-success trap: ``requested.issubset(current)``
        is trivially true for a pre-existing URL, so relying on it would wrongly
        re-add the URL. The up-front filter removes it before the import.
        """
        existing_src = MagicMock(id="src_existing", title="Old", url="https://example.com")
        research, _, mock_source_lister = _make_research()
        mock_source_lister.list = AsyncMock(return_value=[existing_src])
        research.import_sources = AsyncMock(return_value=[{"id": "src_new", "title": "New"}])

        imported = await research.import_sources_with_verification(
            "nb_123",
            "task_123",
            [
                {"url": "https://example.com", "title": "Old (request)"},
                {"url": "https://new.example.com", "title": "New"},
            ],
        )

        assert list(imported) == [{"id": "src_new", "title": "New"}]
        assert imported.already_present == [
            {"id": "src_existing", "title": "Old", "url": "https://example.com"}
        ]
        # The pre-existing URL was NOT re-sent — only the genuinely-new one.
        assert research.import_sources.await_args.args[2] == [
            {"url": "https://new.example.com", "title": "New"}
        ]

    @pytest.mark.asyncio
    async def test_report_only_import_bounded_retries_on_persistent_timeout(self):
        """Report-only deep-research imports (no URLs) can't use the
        URL-match verification path. To bound the worst-case duplicate
        inflation, the retry loop must give up after a small number of
        attempts rather than burning the full ``max_elapsed`` budget —
        otherwise a persistent timeout still produces 5-6× duplicate reports.

        Patches ``time.monotonic`` to never advance past budget, so the only
        thing that can bound the loop is an explicit retry cap on the
        no-URL path.
        """
        research, _, mock_source_lister = _make_research()
        mock_source_lister.list = AsyncMock(return_value=[])
        research.import_sources = AsyncMock(
            side_effect=RPCTimeoutError("Timed out", timeout_seconds=30.0)
        )

        with (
            # Time budget never expires — only the retry cap can stop the loop.
            patch.object(_research_mod.time, "monotonic", return_value=0.0),
            patch.object(_research_mod.asyncio, "sleep", new_callable=AsyncMock) as mock_sleep,
            pytest.raises(RPCTimeoutError),
        ):
            await research.import_sources_with_verification(
                "nb_123",
                "task_123",
                [
                    {
                        "title": "Research Report",
                        "report_markdown": "# Findings\n...",
                        "result_type": 5,
                    }
                ],
                initial_delay=1,
            )

        # Exactly 2 attempts (1 original + 1 retry) before raising. ``<= 2``
        # would also pass if the retry disappeared entirely, which would
        # mask a regression — assert the cap and the single backoff sleep.
        assert research.import_sources.await_count == 2
        mock_sleep.assert_awaited_once_with(1)

    @pytest.mark.asyncio
    async def test_falls_back_to_retry_when_post_timeout_probe_raises(self):
        """If the post-timeout ``sources.list`` probe itself fails (transient
        network blip, server hiccup), the function must log and fall back to
        the legacy retry path rather than crashing or skipping verification
        silently.
        """
        new_src = MagicMock(id="src_new", title="Source 1", url="https://example.com")
        research, _, mock_source_lister = _make_research()
        mock_source_lister.list = AsyncMock(
            side_effect=[
                [],  # baseline
                NetworkError("probe down"),  # post-timeout probe fails
                [new_src],  # post-retry probe (would succeed if reached, unused)
            ]
        )
        research.import_sources = AsyncMock(
            side_effect=[
                RPCTimeoutError("Timed out", timeout_seconds=30.0),
                [{"id": "src_new", "title": "Source 1"}],
            ]
        )

        with patch.object(_research_mod.asyncio, "sleep", new_callable=AsyncMock) as mock_sleep:
            imported = await research.import_sources_with_verification(
                "nb_123",
                "task_123",
                [{"url": "https://example.com", "title": "Source 1"}],
                initial_delay=5,
            )

        assert imported == [{"id": "src_new", "title": "Source 1"}]
        # Probe failure → legacy retry path → 2 import attempts.
        assert research.import_sources.await_count == 2
        mock_sleep.assert_awaited_once_with(5)

    @pytest.mark.asyncio
    async def test_snapshot_propagates_cancelled_error(self):
        """``asyncio.CancelledError`` from the pre-import snapshot must
        propagate so callers can cleanly cancel the operation. A bare
        ``except Exception`` would swallow it and continue running.
        """
        research, _, mock_source_lister = _make_research()
        mock_source_lister.list = AsyncMock(side_effect=asyncio.CancelledError())
        research.import_sources = AsyncMock()

        with pytest.raises(asyncio.CancelledError):
            await research.import_sources_with_verification(
                "nb_123",
                "task_123",
                [{"url": "https://example.com", "title": "Source 1"}],
            )

        # The import should never run — cancellation aborted the snapshot.
        research.import_sources.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_probe_propagates_cancelled_error(self):
        """``asyncio.CancelledError`` from the post-timeout probe must
        propagate, not be swallowed and converted into a retry.
        """
        research, _, mock_source_lister = _make_research()
        mock_source_lister.list = AsyncMock(
            side_effect=[
                [],  # baseline OK
                asyncio.CancelledError(),  # probe cancelled
            ]
        )
        research.import_sources = AsyncMock(
            side_effect=RPCTimeoutError("Timed out", timeout_seconds=30.0)
        )

        with (
            patch.object(_research_mod.asyncio, "sleep", new_callable=AsyncMock),
            pytest.raises(asyncio.CancelledError),
        ):
            await research.import_sources_with_verification(
                "nb_123",
                "task_123",
                [{"url": "https://example.com", "title": "Source 1"}],
            )

        # Only the original attempt — no retry after cancellation.
        assert research.import_sources.await_count == 1


class TestImportSourcesIdempotency:
    """#1961: pre-filter already-present URLs up front on every attempt.

    The timeout-retry path already drops already-present URLs; these tests
    cover the generalization to the happy path (no timeout), plus the
    ``already_present`` side channel and the ``allow_duplicate`` opt-out.
    """

    @pytest.mark.asyncio
    async def test_repeat_import_all_present_imports_nothing(self):
        existing = [
            MagicMock(id="src_a", title="A", url="https://a.example.com"),
            MagicMock(id="src_b", title="B", url="https://b.example.com"),
        ]
        research, _, mock_source_lister = _make_research()
        mock_source_lister.list = AsyncMock(return_value=existing)
        research.import_sources = AsyncMock(return_value=[])

        imported = await research.import_sources_with_verification(
            "nb_123",
            "task_123",
            [
                {"url": "https://a.example.com", "title": "A"},
                {"url": "https://b.example.com", "title": "B"},
            ],
        )

        assert list(imported) == []
        assert imported.already_present == [
            {"id": "src_a", "title": "A", "url": "https://a.example.com"},
            {"id": "src_b", "title": "B", "url": "https://b.example.com"},
        ]
        # Everything already present → no import RPC at all.
        research.import_sources.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_partial_present_imports_only_absent(self):
        existing = [MagicMock(id="src_a", title="A", url="https://a.example.com")]
        research, _, mock_source_lister = _make_research()
        mock_source_lister.list = AsyncMock(return_value=existing)
        research.import_sources = AsyncMock(return_value=[{"id": "src_b", "title": "B"}])

        imported = await research.import_sources_with_verification(
            "nb_123",
            "task_123",
            [
                {"url": "https://a.example.com", "title": "A"},
                {"url": "https://b.example.com", "title": "B"},
            ],
        )

        assert list(imported) == [{"id": "src_b", "title": "B"}]
        assert imported.already_present == [
            {"id": "src_a", "title": "A", "url": "https://a.example.com"}
        ]
        # Only the genuinely-absent source B was handed to import_sources.
        assert research.import_sources.await_args.args[2] == [
            {"url": "https://b.example.com", "title": "B"}
        ]

    @pytest.mark.asyncio
    async def test_allow_duplicate_reimports_all(self):
        existing = [MagicMock(id="src_a", title="A", url="https://a.example.com")]
        research, _, mock_source_lister = _make_research()
        mock_source_lister.list = AsyncMock(return_value=existing)
        research.import_sources = AsyncMock(return_value=[{"id": "src_a2", "title": "A"}])

        imported = await research.import_sources_with_verification(
            "nb_123",
            "task_123",
            [{"url": "https://a.example.com", "title": "A"}],
            allow_duplicate=True,
        )

        assert list(imported) == [{"id": "src_a2", "title": "A"}]
        assert imported.already_present == []
        # allow_duplicate → no pre-filter, the present URL is re-sent.
        assert research.import_sources.await_args.args[2] == [
            {"url": "https://a.example.com", "title": "A"}
        ]

    @pytest.mark.asyncio
    async def test_report_entry_preserved_when_url_already_present(self):
        existing = [MagicMock(id="src_a", title="A", url="https://a.example.com")]
        report_entry = {"title": "Report", "report_markdown": "# R", "result_type": 5}
        research, _, mock_source_lister = _make_research()
        mock_source_lister.list = AsyncMock(return_value=existing)
        research.import_sources = AsyncMock(return_value=[{"id": "rep_1", "title": "Report"}])

        imported = await research.import_sources_with_verification(
            "nb_123",
            "task_123",
            [{"url": "https://a.example.com", "title": "A"}, report_entry],
        )

        assert list(imported) == [{"id": "rep_1", "title": "Report"}]
        assert imported.already_present == [
            {"id": "src_a", "title": "A", "url": "https://a.example.com"}
        ]
        # Report entry has no dedupable URL → kept; the present URL is dropped.
        assert research.import_sources.await_args.args[2] == [report_entry]

    @pytest.mark.asyncio
    async def test_snapshot_failure_imports_all_without_filter(self):
        research, _, mock_source_lister = _make_research()
        mock_source_lister.list = AsyncMock(side_effect=NetworkError("snapshot down"))
        research.import_sources = AsyncMock(return_value=[{"id": "src_a", "title": "A"}])

        imported = await research.import_sources_with_verification(
            "nb_123",
            "task_123",
            [{"url": "https://a.example.com", "title": "A"}],
        )

        assert list(imported) == [{"id": "src_a", "title": "A"}]
        # No baseline → can't tell what's present → import everything (fallback).
        assert imported.already_present == []
        assert research.import_sources.await_args.args[2] == [
            {"url": "https://a.example.com", "title": "A"}
        ]

    @pytest.mark.asyncio
    async def test_dedup_composes_with_timeout_verified_success(self):
        existing_a = MagicMock(id="src_a", title="A", url="https://a.example.com")
        new_b = MagicMock(id="src_b", title="B", url="https://b.example.com")
        research, _, mock_source_lister = _make_research()
        mock_source_lister.list = AsyncMock(
            side_effect=[
                [existing_a],  # baseline — A already present
                [existing_a, new_b],  # post-timeout probe — B committed
            ]
        )
        research.import_sources = AsyncMock(
            side_effect=RPCTimeoutError("Timed out", timeout_seconds=30.0)
        )

        with patch.object(_research_mod.asyncio, "sleep", new_callable=AsyncMock):
            imported = await research.import_sources_with_verification(
                "nb_123",
                "task_123",
                [
                    {"url": "https://a.example.com", "title": "A"},
                    {"url": "https://b.example.com", "title": "B"},
                ],
            )

        assert list(imported) == [{"id": "src_b", "title": "B"}]
        assert imported.already_present == [
            {"id": "src_a", "title": "A", "url": "https://a.example.com"}
        ]
        # A was filtered up front, so only B was ever sent to import_sources.
        assert research.import_sources.await_args.args[2] == [
            {"url": "https://b.example.com", "title": "B"}
        ]

    @pytest.mark.asyncio
    async def test_provenance_validated_before_filter_when_all_present(self):
        """A wrong ``research_task_id`` raises even when every requested URL is
        already present — provenance is validated before the idempotency
        pre-filter can drop the entries (coderabbit review on #1961)."""
        from notebooklm.exceptions import ResearchTaskMismatchError

        existing = [MagicMock(id="src_a", title="A", url="https://a.example.com")]
        research, _, mock_source_lister = _make_research()
        mock_source_lister.list = AsyncMock(return_value=existing)
        research.import_sources = AsyncMock(
            side_effect=AssertionError("import_sources must not be called")
        )

        with pytest.raises(ResearchTaskMismatchError):
            await research.import_sources_with_verification(
                "nb_123",
                "task_123",
                [
                    {
                        "url": "https://a.example.com",
                        "title": "A",
                        "research_task_id": "wrong-task",
                    }
                ],
            )
        research.import_sources.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_already_present_reported_once_for_repeated_url(self):
        """A request repeating the same (normalized) already-present URL reports
        that existing source once, not once per duplicate input (coderabbit)."""
        existing = [MagicMock(id="src_a", title="A", url="https://a.example.com")]
        research, _, mock_source_lister = _make_research()
        mock_source_lister.list = AsyncMock(return_value=existing)
        research.import_sources = AsyncMock(
            side_effect=AssertionError("import_sources must not be called")
        )

        imported = await research.import_sources_with_verification(
            "nb_123",
            "task_123",
            [
                {"url": "https://a.example.com", "title": "A"},
                # Same normalized URL (trailing slash stripped) — a duplicate input.
                {"url": "https://a.example.com/", "title": "A again"},
            ],
        )

        assert list(imported) == []
        assert imported.already_present == [
            {"id": "src_a", "title": "A", "url": "https://a.example.com"}
        ]
        research.import_sources.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_allow_duplicate_reimports_baseline_url_after_timeout(self):
        """`allow_duplicate=True` must be honored on the timeout-reconcile path:
        a pre-existing BASELINE url whose first import attempt times out (but was
        NOT newly committed) is retried and re-added, not treated as an empty
        "already done" success (codex review on #1961). The #1934 safety still
        holds — a URL this attempt actually committed (post-baseline) is dropped.
        """
        baseline_x = MagicMock(id="src_x0", title="X", url="https://x.example.com")
        research, _, mock_source_lister = _make_research()
        mock_source_lister.list = AsyncMock(
            side_effect=[
                [baseline_x],  # baseline — X already present
                [baseline_x],  # post-timeout probe — nothing NEW committed yet
            ]
        )
        research.import_sources = AsyncMock(
            side_effect=[
                RPCTimeoutError("Timed out", timeout_seconds=30.0),
                [{"id": "src_x1", "title": "X"}],  # retry re-adds it
            ]
        )

        with patch.object(_research_mod.asyncio, "sleep", new_callable=AsyncMock) as mock_sleep:
            imported = await research.import_sources_with_verification(
                "nb_123",
                "task_123",
                [{"url": "https://x.example.com", "title": "X"}],
                allow_duplicate=True,
                initial_delay=5,
            )

        # It retried (did NOT short-circuit to empty success) and re-added X.
        assert list(imported) == [{"id": "src_x1", "title": "X"}]
        assert imported.already_present == []
        assert research.import_sources.await_count == 2
        # The baseline URL stayed in the retry batch (not dropped as "present").
        assert research.import_sources.await_args_list[1].args[2] == [
            {"url": "https://x.example.com", "title": "X"}
        ]
        mock_sleep.assert_awaited_once_with(5)
