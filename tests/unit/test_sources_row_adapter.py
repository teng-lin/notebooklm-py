"""Unit tests for the source response adapters drained from raw positional reads.

Covers the three additions that absorbed the ``_source/`` + ``_types/sources``
raw ``name[int]`` RPC-payload reads behind the sanctioned ``_web.rows``
layer (the #1491 single-level burndown for the sources domain):

* :meth:`SourceRow.url_from_metadata` / :meth:`SourceRow.created_at_from_metadata`
  — the metadata-only entry points backing the re-exported
  ``notebooklm.types._extract_source_url`` / ``_extract_source_created_at``
  public helpers. Each asserts BYTE-FOR-BYTE parity with the legacy inline walk
  (the public helpers must not change behavior).
* :class:`SourceGuideRow` — the ``GET_SOURCE_GUIDE`` summary/keyword decode that
  ``_source/content.get_guide`` previously open-coded.
* :class:`SourceFulltextRow` — the ``GET_SOURCE`` descriptor / metadata / HTML /
  text decode that ``_source/content.get_fulltext`` previously open-coded.

Every adapter read is a soft length-guarded degrade; these tests pin the
present / empty / too-short / malformed cases against the legacy behavior so a
future reshape that silently changes them fails here.
"""

from __future__ import annotations

import pytest

from notebooklm._types.common import _datetime_from_timestamp
from notebooklm._types.sources import (
    _extract_source_created_at,
    _extract_source_url,
)
from notebooklm._web.rows.sources import (
    SourceFulltextRow,
    SourceGuideRow,
    SourceRow,
    _warned_drive_id_slots,
    _warned_drive_status_codes,
)
from notebooklm.rpc import RPCMethod
from notebooklm.rpc.types import DriveSourceStatus, SourceStatus

# ---------------------------------------------------------------------------
# Legacy reference implementations (verbatim pre-drain logic) for parity checks
# ---------------------------------------------------------------------------


def _legacy_extract_source_url(metadata, *, allow_bare_http=True):
    """Pre-drain ``_extract_source_url`` body, copied verbatim."""
    if not isinstance(metadata, list):
        return None
    url = None
    if len(metadata) > 7:
        url_list = metadata[7]
        if isinstance(url_list, list) and len(url_list) > 0:
            url = url_list[0]
    if not url and len(metadata) > 5:
        yt_data = metadata[5]
        if isinstance(yt_data, list) and len(yt_data) > 0 and isinstance(yt_data[0], str):
            url = yt_data[0]
    if not url and allow_bare_http and len(metadata) > 0:
        candidate = metadata[0]
        if isinstance(candidate, str) and candidate.startswith("http"):
            url = candidate
    return url


def _legacy_extract_source_created_at(metadata):
    """Pre-drain ``_extract_source_created_at`` body, copied verbatim."""
    if not isinstance(metadata, list) or len(metadata) <= 2:
        return None
    timestamp_list = metadata[2]
    if not isinstance(timestamp_list, list) or not timestamp_list:
        return None
    return _datetime_from_timestamp(timestamp_list[0])


def _legacy_guide(result):
    """Pre-drain ``get_guide`` decode body, copied verbatim."""
    summary = ""
    keywords: list = []
    if result and isinstance(result, list) and len(result) > 0:
        outer = result[0]
        if isinstance(outer, list) and len(outer) > 0:
            inner = outer[0]
            if isinstance(inner, list):
                summary_block = inner[1] if len(inner) > 1 and isinstance(inner[1], list) else None
                if summary_block:
                    summary = summary_block[0] if isinstance(summary_block[0], str) else ""
                keyword_block = inner[2] if len(inner) > 2 and isinstance(inner[2], list) else None
                if keyword_block:
                    keywords = keyword_block[0] if isinstance(keyword_block[0], list) else []
    return summary, keywords


def _legacy_fulltext(result):
    """Pre-drain ``get_fulltext`` title/metadata/html/text decode, verbatim.

    Returns ``(title, metadata, html_content, text_content_blocks)`` — the
    four positional values the adapter now owns.
    """
    title = ""
    metadata = None
    html = None
    text_blocks = None
    descriptor = result[0]
    if isinstance(descriptor, list) and len(descriptor) > 1:
        title = descriptor[1] if isinstance(descriptor[1], str) else ""
        if len(descriptor) > 2 and isinstance(descriptor[2], list):
            metadata = descriptor[2]
    html_block = result[4] if len(result) > 4 and isinstance(result[4], list) else None
    if html_block is not None and len(html_block) > 1:
        candidate = html_block[1]
        if isinstance(candidate, str):
            html = candidate
    text_block = result[3] if len(result) > 3 and isinstance(result[3], list) else None
    if text_block:
        content_blocks = text_block[0]
        if isinstance(content_blocks, list):
            text_blocks = content_blocks
    return title, metadata, html, text_blocks


# ---------------------------------------------------------------------------
# SourceRow.url_from_metadata — parity with legacy _extract_source_url
# ---------------------------------------------------------------------------


_URL_CASES = [
    # (metadata, allow_bare_http)
    ([0, 1, 2, 3, 4, 5, 6, ["https://canonical/"]], True),  # present canonical
    ([0, 1, 2, 3, 4, 5, 6, [""]], True),  # falsy canonical -> raw "" (legacy quirk)
    ([0, 1, 2, 3, 4, 5, 6, [0]], True),  # falsy zero canonical -> raw 0
    ([0, 1, 2, 3, 4, 5, 6, [42]], True),  # non-str truthy -> raw int 42, no coercion
    ([0, 1, 2, 3, 4, 5, 6, []], True),  # empty canonical block
    ([0, 1, 2, 3, 4, 5, 6, "notlist"], True),  # non-list canonical block
    ([0, 1, 2, 3, 4, ["https://yt/"]], True),  # youtube string fallback
    ([0, 1, 2, 3, 4, [42]], True),  # youtube non-string ignored
    ([0, 1, 2, 3, 4, []], True),  # empty youtube block
    (["https://bare/"], True),  # bare http honored
    (["https://bare/"], False),  # bare http suppressed
    (["ftp://bare/"], True),  # bare non-http ignored
    ([42], True),  # bare non-string ignored
    ([], True),  # empty metadata
    ("notalist", True),  # non-list metadata
    (None, True),  # None metadata
    ([0, 1, 2, 3, 4, 5, 6, ["a", "b"]], True),  # canonical multi -> first
]


@pytest.mark.parametrize(("metadata", "allow"), _URL_CASES)
def test_url_from_metadata_matches_legacy_extract(metadata, allow) -> None:
    """``SourceRow.url_from_metadata`` reproduces the legacy walk byte-for-byte."""
    expected = _legacy_extract_source_url(metadata, allow_bare_http=allow)
    actual = SourceRow.url_from_metadata(metadata, allow_bare_http=allow)
    assert actual == expected, f"url mismatch for {metadata!r} allow={allow}"
    # Type identity matters too (legacy returns raw, un-coerced values).
    assert type(actual) is type(expected), f"type mismatch for {metadata!r}"


@pytest.mark.parametrize(("metadata", "allow"), _URL_CASES)
def test_url_public_helper_routes_through_adapter(metadata, allow) -> None:
    """The re-exported ``_extract_source_url`` shim delegates with no drift."""
    assert _extract_source_url(metadata, allow_bare_http=allow) == _legacy_extract_source_url(
        metadata, allow_bare_http=allow
    )


def test_url_from_metadata_canonical_precedence_over_youtube() -> None:
    """Canonical ``[7]`` wins over youtube ``[5]`` when both present."""
    md = [0, 1, 2, 3, 4, ["https://yt/"], 6, ["https://canonical/"]]
    assert SourceRow.url_from_metadata(md) == "https://canonical/"


def test_url_from_metadata_falsy_canonical_falls_through_to_youtube() -> None:
    """A falsy ``[7][0]`` lets the youtube ``[5]`` slot win (legacy fall-through)."""
    md = [0, 1, 2, 3, 4, ["https://yt/"], 6, [""]]
    assert SourceRow.url_from_metadata(md) == "https://yt/"


# ---------------------------------------------------------------------------
# SourceRow.created_at_from_metadata — parity with legacy
# ---------------------------------------------------------------------------


_CREATED_CASES = [
    [0, 1, [1_700_000_000]],  # present numeric
    [0, 1, [1_700_000_000.5]],  # float
    [0, 1, [True]],  # bool subclass of int -> 1s
    [0, 1, []],  # empty timestamp block
    [0, 1, None],  # None timestamp block
    [0, 1, ["abc"]],  # non-numeric string inner
    [0, 1, [None]],  # None inner
    [0, 1, "notlist"],  # non-list timestamp block
    [0, 1],  # too short (no idx 2)
    [],  # empty metadata
    "notalist",  # non-list metadata
    None,  # None metadata
    [0, 1, [1_700_000_000, 99]],  # multi -> first
]


@pytest.mark.parametrize("metadata", _CREATED_CASES)
def test_created_at_from_metadata_matches_legacy_extract(metadata) -> None:
    """``SourceRow.created_at_from_metadata`` reproduces the legacy walk exactly."""
    assert SourceRow.created_at_from_metadata(metadata) == _legacy_extract_source_created_at(
        metadata
    ), f"created_at mismatch for {metadata!r}"


@pytest.mark.parametrize("metadata", _CREATED_CASES)
def test_created_at_public_helper_routes_through_adapter(metadata) -> None:
    """The re-exported ``_extract_source_created_at`` shim delegates with no drift."""
    assert _extract_source_created_at(metadata) == _legacy_extract_source_created_at(metadata)


# ---------------------------------------------------------------------------
# SourceGuideRow — GET_SOURCE_GUIDE decode
# ---------------------------------------------------------------------------


_GUIDE_CASES = [
    [[["id", ["the summary"], [["kw1", "kw2"]]]]],  # present, full
    [[["id", [42], [["kw"]]]]],  # summary[0] non-str -> ""
    [[["id", ["s"], ["notlist"]]]],  # keyword[0] non-list -> []
    [[["id", [], []]]],  # empty blocks
    [[["id", "notlist", "notlist"]]],  # blocks non-list
    [[["id"]]],  # inner too short
    [[["id", ["s"]]]],  # no keyword block
    [[None]],  # inner None
    [None],  # outer None
    [[]],  # empty outer
    [],  # empty result
    None,  # None result
    "x",  # scalar result
    [[["id", ["s", "extra"], [["kw"], "extra"]]]],  # multi-elem blocks -> first
]


@pytest.mark.parametrize("result", _GUIDE_CASES)
def test_source_guide_row_matches_legacy(result) -> None:
    """``SourceGuideRow`` summary/keywords reproduce the legacy ``get_guide`` decode."""
    row = SourceGuideRow(result)
    exp_summary, exp_keywords = _legacy_guide(result)
    assert row.summary == exp_summary, f"summary mismatch for {result!r}"
    assert row.keywords == exp_keywords, f"keywords mismatch for {result!r}"


def test_source_guide_row_present() -> None:
    """A well-formed guide payload yields its summary and keyword list."""
    row = SourceGuideRow([[["id", ["A summary."], [["k1", "k2"]]]]])
    assert row.summary == "A summary."
    assert row.keywords == ["k1", "k2"]


def test_source_guide_row_empty_and_malformed_defaults() -> None:
    """Empty / malformed envelopes degrade to the ``""`` / ``[]`` defaults."""
    for result in (None, [], "x", [[]], [[["id"]]], [None]):
        row = SourceGuideRow(result)
        assert row.summary == ""
        assert row.keywords == []


# ---------------------------------------------------------------------------
# SourceFulltextRow — GET_SOURCE decode
# ---------------------------------------------------------------------------


_FULLTEXT_CASES = [
    [["id"]],  # descriptor too short
    [[["id"], "Title", [0, 1, 2, 3, 4]]],  # descriptor present, metadata present
    [[["id"], 42, [0, 1, 2, 3, 4]]],  # title non-str -> ""
    [[["id"], "T", "notmeta"]],  # metadata non-list
    [[["id"], "T"]],  # no metadata slot
    [[["id"], "T", [0, 1, 2, 3, 9]], None, None, [["a", "b"]], [None, "<html>"]],  # full
    [[["id"], "T", []], None, None, [], []],  # empty text & html blocks
    [[["id"], "T", []], None, None, ["notlist"], [None, 42]],  # text[0] non-list, html non-str
    [[["id"], "T", []], None, None, [["txt"]], [None]],  # html block too short
    ["notdesc"],  # descriptor non-list
]


@pytest.mark.parametrize("result", _FULLTEXT_CASES)
def test_source_fulltext_row_matches_legacy(result) -> None:
    """``SourceFulltextRow`` reproduces the legacy ``get_fulltext`` positional decode."""
    row = SourceFulltextRow(result)
    exp_title, exp_meta, exp_html, exp_text = _legacy_fulltext(result)
    assert row.title == exp_title, f"title mismatch for {result!r}"
    assert row.metadata == exp_meta, f"metadata mismatch for {result!r}"
    assert row.html_content == exp_html, f"html mismatch for {result!r}"
    assert row.text_content_blocks == exp_text, f"text mismatch for {result!r}"


def test_source_fulltext_row_present() -> None:
    """A full ``GET_SOURCE`` payload exposes all four positional values."""
    result = [
        [["id"], "Doc title", [None, None, None, None, 5]],
        None,
        None,
        [["chunk-a", "chunk-b"]],
        [None, "<p>html</p>"],
    ]
    row = SourceFulltextRow(result)
    assert row.title == "Doc title"
    assert row.metadata == [None, None, None, None, 5]
    assert row.text_content_blocks == ["chunk-a", "chunk-b"]
    assert row.html_content == "<p>html</p>"
    # The descriptor wraps as a SourceRow whose type_code reads metadata[4].
    source_row = row.source_row
    assert source_row is not None
    assert source_row.type_code == 5


def test_source_fulltext_row_too_short_descriptor_is_soft() -> None:
    """A descriptor too short to carry a title yields empty/None defaults."""
    row = SourceFulltextRow([["id"]])
    assert row.title == ""
    assert row.metadata is None
    assert row.source_row is None
    assert row.raw_metadata_type_slot is None
    assert row.html_content is None
    assert row.text_content_blocks is None


def test_source_fulltext_row_malformed_result_is_soft() -> None:
    """A non-list result degrades everywhere rather than raising."""
    row = SourceFulltextRow("notalist")
    assert row.title == ""
    assert row.metadata is None
    assert row.html_content is None
    assert row.text_content_blocks is None


def test_source_fulltext_row_raw_metadata_type_slot() -> None:
    """``raw_metadata_type_slot`` surfaces the raw ``metadata[4]`` for the WARNING."""
    # Present non-int type slot (the malformed-type-code case).
    row = SourceFulltextRow([[["id"], "T", [0, 1, 2, 3, "five"]]])
    assert row.raw_metadata_type_slot == "five"
    assert row.source_row is not None
    assert row.source_row.type_code is None  # non-int -> unknown
    # Metadata too short to carry the slot.
    short = SourceFulltextRow([[["id"], "T", [0, 1, 2, 3]]])
    assert short.raw_metadata_type_slot is None


# --- #1832: disambiguate the type_code==14 overload (native Sheet vs Drive PDF) ---


def _meta_with(*, type_code, mime=None, descriptor_mime=None):
    """Build a length-20 metadata array with the positions #1832 relies on.

    ``type_code`` at [4]; top-level MIME at [19]; the Drive-file descriptor
    ``[id, kind_int, mime, ""]`` at [9] when ``descriptor_mime`` is given.
    """
    meta = [None] * 20
    meta[4] = type_code
    if descriptor_mime is not None:
        meta[9] = ["drive-id", 8, descriptor_mime, ""]
    if mime is not None:
        meta[19] = mime
    return meta


def _row(meta):
    return SourceRow.from_entry([["src-id"], "Title", meta, [None, 2]])


def test_source_row_mime_reads_metadata_19() -> None:
    assert _row(_meta_with(type_code=14, mime="application/pdf")).mime == "application/pdf"


def test_source_row_mime_falls_back_to_drive_descriptor() -> None:
    # Top-level [19] absent but the drive descriptor [9][2] carries the mime.
    assert (
        _row(_meta_with(type_code=14, descriptor_mime="application/pdf")).mime == "application/pdf"
    )


def test_source_row_mime_none_when_absent() -> None:
    assert _row(_meta_with(type_code=14)).mime is None


def test_drive_pdf_type_code_14_decodes_to_pdf() -> None:
    """Real Drive-PDF row (captured #1832): type_code 14 + application/pdf → PDF."""
    from notebooklm._types.sources import Source, SourceType

    src = Source.from_row(
        _row(_meta_with(type_code=14, mime="application/pdf", descriptor_mime="application/pdf"))
    )
    assert src.kind == SourceType.PDF


def test_native_sheet_type_code_14_stays_spreadsheet() -> None:
    """Real native-Sheet row (captured #1832): type_code 14 + google-apps mime → GOOGLE_SPREADSHEET (no regression)."""
    from notebooklm._types.sources import Source, SourceType

    src = Source.from_row(
        _row(
            _meta_with(
                type_code=14,
                mime="application/vnd.google-apps.spreadsheet",
                descriptor_mime="application/vnd.google-apps.spreadsheet",
            )
        )
    )
    assert src.kind == SourceType.GOOGLE_SPREADSHEET


def test_type_code_14_no_mime_stays_drive() -> None:
    """type_code 14 with no MIME signal decodes as GOOGLE_DRIVE.

    14 is the backend's catch-all for a Drive file it gave no format-specific
    code; with nothing to disambiguate on, reporting that is the honest answer.
    """
    from notebooklm._types.sources import Source, SourceType

    src = Source.from_row(_row(_meta_with(type_code=14)))
    assert src.kind == SourceType.GOOGLE_DRIVE


def test_type_code_14_unknown_binary_mime_stays_drive() -> None:
    """An unrecognized MIME under 14 is left as GOOGLE_DRIVE, not relabeled/UNKNOWN."""
    from notebooklm._types.sources import Source, SourceType

    src = Source.from_row(_row(_meta_with(type_code=14, mime="application/x-mystery")))
    assert src.kind == SourceType.GOOGLE_DRIVE


def test_non_14_type_code_with_pdf_mime_is_untouched() -> None:
    """The MIME override only fires for type_code==14; other codes pass through.

    A native web page (code 5) carrying a stray ``application/pdf`` MIME must NOT
    be remapped — the disambiguation is gated on the ambiguous code 14 alone.
    """
    from notebooklm._types.sources import Source, SourceType

    src = Source.from_row(_row(_meta_with(type_code=5, mime="application/pdf")))
    assert src._type_code == 5
    assert src.kind == SourceType.WEB_PAGE


# --- #2112 + #2114: retained source content and revision metadata ----------


def _live_enriched_source_row(
    *,
    type_code: int = 8,
    content_mime: str = "text/markdown",
    drive_mime: str | None = None,
) -> SourceRow:
    """Build the live shape shared by the two source-enrichment issues."""
    metadata: list[object] = [None] * 20
    metadata[1] = 1498
    metadata[2] = [1_754_560_000, 123_000_000]
    metadata[3] = ["a25483bc-01fc-4e64-9deb-d2c2cf001887", [1_754_560_100, 456_000_000]]
    metadata[4] = type_code
    metadata[14] = [1_754_560_200, 789_000_000]
    metadata[19] = drive_mime
    return SourceRow.from_entry(
        [
            ["source-id"],
            "The AI-Enabled Organization.md",
            metadata,
            [None, SourceStatus.READY],
            None,
            "https://contribution.usercontent.google.com/download?c=token&filename=source.md",
            "https://drive.google.com/viewer/upload?ds=token",
            [
                "/contrib_service/blobrefs/notebooklm/nos_files/MediaDataBlobref/global::0000",
                None,
                content_mime,
                [["opaque-token"]],
            ],
        ]
    )


def test_source_row_decodes_original_file_links_and_content_mime() -> None:
    row = _live_enriched_source_row()

    assert row.download_url == (
        "https://contribution.usercontent.google.com/download?c=token&filename=source.md"
    )
    assert row.viewer_url == "https://drive.google.com/viewer/upload?ds=token"
    assert row.content_mime == "text/markdown"


def test_source_row_decodes_word_revision_and_last_modified_metadata() -> None:
    row = _live_enriched_source_row()

    assert row.word_count == 1498
    assert row.revision_id == "a25483bc-01fc-4e64-9deb-d2c2cf001887"
    assert row.revision_timestamp_raw == 1_754_560_100
    assert row.revision_timestamp == _datetime_from_timestamp(1_754_560_100)
    assert row.last_modified_at_raw == 1_754_560_200
    assert row.last_modified_at == _datetime_from_timestamp(1_754_560_200)


def test_source_from_row_surfaces_every_enriched_field() -> None:
    from notebooklm._types.sources import Source

    source = Source.from_row(_live_enriched_source_row())

    assert source.download_url is not None
    assert source.viewer_url is not None
    assert source.content_mime == "text/markdown"
    assert source.word_count == 1498
    assert source.revision_id == "a25483bc-01fc-4e64-9deb-d2c2cf001887"
    assert source.revision_timestamp == _datetime_from_timestamp(1_754_560_100)
    assert source.last_modified_at == _datetime_from_timestamp(1_754_560_200)


def test_source_repr_omits_signed_capability_urls() -> None:
    """Routine logging must not persist access-bearing source URL tokens."""
    from notebooklm._types.sources import Source

    download_token = "download-capability-token"
    viewer_token = "viewer-capability-token"
    source = Source(
        id="source-id",
        download_url=f"https://example.test/download?token={download_token}",
        viewer_url=f"https://example.test/view?token={viewer_token}",
    )

    rendered = repr(source)

    assert download_token not in rendered
    assert viewer_token not in rendered
    assert source.download_url is not None
    assert source.viewer_url is not None


def test_content_mime_disambiguates_drive_pdf_without_drive_metadata_mime() -> None:
    """The true content MIME is preferred over the older Drive-only slots."""
    from notebooklm._types.sources import Source, SourceType

    source = Source.from_row(
        _live_enriched_source_row(type_code=14, content_mime="application/pdf")
    )

    assert source.kind is SourceType.PDF


@pytest.mark.parametrize(
    "content_mime",
    ["application/octet-stream", "application/pdf; charset=binary"],
)
def test_drive_mime_disambiguates_when_content_mime_is_unrecognized(
    content_mime: str,
) -> None:
    """An unusable content MIME must not mask the proven Drive MIME fallback."""
    from notebooklm._types.sources import Source, SourceType

    source = Source.from_row(
        _live_enriched_source_row(
            type_code=14,
            content_mime=content_mime,
            drive_mime="application/pdf",
        )
    )

    assert source.kind is SourceType.PDF


def test_enriched_fields_are_none_on_short_rows() -> None:
    row = SourceRow.from_entry([["source-id"], "Title"])

    assert row.download_url is None
    assert row.viewer_url is None
    assert row.content_mime is None
    assert row.word_count is None
    assert row.revision_id is None
    assert row.revision_timestamp_raw is None
    assert row.revision_timestamp is None
    assert row.last_modified_at_raw is None
    assert row.last_modified_at is None


def test_enriched_fields_reject_malformed_values() -> None:
    metadata: list[object] = [None] * 15
    metadata[1] = True
    metadata[3] = [42, "not-a-timestamp"]
    metadata[14] = ["not-numeric"]
    row = SourceRow.from_entry(
        [
            ["source-id"],
            "Title",
            metadata,
            [None, SourceStatus.READY],
            None,
            42,
            "",
            ["blobref", None, 123],
        ]
    )

    assert row.download_url is None
    assert row.viewer_url is None
    assert row.content_mime is None
    assert row.word_count is None
    assert row.revision_id is None
    assert row.revision_timestamp is None
    assert row.last_modified_at is None


def test_source_row_rejects_boolean_timestamps() -> None:
    """JSON booleans are not numeric timestamps despite subclassing ``int``."""
    metadata: list[object] = [None] * 15
    metadata[2] = [True]
    metadata[3] = ["revision-id", [False]]
    metadata[14] = [True]
    row = SourceRow.from_entry([["source-id"], "Title", metadata, [None, SourceStatus.READY]])

    assert row.created_at_raw is None
    assert row.created_at is None
    assert row.revision_timestamp_raw is None
    assert row.revision_timestamp is None
    assert row.last_modified_at_raw is None
    assert row.last_modified_at is None


# --- #2113: SourceRow.drive_document_id (the add_drive idempotency key) ------

#: A Drive file id as it appears on the wire (44-char Base64URL), taken from the
#: live ADD_SOURCE capture in ``tests/cassettes/web/sources_add_drive.yaml``.
_DRIVE_FILE_ID = "1oAk_INJHbIPsIh49jgNqj3FESSGHZrzxFY7t05Lvvl0"


def _live_google_docs_row(document_id: str = _DRIVE_FILE_ID) -> SourceRow:
    """The live-captured Google-native Drive row (``metadata[0]`` block).

    Copied verbatim (ids aside) from the ADD_SOURCE response in
    ``tests/cassettes/web/sources_add_drive.yaml`` — note that NO url slot is
    populated, which is the whole reason #2113 existed.
    """
    return SourceRow.from_entry(
        [
            ["ef72c03c-b429-41cb-ae79-8529d35d6d5b"],
            "Rubisco Research: Status and Future",
            [
                [document_id, "SCRUBBED_AONS", 17],
                3737,
                [1769198541, 320332000],
                ["a25483bc-01fc-4e64-9deb-d2c2cf001887", [1769198540, 885478000]],
                1,
                None,
                1,
                None,
                7610,
            ],
            [None, 2],
        ]
    )


def test_drive_document_id_from_google_docs_metadata() -> None:
    """``metadata[0][0]`` is the Drive documentId on a Google-native row."""
    row = _live_google_docs_row()
    assert row.drive_document_id == _DRIVE_FILE_ID
    # The row that motivated #2113: an identity-bearing id but no URL at all.
    assert row.url is None


def test_drive_document_id_from_drive_descriptor() -> None:
    """``metadata[9][0]`` is the Drive documentId on a Drive-hosted binary row."""
    meta = _meta_with(type_code=14, mime="application/pdf", descriptor_mime="application/pdf")
    meta[9] = [_DRIVE_FILE_ID, 8, "application/pdf", ""]
    row = _row(meta)
    assert row.drive_document_id == _DRIVE_FILE_ID
    assert row.url is None


def test_drive_document_id_prefers_google_docs_block_when_both_present() -> None:
    """Both blocks populated: the ``metadata[0]`` block wins (documented order)."""
    meta = _meta_with(type_code=1)
    meta[0] = ["docs-id", "opaque", 17]
    meta[9] = ["descriptor-id", 8, "application/pdf", ""]
    assert _row(meta).drive_document_id == "docs-id"


def test_drive_document_id_falls_back_when_google_docs_block_absent() -> None:
    """An empty ``metadata[0]`` block does not shadow the descriptor."""
    meta = _meta_with(type_code=14)
    meta[0] = []
    meta[9] = ["descriptor-id", 8, "application/pdf", ""]
    assert _row(meta).drive_document_id == "descriptor-id"


def test_drive_document_id_is_none_for_web_page_row() -> None:
    """A live-shaped web-page row (``metadata[0]`` null, URL at ``[7]``) yields None.

    Shape from the GET_NOTEBOOK capture in
    ``tests/cassettes/web/sources_check_freshness_drive.yaml``.
    """
    row = SourceRow.from_entry(
        [
            ["e9e0faae-89b3-4e38-9528-90cd47d8d356"],
            "Google - Wikipedia",
            [
                None,
                28940,
                [1769104954, 651598000],
                ["1274153a-22ea-4acd-bb3d-886c695bff05", [1769104954, 395827000]],
                5,
                None,
                1,
                ["https://en.wikipedia.org/wiki/Google"],
            ],
            [None, 2],
        ]
    )
    assert row.drive_document_id is None
    assert row.url == "https://en.wikipedia.org/wiki/Google"


def test_drive_document_id_is_none_without_metadata() -> None:
    """A row with no metadata sub-list has no Drive id."""
    assert SourceRow.from_entry([["src-id"], "Title"]).drive_document_id is None


@pytest.mark.parametrize(
    "block",
    [
        # The legacy bare-http string honored by ``url_allow_bare_http``: a str
        # must never be indexed character-wise into a one-char "id".
        "https://example.com/page",
        None,
        [],  # present but empty
        [None],  # documentId slot explicitly null
        [""],  # documentId slot blank
        ["   "],  # documentId slot whitespace-only (unmatchable, so drift)
        [42],  # non-string
        {"documentId": "x"},  # non-list container
    ],
    ids=[
        "bare_http_str",
        "null",
        "empty",
        "null_id",
        "blank_id",
        "whitespace_id",
        "non_string",
        "non_list",
    ],
)
def test_drive_document_id_rejects_malformed_google_docs_block(block: object) -> None:
    meta = _meta_with(type_code=1)
    meta[0] = block
    assert _row(meta).drive_document_id is None


@pytest.mark.parametrize(
    "block",
    [None, [], [None], [""], ["   "], [42], "not-a-list"],
    ids=["null", "empty", "null_id", "blank_id", "whitespace_id", "non_string", "non_list"],
)
def test_drive_document_id_rejects_malformed_drive_descriptor(block: object) -> None:
    meta = _meta_with(type_code=14)
    meta[9] = block
    assert _row(meta).drive_document_id is None


def test_drive_document_id_survives_short_metadata() -> None:
    """A metadata list too short to reach ``[9]`` still reads ``[0]``."""
    assert _row([[_DRIVE_FILE_ID, "opaque", 17], None]).drive_document_id == _DRIVE_FILE_ID


def test_source_from_row_carries_drive_document_id() -> None:
    """``Source`` surfaces the id — this is what the add_drive probe matches on."""
    from notebooklm._types.sources import Source

    src = Source.from_row(_live_google_docs_row())
    assert src.drive_document_id == _DRIVE_FILE_ID
    assert src.url is None


def test_source_from_row_leaves_drive_document_id_none_for_non_drive() -> None:
    from notebooklm._types.sources import Source

    src = Source.from_row(_row(_meta_with(type_code=5)))
    assert src.drive_document_id is None


class TestDriveDocumentIdDriftWarning:
    """A present-but-drifted Drive block is reported, not silently skipped.

    #2113 hid for so long precisely because the client read a slot that never
    matched and said nothing. Structural absence stays quiet (most rows are not
    Drive rows), but a Drive block whose id slot stopped being a usable string is
    the signature of that bug recurring.
    """

    @pytest.fixture(autouse=True)
    def _reset_warned_slots(self):
        """Clear the warn-once set so assertions do not depend on test order.

        ``_warned_drive_id_slots`` is module-level (it has to outlive a row), so
        an earlier test decoding the same slot would otherwise consume this
        test's single warning. Mirrors ``_warned_status_codes`` handling in
        ``tests/unit/test_row_adapters.py::TestSourceRowStatus``.
        """
        _warned_drive_id_slots.clear()
        yield
        _warned_drive_id_slots.clear()

    def test_non_string_id_in_a_populated_block_warns(self, caplog) -> None:
        meta = _meta_with(type_code=1)
        meta[0] = [12345, "opaque", 17]  # documentId slot is not a string
        assert _row(meta).drive_document_id is None
        assert "carries no usable documentId" in caplog.text
        assert "metadata[0]" in caplog.text

    def test_blank_id_in_a_populated_block_warns(self, caplog) -> None:
        meta = _meta_with(type_code=14)
        meta[9] = ["", 8, "application/pdf", ""]
        assert _row(meta).drive_document_id is None
        assert "carries no usable documentId" in caplog.text
        assert "metadata[9]" in caplog.text

    def test_warns_once_per_slot(self, caplog) -> None:
        """A polled notebook re-decodes every row; the drift line fires once."""
        meta = _meta_with(type_code=1)
        meta[0] = [None, "opaque", 17]
        for _ in range(3):
            assert _row(meta).drive_document_id is None
        assert caplog.text.count("carries no usable documentId") == 1

        # A *different* slot is still reported.
        other = _meta_with(type_code=14)
        other[9] = [None, 8, "application/pdf", ""]
        assert _row(other).drive_document_id is None
        assert caplog.text.count("carries no usable documentId") == 2

    @pytest.mark.parametrize(
        ("block", "why"),
        [
            (None, "absent block — the overwhelmingly common non-Drive row"),
            ([], "block too short to hold the id slot at all"),
            ("https://example.com/page", "the legacy bare-http string at metadata[0]"),
        ],
        ids=["null", "empty", "bare_http_str"],
    )
    def test_structural_absence_stays_silent(self, caplog, block: object, why: str) -> None:
        meta = _meta_with(type_code=5)
        meta[0] = block
        assert _row(meta).drive_document_id is None
        assert "documentId" not in caplog.text, f"must not warn for {why}"

    def test_a_healthy_drive_row_never_warns(self, caplog) -> None:
        assert _live_google_docs_row().drive_document_id == _DRIVE_FILE_ID
        assert not caplog.records


# --- #2111: SourceRow.drive_status (Drive-side health) -----------------------
#
# Fixture provenance: the settings-block shapes below are the ones observed
# across 409 live source rows in the 2026-08-07 wire audit
# (Web and Android transports):
#
#     402x  [null, 2]                       — no Drive-status slot
#       4x  [null, 2, null, 3]              — Drive-backed, ACTIVE
#       3x  [null, 2, [null,null,null,[]]]  — a populated settings[2], no [3]
#
# ACTIVE is the ONLY Drive-status value that has been seen on the wire. The
# degraded members are exercised below from the backend enum
# (``docs/android/enums.txt::UserDriveSourceStatus``), NOT from a captured
# response — producing them requires deliberately breaking access to a real
# Drive file. Those rows are a claim about *our decoding*, not about the wire.


def _row_with_settings(settings: object) -> SourceRow:
    """A source entry whose settings block (``entry[3]``) is ``settings``."""
    return SourceRow.from_entry([["src-id"], "Title", _meta_with(type_code=1), settings])


def test_drive_status_decodes_the_live_active_row() -> None:
    """settings ``[null, 2, null, 3]`` — the only populated shape seen live."""
    row = _row_with_settings([None, 2, None, 3])
    assert row.drive_status is DriveSourceStatus.ACTIVE
    # Orthogonal axes: ingestion status is unchanged by the Drive slot.
    assert row.status is SourceStatus.READY


def test_drive_status_is_none_on_the_common_live_row() -> None:
    """settings ``[null, 2]`` — 402 of 409 live rows. No Drive claim at all."""
    row = _row_with_settings([None, 2])
    assert row.drive_status is None
    assert row.status is SourceStatus.READY


def test_drive_status_is_none_on_the_populated_settings_two_row() -> None:
    """settings ``[null, 2, [null,null,null,[]]]`` — 3 of 409 live rows.

    settings[2] is populated but settings[3] is still absent, so the block is
    long enough to be interesting and still makes no Drive claim.
    """
    assert _row_with_settings([None, 2, [None, None, None, []]]).drive_status is None


def test_drive_status_is_none_for_a_live_non_drive_row() -> None:
    """The captured web-page row (no Drive block anywhere) yields ``None``."""
    row = SourceRow.from_entry(
        [
            ["e9e0faae-89b3-4e38-9528-90cd47d8d356"],
            "Google - Wikipedia",
            [
                None,
                28940,
                [1769104954, 651598000],
                ["1274153a-22ea-4acd-bb3d-886c695bff05", [1769104954, 395827000]],
                5,
                None,
                1,
                ["https://en.wikipedia.org/wiki/Google"],
            ],
            [None, 2],
        ]
    )
    assert row.drive_status is None
    assert row.drive_document_id is None


def test_drive_status_is_none_when_the_settings_block_is_absent() -> None:
    """A short row with no settings block at all (e.g. some ADD_SOURCE shapes)."""
    assert SourceRow.from_entry([["src-id"], "Title"]).drive_status is None


@pytest.mark.parametrize(
    "settings",
    [None, "not-a-list", 3, {}],
    ids=["null", "str", "int", "dict"],
)
def test_drive_status_is_none_when_the_settings_block_is_not_a_list(settings: object) -> None:
    assert _row_with_settings(settings).drive_status is None


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (1, DriveSourceStatus.INACCESSIBLE),
        (2, DriveSourceStatus.SYNCING),
        (3, DriveSourceStatus.ACTIVE),
        (4, DriveSourceStatus.DELETED),
        (5, DriveSourceStatus.GEN_AI_ACCESS_DENIED),
    ],
    ids=["inaccessible", "syncing", "active", "deleted", "gen_ai_access_denied"],
)
def test_drive_status_maps_every_backend_enum_value(code: int, expected: DriveSourceStatus) -> None:
    """Each modelled backend ``UserDriveSourceStatus`` value decodes to its member.

    Only ``3`` (ACTIVE) has been observed on the wire; the rest are constructed
    from the recovered backend enum, so this pins the DECODE, not the wire.
    """
    assert _row_with_settings([None, 2, None, code]).drive_status is expected


def test_drive_status_explicit_null_slot_is_none() -> None:
    """A settings block long enough to reach [3] but carrying ``null`` there."""
    assert _row_with_settings([None, 2, None, None]).drive_status is None


def test_drive_status_explicit_unspecified_normalizes_to_none(caplog) -> None:
    """Backend ``0`` means "no claim" — the same thing an absent slot means.

    It is deliberately not a ``DriveSourceStatus`` member: one state must not
    have two representations. It is a *known* value, so it must not warn either.
    """
    assert _row_with_settings([None, 2, None, 0]).drive_status is None
    assert not caplog.records


class TestDriveStatusDrift:
    """A populated-but-unmappable Drive slot degrades to UNKNOWN and warns once.

    ``None`` means "the row made no Drive claim". An unmapped code means "the
    backend said something we could not read" — collapsing the second into the
    first is how #2111 stayed invisible in the first place.
    """

    @pytest.fixture(autouse=True)
    def _reset_warned_codes(self):
        _warned_drive_status_codes.clear()
        yield
        _warned_drive_status_codes.clear()

    @pytest.mark.parametrize(
        "code",
        [6, 99, -2, "3", 3.0, True, False, [3]],
        ids=["future_code", "far_code", "negative", "str", "float", "true", "false", "list"],
    )
    def test_unmapped_values_degrade_to_unknown(self, caplog, code: object) -> None:
        assert _row_with_settings([None, 2, None, code]).drive_status is (DriveSourceStatus.UNKNOWN)
        assert "Unknown Drive source status" in caplog.text
        # The warning is only actionable if it names WHICH value drifted and
        # which RPC produced it — a bare "something drifted" line is useless.
        assert repr(code) in caplog.text
        assert RPCMethod.GET_NOTEBOOK.value in caplog.text

    def test_false_is_not_decoded_as_the_unspecified_normalization(self, caplog) -> None:
        """``False == 0``, but a JSON ``false`` here is drift, not "no claim".

        It must reach ``UNKNOWN`` + a warning rather than silently taking the
        ``0 -> None`` branch.
        """
        assert _row_with_settings([None, 2, None, False]).drive_status is DriveSourceStatus.UNKNOWN
        assert "Unknown Drive source status" in caplog.text

    def test_the_same_value_from_a_second_rpc_is_still_reported(self, caplog) -> None:
        """Dedup is per ``(RPC, value)``, like ``_warned_drive_id_slots``."""
        entry = [["src-id"], "Title", _meta_with(type_code=1), [None, 2, None, 77]]
        assert SourceRow.from_entry(entry).drive_status is DriveSourceStatus.UNKNOWN
        assert (
            SourceRow.from_entry(entry, method_id=RPCMethod.ADD_SOURCE.value).drive_status
            is DriveSourceStatus.UNKNOWN
        )
        assert caplog.text.count("Unknown Drive source status") == 2
        assert RPCMethod.ADD_SOURCE.value in caplog.text

    def test_an_oversized_drift_payload_is_truncated(self, caplog) -> None:
        """A pathological payload cannot retain an unbounded warn-once key."""
        assert (
            _row_with_settings([None, 2, None, ["x" * 5000]]).drive_status
            is DriveSourceStatus.UNKNOWN
        )
        (rendered,) = {value for _method, value in _warned_drive_status_codes}
        assert len(rendered) <= 120

    def test_the_client_sentinel_is_not_a_wire_value(self, caplog) -> None:
        """A literal -1 is drift, not a silent round-trip of our own sentinel."""
        assert _row_with_settings([None, 2, None, -1]).drive_status is DriveSourceStatus.UNKNOWN
        assert "Unknown Drive source status" in caplog.text

    def test_warns_once_per_distinct_value(self, caplog) -> None:
        for _ in range(3):
            assert _row_with_settings([None, 2, None, 42]).drive_status is (
                DriveSourceStatus.UNKNOWN
            )
        assert caplog.text.count("Unknown Drive source status") == 1

        # A different unmapped value is still reported.
        assert _row_with_settings([None, 2, None, 43]).drive_status is DriveSourceStatus.UNKNOWN
        assert caplog.text.count("Unknown Drive source status") == 2

    def test_known_and_absent_values_never_warn(self, caplog) -> None:
        assert _row_with_settings([None, 2, None, 3]).drive_status is DriveSourceStatus.ACTIVE
        assert _row_with_settings([None, 2]).drive_status is None
        assert _row_with_settings([None, 2, None, None]).drive_status is None
        assert not caplog.records


def test_source_from_row_carries_drive_status() -> None:
    """``Source`` surfaces the decoded Drive health; ``is_ready`` is untouched."""
    from notebooklm._types.sources import Source

    src = Source.from_row(_row_with_settings([None, 2, None, 3]))
    assert src.drive_status is DriveSourceStatus.ACTIVE
    assert src.is_drive_degraded is False
    assert src.is_ready is True


@pytest.mark.parametrize(
    "settings",
    [None, "not-a-list", 3, {}],
    ids=["null", "str", "int", "dict"],
)
def test_status_also_fails_closed_on_a_non_list_settings_block(settings: object) -> None:
    """``status`` shares ``_settings_block`` with ``drive_status`` (#2111).

    Without this the ``isinstance(block, list)`` guard is only pinned by the
    Drive-side tests, and removing it would make ``status`` raise ``TypeError``
    from ``len()`` on a scalar settings block, with nothing to catch it.
    """
    assert _row_with_settings(settings).status is SourceStatus.UNKNOWN


@pytest.mark.parametrize(
    "raw",
    [
        [[[["src-id"], "T", None, [None, 2, None, 3]]]],
        [[["src-id"], "T", None, [None, 2, None, 3]]],
        ["src-id", "T", None, [None, 2, None, 3]],
    ],
    ids=["deeply_nested", "medium_nested", "flat"],
)
def test_from_api_response_decodes_drive_status_on_every_wire_shape(raw: list) -> None:
    """The ``ADD_SOURCE`` funnel (``sources.add_drive_file``'s return path).

    ``Source.from_api_response`` dispatches three wire shapes; a populated Drive
    slot must survive all three, since an add response is the one realistic
    place a caller sees one at creation time.
    """
    from notebooklm._types.sources import Source

    src = Source.from_api_response(raw)
    assert src.drive_status is DriveSourceStatus.ACTIVE
    assert src.is_drive_degraded is False


def test_source_from_row_leaves_drive_status_none_for_non_drive_rows() -> None:
    from notebooklm._types.sources import Source

    src = Source.from_row(_row_with_settings([None, 2]))
    assert src.drive_status is None
    assert src.is_drive_degraded is False
