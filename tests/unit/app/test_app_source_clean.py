"""Unit tests for the transport-neutral ``notebooklm._app.source_clean`` core.

These pin the relocated ``source clean`` business logic at the ``_app`` boundary
(independent of the Click adapter):

* :func:`classify_junk_sources` — the junk classifier (error-status / gateway-title
  / fragment-stripped URL dedup with oldest-kept ordering). These assertions
  were **moved** from ``tests/unit/cli/test_source.py::TestSourceCleanClassify``,
  which already called the pure function directly through the
  ``cli.source_cmd._classify_junk_sources`` re-export — they now target the
  neutral symbol with no behaviour change.
* :func:`normalize_url_for_dedup` — fragment-only stripping + scheme/host lower.
* :func:`candidates_payload` — the JSON payload shape projection.
* :func:`run_source_clean` — the classify → confirm → batched-delete orchestration
  (already-clean short-circuit, dry-run, cancellation, chunked deletion with
  partial-failure capture) driven by injected list/delete/confirm callables.

Pure-service tests (no Click / CliRunner): the command-layer wiring is exercised
in ``tests/unit/cli/test_source.py::TestSourceCleanCommand``.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from notebooklm._app.source_clean import (
    candidates_payload,
    classify_junk_sources,
    normalize_url_for_dedup,
    run_source_clean,
)
from notebooklm.types import Source, SourceStatus


def _src(
    sid: str,
    *,
    title: str | None = None,
    url: str | None = None,
    status: int = SourceStatus.READY,
    created_at: datetime | None = None,
) -> Source:
    """Build a Source fixture with sensible defaults for clean tests."""
    return Source(id=sid, title=title, url=url, status=status, created_at=created_at)


# ===========================================================================
# classify_junk_sources (moved from cli/test_source.py::TestSourceCleanClassify)
# ===========================================================================


class TestClassifyJunkSources:
    """Pure classification logic — every branch without mocking the client."""

    def test_empty_notebook_returns_nothing(self) -> None:
        assert classify_junk_sources([]) == []

    def test_error_status_is_flagged(self) -> None:
        s = _src("src_e", status=SourceStatus.ERROR, url="https://ex.com/a")
        out = classify_junk_sources([s])
        assert [(c[0], c[3]) for c in out] == [("src_e", "error_status")]

    def test_unknown_status_is_not_flagged(self) -> None:
        # Unrecognized status codes must NOT be auto-deleted; they may
        # represent future NotebookLM states or missing-status payloads.
        s = _src("src_u", status=99, url="https://ex.com/a")
        assert classify_junk_sources([s]) == []

    def test_zero_status_is_not_flagged(self) -> None:
        # status=0 maps to "unknown" via the truthy fallback. Must NOT delete.
        s = _src("src_z", status=0, url="https://ex.com/a")
        assert classify_junk_sources([s]) == []

    def test_processing_status_is_not_flagged(self) -> None:
        s = _src("src_p", status=SourceStatus.PROCESSING, url="https://ex.com/a")
        assert classify_junk_sources([s]) == []

    @pytest.mark.parametrize(
        "title",
        [
            "Access Denied",
            "403 Forbidden",
            "404 Not Found",
            "Just a Moment...",
            "Attention Required! | Cloudflare",
            "Security check",
            "CAPTCHA verification",
            "  403  ",
        ],
    )
    def test_gateway_titles_are_flagged(self, title: str) -> None:
        s = _src("src_g", title=title, url="https://ex.com/a")
        out = classify_junk_sources([s])
        assert [(c[0], c[3]) for c in out] == [("src_g", "gateway_title")]

    def test_legitimate_titles_starting_with_digits_are_not_flagged(self) -> None:
        # "404 Not Found" is a gateway title, but "100 Ways to ..." is not.
        s = _src("src_ok", title="100 Ways to Cook Pasta", url="https://ex.com/a")
        assert classify_junk_sources([s]) == []

    def test_url_title_on_ready_source_is_not_deleted(self) -> None:
        # Regression: PR review caught that URL-as-title was being treated as
        # junk, which deletes legitimate in-flight sources (Source.title is
        # documented to "may be URL if not yet processed").
        s = _src(
            "src_url",
            title="https://example.com/article",
            url="https://example.com/article",
        )
        assert classify_junk_sources([s]) == []

    def test_dedup_keeps_oldest_and_flags_later_copies(self) -> None:
        # Oldest at t=0; two later duplicates.
        sources = [
            _src("src_3", url="https://ex.com/a", created_at=datetime(2024, 3, 1)),
            _src("src_1", url="https://ex.com/a", created_at=datetime(2024, 1, 1)),
            _src("src_2", url="https://ex.com/a", created_at=datetime(2024, 2, 1)),
        ]
        out = classify_junk_sources(sources)
        deleted_ids = sorted(c[0] for c in out)
        assert deleted_ids == ["src_2", "src_3"]
        assert all(c[3].startswith("duplicate_of:src_1"[:21]) for c in out)

    def test_dedup_when_oldest_is_error(self) -> None:
        # First copy (oldest) is error → flagged as error_status, NOT recorded
        # in seen_urls. Second copy becomes the kept anchor; third copy
        # deduped against it. Both deletions report their own reason.
        sources = [
            _src(
                "src_e",
                url="https://ex.com/a",
                status=SourceStatus.ERROR,
                created_at=datetime(2024, 1, 1),
            ),
            _src("src_ok", url="https://ex.com/a", created_at=datetime(2024, 2, 1)),
            _src("src_dup", url="https://ex.com/a", created_at=datetime(2024, 3, 1)),
        ]
        out = classify_junk_sources(sources)
        by_id = {c[0]: c[3] for c in out}
        assert set(by_id) == {"src_e", "src_dup"}
        assert by_id["src_e"] == "error_status"
        assert by_id["src_dup"].startswith("duplicate_of:")

    def test_dedup_preserves_query_string(self) -> None:
        # Different YouTube video IDs (via ?v=) must NOT be collapsed.
        sources = [
            _src("yt_a", url="https://youtube.com/watch?v=AAA"),
            _src("yt_b", url="https://youtube.com/watch?v=BBB"),
        ]
        assert classify_junk_sources(sources) == []

    def test_dedup_strips_fragment(self) -> None:
        sources = [
            _src("src_1", url="https://ex.com/a#top", created_at=datetime(2024, 1, 1)),
            _src("src_2", url="https://ex.com/a#bottom", created_at=datetime(2024, 2, 1)),
        ]
        out = classify_junk_sources(sources)
        assert [c[0] for c in out] == ["src_2"]

    def test_dedup_is_case_insensitive_on_scheme_and_host(self) -> None:
        # Per RFC 3986, scheme and host are case-insensitive, so mixed-case
        # copies of the same URL must be recognised as duplicates.
        sources = [
            _src("src_1", url="https://Example.COM/a", created_at=datetime(2024, 1, 1)),
            _src("src_2", url="HTTPS://example.com/a", created_at=datetime(2024, 2, 1)),
        ]
        out = classify_junk_sources(sources)
        assert [c[0] for c in out] == ["src_2"]

    def test_undated_sources_go_to_end_of_sort(self) -> None:
        # If src_undated were placed at position 0 (epoch sentinel), it would
        # be kept and src_dated deleted as a duplicate. With float('inf') the
        # dated one wins.
        sources = [
            _src("src_undated", url="https://ex.com/a", created_at=None),
            _src("src_dated", url="https://ex.com/a", created_at=datetime(2024, 1, 1)),
        ]
        out = classify_junk_sources(sources)
        assert [c[0] for c in out] == ["src_undated"]

    def test_source_with_no_url_is_not_deduped(self) -> None:
        # Text-only sources have url=None — they must never be deduped together.
        sources = [
            _src("src_1", title="Note A"),
            _src("src_2", title="Note B"),
        ]
        assert classify_junk_sources(sources) == []


# ===========================================================================
# normalize_url_for_dedup — net-new direct coverage
# ===========================================================================


class TestNormalizeUrlForDedup:
    def test_strips_fragment_only(self) -> None:
        assert normalize_url_for_dedup("https://ex.com/a#frag") == "https://ex.com/a"

    def test_preserves_query_string(self) -> None:
        assert normalize_url_for_dedup("https://ex.com/a?v=1#frag") == "https://ex.com/a?v=1"

    def test_lowercases_scheme_and_host_only(self) -> None:
        # Path case is preserved; scheme + host are lowercased.
        assert normalize_url_for_dedup("HTTPS://Example.COM/A/Path") == "https://example.com/A/Path"


# ===========================================================================
# candidates_payload — JSON projection shape
# ===========================================================================


def test_candidates_payload_shape() -> None:
    candidates = [
        ("src_1", "Page", "ready", "duplicate_of:src_0aaa"),
        ("src_2", "oops", "error", "error_status"),
    ]
    assert candidates_payload(candidates) == [
        {
            "id": "src_1",
            "title": "Page",
            "status": "ready",
            "reason": "duplicate_of:src_0aaa",
        },
        {"id": "src_2", "title": "oops", "status": "error", "reason": "error_status"},
    ]


def test_candidates_payload_empty() -> None:
    assert candidates_payload([]) == []


# ===========================================================================
# run_source_clean — orchestration
# ===========================================================================


def _junk_sources() -> list[Source]:
    """Two junk sources: one error-status, one gateway-title."""
    return [
        _src("src_err", title="oops", status=SourceStatus.ERROR),
        _src("src_block", title="Just a Moment...", url="https://ex.com/x"),
    ]


@pytest.mark.asyncio
async def test_run_clean_already_clean_short_circuits() -> None:
    list_sources = AsyncMock(return_value=[_src("src_ok", title="Page", url="https://ex.com/a")])
    delete_source = AsyncMock()
    result = await run_source_clean(
        notebook_id="nb_1",
        dry_run=False,
        yes=True,
        list_sources=list_sources,
        delete_source=delete_source,
        confirm_delete=lambda n: True,
    )
    assert result.status == "already_clean"
    assert result.candidates == ()
    assert result.candidate_count == 0
    delete_source.assert_not_called()


@pytest.mark.asyncio
async def test_run_clean_dry_run_skips_delete() -> None:
    list_sources = AsyncMock(return_value=_junk_sources())
    delete_source = AsyncMock()
    seen: list[object] = []
    result = await run_source_clean(
        notebook_id="nb_1",
        dry_run=True,
        yes=False,
        list_sources=list_sources,
        delete_source=delete_source,
        confirm_delete=lambda n: True,
        on_candidates=seen.append,
    )
    assert result.status == "dry_run"
    assert result.candidate_count == 2
    assert result.deleted_count == 0
    delete_source.assert_not_called()
    # on_candidates fires once with the classified list.
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_run_clean_declined_confirmation_cancels() -> None:
    list_sources = AsyncMock(return_value=_junk_sources())
    delete_source = AsyncMock()
    result = await run_source_clean(
        notebook_id="nb_1",
        dry_run=False,
        yes=False,
        list_sources=list_sources,
        delete_source=delete_source,
        confirm_delete=lambda n: False,
    )
    assert result.status == "cancelled"
    assert result.candidate_count == 2
    delete_source.assert_not_called()


@pytest.mark.asyncio
async def test_run_clean_yes_deletes_all_candidates() -> None:
    list_sources = AsyncMock(return_value=_junk_sources())
    delete_source = AsyncMock()
    confirm = MagicMock()  # never consulted under yes=True
    started: list[int] = []
    result = await run_source_clean(
        notebook_id="nb_1",
        dry_run=False,
        yes=True,
        list_sources=list_sources,
        delete_source=delete_source,
        confirm_delete=confirm,
        on_delete_start=started.append,
    )
    assert result.status == "completed"
    assert result.deleted_count == 2
    assert result.failure_count == 0
    assert delete_source.await_count == 2
    confirm.assert_not_called()
    assert started == [2]


@pytest.mark.asyncio
async def test_run_clean_captures_partial_failures() -> None:
    list_sources = AsyncMock(return_value=_junk_sources())

    async def fake_delete(notebook_id: str, sid: str) -> None:
        if sid == "src_block":
            raise RuntimeError("boom")

    delete_source = AsyncMock(side_effect=fake_delete)
    result = await run_source_clean(
        notebook_id="nb_1",
        dry_run=False,
        yes=True,
        list_sources=list_sources,
        delete_source=delete_source,
        confirm_delete=lambda n: True,
    )
    assert result.status == "completed"
    assert result.deleted_count == 1
    assert result.failure_count == 1
    failing_ids = {sid for sid, _ in result.failures}
    assert failing_ids == {"src_block"}


@pytest.mark.asyncio
async def test_run_clean_batches_with_sleep_between_chunks() -> None:
    # 12 dup sources → two chunks of 10 + 2; sleep fires once between chunks.
    dups = [
        _src(f"src_{i}", url="https://ex.com/a", created_at=datetime(2024, 1, i + 1))
        for i in range(12)
    ]
    list_sources = AsyncMock(return_value=dups)
    delete_source = AsyncMock()
    sleep = AsyncMock()
    result = await run_source_clean(
        notebook_id="nb_1",
        dry_run=False,
        yes=True,
        list_sources=list_sources,
        delete_source=delete_source,
        confirm_delete=lambda n: True,
        sleep=sleep,
    )
    # Oldest of the 12 is kept; the other 11 are duplicates deleted.
    assert result.status == "completed"
    assert result.deleted_count == 11
    # 11 deletions span two chunks → exactly one inter-chunk sleep.
    sleep.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_clean_uses_injected_classifier() -> None:
    # An injected classifier overrides the default so callers can pre-classify.
    sentinel_candidates = [("src_x", "T", "ready", "error_status")]
    list_sources = AsyncMock(return_value=[_src("src_x", title="T")])
    delete_source = AsyncMock()
    result = await run_source_clean(
        notebook_id="nb_1",
        dry_run=True,
        yes=False,
        list_sources=list_sources,
        delete_source=delete_source,
        confirm_delete=lambda n: True,
        classify_sources=lambda srcs: sentinel_candidates,
    )
    assert result.status == "dry_run"
    assert result.candidates == tuple(sentinel_candidates)
