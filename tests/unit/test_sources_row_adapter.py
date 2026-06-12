"""Unit tests for the source response adapters drained from raw positional reads.

Covers the three additions that absorbed the ``_source/`` + ``_types/sources``
raw ``name[int]`` RPC-payload reads behind the sanctioned ``_row_adapters``
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

from notebooklm._row_adapters.sources import (
    SourceFulltextRow,
    SourceGuideRow,
    SourceRow,
)
from notebooklm._types.common import _datetime_from_timestamp
from notebooklm._types.sources import (
    _extract_source_created_at,
    _extract_source_url,
)

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
