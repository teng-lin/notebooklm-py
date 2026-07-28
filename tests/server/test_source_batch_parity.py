"""Anti-drift gate: the neutral ``_app.source_batch`` fatal set matches REST's status table.

``_app.source_batch`` cannot import the FastAPI-tainted ``CATEGORY_STATUS`` (the
``_app`` import boundary forbids ``server``/``fastapi``), so it expresses "fatal" as
an explicit ``frozenset[ErrorCategory]``. This gate — which CAN import both, since it
lives under ``tests/server`` — proves that frozenset equals the categories whose
``CATEGORY_STATUS`` projection is 401 / 429 / >=500. If a future taxonomy change
diverges the two, this fails loudly.
"""

from __future__ import annotations

import pytest

# CATEGORY_STATUS lives in the fastapi-importing server module.
pytest.importorskip("fastapi")

from notebooklm._app.errors import ErrorCategory  # noqa: E402 - after importorskip guard
from notebooklm._app.source_batch import _FATAL_CATEGORIES  # noqa: E402 - after importorskip guard
from notebooklm.server._errors import CATEGORY_STATUS  # noqa: E402 - after importorskip guard


def test_fatal_categories_match_rest_status_partition() -> None:
    expected = {
        category
        for category, status in CATEGORY_STATUS.items()
        if status in (401, 429) or status >= 500
    }
    assert expected == _FATAL_CATEGORIES


def test_fatal_categories_are_all_real_categories() -> None:
    assert set(ErrorCategory) >= _FATAL_CATEGORIES
