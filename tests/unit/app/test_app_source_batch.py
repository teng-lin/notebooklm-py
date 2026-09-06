"""Unit tests for transport-neutral source batch admission policy."""

from __future__ import annotations

import pytest

from notebooklm._app.source_batch import MAX_BATCH_URLS
from notebooklm._source.batch import validate_source_batch_occurrences
from notebooklm.exceptions import ValidationError


def test_max_batch_urls_is_positive() -> None:
    assert isinstance(MAX_BATCH_URLS, int) and MAX_BATCH_URLS > 0


def test_batch_cap_counts_duplicate_occurrences() -> None:
    validate_source_batch_occurrences(["https://same.example"] * MAX_BATCH_URLS)

    with pytest.raises(ValidationError, match=r"at most 20 entries; got 21"):
        validate_source_batch_occurrences(["https://same.example"] * (MAX_BATCH_URLS + 1))
