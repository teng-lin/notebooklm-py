"""Unit tests for module-level helpers in ``notebooklm._artifact.formatters``.

Focuses on ``_extract_data_table_rows`` — the named extractor that replaces
the raw ``raw_data[0][0][0][0][4][2]`` deep-index chain in
:func:`_parse_data_table`. Strict decoding is the only mode (the
``NOTEBOOKLM_STRICT_DECODE=0`` soft-mode opt-out was retired in v0.7.0): shape
drift raises ``UnknownRPCMethodError`` while a non-list inner value at a valid
path normalises to ``[]``. ``_parse_data_table`` converts the drift exception
into ``ArtifactParseError`` so the download surface stays stable.
"""

from __future__ import annotations

import logging

import pytest

from notebooklm._artifact.formatters import (
    _extract_data_table_rows,
    _parse_data_table,
)
from notebooklm.exceptions import UnknownRPCMethodError
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
# _extract_data_table_rows — drift shapes (raise under strict decoding)
# ---------------------------------------------------------------------------


def test_extract_data_table_rows_missing_inner_list() -> None:
    """Inner ``[4]`` slot exists but lacks the ``[2]`` rows entry — drift raises."""
    # The table-content section ([type, flags, rows_array]) is truncated to
    # ``[type, flags]`` — descending to index 2 fails and safe_index raises.
    raw_data = [[[[[0, 100, None, None, [6, 7]]]]]]

    with pytest.raises(UnknownRPCMethodError):
        _extract_data_table_rows(raw_data)


def test_extract_data_table_rows_wrong_type_at_one_level() -> None:
    """One of the wrapper hops is a string, not a list — drift raises."""
    # Replace the third wrapper layer with a non-indexable string.
    raw_data = [[[["not-a-list", [[0, 100, None, None, [6, 7, []]]]]]]]

    with pytest.raises(UnknownRPCMethodError):
        _extract_data_table_rows(raw_data)


def test_extract_data_table_rows_truncated_structure() -> None:
    """Outer wrapper is only 3 levels deep — descent stops well before [4][2]."""
    raw_data: list = [[[]]]

    with pytest.raises(UnknownRPCMethodError):
        _extract_data_table_rows(raw_data)


# ---------------------------------------------------------------------------
# _extract_data_table_rows — extra coverage for non-list inner value
# ---------------------------------------------------------------------------


def test_extract_data_table_rows_non_list_inner_value(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Inner ``[2]`` is a scalar (e.g. None) at a valid path. Returns ``[]``.

    The descent succeeds to a non-list scalar, so safe_index does NOT raise;
    this guards the ``isinstance(rows_array, list)`` normalisation branch in
    the helper, which keeps the caller's "empty data table" path uniform.
    """
    raw_data = [[[[[0, 100, None, None, [6, 7, None]]]]]]

    with caplog.at_level(logging.WARNING):
        result = _extract_data_table_rows(raw_data)

    assert result == []


# ---------------------------------------------------------------------------
# _parse_data_table — converts drift into ArtifactParseError (regression)
# ---------------------------------------------------------------------------


def test_parse_data_table_raises_artifact_parse_error_on_drift() -> None:
    """``_parse_data_table`` converts shape drift into :class:`ArtifactParseError`.

    ``_extract_data_table_rows`` raises ``UnknownRPCMethodError`` on drift
    under strict decoding; ``_parse_data_table`` catches it and re-raises as
    ``ArtifactParseError`` so the ``download_data_table`` surface is unchanged.
    """
    truncated: list = [[[]]]  # same shape as drift test above

    with pytest.raises(ArtifactParseError):
        _parse_data_table(truncated)


def test_parse_data_table_raises_on_empty_rows() -> None:
    """Genuinely-empty rows array still raises with the existing message."""
    raw_data = [[[[[0, 100, None, None, [6, 7, []]]]]]]

    with pytest.raises(ArtifactParseError, match="Empty data table"):
        _parse_data_table(raw_data)


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


def test_parse_data_table_skips_malformed_row_sections() -> None:
    """Per-row malformed sections are skipped, not fatal: a non-list row, a
    short (<3) row, and a row whose cell-array slot is a non-list are all
    dropped while the valid header + data rows parse. Pins the soft per-row
    skip contract preserved across the #1491 ``safe_index`` migration of the
    ``row_section[2]`` read (the ``len(row_section) < 3`` guard keeps it a
    no-op; a present non-list ``cell_array`` is also skipped)."""
    rows_array = [
        [0, 1, ["H1", "H2"]],  # header (valid)
        "not-a-list",  # non-list row_section -> skip
        [0, 1],  # short (<3) row_section -> skip
        [0, 1, "non-list-cell-array"],  # cell_array not a list -> skip
        [0, 1, ["v1", "v2"]],  # data row (valid)
    ]
    # rows_array lives at raw_data[0][0][0][0][4][2] (mirrors the happy-path test).
    raw_data = [[[[[0, 100, None, None, [6, 7, rows_array]]]]]]

    headers, rows = _parse_data_table(raw_data, cell_text_extractor=lambda cell: cell)

    assert headers == ["H1", "H2"]
    assert rows == [["v1", "v2"]]
