"""Public types for the unified mind-map API (``client.mind_maps``).

NotebookLM has two distinct mind-map objects (issue #1256): the **note-backed**
JSON kind (stored as a note, created via ``GENERATE_MIND_MAP``) and the newer
**interactive** kind (a studio artifact, ``type 4 / variant 4``, created via
``CREATE_ARTIFACT``). :class:`MindMap` is a pure value object that hides the
backing behind a :class:`MindMapKind` discriminator; tree-reading lives on the
API (``client.mind_maps.get_tree``), not on the value.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any


class MindMapKind(str, Enum):
    """Which backend a mind map lives in (issue #1256)."""

    NOTE_BACKED = "note_backed"
    INTERACTIVE = "interactive"


@dataclass(frozen=True)
class MindMap:
    """A mind map, independent of its backing.

    Attributes:
        id: The mind-map id (note id for note-backed, artifact id for interactive).
        notebook_id: The notebook the mind map belongs to.
        title: Display title.
        kind: :class:`MindMapKind` discriminator (``NOTE_BACKED`` / ``INTERACTIVE``).
        created_at: Creation time when known (interactive artifacts expose it;
            ``None`` for note-backed rows that don't carry a timestamp).
        tree: The parsed ``{"name", "children"}`` node tree when cheaply
            available (note-backed list rows carry it; interactive maps leave it
            ``None`` — fetch it with ``client.mind_maps.get_tree(...)``).
    """

    id: str
    notebook_id: str
    title: str
    kind: MindMapKind
    created_at: datetime | None = None
    tree: dict[str, Any] | None = None

    @property
    def is_interactive(self) -> bool:
        """Whether this is the interactive (studio-artifact) kind."""
        return self.kind == MindMapKind.INTERACTIVE
