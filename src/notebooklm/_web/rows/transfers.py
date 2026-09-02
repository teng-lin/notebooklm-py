"""Row adapters for the source/artifact transfer RPCs (#2283).

Three ``batchexecute`` replies share the "mapping row" idea — each entry pairs
an *input* identifier with a freshly created row:

* ``COPY_SOURCES`` (``R27wvc`` / ``CopySourcesAsync``):
  ``[[ [[orig_id], <source entry>], ... ]]``
* ``COPY_ARTIFACTS`` (``mKDdke`` / ``CopyArtifactsAsync``):
  ``[[ [orig_id, <artifact row>], ... ]]``
* ``ADD_SOURCES_ASYNC`` (``X1snv`` / ``AddSourcesAsync``):
  ``[ [<source entry>, ...], None, [ [<source entry>, ack], ... ] ]``

Position knowledge lives here (ADR-0011 / the ``_web/rows`` convention) so the
API layer reads named properties and never subscripts a payload directly. The
shapes are web-derived: none of these messages appear in the recovered Android
app schema, so the constants are registered as ``UNMAPPED`` in
``tests/_guardrails/_wire_contract.py`` with the live evidence on record.

Mapping rows are load-bearing for a *mutating* RPC — the returned ids are the
only proof of which writes committed — so, unlike the suggestion rows, a
malformed entry is not silently dropped: :func:`unwrap_mapping_rows` raises
``DecodingError`` when the envelope is not a list of entries, and each row's
``is_well_formed`` lets the caller fail closed on a short entry.
"""

from __future__ import annotations

import reprlib
from dataclasses import dataclass, field
from typing import Any, ClassVar

from ...exceptions import DecodingError

__all__ = [
    "AddSourcesAsyncResponseRow",
    "CopiedArtifactRow",
    "CopiedSourceRow",
    "SourceAckRow",
    "unwrap_mapping_rows",
]

# Both copy replies wrap their repeated mapping entries in a single-element
# envelope: the entry list is ``result[0]``.
_MAPPING_ROWS_POS = 0


def unwrap_mapping_rows(result: Any, *, method_id: str, source: str) -> list[Any]:
    """Return the mapping-entry list from a ``Copy*Async`` reply.

    An empty reply (``[]`` / ``None``) decodes to ``[]`` — the server answers
    that way when nothing was copied. Any other non-list envelope is schema
    drift and raises :class:`DecodingError`, because a copy whose result we
    cannot read must not be reported as a success with zero rows.
    """
    if result is None or result == []:
        return []
    if not isinstance(result, list) or len(result) <= _MAPPING_ROWS_POS:
        raise DecodingError(
            f"Unrecognized {source} response envelope",
            raw_response=reprlib.repr(result),
            method_id=method_id,
        )
    rows = result[_MAPPING_ROWS_POS]
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise DecodingError(
            f"Unrecognized {source} response envelope",
            raw_response=reprlib.repr(result),
            method_id=method_id,
        )
    return rows


@dataclass(frozen=True)
class CopiedSourceRow:
    """One ``CopySourcesAsync`` mapping entry: ``[[original_id], <source entry>]``.

    Index 0 is the original ``SourceId`` (``[id]`` wrapper, the same single
    nesting ``DeleteSources`` / ``MutateSource`` use); index 1 is the new
    source's entry row (``[[id], title, metadata, settings]``), the shape
    :meth:`~notebooklm._web.rows.sources.SourceRow.from_entry` wraps.
    """

    _raw: Any = field(repr=False)

    _ORIGINAL_POS: ClassVar[int] = 0
    _SOURCE_POS: ClassVar[int] = 1
    _MIN_LEN: ClassVar[int] = 2

    @property
    def original_id(self) -> str | None:
        """The id of the *original* source (the one that was copied), or ``None``.

        Live the slot is the ``[id]`` ``SourceId`` wrapper; a bare string is
        tolerated as the same value un-wrapped (a defensive reading of the
        one-field message, not an observed variant).
        """
        if not isinstance(self._raw, list) or len(self._raw) <= self._ORIGINAL_POS:
            return None
        wrapper = self._raw[self._ORIGINAL_POS]
        if isinstance(wrapper, list) and wrapper and isinstance(wrapper[0], str):
            return wrapper[0] or None
        if isinstance(wrapper, str):
            return wrapper or None
        return None

    @property
    def source_entry(self) -> list[Any] | None:
        """The new source's entry row, or ``None`` when absent / not a list."""
        if not isinstance(self._raw, list) or len(self._raw) <= self._SOURCE_POS:
            return None
        entry = self._raw[self._SOURCE_POS]
        return entry if isinstance(entry, list) else None

    @property
    def is_well_formed(self) -> bool:
        return (
            isinstance(self._raw, list)
            and len(self._raw) >= self._MIN_LEN
            and self.original_id is not None
            and self.source_entry is not None
        )


@dataclass(frozen=True)
class CopiedArtifactRow:
    """One ``CopyArtifactsAsync`` mapping entry: ``[original_id, <artifact row>]``.

    Index 0 is the original artifact id as a bare string (artifact ids are not
    wrapped on this service); index 1 is the full new artifact row, the shape
    :class:`~notebooklm._web.rows.artifacts.ArtifactRow` decodes.
    """

    _raw: Any = field(repr=False)

    _ORIGINAL_POS: ClassVar[int] = 0
    _ARTIFACT_POS: ClassVar[int] = 1
    _MIN_LEN: ClassVar[int] = 2

    @property
    def original_id(self) -> str | None:
        if not isinstance(self._raw, list) or len(self._raw) <= self._ORIGINAL_POS:
            return None
        value = self._raw[self._ORIGINAL_POS]
        return value if isinstance(value, str) and value else None

    @property
    def artifact_row(self) -> list[Any] | None:
        if not isinstance(self._raw, list) or len(self._raw) <= self._ARTIFACT_POS:
            return None
        row = self._raw[self._ARTIFACT_POS]
        return row if isinstance(row, list) else None

    @property
    def is_well_formed(self) -> bool:
        return (
            isinstance(self._raw, list)
            and len(self._raw) >= self._MIN_LEN
            and self.original_id is not None
            and self.artifact_row is not None
        )


@dataclass(frozen=True)
class SourceAckRow:
    """One ``AddSourcesAsync`` acknowledgement: ``[<source entry>, status]``.

    The status int was ``0`` on every live acknowledgement observed (the #2283
    web probe with two URLs, the Android probe and cassettes with one); its non-zero meanings are
    unrecovered, so it is surfaced verbatim rather than interpreted.
    """

    _raw: Any = field(repr=False)

    _SOURCE_POS: ClassVar[int] = 0
    _STATUS_POS: ClassVar[int] = 1
    #: The only acknowledgement status observed live (both front doors).
    KNOWN_OK: ClassVar[int] = 0

    @property
    def source_entry(self) -> list[Any] | None:
        if not isinstance(self._raw, list) or len(self._raw) <= self._SOURCE_POS:
            return None
        entry = self._raw[self._SOURCE_POS]
        return entry if isinstance(entry, list) else None

    @property
    def status(self) -> int | None:
        if not isinstance(self._raw, list) or len(self._raw) <= self._STATUS_POS:
            return None
        value = self._raw[self._STATUS_POS]
        return value if type(value) is int else None

    @property
    def is_ok(self) -> bool:
        """``True`` for the observed ``0`` status (an absent slot is not ok)."""
        return self.status == self.KNOWN_OK


@dataclass(frozen=True)
class AddSourcesAsyncResponseRow:
    """Typed view of the whole ``AddSourcesAsync`` reply.

    ``[0]`` is the repeated stub ``Source`` list (id, url and type only — no
    word count or ingest timestamps, the sources are still queued); ``[2]`` is
    the repeated per-source acknowledgement (:class:`SourceAckRow`). ``[1]`` was
    ``null`` on every observation.
    """

    _raw: Any = field(repr=False)

    _SOURCES_POS: ClassVar[int] = 0
    # Index 1 (proto tag 2) was ``null`` on every observation and is not read.
    _ACKS_POS: ClassVar[int] = 2

    @property
    def source_entries(self) -> list[Any]:
        if not isinstance(self._raw, list) or len(self._raw) <= self._SOURCES_POS:
            return []
        entries = self._raw[self._SOURCES_POS]
        return entries if isinstance(entries, list) else []

    @property
    def ack_rows(self) -> list[SourceAckRow]:
        if not isinstance(self._raw, list) or len(self._raw) <= self._ACKS_POS:
            return []
        acks = self._raw[self._ACKS_POS]
        if not isinstance(acks, list):
            return []
        return [SourceAckRow(item) for item in acks]
