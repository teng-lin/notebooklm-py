"""Typed views over raw batchexecute response rows.

Google's NotebookLM batchexecute responses are positional ``list`` payloads
whose indices are pinned only by what we have captured in cassettes and
observed in production. When Google rotates a shape — a single index
shifts, a leaf becomes a wrapper, a list becomes a dict — every consumer
that hand-rolls position knowledge breaks independently.

This module is the **single point of position knowledge for the
``LIST_ARTIFACTS``, ``GET_NOTES_AND_MIND_MAPS``, and source-row
shapes**: if Google reshapes the wire, the position constants change
**here** and every consumer adapts automatically. The constants
therefore function as the canary contract for wire-shape changes — see
``tests/unit/test_row_adapters.py`` for the pin tests that fail loudly
when anyone edits a position.

The adapters sit **on top of** :func:`notebooklm.rpc.safe_index`:

* Top-level position presence (``len(self._raw) > _POS``) is treated as
  optional — missing trailing positions return sensible defaults in both
  soft and strict modes. This preserves the historical
  ``Artifact.from_api_response`` / ``Source.from_api_response`` contract
  that accepts short rows.
* Deep descent into a present position (``data[9][1][0]``,
  ``data[15][0]``, ``data[1][1]``, ``data[1][4]``, ``metadata[7][0]``,
  ``metadata[2][0]``) flows through :func:`safe_index`. Soft mode
  returns ``None`` on drift, strict mode raises
  :class:`notebooklm.exceptions.UnknownRPCMethodError` — the desired
  ADR-011 signal for genuine Google-side reshape.

Wire-shape variants:

* :class:`ArtifactRow` wraps a single artifact row from ``LIST_ARTIFACTS``.
* :class:`NoteRow` wraps a single note / mind-map row from
  ``GET_NOTES_AND_MIND_MAPS`` and absorbs the legacy-vs-current
  shape divergence (legacy: ``[id, content]``; current:
  ``[id, [id, content, metadata, None, title]]``) so consumers never
  open-code ``row[1][1]`` / ``row[1][4]``.
* :class:`SourceRow` wraps a single source row across three wire shapes
  (deeply nested, medium nested, flat) plus an "already extracted" entry
  layout. The :meth:`SourceRow.from_unknown_shape` classmethod is the
  multi-shape dispatcher; consumers (``Source.from_api_response``,
  ``SourceLister._parse_source``, ``NotebooksAPI.get_source_ids``) read
  named properties regardless of which shape arrived.

The §6.2 row-adapter rollout from ``docs/improvement.md`` is complete
with this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, ClassVar

from ._types.common import _datetime_from_timestamp
from .rpc import ArtifactStatus, RPCMethod, safe_index
from .rpc.types import SourceStatus

__all__ = ["ArtifactRow", "NoteRow", "SourceRow", "SourceRowShape"]


@dataclass(frozen=True)
class ArtifactRow:
    """Typed view of a raw artifact row from a ``LIST_ARTIFACTS`` response.

    The wrapped row is the per-artifact list returned by the ``gArtLc``
    (``LIST_ARTIFACTS``) RPC. Position layout:

    =====  ============================================================
    Index  Meaning
    =====  ============================================================
    0      artifact id (str)
    1      artifact title (str)
    2      type code (int — see :class:`notebooklm.rpc.ArtifactTypeCode`)
    4      processing status (int — see :class:`notebooklm.rpc.ArtifactStatus`)
    9      options block; ``[9][1][0]`` is the variant code (used to
           distinguish QUIZ from FLASHCARDS when type == 4)
    15     timestamp block; ``[15][0]`` is the creation timestamp
           (seconds since epoch)
    =====  ============================================================

    Position knowledge is centralised here. Consumer sites should NEVER
    open-code ``data[2]`` / ``data[4]`` / ``data[15]`` — wrap the row in
    an :class:`ArtifactRow` and read through the typed properties
    instead.

    The dataclass is frozen so accidentally mutating the wrapped row is
    impossible through the adapter; the adapter itself never copies the
    raw row, so it is cheap to construct.
    """

    # Wrapped row; ``repr=False`` so logs don't explode with the entire
    # batchexecute payload when an ArtifactRow appears in a stack trace.
    _raw: list[Any] = field(repr=False)
    # ``method_id`` is intentionally a public extension point: callers
    # wrapping a row that came from a non-LIST_ARTIFACTS method override
    # it so ``safe_index`` drift diagnostics point at the correct RPC.
    # No leading underscore — see the related test
    # ``TestMethodIdPropagation::test_custom_method_id_propagates``.
    method_id: str = RPCMethod.LIST_ARTIFACTS.value

    # ---- Position constants (the canary contract) ------------------------
    # These are ClassVar so the frozen dataclass treats them as class-level
    # constants rather than instance fields. If any of these change,
    # ``tests/unit/test_row_adapters.py::test_position_contract`` MUST be
    # updated in the same commit — that failure is the wire-shape change
    # signal.
    _ID_POS: ClassVar[int] = 0
    _TITLE_POS: ClassVar[int] = 1
    _TYPE_POS: ClassVar[int] = 2
    _STATUS_POS: ClassVar[int] = 4
    _OPTIONS_POS: ClassVar[int] = 9
    _TIMESTAMP_POS: ClassVar[int] = 15

    # ---- Top-level required positions ------------------------------------
    # These use length guards (not ``safe_index``) so short rows continue
    # to receive sensible defaults in BOTH soft and strict modes — that
    # matches the historical ``Artifact.from_api_response`` contract and
    # keeps minimal rows like ``["id", "title", 1, None, 3]`` working.

    @property
    def id(self) -> str:
        """Artifact identifier — empty string when absent."""
        if len(self._raw) <= self._ID_POS:
            return ""
        return str(self._raw[self._ID_POS])

    @property
    def title(self) -> str:
        """Artifact title — empty string when absent."""
        if len(self._raw) <= self._TITLE_POS:
            return ""
        return str(self._raw[self._TITLE_POS])

    @property
    def type_code(self) -> int:
        """Type code (see :class:`ArtifactTypeCode`); ``0`` when absent.

        Returned as the raw ``int``, not the enum, because consumers
        compare against either enum members or raw ints depending on
        context.
        """
        if len(self._raw) <= self._TYPE_POS:
            return 0
        value = self._raw[self._TYPE_POS]
        return value if isinstance(value, int) else 0

    @property
    def status(self) -> int:
        """Processing status code (see :class:`ArtifactStatus`); ``0`` when absent."""
        if len(self._raw) <= self._STATUS_POS:
            return 0
        value = self._raw[self._STATUS_POS]
        return value if isinstance(value, int) else 0

    # ---- Nested descents (delegated to safe_index) -----------------------
    # The outer ``len`` guard preserves the "optional trailing positions"
    # contract; the deeper descent goes through ``safe_index`` so strict
    # mode raises on genuine shape drift.

    @property
    def variant(self) -> int | None:
        """Variant code at ``data[9][1][0]`` — distinguishes QUIZ vs FLASHCARDS.

        Returns ``None`` when:

        * position 9 is absent (short row), or
        * descent through ``[1][0]`` returns ``None`` (soft-mode drift), or
        * the resulting value is not an ``int``.

        Raises :class:`UnknownRPCMethodError` in strict mode when position
        9 is present but its inner shape does not match — that is the
        signal that Google reshaped the options block.
        """
        if len(self._raw) <= self._OPTIONS_POS:
            return None
        options_block = self._raw[self._OPTIONS_POS]
        if not isinstance(options_block, list):
            # Preserves legacy soft-degrade for ``data[9] = None`` rows
            # (observed in older cassettes) without invoking ``safe_index``
            # against a non-list root.
            return None
        value = safe_index(
            options_block,
            1,
            0,
            method_id=self.method_id,
            source="ArtifactRow.variant",
        )
        return value if isinstance(value, int) else None

    @property
    def created_at_raw(self) -> int | float | None:
        """Raw creation timestamp (seconds since epoch) at ``data[15][0]``.

        Exposed separately from :attr:`created_at` because callers that
        sort artifact rows by recency need a value that compares cleanly
        even when the timestamp is missing or ``None``. The
        :meth:`~notebooklm._artifact_listing.ArtifactListingService.select_artifact`
        sort key uses ``row.created_at_raw or 0`` to coerce missing
        values to ``0`` without crashing the comparison.

        Returns ``None`` when:

        * position 15 is absent (short row), or
        * descent through ``[0]`` returns ``None`` (soft-mode drift), or
        * the resulting value is not numeric.
        """
        if len(self._raw) <= self._TIMESTAMP_POS:
            return None
        timestamp_block = self._raw[self._TIMESTAMP_POS]
        if not isinstance(timestamp_block, list) or not timestamp_block:
            # Mirrors the legacy
            # ``len(a) > 15 and isinstance(a[15], list) and a[15]``
            # guard. ``not timestamp_block`` short-circuits an empty
            # ``[]`` envelope so we never invoke ``safe_index`` against
            # it — an empty list at this position is an accepted
            # edge-case rather than drift (some cassettes legitimately
            # have ``data[15] = []``).
            return None
        value = safe_index(
            timestamp_block,
            0,
            method_id=self.method_id,
            source="ArtifactRow.created_at_raw",
        )
        return value if isinstance(value, (int, float)) else None

    @property
    def created_at(self) -> datetime | None:
        """Creation timestamp as a :class:`~datetime.datetime`, or ``None``.

        Wraps :attr:`created_at_raw` and converts via
        :func:`_datetime_from_timestamp`, which returns ``None`` for
        out-of-range / non-numeric values.
        """
        raw = self.created_at_raw
        if raw is None:
            return None
        return _datetime_from_timestamp(raw)

    # ---- Type-matching helper --------------------------------------------

    def matches_type(self, type_code: int, *, completed_only: bool = False) -> bool:
        """Return whether this row matches ``type_code``.

        Args:
            type_code: Raw :class:`ArtifactTypeCode` integer (or any int)
                to compare against the row's :attr:`type_code`.
            completed_only: When ``True``, also require :attr:`status`
                to equal :data:`ArtifactStatus.COMPLETED` (``3``). This
                is the predicate used by
                :meth:`~notebooklm._artifact_listing.ArtifactListingService.select_artifact`
                to pick downloadable artifacts.

        Note:
            This is a *raw* type-code match. The QUIZ vs FLASHCARDS
            variant distinction lives one layer up in
            ``_artifact_listing._matches_artifact_type`` because it
            operates on :class:`Artifact` objects (which know variant
            mapping), not raw rows. Keep that separation intentional —
            the adapter exposes the variant via :attr:`variant` if
            callers need it.
        """
        if self.type_code != type_code:
            return False
        if completed_only:
            return self.status == ArtifactStatus.COMPLETED
        return True


@dataclass(frozen=True)
class NoteRow:
    """Typed view of a raw note / mind-map row from ``GET_NOTES_AND_MIND_MAPS``.

    The wrapped row is the per-note list returned by the ``cFji9``
    (``GET_NOTES_AND_MIND_MAPS``) RPC. Two wire shapes coexist in the
    wild — the adapter absorbs both so consumers never branch on shape:

    * **Legacy** — ``[id, content_string]``: the JSON payload lives
      directly at position 1 as a string. Older cassettes and rows
      created before the metadata envelope rollout still arrive this
      way. There is no per-row title slot in the legacy shape; the
      adapter returns ``""`` for :attr:`title`.

    * **Current** — ``[id, [id, content_string, metadata, None, title]]``:
      the JSON payload lives at ``row[1][1]`` and the title at
      ``row[1][4]``. This is the production shape for any row created
      since the metadata envelope rollout.

    * **Deleted** — ``[id, None, 2]``: position 1 is ``None`` and
      position 2 is the soft-delete sentinel. :attr:`is_deleted` is
      ``True``; :attr:`content` and :attr:`title` both return ``None``
      / ``""`` respectively (callers should classify with
      :attr:`is_deleted` before reading other properties).

    Position knowledge is centralised here. Consumer sites should NEVER
    open-code ``row[1][1]`` / ``row[1][4]`` / ``row[1] is None`` — wrap
    the row in a :class:`NoteRow` and read through the typed properties
    instead. This is exactly the seam that lets a future Google reshape
    fix every consumer with one set of constant changes here.

    The dataclass is frozen so accidentally mutating the wrapped row is
    impossible through the adapter; the adapter itself never copies the
    raw row, so it is cheap to construct.
    """

    # Wrapped row; ``repr=False`` so logs don't explode with the entire
    # batchexecute payload when a NoteRow appears in a stack trace.
    _raw: list[Any] = field(repr=False)
    # ``method_id`` is intentionally a public extension point (matching
    # :class:`ArtifactRow`'s post-#1026 convention): callers wrapping a
    # row that came from a non-default method override it so
    # ``safe_index`` drift diagnostics point at the correct RPC. No
    # leading underscore — see the related test
    # ``TestNoteRowMethodIdField::test_custom_method_id_can_be_supplied``.
    method_id: str = RPCMethod.GET_NOTES_AND_MIND_MAPS.value

    # ---- Position constants (the canary contract) ------------------------
    # These are ClassVar so the frozen dataclass treats them as class-level
    # constants rather than instance fields. If any of these change,
    # ``tests/unit/test_row_adapters.py::TestNoteRowPositionContract``
    # MUST be updated in the same commit — that failure is the wire-shape
    # change signal.
    _ID_POS: ClassVar[int] = 0
    # Position 1 is overloaded: legacy puts the content string here
    # directly; current puts the metadata envelope (a list) here; deleted
    # rows put ``None`` here.
    _CONTENT_POS: ClassVar[int] = 1
    # Position 2 is the soft-delete sentinel slot — ``row[2] == 2`` plus
    # ``row[1] is None`` together signal a deleted row.
    _STATUS_POS: ClassVar[int] = 2
    # Inner envelope positions (only meaningful for the *current* shape
    # where ``row[1]`` is a list of length 5).
    _INNER_CONTENT_POS: ClassVar[int] = 1
    _INNER_TITLE_POS: ClassVar[int] = 4
    # Soft-delete sentinel value at ``_STATUS_POS``.
    _DELETED_SENTINEL: ClassVar[int] = 2

    # ---- Top-level position (the row id) ---------------------------------

    @property
    def id(self) -> str:
        """Row identifier — empty string when absent."""
        if len(self._raw) <= self._ID_POS:
            return ""
        return str(self._raw[self._ID_POS])

    # ---- Deletion detection ----------------------------------------------

    @property
    def is_deleted(self) -> bool:
        """Whether this row is the soft-delete sentinel ``[id, None, 2]``.

        Centralises the ``row[1] is None and row[2] == 2`` check so
        consumers (``NoteService.classify_row``, ``NotesAPI._is_deleted``)
        never re-derive it. Short rows (``len(raw) < 3``) are *not*
        deleted — soft deletion requires both the ``None`` content slot
        and the sentinel at position 2.
        """
        if len(self._raw) <= self._STATUS_POS:
            return False
        return (
            self._raw[self._CONTENT_POS] is None
            and self._raw[self._STATUS_POS] == self._DELETED_SENTINEL
        )

    # ---- Multi-shape content / title dispatch ----------------------------
    # Both descents short-circuit on the legacy ``str``-at-position-1
    # shape *before* invoking ``safe_index`` so the legitimate legacy
    # path emits no DeprecationWarning. The current shape's inner
    # descent flows through ``safe_index`` so strict mode raises on
    # genuine inner-shape drift.

    @property
    def content(self) -> str | None:
        """JSON content payload, dispatching across legacy / current shapes.

        Returns:
            * ``str`` — the JSON payload (from legacy ``row[1]`` or
              current ``row[1][1]``)
            * ``None`` — when the row is too short, deleted, the
              ``row[1]`` slot is an unrecognised type (e.g. an integer),
              or the current-shape inner envelope is too short to carry
              a content slot

        Both the outer length guard and the inner length guard preserve
        the historical "short rows soft-degrade to ``None``" contract —
        ``safe_index`` is invoked only when the inner envelope is long
        enough to legitimately carry the content slot, so genuine
        production short shapes never trip strict-mode drift detection.

        Note: ``safe_index`` is routed through for consistency with
        :class:`ArtifactRow` and to keep one telemetry seam for any
        future relaxation of the length guard. Given the current
        invariants (``isinstance(slot, list)`` + ``len(slot) > 1``),
        ``safe_index`` cannot actually raise here — strict-mode drift
        on this descent is unreachable. Documented via
        ``TestNoteRowShortInnerIsNotDrift`` in the test suite.
        """
        if len(self._raw) <= self._CONTENT_POS:
            return None
        slot = self._raw[self._CONTENT_POS]
        # Legacy shape: ``row[1]`` is the content string itself.
        if isinstance(slot, str):
            return slot
        # Current shape: ``row[1]`` is the metadata envelope list. Some
        # cassettes legitimately have a length-1 or empty inner envelope
        # (older nested rows without the content slot populated) — those
        # are NOT drift, so length-guard before invoking ``safe_index``.
        if isinstance(slot, list):
            if len(slot) <= self._INNER_CONTENT_POS:
                return None
            value = safe_index(
                slot,
                self._INNER_CONTENT_POS,
                method_id=self.method_id,
                source="NoteRow.content",
            )
            return value if isinstance(value, str) else None
        # ``None`` (deleted) or any other type — no extractable content.
        return None

    @property
    def title(self) -> str:
        """Note title, available only on the current shape.

        Returns ``""`` when:

        * the row is in legacy shape (``row[1]`` is a string — there is
          no per-row title slot in that shape), or
        * the row is too short to carry ``row[1]``, or
        * ``row[1]`` is ``None`` (deleted) or not a list, or
        * the inner envelope is too short to carry the title slot
          (length 5 is the canonical current shape; shorter inners
          predate the title rollout and are not drift), or
        * the inner descent through ``[4]`` returns a non-string.

        See the note on :attr:`content` re: ``safe_index`` invariants —
        the same reasoning applies here. The inner length guard makes
        the descent through ``[4]`` unreachable as a drift signal under
        current invariants; ``safe_index`` stays for consistency with
        :class:`ArtifactRow` and as a telemetry seam.
        """
        if len(self._raw) <= self._CONTENT_POS:
            return ""
        slot = self._raw[self._CONTENT_POS]
        if not isinstance(slot, list):
            return ""
        # Length-guard short inners — some legitimate cassette rows have
        # ``[id, content]`` shapes (no title slot) that are not drift.
        if len(slot) <= self._INNER_TITLE_POS:
            return ""
        value = safe_index(
            slot,
            self._INNER_TITLE_POS,
            method_id=self.method_id,
            source="NoteRow.title",
        )
        return value if isinstance(value, str) else ""

    # ---- Mind-map content classification ---------------------------------

    @property
    def is_mind_map(self) -> bool:
        """Whether :attr:`content` looks like a serialised mind-map.

        Convenience wrapper around :meth:`is_mind_map_content` that
        applies the same predicate to ``self.content``. Returns ``False``
        when :attr:`content` is ``None``.
        """
        return self.is_mind_map_content(self.content)

    @staticmethod
    def is_mind_map_content(content: str | None) -> bool:
        """Return whether ``content`` is a serialised mind-map payload.

        Mind maps are JSON object blobs that always contain either a
        ``"children":`` or ``"nodes":`` key at the top level. We match
        on the substring rather than parsing the JSON because (a) the
        payloads can be large and we run this check on every row in a
        notebook list, and (b) the substring discriminator has been
        stable across every cassette captured to date — it's the same
        predicate the wire decoder uses.

        The ``startswith("{")`` guard avoids false positives on plain
        text notes that happen to contain the substring ``"children":``
        verbatim (e.g. a note body like ``My "children": Alice, Bob``).
        Production mind-map payloads are always JSON objects, never
        arrays / strings / etc., so requiring the leading ``{`` is a
        zero-cost reduction in false-positive surface — gemini review
        feedback on #1028.

        Exposed as a ``@staticmethod`` so callers that already have a
        content string in hand (e.g. ``NoteService.classify_row``
        threading through the cached ``content`` value) can classify
        without constructing a fresh :class:`NoteRow`.
        """
        if not content or not content.startswith("{"):
            return False
        return '"children":' in content or '"nodes":' in content


# ---------------------------------------------------------------------------
# SourceRow
# ---------------------------------------------------------------------------


class SourceRowShape(str, Enum):
    """The wire shape that a :class:`SourceRow` was extracted from.

    Source rows arrive over three distinct shapes; the shape is tracked
    on the row only for diagnostics (so drift logs can name the path
    that was taken). All three normalize to the same :class:`SourceRow`
    interface — consumer sites read named properties regardless of
    shape.

    See :meth:`SourceRow.from_unknown_shape` for the dispatcher and
    :class:`SourceRow` for the position contract on the **normalized
    entry** form that the adapter wraps internally.
    """

    #: ``[[[[id], title, metadata, ...]]]`` — deeply-nested response,
    #: e.g. some ``ADD_SOURCE`` shapes where the entry is wrapped in an
    #: extra outer list.
    DEEPLY_NESTED = "deeply_nested"

    #: ``[[[id], title, metadata, ...]]`` — medium-nested, the most
    #: common shape used by ``GET_NOTEBOOK`` and ``ADD_SOURCE``.
    MEDIUM_NESTED = "medium_nested"

    #: ``[id, title, ...]`` — flat shape. Used by some callers that pre-
    #: extracted the entry envelope.
    FLAT = "flat"

    #: A pre-extracted ``[[id], title, metadata, ...]`` entry — what
    #: :meth:`SourceRow.from_entry` wraps directly without dispatching.
    #: Identical layout to ``MEDIUM_NESTED`` after one unwrap; tracked
    #: separately so drift logs can distinguish "dispatcher produced
    #: this" from "caller handed us an already-unwrapped entry".
    ENTRY = "entry"


@dataclass(frozen=True)
class SourceRow:
    """Typed view of a single source row.

    Source rows arrive over three wire shapes (see
    :class:`SourceRowShape`); the :meth:`from_unknown_shape` classmethod
    dispatches the three into a single **normalized entry** layout that
    this adapter wraps:

    =====  ============================================================
    Index  Meaning
    =====  ============================================================
    0      source-id envelope. Variants:

           * ``"id"`` — bare string (legacy / flat shape).
           * ``["id"]`` — typical wrapping.
           * ``[None, True, ["id"]]`` — drive-backed entries nest the
             id one level deeper at ``raw_id[2][0]``. Surfaced by
             :attr:`id` transparently.
    1      title (str) — may be ``None`` / missing on short rows.
    2      metadata sub-list (see below).
    3      status block; ``[3][1]`` is the
           :class:`~notebooklm.rpc.SourceStatus` code (used by
           ``GET_NOTEBOOK`` source-list rows).
    =====  ============================================================

    **Metadata sub-list layout** (``self._raw[2]``):

    =====  ============================================================
    Index  Meaning
    =====  ============================================================
    0      Mixed — sometimes a bare ``http(s)://...`` URL (legacy
           shape, only honored when ``url_allow_bare_http=True``).
    2      timestamp block; ``[2][0]`` is the creation timestamp
           (seconds since epoch).
    4      type code (int — see
           :class:`notebooklm._types.sources.SourceType` mapping in
           ``_types/sources.py``).
    5      youtube/source-specific block; ``[5][0]`` is a YouTube URL.
    7      url block; ``[7][0]`` is the canonical source URL when
           present (takes precedence over ``metadata[5][0]`` and
           ``metadata[0]``).
    =====  ============================================================

    Position knowledge is centralised here. Consumer sites should NEVER
    open-code ``data[0][0]`` / ``data[0][0][0]`` / ``metadata[4]`` —
    wrap the row in a :class:`SourceRow` and read through the typed
    properties instead.

    The dataclass is frozen so accidentally mutating the wrapped row is
    impossible through the adapter; the adapter itself never copies the
    raw row, so it is cheap to construct.
    """

    # Wrapped normalized entry; ``repr=False`` so logs don't explode
    # with the entire batchexecute payload.
    _raw: list[Any] = field(repr=False)
    # ``method_id`` is a public extension point: callers wrapping a row
    # that came from a non-default RPC override it so ``safe_index``
    # drift diagnostics point at the correct method.
    method_id: str = RPCMethod.GET_NOTEBOOK.value
    # Records which dispatcher branch produced this row. Default is
    # ``ENTRY`` because direct construction (``SourceRow(entry)``)
    # bypasses dispatch.
    shape: SourceRowShape = SourceRowShape.ENTRY
    # The deeply-nested ``ADD_SOURCE``-style path historically allowed
    # a bare ``http(s)://...`` value at ``metadata[0]`` to act as the
    # URL when no ``metadata[7]``/``metadata[5]`` entry was present.
    # Medium-nested and entry-shaped rows (``GET_NOTEBOOK`` source list
    # + most ``ADD_SOURCE`` shapes) pack unrelated content into
    # ``metadata[0]`` and must NOT honor it as a URL.
    url_allow_bare_http: bool = False

    # ---- Position constants (the canary contract) ------------------------
    # ClassVar so the frozen dataclass treats them as class-level
    # constants. If any of these change,
    # ``tests/unit/test_row_adapters.py::TestSourceRowPositionContract``
    # MUST be updated in the same commit — that failure is the wire-shape
    # change signal.

    # Top-level (entry) positions.
    _ID_POS: ClassVar[int] = 0
    _TITLE_POS: ClassVar[int] = 1
    _METADATA_POS: ClassVar[int] = 2
    _STATUS_BLOCK_POS: ClassVar[int] = 3
    _STATUS_INNER_POS: ClassVar[int] = 1

    # Metadata sub-list positions.
    _META_BARE_URL_POS: ClassVar[int] = 0
    _META_TIMESTAMP_POS: ClassVar[int] = 2
    _META_TYPE_POS: ClassVar[int] = 4
    _META_YOUTUBE_POS: ClassVar[int] = 5
    _META_URL_POS: ClassVar[int] = 7

    # Id-envelope inner positions (the three layouts at ``self._raw[0]``).
    _ID_ENVELOPE_PLAIN_POS: ClassVar[int] = 0
    _ID_ENVELOPE_DRIVE_PAYLOAD_POS: ClassVar[int] = 2
    _ID_ENVELOPE_DRIVE_INNER_POS: ClassVar[int] = 0

    # Neutral "first element of a single-item list" index, used by url
    # helpers that pull the leading element from ``metadata[7]``,
    # ``metadata[5]``, etc. Kept separate from ``_ID_ENVELOPE_PLAIN_POS``
    # (also ``0``) so a future id-envelope reshape doesn't accidentally
    # break URL extraction — claude review feedback on #1029.
    _LIST_FIRST_POS: ClassVar[int] = 0

    # ---- Dispatchers -----------------------------------------------------

    @classmethod
    def from_unknown_shape(
        cls,
        data: list[Any],
        *,
        method_id: str | None = None,
    ) -> SourceRow:
        """Normalize any of the three source wire shapes into a
        :class:`SourceRow`.

        Shapes handled (matching the legacy ``Source.from_api_response``
        branches):

        1. **Deeply nested** — ``[[[[id], title, metadata, ...]]]``.
           Unwraps ``data[0][0]`` to reach the entry. Honors the legacy
           ``url_allow_bare_http=True`` policy (only this shape lets a
           bare ``http(s)://...`` at ``metadata[0]`` act as the URL).
        2. **Medium nested** — ``[[[id], title, metadata, ...]]``.
           Unwraps ``data[0]`` to reach the entry.
        3. **Flat** — ``[id, title, ...]``. Wraps directly; metadata is
           absent so :attr:`url`, :attr:`type_code`, :attr:`created_at`
           all return ``None`` / ``0``.

        Args:
            data: Raw decoded payload. Must be a non-empty list.
            method_id: Override for diagnostics; defaults to the class
                default (``GET_NOTEBOOK``) when ``None``.

        Returns:
            A :class:`SourceRow` wrapping the normalized entry.

        Raises:
            ValueError: When ``data`` is empty or not a list.
        """
        if not data or not isinstance(data, list):
            raise ValueError(f"Invalid source data: {data!r}")

        mid = method_id if method_id is not None else RPCMethod.GET_NOTEBOOK.value

        outer = data[cls._ID_POS]
        # The medium/deep dispatch mirrors the legacy
        # ``Source.from_api_response`` two-level guard:
        #   data[0] is a non-empty list, AND data[0][0] is a non-empty list.
        # If data[0][0][0] is *itself* a list, we have an extra wrapper
        # (deeply-nested): the entry lives at data[0][0] and its id
        # envelope at data[0][0][0]. Otherwise the entry lives at
        # data[0] and its id envelope at data[0][0].
        if (
            isinstance(outer, list)
            and outer
            and isinstance(outer[cls._ID_POS], list)
            and outer[cls._ID_POS]
        ):
            inner = outer[cls._ID_POS]
            if isinstance(inner[cls._ID_ENVELOPE_PLAIN_POS], list):
                # Deeply nested: data[0][0] IS the entry; its [0] is
                # itself a list (the id envelope), so we have an extra
                # outer wrapper around the entry.
                return cls(
                    _raw=inner,
                    method_id=mid,
                    shape=SourceRowShape.DEEPLY_NESTED,
                    url_allow_bare_http=True,
                )
            # Medium nested: data[0] IS the entry; data[0][0] is its
            # id envelope.
            return cls(
                _raw=outer,
                method_id=mid,
                shape=SourceRowShape.MEDIUM_NESTED,
                url_allow_bare_http=False,
            )

        # Flat: [id, title, ...]
        return cls(
            _raw=data,
            method_id=mid,
            shape=SourceRowShape.FLAT,
            url_allow_bare_http=False,
        )

    @classmethod
    def from_entry(
        cls,
        entry: list[Any],
        *,
        method_id: str | None = None,
    ) -> SourceRow:
        """Wrap an already-extracted entry (``[[id], title, metadata, ...]``).

        Used by callers that walked the response envelope themselves —
        e.g. :class:`notebooklm._source_listing.SourceLister` iterating
        over ``notebook[0][1]`` and
        :meth:`notebooklm._notebooks.NotebooksAPI.get_source_ids`
        iterating over the same envelope. Shape is recorded as
        :attr:`SourceRowShape.ENTRY`.
        """
        mid = method_id if method_id is not None else RPCMethod.GET_NOTEBOOK.value
        return cls(
            _raw=entry,
            method_id=mid,
            shape=SourceRowShape.ENTRY,
            url_allow_bare_http=False,
        )

    # ---- Top-level required positions ------------------------------------
    # Length guards (not ``safe_index``) so short rows continue to
    # receive sensible defaults in BOTH soft and strict modes.

    @property
    def id(self) -> str:
        """Source identifier — empty string when the envelope is malformed.

        Handles three id-envelope variants transparently:

        * Bare string at ``self._raw[0]`` (flat shape).
        * ``["id"]`` at ``self._raw[0]`` (typical).
        * ``[None, True, ["id"]]`` at ``self._raw[0]`` (drive-backed).
        """
        raw_id = self._id_envelope()
        if raw_id is None:
            return ""
        if not isinstance(raw_id, list):
            # Flat shape: id is the entry element directly.
            return str(raw_id)
        # ``[id, ...]`` — typical wrapping.
        if raw_id and raw_id[self._ID_ENVELOPE_PLAIN_POS] is not None:
            return str(raw_id[self._ID_ENVELOPE_PLAIN_POS])
        # ``[None, True, [id]]`` — drive-backed nesting.
        if (
            len(raw_id) > self._ID_ENVELOPE_DRIVE_PAYLOAD_POS
            and isinstance(raw_id[self._ID_ENVELOPE_DRIVE_PAYLOAD_POS], list)
            and raw_id[self._ID_ENVELOPE_DRIVE_PAYLOAD_POS]
        ):
            inner = raw_id[self._ID_ENVELOPE_DRIVE_PAYLOAD_POS][self._ID_ENVELOPE_DRIVE_INNER_POS]
            return str(inner) if inner is not None else ""
        return ""

    def _id_envelope(self) -> Any:
        """Return the raw id envelope (``self._raw[0]``) or ``None``."""
        if len(self._raw) <= self._ID_POS:
            return None
        return self._raw[self._ID_POS]

    @property
    def has_id(self) -> bool:
        """Whether the row resolves to a non-empty :attr:`id`.

        Used by :class:`notebooklm._source_listing.SourceLister` to skip
        rows whose id envelopes legacy ``_extract_source_id`` would
        have rejected (returning ``None``) — including the rare
        ``[None, True, [None]]`` drive-payload-with-``None``-inner case
        that :attr:`id` decodes to ``""``.

        Equivalent to ``bool(self.id)``; exposed as a named predicate
        so consumer call sites read intent-first.
        """
        return bool(self.id)

    @property
    def title(self) -> str | None:
        """Source title — ``None`` when absent (preserves legacy contract).

        Unlike :attr:`ArtifactRow.title`, this returns ``None`` rather
        than an empty string because the legacy
        ``Source.from_api_response`` carried ``title: str | None`` and
        downstream consumers (CLI table renderers, etc.) branch on the
        ``None`` case.

        Non-``None`` non-string values are coerced via ``str()`` so the
        ``str | None`` annotation is honored at runtime — aligns with
        :attr:`ArtifactRow.title`'s coercion (claude review feedback on
        #1029). ``None`` is preserved as-is so the legacy "missing
        title" sentinel still distinguishes from "title is empty string".
        """
        if len(self._raw) <= self._TITLE_POS:
            return None
        value = self._raw[self._TITLE_POS]
        if value is None:
            return None
        return value if isinstance(value, str) else str(value)

    @property
    def metadata(self) -> list[Any] | None:
        """The metadata sub-list at ``self._raw[2]``, or ``None``.

        Returned as ``None`` (not ``[]``) when absent or non-list, so
        callers can distinguish "no metadata block" from "metadata
        block exists but is empty".
        """
        if len(self._raw) <= self._METADATA_POS:
            return None
        value = self._raw[self._METADATA_POS]
        return value if isinstance(value, list) else None

    @property
    def type_code(self) -> int | None:
        """Type code at ``metadata[4]`` (int) or ``None`` when absent.

        Returned as raw ``int``; callers map via
        :func:`notebooklm._types.sources._safe_source_type` to get the
        :class:`~notebooklm._types.sources.SourceType` enum.
        """
        metadata = self.metadata
        if metadata is None or len(metadata) <= self._META_TYPE_POS:
            return None
        value = metadata[self._META_TYPE_POS]
        return value if isinstance(value, int) else None

    @property
    def url(self) -> str | None:
        """Canonical source URL — ``None`` when absent.

        Precedence (matches the legacy ``_extract_source_url`` logic):

        1. :meth:`_url_from_canonical_block` — ``metadata[7][0]`` (typical
           canonical URL slot, present on every modern source).
        2. :meth:`_url_from_youtube_block` — ``metadata[5][0]`` (YouTube-
           style block, only when its first element is a string).
        3. :meth:`_url_from_bare_metadata_zero` — ``metadata[0]`` —
           only honored when :attr:`url_allow_bare_http` is ``True`` AND
           the value starts with ``http``. This restricted fallback
           exists for the deeply-nested ``ADD_SOURCE`` shape.

        Each precedence level is a tiny named helper so the dispatch
        reads at the same level of abstraction (gemini review feedback
        on #1029): the property body is the precedence order, and each
        helper owns one slot's positional knowledge.
        """
        metadata = self.metadata
        if metadata is None:
            return None
        return (
            self._url_from_canonical_block(metadata)
            or self._url_from_youtube_block(metadata)
            or self._url_from_bare_metadata_zero(metadata)
        )

    def _url_from_canonical_block(self, metadata: list[Any]) -> str | None:
        """Extract the URL from ``metadata[7][0]`` (canonical slot).

        Returns ``None`` when position 7 is absent, non-list, empty, or
        when its first element is falsy. Non-string truthy values are
        stringified to honor the legacy
        ``_extract_source_url`` contract where ``url`` is whatever the
        wire stored at this position.
        """
        if len(metadata) <= self._META_URL_POS:
            return None
        url_list = metadata[self._META_URL_POS]
        if not isinstance(url_list, list) or not url_list:
            return None
        first = url_list[self._LIST_FIRST_POS]
        if not first:
            return None
        return first if isinstance(first, str) else str(first)

    def _url_from_youtube_block(self, metadata: list[Any]) -> str | None:
        """Extract the URL from ``metadata[5][0]`` (YouTube-style block).

        Returns ``None`` unless position 5 is a non-empty list whose
        first element is a string. The string requirement preserves
        legacy behavior where non-string YouTube-block elements (e.g.
        the video id at ``[5][1]`` or channel name at ``[5][2]``) are
        not interpreted as URLs.
        """
        if len(metadata) <= self._META_YOUTUBE_POS:
            return None
        yt_block = metadata[self._META_YOUTUBE_POS]
        if (
            isinstance(yt_block, list)
            and yt_block
            and isinstance(yt_block[self._LIST_FIRST_POS], str)
        ):
            return yt_block[self._LIST_FIRST_POS]
        return None

    def _url_from_bare_metadata_zero(self, metadata: list[Any]) -> str | None:
        """Extract the URL from ``metadata[0]`` — restricted fallback.

        Returns ``None`` unless ALL of:

        * :attr:`url_allow_bare_http` is ``True`` (only the deeply-
          nested ``ADD_SOURCE`` shape sets this), AND
        * position 0 exists, is a string, and starts with ``http``.

        The ``http`` prefix guard avoids treating arbitrary
        ``metadata[0]`` strings (e.g. drive ids, mime types) as URLs
        on shapes where this slot packs unrelated content.
        """
        if not self.url_allow_bare_http or len(metadata) <= self._META_BARE_URL_POS:
            return None
        candidate = metadata[self._META_BARE_URL_POS]
        if isinstance(candidate, str) and candidate.startswith("http"):
            return candidate
        return None

    @property
    def created_at_raw(self) -> int | float | None:
        """Raw creation timestamp (seconds since epoch) at ``metadata[2][0]``.

        Returns ``None`` when:

        * metadata is absent / non-list, or
        * ``metadata[2]`` is absent / non-list / empty, or
        * the resulting value is not numeric.

        An empty ``metadata[2] = []`` envelope is treated as a soft
        edge-case (not strict-mode drift), mirroring
        :attr:`ArtifactRow.created_at_raw`.
        """
        metadata = self.metadata
        if metadata is None or len(metadata) <= self._META_TIMESTAMP_POS:
            return None
        timestamp_block = metadata[self._META_TIMESTAMP_POS]
        if not isinstance(timestamp_block, list) or not timestamp_block:
            return None
        value = safe_index(
            timestamp_block,
            0,
            method_id=self.method_id,
            source="SourceRow.created_at_raw",
        )
        return value if isinstance(value, (int, float)) else None

    @property
    def created_at(self) -> datetime | None:
        """Creation timestamp as a :class:`~datetime.datetime`, or ``None``."""
        raw = self.created_at_raw
        if raw is None:
            return None
        return _datetime_from_timestamp(raw)

    @property
    def status(self) -> SourceStatus:
        """Processing status from ``self._raw[3][1]``.

        Used by ``GET_NOTEBOOK`` source-list rows where every entry
        carries a status block. Defaults to
        :data:`SourceStatus.READY` when:

        * position 3 is absent / non-list / too short, or
        * the status code is not one of the known enum values.

        This mirrors the legacy ``SourceLister._extract_status``
        contract — same fallback to :data:`SourceStatus.READY` on any
        unrecognised code. The membership check uses ``SourceStatus(...)``
        directly (catching :class:`ValueError`) rather than an explicit
        member tuple so the adapter automatically accepts any new values
        added to :class:`SourceStatus` without a parallel update here
        (claude review feedback on #1029).
        """
        if (
            len(self._raw) <= self._STATUS_BLOCK_POS
            or not isinstance(self._raw[self._STATUS_BLOCK_POS], list)
            or len(self._raw[self._STATUS_BLOCK_POS]) <= self._STATUS_INNER_POS
        ):
            return SourceStatus.READY

        status_code = self._raw[self._STATUS_BLOCK_POS][self._STATUS_INNER_POS]
        try:
            return SourceStatus(status_code)
        except ValueError:
            return SourceStatus.READY
