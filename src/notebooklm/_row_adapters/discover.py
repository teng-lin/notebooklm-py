"""Row adapters for the ``DISCOVER_SOURCES`` (``Es3dTe``) payload.

The synchronous ``DiscoverSources`` RPC returns one envelope::

    [
        [ [url, title, why_relevant, type], ... ],   # [0] candidate rows
        "<one-line summary of the selection>",        # [1] summary (optional)
        ["<run-id-uuid>"],                            # [2] run id (optional)
    ]

These adapters centralise that positional knowledge so a future Google reshape
is a one-place fix and so genuine drift on the *guaranteed* descents RAISES via
``safe_index`` instead of silently degrading to empty data (ADR-0011).

* The candidate-list slot (``result[0]``) is **guaranteed** — an empty
  discovery legitimately returns ``[]`` there, but a *missing* slot is drift,
  so it descends through ``safe_index``.
* Every per-row field (url / title / why_relevant / type) is
  **routinely-optional**: it is length-guarded inside the row adapter and
  short-circuits to a default, so an over-trimmed row degrades gracefully
  rather than raising.

Position contracts are pinned by ``tests/unit/test_discover_row_adapter.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..rpc import RPCMethod, safe_index

__all__ = [
    "DiscoverResultRow",
    "DiscoveredSourceRow",
]

_METHOD_ID = RPCMethod.DISCOVER_SOURCES.value
_SOURCE = "_discover.discover"

# Envelope positions.
_CANDIDATES_POS = 0

# Per-row positions.
_URL_POS = 0
_TITLE_POS = 1
_WHY_POS = 2
_TYPE_POS = 3

_DEFAULT_SOURCE_TYPE = 1


@dataclass(frozen=True)
class DiscoverResultRow:
    """Typed view over the top-level ``DISCOVER_SOURCES`` envelope."""

    raw: Any

    @property
    def candidate_rows(self) -> list[Any]:
        """Return the list of raw candidate rows (``result[0]``).

        Descends through ``safe_index`` (the slot is guaranteed); a non-list
        value at that slot is treated as "no candidates" so a thin/empty
        response yields ``[]`` rather than raising.
        """
        candidates = safe_index(
            self.raw,
            _CANDIDATES_POS,
            method_id=_METHOD_ID,
            source=_SOURCE,
        )
        if not isinstance(candidates, list):
            return []
        return candidates


@dataclass(frozen=True)
class DiscoveredSourceRow:
    """Typed view over one candidate row ``[url, title, why_relevant, type]``."""

    raw: Any

    @property
    def is_well_formed(self) -> bool:
        """A row is usable when it is a list carrying a non-empty URL string."""
        return isinstance(self.raw, list) and isinstance(self.url, str) and bool(self.url)

    def _slot(self, pos: int) -> Any:
        if isinstance(self.raw, list) and len(self.raw) > pos:
            return self.raw[pos]
        return None

    @property
    def url(self) -> str:
        value = self._slot(_URL_POS)
        return value if isinstance(value, str) else ""

    @property
    def title(self) -> str:
        value = self._slot(_TITLE_POS)
        return value if isinstance(value, str) else ""

    @property
    def why_relevant(self) -> str:
        value = self._slot(_WHY_POS)
        return value if isinstance(value, str) else ""

    @property
    def source_type(self) -> int:
        value = self._slot(_TYPE_POS)
        return (
            value
            if isinstance(value, int) and not isinstance(value, bool)
            else _DEFAULT_SOURCE_TYPE
        )
