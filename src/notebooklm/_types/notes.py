"""Private note type implementations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class Note:
    """Represents a user-created note in a notebook.

    Notes are distinct from artifacts - they are user-created content,
    not AI-generated. Notes support different operations than artifacts
    (export to Docs/Sheets, convert to source).

    Raw note rows are decoded through
    :class:`notebooklm._row_adapters.notes.NoteRow` (the typed positional
    view); :class:`Note` instances are constructed from those named
    properties in :meth:`notebooklm._notes.NotesAPI._parse_note`.
    """

    id: str
    notebook_id: str
    title: str
    content: str
    created_at: datetime | None = None
