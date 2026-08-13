"""Structured-document types shared by the source-content and chat-answer paths.

NotebookLM returns prose as a ``TailwindDoc`` — a nested positional tree whose
shape is recovered in ``docs/mobile/schema.proto`` (section
``orchestration.v1/tailwind_doc.pb.dart``). Three call sites carry the *same*
tree:

* ``GET_SOURCE`` (fulltext) — ``result[3][0]`` is the source's document ``Body``.
* ``GenerateFreeFormStreamed`` — ``responseDoc.body`` is the answer's ``Body``.
* Each streamed-chat citation — ``Citation.fragment.elements`` is a bare list of
  the same ``StructuralElement`` blocks, sliced out of the source document.

Before #2128 / #2120 the client flattened all three to a bag of strings, which
threw away the one thing citations need: the **character offsets**. Every block
and span carries ``start_index`` / ``end_index`` into a single document-wide
coordinate space, and :attr:`StructuredDocument.text` is laid out at those
offsets — so :meth:`StructuredDocument.slice` is by construction what the
backend meant by ``[n, m)``. :class:`DocumentAnnotation` then anchors a document
object (a citation) to a range in that space.

Those offsets are **UTF-16 code units**, not Python characters
(:func:`utf16_len`). The distinction is invisible until a document contains an
emoji and then costs one position per astral character, which is why
:meth:`StructuredDocument.slice` exists rather than callers indexing
:attr:`StructuredDocument.text` directly.

They are also not the only offset space this library exposes: ``source read``'s
``offset`` parameter windows :attr:`~notebooklm.types.SourceFulltext.content`
in Python characters. The two are not interchangeable — different string,
different unit — and mixing them produces a plausible-looking wrong answer
rather than an error. See `#2211
<https://github.com/teng-lin/notebooklm-py/issues/2211>`_.

Which leaves three renderings of the same source, each answering a different
question: :attr:`~notebooklm.types.SourceFulltext.content` is what this client
has always returned and stays byte-identical; :attr:`StructuredDocument.text`
is where offsets resolve; and :meth:`StructuredDocument.render` — surfaced as
:attr:`~notebooklm.types.SourceFulltext.rendered_content` — is the one meant to
be read. :meth:`StructuredDocument.render` documents what separates them.

The types here are transport-neutral and carry no positional knowledge; the
``TailwindDoc`` array positions live in
:mod:`notebooklm._row_adapters.documents`.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import NamedTuple

__all__ = [
    "BlockKind",
    "BlockStyle",
    "DocumentAnnotation",
    "DocumentBlock",
    "ListInfo",
    "ListStyle",
    "StructuredDocument",
    "TextSpan",
    "utf16_len",
]


#: Filler for document positions this client cannot render as text — the
#: OBJECT REPLACEMENT CHARACTER, which is what Google's own document APIs emit
#: at an inline object. It is a BMP character, so one of them occupies exactly
#: one UTF-16 unit and filler never perturbs the offsets it is holding open.
_PLACEHOLDER = "￼"

#: Ceiling on the span a single document may claim, in UTF-16 units. Filler is
#: synthesized locally from offsets the server chose, so a drifted or hostile
#: ``start_index`` would otherwise let a tiny response ask for gigabytes of
#: placeholder. Two million units is ~50x the largest document observed (the
#: 101k-character Wikipedia source in the #2128 sweep) and cheap to hold.
_MAX_DOCUMENT_EXTENT = 2_000_000


class _Placement(NamedTuple):
    """One span's laid-out position: its clamped range and matching text."""

    start: int
    end: int
    text: str


def _validate_range(owner: str, start_index: int, end_index: int) -> None:
    """Raise ``ValueError`` unless ``[start_index, end_index)`` can index a string.

    The row adapters already reject these shapes before constructing anything,
    so this never fires on decoded data. It exists because these types are
    **public**: the adapter is one construction path among several, and an
    offset-bearing value object whose offsets cannot index anything is exactly
    the bug this module was added to remove.

    Raises:
        ValueError: on a negative offset or ``end_index < start_index``.
    """
    if start_index < 0 or end_index < 0:
        raise ValueError(
            f"{owner} offsets must be non-negative "
            f"(got start_index={start_index!r}, end_index={end_index!r})"
        )
    if end_index < start_index:
        raise ValueError(f"{owner} start_index ({start_index}) > end_index ({end_index})")


def utf16_len(text: str) -> int:
    """Length of ``text`` in UTF-16 code units — the unit the backend counts in.

    Every offset on the wire is a UTF-16 code unit, the JavaScript convention,
    which differs from Python's code points on any character outside the Basic
    Multilingual Plane. An emoji is one Python character and **two** UTF-16
    units, so a document containing one drifts by a unit at that point and by a
    further unit at every subsequent one.

    Verified against this module's live fixtures: 22 of 22 spans in the captured
    answer document match their declared width in UTF-16 units, while only 21
    match in Python characters — the single divergence being the span that
    opens with an emoji.
    """
    return len(text.encode("utf-16-le", errors="surrogatepass")) // 2


def _sanitize(text: str) -> str:
    """``text`` with every unpaired surrogate replaced by the placeholder.

    Two sources, one cure. A wire string may carry an escaped **unpaired**
    surrogate that Python's JSON decoder preserves happily, and cutting at a
    UTF-16 offset that lands inside a valid surrogate *pair* manufactures one.
    Either way the result is a ``str`` that cannot be UTF-8 encoded, so merely
    keeping it would move the failure from this module to whoever next printed,
    logged or serialized :attr:`StructuredDocument.text` — a worse place to
    discover it, and past every soft-degrade guard here.

    The substitution is width-preserving, which is what makes it safe: a lone
    surrogate and :data:`_PLACEHOLDER` are one UTF-16 code unit each, so every
    surrounding offset survives untouched.
    """
    if not any(0xD800 <= ord(char) <= 0xDFFF for char in text):
        return text
    return "".join(_PLACEHOLDER if 0xD800 <= ord(char) <= 0xDFFF else char for char in text)


def _utf16_slice(text: str, start: int, end: int) -> str:
    """``text`` between two UTF-16 code-unit offsets, sanitized.

    ``surrogatepass`` on both legs so a string that already contains a lone
    surrogate can be cut at all rather than raising ``UnicodeEncodeError`` out
    of a property; :func:`_sanitize` then removes any that survive or that the
    cut itself created.
    """
    encoded = text.encode("utf-16-le", errors="surrogatepass")
    return _sanitize(encoded[start * 2 : end * 2].decode("utf-16-le", errors="surrogatepass"))


def _fit(text: str, width: int) -> str:
    """``text`` normalised to occupy exactly ``width`` UTF-16 units.

    Truncates a run that claims fewer positions than its text occupies and pads
    one that claims more. Both are wire inconsistencies rather than expected
    shapes — every live capture has ``utf16_len(text) == end_index -
    start_index`` — but silently letting either through would shift every later
    offset, which is precisely the class of failure this module exists to
    prevent. Normalising confines the damage to the one bad run.
    """
    length = utf16_len(text)
    if length == width:
        return _sanitize(text)
    if length < width:
        return _sanitize(text) + _PLACEHOLDER * (width - length)
    # Truncate in UTF-16 space. ``_utf16_slice`` sanitizes any surrogate the cut
    # split, and does so width-preservingly, so no re-padding is needed.
    return _utf16_slice(text, 0, width)


def _clip_span(span: TextSpan, lower: int, upper: int) -> str:
    """The part of ``span``'s text lying inside ``[lower, upper)``.

    Cutting in UTF-16 space rather than by Python index is the whole point: the
    bounds are code units, so an emoji anywhere earlier in the run would shift a
    naive ``span.text[a:b]`` by a position per astral character.

    Never wider than the positions selected. A run whose text is longer than the
    range it declares is a wire inconsistency (:func:`_fit` normalises the same
    shape for the layout), and letting it through untrimmed here would make a
    window *non-monotonic*: widening the request by one unit could multiply the
    answer, because the fully-covered run would suddenly be returned whole. It
    is trimmed rather than padded, though — a run *shorter* than its range
    renders as what decoded, since filler is a position-holding device and this
    rendering holds no positions.

    A run that already fits its selection is returned untouched, which keeps a
    full-document render free of the per-run encode/decode round-trip
    :func:`_utf16_slice` costs.
    """
    start = max(span.start_index, lower)
    end = min(span.end_index, upper)
    if end <= start:
        return ""
    lead = start - span.start_index
    width = end - start
    if lead == 0 and utf16_len(span.text) <= width:
        return span.text
    return _utf16_slice(span.text, lead, lead + width)


class BlockKind(str, Enum):
    """Which variant of ``StructuralElement`` a :class:`DocumentBlock` came from.

    The wire's ``StructuralElement`` is a oneof: exactly one of ``paragraph``,
    ``table``, ``image``, ``codeBlock``, ``a2uiBlock``, ``thought``,
    ``functionCall``, ``functionResponse`` or ``horizontalRule`` is populated,
    and the member names here are that schema's
    (``docs/mobile/schema.proto``).

    This client decodes text out of ``PARAGRAPH`` and ``TABLE`` only.
    Everything else arrives with empty :attr:`DocumentBlock.spans` and its
    positions placeholder-filled in :attr:`StructuredDocument.text` — but for
    two different reasons, and the kind is how a caller tells them apart:

    * ``IMAGE`` and ``HORIZONTAL_RULE`` genuinely carry no text. The filler is
      inherent.
    * ``CODE_BLOCK`` and ``THOUGHT`` **do** carry text on the wire
      (``CodeBlock.content``, ``Thought.elements``) that this client does not
      decode yet. There the filler marks a real gap in what was recovered, not
      a gap in the document.

    The values are the member names, deliberately **not** the wire's oneof tag
    numbers: the recovered schema carries two copies of ``StructuralElement``
    with different tags, so a numeric value here would silently pick one and
    invite ``BlockKind(tag)`` lookups that are right only half the time. The
    two neighbouring enums (:class:`BlockStyle`, :class:`ListStyle`) *are*
    wire-valued and *are* looked up by value; this one is a label.
    """

    UNKNOWN = "unknown"
    PARAGRAPH = "paragraph"
    TABLE = "table"
    IMAGE = "image"
    CODE_BLOCK = "code_block"
    A2UI_BLOCK = "a2ui_block"
    THOUGHT = "thought"
    FUNCTION_CALL = "function_call"
    FUNCTION_RESPONSE = "function_response"
    HORIZONTAL_RULE = "horizontal_rule"


class BlockStyle(Enum):
    """Named paragraph style of a :class:`DocumentBlock`.

    Mirrors the backend's ``NamedStyleType`` enum (``docs/mobile/enums.txt``).
    ``UNSPECIFIED`` is both the wire's ``0`` and this client's fallback for an
    absent or unrecognised style slot — a block with no explicit style is body
    prose, which is also what ``NORMAL_TEXT`` means, so consumers that only care
    about "is this a heading" should use :attr:`DocumentBlock.heading_level`.
    """

    UNSPECIFIED = 0
    NORMAL_TEXT = 1
    TITLE = 2
    SUBTITLE = 3
    HEADING_1 = 4
    HEADING_2 = 5
    HEADING_3 = 6
    HEADING_4 = 7
    HEADING_5 = 8
    HEADING_6 = 9


class ListStyle(Enum):
    """Whether a list block is bulleted or numbered (wire ``ListType``)."""

    UNSPECIFIED = 0
    UNORDERED = 1
    ORDERED = 2


@dataclass(frozen=True)
class ListInfo:
    """List membership of a :class:`DocumentBlock` (wire ``BulletInfo``).

    Attributes:
        style: Bulleted (:attr:`ListStyle.UNORDERED`) or numbered
            (:attr:`ListStyle.ORDERED`).
        nesting_level: 0-based indentation depth.
        glyph: The rendered marker the backend chose — ``"•"`` for bullets,
            ``"1."`` / ``"2."`` … for ordered items. ``""`` when absent.
        ordinal: 1-based position within the innermost list, or ``None``.
    """

    style: ListStyle = ListStyle.UNSPECIFIED
    nesting_level: int = 0
    glyph: str = ""
    ordinal: int | None = None


@dataclass(frozen=True)
class TextSpan:
    """One run of text inside a block, with its character range and styling.

    ``start_index`` / ``end_index`` are a half-open range over
    :attr:`StructuredDocument.text` — not over the enclosing block's own text —
    measured in **UTF-16 code units**, the unit the backend counts in. Resolve
    them with :meth:`StructuredDocument.slice`.

    Attributes:
        start_index: Inclusive start offset in the document's coordinate space.
        end_index: Exclusive end offset.
        text: The run's literal text. Plain-text sources keep their newlines
            here; structured sources express layout as blocks instead.
        bold: Whether the run is bold.
        italic: Whether the run is italic.
        underline: Whether the run is underlined.
        url: Link target when the run is a hyperlink, else ``None``.
    """

    start_index: int
    end_index: int
    text: str
    bold: bool = False
    italic: bool = False
    underline: bool = False
    url: str | None = None

    def __post_init__(self) -> None:
        """Reject an impossible range, and neutralise unpaired surrogates.

        Sanitizing here rather than in each reader is what makes the guarantee
        hold everywhere: :attr:`DocumentBlock.text` and, through it,
        ``ChatReference.cited_text`` read span text directly, so a lone
        surrogate cleaned only inside :attr:`StructuredDocument.text` would
        still reach anyone who printed, logged or JSON-encoded a citation. One
        boundary, every downstream reading.

        The substitution is width-preserving (both are one UTF-16 code unit),
        so :attr:`start_index` / :attr:`end_index` stay true.
        """
        _validate_range("TextSpan", self.start_index, self.end_index)
        object.__setattr__(self, "text", _sanitize(self.text))


@dataclass(frozen=True)
class DocumentBlock:
    """One structural element of a document — a paragraph, heading or list item.

    Attributes:
        start_index: Inclusive start offset (UTF-16 code units) in
            :attr:`StructuredDocument.text`.
        end_index: Exclusive end offset.
        spans: The block's text runs, in document order. Empty for a block
            whose variant carries no text this client decodes — see
            :attr:`kind`.
        style: Named paragraph style (heading level, title, body prose).
        list_info: List membership when the block is a list item, else ``None``.
        kind: Which ``StructuralElement`` variant produced this block.
    """

    start_index: int
    end_index: int
    spans: tuple[TextSpan, ...] = ()
    style: BlockStyle = BlockStyle.UNSPECIFIED
    list_info: ListInfo | None = None
    kind: BlockKind = BlockKind.UNKNOWN

    def __post_init__(self) -> None:
        """Establish the three things a block guarantees about its own spans.

        1. **They are ordered by position.** The wire's container order is not
           authoritative — the offsets are — so a block whose spans arrive
           reversed must still read forwards.
        2. **They lie inside it.** A span is a *part of* a block, so one
           claiming a range beyond its parent is drift. Left alone, that drift
           travels: it displaces the layout cursor across the *next* block,
           whose spans are then dropped as already-covered, and a valid block
           reads back as a malformed neighbour's suffix. Clamping confines the
           damage to the block that carries it.
        3. **They are frozen for real.** ``frozen=True`` stops *rebinding* the
           attribute; it does not copy what was bound, so a caller passing a
           list keeps a live handle able to change this block's ``text`` and
           every ``slice`` taken from the enclosing document afterwards.

        Establishing all three *here* rather than in the layout is what makes
        them hold at every nesting level: table cells hold blocks
        too, and are flattened into their table's spans, so a rule enforced
        only at the top level would not reach them. It also means
        :attr:`text` — and through it ``ChatReference.cited_text``, which never
        goes near the layout — inherits the same guarantees.
        """
        _validate_range("DocumentBlock", self.start_index, self.end_index)
        object.__setattr__(self, "spans", self._normalised_spans())

    def _normalised_spans(self) -> tuple[TextSpan, ...]:
        """This block's spans, ordered by position and clamped to its range."""
        kept: list[TextSpan] = []
        for span in sorted(self.spans, key=lambda span: span.start_index):
            start = max(span.start_index, self.start_index)
            end = min(span.end_index, self.end_index)
            if end <= start:
                continue  # entirely outside the block that claims it
            if start == span.start_index and end == span.end_index:
                kept.append(span)
                continue
            lead = start - span.start_index
            kept.append(
                replace(
                    span,
                    start_index=start,
                    end_index=end,
                    text=_utf16_slice(span.text, lead, lead + (end - start)),
                )
            )
        return tuple(kept)

    @property
    def text(self) -> str:
        """The block's text — its spans concatenated, offsets preserved."""
        return "".join(span.text for span in self.spans)

    @property
    def heading_level(self) -> int | None:
        """Heading depth 1-6 for ``HEADING_1``…``HEADING_6``, else ``None``.

        :attr:`BlockStyle.TITLE` and :attr:`BlockStyle.SUBTITLE` are *not*
        headings for this purpose: they are document-level styles with no
        depth, so they report ``None`` rather than being flattened onto a
        level they do not have.
        """
        if BlockStyle.HEADING_1.value <= self.style.value <= BlockStyle.HEADING_6.value:
            return self.style.value - BlockStyle.HEADING_1.value + 1
        return None

    @property
    def is_list_item(self) -> bool:
        """Whether the block is a bulleted or numbered list item."""
        return self.list_info is not None


@dataclass(frozen=True)
class DocumentAnnotation:
    """An anchor tying a document object to a character range in the document.

    Wire ``AnnotationMapEntry``. On a chat answer, :attr:`object_id` is the
    citation's object id — the same value carried by
    :attr:`notebooklm.types.ChatReference.chunk_id` — and the range says which
    part of the answer that citation supports. Ranges are frequently zero-width
    (``start_index == end_index``): the backend anchors the citation at the
    insertion point of its ``[N]`` marker rather than over a span.

    Attributes:
        object_id: Id of the object being anchored (a citation's object id).
        start_index: Inclusive start offset (UTF-16 code units) in
            :attr:`StructuredDocument.text`.
        end_index: Exclusive end offset; equal to ``start_index`` for a
            zero-width anchor.
    """

    object_id: str
    start_index: int
    end_index: int

    def __post_init__(self) -> None:
        """Reject an anchor that identifies nothing or points nowhere."""
        if not self.object_id:
            raise ValueError("DocumentAnnotation object_id must be non-empty")
        _validate_range("DocumentAnnotation", self.start_index, self.end_index)


@dataclass(frozen=True)
class StructuredDocument:
    """A parsed ``TailwindDoc`` body: blocks, annotations and their text.

    This is the offset-bearing surface citation alignment is built on. The
    document's coordinate space is **not** that of
    :attr:`notebooklm.types.SourceFulltext.content` (a legacy newline-joined
    rendering, kept byte-identical for existing callers) and **not** that of
    :attr:`notebooklm.types.AskResult.answer` (which additionally carries
    markdown emphasis and inline ``[N]`` citation markers the document does
    not). Resolve an offset range with :meth:`slice`, which works from the
    blocks' own absolute offsets and is therefore correct whether or not the
    whole document decoded.

    Attributes:
        blocks: The document's structural elements, in document order.
        annotations: Object-to-range anchors. Empty for documents that carry no
            inline objects — a plain source has nothing to anchor.
    """

    blocks: tuple[DocumentBlock, ...] = ()
    annotations: tuple[DocumentAnnotation, ...] = ()

    def __post_init__(self) -> None:
        """Freeze the collections, and order the annotations by position.

        Freezing: see :meth:`DocumentBlock.__post_init__`.

        Ordering: :meth:`annotations_for` promises document order and
        ``attach_answer_anchors`` takes the *first* anchor for an object id, so
        an unordered container would silently select a different occurrence
        than the one documented. Blocks and spans already normalise by declared
        position; annotations are the third member of that family and were the
        one still trusting container order. The end index breaks ties so two
        anchors at the same start are ordered by extent rather than arbitrarily.
        """
        object.__setattr__(self, "blocks", tuple(self.blocks))
        object.__setattr__(
            self,
            "annotations",
            tuple(
                sorted(
                    self.annotations,
                    key=lambda entry: (entry.start_index, entry.end_index),
                )
            ),
        )

    @property
    def text(self) -> str:
        """The document's plain text, laid out at the backend's own offsets.

        Every span is placed at its declared ``start_index``, so this string is
        by construction the coordinate space every offset on this document
        refers to. **Read a range out of it with :meth:`slice`, not with
        ``text[n:m]``**: the offsets are UTF-16 code units (see
        :func:`utf16_len`), and Python indexes code points, so the two agree
        only until the document's first astral character — one emoji and
        everything after it is off by one.

        No separators are inserted between blocks, because the backend's
        offsets do not account for any: a block that ends at 108 is followed by
        one that starts at 108. Adding a newline would shift every subsequent
        offset and break the layout above, which is the #2128 defect this type
        exists to fix. Use :meth:`render` when you want a human-readable
        rendition instead of an addressable one.

        Positions the document occupies but whose text this client does not
        decode — an image, a horizontal rule (see :class:`BlockKind`) — are
        filled with ``"\\ufffc"`` (OBJECT REPLACEMENT CHARACTER), the same
        placeholder Google's own document APIs use for inline objects. Filling
        rather than skipping is what keeps every later offset correct; the
        character is chosen so that "something is here, and it is not text"
        stays visible instead of being silently papered over with a space.
        Use :attr:`DocumentBlock.text` when you want only what actually
        decoded.

        A span that overlaps one already placed is skipped rather than
        duplicated, so a malformed document yields a short read rather than a
        string longer than its own coordinate space, and the whole document is
        capped at :data:`_MAX_DOCUMENT_EXTENT` units.
        """
        limit = self.extent
        parts: list[str] = []
        cursor = 0
        for start, end, text in self._placed_spans():
            if end <= cursor or start >= limit:
                continue  # already covered, or past the extent cap
            if start > cursor:
                parts.append(_PLACEHOLDER * (start - cursor))
                cursor = start
            # Trim any overlap with what is already placed, then normalise the
            # run to the width it occupies. The trim is done in UTF-16 space,
            # like every other offset arithmetic here.
            trimmed = _utf16_slice(text, cursor - start, utf16_len(text))
            parts.append(_fit(trimmed, min(end, limit) - cursor))
            cursor = min(end, limit)
        if limit > cursor:
            parts.append(_PLACEHOLDER * (limit - cursor))
        return "".join(parts)

    def _placed_spans(self) -> list[_Placement]:
        """Every span as ``(start, end, text)``, in document order.

        Each block has already ordered and clamped its own spans
        (:meth:`DocumentBlock.__post_init__`), so all that is left here is to
        merge across blocks — whose container order is likewise not
        authoritative.
        """
        placed = [
            _Placement(span.start_index, span.end_index, span.text)
            for block in self.blocks
            for span in block.spans
        ]
        placed.sort(key=lambda placement: placement.start)
        return placed

    @property
    def extent(self) -> int:
        """The document's total width in UTF-16 units, capped for safety.

        This is the upper bound of the coordinate space every offset on this
        document lives in: a range is **in range** exactly when
        ``0 <= start < end <= extent``. That is necessary, not sufficient — a
        range inside the bound can still land on positions that decoded no text
        — and it catches only a range that no longer *fits*, not one that fits
        but no longer describes the same text. What it buys is the difference
        between an exact resolution and a plausible-looking wrong one: a caller
        that resolves an unchecked range gets a truncated or empty string back
        with no signal that anything was wrong. ``0`` for a document that
        decoded no blocks.

        The cap matters because :attr:`text` synthesizes filler from offsets the
        *server* chose: an ``int32``-scale ``start_index`` on a drifted row
        would otherwise turn a small response into a multi-gigabyte allocation,
        which no response-size limit can prevent because the bytes are made
        locally. Past :data:`_MAX_DOCUMENT_EXTENT` the document is truncated
        rather than materialised.
        """
        return min(max((block.end_index for block in self.blocks), default=0), _MAX_DOCUMENT_EXTENT)

    def render(self, start_index: int | None = None, end_index: int | None = None) -> str:
        """A readable flat rendering of the document, or of one range of it.

        The third of this library's three renderings of a source, and the only
        one built for *reading*:

        * :attr:`notebooklm.types.SourceFulltext.content` is the legacy flat
          rendering, frozen byte-for-byte. It joins every text **run** with
          ``"\\n"`` — and a run is a sub-paragraph fragment, so a paragraph the
          backend split into three runs becomes three lines. On the captured
          source in ``tests/unit/fixtures/source_fulltext_tailwind_doc.json``
          that turns 13 blocks into 17 lines, one of them a lone ``" "``.
        * :attr:`text` is the offset-faithful layout. It inserts no separators
          at all (that is what makes :meth:`slice` exact) and fills undecodable
          positions with ``"\\ufffc"``, so blocks run together and filler shows.
        * This method joins runs **within** a block and separates **blocks**,
          which is what the flat rendering was reaching for and could not do
          before the tree was parsed (#2128). It is not offset-addressable —
          the separators it inserts are its own — so anything positional still
          belongs on :meth:`slice`.

        A block that contributes no text is omitted rather than rendered as a
        blank line: an image or a horizontal rule has nothing to read, and
        empty strings are dropped by the legacy rendering too.

        Runs that overlap *inside* a block are trimmed against what the block
        has already rendered, so no line is wider than the window that selected
        it. Whole *blocks* that overlap each other each render in full, which
        is the one place this rendering deliberately differs from :attr:`text`:
        the layout de-duplicates because it must keep later offsets true, and
        this has no offsets to keep — dropping one of two blocks that both
        claim a range would hide text that decoded.

        The rendering is deliberately **marker-free**: a list item renders as
        its text, without ``list_info.glyph``, and a heading without any ``#``.
        This is the flat rendering ``content`` should always have been, not a
        markdown one; a decorated rendering is a future keyword argument rather
        than a change of what this returns. Read :attr:`blocks` when you want
        the structure.

        A :attr:`BlockKind.TABLE` renders as **one line with its cell text
        running together** — ``"Operating system"`` and ``"Android 10"``
        adjacent, in this library's captured infobox. That is not a choice made
        here: the parse flattens every row and cell into the table block's
        spans (#2128), and a cell is several runs rather than one, so nothing
        at this level can tell a cell boundary from a formatting change. It is
        the same reading :attr:`DocumentBlock.text` and
        ``ChatReference.cited_text`` already give. Giving tables their
        boundaries back means teaching the *parse* to keep them, which is
        `#2230 <https://github.com/teng-lin/notebooklm-py/issues/2230>`_.

        Args:
            start_index: Inclusive start of the range to render, in **UTF-16
                code units** (see :func:`utf16_len`), clamped up to ``0``.
            end_index: Exclusive end of the range, clamped down to
                :attr:`extent`.

        Returns:
            The rendered text, or ``""`` for an empty document or a range that
            selects nothing. A partially selected block renders only the part
            inside the range, so a window around a citation reads as a window
            rather than as whole surrounding blocks, and the result never
            carries more text than the range selects.

        Raises:
            ValueError: If exactly one bound is ``None``. Passing **neither**
                renders the whole document; passing **both** renders that
                range. A half-specified range is refused rather than guessed
                because the obvious guess is the dangerous one: :meth:`slice`
                takes the same optional pair and returns ``""`` when either is
                absent (a citation the answer did not annotate), so silently
                reading a missing bound as "unbounded" here would turn a
                substituted ``slice`` call into the whole document — a
                plausible-looking wrong answer, which is the failure mode this
                module exists to prevent.
        """
        if (start_index is None) != (end_index is None):
            raise ValueError(
                "StructuredDocument.render() takes both bounds or neither "
                f"(got start_index={start_index!r}, end_index={end_index!r}); "
                "use slice() if your range may be absent"
            )
        lower = 0 if start_index is None else max(0, start_index)
        upper = self.extent if end_index is None else min(end_index, self.extent)
        if upper <= lower:
            return ""
        lines: list[str] = []
        # Container order is not authoritative anywhere else in this module
        # (see :meth:`_placed_spans`), and reading order is the whole point
        # here, so order the blocks by position rather than by arrival.
        for block in sorted(self.blocks, key=lambda block: (block.start_index, block.end_index)):
            if block.end_index <= lower or block.start_index >= upper:
                continue
            # Trim each run against what the block has already rendered, the
            # same rule :attr:`text` applies across the document. A block's
            # spans are ordered and clamped to it but may still *overlap* each
            # other, and concatenating them then repeats the overlap — a line
            # wider than the window that selected it, reading as duplicated
            # citation text.
            parts: list[str] = []
            cursor = lower
            for span in block.spans:
                parts.append(_clip_span(span, max(lower, cursor), upper))
                cursor = max(cursor, min(span.end_index, upper))
            line = "".join(parts)
            if line:
                lines.append(line)
        return "\n".join(lines)

    def annotations_for(self, object_id: str) -> tuple[DocumentAnnotation, ...]:
        """All annotations anchoring ``object_id``, in document order."""
        return tuple(entry for entry in self.annotations if entry.object_id == object_id)

    def slice(self, start_index: int | None, end_index: int | None) -> str:
        """The document text over ``[start_index, end_index)``.

        Plain indexing of :attr:`text`, which is laid out at the backend's
        offsets — so this is exact rather than best-effort. Accepts ``None`` on
        either bound and returns ``""``, because the offsets callers pass in
        are optional (a :class:`~notebooklm.types.ChatReference` the answer did
        not annotate carries ``None``), and making every call site guard that
        would be the more likely source of a bug than the empty string is.
        An inverted, empty or wholly-negative range also returns ``""``;
        ``start_index`` is clamped up to 0 so a negative lower bound cannot be
        reinterpreted as a Python end-relative index.
        """
        if start_index is None or end_index is None:
            return ""
        start = max(0, start_index)
        if end_index <= start:
            return ""
        # Offsets are UTF-16 code units, so cut the UTF-16 encoding rather than
        # indexing the Python string — the two agree only until the document's
        # first astral character (an emoji), after which plain indexing drifts
        # by one position per such character. ``errors="replace"`` covers a
        # bound landing inside a surrogate pair, which the backend does not do
        # but a hand-built range could.
        return _utf16_slice(self.text, start, end_index)
