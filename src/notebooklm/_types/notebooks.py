"""Private notebook type implementations."""

from __future__ import annotations

import logging
import reprlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .common import _datetime_from_timestamp
from .sources import SourceType

logger = logging.getLogger(__name__)


@dataclass
class SourceSummary:
    """Simplified source information for metadata export."""

    kind: SourceType
    title: str | None = None
    url: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        """Convert to dictionary for JSON serialization."""
        return {
            "type": self.kind.value,
            "title": self.title,
            "url": self.url,
        }


def _extract_notebook_sources_count(data: list[Any]) -> int:
    """Extract the embedded source count from a notebook API payload."""
    sources = data[1] if len(data) > 1 else None
    return len(sources) if isinstance(sources, list) else 0


@dataclass
class Notebook:
    """Represents a NotebookLM notebook."""

    id: str
    title: str
    created_at: datetime | None = None
    sources_count: int = 0
    is_owner: bool = True

    @classmethod
    def from_api_response(cls, data: list[Any]) -> Notebook:
        """Parse notebook from API response."""
        raw_title = data[0] if len(data) > 0 and isinstance(data[0], str) else ""
        title = raw_title.replace("thought\n", "").strip()
        sources_count = _extract_notebook_sources_count(data)
        # ``data[2]`` is the notebook id. A short row / ``None`` slot keeps
        # the historical silent ``""``-degrade — this factory parses rows out
        # of whole-list responses, so raising would abort sibling rows. A
        # *present-but-malformed* slot (non-str, non-None) still degrades to
        # ``""`` for the same reason, but now logs a WARNING: a silently
        # fabricated empty id is otherwise indistinguishable from a real row
        # (#1485 absence-vs-malformed policy).
        notebook_id = ""
        if len(data) > 2:
            raw_id = data[2]
            if isinstance(raw_id, str):
                notebook_id = raw_id
            elif raw_id is not None:
                logger.warning(
                    "Notebook row id slot malformed — fabricating empty id "
                    "(expected str at data[2], got %s; row=%s)",
                    type(raw_id).__name__,
                    reprlib.repr(data),
                )

        # ``data[5]`` is the metadata block; bind it once so the timestamp and
        # owner-flag descents read a single named local instead of re-chaining
        # ``data[5][...]`` (the legitimately-absent block defaults below).
        meta = data[5] if len(data) > 5 and isinstance(data[5], list) else None

        created_at = None
        if meta is not None and len(meta) > 5:
            ts_data = meta[5]
            if isinstance(ts_data, list) and len(ts_data) > 0:
                created_at = _datetime_from_timestamp(ts_data[0])

        is_owner = True
        if meta is not None and len(meta) > 1:
            # The API sends False in this slot for owner notebooks; truthy values mean shared.
            is_owner = meta[1] is False

        return cls(
            id=notebook_id,
            title=title,
            created_at=created_at,
            sources_count=sources_count,
            is_owner=is_owner,
        )


@dataclass
class SuggestedTopic:
    """A suggested topic/question for the notebook."""

    question: str
    prompt: str


@dataclass
class NotebookDescription:
    """AI-generated description and suggested topics for a notebook."""

    summary: str
    suggested_topics: list[SuggestedTopic] = field(default_factory=list)

    @classmethod
    def from_api_response(cls, data: dict[str, Any]) -> NotebookDescription:
        """Parse from get_notebook_description() response."""
        topics = [
            SuggestedTopic(question=t.get("question", ""), prompt=t.get("prompt", ""))
            for t in data.get("suggested_topics", [])
        ]
        return cls(
            summary=data.get("summary", ""),
            suggested_topics=topics,
        )


@dataclass
class NotebookMetadata:
    """Combined notebook metadata with sources list."""

    notebook: Notebook
    sources: list[SourceSummary] = field(default_factory=list)

    @property
    def id(self) -> str:
        """Get notebook ID."""
        return self.notebook.id

    @property
    def title(self) -> str:
        """Get notebook title."""
        return self.notebook.title

    @property
    def created_at(self) -> datetime | None:
        """Get creation timestamp."""
        return self.notebook.created_at

    @property
    def is_owner(self) -> bool:
        """Get owner status."""
        return self.notebook.is_owner

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "id": self.id,
            "title": self.title,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "is_owner": self.is_owner,
            "sources": [s.to_dict() for s in self.sources],
        }
