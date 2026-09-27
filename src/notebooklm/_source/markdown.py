"""HTML-to-Markdown conversion for source fulltext."""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

from markdownify import MarkdownConverter

# Keep inline and display math out of markdownify's normal escaping. The
# whitespace guards distinguish math from common currency such as "$5 and
# $10". An escaped dollar is ordinary source text, not a math delimiter.
_MATH_START = re.compile(r"(?<!\\)\$(?!\s)")
_MATH_END = {width: re.compile(r"(?<![\\\s])" + r"\$" * width) for width in (1, 2)}


def _math_spans(text: str) -> Iterator[tuple[int, int, int]]:
    """Yield (start, end, delimiter width) without rescanning unmatched suffixes.

    Prefer display delimiters, as before. Cache each delimiter's next closing
    position so many unmatched dollar signs cannot cause quadratic backtracking.
    """
    missing = len(text) + 1
    closing = {1: -1, 2: -1}
    consumed = 0
    for opening in _MATH_START.finditer(text):
        start = opening.start()
        if start < consumed:
            continue
        for width in (2, 1):
            body_start = start + width
            if width == 2 and (
                not text.startswith("$$", start)
                or (body_start < len(text) and text[body_start].isspace())
            ):
                continue
            if closing[width] < body_start:
                match = _MATH_END[width].search(text, body_start)
                closing[width] = match.start() if match else missing
            if closing[width] != missing:
                consumed = closing[width] + width
                yield start, consumed, width
                break


# NotebookLM can put Markdown emphasis tags across a math span. Once
# markdownify has converted those tags, the resulting delimiters can straddle
# the closing dollar sign. This repair is deliberately narrow and only runs
# for spans containing a LaTeX escape or crossed emphasis delimiters.
_MANGLED_MATH = re.compile(
    r"(?P<lead>(?:(?:\\?[*_]){1,2})?)"
    r"(?<!\$)\$(?!\$)"
    r"(?P<body>[^\n$]+?)"
    r"(?<!\$)\$(?!\$)"
    r"(?P<trail>(?:(?:\\?[*_]){1,2})?)"
)
_EMPHASIS_RUN = re.compile(r"(?:(?:\\?[*_]){1,2})")

# Wire type code used by NotebookLM for imported Markdown sources.
_MARKDOWN_SOURCE_TYPE_CODE = 8


def _has_math_signal(body: str) -> bool:
    """Whether a dollar span contains LaTeX or Markdown-sensitive math."""
    return "\\" in body or any(char in body for char in "_*^{}")


class _SourceMarkdownConverter(MarkdownConverter):
    """Convert NotebookLM Markdown-source renditions without re-escaping them."""

    def escape(self, text: str, parent_tags: Any = None) -> str:
        return text or ""

    def convert_br(self, el: Any, text: str, parent_tags: Any) -> str:
        return "<br>"


class _SourceHtmlConverter(MarkdownConverter):
    """Convert HTML sources while preserving math and table-cell breaks."""

    def escape(self, text: str, parent_tags: Any = None) -> str:
        if not text:
            return ""

        parts: list[str] = []
        end = 0
        for start, stop, width in _math_spans(text):
            parts.append(self._escape_plain(text[end:start], parent_tags))
            if _has_math_signal(text[start + width : stop - width]):
                parts.append(text[start:stop])
            else:
                parts.append(self._escape_plain(text[start:stop], parent_tags))
            end = stop
        parts.append(self._escape_plain(text[end:], parent_tags))
        return "".join(parts)

    def _escape_plain(self, text: str, parent_tags: Any) -> str:
        try:
            return super().escape(text, parent_tags)  # type: ignore[misc]
        except TypeError:
            return super().escape(text)  # type: ignore[misc]

    def convert_br(self, el: Any, text: str, parent_tags: Any) -> str:
        in_table_cell = (
            isinstance(parent_tags, (set, frozenset, list, tuple))
            and ("td" in parent_tags or "th" in parent_tags)
        ) or getattr(el, "find_parent", lambda *_args: None)(("td", "th")) is not None
        if in_table_cell:
            return "<br>"
        return super().convert_br(el, text, parent_tags)  # type: ignore[misc]


def _repair_mangled_math(text: str) -> str:
    """Repair emphasis delimiters that cross an inline LaTeX span."""

    def fix(match: re.Match[str]) -> str:
        body = match.group("body")
        lead, trail = match.group("lead"), match.group("trail")
        if not (lead or trail):
            return match.group(0)
        if not (_has_math_signal(body) or _EMPHASIS_RUN.search(body)):
            return match.group(0)

        body = body.replace("\\_", "_").replace("\\*", "*")
        marker = lead.replace("\\", "")
        if not trail and body.endswith(marker):
            body = body[: -len(marker)]
        elif not lead and body.startswith(trail.replace("\\", "")):
            body = body[len(trail.replace("\\", "")) :]
        if lead and trail:
            return f"{lead}${body}${trail}"
        return f"${body}$"

    return _MANGLED_MATH.sub(fix, text)


def html_to_markdown(html: str, *, source_type: int | None = None) -> str:
    """Convert a source HTML rendition to Markdown.

    Imported Markdown sources contain a hybrid HTML/Markdown rendition, so
    their text must not be escaped again. Other source types use normal HTML
    escaping with math spans protected from Markdown escaping.
    """
    converter: MarkdownConverter
    if source_type == _MARKDOWN_SOURCE_TYPE_CODE:
        converter = _SourceMarkdownConverter(heading_style="ATX")
    else:
        converter = _SourceHtmlConverter(heading_style="ATX")

    return _repair_mangled_math(converter.convert(html))


__all__ = ["html_to_markdown"]
