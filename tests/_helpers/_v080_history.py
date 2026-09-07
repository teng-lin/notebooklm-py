"""Shared historical constants and types for v0.8.0 release gate qualification.

Extracted per Phase 2 of test optimization to eliminate cross-test imports
between test_v080_deprecation_coverage.py and test_v080_release_gate.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src" / "notebooklm"


@dataclass(frozen=True)
class Runway:
    """A v0.7.0 deprecation signal whose presence the gate can verify."""

    module: str
    description: str
    symbol: str | None = None
    notice: str | None = None

    def __post_init__(self) -> None:
        provided = [v for v in (self.symbol, self.notice) if v is not None]
        if len(provided) != 1:
            raise ValueError(
                "Runway must set exactly one of {symbol, notice}; "
                f"got symbol={self.symbol!r}, notice={self.notice!r}"
            )

    @property
    def needle(self) -> str:
        return self.symbol if self.symbol is not None else self.notice  # type: ignore[return-value]

    @property
    def is_symbol(self) -> bool:
        return self.symbol is not None


@dataclass(frozen=True)
class BreakingChange:
    """One tracked v0.8.0-breaking change, runwayed XOR reason-exempted."""

    issue: int
    summary: str
    runway: Runway | None = None
    exemption: str | None = None


V080_BREAKING_CHANGES: tuple[BreakingChange, ...] = ()
