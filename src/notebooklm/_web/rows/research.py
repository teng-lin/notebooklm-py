"""Research row adapters for the ``POLL_RESEARCH`` (``e3bVqc``) payload.

These adapters centralise the positional knowledge that
``_web/rows/research_task.py`` previously open-coded as scattered single-level
subscripts (``result[0]``, ``src[1]``, ``bundle[0]``, ``task_info[1][0]`` …).
The parser wraps the raw lists in the typed views below and reads named
properties so a future Google reshape of the research wire format is a
one-place fix here, and so genuine drift on the *guaranteed* descents RAISES
``UnknownRPCMethodError`` via ``safe_index`` instead of silently degrading to
empty/wrong data (ADR-0011).

Two descent flavours are preserved exactly as the historical parser had them:

* **Guaranteed** slots — the two leading slots of a task row (``task_data[0]``
  task id, ``task_data[1]`` task info) — descend through ``safe_index`` so an
  absent slot RAISES (that is genuine shape drift; the task cannot be parsed
  without them).
* **Routinely-optional** slots — every research-source field and the
  query / sources / summary bundle reads — are length-guarded *inside* the
  adapter and short-circuit to a default, matching the parser's permissive
  contract (a deep-research source legitimately omits its URL, a fast-research
  task legitimately omits its summary, …).

Position contracts (pinned by ``tests/unit/test_research_row_adapter.py``):

* :class:`ResearchTaskRow` — one task row (``tasks[i]``):

  =====  ============================================================
  Index  Meaning
  =====  ============================================================
  0      task id (str) — GUARANTEED (``safe_index``)
  1      task info block (list) — GUARANTEED (``safe_index``)
  2      update time — ``[seconds, nanos]``
  3      create time — ``[seconds, nanos]``
  4      account id (str)
  =====  ============================================================

* :class:`ResearchTaskInfoRow` — one task info block (``task_data[1]``):

  =====  ============================================================
  Index  Meaning
  =====  ============================================================
  1      query block; ``[1][0]`` is the original query text
  2      ``DiscoveryMode`` code (int)
  3      sources/summary bundle; ``[3][0]`` sources list, ``[3][1]`` summary
  4      status code (int)
  =====  ============================================================

* :class:`ResearchResultRow` — one source row (``sources_data[i]``):

  =====  ============================================================
  Index  Meaning
  =====  ============================================================
  0      URL (str) for fast research; ``None`` sentinel for deep research
  1      title (str) — or, for the compat shape, ``[title, report]``
  2      hint (str) — the backend's one-line "why this source" note
  3      authoritative result-type tag
  6      typed content block; kind 3 carries report markdown at position 0
  8      deep-research per-task source ordinal (int)
  =====  ============================================================
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar

from ..._types.common import _datetime_from_timestamp
from ..._types.enums import DiscoveryMode
from ...rpc import RPCMethod, safe_index

logger = logging.getLogger("notebooklm._row_adapters.research")

#: Unmapped ``DiscoveryMode`` codes already warned about, so a polled research
#: task does not re-emit the same drift line on every poll. Sibling of
#: ``_web/rows/sources.py::_warned_drive_status_codes``, and keyed by
#: ``repr(value)`` for the same reason: a drifted slot can hold an unhashable
#: payload, and ``repr`` keeps ``1`` and ``"1"`` distinct.
_warned_discovery_modes: set[str] = set()

#: ``DISCOVERY_MODE_UNSPECIFIED`` — deliberately unmodelled on
#: :class:`~notebooklm.rpc.types.DiscoveryMode` (see its docstring), so the
#: decoder needs the bare wire value to normalize it to ``None``. Mirrors
#: ``_web/rows/sources.py::_DRIVE_STATUS_UNSPECIFIED``.
_DISCOVERY_MODE_UNSPECIFIED: int = 0

__all__ = [
    "ImportedSourceRow",
    "ResearchResultRow",
    "ResearchStartRow",
    "ResearchTaskInfoRow",
    "ResearchTaskRow",
    "unwrap_import_rows",
    "unwrap_poll_tasks",
]

# The ``POLL_RESEARCH`` method id and a stable source label, threaded into
# ``safe_index`` so drift on the guaranteed descents points at the right RPC.
_POLL_METHOD_ID = RPCMethod.POLL_RESEARCH.value

# Envelope-unwrap positions: ``POLL_RESEARCH`` returns either a wrapped
# envelope (``[[task1, ...]]``) or an already-flat list of tasks.
_ENVELOPE_OUTER_POS = 0
_ENVELOPE_PROBE_POS = 0


def unwrap_poll_tasks(result: Any) -> list[Any]:
    """Return the flat list of task rows from a raw ``POLL_RESEARCH`` result.

    ``POLL_RESEARCH`` returns either a wrapped envelope (``[[task1, ...]]``) or
    an already-flat list of tasks. This centralises that ``result[0]`` /
    ``result[0][0]`` envelope probe so the parser stops open-coding it; the
    reads are soft (an unrecognised shape falls through to ``result`` unchanged
    or ``[]``), preserving the historical ``_unwrap_poll_result`` contract.
    """
    if not result or not isinstance(result, list):
        return []
    first = result[_ENVELOPE_OUTER_POS]
    if isinstance(first, list) and len(first) > 0 and isinstance(first[_ENVELOPE_PROBE_POS], list):
        return first
    return result


@dataclass(frozen=True)
class ResearchTaskRow:
    """Typed view of one raw ``POLL_RESEARCH`` task row (``tasks[i]``).

    The two leading slots are GUARANTEED: a task that is missing its id
    (``task_data[0]``) or its info block (``task_data[1]``) is genuine shape
    drift, so both reads descend through ``safe_index`` and RAISE
    ``UnknownRPCMethodError`` on absence — mirroring the historical
    ``_extract_task_id`` / ``_extract_task_info`` contract exactly (an empty or
    non-list ``task_data`` raised; a present-but-wrong-type value logged and
    degraded to ``None``).
    """

    _raw: Any = field(repr=False)

    _ID_POS: ClassVar[int] = 0
    _INFO_POS: ClassVar[int] = 1
    # Timestamps (#2122). The pair is ``[seconds, nanos]``; only the seconds
    # are decoded, matching every other timestamp read in this client
    # (``NoteRow._TS_SECONDS_POS``, ``ArtifactRow``).
    #
    # WHICH SLOT IS WHICH was established by polling a live run twice: [2]
    # advanced by ~7.6s between the two polls while [3] stayed at the value
    # both slots held on the first poll. Reproduced on two accounts, and
    # corroborated by the repo's own cassettes (9/9 task rows: [2] advanced
    # across all 3 within-cassette transitions, [3] was constant across all 4).
    # #2122's issue text has these the other way round; the wire does not.
    _UPDATED_AT_POS: ClassVar[int] = 2
    _CREATED_AT_POS: ClassVar[int] = 3
    _ACCOUNT_ID_POS: ClassVar[int] = 4
    _TS_SECONDS_POS: ClassVar[int] = 0

    _ID_SOURCE: ClassVar[str] = "ResearchTaskRow.task_id"
    _INFO_SOURCE: ClassVar[str] = "ResearchTaskRow.task_info"

    @property
    def task_id_raw(self) -> Any:
        """Raw value at ``task_data[0]`` (str validated by the caller).

        Descends through ``safe_index`` so an absent id slot RAISES — a task
        row that cannot supply its id is genuine drift, not a soft default.
        """
        return safe_index(
            self._raw, self._ID_POS, method_id=_POLL_METHOD_ID, source=self._ID_SOURCE
        )

    @property
    def task_info_raw(self) -> Any:
        """Raw value at ``task_data[1]`` (list validated by the caller).

        Descends through ``safe_index`` so an absent info slot RAISES — a task
        row without its info block is genuine drift.
        """
        return safe_index(
            self._raw, self._INFO_POS, method_id=_POLL_METHOD_ID, source=self._INFO_SOURCE
        )

    def _timestamp_at(self, pos: int) -> datetime | None:
        """Decode the ``[seconds, nanos]`` timestamp pair at ``task_data[pos]``.

        Routinely-optional like every other non-leading task slot: an absent
        slot, a non-list, an empty pair, or a seconds value the stdlib refuses
        all degrade to ``None`` rather than raising — a poll payload that omits
        its timestamps is still a usable task.
        """
        if not isinstance(self._raw, list) or len(self._raw) <= pos:
            return None
        pair = self._raw[pos]
        if not isinstance(pair, list) or len(pair) <= self._TS_SECONDS_POS:
            return None
        seconds = pair[self._TS_SECONDS_POS]
        # ``bool`` is an ``int`` subclass; a wire ``true`` must not become the
        # epoch second 1.
        if not isinstance(seconds, (int, float)) or isinstance(seconds, bool):
            return None
        return _datetime_from_timestamp(seconds)

    @property
    def updated_at(self) -> datetime | None:
        """Last-update time from ``task_data[2]`` — ``None`` when absent (#2122)."""
        return self._timestamp_at(self._UPDATED_AT_POS)

    @property
    def created_at(self) -> datetime | None:
        """Creation time from ``task_data[3]`` — ``None`` when absent (#2122)."""
        return self._timestamp_at(self._CREATED_AT_POS)

    @property
    def account_id(self) -> str | None:
        """Owning account id at ``task_data[4]`` — ``None`` when absent (#2122).

        An opaque numeric-looking string. Two live accounts produced two
        distinct values, each identical across every task and poll of that
        account (``400237754469`` / ``838504205497``), which is what
        establishes it as account-scoped rather than a constant — the repo's
        cassettes alone could not, since all of them carry the same value.

        What it does NOT establish: whether it names the account that *started*
        the run or the one that *owns the notebook*. Both probes ran as the
        same account for both roles, so those are not distinguishable here.
        """
        if not isinstance(self._raw, list) or len(self._raw) <= self._ACCOUNT_ID_POS:
            return None
        value = self._raw[self._ACCOUNT_ID_POS]
        return value if isinstance(value, str) and value else None


class ResearchTaskInfoRow:
    """Typed view of one task info block (``task_data[1]``).

    The ``[1]`` query block, ``[3]`` sources/summary bundle, and ``[4]`` status
    code are themselves read through ``safe_index`` by the parser's
    ``_extract_*`` helpers (an absent slot is drift). This adapter centralises
    the *inner* single-level reads those helpers then perform on the bound
    blocks — ``query_info[0]``, ``query_info[1]``, ``bundle[0]``, ``bundle[1]``
    — which are routinely-optional and short-circuit to a default rather than
    raising.
    """

    _QUERY_TEXT_POS: ClassVar[int] = 0
    _QUERY_SOURCE_TYPE_POS: ClassVar[int] = 1
    _QUERY_SOURCE_TYPE_MIN_LEN: ClassVar[int] = 2
    #: ``DiscoveryMode`` echoed back on the task info block (#2122). Read from
    #: ``task_info`` itself, not from the query block — unlike the four
    #: positions around it, which are inner reads on already-bound sub-blocks.
    _DISCOVERY_MODE_POS: ClassVar[int] = 2
    _SOURCES_POS: ClassVar[int] = 0
    _SUMMARY_POS: ClassVar[int] = 1
    _SUMMARY_MIN_LEN: ClassVar[int] = 2

    @staticmethod
    def query_text(query_info: Any) -> Any:
        """First element of the query block (``task_info[1][0]``) or ``None``.

        ``query_info`` is ``task_info[1]`` (already validated as a list by the
        caller). An empty block legitimately means "no query text", so this
        short-circuits to ``None`` rather than raising.
        """
        return query_info[ResearchTaskInfoRow._QUERY_TEXT_POS] if query_info else None

    @staticmethod
    def query_source_type(query_info: Any) -> Any:
        """Search-source tag at ``task_info[1][1]`` — ``None`` when absent.

        The query block echoes the ``[query, source_type]`` pair that started
        the run (``1`` = web, ``2`` = drive — the same tags
        :meth:`ResearchAPI.start` sends). Live-captured against POLL_RESEARCH
        (issue #1964): a drive run echoes ``['notes', 2]``, a web run
        ``['python asyncio event loop', 1]``.

        Length-guarded soft read: a query block that carries only the text
        legitimately omits the tag, so this degrades to ``None`` instead of
        raising ``IndexError``.
        """
        if not query_info or len(query_info) < ResearchTaskInfoRow._QUERY_SOURCE_TYPE_MIN_LEN:
            return None
        return query_info[ResearchTaskInfoRow._QUERY_SOURCE_TYPE_POS]

    @staticmethod
    def discovery_mode(task_info: Any) -> DiscoveryMode | None:
        """Search mode at ``task_info[2]`` — ``None`` when the slot is absent.

        The same enum the ``START_*_RESEARCH`` params carry, echoed back on the
        poll, so a run's mode is confirmable from the response instead of only
        remembered from the request (#2122). Live: ``1``
        (``DEFAULT_LLM_SEARCH``) on every fast run and ``5``
        (``DEEP_RESEARCH``) on every deep cassette row.

        Return values mirror ``SourceRow.drive_status``:

        * ``None`` — absent slot / ``null`` / short block, and also an explicit
          ``0``: proto3 omits zero-valued fields, so ``DISCOVERY_MODE_UNSPECIFIED``
          means what an absent slot means and must not get a second
          representation.
        * A :class:`~notebooklm.rpc.types.DiscoveryMode` member for a mapped code.
        * :attr:`DiscoveryMode.UNKNOWN` when the slot IS populated with a code
          (or type) this client does not model — deliberately distinct from
          ``None``, so "the backend named a mode we can't read" never
          masquerades as "the backend named no mode". Warns once per raw value:
          ``wait_for_completion`` re-decodes this on every poll.
        """
        if not isinstance(task_info, list) or len(task_info) <= (
            pos := ResearchTaskInfoRow._DISCOVERY_MODE_POS
        ):
            return None
        code = task_info[pos]
        if code is None:
            return None
        # ``type(...) is int`` rejects ``bool`` (an ``int`` subclass) so a wire
        # ``true`` cannot decode to DEFAULT_LLM_SEARCH.
        if type(code) is int:
            # An explicit ``DISCOVERY_MODE_UNSPECIFIED`` (0) collapses into the
            # same ``None`` an absent slot yields: both say "no claim".
            if code == _DISCOVERY_MODE_UNSPECIFIED:
                return None
            # ``UNKNOWN`` (-1) is a client-side sentinel, not a wire value, so
            # a literal -1 is drift and must warn rather than round-trip.
            if code != DiscoveryMode.UNKNOWN:
                try:
                    return DiscoveryMode(code)
                except ValueError:
                    pass
        key = repr(code)
        if key not in _warned_discovery_modes:
            _warned_discovery_modes.add(key)
            logger.warning(
                "unmapped research discovery mode %r (POLL_RESEARCH task_info[%d]); "
                "reporting DiscoveryMode.UNKNOWN",
                code,
                pos,
            )
        return DiscoveryMode.UNKNOWN

    @staticmethod
    def bundle_sources(bundle: Any) -> Any:
        """Sources list at ``task_info[3][0]`` — ``None`` when the slot is absent.

        ``bundle`` is ``task_info[3]``. The parser's caller already
        short-circuits an empty bundle, but the adapter still length-guards its
        own read (mirroring :meth:`bundle_summary`) so an empty bundle degrades
        to the missing-slot default instead of raising ``IndexError`` — the
        soft-read contract this module documents. The caller coerces a
        non-list (including ``None``) to ``[]``.
        """
        if len(bundle) <= ResearchTaskInfoRow._SOURCES_POS:
            return None
        return bundle[ResearchTaskInfoRow._SOURCES_POS]

    @staticmethod
    def bundle_summary(bundle: Any) -> Any:
        """Summary at ``task_info[3][1]`` — ``None`` when the slot is absent.

        A sources-only bundle legitimately omits the summary, so this is a
        length-guarded soft read (``[1]`` only when ``len(bundle) >= 2``).
        """
        if len(bundle) < ResearchTaskInfoRow._SUMMARY_MIN_LEN:
            return None
        return bundle[ResearchTaskInfoRow._SUMMARY_POS]


@dataclass(frozen=True)
class ResearchResultRow:
    """Typed view of one raw research source row (``sources_data[i]``).

    The source row carries three shapes the historical parser handled inline:

    * fast research — ``[url, title, desc, type, ...]``
    * deep research (captured) — ``[None, title, None, type, ..., content_block]``
    * deep research (compat, never observed in a capture) —
      ``[None, [title, report_md], None, type, ...]``

    Every field is routinely-optional (a deep-research row omits its URL; a
    fast-research row omits the report), so the adapter length-guards each read
    and short-circuits to a default — preserving the parser's permissive
    contract (a malformed source is skipped, never raised). Position knowledge
    is centralised here; the parser reads named properties instead of
    ``src[0]`` / ``src[1]`` / ``src[3]`` / ``src[6]``.
    """

    _raw: Any = field(repr=False)

    _URL_POS: ClassVar[int] = 0
    _TITLE_POS: ClassVar[int] = 1
    _HINT_POS: ClassVar[int] = 2
    _RESULT_TYPE_POS: ClassVar[int] = 3
    _CONTENT_BLOCK_POS: ClassVar[int] = 6
    _SOURCE_ORDINAL_POS: ClassVar[int] = 8
    # A source row must carry at least ``[url/sentinel, title]`` to be usable —
    # mirrors the historical ``len(src) < 2`` early return.
    _MIN_LEN: ClassVar[int] = 2

    # Layout of the compat ``[title, report_markdown]`` payload packed at
    # ``src[1]`` — accepted but never observed in a capture (#2140).
    _PAYLOAD_TITLE_POS: ClassVar[int] = 0
    _PAYLOAD_REPORT_POS: ClassVar[int] = 1
    _PAYLOAD_MIN_LEN: ClassVar[int] = 2

    # Captured ``src[6]`` content block:
    # [text | None, kind, snippet | None, ..., structured_document | None]
    _CONTENT_TEXT_POS: ClassVar[int] = 0
    _CONTENT_KIND_POS: ClassVar[int] = 1
    _CONTENT_MIN_LEN: ClassVar[int] = 2
    _REPORT_CONTENT_KIND: ClassVar[int] = 3

    @property
    def is_well_formed(self) -> bool:
        """Whether the row is a list long enough to carry url/sentinel + title."""
        return isinstance(self._raw, list) and len(self._raw) >= self._MIN_LEN

    @property
    def length(self) -> int:
        """Length of the raw row (``0`` when not a list).

        Exposed so the parser can preserve its ``len(src) > 3`` / ``len(src) >= 3``
        branch conditions without re-reaching for ``self._raw``.
        """
        return len(self._raw) if isinstance(self._raw, list) else 0

    @property
    def url_slot(self) -> Any:
        """Raw value at ``src[0]`` — URL (str) for fast research, ``None`` sentinel
        for deep research. ``None`` when the slot is absent."""
        if self.length <= self._URL_POS:
            return None
        return self._raw[self._URL_POS]

    @property
    def title_slot(self) -> Any:
        """Raw value at ``src[1]`` — a title ``str`` or, for the compat shape,
        the ``[title, report_markdown]`` payload. ``None`` when absent."""
        if self.length <= self._TITLE_POS:
            return None
        return self._raw[self._TITLE_POS]

    @property
    def hint(self) -> str:
        """``DiscoveredSource.hint`` at ``src[2]`` — ``""`` when absent (#2122).

        The backend's own one-line explanation of why it surfaced this source
        (e.g. *"Practical walkthrough for managing I/O-bound tasks within a
        single execution thread."*). Live-populated on 10/10 fast-research rows
        in the #2122 probe; the deep-research report row carries ``None`` here,
        which is why this is a routinely-optional soft read like every other
        slot on this row.

        Empty string rather than ``None`` for absence, matching
        :attr:`report_markdown` and the ``str`` fields on
        :class:`~notebooklm._types.research.ResearchSource` (``url`` / ``title``
        / ``report_markdown`` are all non-optional ``str`` there).
        """
        if self.length <= self._HINT_POS:
            return ""
        value = self._raw[self._HINT_POS]
        return value if isinstance(value, str) else ""

    @property
    def result_type_slot(self) -> Any:
        """Authoritative result-type tag at ``src[3]`` — ``None`` when absent.

        The caller (``parse_result_type``) is responsible for normalising the
        value; the adapter only reports presence. ``None`` here makes the parser
        fall back to the web default exactly as ``len(src) > 3`` did.
        """
        if self.length <= self._RESULT_TYPE_POS:
            return None
        return self._raw[self._RESULT_TYPE_POS]

    @property
    def has_result_type(self) -> bool:
        """Whether ``src[3]`` is present (``len(src) > 3``)."""
        return self.length > self._RESULT_TYPE_POS

    @property
    def content_block(self) -> list[Any]:
        """Typed content block at ``src[6]`` — ``[]`` when absent or malformed."""
        if self.length <= self._CONTENT_BLOCK_POS:
            return []
        value = self._raw[self._CONTENT_BLOCK_POS]
        return value if isinstance(value, list) else []

    @property
    def report_markdown(self) -> str:
        """Report markdown from a kind-3 content block, otherwise ``""``."""
        block = self.content_block
        if len(block) < self._CONTENT_MIN_LEN:
            return ""
        kind = block[self._CONTENT_KIND_POS]
        if type(kind) is not int or kind != self._REPORT_CONTENT_KIND:
            return ""
        text = block[self._CONTENT_TEXT_POS]
        return text if isinstance(text, str) and text else ""

    @property
    def source_ordinal(self) -> int | None:
        """The backend's per-task ordinal at ``src[8]`` — ``None`` when absent.

        In the captures this is a 1-based bijection onto ``1..N`` over the
        discovered (kind-1) rows of one research task. It is **not** established
        to be the report's own citation numbering: #2141's evidence is a
        range/count coincidence, and ``tests/cassettes/web/research_deep_poll_long.yaml``
        carries 24 such ordinals against a report with no ``[cite: N]`` markers at
        all. Do not resolve report markers through this without new evidence.

        ``type(value) is int`` rather than ``isinstance``: ``bool`` is an ``int``
        subclass, so a wire ``true`` would otherwise read as ordinal ``1``.
        """
        if self.length <= self._SOURCE_ORDINAL_POS:
            return None
        value = self._raw[self._SOURCE_ORDINAL_POS]
        return value if type(value) is int else None

    @staticmethod
    def deep_payload(payload: Any) -> tuple[str, str] | None:
        """Unpack the compat ``[title, report_markdown]`` payload.

        Returns ``(title, report_markdown)`` only when ``payload`` is a list of
        at least two strings (the exact shape the historical parser required at
        ``src[1]``); otherwise ``None`` so the caller falls through to the
        bare-string-title / fast-research branches.

        This shape has **never appeared in a captured payload** — both real deep
        captures put a bare title here and carry the report in the ``src[6]``
        content block instead. #2140 asked for a deliberate decision, and it is
        kept rather than deleted: it costs one guarded branch, cannot mis-fire on
        the captured shapes (the guard above rejects them), and removing a
        never-observed *reader* would be a bet against payloads we have not
        captured — the wrong side of the bet for a wire whose drift is this
        project's #1 breakage class.
        """
        if (
            isinstance(payload, list)
            and len(payload) >= ResearchResultRow._PAYLOAD_MIN_LEN
            and isinstance(payload[ResearchResultRow._PAYLOAD_TITLE_POS], str)
            and isinstance(payload[ResearchResultRow._PAYLOAD_REPORT_POS], str)
        ):
            return (
                payload[ResearchResultRow._PAYLOAD_TITLE_POS],
                payload[ResearchResultRow._PAYLOAD_REPORT_POS],
            )
        return None


@dataclass(frozen=True)
class ResearchStartRow:
    """Typed view of a ``START_FAST_RESEARCH`` / ``START_DEEP_RESEARCH`` result.

    The kickoff RPCs return ``[task_id, report_id?, …]``. The caller guards the
    row as a non-empty list before constructing this view, so the ``task_id``
    slot is GUARANTEED present and descends through ``safe_index`` (an absent id
    slot on a non-empty row is genuine drift). The ``report_id`` slot is
    routinely-optional (a fast-research start omits it), so it is length-guarded
    and short-circuits to ``None``.

    Position knowledge is centralised here; ``_research.start`` reads named
    properties instead of ``result[0]`` / ``result[1]``.
    """

    _raw: Any = field(repr=False)

    _TASK_ID_POS: ClassVar[int] = 0
    _REPORT_ID_POS: ClassVar[int] = 1

    _TASK_ID_SOURCE: ClassVar[str] = "ResearchStartRow.task_id"

    @property
    def task_id_raw(self) -> Any:
        """Raw value at ``result[0]`` (truthiness validated by the caller).

        The caller guarantees a non-empty list before wrapping, so this descent
        is a no-op on the happy path; ``safe_index`` only fires if the id slot
        itself drifted out (genuine shape drift).
        """
        return safe_index(
            self._raw,
            self._TASK_ID_POS,
            method_id=None,
            source=self._TASK_ID_SOURCE,
        )

    @property
    def report_id(self) -> Any:
        """Optional value at ``result[1]`` — ``None`` when the slot is absent.

        A fast-research start legitimately omits the report id, so this is a
        length-guarded soft read (``[1]`` only when ``len(result) > 1``).
        """
        if len(self._raw) <= self._REPORT_ID_POS:
            return None
        return self._raw[self._REPORT_ID_POS]


# ``IMPORT_RESEARCH`` returns either a wrapped envelope (``[[src1, …]]``) or an
# already-flat list of imported-source rows. These positions centralise the
# envelope probe + per-row reads ``import_sources`` previously open-coded.
_IMPORT_ENVELOPE_OUTER_POS = 0
_IMPORT_ENVELOPE_PROBE_POS = 0
_IMPORT_ROW_ID_ENVELOPE_POS = 0
_IMPORT_ROW_TITLE_POS = 1
_IMPORT_ROW_MIN_LEN = 2


def _looks_like_import_row(value: Any) -> bool:
    """Whether ``value`` has the leading slots of an ``IMPORT_RESEARCH`` row."""
    if not isinstance(value, list) or len(value) < _IMPORT_ROW_MIN_LEN:
        return False
    id_envelope = value[_IMPORT_ROW_ID_ENVELOPE_POS]
    return id_envelope is None or isinstance(id_envelope, list)


def unwrap_import_rows(result: Any) -> list[Any]:
    """Return the flat list of imported-source rows from an ``IMPORT_RESEARCH`` result.

    ``IMPORT_RESEARCH`` returns either a wrapped envelope (``[[src1, …]]``) or an
    already-flat list of rows. This centralises the ``result[0]`` / ``result[0][0]``
    envelope probe so ``import_sources`` stops open-coding it; the reads are soft
    (an unrecognised shape falls through to ``result`` unchanged or ``[]``),
    with one extra disambiguation: the wrap is recognised only when
    ``result[0][0]`` itself looks like an imported-source row. That preserves
    flat single-row responses like ``[[[id], title]]`` instead of mistaking the
    row's id envelope for a wrapper.
    """
    if not result or not isinstance(result, list):
        return []
    first = result[_IMPORT_ENVELOPE_OUTER_POS]
    if (
        isinstance(first, list)
        and len(first) > 0
        and _looks_like_import_row(first[_IMPORT_ENVELOPE_PROBE_POS])
    ):
        return first
    return result


@dataclass(frozen=True)
class ImportedSourceRow:
    """Typed view of one ``IMPORT_RESEARCH`` imported-source row (``result[i]``).

    Each row is ``[[id, …], title, …]``: the id sits inside an envelope at
    ``[0][0]`` and the title at ``[1]``. Every read is routinely-optional (the
    response is documented as incomplete — a row may legitimately omit its id
    envelope), so the adapter length-guards each read and short-circuits to a
    default, preserving ``import_sources``'s permissive "skip rows without an
    id" contract (a malformed row is skipped, never raised). Position knowledge
    is centralised here; the consumer reads named properties instead of
    ``src_data[0]`` / ``id_envelope[0]`` / ``src_data[1]``.
    """

    _raw: Any = field(repr=False)

    _ID_ENVELOPE_POS: ClassVar[int] = _IMPORT_ROW_ID_ENVELOPE_POS
    _ID_POS: ClassVar[int] = 0
    _TITLE_POS: ClassVar[int] = _IMPORT_ROW_TITLE_POS
    # A usable row must carry at least ``[id_envelope, title]`` — mirrors the
    # historical ``len(src_data) >= 2`` guard.
    _MIN_LEN: ClassVar[int] = _IMPORT_ROW_MIN_LEN

    @property
    def is_well_formed(self) -> bool:
        """Whether the row is a list long enough to carry id envelope + title."""
        return isinstance(self._raw, list) and len(self._raw) >= self._MIN_LEN

    @property
    def source_id(self) -> Any:
        """Imported source id at ``src_data[0][0]`` — ``None`` when absent.

        An absent / falsy / non-list id envelope legitimately means "skip this
        row" (the historical contract), so it short-circuits to ``None`` rather
        than raising. The caller keeps the row only when this is truthy.
        """
        if not self.is_well_formed:
            return None
        envelope = self._raw[self._ID_ENVELOPE_POS]
        if not envelope or not isinstance(envelope, list):
            return None
        return envelope[self._ID_POS]

    @property
    def title_slot(self) -> Any:
        """Raw title at ``src_data[1]`` — ``None`` when the row is malformed."""
        if not self.is_well_formed:
            return None
        return self._raw[self._TITLE_POS]
