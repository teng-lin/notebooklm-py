"""Note row adapter for raw ``GET_NOTES_AND_MIND_MAPS`` response rows."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar

from ..rpc import RPCMethod, safe_index

__all__ = ["NoteRow"]


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

    @property
    def has_unrecognized_tombstone(self) -> bool:
        """Whether the content slot is ``None`` without the soft-delete sentinel.

        A live note / mind-map row always carries content at position 1; the
        only legitimate ``None``-content shape on the wire is the soft-delete
        tombstone ``[id, None, 2]`` (see :attr:`is_deleted`). A ``None``
        content slot that is NOT the recognised tombstone — a short
        ``[id, None]`` row, or a drifted sentinel value at position 2 —
        therefore signals tombstone drift: were Google to move the sentinel,
        every deleted row would silently leak as live. Consumers
        (``Artifact.from_mind_map``) WARN on this predicate before falling
        through to their historical treat-as-live behavior, rather than
        leaking silently (#1485 absence-vs-malformed policy).

        ``False`` for rows too short to carry the content slot (no content
        slot is genuine absence, not a null content value).
        """
        if len(self._raw) <= self._CONTENT_POS:
            return False
        return self._raw[self._CONTENT_POS] is None and not self.is_deleted

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
        zero-cost reduction in false-positive surface.

        Exposed as a ``@staticmethod`` so callers that already have a
        content string in hand (e.g. ``NoteService.classify_row``
        threading through the cached ``content`` value) can classify
        without constructing a fresh :class:`NoteRow`.
        """
        if not content or not content.startswith("{"):
            return False
        return '"children":' in content or '"nodes":' in content
