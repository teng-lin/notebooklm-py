"""Private artifact formatting helpers."""

from __future__ import annotations

import html
import json
import logging
import re
from collections.abc import Callable

from ..types import ArtifactParseError

__all__ = [
    "_extract_app_data",
    "_format_flashcards_markdown",
    "_format_interactive_content",
    "_format_quiz_markdown",
]

# Use the ``notebooklm._artifacts`` logger (not this module's) so existing log
# filters keep matching these helper diagnostics.
logger = logging.getLogger("notebooklm._artifacts")


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


def _format_interactive_content(
    app_data: dict,
    title: str,
    output_format: str,
    html_content: str,
    is_quiz: bool,
    quiz_markdown_formatter: Callable[[str, list[dict]], str] | None = None,
    flashcards_markdown_formatter: Callable[[str, list[dict]], str] | None = None,
) -> str:
    """Format quiz or flashcard content for output.

    Args:
        app_data: Parsed data from HTML.
        title: Artifact title.
        output_format: Output format - json, markdown, or html.
        html_content: Original HTML content.
        is_quiz: True for quiz, False for flashcards.
        quiz_markdown_formatter: Optional formatter used by compatibility wrappers.
        flashcards_markdown_formatter: Optional formatter used by compatibility wrappers.

    Returns:
        Formatted content string.
    """
    if output_format == "html":
        return html_content

    if is_quiz:
        questions = app_data.get("quiz", [])
        if output_format == "markdown":
            if quiz_markdown_formatter is None:
                quiz_markdown_formatter = _format_quiz_markdown
            return quiz_markdown_formatter(title, questions)
        return json.dumps({"title": title, "questions": questions}, indent=2)

    cards = app_data.get("flashcards", [])
    if output_format == "markdown":
        if flashcards_markdown_formatter is None:
            flashcards_markdown_formatter = _format_flashcards_markdown
        return flashcards_markdown_formatter(title, cards)
    normalized = [{"front": c.get("f", ""), "back": c.get("b", "")} for c in cards]
    return json.dumps({"title": title, "cards": normalized}, indent=2)
