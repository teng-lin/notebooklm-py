"""Research row adapters for the ``POLL_RESEARCH`` (``e3bVqc``) payload.

These adapters centralise the positional knowledge that
``_research_task_parser.py`` previously open-coded as scattered single-level
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
  =====  ============================================================

* :class:`ResearchTaskInfoRow` — one task info block (``task_data[1]``):

  =====  ============================================================
  Index  Meaning
  =====  ============================================================
  1      query block; ``[1][0]`` is the original query text
  3      sources/summary bundle; ``[3][0]`` sources list, ``[3][1]`` summary
  4      status code (int)
  =====  ============================================================

* :class:`ResearchResultRow` — one source row (``sources_data[i]``):

  =====  ============================================================
  Index  Meaning
  =====  ============================================================
  0      URL (str) for fast research; ``None`` sentinel for deep research
  1      title (str) — or, for current deep research, ``[title, report]``
  3      authoritative result-type tag
  6      legacy deep-research report chunks (list of str)
  =====  ============================================================
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar

from ..rpc import RPCMethod, safe_index

__all__ = [
    "ResearchResultRow",
    "ResearchTaskInfoRow",
    "ResearchTaskRow",
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


class ResearchTaskInfoRow:
    """Typed view of one task info block (``task_data[1]``).

    The ``[1]`` query block, ``[3]`` sources/summary bundle, and ``[4]`` status
    code are themselves read through ``safe_index`` by the parser's
    ``_extract_*`` helpers (an absent slot is drift). This adapter centralises
    the *inner* single-level reads those helpers then perform on the bound
    blocks — ``query_info[0]``, ``bundle[0]``, ``bundle[1]`` — which are
    routinely-optional and short-circuit to a default rather than raising.
    """

    _QUERY_TEXT_POS: ClassVar[int] = 0
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
    * deep research (legacy) — ``[None, title, None, type, ..., [report_md]]``
    * deep research (current) — ``[None, [title, report_md], None, type, ...]``

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
    _RESULT_TYPE_POS: ClassVar[int] = 3
    _LEGACY_CHUNKS_POS: ClassVar[int] = 6
    # A source row must carry at least ``[url/sentinel, title]`` to be usable —
    # mirrors the historical ``len(src) < 2`` early return.
    _MIN_LEN: ClassVar[int] = 2

    # Layout of the current-deep-research ``[title, report_markdown]`` payload
    # packed at ``src[1]``.
    _PAYLOAD_TITLE_POS: ClassVar[int] = 0
    _PAYLOAD_REPORT_POS: ClassVar[int] = 1
    _PAYLOAD_MIN_LEN: ClassVar[int] = 2

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
        """Raw value at ``src[1]`` — a title ``str`` or, for current deep
        research, the ``[title, report_markdown]`` payload. ``None`` when absent."""
        if self.length <= self._TITLE_POS:
            return None
        return self._raw[self._TITLE_POS]

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
    def legacy_report_chunks(self) -> list[Any]:
        """Legacy deep-research report chunks at ``src[6]`` — ``[]`` when absent/non-list."""
        if self.length <= self._LEGACY_CHUNKS_POS:
            return []
        value = self._raw[self._LEGACY_CHUNKS_POS]
        return value if isinstance(value, list) else []

    @staticmethod
    def deep_payload(payload: Any) -> tuple[str, str] | None:
        """Unpack a current-deep-research ``[title, report_markdown]`` payload.

        Returns ``(title, report_markdown)`` only when ``payload`` is a list of
        at least two strings (the exact shape the historical parser required at
        ``src[1]``); otherwise ``None`` so the caller falls through to the
        bare-string-title / fast-research branches.
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
