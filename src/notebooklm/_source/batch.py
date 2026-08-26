"""Positional outcome container for the true-batch URL source facade.

The batch *workflow* — one non-replayed ``ADD_SOURCE`` write plus the positional
attribution and ERROR-row reconciliation that turn its response into per-item
outcomes — is service-owned since P10 R3.5 and lives in
``SourceService.add_urls_batch``.  What remains here is the public-model shape
:meth:`SourcesAPI._add_urls_batch` hands the already batch-shaped MCP and REST
endpoints, projected from the neutral ``SourceUrlBatchItemRecord`` at the
facade.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..exceptions import SourceAddError
from ..types import Source


@dataclass(frozen=True)
class SourceUrlBatchItem:
    """One positional outcome from a true-batch URL add."""

    url: str
    source: Source | None = None
    error: SourceAddError | None = None

    def __post_init__(self) -> None:
        if (self.source is None) == (self.error is None):
            raise ValueError("exactly one of source or error must be set")


__all__ = ["SourceUrlBatchItem"]
