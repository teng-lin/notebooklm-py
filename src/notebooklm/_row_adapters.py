"""Typed views over raw batchexecute response rows.

Google's NotebookLM batchexecute responses are positional ``list`` payloads
whose indices are pinned only by what we have captured in cassettes and
observed in production. When Google rotates a shape — a single index
shifts, a leaf becomes a wrapper, a list becomes a dict — every consumer
that hand-rolls position knowledge breaks independently.

This module is the **single point of position knowledge for the artifact
row shape**: if Google reshapes the wire, the position constants change
**here** and every consumer (currently :class:`notebooklm._types.artifacts.Artifact`
and :class:`notebooklm._artifact_listing.ArtifactListingService`) adapts
automatically. The constants therefore function as the canary contract for
artifact wire-shape changes — see ``tests/unit/test_row_adapters.py`` for
the pin test that fails loudly when anyone edits a position.

The adapter sits **on top of** :func:`notebooklm.rpc.safe_index`:

* Top-level position presence (``len(self._raw) > _POS``) is treated as
  optional — missing trailing positions return sensible defaults in both
  soft and strict modes. This preserves the historical
  ``Artifact.from_api_response`` contract that accepts short rows.
* Deep descent into a present position (``data[9][1][0]``,
  ``data[15][0]``) flows through :func:`safe_index`. Soft mode returns
  ``None`` on drift, strict mode raises
  :class:`notebooklm.exceptions.UnknownRPCMethodError` — the desired
  ADR-011 signal for genuine Google-side reshape.

Out of scope for this module (deferred to follow-up PRs per
``docs/improvement.md`` §6.2): ``SourceRowAdapter`` and ``NoteRowAdapter``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar

from ._types.common import _datetime_from_timestamp
from .rpc import ArtifactStatus, RPCMethod, safe_index

__all__ = ["ArtifactRow"]


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
