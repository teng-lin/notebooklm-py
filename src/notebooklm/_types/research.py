"""Public typed models for the research namespace (issue #1209).

These dataclasses replace the ``dict[str, Any]`` returns of
``ResearchAPI.poll`` / ``start`` / ``wait_for_completion``. They are
attribute-only frozen dataclasses: use ``result.status`` (the dict-subscript
back-compat bridge that warned in v0.7.0 was removed in v0.8.0, issue #1251).
The ``to_public_dict()`` method survives — it builds the historical JSON shape
for CLI output and is unrelated to the dropped subscript bridge.

The models live here (rather than in ``_web.rows.research_task``) so they are
public typed surface; the parser re-imports them and stays the home of the
wire-row parsing logic.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import Enum
from typing import Any

from .enums import DiscoveryMode

logger = logging.getLogger(__name__)

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

# Numeric search-source tags echoed back on a task's query block
# (``task_info[1][1]``) — the same tags ``ResearchAPI.start`` sends. Distinct
# axis from ``RESEARCH_RESULT_TYPE_*`` above despite sharing the values 1/2:
# those tag an individual RESULT ROW's origin, these tag what the RUN searched.
RESEARCH_SOURCE_TYPE_WEB = 1
RESEARCH_SOURCE_TYPE_DRIVE = 2

# ---------------------------------------------------------------------------
# Backend task status codes (``task_info[4]``)
# ---------------------------------------------------------------------------
# Live-captured against POLL_RESEARCH for issue #1964 (see
# docs/rpc-reference.md). Issue #1922 deliberately refused to *infer* a
# no-results state from an unexplained code; codes 1-4 below were observed by
# that probe, each tied to a reproduced scenario (6 was already known):
#
#   1  in-flight run, polled repeatedly before it settled
#   2  fast web + fast drive runs that returned sources
#   3  drive runs whose query matched no Drive file — reproduced 3x with
#      distinct queries, always with an empty ``[None, None, None, None, 1]``
#      sources bundle and zero sources. Not seen on a web run in these probes:
#      a web search with the same gibberish query still returned code 2 with
#      loosely-related results. Three captures are strong evidence, not proof,
#      that 3 means "no matches" — so the decode only asserts it inside the
#      envelope it was observed in (see ``ResearchTask.termination_reason``).
#   4  a deep run cancelled mid-flight via CANCEL_RESEARCH
#   6  completed — inherited from earlier undocumented knowledge and present in
#      no captured payload (0 of 9 task rows across the POLL cassettes; every
#      completed run, deep included, reports 2). Kept as forward-compat only.
#
# These are undocumented Google internals and can change without notice — the
# same volatility class as the RPC method ids. An unrecognised code stays a
# generic terminal failure rather than being guessed at.
RESEARCH_STATUS_CODE_IN_PROGRESS = 1
RESEARCH_STATUS_CODE_COMPLETED = 2
RESEARCH_STATUS_CODE_NO_RESULTS = 3
RESEARCH_STATUS_CODE_CANCELLED = 4
RESEARCH_STATUS_CODE_COMPLETED_DEEP = 6


class ResearchTerminationReason(str, Enum):
    """Why a research run ended — the differentiated view of :class:`ResearchStatus`.

    :class:`ResearchStatus` stays deliberately coarse (a Drive run that matched
    nothing and a genuinely broken run are both ``FAILED``), which left callers
    unable to tell "refine the query" from "something went wrong" (issue
    #1964). This enum carries that distinction alongside — it does **not**
    change ``status``, so existing ``status == "failed"`` checks keep working.

    A ``str`` enum, so ``reason == "no_results"`` compares equal to
    :attr:`NO_RESULTS`, matching :class:`ResearchStatus`'s ergonomics. The
    flip side: because both are ``str`` enums, ``ResearchTerminationReason.
    COMPLETED == ResearchStatus.COMPLETED`` is ``True`` (likewise
    ``IN_PROGRESS``). Compare a reason against a reason.

    ``UNKNOWN`` is the honest default for a terminal code this client has not
    live-captured: the run ended, we can't say why. It is distinct from
    ``None``, which means there was no code to map at all (the ``no_research``
    / ``not_found`` placeholders, or a malformed row). There is deliberately no
    generic ``FAILED`` member: the coarse :class:`ResearchStatus` already says
    that much, so a member here would only restate it.
    """

    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    NO_RESULTS = "no_results"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


_TERMINATION_REASON_BY_CODE = {
    RESEARCH_STATUS_CODE_IN_PROGRESS: ResearchTerminationReason.IN_PROGRESS,
    RESEARCH_STATUS_CODE_COMPLETED: ResearchTerminationReason.COMPLETED,
    RESEARCH_STATUS_CODE_NO_RESULTS: ResearchTerminationReason.NO_RESULTS,
    RESEARCH_STATUS_CODE_CANCELLED: ResearchTerminationReason.CANCELLED,
    RESEARCH_STATUS_CODE_COMPLETED_DEEP: ResearchTerminationReason.COMPLETED,
}


def status_from_termination_reason(
    reason: ResearchTerminationReason | None,
) -> ResearchStatus:
    """Coarsen a termination reason into the lifecycle :class:`ResearchStatus`.

    The SINGLE source of truth for "which wire codes mean in-flight /
    completed / failed". :func:`_web.rows.research_task._status_from_code` used to
    carry a second, independent copy of that table written in bare literals;
    two tables meant a future code added to only one of them could produce a
    self-contradictory task (``status="completed"`` alongside
    ``termination_reason="unknown"`` and a "retry the run" hint on a run that
    succeeded). Deriving one from the other makes that state unrepresentable.

    ``None`` (no code to map) coarsens to ``IN_PROGRESS``, preserving the
    historical treatment of a poll that carried no status code.
    """
    if reason is None or reason == ResearchTerminationReason.IN_PROGRESS:
        return ResearchStatus.IN_PROGRESS
    if reason == ResearchTerminationReason.COMPLETED:
        return ResearchStatus.COMPLETED
    # NO_RESULTS / CANCELLED / UNKNOWN are all terminal non-successes. Unknown
    # codes stay FAILED so wait loops do not spin until timeout after the
    # backend rejects a task.
    return ResearchStatus.FAILED


def termination_reason_from_code(status_code: int | None) -> ResearchTerminationReason | None:
    """Map a raw ``task_info[4]`` status code to a termination reason.

    Returns ``None`` when there is no code to map (the ``no_research`` /
    ``not_found`` placeholders, or a malformed row), and
    :attr:`ResearchTerminationReason.UNKNOWN` for a terminal code outside the
    live-captured set — an unrecognised code means the run ended for a reason
    this client cannot name, which is reported as such rather than guessed.
    """
    if status_code is None:
        return None
    reason = _TERMINATION_REASON_BY_CODE.get(status_code)
    if reason is None:
        # These codes are undocumented Google internals in the same volatility
        # class as the RPC method ids, and an unmapped one is the cheapest
        # signal that the vocabulary moved. Every sibling drift path in the
        # parser logs; this one should too.
        logger.warning(
            "unmapped research status code %r (POLL_RESEARCH task_info[4]); "
            "reporting termination_reason=unknown",
            status_code,
        )
        return ResearchTerminationReason.UNKNOWN
    return reason


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
    # ``NO_RESEARCH`` means "nothing in flight" (an unfiltered poll saw no
    # tasks); ``NOT_FOUND`` is the poll-observed absence of a *specific*
    # requested ``task_id`` (the task is not among the polled results). It is a
    # typed lifecycle sentinel, not an error — distinct from looking up a
    # resource that does not exist, which raises (ADR-0019 Rule 4, #1346).
    NOT_FOUND = "not_found"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


@dataclass(frozen=True)
class ResearchSource:
    """One parsed research source (web result, drive file, or report entry)."""

    url: str
    title: str
    result_type: ResearchResultType = RESEARCH_RESULT_TYPE_WEB
    research_task_id: str | None = None
    report_markdown: str = ""
    source_ordinal: int | None = None
    #: ``DiscoveredSource.hint`` — the backend's own one-line explanation of
    #: why it surfaced this source (#2122). ``""`` when the row carried none;
    #: live-populated on every fast-research web row observed, and absent on
    #: the deep-research report row.
    hint: str = ""

    @classmethod
    def from_public_dict(cls, source: Mapping[str, Any]) -> ResearchSource:
        """Normalize a public source dictionary into the model."""
        url_raw = source.get("url", "")
        title_raw = source.get("title", "Untitled")
        research_task_id_raw = source.get("research_task_id")
        report_markdown_raw = source.get("report_markdown", "")
        source_ordinal_raw = source.get("source_ordinal")
        hint_raw = source.get("hint", "")

        return cls(
            url=url_raw if isinstance(url_raw, str) else "",
            title=title_raw if isinstance(title_raw, str) else "Untitled",
            result_type=parse_result_type(source.get("result_type", RESEARCH_RESULT_TYPE_WEB)),
            research_task_id=research_task_id_raw
            if isinstance(research_task_id_raw, str)
            else None,
            report_markdown=report_markdown_raw if isinstance(report_markdown_raw, str) else "",
            source_ordinal=source_ordinal_raw if type(source_ordinal_raw) is int else None,
            hint=hint_raw if isinstance(hint_raw, str) else "",
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
        if self.source_ordinal is not None:
            public["source_ordinal"] = self.source_ordinal
        if self.hint:
            public["hint"] = self.hint
        return public


ResearchSourceInput = ResearchSource | Mapping[str, Any]


@dataclass(frozen=True)
class ResearchTask:
    """A research task and, at the top level, the sibling tasks seen in a poll.

    Returned by :meth:`ResearchAPI.poll` and
    :meth:`ResearchAPI.wait_for_completion`. ``sources`` are the parsed
    :class:`ResearchSource` rows for *this* task; ``tasks`` lists every task
    visible at the poll (the top-level result carries it, sub-tasks leave it
    empty).

    Use attribute access (``task.status``, ``task.sources``); the dict-subscript
    back-compat bridge was removed in v0.8.0 (issue #1251).
    """

    task_id: str
    status: ResearchStatus
    query: str = ""
    sources: tuple[ResearchSource, ...] = ()
    summary: str = ""
    report: str = ""
    tasks: tuple[ResearchTask, ...] = ()
    # Raw backend status code (``task_info[4]``) preserved verbatim (issue
    # #1922, F10). ``status`` coarsens this into the lifecycle enum and cannot
    # tell a "no matches" failure sub-code from a genuine error; keeping the raw
    # integer lets a caller distinguish failure sub-codes. ``None`` when the poll
    # carried no code (empty / not-found placeholders, or a malformed row). It is
    # deliberately absent from :meth:`to_public_dict` / :meth:`_to_task_dict` so
    # the CLI ``--json`` shape stays byte-stable — the MCP ``research_status``
    # tool surfaces it directly from the attribute.
    #
    # Both this and ``source_type`` are ordinary dataclass fields, so they take
    # part in the generated ``__eq__`` / ``__hash__`` / ``__repr__``: a parsed
    # task does not compare equal to one hand-built without them. That is
    # deliberate rather than an oversight — ``termination_reason``,
    # ``reason_message`` and ``hint`` all derive from these two, so excluding
    # them from comparison would let two "equal" tasks carry different
    # explanations and different remediation.
    status_code: int | None = None
    # Search source the run was started with, echoed back on the query block
    # (``task_info[1][1]``): 1 = web, 2 = drive (issue #1964). ``None`` when the
    # poll carried no tag. Drives the source-specific remediation :attr:`hint`
    # — "no matches" means something different for a Drive filename search than
    # for a web search. Also omitted from the public dict shapes for the same
    # byte-stability reason as ``status_code``.
    source_type: int | None = None
    # The four always-populated task-level slots #2122 recovered. Like
    # ``status_code`` / ``source_type`` above they are deliberately absent from
    # :meth:`to_public_dict` / :meth:`_to_task_dict` so the CLI ``--json`` shape
    # stays byte-stable; the MCP ``research_status`` tool and the REST status
    # route surface them from these attributes instead.
    #
    # ``discovery_mode`` is the mode the run is executing under
    # (``task_info[2]``), echoed back from the start params — so a poll can
    # confirm a run is deep rather than the caller having to remember it.
    discovery_mode: DiscoveryMode | None = None
    # ``created_at`` / ``updated_at`` are ``task_data[3]`` / ``task_data[2]``
    # (that order is not a typo — see ``ResearchTaskRow._UPDATED_AT_POS``).
    # Together they make a run's age and time-in-flight reportable. NOTE both
    # take part in ``__eq__``/``__hash__`` like every field above, so two polls
    # of one in-flight task do NOT compare equal (``updated_at`` advances
    # between them). Same deliberate choice the block above makes for
    # ``status_code``/``source_type``: a task carrying different metadata is a
    # different observation. Nothing in this client compares tasks.
    created_at: datetime | None = None
    updated_at: datetime | None = None
    # ``task_data[4]`` — the opaque account id the run belongs to. Two live
    # accounts produced two distinct, internally-constant values, which is what
    # makes it account-scoped rather than a constant.
    #
    # Deliberately NOT forwarded to the MCP tool or the REST route, unlike its
    # three siblings above: it identifies the *account*, not the run, so it is
    # of no use to an agent deciding what to do next, and account identifiers
    # are the last thing worth copying into an agent's context by default. It
    # stays on the Python object for diagnostics.
    account_id: str | None = None

    @property
    def duration(self) -> timedelta | None:
        """Time between :attr:`created_at` and :attr:`updated_at`, else ``None``.

        The backend advances ``updated_at`` while a run is in flight, so on an
        in-flight task this is how long it has been running as of this poll,
        and on a settled one how long the run took (6s on the live fast run in
        #2122, 5m43s on the live deep run). Whether ``updated_at`` stops
        advancing once a run settles was NOT observed — only one
        in-flight-to-settled transition was captured per run.

        ``None`` when either timestamp is missing, and also when the interval
        is **negative**. A negative duration is not a real outcome: it means
        the two positional slots have been read the wrong way round, which is
        this decode's most fragile claim (the issue text had them reversed and
        only live polling settled it). Returning ``None`` keeps a future slot
        swap from surfacing as a negative ``duration_seconds`` in the MCP tool
        and the REST route, where an agent would act on it.
        """
        if self.created_at is None or self.updated_at is None:
            return None
        elapsed = self.updated_at - self.created_at
        if elapsed < timedelta(0):
            logger.warning(
                "research task %r reports updated_at (%s) BEFORE created_at (%s); "
                "reporting duration=None — the POLL_RESEARCH timestamp slots may have moved",
                self.task_id,
                self.updated_at.isoformat(),
                self.created_at.isoformat(),
            )
            return None
        return elapsed

    @property
    def termination_reason(self) -> ResearchTerminationReason | None:
        """Why this run ended, differentiating the coarse :attr:`status` (#1964).

        Derived from the raw :attr:`status_code`, so a Drive run that simply
        matched nothing (``NO_RESULTS``) is distinguishable from a cancelled
        run (``CANCELLED``) and from a terminal code this client has not
        captured (``UNKNOWN``) — all three of which :attr:`status` flattens to
        ``FAILED``. ``None`` when the poll carried no code.

        The ``NO_RESULTS`` mapping is only asserted inside the envelope it was
        observed in — a code-3 row that nonetheless carries sources falls back
        to ``UNKNOWN`` rather than reporting "found no matches" alongside the
        matches it found. Three live captures are good evidence for what code 3
        means, not proof, so the decode refuses to contradict itself.
        """
        reason = termination_reason_from_code(self.status_code)
        if reason == ResearchTerminationReason.NO_RESULTS and self.sources:
            return ResearchTerminationReason.UNKNOWN
        return reason

    @property
    def is_drive_search(self) -> bool:
        """Whether this run is KNOWN to have searched Google Drive."""
        return self.source_type == RESEARCH_SOURCE_TYPE_DRIVE

    @property
    def is_web_search(self) -> bool:
        """Whether this run is KNOWN to have searched the web."""
        return self.source_type == RESEARCH_SOURCE_TYPE_WEB

    @property
    def _searched_label(self) -> str | None:
        """Name of the searched corpus, or ``None`` when the tag is unknown.

        Deliberately tri-state. ``source_type`` is preserved verbatim (an
        unrecognised int is kept for diagnostics, as ``status_code`` is), but
        only the two live-captured tags are *interpreted* — anything else, and
        the absent-tag case, must not be silently narrated as a web search.
        """
        if self.is_drive_search:
            return "Google Drive"
        if self.is_web_search:
            return "the web"
        return None

    @property
    def reason_message(self) -> str | None:
        """Human-readable explanation of a non-successful outcome, else ``None``.

        Every run whose termination reason is NOT success-or-in-flight carries
        one, so a caller never has to render a bare ``failed`` with nothing to
        show a user (#1964). Note this keys off the REASON, not the source
        count: a run that completed normally with zero sources is a
        ``completed`` outcome and returns ``None`` here — the empty-import
        refusal for that case lives in ``_app.research``.
        """
        reason = self.termination_reason
        if reason is None or reason in (
            ResearchTerminationReason.COMPLETED,
            ResearchTerminationReason.IN_PROGRESS,
        ):
            return None
        where = self._searched_label
        quoted = f" for {self.query!r}" if self.query else ""
        if reason == ResearchTerminationReason.NO_RESULTS:
            scope = f" of {where}" if where else ""
            return f"The search{scope} found no matches{quoted}."
        if reason == ResearchTerminationReason.CANCELLED:
            return f"The research run{quoted} was cancelled before it completed."
        # Deliberately does NOT assert the run ended: an unrecognised code may
        # turn out to be a future non-terminal state, so report only what was
        # actually observed.
        return (
            f"The research run{quoted} reported an unrecognised backend status "
            f"code ({self.status_code})."
        )

    @property
    def hint(self) -> str | None:
        """Remediation guidance matched to :attr:`termination_reason`, else ``None``.

        The advice genuinely differs by search source: an empty Drive search is
        usually a filename-matching problem with a direct workaround (add the
        document by id), whereas an empty web search calls for a broader query.
        """
        reason = self.termination_reason
        if reason == ResearchTerminationReason.NO_RESULTS:
            if self.is_drive_search:
                return (
                    "Try the exact Drive filename, the document URL, or add the "
                    "document directly by its document id."
                )
            if self.is_web_search:
                return "Try a broader or differently-worded query."
            # Search source unknown: offer BOTH remediations rather than
            # guessing. Emitting only the web advice here would hand a Drive
            # run whose tag drifted the one hint that cannot help it — the
            # exact complaint #1964 was filed about.
            return (
                "Try a broader or differently-worded query; if this searched "
                "Drive, try the exact filename, the document URL, or the "
                "document id."
            )
        if reason == ResearchTerminationReason.CANCELLED:
            return "Start a new research run if you still need these sources."
        if reason == ResearchTerminationReason.UNKNOWN:
            # The MESSAGE deliberately does not assert the run ended (the code
            # is unrecognised, so we cannot know). The HINT states what this
            # client actually does with it: ``status_from_termination_reason``
            # coarsens UNKNOWN to FAILED, so wait loops stop rather than spin —
            # advising "poll again" would contradict our own handling.
            return "This client treats an unrecognised code as terminal — start a new run."
        return None

    @classmethod
    def empty(cls) -> ResearchTask:
        """Return the empty ``no_research`` placeholder result.

        Mirrors the historical ``{"status": "no_research", "tasks": []}`` dict
        returned when no research task is in flight.
        """
        return cls(task_id="", status=ResearchStatus.NO_RESEARCH)

    @classmethod
    def not_found(cls, task_id: str) -> ResearchTask:
        """Return the ``not_found`` placeholder for an absent pinned task.

        Used when a poll explicitly requested ``task_id`` but that task is not
        among the polled results. Distinct from :meth:`empty` (nothing in
        flight): this carries the requested ``task_id`` and the typed
        :attr:`ResearchStatus.NOT_FOUND` sentinel (ADR-0019 Rule 4).
        """
        return cls(task_id=task_id, status=ResearchStatus.NOT_FOUND)

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

        Used internally to build legacy JSON output. The ``no_research``
        placeholder mirrors the old empty-poll dict, which omitted the per-task
        fields and carried only ``status`` + ``tasks``.
        """
        sibling_tasks = [task._to_task_dict() for task in self.tasks]
        if self.status == ResearchStatus.NO_RESEARCH and not self.task_id:
            return {"status": self.status.value, "tasks": sibling_tasks}
        return {**self._to_task_dict(), "tasks": sibling_tasks}


@dataclass(frozen=True)
class ResearchStart:
    """Result of :meth:`ResearchAPI.start` — identifiers for a started task.

    Use attribute access (``result.task_id``); the dict-subscript back-compat
    bridge was removed in v0.8.0 (issue #1251).
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
class MindMapResult:
    """Result of :meth:`ArtifactsAPI.generate_mind_map`.

    ``mind_map`` is the parsed mind-map structure (a dict when the backend
    returned JSON, the raw value otherwise, or ``None`` on an empty response).
    ``note_id`` is the id of the note the mind map was persisted to, or
    ``None`` when persistence did not yield a usable id. ``created_at`` is the
    persisted note's creation time when the CREATE_NOTE response carried it
    (issue #1529), else ``None``.

    Use attribute access (``result.mind_map``); the dict-subscript back-compat
    bridge was removed in v0.8.0 (issue #1251).
    """

    mind_map: Any = None
    note_id: str | None = None
    created_at: datetime | None = None

    def to_public_dict(self) -> dict[str, Any]:
        """Return the historical compatibility dictionary shape."""
        return {"mind_map": self.mind_map, "note_id": self.note_id}


@dataclass(frozen=True)
class SourceGuide:
    """Result of :meth:`SourcesAPI.get_guide` — the AI "Source Guide".

    ``summary`` is the AI-generated markdown summary (with ``**bold**``
    keywords); ``keywords`` is the tuple of topic keyword strings (a tuple, not
    a list, so the frozen dataclass stays genuinely immutable — matching
    :attr:`ResearchTask.sources` / :attr:`ResearchTask.tasks`).

    Use attribute access (``guide.summary``, ``guide.keywords``); the
    dict-subscript back-compat bridge was removed in v0.8.0 (issue #1251).
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
        the JSON shape stays a ``list``.
        """
        return {"summary": self.summary, "keywords": list(self.keywords)}
