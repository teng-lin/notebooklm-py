"""Unit tests for source HTML-to-Markdown conversion."""

from __future__ import annotations

import pytest

pytest.importorskip("markdownify")

from markdownify import MarkdownConverter  # noqa: E402

from notebooklm._source.markdown import (  # noqa: E402
    _repair_mangled_math,
    _SourceHtmlConverter,
    _SourceMarkdownConverter,
    html_to_markdown,
)


def test_html_table_cell_break_is_preserved() -> None:
    output = html_to_markdown("<table><tr><td>Column 1 <br> Line 2</td><td>Value</td></tr></table>")

    assert "Column 1 <br> Line 2" in output
    row = next(line for line in output.splitlines() if "Column 1" in line)
    assert row.count("|") == 3


def test_markdown_source_break_is_preserved_inside_pipe_table() -> None:
    output = html_to_markdown(
        "<p>| A | B |<br>| --- | --- |<br>| x <br> y | z |</p>",
        source_type=8,
    )

    assert output == "| A | B |<br>| --- | --- |<br>| x <br> y | z |"


def test_break_outside_table_keeps_normal_html_behavior() -> None:
    output = html_to_markdown("<p>a<br>b</p>")

    assert "<br>" not in output
    assert "a" in output and "b" in output


def test_inline_latex_is_not_escaped() -> None:
    output = html_to_markdown("<p>$LT\\alpha_1\\beta_2$</p>")

    assert output == "$LT\\alpha_1\\beta_2$"


def test_latex_emphasis_overlap_is_repaired() -> None:
    output = html_to_markdown(
        "<p><strong>(B)</strong> <strong>membrane-bound</strong> "
        "<strong>$LT\\alpha_1\\beta_2</strong>$</p>"
    )

    assert output == "**(B)** **membrane-bound** $LT\\alpha_1\\beta_2$"


def test_simple_math_emphasis_overlap_is_repaired() -> None:
    output = html_to_markdown("<p><strong>$x = y</strong>$</p>")

    assert output == "$x = y$"


def test_italic_math_emphasis_overlap_is_repaired() -> None:
    output = html_to_markdown("<p><em>$x_1</em>$</p>")

    assert output == "$x_1$"


def test_math_asterisks_without_emphasis_are_preserved() -> None:
    output = html_to_markdown("<p>$a**b$</p>")

    assert output == "$a**b$"


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        ("<p>prices $5 and $10</p>", "prices $5 and $10"),
        ("<p>bold price <strong>$5</strong></p>", "bold price **$5**"),
        ("<p>display $$E=mc^2$$</p>", "display $$E=mc^2$$"),
        ("<p>$x+\\$y_1$</p>", "$x+\\$y_1$"),
        ("<p>$file\\_name$</p>", "$file\\_name$"),
        (
            "<p>We have $5 and we need$10*2 dollars</p>",
            "We have $5 and we need$10\\*2 dollars",
        ),
    ],
)
def test_non_target_dollar_text_is_preserved(html: str, expected: str) -> None:
    output = html_to_markdown(html)

    assert output == expected


# ---------------------------------------------------------------------------
# Escaping and math-repair branches
# ---------------------------------------------------------------------------


def test_html_escape_of_empty_text_short_circuits() -> None:
    converter = _SourceHtmlConverter(heading_style="ATX")

    assert converter.escape("") == ""
    assert converter.escape(None) == ""


def test_markdown_source_escape_never_re_escapes() -> None:
    """An imported Markdown rendition is already Markdown; escaping would double it."""
    converter = _SourceMarkdownConverter(heading_style="ATX")

    assert converter.escape("a_b *c*") == "a_b *c*"
    assert converter.escape("") == ""


def test_html_escape_falls_back_to_the_single_argument_signature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Older markdownify releases take ``escape(text)`` with no ``parent_tags``."""
    seen: list[tuple] = []

    def _one_argument_escape(self, text, *extra):  # noqa: ANN001, ANN202
        seen.append(extra)
        if extra:
            raise TypeError("escape() takes 2 positional arguments but 3 were given")
        return text

    monkeypatch.setattr(MarkdownConverter, "escape", _one_argument_escape, raising=True)
    converter = _SourceHtmlConverter(heading_style="ATX")

    assert converter.escape("plain") == "plain"
    # First the two-argument call, then the retry without ``parent_tags``.
    assert seen == [(None,), ()]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param("$x + 1$", "$x + 1$", id="no-surrounding-emphasis"),
        pytest.param("*$plain$*", "*$plain$*", id="no-math-signal-in-body"),
        pytest.param(r"*$a\_b$*", "*$a_b$*", id="emphasis-on-both-sides"),
        pytest.param(r"*$a\_b*$", "$a_b$", id="lead-only-marker-trapped-in-body"),
        pytest.param(r"$*a\_b$*", "$a_b$", id="trail-only-marker-trapped-in-body"),
        pytest.param("**$x^2$**", "**$x^2$**", id="double-emphasis-preserved"),
        pytest.param("costs $5 and $10", "costs $5 and $10", id="currency-untouched"),
    ],
)
def test_mangled_math_repair_is_narrow(text: str, expected: str) -> None:
    assert _repair_mangled_math(text) == expected


def test_math_spans_survive_html_escaping() -> None:
    output = html_to_markdown(r"<p>Let $a\_b$ be given.</p>")

    assert r"$a\_b$" in output


def test_a_dollar_span_without_math_signal_is_escaped_like_prose() -> None:
    output = html_to_markdown("<p>Between $a$ and $b$ there is a_gap.</p>")

    assert r"a\_gap" in output
