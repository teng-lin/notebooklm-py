"""Transport-neutral positional result for source URL batch creation."""

from __future__ import annotations

from dataclasses import dataclass

from ..exceptions import SourceAddError
from ..types import Source


@dataclass(frozen=True)
class SourceUrlBatchItem:
    """One positional outcome from a backend-owned URL-add workflow."""

    url: str
    source: Source | None = None
    error: SourceAddError | None = None

    def __post_init__(self) -> None:
        if (self.source is None) == (self.error is None):
            raise ValueError("exactly one of source or error must be set")


__all__ = ["SourceUrlBatchItem"]
