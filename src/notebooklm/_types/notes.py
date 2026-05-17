"""Private note type implementations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .common import _datetime_from_timestamp as _common_datetime_from_timestamp


def _datetime_from_timestamp(value: Any) -> datetime | None:
    """Convert an API seconds timestamp to ``datetime``, returning ``None`` if invalid."""
    return _common_datetime_from_timestamp(value, datetime_type=datetime)


@dataclass
class Note:
    """Represents a user-created note in a notebook.

    Notes are distinct from artifacts - they are user-created content,
    not AI-generated. Notes support different operations than artifacts
    (export to Docs/Sheets, convert to source).
    """

    id: str
    notebook_id: str
    title: str
    content: str
    created_at: datetime | None = None

    @classmethod
    def from_api_response(cls, data: list[Any], notebook_id: str) -> Note:
        """Parse note from API response.

        Args:
            data: Raw API response list.
            notebook_id: The parent notebook ID.

        Returns:
            Note instance.
        """
        note_id = data[0] if len(data) > 0 else ""
        title = data[1] if len(data) > 1 else ""
        content = data[2] if len(data) > 2 else ""

        created_at = None
        if len(data) > 3 and isinstance(data[3], list) and len(data[3]) > 0:
            created_at = _datetime_from_timestamp(data[3][0])

        return cls(
            id=str(note_id),
            notebook_id=notebook_id,
            title=str(title),
            content=str(content),
            created_at=created_at,
        )
