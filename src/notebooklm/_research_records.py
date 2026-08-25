"""Transport-neutral records and operation definitions for Research."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum, unique
from typing import Final

from ._operations import CallPolicy, Operation, OperationDef


@dataclass(frozen=True, slots=True)
class ResearchSourceRecord:
    """One neutral research result row (web hit, drive file, or report entry)."""

    url: str
    title: str
    result_type: int | str
    research_task_id: str | None = None
    report_markdown: str = ""
    source_ordinal: int | None = None
    hint: str = ""


@dataclass(frozen=True, slots=True)
class ResearchTaskRecord:
    """Neutral research task observed by one poll."""

    task_id: str
    status: str
    query: str = ""
    sources: tuple[ResearchSourceRecord, ...] = ()
    summary: str = ""
    report: str = ""
    status_code: int | None = None
    source_type: int | None = None
    discovery_mode: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    account_id: str | None = None


@unique
class ResearchTaskStatus(str, Enum):
    """Lifecycle status one polled research task reports.

    The values are exactly ``ResearchTaskRecord.status``, which the codec fills
    from the wire and which the public ``ResearchStatus`` mirrors one-for-one.
    Kept as a neutral enum so the wait loop can compare a decoded status
    against a named member instead of against a public model's enum.

    ``NO_RESEARCH`` and ``NOT_FOUND`` are absence sentinels rather than backend
    states: no task was in flight at all, and a pinned discriminator was not
    among the polled tasks. Neither is ever decoded off the wire — they are
    what :class:`ResearchTaskSelectionResult` expresses structurally.
    """

    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    NO_RESEARCH = "no_research"
    NOT_FOUND = "not_found"


#: Statuses a bounded wait stops on. Every other status keeps it polling until
#: its deadline: a pinned task briefly absent from a poll is replication lag,
#: not a terminal outcome.
RESEARCH_TERMINAL_STATUSES: Final[frozenset[ResearchTaskStatus]] = frozenset(
    {ResearchTaskStatus.COMPLETED, ResearchTaskStatus.FAILED}
)


@dataclass(frozen=True, slots=True)
class ResearchTaskSelectionResult:
    """The task one poll selected, plus every task that poll observed.

    Three outcomes, distinguished structurally rather than by a status string:
    ``task`` set is a selection; ``task`` unset with ``missing_task_id`` set is
    the poll-observed absence of a specifically requested id; both unset is an
    empty poll. A facade turns each into the public lifecycle sentinel it has
    always returned, which is why the service needs no ``NOT_FOUND`` /
    ``NO_RESEARCH`` placeholder record of its own.

    ``tasks`` is the whole poll in backend order, unfiltered by the selection
    when no discriminator was given and filtered to the match when one was.
    """

    task: ResearchTaskRecord | None = None
    tasks: tuple[ResearchTaskRecord, ...] = ()
    missing_task_id: str | None = None


@dataclass(frozen=True, slots=True)
class ResearchWaitInput:
    """One bounded wait over a notebook's in-flight research.

    ``timeout`` and ``poll_interval`` arrive already validated and already
    resolved: the public ``initial_interval`` sentinel, its ``TypeError`` for a
    non-numeric value and the ``ValueError`` for a non-positive cadence are the
    facade's, because they describe a *published keyword* rather than the
    workflow.
    """

    notebook_id: str
    task_id: str | None = None
    timeout: float = 1800.0
    poll_interval: float = 5.0


@unique
class ResearchSearchSource(str, Enum):
    """Corpus a research run searches."""

    WEB = "web"
    DRIVE = "drive"


@unique
class ResearchMode(str, Enum):
    """Discovery depth a research run executes under."""

    FAST = "fast"
    DEEP = "deep"


@dataclass(frozen=True, slots=True)
class ResearchStartInput:
    """Validated request for one research run."""

    notebook_id: str
    query: str
    search_source: ResearchSearchSource
    mode: ResearchMode


@dataclass(frozen=True, slots=True)
class ResearchStartResult:
    """Identifiers a started run volunteered."""

    task_id: str
    report_id: str | None


@dataclass(frozen=True, slots=True)
class ResearchPollInput:
    """Notebook whose in-flight research tasks are being listed."""

    notebook_id: str


@dataclass(frozen=True, slots=True)
class ResearchPollResult:
    """Every research task visible at one poll, in backend order."""

    tasks: tuple[ResearchTaskRecord, ...]


@dataclass(frozen=True, slots=True)
class ResearchCancelInput:
    """Run to cancel plus the notebook used purely for request routing."""

    notebook_id: str
    run_id: str


@dataclass(frozen=True, slots=True)
class ResearchCancelResult:
    """Fire-and-forget cancel acknowledgement; it carries no success signal."""


@unique
class ResearchImportEntryKind(str, Enum):
    """How one requested import entry is carried to the backend."""

    WEB = "web"
    REPORT = "report"


@dataclass(frozen=True, slots=True)
class ResearchImportEntry:
    """One neutral entry in an import batch, in the order it is sent."""

    kind: ResearchImportEntryKind
    title: str
    url: str = ""
    report_markdown: str = ""


@dataclass(frozen=True, slots=True)
class ResearchImportInput:
    """One import attempt for an already-filtered, already-ordered batch."""

    notebook_id: str
    task_id: str
    entries: tuple[ResearchImportEntry, ...]
    attempt_timeout: float | None = None


@dataclass(frozen=True, slots=True)
class ResearchImportedSourceRecord:
    """One source the import response confirmed by id."""

    id: str
    title: str


@dataclass(frozen=True, slots=True)
class ResearchImportResult:
    """Sources the import response acknowledged; may under-report commits."""

    imported: tuple[ResearchImportedSourceRecord, ...]


@dataclass(frozen=True, slots=True)
class ResearchImportCandidate:
    """One requested import entry, already lifted out of its public form.

    ``report`` is the facade's verdict on the historical public-dict report
    predicate — a report row whose markdown and title survived the public shape
    it was requested in. The service never re-derives it, so nothing below the
    facade has to know that a caller may pass either a ``ResearchSource`` or a
    loose mapping.
    """

    source: ResearchSourceRecord
    report: bool = False


@dataclass(frozen=True, slots=True)
class ResearchImportBatchInput:
    """One import attempt over an already-coerced candidate batch.

    ``remaining_budget`` is what is left of an enclosing
    :class:`ResearchImportVerifyInput`'s ``max_elapsed`` when this attempt
    starts; it clamps the attempt's read window so one attempt cannot outlive
    that budget (#2205). ``None`` is a direct caller taking the full
    batch-scaled window.
    """

    notebook_id: str
    task_id: str
    candidates: tuple[ResearchImportCandidate, ...] = ()
    remaining_budget: float | None = None


@dataclass(frozen=True, slots=True)
class ResearchImportVerifyInput:
    """One import reconciled against the notebook's sources under a budget."""

    notebook_id: str
    task_id: str
    candidates: tuple[ResearchImportCandidate, ...] = ()
    max_elapsed: float = 1800.0
    initial_delay: float = 5.0
    backoff_factor: float = 2.0
    max_delay: float = 60.0
    allow_duplicate: bool = False


@dataclass(frozen=True, slots=True)
class ResearchPresentSourceRecord:
    """One requested source the notebook already carried, skipped by #1961.

    Carries the EXISTING source that matched, not the requested entry: its
    ``url`` is what the match was made on, so a caller can report which of its
    requested URLs was recognised.
    """

    id: str
    title: str
    url: str


@dataclass(frozen=True, slots=True)
class ResearchImportVerifyResult:
    """Newly-imported sources plus the ones the idempotency pre-filter skipped.

    The two halves are disjoint by construction: a candidate is either imported
    by this call (or probe-confirmed as imported by an attempt of it) or was
    already present before it started.
    """

    imported: tuple[ResearchImportedSourceRecord, ...] = ()
    already_present: tuple[ResearchPresentSourceRecord, ...] = ()


RESEARCH_START_DEF: OperationDef[ResearchStartInput, ResearchStartResult] = OperationDef(
    Operation.RESEARCH_START,
    CallPolicy.STATEFUL_START,
    ResearchStartInput,
    ResearchStartResult,
)
RESEARCH_POLL_DEF: OperationDef[ResearchPollInput, ResearchPollResult] = OperationDef(
    Operation.RESEARCH_POLL,
    CallPolicy.READ,
    ResearchPollInput,
    ResearchPollResult,
)
RESEARCH_CANCEL_DEF: OperationDef[ResearchCancelInput, ResearchCancelResult] = OperationDef(
    Operation.RESEARCH_CANCEL,
    CallPolicy.MUTATION,
    ResearchCancelInput,
    ResearchCancelResult,
)
RESEARCH_IMPORT_DEF: OperationDef[ResearchImportInput, ResearchImportResult] = OperationDef(
    Operation.RESEARCH_IMPORT,
    CallPolicy.MUTATION,
    ResearchImportInput,
    ResearchImportResult,
)

__all__ = [
    "RESEARCH_CANCEL_DEF",
    "RESEARCH_IMPORT_DEF",
    "RESEARCH_POLL_DEF",
    "RESEARCH_START_DEF",
    "RESEARCH_TERMINAL_STATUSES",
    "ResearchCancelInput",
    "ResearchCancelResult",
    "ResearchImportBatchInput",
    "ResearchImportCandidate",
    "ResearchImportEntry",
    "ResearchImportEntryKind",
    "ResearchImportInput",
    "ResearchImportResult",
    "ResearchImportVerifyInput",
    "ResearchImportVerifyResult",
    "ResearchImportedSourceRecord",
    "ResearchMode",
    "ResearchPollInput",
    "ResearchPollResult",
    "ResearchPresentSourceRecord",
    "ResearchSearchSource",
    "ResearchSourceRecord",
    "ResearchStartInput",
    "ResearchStartResult",
    "ResearchTaskRecord",
    "ResearchTaskSelectionResult",
    "ResearchTaskStatus",
    "ResearchWaitInput",
]
