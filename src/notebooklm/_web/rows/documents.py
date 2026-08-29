"""Row adapters for the ``TailwindDoc`` document tree.

All positional knowledge for NotebookLM's document tree lives here. The shape is
recovered from ``docs/android/schema.proto`` (section
``orchestration.v1/tailwind_doc.pb.dart``) and live-confirmed against both
carriers of the tree (issues #2128 and #2120)::

    Body               = [content, inlineObjectLocations]
    StructuralElement  = [startIndex, endIndex, paragraph, _, table, image,
                          codeBlock, a2uiBlock, thought, functionCall,
                          functionResponse, horizontalRule]
    Paragraph          = [elements, paragraphStyle, _, bulletInfo]
    Table              = [rows, columns, tableRows]
    TableRow           = [startIndex, endIndex, tableCells]
    TableCell          = [startIndex, endIndex, content]   # StructuralElement[]
    ParagraphElement   = [startIndex, endIndex, textRun, image, resource]
    TextRun            = [content, textStyle]
    TextStyle          = [bold, italic, underline, url]
    ParagraphStyle     = [_, namedStyleType]
    BulletInfo         = [_, _, nestingLevel, {101: glyph, 102: listType, ...}]
    AnnotationMapEntry = [objectId, contentRange]
    ObjectId           = [id]
    Range              = [_, startIndex, endIndex]

``BulletInfo``'s ``glyph`` / ``listType`` / ``ordinal`` fields carry protobuf
tags 101-104, which the ``batchexecute`` array encoding cannot place
positionally; they arrive in a trailing sparse object keyed by tag number, so
:class:`BulletInfoRow` reads them by key rather than by index.

Reads follow this package's established absence-vs-malformed split (#1485):

* **Element-level reads degrade softly.** A missing, short or wrongly-typed slot
  inside a block yields the neutral default rather than raising. A document is
  prose, and half a paragraph is worth more to a caller than an exception — the
  offsets of the blocks that *did* decode stay correct either way, because they
  are absolute document positions rather than running counts.
* **A reshaped top-level container WARNS and yields an empty document.** A
  truthy non-list where ``Body``'s content or annotation list belongs is
  structural drift, not an empty document, and must not pass unremarked: it is
  invisible otherwise, because ``SourceFulltext.content`` is harvested by a
  separate recursive string walk that keeps working through such a reshape, so
  the only symptom would be offsets silently ceasing to line up — the #2128
  defect class itself.

  It **warns** rather than raising, which is where this module departs from the
  citation container in ``AnswerRow.citations``. There, the citations *are* the
  payload, so a reshaped container makes the response untrustworthy. Here the
  document is an addition to a call whose load-bearing return is
  ``SourceFulltext.content``; raising would take a working ``get_fulltext``
  down to signal that a supplementary decode failed. The warning is the signal,
  and the caller still gets the text.

Recursion through table cells is depth-bounded (:data:`_MAX_BLOCK_DEPTH`), like
the other walks over server-controlled trees in this codebase.
"""

from __future__ import annotations

import logging
import reprlib
from dataclasses import dataclass, field
from typing import Any, ClassVar, NamedTuple

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

__all__ = [
    "AnnotationEntryRow",
    "BulletInfoRow",
    "DocumentBodyRow",
    "ParagraphElementRow",
    "ParagraphRow",
    "StructuralElementRow",
    "TableContent",
    "TableRow",
    "TextRunRow",
    "build_blocks",
    "build_document",
]


#: Recursion bound for nested table decoding. ``TableCell.content`` holds
#: ``StructuralElement`` rows, which may themselves be tables, so the walk is
#: mutually recursive over server-controlled input. Matches the intent of
#: ``_source/content.extract_all_text``'s and
#: ``_web/rows/chat_stream.extract_uuid_from_nested``'s
#: bounds: a pathological tree must degrade, not raise ``RecursionError``.
#: Real documents nest one or two levels.
_MAX_BLOCK_DEPTH = 20

logger = logging.getLogger("notebooklm._row_adapters.documents")


def _as_list(value: Any) -> list[Any] | None:
    return value if isinstance(value, list) else None


def _at(raw: Any, pos: int) -> Any:
    """``raw[pos]`` when ``raw`` is a long-enough list, else ``None``."""
    if not isinstance(raw, list) or len(raw) <= pos:
        return None
    return raw[pos]


def _as_index(value: Any) -> int | None:
    """``value`` as a non-negative character offset, else ``None``.

    ``bool`` is an ``int`` subclass in Python and is rejected explicitly; a
    negative offset cannot index a string and is treated as absent.
    """
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return None
    return value


@dataclass(frozen=True)
class TextRunRow:
    """Typed view of a ``TextRun`` — ``[content, textStyle]``."""

    _raw: Any = field(repr=False)

    _CONTENT_POS: ClassVar[int] = 0
    _STYLE_POS: ClassVar[int] = 1
    # TextStyle = [bold, italic, underline, url]
    _BOLD_POS: ClassVar[int] = 0
    _ITALIC_POS: ClassVar[int] = 1
    _UNDERLINE_POS: ClassVar[int] = 2
    _URL_POS: ClassVar[int] = 3

    @property
    def content(self) -> str | None:
        """Literal run text at ``text_run[0]`` — ``None`` when absent."""
        value = _at(self._raw, self._CONTENT_POS)
        return value if isinstance(value, str) else None

    @property
    def _style(self) -> Any:
        return _at(self._raw, self._STYLE_POS)

    def _flag(self, pos: int) -> bool:
        return _at(self._style, pos) is True

    @property
    def bold(self) -> bool:
        return self._flag(self._BOLD_POS)

    @property
    def italic(self) -> bool:
        return self._flag(self._ITALIC_POS)

    @property
    def underline(self) -> bool:
        return self._flag(self._UNDERLINE_POS)

    @property
    def url(self) -> str | None:
        """Link target at ``textStyle[3]`` — ``None`` when the run is not a link."""
        value = _at(self._style, self._URL_POS)
        return value if isinstance(value, str) and value else None


@dataclass(frozen=True)
class ParagraphElementRow:
    """Typed view of a ``ParagraphElement`` — ``[start, end, textRun, ...]``."""

    _raw: Any = field(repr=False)

    _START_POS: ClassVar[int] = 0
    _END_POS: ClassVar[int] = 1
    _TEXT_RUN_POS: ClassVar[int] = 2

    @property
    def span(self) -> TextSpan | None:
        """This element as a :class:`TextSpan`, or ``None`` when not a text run.

        ``None`` covers both a malformed element and a legitimately non-text
        one: ``ParagraphElement`` also carries ``image`` and ``resource``
        variants, which have no ``content`` to contribute to the document text.
        """
        start = _as_index(_at(self._raw, self._START_POS))
        end = _as_index(_at(self._raw, self._END_POS))
        if start is None or end is None or end < start:
            return None
        run = TextRunRow(_at(self._raw, self._TEXT_RUN_POS))
        content = run.content
        if content is None:
            return None
        return TextSpan(
            start_index=start,
            end_index=end,
            text=content,
            bold=run.bold,
            italic=run.italic,
            underline=run.underline,
            url=run.url,
        )


@dataclass(frozen=True)
class BulletInfoRow:
    """Typed view of a ``BulletInfo`` — list membership of a paragraph.

    ``nestingLevel`` is protobuf tag 3 and so arrives positionally, while
    ``glyph`` / ``listType`` / ``ordinal`` are tags 101-104 and arrive in a
    trailing sparse object keyed by the tag number as a string.
    """

    _raw: Any = field(repr=False)

    _NESTING_LEVEL_POS: ClassVar[int] = 2
    _GLYPH_TAG: ClassVar[str] = "101"
    _LIST_TYPE_TAG: ClassVar[str] = "102"
    _ORDINAL_TAG: ClassVar[str] = "103"

    @property
    def _sparse(self) -> dict[str, Any]:
        """The trailing high-tag object, or ``{}`` when the row carries none.

        Scans from the end because the object is always last when present; a
        row without one (only ``nestingLevel`` set) is normal, not drift.
        """
        for item in reversed(_as_list(self._raw) or ()):
            if isinstance(item, dict):
                return item
        return {}

    @property
    def info(self) -> ListInfo:
        """This row as a :class:`ListInfo` (neutral defaults when slots are absent)."""
        sparse = self._sparse
        raw_type = sparse.get(self._LIST_TYPE_TAG)
        try:
            style = ListStyle(raw_type)
        except ValueError:
            style = ListStyle.UNSPECIFIED
        glyph = sparse.get(self._GLYPH_TAG)
        ordinal = sparse.get(self._ORDINAL_TAG)
        nesting = _at(self._raw, self._NESTING_LEVEL_POS)
        return ListInfo(
            style=style,
            nesting_level=nesting
            if isinstance(nesting, int) and not isinstance(nesting, bool)
            else 0,
            glyph=glyph if isinstance(glyph, str) else "",
            ordinal=ordinal if isinstance(ordinal, int) and not isinstance(ordinal, bool) else None,
        )


@dataclass(frozen=True)
class ParagraphRow:
    """Typed view of a ``Paragraph`` — ``[elements, paragraphStyle, _, bulletInfo]``."""

    _raw: Any = field(repr=False)

    _ELEMENTS_POS: ClassVar[int] = 0
    _STYLE_POS: ClassVar[int] = 1
    _BULLET_INFO_POS: ClassVar[int] = 3
    # ParagraphStyle = [_, namedStyleType]
    _NAMED_STYLE_POS: ClassVar[int] = 1

    @property
    def spans(self) -> tuple[TextSpan, ...]:
        """The paragraph's text runs, in document order; ``()`` when absent."""
        elements = _as_list(_at(self._raw, self._ELEMENTS_POS))
        if elements is None:
            return ()
        spans = (ParagraphElementRow(element).span for element in elements)
        return tuple(span for span in spans if span is not None)

    @property
    def style(self) -> BlockStyle:
        """Named paragraph style; ``UNSPECIFIED`` when absent or unrecognised."""
        raw = _at(_at(self._raw, self._STYLE_POS), self._NAMED_STYLE_POS)
        try:
            return BlockStyle(raw)
        except ValueError:
            return BlockStyle.UNSPECIFIED

    @property
    def list_info(self) -> ListInfo | None:
        """List membership, or ``None`` when the paragraph is not a list item."""
        raw = _as_list(_at(self._raw, self._BULLET_INFO_POS))
        if raw is None:
            return None
        return BulletInfoRow(raw).info


class TableContent(NamedTuple):
    """What a ``Table`` decodes to: flattened runs, and the grid holding them.

    Two views of the same walk, which is why they are returned together rather
    than by two properties that would each have to make it. ``spans`` is the
    offset-bearing sequence :class:`~notebooklm.types.DocumentBlock` carries
    like any other block's; ``rows`` is the boundary information that
    flattening those runs into one sequence destroys (#2230).
    """

    spans: tuple[TextSpan, ...]
    rows: tuple[tuple[TableCell, ...], ...]


@dataclass(frozen=True)
class TableRow:
    """Typed view of a ``Table`` — ``[rows, columns, tableRows]``.

    The class name follows this package's ``<thing>Row`` convention and so
    collides confusingly with the proto's *own* ``TableRow`` message, which is
    one level below: the schema defines ``Table`` (a grid) containing
    ``TableRow``s containing ``TableCell``s. Only the text matters here, so this
    single adapter walks both of those levels inline rather than adding a
    ``TableRowRow`` whose name would be worse than the collision.

    A cell's ``content`` is ``repeated StructuralElement`` — the same rows as
    the document body — so the walk recurses through :func:`build_blocks`
    instead of duplicating the paragraph decode. Tables were the biggest hole
    in ``StructuredDocument.text``: an infobox in a Wikipedia source occupies
    150+ characters of the coordinate space. ``CodeBlock`` and ``Thought`` also
    carry undecoded text and remain holes, but are placeholder-filled rather
    than dropped, so they cost their own characters and not the alignment of
    everything after them.
    """

    _raw: Any = field(repr=False)

    _TABLE_ROWS_POS: ClassVar[int] = 2
    #: ``TableRow = [startIndex, endIndex, tableCells]``
    _ROW_START_POS: ClassVar[int] = 0
    _ROW_END_POS: ClassVar[int] = 1
    _ROW_CELLS_POS: ClassVar[int] = 2
    #: ``TableCell = [startIndex, endIndex, content]``
    _CELL_START_POS: ClassVar[int] = 0
    _CELL_END_POS: ClassVar[int] = 1
    _CELL_CONTENT_POS: ClassVar[int] = 2

    def decode(self, depth: int, bounds: tuple[int, int]) -> TableContent:
        """Every text run in the table, row-major, **and** the grid it came from.

        ``bounds`` is the enclosing table element's range, narrowed at each
        level by the row's and then the cell's own declared range. A cell's
        blocks are *part of* that cell exactly as a block's spans are part of
        the block, so the same containment rule has to be applied on the way
        down: a child block whose own range overruns its cell is valid relative
        to itself, and only the container knows better. Without this, one
        malformed cell's text overlaps — and so displaces — its sibling's.

        The runs stay flattened, because the flat sequence is what
        :attr:`~notebooklm.types.StructuredDocument.text` lays out at the
        backend's offsets. What changed in #2230 is that the *cell bounds* this
        walk computes are no longer thrown away one line later: they are
        returned alongside as :class:`~notebooklm.types.TableCell` ranges, so a
        reader can tell a cell boundary from a formatting change. They are the
        narrowed bounds — the same ranges the cell's own blocks were clamped
        to — so a cell can never claim a position its table does not own, and
        carrying them adds no character to the coordinate space.

        A cell holding a nested table contributes that table's text to *its*
        cell rather than its own cells to the grid: the inner boundaries would
        overlap the outer cell that contains them, and a grid of overlapping
        cells cannot be rendered as rows. Nested tables therefore read as they
        did before — one cell of run-together text.
        """
        collected: list[TextSpan] = []
        rows: list[tuple[TableCell, ...]] = []
        for row in _as_list(_at(self._raw, self._TABLE_ROWS_POS)) or []:
            row_bounds = _narrow(bounds, _at(row, self._ROW_START_POS), _at(row, self._ROW_END_POS))
            if row_bounds is None:
                continue
            cells: list[TableCell] = []
            for cell in _as_list(_at(row, self._ROW_CELLS_POS)) or []:
                cell_bounds = _clamp(
                    row_bounds, _at(cell, self._CELL_START_POS), _at(cell, self._CELL_END_POS)
                )
                cell_start, cell_end = cell_bounds
                cells.append(TableCell(start_index=cell_start, end_index=cell_end))
                if cell_start == cell_end:
                    continue  # an empty column: a boundary to keep, no text to walk
                for block in build_blocks(
                    _at(cell, self._CELL_CONTENT_POS), depth, bounds=cell_bounds
                ):
                    collected.extend(block.spans)
            if any(cell.start_index < cell.end_index for cell in cells):
                rows.append(tuple(cells))
        return TableContent(spans=tuple(collected), rows=tuple(rows))


@dataclass(frozen=True)
class StructuralElementRow:
    """Typed view of a ``StructuralElement`` — ``[start, end, paragraph, ...]``.

    ``StructuralElement`` is a oneof over nine variants. Only ``paragraph``
    (tag 3) and ``table`` (tag 5) carry text this client decodes; the rest are
    identified by :attr:`kind` and contribute offsets without text — some
    because they have none, some because their text is not decoded yet (see
    :class:`~notebooklm.types.BlockKind`).
    """

    _raw: Any = field(repr=False)

    _START_POS: ClassVar[int] = 0
    _END_POS: ClassVar[int] = 1
    _PARAGRAPH_POS: ClassVar[int] = 2
    _TABLE_POS: ClassVar[int] = 4
    _IMAGE_POS: ClassVar[int] = 5
    _CODE_BLOCK_POS: ClassVar[int] = 6
    _A2UI_BLOCK_POS: ClassVar[int] = 7
    _THOUGHT_POS: ClassVar[int] = 8
    _FUNCTION_CALL_POS: ClassVar[int] = 9
    _FUNCTION_RESPONSE_POS: ClassVar[int] = 10
    _HORIZONTAL_RULE_POS: ClassVar[int] = 11

    #: Variant slot -> kind, in oneof tag order. The first populated slot wins,
    #: which is unambiguous because the wire populates exactly one.
    _VARIANTS: ClassVar[tuple[tuple[int, BlockKind], ...]] = (
        (_PARAGRAPH_POS, BlockKind.PARAGRAPH),
        (_TABLE_POS, BlockKind.TABLE),
        (_IMAGE_POS, BlockKind.IMAGE),
        (_CODE_BLOCK_POS, BlockKind.CODE_BLOCK),
        (_A2UI_BLOCK_POS, BlockKind.A2UI_BLOCK),
        (_THOUGHT_POS, BlockKind.THOUGHT),
        (_FUNCTION_CALL_POS, BlockKind.FUNCTION_CALL),
        (_FUNCTION_RESPONSE_POS, BlockKind.FUNCTION_RESPONSE),
        (_HORIZONTAL_RULE_POS, BlockKind.HORIZONTAL_RULE),
    )

    @property
    def kind(self) -> BlockKind:
        """Which variant is populated — ``UNKNOWN`` when none is."""
        for pos, kind in self._VARIANTS:
            if _at(self._raw, pos) is not None:
                return kind
        return BlockKind.UNKNOWN

    def block(
        self, depth: int = 0, *, bounds: tuple[int, int] | None = None
    ) -> DocumentBlock | None:
        """This element as a :class:`DocumentBlock`, or ``None`` when unusable.

        ``None`` only when the element carries no valid character range. An
        element with a range but no decodable text still yields a block: it
        occupies document offsets, and dropping it would leave an unexplained
        gap in the coordinate space that every downstream offset depends on.
        """
        start = _as_index(_at(self._raw, self._START_POS))
        end = _as_index(_at(self._raw, self._END_POS))
        if start is None or end is None or end < start:
            return None
        if bounds is not None:
            # A nested block is part of its container (a table cell), so it
            # cannot claim positions outside it. Clamping here is the block-level
            # twin of the span-level clamp in ``DocumentBlock.__post_init__``.
            start, end = max(start, bounds[0]), min(end, bounds[1])
            if end <= start:
                return None
        kind = self.kind
        if kind is BlockKind.TABLE:
            table = TableRow(_at(self._raw, self._TABLE_POS)).decode(depth, (start, end))
            return DocumentBlock(
                start_index=start,
                end_index=end,
                spans=table.spans,
                kind=kind,
                table_rows=table.rows,
            )
        paragraph = ParagraphRow(_at(self._raw, self._PARAGRAPH_POS))
        return DocumentBlock(
            start_index=start,
            end_index=end,
            spans=paragraph.spans,
            style=paragraph.style,
            list_info=paragraph.list_info,
            kind=kind,
        )


@dataclass(frozen=True)
class AnnotationEntryRow:
    """Typed view of an ``AnnotationMapEntry`` — ``[objectId, contentRange]``.

    The field order is the proto's (``objectId`` = tag 1, ``contentRange`` =
    tag 2) and is confirmed by live capture: the ``objectId`` slot holds the
    same id as the citation's ``DocumentObject.objectId``, which is what makes
    the join to a :class:`~notebooklm.types.ChatReference` possible.
    """

    _raw: Any = field(repr=False)

    _OBJECT_ID_POS: ClassVar[int] = 0
    _RANGE_POS: ClassVar[int] = 1
    # ObjectId = [id]
    _OBJECT_ID_VALUE_POS: ClassVar[int] = 0
    # Range = [_, startIndex, endIndex]
    _RANGE_START_POS: ClassVar[int] = 1
    _RANGE_END_POS: ClassVar[int] = 2

    @property
    def annotation(self) -> DocumentAnnotation | None:
        """This entry as a :class:`DocumentAnnotation`, or ``None`` when unusable.

        An entry without an object id or without a valid range anchors nothing,
        so it is dropped rather than surfaced half-populated.
        """
        object_id = _at(_at(self._raw, self._OBJECT_ID_POS), self._OBJECT_ID_VALUE_POS)
        if not isinstance(object_id, str) or not object_id:
            return None
        raw_range = _at(self._raw, self._RANGE_POS)
        start = _as_index(_at(raw_range, self._RANGE_START_POS))
        end = _as_index(_at(raw_range, self._RANGE_END_POS))
        if start is None or end is None or end < start:
            return None
        return DocumentAnnotation(object_id=object_id, start_index=start, end_index=end)


@dataclass(frozen=True)
class DocumentBodyRow:
    """Typed view of a ``Body`` — ``[content, inlineObjectLocations]``."""

    _raw: Any = field(repr=False)

    _CONTENT_POS: ClassVar[int] = 0
    _ANNOTATIONS_POS: ClassVar[int] = 1

    def _container(self, pos: int, label: str) -> list[Any]:
        """The list at ``body[pos]`` — ``[]`` when absent, warning on drift.

        Falsy (absent / ``None`` / empty) is a legitimate "nothing here" and is
        silent. A truthy non-list is a reshaped container: it warns and still
        degrades to ``[]``, for the reason the module docstring gives.
        """
        value = _at(self._raw, pos)
        if not value:
            return []
        if not isinstance(value, list):
            logger.warning(
                "Document %s container holds %s, expected a list; "
                "returning an empty document (source text is unaffected, but "
                "citation offsets will not resolve): %s",
                label,
                type(value).__name__,
                reprlib.repr(value),
            )
            return []
        return value

    @property
    def elements(self) -> list[Any]:
        """Raw ``StructuralElement`` list at ``body[0]`` — ``[]`` when absent."""
        return self._container(self._CONTENT_POS, "elements")

    @property
    def annotation_entries(self) -> list[Any]:
        """Raw ``AnnotationMapEntry`` list at ``body[1]`` — ``[]`` when absent.

        Empty is the norm, not a defect: only documents carrying inline objects
        (a chat answer with citations) populate the map.
        """
        return self._container(self._ANNOTATIONS_POS, "annotation_entries")


def _clamp(bounds: tuple[int, int], raw_start: Any, raw_end: Any) -> tuple[int, int]:
    """``bounds`` intersected with a declared child range, empty result kept.

    A malformed child range cannot widen its parent — only narrow it — so an
    absent or unusable one leaves the parent's bounds untouched rather than
    dropping the subtree. An intersection that is *empty* comes back as a
    zero-width range at the intersection point rather than as ``None``: for
    text that means "nothing here" (see :func:`_narrow`), but for a **table
    cell** it means "a column that holds no characters", which is information a
    reader needs — a row of ``A``, ``""``, ``C`` must not read as two columns.
    """
    start, end = bounds
    child_start = _as_index(raw_start)
    child_end = _as_index(raw_end)
    if child_start is not None:
        start = max(start, child_start)
    if child_end is not None:
        end = min(end, child_end)
    return (start, max(start, end))


def _narrow(bounds: tuple[int, int], raw_start: Any, raw_end: Any) -> tuple[int, int] | None:
    """:func:`_clamp`, with an empty intersection reported as ``None``.

    What every caller that is descending after *text* wants: a subtree
    occupying no positions can carry none, so there is nothing to walk.
    """
    start, end = _clamp(bounds, raw_start, raw_end)
    return None if end <= start else (start, end)


def build_blocks(
    elements: Any, depth: int = 0, *, bounds: tuple[int, int] | None = None
) -> tuple[DocumentBlock, ...]:
    """Parse a raw ``StructuralElement`` list into :class:`DocumentBlock` objects.

    Takes the *bare element list* — ``body[0]`` for a whole document, or
    ``Citation.fragment.elements`` for a cited fragment, which is the same list
    type sliced out of the source document (#2120). A non-list input yields
    ``()``.

    ``depth`` tracks nesting through table cells, whose content is more
    ``StructuralElement`` rows; past :data:`_MAX_BLOCK_DEPTH` the walk stops and
    warns rather than exhausting the stack on a pathological tree.
    """
    raw = _as_list(elements)
    if raw is None:
        return ()
    if depth > _MAX_BLOCK_DEPTH:
        logger.warning(
            "Max nesting depth (%d) reached decoding a document; "
            "dropping the remaining nested blocks",
            _MAX_BLOCK_DEPTH,
        )
        return ()
    blocks = (StructuralElementRow(element).block(depth + 1, bounds=bounds) for element in raw)
    # Sorted by declared position, not container order. The offsets are what
    # define document order, and every consumer depends on that: the layout in
    # ``StructuredDocument.text``, the rendering order of
    # ``SourceFulltext.document.blocks``, and — the one with no other
    # protection — ``ChatReference.cited_text``, which concatenates block text
    # directly and would otherwise read a reordered fragment backwards (#2120).
    return tuple(
        sorted(
            (block for block in blocks if block is not None),
            key=lambda block: block.start_index,
        )
    )


def build_document(body: Any) -> StructuredDocument:
    """Parse a raw ``Body`` into a :class:`StructuredDocument`.

    An absent / falsy / non-list ``body`` yields an empty document rather than
    ``None`` so callers never have to branch; :attr:`StructuredDocument.text` is
    then ``""``. A *truthy non-list* in either of ``Body``'s two slots is
    structural drift: it logs a WARNING and still yields an empty document — see
    the module docstring for why the container is treated more loudly than the
    elements inside it, and why it nonetheless does not raise.
    """
    row = DocumentBodyRow(body)
    annotations = (AnnotationEntryRow(entry).annotation for entry in row.annotation_entries)
    return StructuredDocument(
        blocks=build_blocks(row.elements),
        annotations=tuple(entry for entry in annotations if entry is not None),
    )
