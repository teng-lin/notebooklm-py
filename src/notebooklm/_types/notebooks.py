"""Private notebook type implementations."""

from __future__ import annotations

import logging
import reprlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..rpc import RPCMethod, safe_index
from .common import _datetime_from_timestamp
from .sources import SourceType

logger = logging.getLogger(__name__)

# ``Notebook.from_api_response`` decodes rows from BOTH ``LIST_NOTEBOOKS`` (each
# row in the list envelope) and ``GET_NOTEBOOK`` (the single ``nb_info`` row).
# The positional descents below route through ``safe_index`` purely for the
# shared schema-drift telemetry seam; every descent is *length-guarded first*
# so ``safe_index`` is only ever invoked on a slot the guard already proved
# present — it therefore cannot raise here, preserving the historical
# "short / malformed rows soft-degrade to a default" contract (the same
# length-guard-then-``safe_index`` style ``NoteRow`` uses). ``LIST_NOTEBOOKS``
# is used as the representative ``method_id`` for diagnostics since the list
# path is the primary producer; a drift diagnostic would still point at the
# notebook-row family.
_NOTEBOOK_METHOD_ID = RPCMethod.LIST_NOTEBOOKS.value


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
    sources = (
        safe_index(data, 1, method_id=_NOTEBOOK_METHOD_ID, source="Notebook.sources_count")
        if len(data) > 1
        else None
    )
    return len(sources) if isinstance(sources, list) else 0


@dataclass
class Notebook:
    """Represents a NotebookLM notebook."""

    id: str
    title: str
    created_at: datetime | None = None
    sources_count: int = 0
    is_owner: bool = True
    # ``modified_at`` is appended at the END of the field list so positional
    # construction stays unaffected (additive, defaults to ``None``).
    modified_at: datetime | None = None

    @classmethod
    def from_api_response(cls, data: list[Any]) -> Notebook:
        """Parse notebook from API response."""
        title_slot = (
            safe_index(data, 0, method_id=_NOTEBOOK_METHOD_ID, source="Notebook.title")
            if len(data) > 0
            else None
        )
        raw_title = title_slot if isinstance(title_slot, str) else ""
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
            raw_id = safe_index(data, 2, method_id=_NOTEBOOK_METHOD_ID, source="Notebook.id")
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
        # ``data[5][...]`` (the legitimately-absent block defaults below). The
        # slot read goes through ``safe_index`` (length-guarded first, so it
        # cannot raise) and the result is only retained when it is a list.
        meta_slot = (
            safe_index(data, 5, method_id=_NOTEBOOK_METHOD_ID, source="Notebook.metadata")
            if len(data) > 5
            else None
        )
        meta = meta_slot if isinstance(meta_slot, list) else None

        # ``meta[8]`` (``data[5][8][0]``) is the CREATION instant: a controlled
        # probe (create → add source @T0 → add source @T1) showed this slot
        # stayed pinned at the creation time across modifications, while
        # ``meta[5]`` advanced on each edit. The two slots were historically
        # swapped — ``created_at`` read ``meta[5]`` and so exposed the
        # last-modified time. ``meta[5]`` (``data[5][5][0]``) is now correctly
        # surfaced as ``modified_at``.
        created_at = None
        if meta is not None and len(meta) > 8:
            created_ts = safe_index(
                meta, 8, method_id=_NOTEBOOK_METHOD_ID, source="Notebook.created_at"
            )
            if isinstance(created_ts, list) and len(created_ts) > 0:
                created_at = _datetime_from_timestamp(
                    safe_index(
                        created_ts, 0, method_id=_NOTEBOOK_METHOD_ID, source="Notebook.created_at"
                    )
                )

        modified_at = None
        if meta is not None and len(meta) > 5:
            modified_ts = safe_index(
                meta, 5, method_id=_NOTEBOOK_METHOD_ID, source="Notebook.modified_at"
            )
            if isinstance(modified_ts, list) and len(modified_ts) > 0:
                modified_at = _datetime_from_timestamp(
                    safe_index(
                        modified_ts, 0, method_id=_NOTEBOOK_METHOD_ID, source="Notebook.modified_at"
                    )
                )

        is_owner = True
        if meta is not None and len(meta) > 1:
            # The API sends False in this slot for owner notebooks; truthy values mean shared.
            is_owner = (
                safe_index(meta, 1, method_id=_NOTEBOOK_METHOD_ID, source="Notebook.is_owner")
                is False
            )

        return cls(
            id=notebook_id,
            title=title,
            created_at=created_at,
            sources_count=sources_count,
            is_owner=is_owner,
            modified_at=modified_at,
        )


@dataclass
class SuggestedTopic:
    """A suggested topic/question for the notebook."""

    question: str
    prompt: str


@dataclass(frozen=True)
class PromptSuggestion:
    """An AI-suggested question/prompt to ask a notebook.

    Returned by :meth:`NotebooksAPI.suggest_prompts` (the ``otmP3b`` /
    ``GeneratePromptSuggestions`` RPC). Each suggestion pairs a short,
    human-readable ``title`` with a ready-to-send multi-line ``prompt`` that can
    be passed straight to :meth:`ChatAPI.ask`.

    Attributes:
        title: Short label for the suggestion (e.g. ``"Professional Briefing"``).
        prompt: The full multi-line instruction string to ask the notebook.
    """

    title: str
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
    def modified_at(self) -> datetime | None:
        """Get last-modified timestamp."""
        return self.notebook.modified_at

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
            "modified_at": self.modified_at.isoformat() if self.modified_at else None,
            "is_owner": self.is_owner,
            "sources": [s.to_dict() for s in self.sources],
        }
