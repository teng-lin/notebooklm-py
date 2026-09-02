"""Positional views for ``RetrieveRelevantChunks`` source-passage rows."""

from __future__ import annotations

import logging
import reprlib
from dataclasses import dataclass, field
from typing import Any, ClassVar

from ...exceptions import DecodingError
from ...types import RelevantChunk

__all__ = [
    "RelevantChunkRow",
    "RelevantChunkSourceRow",
    "decode_relevant_chunks",
    "unwrap_relevant_chunk_sources",
]


@dataclass(frozen=True)
class RelevantChunkSourceRow:
    """Typed view of one ``[source_id, [chunk, ...]]`` response row."""

    _raw: Any = field(repr=False)

    _SOURCE_ID_POS: ClassVar[int] = 0
    _CHUNKS_POS: ClassVar[int] = 1

    @property
    def source_id(self) -> str | None:
        if not isinstance(self._raw, list) or len(self._raw) <= self._SOURCE_ID_POS:
            return None
        value = self._raw[self._SOURCE_ID_POS]
        return value if isinstance(value, str) and value else None

    @property
    def chunk_rows(self) -> list[Any]:
        if not isinstance(self._raw, list) or len(self._raw) <= self._CHUNKS_POS:
            return []
        value = self._raw[self._CHUNKS_POS]
        return value if isinstance(value, list) else []

    @property
    def is_well_formed(self) -> bool:
        if self.source_id is None or not isinstance(self._raw, list):
            return False
        if len(self._raw) <= self._CHUNKS_POS:
            return False
        return self._raw[self._CHUNKS_POS] is None or isinstance(self._raw[self._CHUNKS_POS], list)


@dataclass(frozen=True)
class RelevantChunkRow:
    """Typed view of a ranked chunk and its optional source-relative span."""

    _raw: Any = field(repr=False)

    _CONTENT_POS: ClassVar[int] = 0
    _TEXT_POS: ClassVar[int] = 0
    _PARTS_POS: ClassVar[int] = 0
    _RANK_POS: ClassVar[int] = 1
    _SPANS_POS: ClassVar[int] = 2
    _FIRST_SPAN_POS: ClassVar[int] = 0
    _SPAN_START_POS: ClassVar[int] = 1
    _SPAN_END_POS: ClassVar[int] = 2

    @property
    def text(self) -> str | None:
        if not isinstance(self._raw, list) or len(self._raw) <= self._CONTENT_POS:
            return None
        content = self._raw[self._CONTENT_POS]
        if not isinstance(content, list) or len(content) <= self._TEXT_POS:
            return None
        text = content[self._TEXT_POS]
        if not isinstance(text, list) or len(text) <= self._PARTS_POS:
            return None
        parts = text[self._PARTS_POS]
        if not isinstance(parts, list) or not parts:
            return None
        if any(not isinstance(part, str) for part in parts):
            return None
        joined = "".join(parts)
        return joined or None

    @property
    def rank(self) -> int | None:
        if not isinstance(self._raw, list) or len(self._raw) <= self._RANK_POS:
            return None
        value = self._raw[self._RANK_POS]
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
        return None

    @property
    def span(self) -> tuple[int, int] | None:
        if not isinstance(self._raw, list) or len(self._raw) <= self._SPANS_POS:
            return None
        spans = self._raw[self._SPANS_POS]
        if not isinstance(spans, list) or not spans:
            return None
        span = spans[self._FIRST_SPAN_POS]
        if not isinstance(span, list) or len(span) <= self._SPAN_END_POS:
            return None
        start, end = span[self._SPAN_START_POS], span[self._SPAN_END_POS]
        if (
            any(
                not isinstance(value, int) or isinstance(value, bool) or value < 0
                for value in (start, end)
            )
            or start > end
        ):
            return None
        return start, end

    @property
    def is_well_formed(self) -> bool:
        return self.text is not None


def unwrap_relevant_chunk_sources(payload: Any, *, method_id: str) -> list[Any]:
    """Unwrap the live ``[[source-row, ...]]`` response envelope.

    Null and empty envelopes are legitimate no-result replies. A non-empty
    envelope with no list-shaped source row is foreign wire data and raises;
    once at least one row is recognizable, malformed siblings are returned for
    the row decoder to warn about and skip individually.
    """
    if payload is None or payload == [] or payload == [None] or payload == [[]]:
        return []
    if not isinstance(payload, list) or len(payload) != 1:
        raise DecodingError(
            "Unexpected RetrieveRelevantChunks response envelope",
            method_id=method_id,
        )
    rows = payload[0]
    if not isinstance(rows, list):
        raise DecodingError(
            "Unexpected RetrieveRelevantChunks source rows",
            method_id=method_id,
        )
    if rows and not any(isinstance(row, list) for row in rows):
        raise DecodingError(
            "RetrieveRelevantChunks response contains no source rows",
            method_id=method_id,
        )
    return rows


def decode_relevant_chunks(
    payload: Any,
    *,
    method_id: str,
    logger: logging.Logger,
) -> list[RelevantChunk]:
    """Decode a ``RetrieveRelevantChunks`` response in wire order."""
    decoded: list[RelevantChunk] = []
    for raw_source in unwrap_relevant_chunk_sources(payload, method_id=method_id):
        source = RelevantChunkSourceRow(raw_source)
        if not source.is_well_formed:
            logger.warning(
                "RetrieveRelevantChunks: skipping malformed source row: %s",
                reprlib.repr(raw_source),
            )
            continue
        source_id = source.source_id
        assert source_id is not None
        for raw_chunk in source.chunk_rows:
            chunk = RelevantChunkRow(raw_chunk)
            text = chunk.text
            if not chunk.is_well_formed or text is None:
                logger.warning(
                    "RetrieveRelevantChunks: skipping malformed chunk row for source %s: %s",
                    source_id,
                    reprlib.repr(raw_chunk),
                )
                continue
            span = chunk.span
            decoded.append(
                RelevantChunk(
                    source_id=source_id,
                    text=text,
                    rank=chunk.rank or 0,
                    start=None if span is None else span[0],
                    end=None if span is None else span[1],
                )
            )
    return decoded
