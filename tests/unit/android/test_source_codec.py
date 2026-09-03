"""Edge-case coverage for the Android source codec's rejection branches.

``tests/unit/android/test_notebook_source_reads.py`` pins the populated decode
and ``test_source_writes.py`` drives guide selection through the public API.
These cases call the codec directly to reach the branches neither suite can
produce: a source row the backend sent without any metadata block, the
``DecodingError`` pass-through that keeps a drift signal from being reworded
into a generic one, and the wire-tag reader used to describe a rejected guide.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from notebooklm._android.codecs.sources import (
    _top_level_tags,
    decode_source,
    decode_sources,
    select_document_guide,
)
from notebooklm._android.proto.google.internal.labs.tailwind.orchestration.v1 import (
    read_pb2,
    sources_pb2,
)
from notebooklm.exceptions import DecodingError
from notebooklm.types import SourceStatus

METHOD_ID = "test-method"
SOURCE_A = "source-a"
SOURCE_B = "source-b"

LOGGER = logging.getLogger("tests.android.source_codec")


def _source(source_id: str = SOURCE_A, *, title: str = "Title") -> Any:
    return read_pb2.Source(source_id=read_pb2.SourceId(id=source_id), title=title)


class _ExplodingSource:
    """Stands in for a row whose shape drifted out from under the codec."""

    def __init__(self, error: BaseException) -> None:
        self._error = error

    def HasField(self, _name: str) -> bool:
        raise self._error


# ---------------------------------------------------------------------------
# decode_source
# ---------------------------------------------------------------------------


def test_decode_source_without_a_metadata_block_invents_nothing() -> None:
    """``metadata`` is optional on the wire; its absence is data, not an error.

    Every field the metadata block would have supplied has to stay unset rather
    than fall back to a plausible default -- a guessed ``_type_code`` would be
    reported to callers as a real source kind.
    """
    decoded = decode_source(_source(), method_id=METHOD_ID)

    assert (decoded.id, decoded.title) == (SOURCE_A, "Title")
    assert decoded.created_at is None
    assert decoded.url is None
    assert decoded.drive_document_id is None
    assert decoded.content_mime is None
    assert decoded.expert_intelligence is None
    assert decoded._type_code == 0
    # No ``settings`` block either, so the status stays explicitly unknown.
    assert decoded.status is SourceStatus.UNKNOWN
    assert decoded.drive_status is None


def test_decode_source_passes_an_inner_decoding_error_through_unchanged() -> None:
    """A named drift signal must not be reworded into the generic wrapper."""
    inner = DecodingError("inner drift", method_id="inner-method")

    with pytest.raises(DecodingError) as raised:
        decode_source(_ExplodingSource(inner), method_id=METHOD_ID)

    assert raised.value is inner


def test_decode_source_wraps_an_unexpected_shape_with_its_index() -> None:
    with pytest.raises(DecodingError, match="Could not decode Android source response at index 4"):
        decode_source(_ExplodingSource(AttributeError("gone")), method_id=METHOD_ID, index=4)


# ---------------------------------------------------------------------------
# decode_sources
# ---------------------------------------------------------------------------


def test_decode_sources_passes_an_inner_decoding_error_through_unchanged() -> None:
    """The list path shares the pass-through contract with the single decode."""
    inner = DecodingError("inner drift", method_id="inner-method")

    with pytest.raises(DecodingError) as raised:
        decode_sources(
            [_source(), _ExplodingSource(inner)],
            method_id=METHOD_ID,
            strict=False,
            logger=LOGGER,
        )

    assert raised.value is inner


@pytest.mark.parametrize("strict", [True, False], ids=["strict", "lenient"])
def test_decode_sources_wraps_an_unexpected_shape_at_its_index(strict: bool) -> None:
    """An unreadable row is fatal in both modes: only a *missing id* is skippable."""
    with pytest.raises(DecodingError, match="Could not decode Android source response at index 1"):
        decode_sources(
            [_source(), _ExplodingSource(AttributeError("gone"))],
            method_id=METHOD_ID,
            strict=strict,
            logger=LOGGER,
        )


# ---------------------------------------------------------------------------
# _top_level_tags (the diagnostic reader behind a rejected guide)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        pytest.param(b"", [], id="empty"),
        pytest.param(b"\x08\x96\x01", [1], id="varint"),
        pytest.param(b"\x12\x02hi", [2], id="length-delimited"),
        pytest.param(b"\x1d\x00\x00\x00\x00", [3], id="fixed32"),
        pytest.param(b"\x21" + b"\x00" * 8, [4], id="fixed64"),
        pytest.param(b"\x08\x01\x12\x02hi\x1d\x00\x00\x00\x00", [1, 2, 3], id="mixed"),
    ],
)
def test_top_level_tags_reads_every_wire_type_without_reading_values(
    payload: bytes, expected: list[int]
) -> None:
    """Each wire type has its own skip rule; a wrong length would desynchronize.

    A payload whose values were mis-skipped would report the *value* bytes as
    further tags, so the exact list -- not merely its first entry -- is the
    assertion that bites.
    """
    assert _top_level_tags(payload) == expected


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        pytest.param(b"\x0b", [1, -1], id="unsupported-wire-type"),
        pytest.param(b"\x12\x05ab", [2, -1], id="length-runs-past-payload"),
        pytest.param(b"\x08", [1, -1], id="truncated-varint"),
        pytest.param(b"\x08\x80", [1, -1], id="unterminated-varint"),
    ],
)
def test_top_level_tags_marks_an_unparsable_tail_instead_of_raising(
    payload: bytes, expected: list[int]
) -> None:
    """The reader only ever runs while building an error, so it must not raise.

    ``-1`` is the sentinel for *"the tags past this point are unknown"*; the
    tags read before the damage are kept because they are what the diagnosis
    turns on.
    """
    assert _top_level_tags(payload) == expected


def test_rejected_guide_reports_tags_of_every_wire_type_it_carries() -> None:
    """The tag list is read off the wire, so unmodelled fields must show up.

    ``DocumentGuide`` models tags 1-3 only. A label that had *moved* rather
    than been dropped would appear here as an unmodelled tag, which is the
    whole reason the diagnostic exists (#2276) -- and the varint/fixed32/
    fixed64 forms have to survive the reader as well as the string form.
    """
    guide = sources_pb2.DocumentGuide()
    guide.source.source_id.id = SOURCE_B
    guide.MergeFromString(
        guide.SerializeToString()
        + b"\x38\x96\x01"  # tag 7, varint
        + b"\x45\x00\x00\x00\x00"  # tag 8, fixed32
        + b"\x49"
        + b"\x00" * 8  # tag 9, fixed64
    )
    response = sources_pb2.GenerateDocumentGuidesResponse(guides=[guide])

    with pytest.raises(DecodingError) as raised:
        select_document_guide(response, source_id=SOURCE_A, method_id=METHOD_ID)

    assert raised.value.raw_response == "[1,7,8,9]"
    assert raised.value.found_ids == [SOURCE_B]


def test_rejected_guide_carrying_an_unreadable_field_still_reports_its_tags() -> None:
    """A group-encoded unknown field is unskippable, and must not mask the error.

    The reader is only reached from the rejection path, so an exception here
    would replace a precise "did not match the requested source id" with an
    unrelated failure.
    """
    guide = sources_pb2.DocumentGuide()
    guide.source.source_id.id = SOURCE_B
    # Tag 7 as a group (wire type 3): preserved as an unknown field, and
    # re-serialized in a form ``_skip_value`` deliberately refuses.
    guide.MergeFromString(guide.SerializeToString() + b"\x3b\x3c")
    response = sources_pb2.GenerateDocumentGuidesResponse(guides=[guide])

    with pytest.raises(DecodingError, match="did not match the requested source id") as raised:
        select_document_guide(response, source_id=SOURCE_A, method_id=METHOD_ID)

    assert raised.value.raw_response == "[1,7,-1]"
