"""Pure-value type for a NotebookLM source label.

Re-exported from ``notebooklm.types``. A source ``Label`` describes source
membership only — **no ``kind``, no ``artifact_ids``** (a future artifact-label
surface is a separate type).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Label:
    """A NotebookLM source label (a topic grouping of sources).

    Notebook-scoped. Membership is many-to-many: a source may belong to multiple
    labels, and a label owns a list of source IDs (the source carries no
    back-reference).
    """

    id: str
    name: str
    notebook_id: str | None = None
    emoji: str | None = None
    # Source UUIDs in this label. Empty for a freshly-created (still empty) label.
    source_ids: list[str] = field(default_factory=list)

    @classmethod
    def from_api_response(
        cls,
        data: list[Any],
        *,
        notebook_id: str | None = None,
        method_id: str | None = None,
    ) -> Label:
        """Parse one label 4-tuple ``[name, sources, label_id, emoji]``."""
        from .._row_adapters.labels import LabelRow

        row = LabelRow.from_label_tuple(data, method_id=method_id)
        return cls(
            id=row.id,
            name=row.name,
            notebook_id=notebook_id,
            emoji=row.emoji or None,
            source_ids=list(row.source_ids),
        )
