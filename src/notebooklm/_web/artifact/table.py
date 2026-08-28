"""Web data-table row parsing helpers."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from ...exceptions import UnknownRPCMethodError
from ...rpc import RPCMethod, safe_index
from ...types import ArtifactParseError

logger = logging.getLogger("notebooklm._artifacts")


def _extract_cell_text(cell: Any) -> str:
    """Recursively extract text from a nested cell structure.

    Data table cells have deeply nested arrays with position markers (integers)
    and text content (strings). This function traverses the structure and
    concatenates all text fragments found.
    """
    if isinstance(cell, str):
        return cell
    if isinstance(cell, int):
        return ""
    if isinstance(cell, list):
        return "".join(text for item in cell if (text := _extract_cell_text(item)))
    return ""


def _extract_data_table_rows(raw_data: Any) -> list[Any]:
    """Extract data-table rows from the LIST_ARTIFACTS (gArtLc) response shape.

    Navigates the rich-text wrapper at ``raw_data[0][0][0][0][4][2]`` to reach
    the rows array. The first four ``[0]`` hops are wrapper layers; ``[4]`` is
    the table content section ``[type, flags, rows_array]``, and ``[2]`` is
    the rows array itself.

    Inner-most access goes through :func:`safe_index`, which enforces
    strict decoding: a shape drift raises ``UnknownRPCMethodError`` so we
    fail fast. A path that descends successfully to a non-list scalar is
    normalised to ``[]`` here.

    Returns:
        The rows array on success, or ``[]`` when the inner value descends
        to a non-list scalar. Shape drift raises ``UnknownRPCMethodError``
        from :func:`safe_index`.
    """
    rows_array = safe_index(
        raw_data,
        0,
        0,
        0,
        0,
        4,
        2,
        method_id=RPCMethod.LIST_ARTIFACTS.value,
        source="_artifacts._extract_data_table_rows",
    )
    if not isinstance(rows_array, list):
        # The upstream shape is occasionally seen as a non-list scalar even
        # when descent succeeds — normalise it to the empty-list sentinel so
        # the caller's "empty data table" path handles it uniformly.
        logger.warning(
            "data table rows_array is not a list (type=%s); treating as empty",
            type(rows_array).__name__,
        )
        return []
    return rows_array


def _parse_data_table(
    raw_data: list,
    rows_extractor: Callable[[Any], list[Any]] | None = None,
    cell_text_extractor: Callable[[Any], str] | None = None,
) -> tuple[list[str], list[list[str]]]:
    """Parse rich-text data table into headers and rows.

    Data tables from NotebookLM have a complex nested structure with position
    markers. This function delegates inner-most navigation to
    :func:`_extract_data_table_rows` and then extracts text from each cell.

    Each row has format: ``[start_pos, end_pos, [cell_array]]``.
    Each cell is deeply nested: ``[pos, pos, [[pos, pos, [[pos, pos, [["text"]]]]]]]``.

    Returns:
        Tuple of (headers, rows) where headers is a list of column names
        and rows is a list of row data (each row is a list of cell strings).

    Raises:
        ArtifactParseError: If the data structure cannot be parsed or is empty.
    """
    try:
        if rows_extractor is None:
            rows_extractor = _extract_data_table_rows
        if cell_text_extractor is None:
            cell_text_extractor = _extract_cell_text

        rows_array = rows_extractor(raw_data)
        if not rows_array:
            # Genuinely-empty data table.
            raise ArtifactParseError("data_table", details="Empty data table")

        headers: list[str] = []
        rows: list[list[str]] = []

        for i, row_section in enumerate(rows_array):
            # Each row_section is [start_pos, end_pos, cell_array]
            if not isinstance(row_section, list) or len(row_section) < 3:
                continue

            # The ``len(row_section) < 3`` guard above guarantees the cell-array
            # slot is present, so the ``safe_index`` seam is a no-op here; it
            # keeps the position knowledge off a raw ``row_section[2]`` subscript
            # (and any genuine drift it surfaced would be caught by the enclosing
            # ``except`` and re-raised as ``ArtifactParseError``, as today).
            cell_array = safe_index(
                row_section,
                2,
                method_id=RPCMethod.LIST_ARTIFACTS.value,
                source="_artifacts._parse_data_table",
            )
            if not isinstance(cell_array, list):
                continue

            row_values = [cell_text_extractor(cell) for cell in cell_array]

            if i == 0:
                headers = row_values
            else:
                rows.append(row_values)

        # Validate we extracted usable data
        if not headers:
            raise ArtifactParseError(
                "data_table",
                details="Failed to extract headers from data table",
            )

        return headers, rows

    except (IndexError, TypeError, KeyError, UnknownRPCMethodError) as e:
        # ``_extract_data_table_rows`` raises ``UnknownRPCMethodError`` on
        # shape drift under strict decoding; convert it (and the raw
        # index/type/key errors from cell walking) into the domain-level
        # ``ArtifactParseError`` so the ``download_data_table`` surface stays
        # unchanged.
        raise ArtifactParseError(
            "data_table",
            details=f"Failed to parse data table structure: {e}",
            cause=e,
        ) from e


__all__ = ["_extract_cell_text", "_extract_data_table_rows", "_parse_data_table"]
