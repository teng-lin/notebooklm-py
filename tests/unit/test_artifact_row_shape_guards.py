"""Shape rejections in the positional ``ArtifactRow`` decoder.

Every property here reads a fixed index out of a backend-controlled list. The
documented contract is that a short or malformed row degrades to an empty /
``None`` result rather than raising, so a server-side reshape surfaces as
missing data instead of an ``IndexError`` in the caller's face. These pin the
degrade paths; the happy paths are covered by the golden-row suites.
"""

from __future__ import annotations

from typing import Any

import pytest

from notebooklm._types.artifact_content import ArtifactMediaType
from notebooklm._web.rows.artifacts import ArtifactRow, ReportSuggestionRow
from notebooklm.exceptions import UnknownRPCMethodError
from notebooklm.rpc.types import ArtifactTypeCode

URL = "https://lh3.googleusercontent.com/asset"


def _row(**slots: Any) -> ArtifactRow:
    """Build a row with ``slots`` placed at their positional indices."""
    size = max(slots) + 1 if slots else 5
    raw: list[Any] = [None] * max(size, 5)
    raw[0] = "art-1"
    raw[1] = "Title"
    raw[2] = ArtifactTypeCode.AUDIO.value
    raw[4] = 3
    for index, value in slots.items():
        raw[index] = value
    return ArtifactRow(raw)


def _audio(media_list: Any) -> ArtifactRow:
    metadata: list[Any] = [None] * (ArtifactRow._AUDIO_MEDIA_LIST_POS + 1)
    metadata[ArtifactRow._AUDIO_MEDIA_LIST_POS] = media_list
    return _row(**{str(ArtifactRow._AUDIO_METADATA_POS): metadata})


def _audio_row(media_list: Any) -> ArtifactRow:
    metadata: list[Any] = [None] * (ArtifactRow._AUDIO_MEDIA_LIST_POS + 1)
    metadata[ArtifactRow._AUDIO_MEDIA_LIST_POS] = media_list
    raw: list[Any] = [None] * (ArtifactRow._AUDIO_METADATA_POS + 1)
    raw[0], raw[1], raw[2], raw[4] = "art-1", "Title", ArtifactTypeCode.AUDIO.value, 3
    raw[ArtifactRow._AUDIO_METADATA_POS] = metadata
    return ArtifactRow(raw)


# ---------------------------------------------------------------------------
# source_ids
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sources", "expected"),
    [
        pytest.param(None, (), id="absent"),
        pytest.param([], (), id="empty"),
        pytest.param([["s1"], ["s2"]], ("s1", "s2"), id="flat"),
        pytest.param([[["s1"]]], ("s1",), id="nested-one-level"),
        pytest.param(["not-a-list", ["s1"]], ("s1",), id="non-list-entry-skipped"),
        pytest.param([[], ["s1"]], ("s1",), id="empty-entry-skipped"),
        pytest.param([[7], ["s1"]], ("s1",), id="non-string-id-skipped"),
        pytest.param([[[]], ["s1"]], ("s1",), id="empty-nested-id-skipped"),
    ],
)
def test_source_ids_skips_entries_it_cannot_read(sources: Any, expected: tuple) -> None:
    row = ArtifactRow(["art-1", "Title", 1, sources, 3])

    assert row.source_ids == expected


# ---------------------------------------------------------------------------
# audio_url / _media_entries
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "media_list",
    [
        pytest.param(None, id="absent"),
        pytest.param("not-a-list", id="not-a-list"),
        pytest.param([], id="empty"),
        pytest.param(["not-a-list"], id="non-list-entry"),
        pytest.param([[]], id="empty-entry"),
        pytest.param([["ftp://elsewhere/x"]], id="untrusted-url"),
    ],
)
def test_audio_url_is_absent_for_an_unusable_media_list(media_list: Any) -> None:
    assert _audio_row(media_list).audio_url is None


def test_audio_url_prefers_the_mp4_entry_over_the_first_usable_one() -> None:
    row = _audio_row([[URL + "/fallback", 1, "audio/other"], [URL + "/mp4", 1, "audio/mp4"]])

    assert row.audio_url == URL + "/mp4"


def test_audio_url_falls_back_to_the_first_usable_entry() -> None:
    row = _audio_row(["skipped", [], [URL + "/first", 1, "audio/other"]])

    assert row.audio_url == URL + "/first"


@pytest.mark.parametrize(
    "media_list",
    [
        pytest.param("not-a-list", id="not-a-list"),
        pytest.param(["not-a-list"], id="non-list-entry"),
        pytest.param([[]], id="empty-entry"),
        pytest.param([["ftp://elsewhere/x"]], id="non-http-scheme"),
    ],
)
def test_media_entries_drops_everything_it_cannot_admit(media_list: Any) -> None:
    assert _audio_row(media_list).media_urls == ()


def test_media_entries_keeps_the_type_code_even_for_an_unknown_kind() -> None:
    row = _audio_row([[URL, 999, "audio/mp4"]])

    (entry,) = row.media_urls
    assert entry.url == URL
    assert entry.type_code == 999
    assert entry.kind is ArtifactMediaType.UNKNOWN


def test_media_entries_ignores_a_boolean_type_code() -> None:
    """``bool`` is an ``int`` subclass — admitting it would fabricate kind 1."""
    row = _audio_row([[URL, True, "audio/mp4"]])

    (entry,) = row.media_urls
    assert entry.type_code is None


# ---------------------------------------------------------------------------
# _image_fields
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param(None, (None, None, None), id="not-a-list"),
        pytest.param([], (None, None, None), id="empty"),
        pytest.param([URL], (URL, None, None), id="url-only"),
        pytest.param([URL, 800, 600], (URL, 800, 600), id="full"),
        pytest.param(["ftp://elsewhere/x", 800, 600], (None, 800, 600), id="non-http-scheme"),
        pytest.param([7, 800, 600], (None, 800, 600), id="non-string-url"),
        pytest.param([URL, "800", "600"], (URL, None, None), id="stringly-typed-size"),
        pytest.param([URL, True, False], (URL, None, None), id="boolean-size"),
    ],
)
def test_image_fields_admits_each_slot_independently(value: Any, expected: tuple) -> None:
    assert ArtifactRow._image_fields(value) == expected


# ---------------------------------------------------------------------------
# infographics / slides
# ---------------------------------------------------------------------------


def _infographic_row(items: Any) -> ArtifactRow:
    block: list[Any] = [None] * (ArtifactRow._INFOGRAPHIC_ITEMS_POS + 1)
    block[ArtifactRow._INFOGRAPHIC_ITEMS_POS] = items
    raw: list[Any] = [None] * (ArtifactRow._INFOGRAPHIC_METADATA_POS + 1)
    raw[0], raw[1], raw[2], raw[4] = "art-1", "Title", ArtifactTypeCode.INFOGRAPHIC.value, 3
    raw[ArtifactRow._INFOGRAPHIC_METADATA_POS] = block
    return ArtifactRow(raw)


@pytest.mark.parametrize(
    "items",
    [
        pytest.param("not-a-list", id="items-not-a-list"),
        pytest.param(["not-a-list"], id="non-list-item"),
    ],
)
def test_infographics_drops_unreadable_items(items: Any) -> None:
    assert _infographic_row(items).infographics == ()


def test_infographics_are_absent_when_the_block_is_short() -> None:
    raw: list[Any] = [None] * (ArtifactRow._INFOGRAPHIC_METADATA_POS + 1)
    raw[0], raw[1], raw[2], raw[4] = "art-1", "Title", ArtifactTypeCode.INFOGRAPHIC.value, 3
    raw[ArtifactRow._INFOGRAPHIC_METADATA_POS] = ["only-one"]

    assert ArtifactRow(raw).infographics == ()


def _slide_row(items: Any) -> ArtifactRow:
    block: list[Any] = [None] * (ArtifactRow._SLIDE_ITEMS_POS + 1)
    block[ArtifactRow._SLIDE_ITEMS_POS] = items
    raw: list[Any] = [None] * (ArtifactRow._SLIDE_DECK_METADATA_POS + 1)
    raw[0], raw[1], raw[2], raw[4] = "art-1", "Title", ArtifactTypeCode.SLIDE_DECK.value, 3
    raw[ArtifactRow._SLIDE_DECK_METADATA_POS] = block
    return ArtifactRow(raw)


@pytest.mark.parametrize(
    "items",
    [
        pytest.param("not-a-list", id="items-not-a-list"),
        pytest.param(["not-a-list"], id="non-list-item"),
    ],
)
def test_slides_drop_unreadable_items(items: Any) -> None:
    assert _slide_row(items).slides == ()


# ---------------------------------------------------------------------------
# report_markdown / report_kind
# ---------------------------------------------------------------------------


def _report_row(payload: Any) -> ArtifactRow:
    raw: list[Any] = [None] * (ArtifactRow._REPORT_MARKDOWN_POS + 1)
    raw[0], raw[1], raw[2], raw[4] = "art-1", "Title", ArtifactTypeCode.REPORT.value, 3
    raw[ArtifactRow._REPORT_MARKDOWN_POS] = payload
    return ArtifactRow(raw)


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(None, id="absent"),
        pytest.param([None], id="null-content"),
        pytest.param([[7]], id="non-string-content"),
    ],
)
def test_report_markdown_is_absent_for_an_unusable_payload(payload: Any) -> None:
    assert _report_row(payload).report_markdown is None


def test_an_empty_report_wrapper_is_reported_as_drift() -> None:
    """A present-but-empty wrapper is a reshape, not "no report"."""
    row = _report_row([])

    with pytest.raises(UnknownRPCMethodError):
        _ = row.report_markdown


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(None, id="absent"),
        pytest.param(["content"], id="no-options-block"),
        pytest.param(["content", "not-a-list"], id="options-not-a-list"),
        pytest.param(["content", []], id="options-empty"),
    ],
)
def test_report_kind_is_absent_for_an_unusable_options_block(payload: Any) -> None:
    assert _report_row(payload).report_kind is None


# ---------------------------------------------------------------------------
# ReportSuggestionRow._str_at bounds guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param(
            ["Title", "Desc", None, None, "Prompt"],
            ("Title", "Desc", "Prompt"),
            id="present",
        ),
        pytest.param(["Title"], ("Title", "", ""), id="short-row"),
        pytest.param([7, 8, 9, 10, 11], ("", "", ""), id="non-string-slots"),
        pytest.param("not-a-list", ("", "", ""), id="row-not-a-list"),
        pytest.param([], ("", "", ""), id="empty-row"),
    ],
)
def test_a_short_or_malformed_suggestion_row_degrades_to_empty_strings(
    raw: Any, expected: tuple[str, str, str]
) -> None:
    row = ReportSuggestionRow(raw)

    assert (row.title, row.description, row.prompt) == expected
