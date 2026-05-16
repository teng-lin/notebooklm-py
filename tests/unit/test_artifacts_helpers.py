"""Unit tests for module-level helpers in ``notebooklm._artifacts``.

Focuses on ``_extract_data_table_rows`` — the named extractor that replaces
the raw ``raw_data[0][0][0][0][4][2]`` deep-index chain in
:func:`_parse_data_table`. These tests pin the soft-mode contract (returns
``[]`` on shape drift, never raises) and exercise the four canonical drift
shapes plus the happy path. T8.D2a.
"""

from __future__ import annotations

import logging

import pytest

import notebooklm._artifacts as artifacts_module
from notebooklm._artifacts import (
    _extract_data_table_rows,
    _parse_data_table,
)
from notebooklm.types import ArtifactParseError

# ---------------------------------------------------------------------------
# _extract_data_table_rows — happy path
# ---------------------------------------------------------------------------


def test_extract_data_table_rows_happy_path() -> None:
    """Well-formed CGsXqf shape: returns the inner rows array unchanged."""
    rows_payload = [
        [0, 5, [[0, 5, [[0, 5, [["Col1"]]]]]]],
        [5, 10, [[5, 10, [[5, 10, [["A"]]]]]]],
    ]
    # Build the nested structure: raw_data[0][0][0][0][4][2] -> rows_payload
    raw_data = [[[[[0, 100, None, None, [6, 7, rows_payload]]]]]]

    result = _extract_data_table_rows(raw_data)

    assert result == rows_payload
    # Identity matters: helper must not copy / re-wrap the inner array.
    assert result is rows_payload


# ---------------------------------------------------------------------------
# _extract_data_table_rows — drift shapes (must NOT raise)
# ---------------------------------------------------------------------------


def test_extract_data_table_rows_missing_inner_list(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Inner ``[4]`` slot exists but lacks the ``[2]`` rows entry."""
    # The table-content section ([type, flags, rows_array]) is truncated to
    # ``[type, flags]`` — descending to index 2 must miss without raising.
    raw_data = [[[[[0, 100, None, None, [6, 7]]]]]]

    with caplog.at_level(logging.WARNING):
        result = _extract_data_table_rows(raw_data)

    assert result == []
    # safe_index emits the structured drift warning; we don't assert on the
    # exact wording to keep the test resilient to log-format tweaks.
    assert any("safe_index" in rec.message or "drift" in rec.message for rec in caplog.records)


def test_extract_data_table_rows_wrong_type_at_one_level() -> None:
    """One of the wrapper hops is a string, not a list. Must return ``[]``."""
    # Replace the third wrapper layer with a non-indexable string.
    raw_data = [[[["not-a-list", [[0, 100, None, None, [6, 7, []]]]]]]]

    result = _extract_data_table_rows(raw_data)

    assert result == []


def test_extract_data_table_rows_truncated_structure() -> None:
    """Outer wrapper is only 3 levels deep — descent stops well before [4][2]."""
    raw_data: list = [[[]]]

    result = _extract_data_table_rows(raw_data)

    assert result == []


# ---------------------------------------------------------------------------
# _extract_data_table_rows — extra coverage for non-list inner value
# ---------------------------------------------------------------------------


def test_extract_data_table_rows_non_list_inner_value(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Inner ``[2]`` is a scalar (e.g. None) instead of a list. Returns ``[]``.

    This guards the ``isinstance(rows_array, list)`` normalisation branch in
    the helper, which keeps the caller's "empty data table" path uniform.
    """
    raw_data = [[[[[0, 100, None, None, [6, 7, None]]]]]]

    with caplog.at_level(logging.WARNING):
        result = _extract_data_table_rows(raw_data)

    assert result == []


# ---------------------------------------------------------------------------
# _parse_data_table — fallback path still raises on drift (regression)
# ---------------------------------------------------------------------------


def test_parse_data_table_raises_artifact_parse_error_on_drift() -> None:
    """The fallback at _artifacts.py:236-240 must still raise on shape drift.

    Even though the helper returns ``[]`` on drift, ``_parse_data_table`` must
    convert that to :class:`ArtifactParseError` so the
    ``download_data_table`` surface is unchanged.
    """
    truncated: list = [[[]]]  # same shape as drift test above

    with pytest.raises(ArtifactParseError):
        _parse_data_table(truncated)


def test_parse_data_table_raises_on_empty_rows() -> None:
    """Genuinely-empty rows array still raises with the existing message."""
    raw_data = [[[[[0, 100, None, None, [6, 7, []]]]]]]

    with pytest.raises(ArtifactParseError, match="Empty data table"):
        _parse_data_table(raw_data)


def test_parse_data_table_wrapper_honors_artifacts_private_helper_seams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The _artifacts wrapper keeps sibling helper monkeypatch seams intact."""
    rows_payload = [
        [0, 5, ["header_cell"]],
        [5, 10, ["value_cell"]],
    ]

    monkeypatch.setattr(
        artifacts_module,
        "_extract_data_table_rows",
        lambda raw_data: rows_payload,
    )
    monkeypatch.setattr(
        artifacts_module,
        "_extract_cell_text",
        lambda cell: f"text:{cell}",
    )

    headers, rows = artifacts_module._parse_data_table(["patched raw data"])

    assert headers == ["text:header_cell"]
    assert rows == [["text:value_cell"]]


@pytest.mark.parametrize(
    ("app_data", "is_quiz", "patched_name", "expected_items"),
    [
        ({"quiz": [{"question": "Q"}]}, True, "_format_quiz_markdown", [{"question": "Q"}]),
        ({"flashcards": [{"f": "front"}]}, False, "_format_flashcards_markdown", [{"f": "front"}]),
    ],
)
def test_format_interactive_content_wrapper_honors_artifacts_markdown_seams(
    monkeypatch: pytest.MonkeyPatch,
    app_data: dict,
    is_quiz: bool,
    patched_name: str,
    expected_items: list[dict],
) -> None:
    """Interactive markdown wrappers keep sibling formatter monkeypatch seams intact."""
    captured: dict[str, object] = {}

    def formatter(title: str, items: list[dict]) -> str:
        captured["title"] = title
        captured["items"] = items
        return "patched markdown"

    monkeypatch.setattr(artifacts_module, patched_name, formatter)

    result = artifacts_module._format_interactive_content(
        app_data,
        "Patched Title",
        "markdown",
        "<html></html>",
        is_quiz,
    )

    assert result == "patched markdown"
    assert captured == {"title": "Patched Title", "items": expected_items}


def test_parse_data_table_happy_path() -> None:
    """End-to-end sanity: helper + parser still produce the expected CSV shape."""
    rows_payload = [
        [
            0,
            20,
            [
                [0, 5, [[0, 5, [[0, 5, [["Col1"]]]]]]],
                [5, 10, [[5, 10, [[5, 10, [["Col2"]]]]]]],
            ],
        ],
        [
            20,
            40,
            [
                [20, 25, [[20, 25, [[20, 25, [["A"]]]]]]],
                [25, 30, [[25, 30, [[25, 30, [["B"]]]]]]],
            ],
        ],
    ]
    raw_data = [[[[[0, 100, None, None, [6, 7, rows_payload]]]]]]

    headers, rows = _parse_data_table(raw_data)

    assert headers == ["Col1", "Col2"]
    assert rows == [["A", "B"]]
