"""Unit tests for source HTML-to-Markdown conversion."""

from __future__ import annotations

import pytest

pytest.importorskip("markdownify")

from notebooklm._source.markdown import html_to_markdown  # noqa: E402


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
