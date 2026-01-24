"""Artifact parsing and formatting helper functions.

Pure functions for parsing artifact data structures and formatting output.
These helpers are used by ArtifactDownloader for processing quiz, flashcard,
and data table artifacts.
"""

import html
import json
import re
from typing import Any

from .types import ArtifactParseError


def _extract_app_data(html_content: str) -> dict:
    """Extract JSON from data-app-data HTML attribute.

    The quiz/flashcard HTML embeds JSON in a data-app-data attribute
    with HTML-encoded content (e.g., &quot; for quotes).
    """
    match = re.search(r'data-app-data="([^"]+)"', html_content)
    if not match:
        raise ArtifactParseError(
            "quiz/flashcard",
            details="No data-app-data attribute found in HTML",
        )

    encoded_json = match.group(1)
    decoded_json = html.unescape(encoded_json)
    return json.loads(decoded_json)


def _format_quiz_markdown(title: str, questions: list[dict]) -> str:
    """Format quiz as markdown."""
    lines = [f"# {title}", ""]
    for i, q in enumerate(questions, 1):
        lines.append(f"## Question {i}")
        lines.append(q.get("question", ""))
        lines.append("")
        for opt in q.get("answerOptions", []):
            marker = "[x]" if opt.get("isCorrect") else "[ ]"
            lines.append(f"- {marker} {opt.get('text', '')}")
        if q.get("hint"):
            lines.append("")
            lines.append(f"**Hint:** {q['hint']}")
        lines.append("")
    return "\n".join(lines)


def _format_flashcards_markdown(title: str, cards: list[dict]) -> str:
    """Format flashcards as markdown."""
    lines = [f"# {title}", ""]
    for i, card in enumerate(cards, 1):
        front = card.get("f", "")
        back = card.get("b", "")
        lines.extend(
            [
                f"## Card {i}",
                "",
                f"**Q:** {front}",
                "",
                f"**A:** {back}",
                "",
                "---",
                "",
            ]
        )
    return "\n".join(lines)


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


def _parse_data_table(raw_data: list) -> tuple[list[str], list[list[str]]]:
    """Parse rich-text data table into headers and rows.

    Data tables from NotebookLM have a complex nested structure with position
    markers. This function navigates to the rows array and extracts text from
    each cell.

    Structure: raw_data[0][0][0][0][4][2] contains the rows array where:
    - [0][0][0][0] navigates through wrapper layers
    - [4] contains the table content section [type, flags, rows_array]
    - [2] is the actual rows array

    Each row has format: [start_pos, end_pos, [cell_array]]
    Each cell is deeply nested: [pos, pos, [[pos, pos, [[pos, pos, [["text"]]]]]]]

    Returns:
        Tuple of (headers, rows) where headers is a list of column names
        and rows is a list of row data (each row is a list of cell strings).

    Raises:
        ArtifactParseError: If the data structure cannot be parsed or is empty.
    """
    try:
        # Navigate through nested wrappers to reach the rows array
        rows_array = raw_data[0][0][0][0][4][2]
        if not rows_array:
            raise ArtifactParseError("data_table", details="Empty data table")

        headers: list[str] = []
        rows: list[list[str]] = []

        for i, row_section in enumerate(rows_array):
            # Each row_section is [start_pos, end_pos, cell_array]
            if not isinstance(row_section, list) or len(row_section) < 3:
                continue

            cell_array = row_section[2]
            if not isinstance(cell_array, list):
                continue

            row_values = [_extract_cell_text(cell) for cell in cell_array]

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

    except (IndexError, TypeError, KeyError) as e:
        raise ArtifactParseError(
            "data_table",
            details=f"Failed to parse data table structure: {e}",
            cause=e,
        ) from e
