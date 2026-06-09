"""Chat row adapters for the streamed-chat (``GenerateFreeFormStreamed``) payload.

The streamed-chat endpoint is **not** a ``batchexecute`` RPC, so there is no
obfuscated method ID to thread through ``safe_index`` — descents pass
``method_id=None`` and rely on named ``source`` labels to localise schema drift
in raised :class:`UnknownRPCMethodError` diagnostics (ADR-0011).

These adapters centralise the positional knowledge that ``_chat/wire.py``
previously open-coded as scattered single-level subscripts (``first[4]``,
``cite[1]``, ``cite_inner[5]``, ``passage_data[0]`` …). Consumer sites should
wrap the raw lists in the typed views below and read named properties so a
future Google reshape of the chat wire format is a one-place fix here, and so
genuine drift RAISES ``UnknownRPCMethodError`` via ``safe_index`` instead of
silently degrading to an empty/wrong answer.

Position contracts (pinned by ``tests/unit/test_chat_row_adapter.py``):

* :class:`AnswerRow` — one populated answer record (``inner_data[0]``):

  =====  ============================================================
  Index  Meaning
  =====  ============================================================
  0      answer text (str)
  2      conversation-id block; ``[2][0]`` is the server conversation id
  4      type/flags block; ``[4][-1] == 1`` marks an answer record and
         ``[4][3]`` is the citation list
  =====  ============================================================

* :class:`CitationRow` — one citation entry (``type_info[3][i]``):

  =====  ============================================================
  Index  Meaning
  =====  ============================================================
  0      chunk-id block; ``[0][0]`` is the chunk id
  1      citation detail block (:class:`CitationDetail`)
  =====  ============================================================

* :class:`CitationDetail` — ``cite[1]``:

  =====  ============================================================
  Index  Meaning
  =====  ============================================================
  2      relevance score (float 0.0-1.0)
  3      answer-range block ``[[None, answer_start, answer_end]]``
  4      source-side passages list
  5      nested source-id data
  =====  ============================================================

* :class:`PassageRow` — one passage record (``passage_wrapper[0]``):

  =====  ============================================================
  Index  Meaning
  =====  ============================================================
  0      source-side start char (int)
  1      source-side end char (int)
  2      nested text payload
  =====  ============================================================
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar

from ..rpc import safe_index

__all__ = [
    "AnswerRow",
    "CitationRow",
    "CitationDetail",
    "ErrorPayloadRow",
    "PassageRow",
    "StreamFrameRow",
    "TextLeafRow",
]


@dataclass(frozen=True)
class StreamFrameRow:
    """Typed view of one streamed-chat envelope frame.

    Frames arrive as ``["wrb.fr", None, inner_json, ...]`` (a successful RPC
    result) or ``["er", rpc_id, code, ...]`` (a server-side error). This adapter
    centralises the ``item[0]`` tag, ``item[2]`` inner-JSON / error-code, and
    ``item[5]`` error-payload reads so ``_chat/wire.py`` stops open-coding them
    (issue #1491).

    The ``tag`` read goes through ``safe_index`` because the caller already
    guarantees ``len(item) >= 2`` — the frame tag slot is the one position that
    must always be present, so its absence is genuine drift. Every other slot is
    optional and short-circuits to ``None`` on a short frame.
    """

    _raw: list[Any] = field(repr=False)

    _TAG_POS: ClassVar[int] = 0
    _INNER_JSON_POS: ClassVar[int] = 2
    _ERROR_CODE_POS: ClassVar[int] = 2
    _ERROR_PAYLOAD_POS: ClassVar[int] = 5

    #: Source label for ``safe_index`` drift diagnostics on the tag descent.
    _SOURCE: ClassVar[str] = "ChatStreamFrameRow.tag"

    @property
    def tag(self) -> Any:
        """Frame tag at ``item[0]`` (``"wrb.fr"`` / ``"er"``).

        The caller guarantees ``len(item) >= 2`` so this is a no-op on the happy
        path; ``safe_index`` only fires if the tag slot itself drifted out.
        """
        return safe_index(self._raw, self._TAG_POS, method_id=None, source=self._SOURCE)

    @property
    def inner_json(self) -> Any:
        """Inner-JSON payload at ``item[2]`` (a ``str`` for ``wrb.fr`` frames)."""
        if len(self._raw) <= self._INNER_JSON_POS:
            return None
        return self._raw[self._INNER_JSON_POS]

    @property
    def error_code(self) -> Any:
        """Optional error code at ``item[2]`` of an ``"er"`` frame.

        Read with a length guard (not ``safe_index``): an absent code is normal
        for a short ``"er"`` frame and must NOT be treated as schema drift — the
        frame itself is the error signal.
        """
        if len(self._raw) <= self._ERROR_CODE_POS:
            return None
        return self._raw[self._ERROR_CODE_POS]

    @property
    def error_payload(self) -> list[Any] | None:
        """Optional server-side error payload at ``item[5]`` (a list) or ``None``."""
        if len(self._raw) <= self._ERROR_PAYLOAD_POS:
            return None
        value = self._raw[self._ERROR_PAYLOAD_POS]
        return value if isinstance(value, list) else None


@dataclass(frozen=True)
class ErrorPayloadRow:
    """Typed view of a streamed-chat error payload (``item[5]``).

    Structure: ``[8, None, [["type.googleapis.com/.../UserDisplayableError", …]]]``.
    Centralises the ``error_payload[2]`` and inner ``entry[0]`` reads so
    ``raise_if_rate_limited`` stops open-coding them (issue #1491).
    """

    _raw: list[Any] = field(repr=False)

    _ENTRIES_POS: ClassVar[int] = 2

    @property
    def entries(self) -> list[Any]:
        """Error entries at ``error_payload[2]`` — ``[]`` when absent/non-list."""
        if len(self._raw) <= self._ENTRIES_POS:
            return []
        value = self._raw[self._ENTRIES_POS]
        return value if isinstance(value, list) else []

    @staticmethod
    def entry_type(entry: Any) -> str | None:
        """The leading type string at ``entry[0]`` of one error entry, or ``None``."""
        if not isinstance(entry, list) or not entry:
            return None
        value = entry[0]
        return value if isinstance(value, str) else None


@dataclass(frozen=True)
class TextLeafRow:
    """Typed view of one deeply-nested passage text leaf (``inner`` triple).

    Centralises the ``inner[2]`` text-payload read in ``collect_texts_from_nested``
    so the nested-walk decoder stops open-coding the position (issue #1491).
    """

    _raw: Any = field(repr=False)

    _TEXT_POS: ClassVar[int] = 2
    _MIN_LEN: ClassVar[int] = 3

    @property
    def is_well_formed(self) -> bool:
        """Whether the leaf is a list long enough to carry the text payload."""
        return isinstance(self._raw, list) and len(self._raw) >= self._MIN_LEN

    @property
    def text_value(self) -> Any:
        """Raw text payload at ``inner[2]`` (str / list validated upstream)."""
        if not self.is_well_formed:
            return None
        return self._raw[self._TEXT_POS]


@dataclass(frozen=True)
class AnswerRow:
    """Typed view of one populated streamed-chat answer record.

    The wrapped row is ``inner_data[0]`` of a decoded ``wrb.fr`` envelope
    whose ``inner_data`` is a populated list (heartbeats decode to ``[]``
    and never reach this adapter). Position knowledge is centralised here;
    consumer sites should NEVER open-code ``first[0]`` / ``first[2][0]`` /
    ``first[4][-1]`` / ``first[4][3]``.

    The dataclass is frozen so the wrapped row can't be mutated through the
    adapter; the adapter never copies the raw row, so it is cheap to build.
    """

    # Wrapped row; ``repr=False`` so logs don't explode with the entire
    # streamed-chat payload when an AnswerRow appears in a stack trace.
    _raw: list[Any] = field(repr=False)

    # ---- Position constants (the canary contract) ------------------------
    # ClassVar so the frozen dataclass treats these as class-level constants
    # rather than instance fields. If any of these change,
    # ``tests/unit/test_chat_row_adapter.py::TestAnswerRowPositionContract``
    # MUST be updated in the same commit — that failure is the wire-shape
    # change signal.
    _TEXT_POS: ClassVar[int] = 0
    _CONV_BLOCK_POS: ClassVar[int] = 2
    _TYPE_BLOCK_POS: ClassVar[int] = 4
    _ANSWER_MARKER_POS: ClassVar[int] = -1
    _CITATIONS_POS: ClassVar[int] = 3
    _ANSWER_MARKER_VALUE: ClassVar[int] = 1

    @property
    def raw(self) -> list[Any]:
        """The wrapped raw answer row."""
        return self._raw

    @property
    def text(self) -> str | None:
        """Answer text at ``first[0]`` — ``None`` when absent or not a string.

        The caller guarantees ``len(self._raw) > 0`` before constructing the
        row, so the ``safe_index`` descent is a no-op on the happy path; the
        ``ChatAnswerRow.text`` label localises any top-level reshape in
        diagnostics.
        """
        if len(self._raw) <= self._TEXT_POS:
            return None
        value = safe_index(
            self._raw,
            self._TEXT_POS,
            method_id=None,
            source="ChatAnswerRow.text",
        )
        return value if isinstance(value, str) and value else None

    @property
    def server_conversation_id(self) -> str | None:
        """Server conversation id at ``first[2][0]``.

        An absent / empty / non-list block legitimately means "no server
        conversation id present" (not drift) so it short-circuits to ``None``
        before invoking ``safe_index``.
        """
        if len(self._raw) <= self._CONV_BLOCK_POS:
            return None
        conv_block = self._raw[self._CONV_BLOCK_POS]
        if not isinstance(conv_block, list) or not conv_block:
            return None
        value = conv_block[0]
        return value if isinstance(value, str) else None

    @property
    def _type_block(self) -> list[Any] | None:
        """The optional type/flags block at ``first[4]`` (a list) or ``None``.

        An absent block legitimately means "not an answer record" (non-answer
        records carry no type block), so a short row or a non-list slot
        short-circuits to ``None`` rather than tripping ``safe_index``.
        """
        if len(self._raw) <= self._TYPE_BLOCK_POS:
            return None
        block = self._raw[self._TYPE_BLOCK_POS]
        return block if isinstance(block, list) else None

    @property
    def is_answer(self) -> bool:
        """Whether the type block marks this record as an answer (``[4][-1] == 1``).

        An absent / empty type block legitimately means "not an answer", so the
        flag read is a single-level ``type_block[-1]`` index on a bound local
        rather than a chained ``first[4][-1]`` descent.
        """
        type_block = self._type_block
        return (
            type_block is not None
            and len(type_block) > 0
            and type_block[self._ANSWER_MARKER_POS] == self._ANSWER_MARKER_VALUE
        )

    @property
    def citations(self) -> list[Any]:
        """Raw citation entries at ``first[4][3]`` — empty list when absent.

        Mirrors the historical ``parse_citations`` permissive contract: a
        short / non-list type block or a non-list citation slot degrades to
        ``[]`` rather than raising, because a missing citation list is a normal
        "answer without citations" shape, not wire drift.
        """
        type_block = self._type_block
        if type_block is None or len(type_block) <= self._CITATIONS_POS:
            return []
        citations = type_block[self._CITATIONS_POS]
        return citations if isinstance(citations, list) else []

    def citation_rows(self) -> list[CitationRow]:
        """Wrap each raw citation entry as a :class:`CitationRow`."""
        return [CitationRow(cite) for cite in self.citations]


@dataclass(frozen=True)
class CitationRow:
    """Typed view of one streamed-chat citation entry (``type_info[3][i]``).

    Centralises the ``cite[0][0]`` chunk-id and ``cite[1]`` detail-block
    position knowledge. Consumer sites should NEVER open-code ``cite[0]`` /
    ``cite[1]``.
    """

    _raw: Any = field(repr=False)

    _CHUNK_BLOCK_POS: ClassVar[int] = 0
    _DETAIL_POS: ClassVar[int] = 1
    _MIN_LEN: ClassVar[int] = 2

    @property
    def is_well_formed(self) -> bool:
        """Whether the entry is a list long enough to carry chunk + detail."""
        return isinstance(self._raw, list) and len(self._raw) >= self._MIN_LEN

    @property
    def chunk_id(self) -> str | None:
        """Chunk id at ``cite[0][0]``.

        An absent / empty / non-list chunk block legitimately means "no chunk
        id" (the citation is still kept), so it short-circuits to ``None``.
        """
        if not self.is_well_formed:
            return None
        chunk_block = self._raw[self._CHUNK_BLOCK_POS]
        if not isinstance(chunk_block, list) or not chunk_block:
            return None
        value = chunk_block[0]
        return value if isinstance(value, str) else None

    @property
    def detail(self) -> CitationDetail | None:
        """The citation detail block at ``cite[1]`` as a :class:`CitationDetail`.

        Returns ``None`` when the entry is too short or ``cite[1]`` is not a
        list — both legitimately mean "unusable citation, skip it" rather than
        wire drift.
        """
        if not self.is_well_formed:
            return None
        inner = self._raw[self._DETAIL_POS]
        if not isinstance(inner, list):
            return None
        return CitationDetail(inner)


@dataclass(frozen=True)
class CitationDetail:
    """Typed view of a citation detail block (``cite[1]``).

    Centralises the score / answer-range / passages / source-id position
    knowledge. Consumer sites should NEVER open-code ``cite_inner[2]`` /
    ``cite_inner[3]`` / ``cite_inner[4]`` / ``cite_inner[5]``.
    """

    _raw: list[Any] = field(repr=False)

    _SCORE_POS: ClassVar[int] = 2
    _ANSWER_RANGE_POS: ClassVar[int] = 3
    _PASSAGES_POS: ClassVar[int] = 4
    _SOURCE_ID_POS: ClassVar[int] = 5

    # Inner answer-range layout: ``cite_inner[3] = [[None, start, end]]``.
    _ANSWER_RANGE_START_POS: ClassVar[int] = 1
    _ANSWER_RANGE_END_POS: ClassVar[int] = 2

    @property
    def raw_list(self) -> list[Any]:
        """The wrapped ``cite[1]`` detail list (for legacy raw-list consumers)."""
        return self._raw

    @property
    def raw_score(self) -> Any:
        """Raw value at ``cite_inner[2]`` (validation lives in the caller)."""
        if len(self._raw) <= self._SCORE_POS:
            return None
        return self._raw[self._SCORE_POS]

    @property
    def source_id_data(self) -> Any:
        """Nested source-id data at ``cite_inner[5]`` — ``None`` when absent."""
        if len(self._raw) <= self._SOURCE_ID_POS:
            return None
        return self._raw[self._SOURCE_ID_POS]

    @property
    def passages(self) -> list[Any]:
        """Source-side passages list at ``cite_inner[4]`` — ``[]`` when absent."""
        if len(self._raw) <= self._PASSAGES_POS:
            return []
        value = self._raw[self._PASSAGES_POS]
        return value if isinstance(value, list) else []

    def answer_range(self) -> tuple[Any, Any]:
        """Raw ``(start, end)`` from ``cite_inner[3][0]`` (``[None, start, end]``).

        Returns ``(None, None)`` when the answer-range block is absent,
        not a list, empty, its first element is not a list, or that inner
        list is too short — all legitimate "no answer range" shapes, not drift.
        The numeric / ordering validation lives in the caller.
        """
        if len(self._raw) <= self._ANSWER_RANGE_POS:
            return None, None
        outer = self._raw[self._ANSWER_RANGE_POS]
        if not isinstance(outer, list) or not outer:
            return None, None
        inner = outer[0]
        if not isinstance(inner, list) or len(inner) <= self._ANSWER_RANGE_END_POS:
            return None, None
        return inner[self._ANSWER_RANGE_START_POS], inner[self._ANSWER_RANGE_END_POS]


@dataclass(frozen=True)
class PassageRow:
    """Typed view of one source-side passage *wrapper* (``cite_inner[4][i]``).

    The wrapped value is the outer ``passage_wrapper`` (``[passage_data, …]``);
    the adapter unwraps the inner ``passage_data`` at ``[0]`` and centralises its
    ``[0]`` / ``[1]`` / ``[2]`` start / end / text-payload reads. Consumer sites
    should NEVER open-code ``passage_wrapper[0]`` or ``passage_data[0..2]``
    (issue #1491).
    """

    _raw: Any = field(repr=False)

    _PASSAGE_DATA_POS: ClassVar[int] = 0
    _START_POS: ClassVar[int] = 0
    _END_POS: ClassVar[int] = 1
    _TEXT_PAYLOAD_POS: ClassVar[int] = 2
    _MIN_LEN: ClassVar[int] = 3

    @property
    def _passage_data(self) -> list[Any] | None:
        """Inner ``passage_data`` at ``passage_wrapper[0]`` when well-formed.

        Returns ``None`` (rather than raising) for an empty wrapper or an inner
        record too short to carry start/end/text — both legitimate "skip this
        passage" shapes, not wire drift.
        """
        if not isinstance(self._raw, list) or len(self._raw) <= self._PASSAGE_DATA_POS:
            return None
        data = self._raw[self._PASSAGE_DATA_POS]
        if not isinstance(data, list) or len(data) < self._MIN_LEN:
            return None
        return data

    @property
    def is_well_formed(self) -> bool:
        """Whether the wrapper holds an inner record long enough for start/end/text."""
        return self._passage_data is not None

    @property
    def start_char(self) -> Any:
        """Raw source-side start char at ``passage_data[0]`` (int validated upstream)."""
        data = self._passage_data
        return None if data is None else data[self._START_POS]

    @property
    def end_char(self) -> Any:
        """Raw source-side end char at ``passage_data[1]`` (int validated upstream)."""
        data = self._passage_data
        return None if data is None else data[self._END_POS]

    @property
    def text_payload(self) -> Any:
        """Nested text payload at ``passage_data[2]`` — ``None`` when malformed."""
        data = self._passage_data
        return None if data is None else data[self._TEXT_PAYLOAD_POS]
