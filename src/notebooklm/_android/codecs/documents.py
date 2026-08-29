"""Shared rendering of the admitted Android ``TailwindDoc`` structure."""

from __future__ import annotations

from typing import Any

from ..._types.documents import (
    BlockKind,
    BlockStyle,
    DocumentAnnotation,
    DocumentBlock,
    ListInfo,
    ListStyle,
    StructuredDocument,
    TableCell,
    TextSpan,
)

_MAX_STRUCTURAL_DEPTH = 64


def _bounded_range(
    start: int,
    end: int,
    bounds: tuple[int, int] | None,
    *,
    allow_empty: bool = False,
) -> tuple[int, int] | None:
    if start < 0 or end < start:
        return None
    if bounds is None:
        return start, end
    lower_bound, upper_bound = bounds
    start = max(start, lower_bound)
    end = min(end, upper_bound)
    return None if end < start or (end == start and not allow_empty) else (start, end)


def _paragraph_style(paragraph: Any) -> BlockStyle:
    if not paragraph.HasField("paragraph_style"):
        return BlockStyle.UNSPECIFIED
    try:
        return BlockStyle(int(paragraph.paragraph_style.named_style_type))
    except ValueError:
        return BlockStyle.UNSPECIFIED


def _list_info(paragraph: Any) -> ListInfo | None:
    if not paragraph.HasField("bullet_info"):
        return None
    bullet = paragraph.bullet_info
    try:
        style = ListStyle(int(bullet.list_type))
    except ValueError:
        style = ListStyle.UNSPECIFIED
    ordinal = int(bullet.ordinal)
    return ListInfo(
        style=style,
        nesting_level=int(bullet.nesting_level),
        glyph=bullet.glyph,
        ordinal=ordinal if ordinal > 0 else None,
    )


def _paragraph_spans(paragraph: Any) -> tuple[TextSpan, ...]:
    spans: list[TextSpan] = []
    for element in paragraph.elements:
        start = int(element.start_index)
        end = int(element.end_index)
        if start < 0 or end < start or not element.HasField("text_run"):
            continue
        run = element.text_run
        style = run.text_style if run.HasField("text_style") else None
        spans.append(
            TextSpan(
                start_index=start,
                end_index=end,
                text=run.content,
                bold=bool(style.bold) if style is not None else False,
                italic=bool(style.italic) if style is not None else False,
                underline=bool(style.underline) if style is not None else False,
                url=(style.url or None) if style is not None else None,
            )
        )
    return tuple(spans)


def _kind(element: Any) -> BlockKind:
    for field, kind in (
        ("paragraph", BlockKind.PARAGRAPH),
        ("table", BlockKind.TABLE),
        ("image", BlockKind.IMAGE),
        ("code_block", BlockKind.CODE_BLOCK),
        ("a2ui_block", BlockKind.A2UI_BLOCK),
        ("thought", BlockKind.THOUGHT),
        ("function_call", BlockKind.FUNCTION_CALL),
        ("function_response", BlockKind.FUNCTION_RESPONSE),
        ("horizontal_rule", BlockKind.HORIZONTAL_RULE),
    ):
        if element.HasField(field):
            return kind
    return BlockKind.UNKNOWN


def _decode_table(
    table: Any,
    *,
    bounds: tuple[int, int],
    depth: int,
) -> tuple[tuple[TextSpan, ...], tuple[tuple[TableCell, ...], ...]]:
    if depth >= _MAX_STRUCTURAL_DEPTH:
        return (), ()
    spans: list[TextSpan] = []
    rows: list[tuple[TableCell, ...]] = []
    for row in table.table_rows:
        row_range = _bounded_range(int(row.start_index), int(row.end_index), bounds)
        if row_range is None:
            continue
        cells: list[TableCell] = []
        for cell in row.table_cells:
            cell_range = _bounded_range(
                int(cell.start_index),
                int(cell.end_index),
                row_range,
                allow_empty=True,
            )
            if cell_range is None:
                continue
            cell_start, cell_end = cell_range
            cells.append(TableCell(start_index=cell_start, end_index=cell_end))
            if cell_start == cell_end:
                continue
            for child in cell.content:
                block = _decode_block(child, bounds=cell_range, depth=depth + 1)
                if block is not None:
                    spans.extend(block.spans)
        if any(cell.start_index < cell.end_index for cell in cells):
            rows.append(tuple(cells))
    return tuple(spans), tuple(rows)


def _decode_block(
    element: Any,
    *,
    bounds: tuple[int, int] | None = None,
    depth: int = 0,
) -> DocumentBlock | None:
    block_range = _bounded_range(int(element.start_index), int(element.end_index), bounds)
    if block_range is None:
        return None
    start, end = block_range
    kind = _kind(element)
    if kind is BlockKind.PARAGRAPH:
        paragraph = element.paragraph
        return DocumentBlock(
            start_index=start,
            end_index=end,
            spans=_paragraph_spans(paragraph),
            style=_paragraph_style(paragraph),
            list_info=_list_info(paragraph),
            kind=kind,
        )
    if kind is BlockKind.TABLE:
        spans, rows = _decode_table(
            element.table,
            bounds=block_range,
            depth=depth,
        )
        return DocumentBlock(
            start_index=start,
            end_index=end,
            spans=spans,
            kind=kind,
            table_rows=rows,
        )
    return DocumentBlock(start_index=start, end_index=end, kind=kind)


def decode_blocks(elements: Any) -> tuple[DocumentBlock, ...]:
    """Decode StructuralElements in declared-offset order."""

    blocks = (block for element in elements if (block := _decode_block(element)) is not None)
    return tuple(sorted(blocks, key=lambda block: (block.start_index, block.end_index)))


def decode_document(document: Any) -> StructuredDocument:
    """Decode the shared exact paragraph/table structure and annotations."""

    if not document.HasField("body"):
        return StructuredDocument()
    annotations: list[DocumentAnnotation] = []
    for entry in document.body.inline_object_locations:
        if not entry.HasField("object_id") or not entry.object_id.id:
            continue
        if not entry.HasField("content_range"):
            continue
        start = int(entry.content_range.start_index)
        end = int(entry.content_range.end_index)
        if start < 0 or end < start:
            continue
        annotations.append(
            DocumentAnnotation(
                object_id=entry.object_id.id,
                start_index=start,
                end_index=end,
            )
        )
    return StructuredDocument(
        blocks=decode_blocks(document.body.content),
        annotations=tuple(annotations),
    )


def _plain_text_parts(element: Any, *, depth: int) -> list[str]:
    if depth >= _MAX_STRUCTURAL_DEPTH:
        return []
    if element.HasField("paragraph"):
        return [
            part.text_run.content
            for part in element.paragraph.elements
            if part.HasField("text_run") and part.text_run.content
        ]
    if element.HasField("table"):
        return [
            text
            for row in element.table.table_rows
            for cell in row.table_cells
            for child in cell.content
            for text in _plain_text_parts(child, depth=depth + 1)
        ]
    if element.HasField("code_block"):
        return [element.code_block.content] if element.code_block.content else []
    if element.HasField("a2ui_block"):
        return [element.a2ui_block.json] if element.a2ui_block.json else []
    if element.HasField("thought"):
        return [
            text
            for child in element.thought.elements
            for text in _plain_text_parts(child, depth=depth + 1)
        ]
    # Image, horizontal-rule, and paragraph image/resource leaves carry no
    # human-readable text or alt label in the admitted schema. Their URLs/IDs
    # are metadata, not source prose, so the plain rendering omits them.
    return []


def structural_elements_plain_text(elements: Any) -> str:
    """Render text-bearing structural elements in deterministic wire order."""

    return "\n".join(text for element in elements for text in _plain_text_parts(element, depth=0))


def tailwind_doc_plain_text(document: Any) -> str:
    """Return every admitted text-bearing leaf in deterministic wire order."""

    if not document.HasField("body"):
        return ""
    return structural_elements_plain_text(document.body.content)


def _inline_markdown(paragraph: Any) -> str:
    parts: list[str] = []
    for element in paragraph.elements:
        if element.HasField("text_run"):
            text = element.text_run.content
            if element.text_run.HasField("text_style"):
                style = element.text_run.text_style
                if style.code:
                    text = f"`{text}`"
                if style.bold:
                    text = f"**{text}**"
                if style.italic:
                    text = f"*{text}*"
                if style.strikethrough:
                    text = f"~~{text}~~"
                if style.underline:
                    text = f"<u>{text}</u>"
                if style.math:
                    text = f"${text}$"
                if style.url:
                    text = f"[{text}]({style.url})"
            parts.append(text)
        elif element.HasField("image") and element.image.url:
            parts.append(f"![image]({element.image.url})")
        elif element.HasField("resource") and element.resource.id:
            parts.append(f"[resource: {element.resource.id}]")
    return "".join(parts).strip()


def structural_element_markdown(element: Any) -> str:
    """Render one exact TailwindDoc structural variant as Markdown."""

    if element.HasField("paragraph"):
        paragraph = element.paragraph
        text = _inline_markdown(paragraph)
        if not text:
            return ""
        if paragraph.HasField("bullet_info"):
            bullet = paragraph.bullet_info
            indent = "  " * max(0, min(int(bullet.nesting_level), 12))
            if int(bullet.list_type) == 2:
                ordinal = int(bullet.absolute_ordinal or bullet.ordinal or 1)
                return f"{indent}{ordinal}. {text}"
            return f"{indent}- {text}"
        if paragraph.HasField("paragraph_style"):
            named_style = int(paragraph.paragraph_style.named_style_type)
            if named_style == 2:
                return f"# {text}"
            if named_style == 3:
                return f"## {text}"
            if 4 <= named_style <= 9:
                return f"{'#' * (named_style - 3)} {text}"
        return text
    if element.HasField("table"):
        rows: list[list[str]] = []
        for row in element.table.table_rows:
            cells: list[str] = []
            for cell in row.table_cells:
                cell_blocks = [structural_element_markdown(block) for block in cell.content]
                cells.append(
                    "<br>".join(block for block in cell_blocks if block).replace("|", "\\|")
                )
            rows.append(cells)
        if not rows:
            return ""
        width = max(len(row) for row in rows)
        normalized = [row + [""] * (width - len(row)) for row in rows]
        lines = ["| " + " | ".join(row) + " |" for row in normalized]
        lines.insert(1, "| " + " | ".join("---" for _ in range(width)) + " |")
        return "\n".join(lines)
    if element.HasField("horizontal_rule"):
        return "---"
    if element.HasField("code_block"):
        hint = element.code_block.language_hint
        return f"```{hint}\n{element.code_block.content}\n```"
    if element.HasField("a2ui_block"):
        return f"```json\n{element.a2ui_block.json}\n```"
    if element.HasField("image") and element.image.url:
        return f"![image]({element.image.url})"
    if element.HasField("thought"):
        return "\n\n".join(
            block
            for block in (structural_element_markdown(child) for child in element.thought.elements)
            if block
        )
    return ""


def tailwind_doc_markdown(document: Any) -> str:
    """Render a TailwindDoc body without appending its object metadata."""

    if not document.HasField("body"):
        return ""
    return "\n\n".join(
        block
        for block in (structural_element_markdown(element) for element in document.body.content)
        if block
    ).strip()


__all__ = [
    "decode_blocks",
    "decode_document",
    "structural_element_markdown",
    "structural_elements_plain_text",
    "tailwind_doc_markdown",
    "tailwind_doc_plain_text",
]
