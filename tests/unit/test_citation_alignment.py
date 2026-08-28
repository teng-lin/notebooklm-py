"""Citation-alignment tests built on live-captured wire payloads (#2120, #2128).

Every fixture here is a **real** response body, not a hand-built row:

* ``tests/unit/fixtures/chat_answer_row_with_citations.json`` — the answer
  record (``inner_data[0]``) of a ``GenerateFreeFormStreamed`` response to a
  three-part question over a plain-text source. It carries a 536-character
  answer, the answer's own ``responseDoc`` with a 3-entry annotation map, and
  three citations whose fragments hold 12, 10 and 10 blocks.
* ``tests/unit/fixtures/source_fulltext_tailwind_doc.json`` — a ``GET_SOURCE``
  fulltext response for a markdown source, exercising every structural feature
  the flat extractor discarded: ``HEADING_1``/``2``/``3``, a bulleted list and
  a numbered list.

That provenance is the point. The bugs these tests cover survived a large
synthetic suite precisely because the synthetic rows encoded the code's belief
about the wire rather than the wire: a fixture shaped like
``[[start, end, payload]]`` "proved" the passage loop worked, when the real
payload nests one level deeper and made the loop run exactly once.

The numbers asserted below are the capture's own — 556 characters of cited
text where the pre-fix client returned 37, an annotation at ``[219, 235]``
reading ``"into electricity"`` — so a regression shows up as a wrong number
rather than as a fixture that quietly agrees with whatever the code does.
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest

from notebooklm._types import documents as documents_types
from notebooklm._web.rows.chat import AnswerRow, CitationDetail, CitationRow
from notebooklm._web.rows.chat_stream import (
    attach_answer_anchors,
    extract_fragment_range,
    extract_text_passages,
    parse_citations,
    parse_streaming_chat_response,
)
from notebooklm._web.rows.documents import build_blocks, build_document
from notebooklm.types import (
    BlockKind,
    BlockStyle,
    ChatReference,
    DocumentAnnotation,
    DocumentBlock,
    ListStyle,
    SourceFulltext,
    StructuredDocument,
    TableCell,
    TextSpan,
    utf16_len,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"

#: Object id of the first citation in the capture, and the annotation-map key
#: that anchors it to the answer.
FIRST_CITATION_OBJECT_ID = "7f930ec3-ab0e-4817-a541-dc8d496b7c67"
SOURCE_ID = "f0853641-471f-4b9f-a6de-1513dccb474a"


@pytest.fixture(scope="module")
def answer_row() -> list[Any]:
    """The captured streamed-chat answer record (``inner_data[0]``)."""
    return json.loads((FIXTURES_DIR / "chat_answer_row_with_citations.json").read_text())


@pytest.fixture(scope="module")
def source_result() -> list[Any]:
    """The captured ``GET_SOURCE`` fulltext response."""
    return json.loads((FIXTURES_DIR / "source_fulltext_tailwind_doc.json").read_text())


@pytest.fixture(scope="module")
def first_citation_detail(answer_row: list[Any]) -> list[Any]:
    """``cite[1]`` of the capture's first citation — a raw ``Citation``."""
    detail = CitationRow(AnswerRow(answer_row).citations[0]).detail
    assert detail is not None
    return detail.raw_list


# ---------------------------------------------------------------------------
# #2128 — the source document is parsed, not flattened
# ---------------------------------------------------------------------------


class TestSourceDocumentStructure:
    """The ``TailwindDoc`` tree keeps its structure and its offsets."""

    def test_headings_are_recovered_with_their_levels(self, source_result: list[Any]) -> None:
        document = build_document(source_result[3][0])
        headings = [(b.heading_level, b.text) for b in document.blocks if b.heading_level]
        assert headings == [
            (1, "Photosynthesis"),
            (2, "Light-dependent reactions"),
            (2, "The Calvin cycle"),
            (3, "Limiting factors"),
        ]

    def test_bulleted_and_numbered_lists_are_distinguished(self, source_result: list[Any]) -> None:
        document = build_document(source_result[3][0])
        items = [(b.list_info, b.text) for b in document.blocks if b.is_list_item]
        assert [(info.style, info.glyph, info.ordinal) for info, _ in items] == [
            (ListStyle.UNORDERED, "•", 1),
            (ListStyle.UNORDERED, "•", 2),
            (ListStyle.UNORDERED, "•", 3),
            (ListStyle.ORDERED, "1.", 1),
            (ListStyle.ORDERED, "2.", 2),
            (ListStyle.ORDERED, "3.", 3),
        ]
        assert [text for _, text in items][-1] == "Temperature"

    def test_body_prose_is_not_mistaken_for_a_heading(self, source_result: list[Any]) -> None:
        document = build_document(source_result[3][0])
        prose = next(b for b in document.blocks if b.text.startswith("Photosynthesis is"))
        assert prose.heading_level is None
        assert prose.is_list_item is False
        assert prose.style is BlockStyle.UNSPECIFIED

    def test_block_offsets_index_the_document_text(self, source_result: list[Any]) -> None:
        """The core #2128 property: offsets are usable, which they were not before.

        The flat rendering joined runs with ``"\\n"``, inserting separators the
        wire's offsets never accounted for; after that, no offset indexed
        anything. Here every block and every span slices back to its own text.
        """
        document = build_document(source_result[3][0])
        text = document.text
        assert len(text) == 532
        for block in document.blocks:
            assert text[block.start_index : block.end_index] == block.text
            for span in block.spans:
                assert text[span.start_index : span.end_index] == span.text

    def test_blocks_tile_the_coordinate_space_without_gaps(self, source_result: list[Any]) -> None:
        document = build_document(source_result[3][0])
        blocks = document.blocks
        assert blocks[0].start_index == 0
        assert blocks[-1].end_index == len(document.text)
        for previous, following in pairwise(blocks):
            assert following.start_index == previous.end_index

    def test_a_plain_source_carries_no_annotations(self, source_result: list[Any]) -> None:
        """Empty is the correct answer here, not a decode failure.

        Only documents with inline objects (a chat answer with citations)
        populate the map, so an empty tuple on a plain source is the contract —
        asserted so a future change that starts inventing annotations is caught.
        """
        assert build_document(source_result[3][0]).annotations == ()


class TestSourceFulltextIntegration:
    """``SourceFulltext`` gains the document without moving ``content``."""

    @pytest.mark.asyncio
    async def test_document_is_populated_and_content_is_unchanged(
        self, source_result: list[Any]
    ) -> None:
        from notebooklm._web.sources.content import SourceContentRenderer

        class _StubRpc:
            async def rpc_call(self, *args: Any, **kwargs: Any) -> Any:
                return source_result

        renderer = SourceContentRenderer(_StubRpc())
        fulltext = await renderer.get_fulltext("nb", SOURCE_ID)

        # ``content`` stays the legacy flat rendering, byte for byte: every
        # text run joined with "\n" in traversal order, mid-paragraph splits
        # and all. Existing callers must not move.
        expected_flat = "\n".join(renderer.extract_all_text(source_result[3][0]))
        assert fulltext.content == expected_flat
        assert fulltext.char_count == len(expected_flat)
        assert "Photosynthesis\nPhotosynthesis is the process" in fulltext.content

        # ...and the structured document is the offset-bearing surface.
        assert len(fulltext.document.blocks) == 13
        assert fulltext.document.text != fulltext.content
        assert len(fulltext.document.text) == 532
        # The offsets are usable against the document and not against content —
        # the whole of #2128 in two lines.
        assert fulltext.document.slice(108, 133) == "Light-dependent reactions"
        assert fulltext.content[108:133] != "Light-dependent reactions"

    @pytest.mark.asyncio
    async def test_markdown_format_still_carries_the_document(
        self, source_result: list[Any]
    ) -> None:
        """``output_format`` picks the flat rendering, not what the payload holds.

        The tree is in the response either way, so gating the parse on the text
        branch would leave a markdown caller unable to resolve citation offsets
        against a document that was right there.
        """
        from notebooklm._web.sources.content import SourceContentRenderer

        class _StubRpc:
            async def rpc_call(self, *args: Any, **kwargs: Any) -> Any:
                return source_result

        pytest.importorskip("markdownify")
        renderer = SourceContentRenderer(_StubRpc())
        fulltext = await renderer.get_fulltext("nb", SOURCE_ID, output_format="markdown")
        assert len(fulltext.document.blocks) == 13
        assert fulltext.document.slice(108, 133) == "Light-dependent reactions"


class TestOffsetResolution:
    """``slice`` resolves a range from the blocks' own offsets, always."""

    def test_slice_reads_the_documents_own_offsets(self, source_result: list[Any]) -> None:
        document = build_document(source_result[3][0])
        assert document.slice(108, 133) == document.text[108:133] == "Light-dependent reactions"

    def test_a_textless_block_holds_its_place_instead_of_shifting_the_rest(self) -> None:
        """The property that makes offsets usable at all (#2128).

        A block that occupies offsets without contributing text — an image, a
        rule — is filled rather than skipped. Skipping it would pull every
        later character backwards, which is exactly how the flat rendering lost
        the coordinate space.
        """
        document = StructuredDocument(
            blocks=(
                DocumentBlock(start_index=0, end_index=10, kind=BlockKind.IMAGE),
                DocumentBlock(
                    start_index=10,
                    end_index=15,
                    spans=(TextSpan(start_index=10, end_index=15, text="hello"),),
                    kind=BlockKind.PARAGRAPH,
                ),
            )
        )
        assert len(document.text) == 15
        assert document.slice(10, 15) == "hello"
        assert document.slice(0, 10) == "\ufffc" * 10

    def test_a_span_whose_text_disagrees_with_its_range_is_normalised(self) -> None:
        """A wire inconsistency must not shift every offset after it.

        Nothing forces ``len(span.text)`` to equal the width the span declares,
        and this repo's own VCR cassettes induce a mismatch by scrubbing names
        to a fixed-width placeholder. An over-long run is truncated and a short
        one padded, so the damage stays inside that one run.
        """
        document = StructuredDocument(
            blocks=(
                DocumentBlock(
                    start_index=0,
                    end_index=10,
                    spans=(
                        TextSpan(start_index=0, end_index=3, text="TOOLONG"),
                        TextSpan(start_index=3, end_index=10, text="short"),
                    ),
                ),
            )
        )
        assert len(document.text) == 10
        assert document.slice(0, 3) == "TOO"
        assert document.slice(3, 10) == "short\ufffc\ufffc"

    def test_out_of_order_and_overlapping_spans_do_not_inflate_the_text(self) -> None:
        """A malformed document yields a short read, never a long one."""
        document = StructuredDocument(
            blocks=(
                DocumentBlock(5, 10, (TextSpan(5, 10, "world"),)),
                DocumentBlock(0, 5, (TextSpan(0, 5, "hello"),)),
            )
        )
        assert document.text == "helloworld"
        overlapping = StructuredDocument(
            blocks=(
                DocumentBlock(0, 5, (TextSpan(0, 5, "aaaaa"),)),
                DocumentBlock(0, 5, (TextSpan(0, 5, "bbbbb"),)),
            )
        )
        assert overlapping.text == "aaaaa"

    @pytest.mark.parametrize("bounds", [(None, 5), (5, None), (None, None)])
    def test_slice_accepts_the_none_offsets_a_reference_may_carry(
        self, bounds: tuple[int | None, int | None]
    ) -> None:
        """``answer_anchor_*`` is ``None`` on an unannotated reference.

        Making every call site guard that is a likelier source of bugs than
        returning the empty string, so ``slice`` absorbs it.
        """
        document = StructuredDocument(blocks=(DocumentBlock(0, 3, (TextSpan(0, 3, "abc"),)),))
        assert document.slice(*bounds) == ""

    def test_slice_clips_a_partially_overlapping_span(self) -> None:
        document = StructuredDocument(
            blocks=(
                DocumentBlock(
                    start_index=0,
                    end_index=11,
                    spans=(TextSpan(start_index=0, end_index=11, text="hello world"),),
                    kind=BlockKind.PARAGRAPH,
                ),
            )
        )
        assert document.slice(6, 11) == "world"
        assert document.slice(0, 5) == "hello"
        assert document.slice(3, 8) == "lo wo"

    @pytest.mark.parametrize("bounds", [(5, 5), (9, 2), (-4, -1)])
    def test_empty_or_inverted_range_returns_empty(self, bounds: tuple[int, int]) -> None:
        document = StructuredDocument(
            blocks=(
                DocumentBlock(
                    start_index=0,
                    end_index=3,
                    spans=(TextSpan(start_index=0, end_index=3, text="abc"),),
                ),
            )
        )
        assert document.slice(*bounds) == ""

    def test_annotations_for_returns_every_anchor_of_one_object(self) -> None:
        document = StructuredDocument(
            annotations=(
                DocumentAnnotation("a", 1, 2),
                DocumentAnnotation("b", 3, 4),
                DocumentAnnotation("a", 5, 6),
            )
        )
        assert document.annotations_for("a") == (
            DocumentAnnotation("a", 1, 2),
            DocumentAnnotation("a", 5, 6),
        )
        assert document.annotations_for("missing") == ()


class TestReadableRendering:
    """``render()`` — the readable third rendering, derived from the tree (#2211).

    ``content`` joins text *runs*; ``text`` joins nothing at all because its
    offsets forbid it. Neither reads well, and the numbers below are the
    capture's own measure of how badly.
    """

    def test_render_reassembles_the_paragraph_the_flat_rendering_splits(
        self, source_result: list[Any]
    ) -> None:
        """13 blocks read as 13 lines, where the flat rendering makes 17.

        The capture's second block is one paragraph the backend emitted as
        three runs — ``"…light energy into"``, ``" "``, ``"chemical energy."``
        — and ``content`` puts each on its own line. That is the defect this
        rendering exists to fix, so both numbers are asserted: a render that
        stopped joining runs would read 17 too.
        """
        from notebooklm._web.sources.content import SourceContentRenderer

        document = build_document(source_result[3][0])
        flat = "\n".join(SourceContentRenderer(None).extract_all_text(source_result[3][0]))

        assert len(document.blocks) == 13
        assert len(flat.split("\n")) == 17
        assert len(document.render().split("\n")) == 13

        # The paragraph the flat rendering broke into three lines, whole.
        assert (
            "Photosynthesis is the process by which green plants convert light "
            "energy into chemical energy."
        ) in document.render()
        assert "light energy into\n \nchemical energy." in flat

    def test_render_is_the_readable_rendering_and_not_the_addressable_one(
        self, source_result: list[Any]
    ) -> None:
        """Its separators are its own, so it is deliberately not sliceable.

        The distinction the issue turns on: ``text`` is laid out at the
        backend's offsets (``slice(108, 133)`` is exactly what the backend
        meant), while ``render()`` inserts 12 newlines of its own and so
        answers a different question.
        """
        document = build_document(source_result[3][0])
        rendered = document.render()

        assert document.slice(108, 133) == "Light-dependent reactions"
        assert rendered[108:133] != "Light-dependent reactions"
        assert utf16_len(document.text) == document.extent == 532
        assert utf16_len(rendered) == 532 + 12  # one separator per block boundary
        assert "\n" not in document.text

    def test_a_window_renders_only_the_part_of_a_block_inside_it(
        self, source_result: list[Any]
    ) -> None:
        """A range that cuts through blocks reads as a window, not as whole blocks.

        This is what lets a citation window be tight around the cited span
        instead of spilling into the surrounding blocks entirely.
        """
        document = build_document(source_result[3][0])
        # 100..200 opens mid-paragraph (block 14-108) and closes mid-sentence
        # of the block at 183-216.
        assert document.render(100, 200) == (
            " energy.\nLight-dependent reactions\n"
            "These occur in the thylakoid membrane. Key points:\nWater is split, r"
        )
        # Same range on the addressable surface: identical text, no separators.
        assert document.slice(100, 200) == document.render(100, 200).replace("\n", "")

    def test_a_window_is_addressed_in_utf16_units_like_every_other_offset(self) -> None:
        """The bug class #2210 spent five rounds on, at this new surface.

        With an emoji ahead of it, a window read by Python index lands a
        character early and silently returns neighbouring text.
        """
        document = StructuredDocument(
            blocks=(
                DocumentBlock(0, 2, (TextSpan(0, 2, "\U0001f52c"),)),
                DocumentBlock(2, 7, (TextSpan(2, 7, "hello"),)),
                DocumentBlock(7, 12, (TextSpan(7, 12, "world"),)),
            )
        )
        assert document.extent == 12
        assert document.render(2, 7) == "hello"
        assert document.render(7, 12) == "world"
        # The same bounds read as Python code points land a character early:
        # the emoji is one character and two units, so everything after it
        # drifts by one.
        assert document.render().replace("\n", "")[7:12] == "orld"

    def test_a_window_cutting_into_a_span_cuts_it_in_utf16_units_too(self) -> None:
        """The same unit confusion, one level down — inside a single run.

        A window ending mid-block clips the run itself, and clipping by Python
        index drifts by exactly the same amount. It survives the block-level
        test above (which only ever selects whole runs), so it needs its own.
        """
        document = StructuredDocument(
            blocks=(DocumentBlock(0, 13, (TextSpan(0, 13, "\U0001f52c alpha beta"),)),)
        )
        assert document.extent == 13
        assert document.render(2, 8) == " alpha"
        # Clipping the same run by Python index starts a character late.
        assert "\U0001f52c alpha beta"[2:8] == "alpha "

    def test_a_window_cutting_a_surrogate_pair_in_half_does_not_leak_one(self) -> None:
        """A half-emoji must degrade to filler, not to an unencodable string.

        The cut is the caller's, not the wire's, so this shape is reachable
        from any window a caller chooses — and a lone surrogate reaching a
        reader moves the failure to whoever next prints or serializes it.
        """
        document = StructuredDocument(
            blocks=(DocumentBlock(0, 2, (TextSpan(0, 2, "\U0001f52c"),)),)
        )
        assert document.render(0, 1) == "￼"
        document.render(0, 1).encode("utf-8")

    def test_a_block_with_no_text_is_omitted_rather_than_rendered_blank(self) -> None:
        """Filler holds a *position*; a reader wants neither the filler nor a gap.

        ``text`` must fill an image's positions to keep later offsets true.
        ``render()`` has no offsets to keep, so it drops the block entirely
        instead of emitting a blank line or a ``"\\ufffc"``.
        """
        document = StructuredDocument(
            blocks=(
                DocumentBlock(0, 2, (TextSpan(0, 2, "ab"),)),
                DocumentBlock(2, 5, kind=BlockKind.IMAGE),
                DocumentBlock(5, 7, (TextSpan(5, 7, "cd"),)),
            )
        )
        assert document.text == "ab￼￼￼cd"  # three positions held open
        assert document.render() == "ab\ncd"

    def test_render_reads_forwards_however_the_blocks_arrived(self) -> None:
        """Container order is not authoritative anywhere else here, nor here."""
        document = StructuredDocument(
            blocks=(
                DocumentBlock(5, 10, (TextSpan(5, 10, "world"),)),
                DocumentBlock(0, 5, (TextSpan(0, 5, "hello"),)),
            )
        )
        assert document.render() == "hello\nworld"

    def test_runs_that_overlap_inside_a_block_are_trimmed_not_repeated(self) -> None:
        """A block clamps its runs to itself, but not to each other.

        Two runs claiming the same positions concatenate into a line twice as
        wide as the positions it covers, which reads as duplicated citation
        text. Trimmed against what the block has already rendered — the same
        rule the offset-faithful layout applies across the document.
        """
        document = StructuredDocument(
            blocks=(DocumentBlock(0, 5, (TextSpan(0, 5, "aaaaa"), TextSpan(0, 5, "bbbbb"))),)
        )
        assert document.text == "aaaaa"
        assert document.render() == "aaaaa"
        # Partial overlap: the second run contributes only the positions the
        # first left uncovered — three of its five units, cut in UTF-16 space.
        partial = StructuredDocument(
            blocks=(DocumentBlock(0, 8, (TextSpan(0, 5, "aaaaa"), TextSpan(3, 8, "bbbbb"))),)
        )
        assert partial.render() == "aaaaabbb"
        assert partial.render() == partial.text  # the layout reaches the same reading

    def test_a_half_specified_window_is_refused_rather_than_guessed(self) -> None:
        """The one place ``render`` and ``slice`` would silently disagree.

        ``slice`` takes the same optional pair and returns ``""`` when either
        bound is absent, because a citation the answer did not annotate carries
        ``None``. Reading a missing bound here as "unbounded" would turn a
        substituted ``slice`` call into the whole document — the plausible
        wrong answer this module exists to prevent — so it raises instead.
        """
        document = StructuredDocument(blocks=(DocumentBlock(0, 5, (TextSpan(0, 5, "hello"),)),))
        for start, end in ((0, None), (None, 5)):
            with pytest.raises(ValueError, match="both bounds or neither"):
                document.render(start, end)
        assert document.slice(None, 5) == document.slice(0, None) == ""
        assert document.render() == "hello"

    def test_a_run_wider_than_the_range_it_declares_is_trimmed_to_it(self) -> None:
        """A rendering may not return text outside the space it renders from.

        The wire has never sent this (every capture has ``utf16_len(text) ==
        end_index - start_index``), but returning the run whole would let a
        window come back wider than the range asked for, and make widening a
        window by one unit multiply its answer. Trimmed, not padded: a run
        *short* of its range renders as what decoded, since filler holds
        positions and this rendering holds none.
        """
        too_long = StructuredDocument(
            blocks=(DocumentBlock(0, 2, (TextSpan(0, 2, "much too long"),)),)
        )
        assert too_long.text == "mu"
        assert too_long.render() == too_long.render(0, 2) == "mu"
        assert too_long.render(0, 1) == "m"

        too_short = StructuredDocument(blocks=(DocumentBlock(0, 10, (TextSpan(0, 10, "short"),)),))
        assert too_short.text == "short￼￼￼￼￼"  # the layout pads to hold the positions
        assert too_short.render() == "short"  # the rendering has none to hold

    def test_list_markers_and_heading_levels_are_deliberately_not_rendered(
        self, source_result: list[Any]
    ) -> None:
        """This is the flat rendering ``content`` should have been, not markdown.

        The capture's numbered list carries ``ListInfo(glyph="1."…)`` and its
        heading a ``HEADING_3`` style, and the rendering emits neither — a
        decision worth pinning, so that adding markers is a deliberate change
        (a keyword argument) rather than a silent one. The structure stays
        available on ``blocks`` for callers that want to render it themselves.
        """
        document = build_document(source_result[3][0])
        ordered = [block for block in document.blocks if block.is_list_item]
        assert [block.list_info.glyph for block in ordered if block.list_info] == [
            "•",
            "•",
            "•",
            "1.",
            "2.",
            "3.",
        ]
        rendered = document.render().split("\n")
        assert "Limiting factors" in rendered  # a HEADING_3, rendered bare
        assert "Temperature" in rendered  # an item whose glyph is "3."
        assert not any(line.startswith(("•", "1.", "#")) for line in rendered)

    def test_an_empty_or_inverted_window_renders_nothing(self) -> None:
        document = StructuredDocument(blocks=(DocumentBlock(0, 5, (TextSpan(0, 5, "hello"),)),))
        assert document.render(3, 3) == ""
        assert document.render(4, 2) == ""
        assert document.render(9, 20) == ""
        assert StructuredDocument().render() == ""
        # A negative lower bound is clamped, not reinterpreted end-relative.
        assert document.render(-4, 5) == "hello"

    def test_a_table_renders_one_line_per_row_with_its_cells_separated(self) -> None:
        """The #2230 shape, isolated from the capture.

        A table is the one block kind that is not one line: its rows are the
        lines and its cells are separated within them. Both boundaries come
        from ``table_rows`` — a cell is several runs, so neither is recoverable
        from the spans, which is exactly why the parse carries them.
        """
        document = StructuredDocument(
            blocks=(
                DocumentBlock(
                    0,
                    16,
                    (
                        TextSpan(0, 2, "OS"),
                        # Two runs, one cell — separating runs would split it.
                        TextSpan(2, 9, "Android"),
                        TextSpan(9, 12, " 10"),
                        TextSpan(12, 16, "Cost"),
                    ),
                    kind=BlockKind.TABLE,
                    table_rows=(
                        (TableCell(0, 2), TableCell(2, 12)),
                        (TableCell(12, 16),),
                    ),
                ),
            )
        )
        assert document.render() == "OS\tAndroid 10\nCost"
        # The layout is untouched: no separator, one line, same offsets.
        assert document.text == "OSAndroid 10Cost"
        assert document.blocks[0].text == "OSAndroid 10Cost"

    def test_a_table_row_renders_only_the_cells_the_window_selects(self) -> None:
        """A window through a table stays a window, as it does through prose.

        Rows and cells outside the range contribute nothing and are dropped
        with their separators, so a citation window over one cell does not read
        as the whole grid.
        """
        document = StructuredDocument(
            blocks=(
                DocumentBlock(
                    0,
                    12,
                    (TextSpan(0, 3, "aaa"), TextSpan(3, 6, "bbb"), TextSpan(6, 12, "cccddd")),
                    kind=BlockKind.TABLE,
                    table_rows=(
                        (TableCell(0, 3), TableCell(3, 6)),
                        (TableCell(6, 9), TableCell(9, 12)),
                    ),
                ),
            )
        )
        assert document.render() == "aaa\tbbb\nccc\tddd"
        assert document.render(0, 6) == "aaa\tbbb"
        assert document.render(6, 12) == "ccc\tddd"
        # A window inside one cell renders that cell alone, no separator.
        assert document.render(4, 6) == "bb"
        # A row the window misses entirely is dropped, not rendered blank.
        assert document.render(0, 3) == "aaa"

    def test_an_empty_cell_keeps_its_column_but_not_a_trailing_separator(self) -> None:
        """Where the separator survives an empty cell, and where it does not.

        A *leading* empty cell keeps its separator, so a value stays in the
        column it was written in rather than sliding left into a neighbour's. A
        *trailing* one has no column to hold open, so it drops its separator
        with it — and a row that is empty all the way across drops entirely,
        like any other block with nothing to read.
        """
        document = StructuredDocument(
            blocks=(
                DocumentBlock(
                    0,
                    9,
                    (TextSpan(3, 6, "mid"),),
                    kind=BlockKind.TABLE,
                    table_rows=(
                        (TableCell(0, 3), TableCell(3, 6), TableCell(6, 9)),
                        (TableCell(6, 9), TableCell(6, 9)),
                    ),
                ),
            )
        )
        assert document.render() == "\tmid"

    def test_an_empty_column_holds_its_place_instead_of_shifting_the_row(self) -> None:
        """A cell with no characters is still a column (#2230 review).

        An empty cell's natural range is zero-width, and dropping it would
        slide every value after it one column left — a three-column row of
        ``A`` / *nothing* / ``C`` reading as ``A\\tC``, which says ``C`` is in
        column two. The boundary is kept; only a row that is *nothing but*
        empty columns goes, since it has no positions to hold.
        """
        block = DocumentBlock(
            0,
            2,
            (TextSpan(0, 1, "A"), TextSpan(1, 2, "C")),
            kind=BlockKind.TABLE,
            table_rows=(
                (TableCell(0, 1), TableCell(1, 1), TableCell(1, 2)),
                (TableCell(2, 2), TableCell(2, 2)),  # nothing but empty columns
            ),
        )
        assert block.table_rows == ((TableCell(0, 1), TableCell(1, 1), TableCell(1, 2)),)
        assert StructuredDocument(blocks=(block,)).render() == "A\t\tC"

    def test_a_cells_own_tab_survives_the_row_join(self) -> None:
        """Trailing empty *cells* are dropped, not trailing separator characters.

        A run keeps its own whitespace (:attr:`TextSpan.text` is the literal
        run), so a cell can legitimately end in a tab. Stripping the joined
        line would eat that character along with the separator, silently
        editing the source's text — which a rendering may never do.

        Both review findings meet in one row here: an empty middle column that
        must keep its separator, and a final cell whose own tab must survive
        the rule that removes trailing ones. Hand-built rather than taken from
        a capture because no capture in this repo contains either shape — the
        infobox's cells are all non-empty and none ends in a tab.
        """
        document = StructuredDocument(
            blocks=(
                DocumentBlock(
                    0,
                    3,
                    (TextSpan(0, 1, "A"), TextSpan(1, 3, "C\t")),
                    kind=BlockKind.TABLE,
                    table_rows=((TableCell(0, 1), TableCell(1, 1), TableCell(1, 3)),),
                ),
            )
        )
        assert document.render() == "A\t\tC\t"

    def test_cells_are_ordered_and_clamped_like_every_other_offset_here(self) -> None:
        """``table_rows`` gets the guarantees ``spans`` gets, for the same reasons.

        Container order is not authoritative; a cell claiming positions its
        block does not own is drift and is clamped to the block, or dropped
        when nothing survives; and a row left with no cells goes with them.
        """
        block = DocumentBlock(
            0,
            6,
            (TextSpan(0, 3, "abc"), TextSpan(3, 6, "def")),
            kind=BlockKind.TABLE,
            table_rows=(
                (TableCell(20, 30),),  # wholly outside — dropped, and its row with it
                (TableCell(3, 9),),  # overruns the block — clamped to 3..6
                (TableCell(0, 3),),  # arrives after the row that follows it
            ),
        )
        assert block.table_rows == ((TableCell(0, 3),), (TableCell(3, 6),))
        assert StructuredDocument(blocks=(block,)).render() == "abc\ndef"

    def test_rows_that_interleave_degrade_to_the_stray_line_not_to_a_wrong_cell(
        self,
    ) -> None:
        """The bound on the forward walk's monotonicity assumption (#2230 review).

        :func:`_spans_by_cell` walks the flattened cells once, so its cursor
        assumes they advance. Rows are ordered by position and cells within a
        row are too, but a hand-built block can still *interleave* them — a
        later row starting inside an earlier one — and no decoded document can
        (row and cell bounds come from successive narrowings of the table's own
        range).

        What matters is which way it fails. A run is only ever assigned to a
        cell that actually contains its start, so it can never be rendered in
        the wrong column; the cursor can only make it match *fewer* cells than
        it overlaps, and a run matching none becomes the trailing stray line.
        Fewer columns and a visible extra line, never a value silently sitting
        under the wrong header.
        """
        document = StructuredDocument(
            blocks=(
                DocumentBlock(
                    0,
                    30,
                    (
                        TextSpan(0, 5, "aaaaa"),
                        TextSpan(10, 15, "bbbbb"),
                        TextSpan(20, 30, "cccccccccc"),
                    ),
                    kind=BlockKind.TABLE,
                    # The second row opens inside the first row's span.
                    table_rows=((TableCell(0, 5), TableCell(20, 30)), (TableCell(10, 15),)),
                ),
            )
        )
        assert document.render() == "aaaaa\tcccccccccc\nbbbbb"
        # Every run still reaches the rendering, and the layout is untouched.
        assert document.text == "aaaaa￼￼￼￼￼bbbbb￼￼￼￼￼cccccccccc"

    def test_a_run_no_cell_claims_is_rendered_rather_than_dropped(self) -> None:
        """The regrouping may not lose text, however inconsistent its input.

        Unreachable from a decoded document — every run in a table block was
        read out of one of its cells — but ``DocumentBlock`` is public and can
        be built with cells that do not cover its spans. Such a run renders
        after the grid rather than vanishing into it.
        """
        document = StructuredDocument(
            blocks=(
                DocumentBlock(
                    0,
                    9,
                    (TextSpan(0, 3, "aaa"), TextSpan(3, 6, "bbb"), TextSpan(6, 9, "ccc")),
                    kind=BlockKind.TABLE,
                    table_rows=((TableCell(0, 3), TableCell(6, 9)),),
                ),
            )
        )
        assert document.render() == "aaa\tccc\nbbb"
        # It is still a windowed rendering: a window that excludes the stray
        # drops its line rather than emitting a blank one.
        assert document.render(0, 3) == "aaa"

    @pytest.mark.asyncio
    async def test_fulltext_exposes_the_rendering_without_moving_content(
        self, source_result: list[Any]
    ) -> None:
        """``SourceFulltext.rendered_content`` is additive: ``content`` is untouched."""
        from notebooklm._web.sources.content import SourceContentRenderer

        class _StubRpc:
            async def rpc_call(self, *args: Any, **kwargs: Any) -> Any:
                return source_result

        renderer = SourceContentRenderer(_StubRpc())
        fulltext = await renderer.get_fulltext("nb", SOURCE_ID)

        # The capture's own first three lines, quoted rather than re-derived
        # from the extractor: the paragraph arrives split across two of them.
        assert fulltext.content.startswith(
            "Photosynthesis\n"
            "Photosynthesis is the process by which green plants convert light energy into\n"
            " \n"
        )
        assert fulltext.rendered_content.startswith(
            "Photosynthesis\n"
            "Photosynthesis is the process by which green plants convert light "
            "energy into chemical energy.\n"
        )
        assert fulltext.rendered_content == fulltext.document.render()
        assert len(fulltext.rendered_content.split("\n")) == 13
        assert len(fulltext.content.split("\n")) == 17
        # A size over ``content``, deliberately not recomputed for the new view.
        assert fulltext.char_count == len(fulltext.content) == 548

    def test_rendered_content_is_empty_when_no_document_decoded(self) -> None:
        """Strictly structural: it never falls back to flattening ``content``.

        A source whose text arrived as bare strings has ``content`` and no
        blocks; reporting ``content`` here would make the field mean two
        different things depending on the response shape.
        """
        fulltext = SourceFulltext(source_id="s", title="t", content="some text", char_count=9)
        assert fulltext.content == "some text"
        assert fulltext.rendered_content == ""


def _tiles_exactly(block: DocumentBlock) -> bool:
    """Whether ``block``'s spans cover it exactly, once each, at their declared widths.

    The well-formedness predicate both adversarial invariants below are stated
    against: a block that tiles is one the layout and the rendering have no
    reason to normalise, so it must read back verbatim through either. Every
    live capture tiles; the shapes that do not are the ones review invented.
    """
    spans = block.spans
    return bool(
        spans
        and spans[0].start_index == block.start_index
        and spans[-1].end_index == block.end_index
        and all(later.start_index == earlier.end_index for earlier, later in pairwise(spans))
        and all(utf16_len(span.text) == span.end_index - span.start_index for span in spans)
    )


#: Adversarial documents, each built to break one assumption the layout makes
#: about wire-supplied offsets. Every entry is a shape review found (or the
#: neighbouring shape it implied) across five rounds on #2210.
_ADVERSARIAL_DOCUMENTS: tuple[tuple[str, StructuredDocument], ...] = (
    (
        "span-overflows-its-block",
        lambda: StructuredDocument(
            blocks=(
                DocumentBlock(0, 2, (TextSpan(0, 4, "BAAD"),)),
                DocumentBlock(2, 4, (TextSpan(2, 4, "ok"),)),
            )
        ),
    ),
    (
        "span-starts-before-its-block",
        lambda: StructuredDocument(blocks=(DocumentBlock(4, 6, (TextSpan(0, 6, "abcdef"),)),)),
    ),
    (
        "span-entirely-outside-its-block",
        lambda: StructuredDocument(blocks=(DocumentBlock(0, 2, (TextSpan(8, 10, "xx"),)),)),
    ),
    (
        "blocks-out-of-order",
        lambda: StructuredDocument(
            blocks=(
                DocumentBlock(5, 10, (TextSpan(5, 10, "world"),)),
                DocumentBlock(0, 5, (TextSpan(0, 5, "hello"),)),
            )
        ),
    ),
    (
        "blocks-overlap",
        lambda: StructuredDocument(
            blocks=(
                DocumentBlock(0, 5, (TextSpan(0, 5, "aaaaa"),)),
                DocumentBlock(0, 5, (TextSpan(0, 5, "bbbbb"),)),
            )
        ),
    ),
    (
        "gap-between-blocks",
        lambda: StructuredDocument(
            blocks=(
                DocumentBlock(0, 2, (TextSpan(0, 2, "ab"),)),
                DocumentBlock(9, 11, (TextSpan(9, 11, "cd"),)),
            )
        ),
    ),
    (
        "text-shorter-than-its-range",
        lambda: StructuredDocument(blocks=(DocumentBlock(0, 10, (TextSpan(0, 10, "short"),)),)),
    ),
    (
        "text-longer-than-its-range",
        lambda: StructuredDocument(
            blocks=(DocumentBlock(0, 2, (TextSpan(0, 2, "much too long"),)),)
        ),
    ),
    (
        "astral-character",
        lambda: StructuredDocument(blocks=(DocumentBlock(0, 2, (TextSpan(0, 2, "\U0001f52c"),)),)),
    ),
    (
        "astral-split-by-a-bad-width",
        lambda: StructuredDocument(blocks=(DocumentBlock(0, 1, (TextSpan(0, 1, "\U0001f52c"),)),)),
    ),
    (
        "inbound-lone-surrogate",
        lambda: StructuredDocument(
            blocks=(DocumentBlock(0, 4, (TextSpan(0, 4, json.loads('"\\ud800abc"')),)),)
        ),
    ),
    (
        "textless-block-between-two-real-ones",
        lambda: StructuredDocument(
            blocks=(
                DocumentBlock(0, 2, (TextSpan(0, 2, "ab"),)),
                DocumentBlock(2, 5, kind=BlockKind.IMAGE),
                DocumentBlock(5, 7, (TextSpan(5, 7, "cd"),)),
            )
        ),
    ),
    (
        "spans-out-of-order-within-a-block",
        lambda: StructuredDocument(
            blocks=(DocumentBlock(0, 10, (TextSpan(5, 10, "world"), TextSpan(0, 5, "hello"))),)
        ),
    ),
    (
        # A block clamps its spans to itself but not to each other, so two runs
        # can claim the same positions. Concatenating them repeats the overlap.
        "spans-overlap-within-a-block",
        lambda: StructuredDocument(
            blocks=(DocumentBlock(0, 5, (TextSpan(0, 5, "aaaaa"), TextSpan(0, 5, "bbbbb"))),)
        ),
    ),
    ("empty-document", StructuredDocument),
)


class TestLayoutInvariantsUnderAdversarialInput:
    """Properties that must hold for *any* document, however malformed.

    Five review rounds on #2210 each found the layout trusting wire-supplied
    offsets in one more way, and each was fixed as its own instance. These are
    the invariants those instances were special cases of, asserted over every
    adversarial shape at once — so the next such defect fails here rather than
    in review.
    """

    @pytest.mark.parametrize(
        ("label", "factory"), _ADVERSARIAL_DOCUMENTS, ids=[n for n, _ in _ADVERSARIAL_DOCUMENTS]
    )
    def test_text_is_exactly_the_documents_extent(self, label: str, factory: Any) -> None:
        """The layout fills its coordinate space exactly — no more, no less.

        Short means later offsets resolve to earlier text; long means the
        document claims positions the blocks never justified.
        """
        document = factory()
        expected = max((block.end_index for block in document.blocks), default=0)
        assert utf16_len(document.text) == expected

    @pytest.mark.parametrize(
        ("label", "factory"), _ADVERSARIAL_DOCUMENTS, ids=[n for n, _ in _ADVERSARIAL_DOCUMENTS]
    )
    def test_text_is_always_encodable_and_serializable(self, label: str, factory: Any) -> None:
        """Whatever comes out must survive being printed, logged or serialized."""
        text = factory().text
        # Must not *raise*; an empty document legitimately encodes to b"".
        text.encode("utf-8")
        json.dumps(text, ensure_ascii=False)

    @pytest.mark.parametrize(
        ("label", "factory"), _ADVERSARIAL_DOCUMENTS, ids=[n for n, _ in _ADVERSARIAL_DOCUMENTS]
    )
    def test_a_window_never_renders_more_text_than_it_selects(
        self, label: str, factory: Any
    ) -> None:
        """The rendering stays inside the coordinate space it renders from.

        No block may come back wider than the window that selected it. Without
        this a run whose text is wider than its declared range escapes: the
        window returns text the caller's range does not cover, and widening the
        request by one unit can multiply the answer — the fully-covered run
        being suddenly returned whole. That is the drift class ``_fit`` already
        contains for :attr:`text`.

        Stated per line rather than over the whole string because overlapping
        blocks (a malformed document) each render in full, by design: the
        rendering holds no offsets, so it has nothing to keep true by
        de-duplicating, and dropping one of two blocks that both claim a range
        would hide text that decoded.
        """
        document = factory()
        limit = document.extent
        for start in range(limit + 2):
            for end in range(start, limit + 2):
                rendered = document.render(start, end)
                for line in rendered.split("\n") if rendered else []:
                    assert utf16_len(line) <= end - start

    @pytest.mark.parametrize(
        ("label", "factory"), _ADVERSARIAL_DOCUMENTS, ids=[n for n, _ in _ADVERSARIAL_DOCUMENTS]
    )
    def test_render_never_emits_a_blank_line(self, label: str, factory: Any) -> None:
        """A block with nothing to read is dropped, not turned into a gap.

        Whole-document and windowed: a window that clips a block down to
        nothing must drop it too, or a tight citation window fills with blank
        lines from its neighbours.
        """
        document = factory()
        for start in range(document.extent + 2):
            for end in range(start, document.extent + 2):
                rendered = document.render(start, end)
                assert "" not in (rendered.split("\n") if rendered else [])

    @pytest.mark.parametrize(
        ("label", "factory"), _ADVERSARIAL_DOCUMENTS, ids=[n for n, _ in _ADVERSARIAL_DOCUMENTS]
    )
    def test_well_formed_blocks_read_back_verbatim_and_in_order(
        self, label: str, factory: Any
    ) -> None:
        """Damage stays inside the block that carries it — the render's copy.

        The sibling assertion for :meth:`slice`, stated on the readable
        surface: every block whose spans tile it exactly must appear as its own
        line, and those lines must appear in position order however the blocks
        arrived. Restricted to blocks that tile — the same predicate the
        ``slice`` sibling uses — because a malformed one is legitimately
        normalised: an over-wide run is trimmed to its range, and runs that
        overlap each other are trimmed against what the block already
        rendered. Stated as a subsequence, so it says nothing about what the
        neighbours render as.
        """
        document = factory()
        expected = [
            block.text
            for block in sorted(document.blocks, key=lambda b: (b.start_index, b.end_index))
            if block.text and _tiles_exactly(block)
        ]
        lines = iter(document.render().split("\n"))
        for text in expected:
            assert any(line == text for line in lines), (
                f"{text!r} missing from the render, or out of position order"
            )

    @pytest.mark.parametrize(
        ("label", "factory"), _ADVERSARIAL_DOCUMENTS, ids=[n for n, _ in _ADVERSARIAL_DOCUMENTS]
    )
    def test_render_is_always_encodable_and_serializable(self, label: str, factory: Any) -> None:
        """Whatever comes out must survive being printed, logged or serialized.

        The same guarantee ``text`` carries: a lone surrogate reaching a reader
        moves the failure to whoever next encodes it. Windowed as well as
        whole, because the cut itself can manufacture one.
        """
        document = factory()
        extent = document.extent
        for start in range(extent + 2):
            for end in range(start, extent + 2):
                rendered = document.render(start, end)
                rendered.encode("utf-8")
                json.dumps(rendered, ensure_ascii=False)

    @pytest.mark.parametrize(
        ("label", "factory"), _ADVERSARIAL_DOCUMENTS, ids=[n for n, _ in _ADVERSARIAL_DOCUMENTS]
    )
    def test_slice_never_exceeds_the_range_it_was_asked_for(self, label: str, factory: Any) -> None:
        document = factory()
        extent = utf16_len(document.text)
        for start in range(extent + 2):
            for end in range(start, extent + 2):
                assert utf16_len(document.slice(start, end)) <= end - start

    @pytest.mark.parametrize(
        ("label", "factory"), _ADVERSARIAL_DOCUMENTS, ids=[n for n, _ in _ADVERSARIAL_DOCUMENTS]
    )
    def test_a_malformed_block_cannot_corrupt_a_well_formed_neighbour(
        self, label: str, factory: Any
    ) -> None:
        """Damage stays inside the block that carries it.

        Every block whose own spans tile it exactly must read back verbatim,
        no matter what its neighbours claim. This is the invariant the
        span-overflow defect broke: a bad span in block N replaced the text of
        block N+1.
        """
        document = factory()
        for block in document.blocks:
            tiles = _tiles_exactly(block)
            overlapped = any(
                other is not block
                and other.start_index < block.end_index
                and other.end_index > block.start_index
                for other in document.blocks
            )
            if tiles and not overlapped:
                assert document.slice(block.start_index, block.end_index) == block.text


class TestFragmentInvariantsUnderAdversarialInput:
    """The same properties for ``cited_text``, which never touches the layout.

    ``ChatReference.cited_text`` concatenates ``DocumentBlock.text`` directly,
    so every guarantee established only inside ``StructuredDocument`` misses
    it. That gap produced two separate review findings; these assert the
    fragment surface on its own terms.
    """

    @pytest.mark.parametrize(
        ("label", "fragment", "expected_text", "expected_range"),
        [
            (
                "blocks-out-of-order",
                [[[5, 10, [[[5, 10, ["world"]]]]], [0, 5, [[[0, 5, ["hello"]]]]]]],
                "helloworld",
                (0, 10),
            ),
            (
                "spans-out-of-order-within-a-block",
                [[[0, 10, [[[5, 10, ["world"]], [0, 5, ["hello"]]]]]]],
                "helloworld",
                (0, 10),
            ),
            (
                "span-overflows-its-block",
                [[[0, 2, [[[0, 4, ["BAAD"]]]]], [2, 4, [[[2, 4, ["ok"]]]]]]],
                "BAok",
                (0, 4),
            ),
        ],
    )
    def test_cited_text_reads_in_declared_offset_order(
        self,
        label: str,
        fragment: Any,
        expected_text: str,
        expected_range: tuple[int, int],
    ) -> None:
        """Container order is not authoritative; the offsets are.

        A reordered fragment concatenated in arrival order reads backwards
        while its own ``start_char`` / ``end_char`` say otherwise — the text
        and the range it claims would describe different things.
        """
        cited_text, start_char, end_char = extract_text_passages([None, None, None, None, fragment])
        assert cited_text == expected_text
        assert (start_char, end_char) == expected_range

    @pytest.mark.parametrize(
        ("label", "bad_cell"),
        [
            # The malformed range is on the *span*, inside a well-formed block.
            ("span-overflows-its-cell", [0, 2, [[0, 2, [[[0, 4, ["BAAD"]]]]]]]),
            # The malformed range is on the nested *block* itself, so it is
            # self-consistent and only its container knows better.
            ("block-overflows-its-cell", [0, 2, [[0, 4, [[[0, 4, ["BAAD"]]]]]]]),
        ],
    )
    def test_a_nested_table_block_cannot_corrupt_its_sibling(
        self, label: str, bad_cell: Any
    ) -> None:
        """The containment rule has to apply at every level of the nesting.

        A table's cells hold blocks of their own, flattened into the table's
        spans, so a rule enforced only on top-level blocks never reaches them.
        Both parametrisations matter and needed separate fixes: clamping spans
        to their block leaves a *block* that overruns its cell untouched,
        because that block is perfectly consistent with itself.
        """
        # Table = [rows, columns, tableRows]; TableRow = [start, end, cells];
        # TableCell = [start, end, content] where content is StructuralElements.
        good_cell = [2, 4, [[2, 4, [[[2, 4, ["ok"]]]]]]]
        table_element = [0, 4, None, None, [1, 2, [[0, 4, [bad_cell, good_cell]]]]]
        (block,) = build_blocks([table_element])
        assert block.kind is BlockKind.TABLE
        assert block.text == "BAok"
        assert StructuredDocument(blocks=(block,)).slice(2, 4) == "ok"


class TestDocumentDegradation:
    """Malformed tree nodes degrade to defaults rather than raising."""

    def test_non_list_body_yields_an_empty_document(self) -> None:
        for body in ("not a list", None, 42, {}):
            assert build_document(body) == StructuredDocument()

    @pytest.mark.parametrize("elements", ["not a list", None, 42, {}])
    def test_non_list_elements_yield_no_blocks(self, elements: Any) -> None:
        """The bare-element-list entry point degrades the same way."""
        assert build_blocks(elements) == ()

    def test_bullet_info_without_a_sparse_object_keeps_defaults(self) -> None:
        """``BulletInfo``'s high tags are optional; their absence is not drift."""
        elements = [[0, 3, [[[0, 3, ["abc"]]], None, None, [None, None, 2]]]]
        (block,) = build_blocks(elements)
        assert block.list_info is not None
        assert block.list_info.style is ListStyle.UNSPECIFIED
        assert block.list_info.nesting_level == 2
        assert block.list_info.glyph == ""
        assert block.list_info.ordinal is None

    def test_non_list_bullet_info_is_not_a_list_item(self) -> None:
        elements = [[0, 3, [[[0, 3, ["abc"]]], None, None, "not-a-list"]]]
        (block,) = build_blocks(elements)
        assert block.list_info is None

    def test_unrecognised_list_type_falls_back_to_unspecified(self) -> None:
        elements = [[0, 3, [[[0, 3, ["abc"]]], None, None, [None, None, 0, {"102": 99}]]]]
        (block,) = build_blocks(elements)
        assert block.list_info.style is ListStyle.UNSPECIFIED

    def test_unrecognised_named_style_falls_back_to_unspecified(self) -> None:
        elements = [[0, 3, [[[0, 3, ["abc"]]], [None, 99]]]]
        (block,) = build_blocks(elements)
        assert block.style is BlockStyle.UNSPECIFIED
        assert block.heading_level is None

    def test_text_style_flags_and_link_are_decoded(self) -> None:
        elements = [
            [0, 3, [[[0, 3, ["abc", [True, True, True, "https://example.test"]]]]]],
        ]
        (block,) = build_blocks(elements)
        (span,) = block.spans
        assert (span.bold, span.italic, span.underline) == (True, True, True)
        assert span.url == "https://example.test"

    def test_absent_text_style_leaves_every_flag_false(self) -> None:
        (block,) = build_blocks([[0, 3, [[[0, 3, ["abc"]]]]]])
        (span,) = block.spans
        assert (span.bold, span.italic, span.underline, span.url) == (
            False,
            False,
            False,
            None,
        )

    @pytest.mark.parametrize(
        "element",
        [
            [None, 5, None],  # start not an index
            [0, None, None],  # end not an index
            [True, 5, None],  # bool is an int subclass — rejected
            [-1, 5, None],  # negative offset
            [9, 2, None],  # inverted range
            "not a list",
        ],
        ids=["no-start", "no-end", "bool-start", "negative", "inverted", "non-list"],
    )
    def test_element_with_an_unusable_range_is_dropped(self, element: Any) -> None:
        assert build_blocks([element]) == ()

    @pytest.mark.parametrize(
        "entry",
        [
            [["obj"], None],  # no range
            [["obj"], [None, 9, 2]],  # inverted range
            [[], [None, 1, 2]],  # empty object-id block
            [[42], [None, 1, 2]],  # non-string object id
            [[""], [None, 1, 2]],  # empty object id
            "not a list",
        ],
        ids=["no-range", "inverted", "empty-id-block", "non-string-id", "empty-id", "non-list"],
    )
    def test_unusable_annotation_entry_is_dropped(self, entry: Any) -> None:
        assert build_document([[], [entry]]).annotations == ()

    def test_nested_tables_stop_at_the_depth_bound(self, caplog: Any) -> None:
        """A pathological tree degrades; it must not exhaust the stack.

        ``TableCell.content`` holds ``StructuralElement`` rows, which may be
        tables again, so the walk is mutually recursive over server-controlled
        input. Real documents nest once or twice.
        """
        innermost = [0, 5, [[[0, 5, ["deep"]]]]]
        element = innermost
        for _ in range(60):
            element = [0, 5, None, None, [1, 1, [[0, 5, [[0, 5, [element]]]]]]]
        with caplog.at_level(logging.WARNING, logger="notebooklm._row_adapters.documents"):
            blocks = build_blocks([element])
        assert blocks  # the outer levels still decoded
        assert "Max nesting depth" in caplog.text

    def test_invalid_ranges_are_rejected_by_the_public_constructors(self) -> None:
        """The adapters never emit these; the public constructors must refuse them.

        These types are exported from ``notebooklm.types``, so the adapter is
        one construction path among several — and an offset-bearing value whose
        offsets cannot index anything is the bug this module exists to remove.
        """
        with pytest.raises(ValueError, match="offsets must be non-negative"):
            TextSpan(start_index=-1, end_index=3, text="abc")
        with pytest.raises(ValueError, match="start_index .* > end_index"):
            TextSpan(start_index=9, end_index=2, text="abc")
        with pytest.raises(ValueError, match="start_index .* > end_index"):
            DocumentBlock(start_index=9, end_index=2)
        with pytest.raises(ValueError, match="offsets must be non-negative"):
            DocumentAnnotation(object_id="a", start_index=0, end_index=-1)
        with pytest.raises(ValueError, match="start_index .* > end_index"):
            DocumentAnnotation(object_id="a", start_index=9, end_index=2)
        with pytest.raises(ValueError, match="object_id must be non-empty"):
            DocumentAnnotation(object_id="", start_index=0, end_index=1)

    def test_a_reshaped_body_container_warns_and_yields_an_empty_document(
        self, caplog: Any
    ) -> None:
        """Drift here is otherwise invisible, because ``content`` keeps working.

        ``SourceFulltext.content`` is harvested by a separate recursive string
        walk that survives a reshaped ``Body``, so without this warning the only
        symptom would be citation offsets quietly ceasing to resolve — the
        #2128 defect class itself. It warns rather than raising because the
        document is an addition to a call whose load-bearing return is the text.
        """
        with caplog.at_level(logging.WARNING, logger="notebooklm._row_adapters.documents"):
            document = build_document(["not-a-list-of-elements"])
        assert document == StructuredDocument()
        assert "expected a list" in caplog.text

    def test_a_paragraph_element_carrying_no_text_run_is_skipped(self) -> None:
        """``ParagraphElement`` also has image / resource variants."""
        (block,) = build_blocks([[0, 5, [[[0, 5, None], [0, 5, ["ok"]]]]]])
        assert [span.text for span in block.spans] == ["ok"]

    def test_a_paragraph_element_with_an_impossible_range_is_skipped(self) -> None:
        """Dropped before it can become a ``TextSpan`` the type would reject."""
        (block,) = build_blocks([[0, 5, [[[9, 2, ["inverted"]], [0, 5, ["ok"]]]]]])
        assert [span.text for span in block.spans] == ["ok"]

    def test_a_nested_block_wholly_outside_its_cell_is_dropped(self) -> None:
        """Narrowing to an empty range drops the block rather than inverting it."""
        cell = [0, 2, [[5, 9, [[[5, 9, ["elsewhere"]]]]]]]
        table = [1, 1, [[0, 2, [cell]]]]
        (block,) = build_blocks([[0, 2, None, None, table]])
        assert block.spans == ()

    def test_trailing_textless_block_still_occupies_its_positions(self) -> None:
        """The document ends where its last block says, not where its text runs out."""
        document = StructuredDocument(
            blocks=(
                DocumentBlock(0, 3, (TextSpan(0, 3, "abc"),)),
                DocumentBlock(3, 9, kind=BlockKind.HORIZONTAL_RULE),
            )
        )
        assert len(document.text) == 9
        assert document.text.startswith("abc")

    def test_collections_are_frozen_against_the_callers_list(self) -> None:
        """``frozen=True`` stops rebinding, not mutation of what was bound.

        A caller who hands in a list keeps a live handle to it; without
        normalising, mutating it afterwards would change this document's
        ``text`` and every ``slice`` taken from it, which is not what a frozen
        value object promises.
        """
        spans = [TextSpan(0, 3, "abc")]
        block = DocumentBlock(0, 3, spans)
        blocks = [block]
        annotations = [DocumentAnnotation("a", 0, 1)]
        document = StructuredDocument(blocks, annotations)

        spans.append(TextSpan(3, 9, "MUTATED"))
        blocks.append(DocumentBlock(3, 9))
        annotations.append(DocumentAnnotation("b", 3, 4))

        assert isinstance(block.spans, tuple)
        assert isinstance(document.blocks, tuple)
        assert isinstance(document.annotations, tuple)
        assert block.text == "abc"
        assert document.text == "abc"
        assert len(document.annotations) == 1

    def test_an_unpaired_surrogate_degrades_instead_of_raising(self) -> None:
        """A malformed run must not blow up a property.

        JSON preserves an escaped lone surrogate (``"\\ud800"``) that strict
        UTF-16 refuses to encode, so a single bad run would raise
        ``UnicodeEncodeError`` out of ``text`` — past every soft-degrade guard
        in the decoder, and from a property rather than a call. The UTF-16
        conversions use ``surrogatepass`` on both legs to keep it local.
        """
        lone = json.loads('"\\ud800abc"')
        document = StructuredDocument(blocks=(DocumentBlock(0, 4, (TextSpan(0, 4, lone),)),))
        assert utf16_len(lone) == 4
        # Substituted, not merely survived: keeping the surrogate would move
        # the failure to whoever next printed or serialized the text.
        assert document.text == "\ufffcabc"
        assert document.text.encode("utf-8")
        assert utf16_len(document.text) == 4
        assert document.slice(1, 4) == "abc"

    def test_cited_text_is_sanitized_too_not_just_document_text(self) -> None:
        """The reading that escapes the document is the one that gets printed.

        ``DocumentBlock.text`` — and so ``ChatReference.cited_text`` — reads
        span text directly, not through ``StructuredDocument.text``. Cleaning
        only the latter would leave a lone surrogate on every citation a caller
        prints, logs or JSON-encodes, which is where it would actually be seen.
        Sanitizing at the ``TextSpan`` boundary covers both.
        """
        lone = json.loads('"\\ud800abc"')
        fragment = [[[0, 4, [[[0, 4, [lone]]]]]]]
        cited_text, start_char, end_char = extract_text_passages([None, None, None, None, fragment])
        assert cited_text == "\ufffcabc"
        assert cited_text.encode("utf-8")
        assert json.dumps(cited_text, ensure_ascii=False)
        # Width-preserving, so the range it claims is still true.
        assert utf16_len(cited_text) == end_char - start_char

    def test_a_cut_through_a_surrogate_pair_does_not_leak_one(self) -> None:
        """A malformed width can split an astral character; the half must not escape.

        ``_fit`` truncates to the declared width, which for a badly-declared
        span can land between a pair's two units. The resulting lone surrogate
        would make ``text`` un-encodable — the same hazard as an inbound one,
        manufactured locally — so it is substituted width-preservingly.
        """
        document = StructuredDocument(
            blocks=(DocumentBlock(0, 1, (TextSpan(0, 1, "\U0001f52c"),)),)
        )
        assert document.text == "\ufffc"
        assert document.text.encode("utf-8")
        assert utf16_len(document.text) == 1

    def test_slice_returns_the_range_but_counts_python_characters(self) -> None:
        """The distinction the docstrings now make, pinned.

        ``slice(a, b)`` covers exactly ``[a, b)`` of the backend's space, but
        the string it returns is Python — so ``len()`` of it is not the range
        width whenever astral characters are involved. Anyone deriving a
        TailwindDoc offset from that ``len()`` lands short.
        """
        document = StructuredDocument(
            blocks=(DocumentBlock(0, 2, (TextSpan(0, 2, "\U0001f52c"),)),)
        )
        sliced = document.slice(0, 2)
        assert sliced == "\U0001f52c"
        assert len(sliced) == 1
        assert utf16_len(sliced) == 2

    def test_the_extent_cap_truncates_rather_than_materialising(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A drifted offset must not turn a tiny response into a huge allocation.

        The filler is synthesized locally from offsets the *server* chose, so no
        response-size limit bounds it — an ``int32``-scale ``start_index`` would
        otherwise ask for gigabytes. Past the cap the document truncates, and
        the span straddling the cap is clipped rather than laid out whole.
        """
        monkeypatch.setattr(documents_types, "_MAX_DOCUMENT_EXTENT", 8)
        document = StructuredDocument(
            blocks=(
                DocumentBlock(0, 4, (TextSpan(0, 4, "abcd"),)),
                DocumentBlock(4, 2_000_000_000, (TextSpan(4, 2_000_000_000, "efghIJKLMNOP"),)),
            )
        )
        # Truncated at the cap, and the straddling span is clipped to fit it
        # rather than laid out whole and then overflowing.
        assert utf16_len(document.text) == 8
        assert document.text == "abcdefgh"

    def test_kind_is_unknown_when_no_variant_is_populated(self) -> None:
        (block,) = build_blocks([[0, 5, None]])
        assert block.kind is BlockKind.UNKNOWN
        assert block.spans == ()

    def test_horizontal_rule_keeps_its_offsets_and_reports_its_kind(self) -> None:
        (block,) = build_blocks([[0, 1, None, None, None, None, None, None, None, None, None, []]])
        assert block.kind is BlockKind.HORIZONTAL_RULE
        assert (block.start_index, block.end_index) == (0, 1)
        assert block.text == ""

    @pytest.mark.parametrize(
        ("label", "table"),
        [
            # A row declaring a range disjoint from its table contributes nothing.
            ("row-outside-the-table", [1, 1, [[9, 12, [[9, 12, [[9, 12, [[[9, 12, ["x"]]]]]]]]]]]),
            # ...and likewise a cell disjoint from its row.
            ("cell-outside-the-row", [1, 1, [[0, 4, [[9, 12, [[9, 12, [[[9, 12, ["x"]]]]]]]]]]]),
        ],
    )
    def test_a_table_part_outside_its_container_contributes_nothing(
        self, label: str, table: Any
    ) -> None:
        """Narrowing bottoms out at "empty", not at a negative range."""
        (block,) = build_blocks([[0, 4, None, None, table]])
        assert block.kind is BlockKind.TABLE
        assert block.spans == ()

    def test_a_table_part_with_no_declared_range_inherits_its_container(self) -> None:
        """An absent child range narrows nothing rather than dropping the subtree.

        A malformed range cannot *widen* its parent, so the safe reading of an
        unusable one is "no additional constraint" — dropping the subtree would
        lose text the container's own bounds already vouch for.
        """
        cell = [None, None, [[0, 2, [[[0, 2, ["ok"]]]]]]]
        table = [1, 1, [[None, None, [cell]]]]
        (block,) = build_blocks([[0, 2, None, None, table]])
        assert block.text == "ok"

    def test_malformed_table_yields_a_table_block_with_no_text(self) -> None:
        (block,) = build_blocks([[0, 4, None, None, [2, 2, "not-a-list"]]])
        assert block.kind is BlockKind.TABLE
        assert block.spans == ()


# ---------------------------------------------------------------------------
# #2120 defect 3 — cited_text covers the whole fragment
# ---------------------------------------------------------------------------


class TestFragmentDescent:
    """The two-level descent, and the trap of doing only half of it."""

    def test_fragment_elements_reach_the_blocks_not_the_wrapper(
        self, first_citation_detail: list[Any]
    ) -> None:
        detail = CitationDetail(first_citation_detail)
        # The one-level read lands on the ``TailwindDocFragment`` message,
        # which has exactly one field — hence the length-1 list that made the
        # old passage loop run exactly once.
        assert len(first_citation_detail[detail._FRAGMENT_POS]) == 1
        # The two-level read lands on the blocks.
        assert len(detail.fragment_elements) == 12

    def test_cited_text_spans_the_whole_fragment(self, first_citation_detail: list[Any]) -> None:
        """556 characters from 12 blocks — the pre-fix client returned 37."""
        cited_text, start_char, end_char = extract_text_passages(first_citation_detail)
        assert (start_char, end_char) == (0, 556)
        assert len(cited_text) == 556
        assert cited_text.startswith("The Voyager Program: Mission Overview\n")
        # Text from the 11th block, which the one-level descent never reached.
        assert "a photopolarimeter" in cited_text

    def test_cited_text_length_equals_the_character_range(self, answer_row: list[Any]) -> None:
        """The invariant the old code only approximated, now exact.

        ``cited_text`` is the fragment's blocks concatenated verbatim — no
        separators, no stripping — so on an all-prose fragment its length *is*
        the range width. That makes the field checkable rather than merely
        plausible. (It falls short of the width only when a block occupies
        offsets whose text this client does not decode; see
        ``test_cited_text_is_never_longer_than_its_range``.)
        """
        for ref in parse_citations(answer_row):
            assert len(ref.cited_text) == ref.end_char - ref.start_char

    def test_cited_text_is_never_longer_than_its_range(self, answer_row: list[Any]) -> None:
        """The half of the length contract that holds unconditionally."""
        for ref in parse_citations(answer_row):
            assert len(ref.cited_text) <= ref.end_char - ref.start_char

    def test_one_level_descent_would_yield_no_text_at_all(
        self, first_citation_detail: list[Any]
    ) -> None:
        """Pins the regression the naive one-line fix produces.

        Descending to ``cite_inner[4][0]`` while still unwrapping each
        element's ``[0]`` — the pre-#2120 ``PassageRow`` behaviour — reads an
        element's ``startIndex`` (an ``int``) where a nested record is
        expected. Every element then fails well-formedness, all are skipped,
        and ``cited_text`` becomes ``None`` for every citation: strictly worse
        than the truncation it was meant to fix.

        This test reproduces that decode directly against the real fragment, so
        the trap stays documented in executable form rather than in a comment.
        """
        elements = CitationDetail(first_citation_detail).fragment_elements
        assert len(elements) == 12

        # Feed the production decoder exactly what the retired PassageRow would
        # have handed it — each element unwrapped one level too far — and watch
        # the whole fragment vanish. Going through the real entry point is the
        # point: asserting properties of the fixture instead would pass under
        # the very regression this test is named for.
        half_fixed = [None, None, None, None, [[element[0] for element in elements]]]
        assert extract_text_passages(half_fixed) == (None, None, None)

        # ...because an element's [0] is its startIndex, an int, where the
        # unwrap expects a nested record.
        assert [element[0] for element in elements][:4] == [0, 38, 39, 118]

    def test_every_citation_in_the_capture_recovers_its_full_fragment(
        self, answer_row: list[Any]
    ) -> None:
        refs = parse_citations(answer_row)
        assert [(r.start_char, r.end_char, len(r.cited_text)) for r in refs] == [
            (0, 556, 556),
            (556, 1130, 574),
            (1130, 1695, 565),
        ]

    def test_table_blocks_contribute_their_cell_text(self) -> None:
        """A fragment can hold tables, and their text must not be a hole.

        ``tests/unit/fixtures/citation_fragment_with_tables.json`` is the first
        citation of the repo's own ``chat_ask.yaml`` cassette — an answer over
        a Wikipedia source whose fragment opens with two infobox tables. They
        occupy 151 and 83 characters of the coordinate space; a decoder that
        only understood paragraphs would report them as empty and desynchronise
        every offset after them.
        """
        elements = json.loads((FIXTURES_DIR / "citation_fragment_with_tables.json").read_text())
        blocks = build_blocks(elements)
        assert [b.kind.name for b in blocks] == [
            "TABLE",
            "TABLE",
            "PARAGRAPH",
            "PARAGRAPH",
        ]
        tables = blocks[:2]
        assert [len(b.text) for b in tables] == [151, 83]
        assert [b.end_index - b.start_index for b in tables] == [151, 83]
        assert tables[0].text.startswith("Android2026.01.06")
        assert "Operating system" in tables[1].text

    def test_a_tables_cells_are_separated_by_the_boundaries_the_parse_carries(self) -> None:
        """The readable rendering's former blind spot, fixed (#2230).

        ``render()`` joins a block's runs with no separator, which is right for
        a paragraph and was lossy for a table: the parse flattens every row and
        cell into the table block's spans, so cell values used to end up
        adjacent — ``"Operating systemAndroid 10+, iOS 17+ [a]Included
        with…"``.

        Separating *runs* was never the fix, and this capture is why: a single
        cell is several runs (``"Android 10"``, ``"+, "``, ``"iOS 17"``,
        ``"+ "``, ``"[a]"`` are one cell), so run separation would split cells
        as often as it separated them. The boundary has to survive the parse,
        which is what :attr:`DocumentBlock.table_rows` now carries — as
        offsets, so nothing about the coordinate space moves.
        """
        elements = json.loads((FIXTURES_DIR / "citation_fragment_with_tables.json").read_text())
        document = StructuredDocument(blocks=tuple(build_blocks(elements)))
        table = document.blocks[1]
        assert table.kind is BlockKind.TABLE
        # The runs are still flattened, and still cut across cell values.
        assert [span.text for span in table.spans][:5] == [
            "Operating system",
            "Android 10",
            "+, ",
            "iOS 17",
            "+ ",
        ]
        # The capture declares *four* rows; the first is zero-width and holds
        # no positions, so it is dropped rather than rendered as a line of
        # separators with nothing between them.
        assert len(elements[1][4][2]) == 4
        assert table.table_rows == (
            (TableCell(1610, 1626), TableCell(1626, 1650)),
            (TableCell(1650, 1663), TableCell(1663, 1669)),
            (TableCell(1669, 1676), TableCell(1676, 1693)),
        )
        assert document.render(table.start_index, table.end_index) == (
            "Operating system\tAndroid 10+, iOS 17+ [a]\n"
            "Included with\tGemini\n"
            "Website\tnotebooklm.google"
        )
        # The cell ranges are the document's own offsets, not a rendering
        # artefact: each one resolves on the addressable surface.
        assert [
            document.slice(cell.start_index, cell.end_index) for cell in table.table_rows[0]
        ] == [
            "Operating system",
            "Android 10+, iOS 17+ [a]",
        ]
        # ...and ``DocumentBlock.text`` — the reading ``cited_text`` gives —
        # is deliberately unchanged, because no character was added anywhere.
        assert table.text == (
            "Operating systemAndroid 10+, iOS 17+ [a]Included withGeminiWebsitenotebooklm.google"
        )

    def test_carrying_the_cell_boundaries_moves_no_offset(self) -> None:
        """The regression the fix had to avoid, asserted directly (#2230).

        ``StructuredDocument.text`` is the coordinate space every citation
        offset indexes, so the cheap version of this fix — inserting a
        separator into the layout — would have shifted every offset after the
        capture's first table and broken the alignment #2226 / #2210 landed.
        The boundaries are therefore carried as *offsets*: the document laid
        out with them is byte-identical to the same document with every one of
        them stripped, which is what the parse produced before this change.
        """
        elements = json.loads((FIXTURES_DIR / "citation_fragment_with_tables.json").read_text())
        blocks = build_blocks(elements)
        document = StructuredDocument(blocks=blocks)
        stripped = StructuredDocument(blocks=tuple(replace(b, table_rows=()) for b in blocks))

        assert any(block.table_rows for block in document.blocks)
        assert not any(block.table_rows for block in stripped.blocks)
        assert document.text == stripped.text
        assert utf16_len(document.text) == document.extent == stripped.extent == 2089
        assert [(b.start_index, b.end_index, b.text) for b in document.blocks] == [
            (b.start_index, b.end_index, b.text) for b in stripped.blocks
        ]
        # The rendering's separator exists in the rendering alone.
        assert "\t" not in document.text
        assert "\t" in document.render()
        # Spot-check the space itself against the capture: the two cells that
        # straddle the fix, and the paragraph that follows both tables.
        assert document.slice(1610, 1626) == "Operating system"
        assert document.slice(1626, 1650) == "Android 10+, iOS 17+ [a]"
        assert document.slice(1693, 1727) == "Duration: 11 minutes and 15 second"

    def test_the_parse_keeps_an_empty_cell_as_a_column(self) -> None:
        """An empty cell is a boundary the decode must keep (#2230 review).

        The capture proves the shape: its second table's declared header cells
        are literally ``[1610, 1610]`` — a zero-width range, which is how an
        empty cell arrives. The text walk rightly skips such a cell (there is
        nothing to descend into), and the *boundary* walk must not, or a row of
        ``A`` / *nothing* / ``C`` decodes as two columns and reports ``C`` in
        column two.

        The rows below are built in the capture's own positional shape —
        ``StructuralElement = [start, end, paragraph]``, ``Table = [rows,
        columns, tableRows]``, ``TableCell = [start, end, content]`` — because
        no captured table has an empty interior cell to read this off.
        """
        elements = json.loads((FIXTURES_DIR / "citation_fragment_with_tables.json").read_text())
        assert elements[1][4][2][0][2][0] == [1610, 1610]  # the wire's empty cell

        def paragraph(start: int, end: int, text: str) -> list[Any]:
            return [start, end, [[[start, end, [text, None]]], [None, 1]]]

        table = [
            0,
            3,
            None,
            None,
            [
                1,
                3,
                [[0, 3, [[0, 1, [paragraph(0, 1, "A")]], [1, 1], [1, 3, [paragraph(1, 3, "C")]]]]],
            ],
        ]
        block = build_blocks([table])[0]
        assert block.kind is BlockKind.TABLE
        assert block.table_rows == ((TableCell(0, 1), TableCell(1, 1), TableCell(1, 3)),)
        assert StructuredDocument(blocks=(block,)).render() == "A\t\tC"
        # ...and the empty column costs the coordinate space nothing.
        assert StructuredDocument(blocks=(block,)).text == "AC￼"

    def test_the_legacy_flat_rendering_does_not_see_the_cells_at_all(self) -> None:
        """``SourceFulltext.content`` is byte-frozen, and stays that way (#2230).

        It is harvested by a separate recursive string walk over the raw rows —
        every string in traversal order, structure discarded — so a change to
        the parsed tree cannot reach it. Asserted rather than assumed, because
        "the legacy rendering is unaffected" is exactly the claim a decoder
        change is most likely to break silently.
        """
        from notebooklm._web.sources.content import SourceContentRenderer

        elements = json.loads((FIXTURES_DIR / "citation_fragment_with_tables.json").read_text())
        flat = "\n".join(SourceContentRenderer(None).extract_all_text(elements))

        # One line per *run* (and per link URL), the pre-#2128 behaviour.
        assert flat.split("\n")[:3] == [
            "Android",
            "2026.01.06 (Build 853388154) / 8 January 2026; 4 months ago ( 2026-01-08) ",
            "[1]",
        ]
        assert (
            "Operating system\nhttps://en.wikipedia.org/wiki/Operating_system\n"
            "Android 10\nhttps://en.wikipedia.org/wiki/Android_10\n+, \niOS 17"
        ) in flat
        assert "\t" not in flat

    def test_absent_fragment_degrades_to_all_none(self) -> None:
        assert extract_text_passages([None, None, 0.5, [[None, 0, 5]]]) == (None, None, None)

    @pytest.mark.parametrize(
        "fragment",
        [
            [],  # fragment message present but empty
            [[]],  # elements list present but empty
            ["not-a-list"],  # elements slot is not a list
            [[["only-two", 1]]],  # element too short for a range
        ],
        ids=["empty-message", "empty-elements", "non-list-elements", "short-element"],
    )
    def test_malformed_fragment_degrades_to_all_none(self, fragment: Any) -> None:
        assert extract_text_passages([None, None, None, None, fragment]) == (None, None, None)


# ---------------------------------------------------------------------------
# #2120 defect 2 — the range at Citation tag 4 is source-side
# ---------------------------------------------------------------------------


class TestFragmentRangeIsSourceSide:
    def test_range_equals_the_union_of_the_fragment_blocks(self, answer_row: list[Any]) -> None:
        """The claim that the old ``answer_*`` docstring got backwards."""
        for cite in AnswerRow(answer_row).citations:
            detail = CitationRow(cite).detail
            assert detail is not None
            declared = extract_fragment_range(detail.raw_list)
            blocks = build_blocks(detail.fragment_elements)
            assert declared == (blocks[0].start_index, max(b.end_index for b in blocks))

    def test_range_exceeds_the_answer_length(self, answer_row: list[Any]) -> None:
        """Direct proof it cannot be an answer-text range.

        The capture's answer is 536 characters; its third citation reports
        ``[1130, 1695]``. Anyone slicing the answer with these — which the old
        docstring invited — indexes far off the end.
        """
        answer = answer_row[0]
        assert len(answer) == 536
        refs = parse_citations(answer_row)
        assert refs[-1].fragment_start_char > len(answer)
        assert refs[-1].fragment_end_char > len(answer)

    def test_deprecated_alias_still_reports_the_same_value(self, answer_row: list[Any]) -> None:
        """The rename must not change what the old name returns."""
        for ref in parse_citations(answer_row):
            assert ref.answer_start_char == ref.fragment_start_char
            assert ref.answer_end_char == ref.fragment_end_char


class TestChatReferenceAlias:
    """The deprecated ``answer_*`` pair stays in lock-step, both directions."""

    def test_legacy_keyword_populates_the_canonical_field(self) -> None:
        ref = ChatReference(source_id="s", answer_start_char=3, answer_end_char=9)
        assert (ref.fragment_start_char, ref.fragment_end_char) == (3, 9)

    def test_canonical_keyword_populates_the_alias(self) -> None:
        ref = ChatReference(source_id="s", fragment_start_char=3, fragment_end_char=9)
        assert (ref.answer_start_char, ref.answer_end_char) == (3, 9)

    def test_canonical_wins_a_disagreement(self) -> None:
        ref = ChatReference(
            source_id="s",
            answer_start_char=1,
            answer_end_char=2,
            fragment_start_char=3,
            fragment_end_char=9,
        )
        assert (ref.fragment_start_char, ref.fragment_end_char) == (3, 9)
        assert (ref.answer_start_char, ref.answer_end_char) == (3, 9)

    def test_post_construction_write_to_either_name_round_trips(self) -> None:
        ref = ChatReference(source_id="s")
        ref.answer_start_char = 7
        assert ref.fragment_start_char == 7
        ref.fragment_end_char = 11
        assert ref.answer_end_char == 11

    def test_positional_construction_keeps_its_slot(self) -> None:
        """The alias stays ahead of the canonical field for exactly this reason."""
        ref = ChatReference("s", 1, "text", 0, 4, "chunk", "passage", 20, 30, 0.5)
        assert (ref.answer_start_char, ref.answer_end_char) == (20, 30)
        assert (ref.fragment_start_char, ref.fragment_end_char) == (20, 30)
        assert ref.score == 0.5

    def test_half_populated_pair_is_rejected_under_the_canonical_name(self) -> None:
        with pytest.raises(ValueError, match="fragment_start_char/fragment_end_char"):
            ChatReference(source_id="s", answer_start_char=5)

    def test_inverted_answer_anchor_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="answer_anchor_start"):
            ChatReference(source_id="s", answer_anchor_start=9, answer_anchor_end=2)

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"answer_anchor_start": -5, "answer_anchor_end": 2},
            {"start_char": -1, "end_char": 3},
            {"fragment_start_char": -2, "fragment_end_char": -1},
        ],
        ids=["answer-anchor", "source-range", "fragment-range"],
    )
    def test_negative_offsets_are_rejected(self, kwargs: dict[str, int]) -> None:
        """A negative offset resolves to a *plausible* answer, which is worse.

        ``slice()`` clamps a negative lower bound to 0, so an impossible anchor
        would present as a citation highlighting the start of the answer rather
        than as an error. Rejecting at construction matches the range
        validation on ``DocumentAnnotation``, which these are resolved against.
        """
        with pytest.raises(ValueError, match="must be non-negative"):
            ChatReference(source_id="s", **kwargs)


# ---------------------------------------------------------------------------
# #2120 defect 1 — the answer-side annotation map is read
# ---------------------------------------------------------------------------


class TestAnswerAnnotationMap:
    def test_annotation_map_is_decoded_from_the_answer_document(
        self, answer_row: list[Any]
    ) -> None:
        document = AnswerRow(answer_row).document
        assert document.annotations == (
            DocumentAnnotation(FIRST_CITATION_OBJECT_ID, 85, 85),
            DocumentAnnotation("4d45b35e-028b-4b6c-845e-95c093813f20", 219, 235),
            DocumentAnnotation("759eb414-db8f-4eed-8ce3-df54c15da872", 328, 328),
        )

    def test_annotation_ranges_index_the_document_text(self, answer_row: list[Any]) -> None:
        document = AnswerRow(answer_row).document
        spanning = document.annotations[1]
        assert document.slice(spanning.start_index, spanning.end_index) == "into electricity"

    def test_zero_width_anchors_are_preserved_not_dropped(self, answer_row: list[Any]) -> None:
        """Two of three anchors are zero-width — the marker insertion point.

        A decoder that treated ``start == end`` as "empty, therefore junk"
        would silently lose two thirds of this map.
        """
        document = AnswerRow(answer_row).document
        zero_width = [a for a in document.annotations if a.start_index == a.end_index]
        assert len(zero_width) == 2

    def test_answer_document_text_is_not_the_answer_string(self, answer_row: list[Any]) -> None:
        """The distinction the new field names exist to make loud.

        The answer carries markdown emphasis and inline ``[N]`` markers; the
        document does not. Indexing one with the other's offsets is the exact
        misuse #2120 was filed about.
        """
        document = AnswerRow(answer_row).document
        assert len(answer_row[0]) == 536
        assert len(document.text) == 476
        assert "**" in answer_row[0]
        assert "**" not in document.text
        assert "[1]" in answer_row[0]
        assert "[1]" not in document.text

    def test_the_documents_offsets_are_utf16_code_units(self, answer_row: list[Any]) -> None:
        """The unit the backend counts in, and Python does not.

        The captured answer opens its last block with an emoji: one Python
        character, two UTF-16 units. The document is 476 Python characters and
        477 units, and every block still round-trips through ``slice`` —
        which it would not if the offsets were being read as code points.
        """
        document = AnswerRow(answer_row).document
        assert len(document.text) == 476
        assert utf16_len(document.text) == 477
        assert any(ord(char) > 0xFFFF for char in document.text)

        astral_block = document.blocks[-1]
        assert (astral_block.start_index, astral_block.end_index) == (329, 477)
        assert utf16_len(astral_block.text) == 148
        assert len(astral_block.text) == 147

        for block in document.blocks:
            assert document.slice(block.start_index, block.end_index) == block.text

    def test_an_offset_after_an_astral_character_needs_the_utf16_reading(self) -> None:
        """Where reading the offsets as code points actually goes wrong.

        The captured document happens to carry its emoji in its *last* block, so
        plain indexing survives there by running off the end of the string. Put
        anything after the emoji and the drift is immediate — one position per
        astral character, silently returning neighbouring text.
        """
        document = StructuredDocument(
            blocks=(
                DocumentBlock(0, 2, (TextSpan(0, 2, "\U0001f52c"),)),
                DocumentBlock(2, 7, (TextSpan(2, 7, "hello"),)),
            )
        )
        assert utf16_len(document.text) == 7
        assert len(document.text) == 6
        assert document.slice(2, 7) == "hello"
        # Reading the same offsets as Python code points lands a character early.
        assert document.text[2:7] == "ello"

    def test_refs_are_stamped_with_their_answer_range(self, answer_row: list[Any]) -> None:
        refs = parse_citations(answer_row)
        assert [(r.answer_anchor_start, r.answer_anchor_end) for r in refs] == [
            (85, 85),
            (219, 235),
            (328, 328),
        ]

    def test_join_is_by_object_id_not_by_position(self) -> None:
        """A reordered map must still land each range on the right reference.

        Joining by list position would pass on the happy path and silently
        mis-anchor whenever the server reorders the map or this client skips a
        malformed citation.
        """
        refs = [
            ChatReference(source_id="s", chunk_id="alpha"),
            ChatReference(source_id="s", chunk_id="beta"),
        ]
        document = StructuredDocument(
            blocks=(DocumentBlock(0, 60, (TextSpan(0, 60, "x" * 60),)),),
            annotations=(
                DocumentAnnotation("beta", 40, 50),
                DocumentAnnotation("alpha", 10, 20),
            ),
        )
        stamped = attach_answer_anchors(refs, document)
        assert [(r.chunk_id, r.answer_anchor_start) for r in stamped] == [
            ("alpha", 10),
            ("beta", 40),
        ]

    def test_reference_without_a_matching_annotation_keeps_none(self) -> None:
        refs = [ChatReference(source_id="s", chunk_id="unmatched")]
        document = StructuredDocument(
            blocks=(DocumentBlock(0, 4, (TextSpan(0, 4, "abcd"),)),),
            annotations=(DocumentAnnotation("other", 1, 2),),
        )
        (stamped,) = attach_answer_anchors(refs, document)
        assert stamped.answer_anchor_start is None
        assert stamped.answer_anchor_end is None

    def test_an_anchor_past_the_documents_extent_is_dropped(self, caplog: Any) -> None:
        """An unresolvable anchor must read as absent, not as anchored-to-nothing.

        A range beyond the document is ordered and non-negative, so it survives
        the entry-level validation, but resolving it yields ``""``. Passing it
        through would hand the caller a citation that claims to mark a span of
        the answer and marks nothing — the plausible-looking failure this whole
        change exists to remove. ``None`` is the honest report.
        """
        refs = [ChatReference(source_id="s", chunk_id="c")]
        document = StructuredDocument(
            blocks=(DocumentBlock(0, 5, (TextSpan(0, 5, "hello"),)),),
            annotations=(DocumentAnnotation("c", 0, 99),),
        )
        with caplog.at_level(logging.WARNING, logger="notebooklm._chat"):
            (stamped,) = attach_answer_anchors(refs, document)
        assert stamped.answer_anchor_start is None
        assert stamped.answer_anchor_end is None
        assert "beyond the answer document" in caplog.text

    def test_an_anchor_exactly_at_the_extent_is_kept(self) -> None:
        """The bound is inclusive of the end — a zero-width anchor at EOF is real."""
        refs = [ChatReference(source_id="s", chunk_id="c")]
        document = StructuredDocument(
            blocks=(DocumentBlock(0, 5, (TextSpan(0, 5, "hello"),)),),
            annotations=(DocumentAnnotation("c", 5, 5),),
        )
        (stamped,) = attach_answer_anchors(refs, document)
        assert (stamped.answer_anchor_start, stamped.answer_anchor_end) == (5, 5)

    def test_empty_map_returns_the_refs_untouched(self) -> None:
        refs = [ChatReference(source_id="s", chunk_id="x")]
        assert attach_answer_anchors(refs, StructuredDocument()) is refs

    def test_annotations_are_ordered_by_position_not_container_order(self) -> None:
        """The third member of the ordered-by-declared-position family.

        ``annotations_for`` promises document order and the anchor join takes
        the first entry for an object id, so trusting container order would
        both break the documented contract and silently select a different
        occurrence. Blocks and spans already normalise; annotations were the
        one still trusting arrival order.
        """
        document = StructuredDocument(
            blocks=(DocumentBlock(0, 10, (TextSpan(0, 10, "0123456789"),)),),
            annotations=(
                DocumentAnnotation("c", 8, 9),
                DocumentAnnotation("c", 1, 2),
                DocumentAnnotation("c", 1, 5),
            ),
        )
        assert [(a.start_index, a.end_index) for a in document.annotations_for("c")] == [
            (1, 2),
            (1, 5),
            (8, 9),
        ]
        (stamped,) = attach_answer_anchors([ChatReference(source_id="s", chunk_id="c")], document)
        assert (stamped.answer_anchor_start, stamped.answer_anchor_end) == (1, 2)

    def test_first_annotation_in_document_order_wins(self) -> None:
        """One object id can carry several anchors; the reference gets one.

        The capture behind this module's fixtures has exactly that — its first
        citation is anchored twice. Pinning *which* anchor lands keeps the
        choice deliberate rather than incidental, and ``annotations_for`` stays
        the way to reach the rest.
        """
        refs = [ChatReference(source_id="s", chunk_id="dup")]
        document = StructuredDocument(
            blocks=(DocumentBlock(0, 60, (TextSpan(0, 60, "x" * 60),)),),
            annotations=(
                DocumentAnnotation("dup", 10, 20),
                DocumentAnnotation("dup", 40, 50),
            ),
        )
        (stamped,) = attach_answer_anchors(refs, document)
        assert (stamped.answer_anchor_start, stamped.answer_anchor_end) == (10, 20)
        assert len(document.annotations_for("dup")) == 2


class TestStreamingParseCarriesTheDocument:
    def test_ask_result_document_reaches_the_parse_result(self, answer_row: list[Any]) -> None:
        """End to end, from the raw stream body to the parsed result."""
        body = ")]}'\n\n" + json.dumps(
            [["wrb.fr", None, json.dumps([answer_row]), None, None, None, "generic"]]
        )
        result = parse_streaming_chat_response(body)
        assert len(result.answer) == 536
        assert len(result.answer_document.blocks) == 4
        assert len(result.answer_document.annotations) == 3
        assert [r.answer_anchor_start for r in result.references] == [85, 219, 328]

    def test_a_response_without_a_document_yields_an_empty_one(self) -> None:
        """Never ``None``, so consumers never branch."""
        row = ["some answer", None, None, None, [None, None, None, None, 1]]
        body = ")]}'\n\n" + json.dumps(
            [["wrb.fr", None, json.dumps([row]), None, None, None, "generic"]]
        )
        result = parse_streaming_chat_response(body)
        assert result.answer_document == StructuredDocument()
        assert result.answer_document.text == ""

    def test_overlapping_blocks_are_merged_not_concatenated(self) -> None:
        """``cited_text`` must never be wider than the range it reports.

        Two blocks overlapping by two units — ``[0,4)="abcd"`` and
        ``[2,6)="cdef"`` — describe six units of source. Concatenating them
        whole yields eight characters against a six-unit range, and
        ``save-as-note`` then emits an eight-unit local passage for a six-unit
        span. The already-covered prefix is trimmed instead, which is the same
        rule the structured-document layout applies.
        """
        fragment = [[[0, 4, [[[0, 4, ["abcd"]]]]], [2, 6, [[[2, 6, ["cdef"]]]]]]]
        cited_text, start_char, end_char = extract_text_passages([None, None, None, None, fragment])
        assert cited_text == "abcdef"
        assert (start_char, end_char) == (0, 6)
        assert utf16_len(cited_text) == end_char - start_char

    def test_a_block_wholly_covered_by_an_earlier_one_adds_nothing(self) -> None:
        fragment = [[[0, 6, [[[0, 6, ["abcdef"]]]]], [2, 4, [[[2, 4, ["cd"]]]]]]]
        cited_text, start_char, end_char = extract_text_passages([None, None, None, None, fragment])
        assert cited_text == "abcdef"
        assert (start_char, end_char) == (0, 6)

    def test_the_search_key_is_capped_at_the_first_block_boundary(self) -> None:
        """The key must not straddle a join ``content`` renders differently.

        Covers the branch where a citation does *not* start at any block —
        there is then no boundary to cap at, and the 40-character default must
        stand rather than the cap silently shortening every key.
        """
        from notebooklm.types import SourceFulltext

        blocks = (
            DocumentBlock(0, 3, (TextSpan(0, 3, "abc"),)),
            DocumentBlock(3, 40, (TextSpan(3, 40, "d" * 37),)),
        )
        fulltext = SourceFulltext(
            source_id="s",
            title="t",
            content="abc\n" + "d" * 37,
            char_count=41,
            document=StructuredDocument(blocks=blocks),
        )
        # Starts at a block -> capped there, so the key never crosses the join.
        assert fulltext.find_citation_context("abc" + "d" * 37)
        # Starts mid-block -> no boundary applies, key stays the 40-char default.
        assert fulltext.find_citation_context("d" * 37)

    def test_a_genuine_gap_between_blocks_is_omitted_not_filled(self) -> None:
        """The readable value, not the offset-faithful one.

        ``cited_text`` deliberately does not pad a gap the source did not
        include — that is ``document.slice()``'s job — so here it stays shorter
        than its range rather than acquiring placeholder characters.
        """
        fragment = [[[0, 4, [[[0, 4, ["abcd"]]]]], [9, 13, [[[9, 13, ["efgh"]]]]]]]
        cited_text, start_char, end_char = extract_text_passages([None, None, None, None, fragment])
        assert cited_text == "abcdefgh"
        assert (start_char, end_char) == (0, 13)
        assert utf16_len(cited_text) < end_char - start_char
