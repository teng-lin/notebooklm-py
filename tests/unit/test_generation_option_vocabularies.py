"""The generation option vocabularies must agree across the port (P10 R5.1a).

ADR-0035 addendum D1(a) splits one fact in two: ``_studio/generation.py`` owns
the reviewed *neutral* vocabulary a caller may name, and ``_web/codec`` owns the
map from those same strings onto Google's wire enums.  Nothing at runtime
compares them — a value the service accepts but the codec cannot map would reach
the port as an "unresolved generation input" contract error, and a value the
codec knows but the service rejects would be silently unreachable.

These tests are that comparison.  Each pair must stay key-for-key identical, so
adding a wire enum member is a two-line change the gate points at.
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from notebooklm._studio import generation as service
from notebooklm._web.codec import generation as codec
from notebooklm._web.codec import studio_documents as documents

VOCABULARIES: list[tuple[str, frozenset[str], Mapping[str, object]]] = [
    ("audio format", service.AUDIO_FORMATS, codec._AUDIO_FORMATS),
    ("audio length", service.AUDIO_LENGTHS, codec._AUDIO_LENGTHS),
    ("quiz quantity", service.QUIZ_QUANTITIES, codec._QUIZ_QUANTITIES),
    ("quiz difficulty", service.QUIZ_DIFFICULTIES, codec._QUIZ_DIFFICULTIES),
    (
        "infographic orientation",
        service.INFOGRAPHIC_ORIENTATIONS,
        codec._INFOGRAPHIC_ORIENTATIONS,
    ),
    ("infographic detail", service.INFOGRAPHIC_DETAILS, codec._INFOGRAPHIC_DETAILS),
    ("infographic style", service.INFOGRAPHIC_STYLES, codec._INFOGRAPHIC_STYLES),
    ("slide-deck format", service.SLIDE_DECK_FORMATS, codec._SLIDE_DECK_FORMATS),
    ("slide-deck length", service.SLIDE_DECK_LENGTHS, codec._SLIDE_DECK_LENGTHS),
    ("video format", service.VIDEO_FORMATS, documents._VIDEO_FORMATS),
    ("video style", service.VIDEO_STYLES, documents._VIDEO_STYLES),
    ("report format", service.REPORT_FORMATS, documents._REPORT_FORMATS),
]


@pytest.mark.parametrize(
    ("name", "reviewed", "wire"),
    VOCABULARIES,
    ids=[name for name, _reviewed, _wire in VOCABULARIES],
)
def test_the_service_vocabulary_is_exactly_the_codec_wire_map(
    name: str, reviewed: frozenset[str], wire: Mapping[str, object]
) -> None:
    assert reviewed == frozenset(wire), name
