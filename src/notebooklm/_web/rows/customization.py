"""Row adapters for ``GetArtifactCustomizationChoices`` (``sqTeoe``).

The reply is one ``ArtifactCustomizationChoices`` message wrapped in the usual
single-element envelope::

    [[ <audio choices>, <video choices>, <slide-deck choices>, <report presets> ]]

Each family is itself a one-field message (``repeated ... = 1``), so a family
container is ``[[row, row, ...]]`` and its rows live at ``family[0]``:

* audio / video / slide-deck rows: ``[code, title, description]``
  (recovered ``SlidesType { DeckType deckType = 1; string title = 2; string
  description = 3 }`` — the audio and video families reuse the same layout with
  their own format enums);
* report rows: ``[report_type, description, directive]``
  (recovered ``TailoredReportTypeOption { reportType = 1; reportDescription = 2;
  reportDirective = 3 }``).

The envelope itself is load-bearing (the server always serves the table, so a
missing / non-list envelope is drift and :func:`unwrap_customization_choices`
raises); individual rows stay best-effort.

The Android APK schema declares only the slide-deck (tag 3) and report (tag 4)
families; the audio (tag 1) and video (tag 2) families are live-observed on
both front doors (2026-09-01) and registered as ``UNMAPPED`` in
``tests/_guardrails/_wire_contract.py``. Within a recognised envelope a short /
malformed *row* degrades to defaults rather than raising — the same permissive
contract as :class:`~notebooklm._web.rows.artifacts.ReportSuggestionRow`.
"""

from __future__ import annotations

import reprlib
from dataclasses import dataclass, field
from typing import Any, ClassVar

from ...exceptions import DecodingError

__all__ = [
    "CustomizationChoiceRow",
    "CustomizationChoicesRow",
    "ReportPresetRow",
    "unwrap_customization_choices",
]

# The single-element envelope: the ``ArtifactCustomizationChoices`` message is
# ``result[0]``.
_CHOICES_ENVELOPE_POS = 0


def unwrap_customization_choices(
    result: Any, *, method_id: str, source: str
) -> CustomizationChoicesRow:
    """Wrap the ``ArtifactCustomizationChoices`` message inside ``result``.

    The server always serves the table (live: ``[]``, a bogus notebook id and
    every artifact type return the same ~3.3 KB payload), so an absent or
    non-list envelope is schema drift and raises :class:`DecodingError` rather
    than degrading to four empty families — an empty ``artifact choices`` that
    exits 0 would hide exactly the re-shape this decode exists to notice.
    Per-row leniency lives on the row views, inside a recognised envelope.
    """
    if (
        not isinstance(result, list)
        or len(result) <= _CHOICES_ENVELOPE_POS
        or not isinstance(result[_CHOICES_ENVELOPE_POS], list)
    ):
        raise DecodingError(
            f"Unrecognized {source} response envelope",
            raw_response=reprlib.repr(result),
            method_id=method_id,
        )
    return CustomizationChoicesRow(result[_CHOICES_ENVELOPE_POS])


def _str_at(raw: Any, position: int) -> str:
    if not isinstance(raw, list) or len(raw) <= position:
        return ""
    value = raw[position]
    return value if isinstance(value, str) else ""


@dataclass(frozen=True)
class CustomizationChoiceRow:
    """One format choice row: ``[code, title, description]``."""

    _raw: Any = field(repr=False)

    _CODE_POS: ClassVar[int] = 0
    _TITLE_POS: ClassVar[int] = 1
    _DESCRIPTION_POS: ClassVar[int] = 2
    _MIN_LEN: ClassVar[int] = 2

    @property
    def code(self) -> int | None:
        if not isinstance(self._raw, list) or len(self._raw) <= self._CODE_POS:
            return None
        value = self._raw[self._CODE_POS]
        return value if type(value) is int else None

    @property
    def title(self) -> str:
        return _str_at(self._raw, self._TITLE_POS)

    @property
    def description(self) -> str:
        return _str_at(self._raw, self._DESCRIPTION_POS)

    @property
    def is_well_formed(self) -> bool:
        """A usable row carries at least an int code and a title."""
        return (
            isinstance(self._raw, list)
            and len(self._raw) >= self._MIN_LEN
            and self.code is not None
            and bool(self.title)
        )


@dataclass(frozen=True)
class ReportPresetRow:
    """One report preset row: ``[report_type, description, directive]``."""

    _raw: Any = field(repr=False)

    _REPORT_TYPE_POS: ClassVar[int] = 0
    _DESCRIPTION_POS: ClassVar[int] = 1
    _DIRECTIVE_POS: ClassVar[int] = 2
    _MIN_LEN: ClassVar[int] = 3

    @property
    def report_type(self) -> str:
        return _str_at(self._raw, self._REPORT_TYPE_POS)

    @property
    def description(self) -> str:
        return _str_at(self._raw, self._DESCRIPTION_POS)

    @property
    def directive(self) -> str:
        return _str_at(self._raw, self._DIRECTIVE_POS)

    @property
    def is_well_formed(self) -> bool:
        """A usable preset carries a name and the directive it expands to."""
        return (
            isinstance(self._raw, list)
            and len(self._raw) >= self._MIN_LEN
            and bool(self.report_type)
            and bool(self.directive)
        )


@dataclass(frozen=True)
class CustomizationChoicesRow:
    """Typed view of the ``ArtifactCustomizationChoices`` message.

    The four family slots each hold a one-field message whose repeated rows sit
    at ``family[0]`` (:attr:`_FAMILY_ROWS_POS`).
    """

    _raw: Any = field(repr=False)

    _AUDIO_POS: ClassVar[int] = 0
    _VIDEO_POS: ClassVar[int] = 1
    _SLIDE_DECK_POS: ClassVar[int] = 2
    _REPORTS_POS: ClassVar[int] = 3
    _FAMILY_ROWS_POS: ClassVar[int] = 0

    def _family_rows(self, position: int) -> list[Any]:
        if not isinstance(self._raw, list) or len(self._raw) <= position:
            return []
        family = self._raw[position]
        if not isinstance(family, list) or len(family) <= self._FAMILY_ROWS_POS:
            return []
        rows = family[self._FAMILY_ROWS_POS]
        return rows if isinstance(rows, list) else []

    @property
    def audio_rows(self) -> list[CustomizationChoiceRow]:
        return [CustomizationChoiceRow(row) for row in self._family_rows(self._AUDIO_POS)]

    @property
    def video_rows(self) -> list[CustomizationChoiceRow]:
        return [CustomizationChoiceRow(row) for row in self._family_rows(self._VIDEO_POS)]

    @property
    def slide_deck_rows(self) -> list[CustomizationChoiceRow]:
        return [CustomizationChoiceRow(row) for row in self._family_rows(self._SLIDE_DECK_POS)]

    @property
    def report_rows(self) -> list[ReportPresetRow]:
        return [ReportPresetRow(row) for row in self._family_rows(self._REPORTS_POS)]
