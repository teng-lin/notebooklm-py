"""Edge-case coverage for the shared Android ``TailwindDoc`` codec.

``tests/unit/android/test_chat.py`` pins the happy-path decode of a fully
populated document. These cases cover the rejection/clamping branches and the
Markdown/plain-text renderers, which the transport-level suites never reach.
"""

from __future__ import annotations

import pytest

from notebooklm._android.codecs.documents import (
    decode_blocks,
    decode_document,
    structural_element_markdown,
    structural_elements_plain_text,
    tailwind_doc_markdown,
    tailwind_doc_plain_text,
)
from notebooklm._android.proto.google.internal.labs.tailwind.orchestration.v1 import (
    chat_pb2,
)
from notebooklm._types.documents import BlockKind, BlockStyle, ListStyle


def _paragraph(
    text: str,
    *,
    start: int = 0,
    end: int | None = None,
    style: chat_pb2.TextStyle | None = None,
    named_style: int | None = None,
    bullet: chat_pb2.BulletInfo | None = None,
) -> chat_pb2.StructuralElement:
    """Build a single-run paragraph element spanning ``text``."""
    end = start + len(text) if end is None else end
    run = chat_pb2.TextRun(content=text)
    if style is not None:
        run.text_style.CopyFrom(style)
    paragraph = chat_pb2.Paragraph(
        elements=[chat_pb2.ParagraphElement(start_index=start, end_index=end, text_run=run)]
    )
    if named_style is not None:
        paragraph.paragraph_style.named_style_type = named_style
    if bullet is not None:
        paragraph.bullet_info.CopyFrom(bullet)
    return chat_pb2.StructuralElement(start_index=start, end_index=end, paragraph=paragraph)


def _doc(*elements: chat_pb2.StructuralElement) -> chat_pb2.TailwindDoc:
    return chat_pb2.TailwindDoc(body=chat_pb2.Body(content=list(elements)))


# --------------------------------------------------------------------------
# _bounded_range rejection / clamping (via decode_blocks)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("start", "end"),
    [
        pytest.param(-1, 4, id="negative-start"),
        pytest.param(6, 2, id="end-before-start"),
    ],
)
def test_decode_blocks_drops_structurally_invalid_ranges(start: int, end: int) -> None:
    element = chat_pb2.StructuralElement(
        start_index=start,
        end_index=end,
        paragraph=chat_pb2.Paragraph(),
    )

    assert decode_blocks([element]) == ()


def test_decode_blocks_sorts_by_declared_offsets() -> None:
    blocks = decode_blocks(
        [
            _paragraph("second", start=10, end=16),
            _paragraph("first", start=0, end=5),
        ]
    )

    assert [(block.start_index, block.end_index) for block in blocks] == [(0, 5), (10, 16)]


def test_paragraph_spans_skip_non_text_and_inverted_elements() -> None:
    paragraph = chat_pb2.Paragraph(
        elements=[
            # end < start -> rejected before the text_run check.
            chat_pb2.ParagraphElement(
                start_index=5, end_index=1, text_run=chat_pb2.TextRun(content="bad")
            ),
            # No text_run -> not a span.
            chat_pb2.ParagraphElement(
                start_index=0, end_index=3, image=chat_pb2.Image(url="https://x.invalid/i")
            ),
            # No text_style -> the style-less span defaults.
            chat_pb2.ParagraphElement(
                start_index=3, end_index=7, text_run=chat_pb2.TextRun(content="ok!!")
            ),
        ]
    )
    element = chat_pb2.StructuralElement(start_index=0, end_index=7, paragraph=paragraph)

    [block] = decode_blocks([element])

    [span] = block.spans
    assert (span.start_index, span.end_index, span.text) == (3, 7, "ok!!")
    assert (span.bold, span.italic, span.underline, span.url) == (False, False, False, None)


def test_paragraph_style_and_list_type_out_of_range_fall_back_to_unspecified() -> None:
    element = _paragraph(
        "x",
        named_style=99,
        bullet=chat_pb2.BulletInfo(list_type=99, nesting_level=1, glyph="*", ordinal=0),
    )

    [block] = decode_blocks([element])

    assert block.style is BlockStyle.UNSPECIFIED
    assert block.list_info is not None
    assert block.list_info.style is ListStyle.UNSPECIFIED
    # ordinal 0 is "unset" on the wire and normalises to ``None``.
    assert block.list_info.ordinal is None


def test_unknown_structural_variant_decodes_as_unknown_kind() -> None:
    [block] = decode_blocks([chat_pb2.StructuralElement(start_index=0, end_index=1)])

    assert block.kind is BlockKind.UNKNOWN
    assert block.spans == ()


# --------------------------------------------------------------------------
# Table decoding: clamping, empty cells, nested content
# --------------------------------------------------------------------------


def test_decode_table_clamps_cells_and_keeps_nested_spans() -> None:
    table = chat_pb2.Table(
        table_rows=[
            # Row entirely outside the block bounds -> dropped.
            chat_pb2.TableRow(start_index=100, end_index=120),
            chat_pb2.TableRow(
                start_index=0,
                end_index=20,
                table_cells=[
                    # Zero-width cell: recorded, but its content is not descended.
                    chat_pb2.TableCell(
                        start_index=0,
                        end_index=0,
                        content=[_paragraph("ignored", start=0, end=0)],
                    ),
                    # Cell whose declared end runs past the row -> clamped to 20.
                    chat_pb2.TableCell(
                        start_index=2,
                        end_index=999,
                        content=[_paragraph("cell", start=2, end=6)],
                    ),
                    # Inverted cell -> dropped.
                    chat_pb2.TableCell(start_index=9, end_index=4),
                ],
            ),
        ]
    )
    element = chat_pb2.StructuralElement(start_index=0, end_index=20, table=table)

    [block] = decode_blocks([element])

    assert block.kind is BlockKind.TABLE
    [row] = block.table_rows
    assert [(cell.start_index, cell.end_index) for cell in row] == [(0, 0), (2, 20)]
    assert [span.text for span in block.spans] == ["cell"]


def test_decode_table_skips_cell_children_that_fail_range_admission() -> None:
    """A child whose declared range escapes its cell contributes no spans."""
    table = chat_pb2.Table(
        table_rows=[
            chat_pb2.TableRow(
                start_index=0,
                end_index=10,
                table_cells=[
                    chat_pb2.TableCell(
                        start_index=0,
                        end_index=10,
                        content=[
                            # Inverted child range -> ``_decode_block`` returns None.
                            _paragraph("dropped", start=9, end=2),
                            _paragraph("kept", start=0, end=4),
                        ],
                    )
                ],
            )
        ]
    )
    element = chat_pb2.StructuralElement(start_index=0, end_index=10, table=table)

    [block] = decode_blocks([element])

    assert [span.text for span in block.spans] == ["kept"]


def test_decode_table_drops_rows_whose_cells_are_all_empty() -> None:
    table = chat_pb2.Table(
        table_rows=[
            chat_pb2.TableRow(
                start_index=0,
                end_index=10,
                table_cells=[chat_pb2.TableCell(start_index=0, end_index=0)],
            )
        ]
    )
    element = chat_pb2.StructuralElement(start_index=0, end_index=10, table=table)

    [block] = decode_blocks([element])

    assert block.table_rows == ()


def _nested_tables(depth: int) -> chat_pb2.StructuralElement:
    """Build ``depth`` table-in-cell levels around a ``deep`` paragraph.

    Built by in-place mutation: the ``chat_pb2.X(...)`` constructors round-trip
    through the wire format and would trip protobuf's own 100-level recursion
    limit long before the codec's structural cap.
    """
    root = chat_pb2.StructuralElement(start_index=0, end_index=4)
    element = root
    for _ in range(depth):
        row = element.table.table_rows.add(start_index=0, end_index=4)
        cell = row.table_cells.add(start_index=0, end_index=4)
        element = cell.content.add(start_index=0, end_index=4)
    run = element.paragraph.elements.add(start_index=0, end_index=4)
    run.text_run.content = "deep"
    return root


def test_decode_table_descends_nesting_below_the_structural_depth_cap() -> None:
    [block] = decode_blocks([_nested_tables(3)])

    assert [span.text for span in block.spans] == ["deep"]


def test_decode_table_stops_descending_past_the_structural_depth_cap() -> None:
    """Beyond 64 levels the codec stops descending instead of recursing."""
    [block] = decode_blocks([_nested_tables(70)])

    assert block.spans == ()


# --------------------------------------------------------------------------
# decode_document: body guard + annotation admission
# --------------------------------------------------------------------------


def test_decode_document_without_body_is_empty() -> None:
    decoded = decode_document(chat_pb2.TailwindDoc())

    assert decoded.blocks == ()
    assert decoded.annotations == ()


def test_decode_document_admits_only_well_formed_annotations() -> None:
    body = chat_pb2.Body(content=[_paragraph("hello")])
    entries = body.inline_object_locations
    # No object_id at all.
    entries.add().content_range.end_index = 4
    # Empty object id.
    empty_id = entries.add()
    empty_id.object_id.id = ""
    empty_id.content_range.end_index = 4
    # No content_range.
    entries.add().object_id.id = "obj-no-range"
    # Inverted range.
    inverted = entries.add()
    inverted.object_id.id = "obj-inverted"
    inverted.content_range.start_index = 9
    inverted.content_range.end_index = 2
    # Admitted.
    good = entries.add()
    good.object_id.id = "obj-good"
    good.content_range.start_index = 1
    good.content_range.end_index = 4

    decoded = decode_document(chat_pb2.TailwindDoc(body=body))

    assert [(a.object_id, a.start_index, a.end_index) for a in decoded.annotations] == [
        ("obj-good", 1, 4)
    ]


# --------------------------------------------------------------------------
# Plain-text rendering
# --------------------------------------------------------------------------


def test_tailwind_doc_plain_text_without_body_is_empty() -> None:
    assert tailwind_doc_plain_text(chat_pb2.TailwindDoc()) == ""


def test_plain_text_renders_every_text_bearing_variant_in_wire_order() -> None:
    document = _doc(
        _paragraph("para"),
        chat_pb2.StructuralElement(
            table=chat_pb2.Table(
                table_rows=[
                    chat_pb2.TableRow(
                        table_cells=[
                            chat_pb2.TableCell(content=[_paragraph("cell")]),
                        ]
                    )
                ]
            )
        ),
        chat_pb2.StructuralElement(code_block=chat_pb2.CodeBlock(content="code()")),
        chat_pb2.StructuralElement(a2ui_block=chat_pb2.A2uiBlock(json='{"a":1}')),
        chat_pb2.StructuralElement(thought=chat_pb2.Thought(elements=[_paragraph("thinking")])),
        # Metadata-only leaves contribute nothing.
        chat_pb2.StructuralElement(image=chat_pb2.Image(url="https://x.invalid/i")),
        chat_pb2.StructuralElement(horizontal_rule=chat_pb2.HorizontalRule()),
    )

    assert tailwind_doc_plain_text(document) == 'para\ncell\ncode()\n{"a":1}\nthinking'


def test_plain_text_skips_empty_leaf_payloads() -> None:
    document = _doc(
        chat_pb2.StructuralElement(code_block=chat_pb2.CodeBlock(content="")),
        chat_pb2.StructuralElement(a2ui_block=chat_pb2.A2uiBlock(json="")),
        _paragraph(""),
    )

    assert tailwind_doc_plain_text(document) == ""


def _nested_thoughts(depth: int) -> chat_pb2.StructuralElement:
    """Build ``depth`` thought-in-thought levels around a ``deep`` paragraph."""
    root = chat_pb2.StructuralElement()
    element = root
    for _ in range(depth):
        element = element.thought.elements.add()
    element.paragraph.elements.add().text_run.content = "deep"
    return root


def test_plain_text_descends_nesting_below_the_structural_depth_cap() -> None:
    assert structural_elements_plain_text([_nested_thoughts(3)]) == "deep"


def test_plain_text_stops_at_the_structural_depth_cap() -> None:
    assert structural_elements_plain_text([_nested_thoughts(70)]) == ""


# --------------------------------------------------------------------------
# Markdown rendering
# --------------------------------------------------------------------------


def test_tailwind_doc_markdown_without_body_is_empty() -> None:
    assert tailwind_doc_markdown(chat_pb2.TailwindDoc()) == ""


def test_inline_markdown_applies_every_admitted_text_style() -> None:
    style = chat_pb2.TextStyle(
        bold=True,
        italic=True,
        underline=True,
        code=True,
        strikethrough=True,
        math=1,
        url="https://example.invalid/a",
    )

    rendered = structural_element_markdown(_paragraph("t", style=style))

    assert rendered == "[$<u>~~***`t`***~~</u>$](https://example.invalid/a)"


def test_inline_markdown_leaves_unstyled_runs_verbatim() -> None:
    """A present-but-empty ``text_style`` adds no wrappers."""
    rendered = structural_element_markdown(_paragraph("plain", style=chat_pb2.TextStyle()))

    assert rendered == "plain"


def test_inline_markdown_renders_image_and_resource_elements() -> None:
    paragraph = chat_pb2.Paragraph(
        elements=[
            chat_pb2.ParagraphElement(image=chat_pb2.Image(url="https://x.invalid/i")),
            chat_pb2.ParagraphElement(resource=chat_pb2.Resource(id="res-1")),
            # Empty image/resource payloads are skipped entirely.
            chat_pb2.ParagraphElement(image=chat_pb2.Image()),
            chat_pb2.ParagraphElement(resource=chat_pb2.Resource(id="")),
        ]
    )
    element = chat_pb2.StructuralElement(paragraph=paragraph)

    assert structural_element_markdown(element) == "![image](https://x.invalid/i)[resource: res-1]"


def test_empty_paragraph_renders_as_empty_string() -> None:
    assert structural_element_markdown(_paragraph("")) == ""


@pytest.mark.parametrize(
    ("bullet", "expected"),
    [
        pytest.param(
            chat_pb2.BulletInfo(list_type=2, nesting_level=1, absolute_ordinal=7),
            "  7. item",
            id="ordered-absolute-ordinal",
        ),
        pytest.param(
            chat_pb2.BulletInfo(list_type=2, ordinal=3),
            "3. item",
            id="ordered-falls-back-to-ordinal",
        ),
        pytest.param(
            chat_pb2.BulletInfo(list_type=2),
            "1. item",
            id="ordered-defaults-to-one",
        ),
        pytest.param(
            chat_pb2.BulletInfo(list_type=1, nesting_level=2),
            "    - item",
            id="unordered-indented",
        ),
        pytest.param(
            chat_pb2.BulletInfo(list_type=1, nesting_level=50),
            "  " * 12 + "- item",
            id="indent-clamped-at-twelve",
        ),
    ],
)
def test_bullet_markdown_variants(bullet: chat_pb2.BulletInfo, expected: str) -> None:
    assert structural_element_markdown(_paragraph("item", bullet=bullet)) == expected


@pytest.mark.parametrize(
    ("named_style", "expected"),
    [
        pytest.param(1, "head", id="normal-text-unprefixed"),
        pytest.param(2, "# head", id="title"),
        pytest.param(3, "## head", id="subtitle"),
        pytest.param(4, "# head", id="heading-1"),
        pytest.param(9, "###### head", id="heading-6"),
        pytest.param(42, "head", id="out-of-range-unprefixed"),
    ],
)
def test_named_style_markdown_variants(named_style: int, expected: str) -> None:
    assert structural_element_markdown(_paragraph("head", named_style=named_style)) == expected


def test_table_markdown_pads_ragged_rows_and_escapes_pipes() -> None:
    table = chat_pb2.Table(
        table_rows=[
            chat_pb2.TableRow(
                table_cells=[
                    chat_pb2.TableCell(content=[_paragraph("a|b")]),
                    chat_pb2.TableCell(content=[_paragraph("c"), _paragraph(""), _paragraph("d")]),
                ]
            ),
            chat_pb2.TableRow(table_cells=[chat_pb2.TableCell(content=[_paragraph("e")])]),
        ]
    )

    rendered = structural_element_markdown(chat_pb2.StructuralElement(table=table))

    # The short second row is padded out to the widest row.
    assert rendered == "| a\\|b | c<br>d |\n| --- | --- |\n| e |  |"


def test_table_markdown_without_rows_is_empty() -> None:
    element = chat_pb2.StructuralElement(table=chat_pb2.Table())

    assert structural_element_markdown(element) == ""


@pytest.mark.parametrize(
    ("element", "expected"),
    [
        pytest.param(
            chat_pb2.StructuralElement(horizontal_rule=chat_pb2.HorizontalRule()),
            "---",
            id="horizontal-rule",
        ),
        pytest.param(
            chat_pb2.StructuralElement(
                code_block=chat_pb2.CodeBlock(content="x = 1", language_hint="python")
            ),
            "```python\nx = 1\n```",
            id="code-block-with-hint",
        ),
        pytest.param(
            chat_pb2.StructuralElement(code_block=chat_pb2.CodeBlock(content="x")),
            "```\nx\n```",
            id="code-block-without-hint",
        ),
        pytest.param(
            chat_pb2.StructuralElement(a2ui_block=chat_pb2.A2uiBlock(json="{}")),
            "```json\n{}\n```",
            id="a2ui-block",
        ),
        pytest.param(
            chat_pb2.StructuralElement(image=chat_pb2.Image(url="https://x.invalid/i")),
            "![image](https://x.invalid/i)",
            id="image",
        ),
        pytest.param(
            chat_pb2.StructuralElement(image=chat_pb2.Image()),
            "",
            id="image-without-url",
        ),
        pytest.param(
            chat_pb2.StructuralElement(),
            "",
            id="unset-variant",
        ),
    ],
)
def test_leaf_markdown_variants(element: chat_pb2.StructuralElement, expected: str) -> None:
    assert structural_element_markdown(element) == expected


def test_thought_markdown_joins_non_empty_children() -> None:
    element = chat_pb2.StructuralElement(
        thought=chat_pb2.Thought(elements=[_paragraph("one"), _paragraph(""), _paragraph("two")])
    )

    assert structural_element_markdown(element) == "one\n\ntwo"


def test_tailwind_doc_markdown_joins_blocks_and_drops_empty_ones() -> None:
    document = _doc(
        _paragraph("intro"),
        _paragraph(""),
        chat_pb2.StructuralElement(horizontal_rule=chat_pb2.HorizontalRule()),
        _paragraph("outro"),
    )

    assert tailwind_doc_markdown(document) == "intro\n\n---\n\noutro"
