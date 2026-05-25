"""Typed views over raw batchexecute response rows.

Google's NotebookLM batchexecute responses are positional ``list`` payloads
whose indices are pinned only by what we have captured in cassettes and
observed in production. When Google rotates a shape — a single index
shifts, a leaf becomes a wrapper, a list becomes a dict — every consumer
that hand-rolls position knowledge breaks independently.

This module is the **single point of position knowledge for the
``LIST_ARTIFACTS`` and ``GET_NOTES_AND_MIND_MAPS`` row shapes**: if
Google reshapes the wire, the position constants change **here** and
every consumer adapts automatically. The constants therefore function
as the canary contract for wire-shape changes — see
``tests/unit/test_row_adapters.py`` for the pin tests that fail loudly
when anyone edits a position.

The adapters sit **on top of** :func:`notebooklm.rpc.safe_index`:

* Top-level position presence (``len(self._raw) > _POS``) is treated as
  optional — missing trailing positions return sensible defaults in both
  soft and strict modes. This preserves the historical
  ``Artifact.from_api_response`` contract that accepts short rows.
* Deep descent into a present position (``data[9][1][0]``,
  ``data[15][0]``, ``data[1][1]``, ``data[1][4]``) flows through
  :func:`safe_index`. Soft mode returns ``None`` on drift, strict mode
  raises :class:`notebooklm.exceptions.UnknownRPCMethodError` — the
  desired ADR-011 signal for genuine Google-side reshape.

Wire-shape variants:

* :class:`ArtifactRow` wraps a single artifact row from ``LIST_ARTIFACTS``.
* :class:`NoteRow` wraps a single note / mind-map row from
  ``GET_NOTES_AND_MIND_MAPS`` and absorbs the legacy-vs-current
  shape divergence (legacy: ``[id, content]``; current:
  ``[id, [id, content, metadata, None, title]]``) so consumers never
  open-code ``row[1][1]`` / ``row[1][4]``.

Out of scope for this module (deferred to a follow-up PR per
``docs/improvement.md`` §6.2): ``SourceRowAdapter``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar

from ._types.common import _datetime_from_timestamp
from .rpc import ArtifactStatus, RPCMethod, safe_index

__all__ = ["ArtifactRow", "NoteRow"]


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
