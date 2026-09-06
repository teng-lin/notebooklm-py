"""Unit tests for transport-neutral source batch admission policy."""

from __future__ import annotations

from notebooklm._app.source_batch import MAX_BATCH_URLS


def test_max_batch_urls_is_positive() -> None:
    assert isinstance(MAX_BATCH_URLS, int) and MAX_BATCH_URLS > 0
