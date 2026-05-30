"""Public typed models for the research namespace (issue #1209).

These dataclasses replace the ``dict[str, Any]`` returns of
``ResearchAPI.poll`` / ``start`` / ``wait_for_completion``. They mix in
:class:`~notebooklm._deprecation.MappingCompatMixin` so legacy
``result["status"]`` dict-subscript access keeps working (emitting a
``DeprecationWarning``) for one MINOR cycle; the dict-subscript bridge is
removed in v0.8.0 (see ``docs/deprecations.md``).

The models live here (rather than in ``_research_task_parser``) so they are
public typed surface; the parser re-imports them and stays the home of the
wire-row parsing logic.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, ClassVar

from .._deprecation import MappingCompatMixin

# Numeric ``result_type`` tags carried on a research source row. Web is the
# default; deep-research report entries use the report tag.
RESEARCH_RESULT_TYPE_WEB = 1
RESEARCH_RESULT_TYPE_DRIVE = 2
RESEARCH_RESULT_TYPE_REPORT = 5
_RESEARCH_RESULT_TYPE_ALIASES = {
    "web": RESEARCH_RESULT_TYPE_WEB,
    "drive": RESEARCH_RESULT_TYPE_DRIVE,
    "report": RESEARCH_RESULT_TYPE_REPORT,
}

ResearchResultType = int | str


def parse_result_type(value: Any) -> ResearchResultType:
    """Normalize known research source type tags while preserving unknown tags."""
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return _RESEARCH_RESULT_TYPE_ALIASES.get(value.lower(), value)
    return RESEARCH_RESULT_TYPE_WEB


class ResearchStatus(str, Enum):
    """Lifecycle status of a research task.

    A ``str`` enum, so equality with the historical magic strings keeps
    working: ``task.status == ResearchStatus.COMPLETED`` and
    ``task.status == "completed"`` are both ``True``. The values match the
    status strings the research code has always produced.
    """

    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    NO_RESEARCH = "no_research"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


@dataclass(frozen=True)
class ResearchSource(MappingCompatMixin):
    """One parsed research source (web result, drive file, or report entry)."""

    url: str
    title: str
    result_type: ResearchResultType = RESEARCH_RESULT_TYPE_WEB
    research_task_id: str | None = None
    report_markdown: str = ""

    # Legacy dict keys are only emitted when non-empty in ``to_public_dict``;
    # the mixin maps every key it knows to its attribute. ``research_task_id``
    # and ``report_markdown`` are still listed so ``source["report_markdown"]``
    # keeps working even though the dict-builder omits them when falsy.
    _COMPAT_KEYS: ClassVar[dict[str, str]] = {
        "url": "url",
        "title": "title",
        "result_type": "result_type",
        "research_task_id": "research_task_id",
        "report_markdown": "report_markdown",
    }

    @classmethod
    def from_public_dict(cls, source: Mapping[str, Any]) -> ResearchSource:
        """Normalize a public source dictionary into the model."""
        url_raw = source.get("url", "")
        title_raw = source.get("title", "Untitled")
        research_task_id_raw = source.get("research_task_id")
        report_markdown_raw = source.get("report_markdown", "")

        return cls(
            url=url_raw if isinstance(url_raw, str) else "",
            title=title_raw if isinstance(title_raw, str) else "Untitled",
            result_type=parse_result_type(source.get("result_type", RESEARCH_RESULT_TYPE_WEB)),
            research_task_id=research_task_id_raw
            if isinstance(research_task_id_raw, str)
            else None,
            report_markdown=report_markdown_raw if isinstance(report_markdown_raw, str) else "",
        )

    @property
    def is_report(self) -> bool:
        return self.result_type == RESEARCH_RESULT_TYPE_REPORT

    def with_report_markdown(self, report: str) -> ResearchSource:
        """Return a copy with ``report_markdown`` replaced."""
        return replace(self, report_markdown=report)

    def to_public_dict(self) -> dict[str, Any]:
        """Return the historical compatibility dictionary shape."""
        public: dict[str, Any] = {
            "url": self.url,
            "title": self.title,
            "result_type": self.result_type,
        }
        if self.research_task_id is not None:
            public["research_task_id"] = self.research_task_id
        if self.report_markdown:
            public["report_markdown"] = self.report_markdown
        return public


@dataclass(frozen=True)
class ResearchTask(MappingCompatMixin):
    """A research task and, at the top level, the sibling tasks seen in a poll.

    Returned by :meth:`ResearchAPI.poll` and
    :meth:`ResearchAPI.wait_for_completion`. ``sources`` are the parsed
    :class:`ResearchSource` rows for *this* task; ``tasks`` lists every task
    visible at the poll (the top-level result carries it, sub-tasks leave it
    empty).

    Use attribute access (``task.status``, ``task.sources``). Legacy
    ``task["status"]`` dict-subscript access still works via
    :class:`~notebooklm._deprecation.MappingCompatMixin` but emits a
    ``DeprecationWarning``; it is removed in v0.8.0.
    """

    task_id: str
    status: ResearchStatus
    query: str = ""
    sources: tuple[ResearchSource, ...] = ()
    summary: str = ""
    report: str = ""
    tasks: tuple[ResearchTask, ...] = ()

    # ``tasks`` is intentionally part of the legacy dict shape only for the
    # top-level poll result; sub-tasks emit ``tasks: []`` historically too,
    # so listing it here for every instance matches the old behavior.
    _COMPAT_KEYS: ClassVar[dict[str, str]] = {
        "task_id": "task_id",
        "status": "status",
        "query": "query",
        "sources": "sources",
        "summary": "summary",
        "report": "report",
        "tasks": "tasks",
    }

    @classmethod
    def empty(cls) -> ResearchTask:
        """Return the empty ``no_research`` placeholder result.

        Mirrors the historical ``{"status": "no_research", "tasks": []}`` dict
        returned when no research task is in flight.
        """
        return cls(task_id="", status=ResearchStatus.NO_RESEARCH)

    def _to_task_dict(self) -> dict[str, Any]:
        """Return the per-task dict shape (without the sibling ``tasks`` list).

        This is the historical shape of an individual task — both the entries
        inside the top-level ``tasks`` list and the output of
        :func:`parse_research_tasks`. It deliberately omits ``tasks`` so nested
        siblings do not recurse.
        """
        return {
            "task_id": self.task_id,
            "status": self.status.value,
            "query": self.query,
            "sources": [source.to_public_dict() for source in self.sources],
            "summary": self.summary,
            "report": self.report,
        }

    def to_public_dict(self) -> dict[str, Any]:
        """Return the historical top-level result dictionary shape.

        Used internally to build legacy JSON output and to back the
        :class:`~notebooklm._deprecation.MappingCompatMixin` dict-subscript
        bridge. The ``no_research`` placeholder mirrors the old empty-poll
        dict, which omitted the per-task fields and carried only ``status`` +
        ``tasks``.
        """
        sibling_tasks = [task._to_task_dict() for task in self.tasks]
        if self.status == ResearchStatus.NO_RESEARCH and not self.task_id:
            return {"status": self.status.value, "tasks": sibling_tasks}
        return {**self._to_task_dict(), "tasks": sibling_tasks}


@dataclass(frozen=True)
class ResearchStart(MappingCompatMixin):
    """Result of :meth:`ResearchAPI.start` — identifiers for a started task.

    Use attribute access (``result.task_id``). Legacy ``result["task_id"]``
    dict-subscript access still works (with a ``DeprecationWarning``) until
    v0.8.0.
    """

    task_id: str
    report_id: str | None
    notebook_id: str
    query: str
    mode: str

    def to_public_dict(self) -> dict[str, Any]:
        """Return the historical compatibility dictionary shape."""
        return {
            "task_id": self.task_id,
            "report_id": self.report_id,
            "notebook_id": self.notebook_id,
            "query": self.query,
            "mode": self.mode,
        }


@dataclass(frozen=True)
class MindMapResult(MappingCompatMixin):
    """Result of :meth:`ArtifactsAPI.generate_mind_map`.

    ``mind_map`` is the parsed mind-map structure (a dict when the backend
    returned JSON, the raw value otherwise, or ``None`` on an empty response).
    ``note_id`` is the id of the note the mind map was persisted to, or
    ``None`` when persistence did not yield a usable id.

    Use attribute access (``result.mind_map``). Legacy ``result["mind_map"]``
    dict-subscript access still works (with a ``DeprecationWarning``) until
    v0.8.0.
    """

    mind_map: Any = None
    note_id: str | None = None

    def to_public_dict(self) -> dict[str, Any]:
        """Return the historical compatibility dictionary shape."""
        return {"mind_map": self.mind_map, "note_id": self.note_id}


@dataclass(frozen=True)
class SourceGuide(MappingCompatMixin):
    """Result of :meth:`SourcesAPI.get_guide` — the AI "Source Guide".

    ``summary`` is the AI-generated markdown summary (with ``**bold**``
    keywords); ``keywords`` is the tuple of topic keyword strings (a tuple, not
    a list, so the frozen dataclass stays genuinely immutable — matching
    :attr:`ResearchTask.sources` / :attr:`ResearchTask.tasks`).

    Use attribute access (``guide.summary``). Legacy ``guide["summary"]``
    dict-subscript access still works (with a ``DeprecationWarning``) until
    v0.8.0; ``guide["keywords"]`` keeps returning a ``list`` for back-compat.
    """

    summary: str = ""
    keywords: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # Accept a list/iterable for ergonomics (callers and the renderer build
        # ``keywords`` as a list) while storing an immutable tuple. ``object``
        # bypass is required because the dataclass is frozen.
        if not isinstance(self.keywords, tuple):
            object.__setattr__(self, "keywords", tuple(self.keywords))

    def to_public_dict(self) -> dict[str, Any]:
        """Return the historical compatibility dictionary shape.

        ``keywords`` is materialized as a fresh ``list`` so a caller mutating
        the returned dict cannot corrupt the frozen dataclass's state, and so
        the legacy ``guide["keywords"]`` shape stays a ``list``.
        """
        return {"summary": self.summary, "keywords": list(self.keywords)}
