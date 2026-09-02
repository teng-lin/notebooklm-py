"""Unit tests for Google Play Books ("Expert Intelligence") support (#2292).

Covers the pure, transport-free pieces: the web request builders (list params +
the ``ExpertIntelligenceContent`` add spec), the positional row decoders, the
``SourceRow.expert_intelligence`` metadata decode, the neutral serializer, and
the public dataclasses/enums.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from notebooklm._app.serialize import play_book_summary
from notebooklm._web.params.sources import (
    _EXPERT_INTELLIGENCE_SPEC_INDEX,
    _SPEC_F11_INDEX,
    build_list_play_books_params,
    build_play_book_source_spec,
)
from notebooklm._web.rows.play_books import (
    decode_play_book_row,
    decode_play_books_response,
)
from notebooklm._web.rows.sources import SourceRow, first_added_source_id
from notebooklm.exceptions import DecodingError
from notebooklm.types import (
    ExpertIntelligenceSourceMetadata,
    PlayBook,
    PlayBookExportReason,
)

# One live-captured ListExpertIntelligenceContent row (The Art of War).
_ART_OF_WAR = [
    "QhsZEAAAQBAJ",
    1,
    "The Art of War",
    "<p>Sun Tzu…</p>",
    "https://play.google.com/books/publisher/content/images/frontcover/QhsZEAAAQBAJ",
    False,
    None,
    ["Sun Tzu"],
    4.6458335,
    [1788284189],
]
# A blocked row (publisher opted out).
_BLOCKED = [
    "kLrxEQAAQBAJ",
    1,
    "Bill Gates and the Birth of Microsoft",
    "<p>…</p>",
    "https://play.google.com/books/publisher/content/images/frontcover/kLrxEQAAQBAJ",
    True,
    1,
    ["Author"],
    None,
    [1788284190],
]


class TestParams:
    def test_list_params_are_provider_google_play_books(self) -> None:
        assert build_list_play_books_params() == [None, 1]

    def test_spec_places_expert_intelligence_at_index_15(self) -> None:
        spec = build_play_book_source_spec(
            "CID", "Title", "<p>Desc</p>", "https://cover", 4.5, ["A", "B"]
        )
        assert len(spec) == 16
        assert spec[_SPEC_F11_INDEX] == 1
        ei = spec[_EXPERT_INTELLIGENCE_SPEC_INDEX]
        assert ei == [1, "CID", "Title", "<p>Desc</p>", "https://cover", 4.5, ["A", "B"]]

    def test_spec_leaves_every_other_slot_none(self) -> None:
        spec = build_play_book_source_spec("CID", None, None, None, None, [])
        for index, value in enumerate(spec):
            if index in (_SPEC_F11_INDEX, _EXPERT_INTELLIGENCE_SPEC_INDEX):
                continue
            assert value is None, f"slot {index} should be None"

    def test_spec_copies_the_authors_list(self) -> None:
        authors = ["A"]
        spec = build_play_book_source_spec("CID", "T", None, None, None, authors)
        spec[_EXPERT_INTELLIGENCE_SPEC_INDEX][6].append("B")
        assert authors == ["A"], "the spec must not alias the caller's authors list"


class TestRowDecode:
    def test_exportable_row(self) -> None:
        book = decode_play_book_row(_ART_OF_WAR)
        assert book == PlayBook(
            content_id="QhsZEAAAQBAJ",
            title="The Art of War",
            authors=("Sun Tzu",),
            description_html="<p>Sun Tzu…</p>",
            cover_url="https://play.google.com/books/publisher/content/images/frontcover/QhsZEAAAQBAJ",
            export_disabled=False,
            reason=None,
            field_type=4.6458335,
            updated_at=datetime.fromtimestamp(1788284189, tz=timezone.utc),
        )

    def test_blocked_row_decodes_reason(self) -> None:
        book = decode_play_book_row(_BLOCKED)
        assert book.export_disabled is True
        assert book.reason is PlayBookExportReason.OPTED_OUT
        assert book.field_type is None

    @pytest.mark.parametrize(
        "code,reason",
        [
            (1, PlayBookExportReason.OPTED_OUT),
            (2, PlayBookExportReason.UNSUPPORTED_CONTENT),
            (3, PlayBookExportReason.NOT_OWNED),
            (4, PlayBookExportReason.LICENSE_RESTRICTION),
            (0, None),
            (99, None),
        ],
    )
    def test_reason_code_mapping(self, code: int, reason: PlayBookExportReason | None) -> None:
        row = list(_BLOCKED)
        row[6] = code
        assert decode_play_book_row(row).reason is reason

    def test_short_row_is_tolerated(self) -> None:
        book = decode_play_book_row(["OnlyId"])
        assert book.content_id == "OnlyId"
        assert book.title is None
        assert book.authors == ()
        assert book.export_disabled is False

    def test_response_wraps_rows_in_outer_list(self) -> None:
        books = decode_play_books_response([[_ART_OF_WAR, _BLOCKED]])
        assert [b.content_id for b in books] == ["QhsZEAAAQBAJ", "kLrxEQAAQBAJ"]

    def test_response_none_and_empty_are_empty_library(self) -> None:
        assert decode_play_books_response(None) == []
        assert decode_play_books_response([]) == []

    def test_response_bad_shape_raises(self) -> None:
        with pytest.raises(DecodingError):
            decode_play_books_response("nope")
        with pytest.raises(DecodingError):
            decode_play_books_response(["not-a-row-list"])

    def test_malformed_row_raises_rather_than_dropping(self) -> None:
        # A non-list row is shape drift, not a droppable value — surface it.
        with pytest.raises(DecodingError, match="Malformed"):
            decode_play_books_response([[_ART_OF_WAR, "not-a-row"]])

    @pytest.mark.parametrize("bad_id", [None, "", 123])
    def test_missing_content_id_raises(self, bad_id: object) -> None:
        # The content id is the book's identity; a row without one is a wire
        # break, not a hollow-but-valid PlayBook(content_id="").
        row = list(_ART_OF_WAR)
        row[0] = bad_id
        with pytest.raises(DecodingError, match="content id"):
            decode_play_book_row(row)


class TestSourceRowExpertIntelligence:
    def _row_with_metadata(self, metadata: list) -> SourceRow:
        return SourceRow.from_entry([["src_1"], "The Odyssey", metadata])

    def test_decodes_field_19_metadata(self) -> None:
        metadata = [None] * 18 + [
            [
                "6hwZEAAAQBAJ",
                1,
                "The Odyssey",
                ["Homer"],
                "https://cover",
                "<p>desc</p>",
                4.2959185,
                "CONTEXT_TYPE_EXPERT_INTELLIGENCE_PLAY_BOOKS",
            ],
            "application/epub+zip",
        ]
        row = self._row_with_metadata(metadata)
        assert row.expert_intelligence == ExpertIntelligenceSourceMetadata(
            content_id="6hwZEAAAQBAJ",
            provider=1,
            title="The Odyssey",
            authors=("Homer",),
            thumbnail_image_url="https://cover",
            description="<p>desc</p>",
            field_type=4.2959185,
        )

    def test_absent_metadata_is_none(self) -> None:
        row = self._row_with_metadata([None] * 6 + [20])
        assert row.expert_intelligence is None

    def test_non_list_block_is_none(self) -> None:
        row = self._row_with_metadata([None] * 18 + ["not-a-list"])
        assert row.expert_intelligence is None


class TestFirstAddedSourceId:
    def test_extracts_id_from_async_add_response(self) -> None:
        payload = [
            [[["src_new"], "New Source", [None, None, None, None, 20]]],
            None,
            [[[["src_new"], "New Source", [None, None, None, None, 20]], 0]],
        ]
        assert first_added_source_id(payload) == "src_new"

    def test_empty_id_slot_is_none(self) -> None:
        payload = [[[[""], "New Source", [None]]], None, []]
        assert first_added_source_id(payload) is None


class TestSerializer:
    def test_play_book_summary_shape(self) -> None:
        book = decode_play_book_row(_ART_OF_WAR)
        assert play_book_summary(book) == {
            "content_id": "QhsZEAAAQBAJ",
            "title": "The Art of War",
            "authors": ["Sun Tzu"],
            "export_disabled": False,
            "reason": None,
            "field_type": 4.6458335,
            "cover_url": "https://play.google.com/books/publisher/content/images/frontcover/QhsZEAAAQBAJ",
            "store_url": "https://play.google.com/store/books/details?id=QhsZEAAAQBAJ&pcampaignid=nblm",
            "updated_at": "2026-09-01T17:36:29+00:00",
        }

    def test_blocked_summary_reports_reason_value(self) -> None:
        assert play_book_summary(decode_play_book_row(_BLOCKED))["reason"] == "opted_out"


class TestPublicTypes:
    def test_store_url(self) -> None:
        book = decode_play_book_row(_ART_OF_WAR)
        assert book.store_url == (
            "https://play.google.com/store/books/details?id=QhsZEAAAQBAJ&pcampaignid=nblm"
        )

    def test_reason_is_str_enum(self) -> None:
        assert PlayBookExportReason.OPTED_OUT == "opted_out"
