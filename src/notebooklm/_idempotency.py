"""Transport-neutral commit evidence and replay decisions."""

from __future__ import annotations

import asyncio
import traceback
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Literal, Protocol, TypeVar
from uuid import uuid4

from ._redact import redact
from .exceptions import (
    NetworkError,
    NotebookLMError,
    RateLimitError,
    RPCError,
    ServerError,
)
from .outcomes import (
    BatchOutcome,
    CommitState,
    OperationMetadata,
    ReconciliationCandidate,
    ReconciliationReport,
    RecoveryAction,
    _AttemptMetadata,
)

T = TypeVar("T")
_E = TypeVar("_E", bound=BaseException)


@dataclass(frozen=True)
class SendIdentity:
    """Value identity for one semantic send within a local invocation."""

    invocation_id: str
    operation: str
    method: str
    phase: str
    member: int | None = None


@dataclass
class AttemptRecord:
    """Mutable settlement record for one physical dispatch attempt."""

    ordinal: int
    commit_state: CommitState
    evidence: str | None = None
    known_resource_ids: tuple[str, ...] = ()


@dataclass
class JournalEntry:
    """Bound semantic send whose attempts aggregate conservatively."""

    identity: SendIdentity
    _journal: OperationJournal = field(repr=False, compare=False)
    _attempts: list[AttemptRecord] = field(default_factory=list, repr=False)
    _preflight_state: CommitState = CommitState.NOT_SENT
    _preflight_evidence: str | None = None
    _known_resource_ids: list[str] = field(default_factory=list, repr=False)
    recovery_action: RecoveryAction = RecoveryAction.NONE
    source_id: str | None = None
    stage: str | None = None
    reconciliation: ReconciliationReport | None = None
    batch_outcome: BatchOutcome | None = None
    prerequisite_ids: tuple[str, ...] = ()

    @property
    def attempts(self) -> tuple[AttemptRecord, ...]:
        return tuple(self._attempts)

    @property
    def known_resource_ids(self) -> tuple[str, ...]:
        return tuple(self._known_resource_ids)

    @property
    def commit_state(self) -> CommitState:
        states = tuple(attempt.commit_state for attempt in self._attempts)
        if CommitState.UNKNOWN in states:
            return CommitState.UNKNOWN
        if CommitState.CONFIRMED in states:
            return CommitState.CONFIRMED
        if CommitState.REJECTED in states:
            return CommitState.REJECTED
        return self._preflight_state

    def mark_dispatched(self) -> AttemptRecord:
        return self._journal.mark_dispatched(self)

    def record(
        self,
        state: CommitState,
        evidence: str,
        *,
        attempt: AttemptRecord | None = None,
        known_resource_ids: tuple[str, ...] = (),
    ) -> None:
        self._journal.record(
            self,
            state,
            evidence,
            attempt=attempt,
            known_resource_ids=known_resource_ids,
        )

    def remember_resource_ids(self, *resource_ids: str) -> None:
        """Retain already-known handles without settling an attempt."""

        self._journal.remember_resource_ids(self, resource_ids)

    def snapshot(self) -> OperationMetadata:
        return self._journal.snapshot(self)


class OperationJournal:
    """Private journal of mutation evidence for one logical workflow."""

    def __init__(self, operation: str) -> None:
        self.operation = redact(operation, max_length=200)
        self._entries: dict[SendIdentity, JournalEntry] = {}

    @staticmethod
    def invocation_id() -> str:
        return uuid4().hex

    @property
    def entries(self) -> tuple[JournalEntry, ...]:
        return tuple(self._entries.values())

    def entry(self, identity: SendIdentity) -> JournalEntry:
        if identity.operation != self.operation:
            raise ValueError("send identity belongs to a different operation")
        return self._entries.setdefault(identity, JournalEntry(identity, self))

    def new_entry(
        self,
        *,
        method: str,
        phase: str = "mutation",
        member: int | None = None,
        invocation_id: str | None = None,
    ) -> JournalEntry:
        return self.entry(
            SendIdentity(
                invocation_id or self.invocation_id(),
                self.operation,
                redact(method, max_length=200),
                redact(phase, max_length=200),
                member,
            )
        )

    def mark_dispatched(self, entry: JournalEntry) -> AttemptRecord:
        self._check_entry(entry)
        attempt = AttemptRecord(len(entry._attempts) + 1, CommitState.UNKNOWN, "dispatched")
        entry._attempts.append(attempt)
        return attempt

    def remember_resource_ids(
        self,
        entry: JournalEntry,
        resource_ids: tuple[str, ...],
    ) -> None:
        self._check_entry(entry)
        for resource_id in resource_ids:
            safe_id = redact(resource_id, max_length=200)
            if safe_id and safe_id not in entry._known_resource_ids:
                entry._known_resource_ids.append(safe_id)

    def record(
        self,
        entry: JournalEntry,
        state: CommitState,
        evidence: str,
        *,
        attempt: AttemptRecord | None = None,
        known_resource_ids: tuple[str, ...] = (),
    ) -> None:
        self._check_entry(entry)
        for resource_id in known_resource_ids:
            safe_id = redact(resource_id, max_length=200)
            if safe_id and safe_id not in entry._known_resource_ids:
                entry._known_resource_ids.append(safe_id)
        if attempt is None and entry._attempts:
            attempt = entry._attempts[-1]
        if attempt is None:
            if state is not CommitState.NOT_SENT:
                raise ValueError("dispatch must be recorded before mutation settlement")
            entry._preflight_state = state
            entry._preflight_evidence = redact(evidence, max_length=200)
            return
        if attempt not in entry._attempts:
            raise ValueError("attempt does not belong to journal entry")
        if attempt.commit_state is not CommitState.UNKNOWN and attempt.commit_state is not state:
            raise ValueError("a settled attempt cannot be overwritten")
        attempt.commit_state = state
        attempt.evidence = redact(evidence, max_length=200)
        attempt.known_resource_ids = tuple(
            dict.fromkeys(
                (
                    *attempt.known_resource_ids,
                    *(redact(item, max_length=200) for item in known_resource_ids if item),
                )
            )
        )

    def snapshot(
        self,
        entry: JournalEntry | None = None,
        *,
        primary: JournalEntry | None = None,
        extra_entries: tuple[JournalEntry, ...] = (),
    ) -> OperationMetadata:
        """Freeze one entry or an aggregate of every semantic workflow send."""

        if entry is not None:
            self._check_entry(entry)
            return self._entry_snapshot(entry)
        unique_entries: list[JournalEntry] = []
        for candidate in (*self.entries, *extra_entries):
            if not any(candidate is existing for existing in unique_entries):
                unique_entries.append(candidate)
        all_entries = tuple(unique_entries)
        if not all_entries:
            return OperationMetadata(operation=self.operation)
        if primary is not None and not any(primary is item for item in all_entries):
            raise ValueError("primary entry does not belong to the workflow snapshot")
        selected = primary or all_entries[0]
        leaves = tuple(item._journal._entry_snapshot(item) for item in all_entries)
        mutation_leaves = (
            tuple(
                leaf
                for leaf in leaves
                if leaf.phase not in {"baseline", "readback", "observation", "cleanup", "wait"}
            )
            or leaves
        )
        states = tuple(leaf.commit_state for leaf in mutation_leaves)
        state = (
            CommitState.UNKNOWN
            if CommitState.UNKNOWN in states
            else CommitState.CONFIRMED
            if CommitState.CONFIRMED in states
            else CommitState.REJECTED
            if CommitState.REJECTED in states
            else CommitState.NOT_SENT
        )
        selected_leaf = selected._journal._entry_snapshot(selected)
        return replace(
            selected_leaf,
            commit_state=state,
            known_resource_ids=tuple(
                dict.fromkeys(
                    resource_id for leaf in leaves for resource_id in leaf.known_resource_ids
                )
            ),
            attempts=tuple(attempt for leaf in leaves for attempt in leaf.attempts),
            prerequisite_ids=tuple(
                dict.fromkeys(
                    resource_id for leaf in leaves for resource_id in leaf.prerequisite_ids
                )
            ),
            entries=leaves,
        )

    def _entry_snapshot(self, entry: JournalEntry) -> OperationMetadata:
        identity = entry.identity
        return OperationMetadata(
            commit_state=entry.commit_state,
            operation=identity.operation,
            invocation_id=identity.invocation_id,
            method=identity.method,
            phase=identity.phase,
            member=identity.member,
            known_resource_ids=entry.known_resource_ids,
            recovery_action=entry.recovery_action,
            source_id=entry.source_id,
            stage=entry.stage,
            reconciliation=entry.reconciliation,
            batch_outcome=entry.batch_outcome,
            attempts=tuple(
                _AttemptMetadata(
                    ordinal=item.ordinal,
                    commit_state=item.commit_state,
                    evidence=item.evidence,
                    known_resource_ids=item.known_resource_ids,
                )
                for item in entry._attempts
            ),
            prerequisite_ids=entry.prerequisite_ids,
        )

    def _check_entry(self, entry: JournalEntry) -> None:
        if entry._journal is not self or self._entries.get(entry.identity) is not entry:
            raise ValueError("journal entry is not bound to this journal")


def attach_operation_metadata(exc: _E, metadata: OperationMetadata) -> _E:
    """Attach one immutable canonical carrier to a public exception."""

    exc._operation_metadata = metadata  # type: ignore[attr-defined]
    # ``operation`` existed as a temporary P1 projection. Keep it readable
    # through the migration without making it another metadata authority.
    if isinstance(exc, NotebookLMError) and metadata.operation is not None:
        exc.operation = metadata.operation  # type: ignore[attr-defined]
    if isinstance(exc, NotebookLMError) and metadata.reconciliation is not None:
        exc.reconciliation_candidates = tuple(  # type: ignore[attr-defined]
            candidate.id for candidate in metadata.reconciliation.candidates
        )
        exc.unresolved_inputs = (  # type: ignore[attr-defined]
            metadata.reconciliation.unresolved_inputs
        )
    return exc


def attach_journal_entry(
    exc: _E,
    entry: JournalEntry,
    *,
    recovery_action: RecoveryAction | None = None,
    workflow: bool = False,
) -> _E:
    """Attach the authoritative snapshot of a bound semantic send."""

    existing = getattr(exc, "operation_metadata", None)
    if existing is not None:
        entry.remember_resource_ids(*existing.known_resource_ids)
        entry.source_id = entry.source_id or existing.source_id
        entry.stage = entry.stage or existing.stage
        entry.reconciliation = entry.reconciliation or existing.reconciliation
        entry.batch_outcome = entry.batch_outcome or existing.batch_outcome
        entry.prerequisite_ids = tuple(
            dict.fromkeys((*entry.prerequisite_ids, *existing.prerequisite_ids))
        )
        if entry.recovery_action is RecoveryAction.NONE:
            entry.recovery_action = existing.recovery_action
    if recovery_action is not None:
        entry.recovery_action = recovery_action
    metadata = entry._journal.snapshot(primary=entry) if workflow else entry.snapshot()
    return attach_operation_metadata(exc, metadata)


def attach_operation_journal(
    exc: _E,
    journal: OperationJournal,
    *,
    primary: JournalEntry | None = None,
    recovery_action: RecoveryAction | None = None,
    extra_entries: tuple[JournalEntry, ...] = (),
) -> _E:
    """Attach an immutable workflow-wide aggregate while preserving every send."""

    existing = getattr(exc, "operation_metadata", None)
    if existing is not None and primary is not None:
        primary.remember_resource_ids(*existing.known_resource_ids)
        primary.source_id = primary.source_id or existing.source_id
        primary.stage = primary.stage or existing.stage
        primary.reconciliation = primary.reconciliation or existing.reconciliation
        primary.batch_outcome = primary.batch_outcome or existing.batch_outcome
        primary.prerequisite_ids = tuple(
            dict.fromkeys((*primary.prerequisite_ids, *existing.prerequisite_ids))
        )
        if primary.recovery_action is RecoveryAction.NONE:
            primary.recovery_action = existing.recovery_action
    metadata = journal.snapshot(primary=primary, extra_entries=extra_entries)
    if recovery_action is not None:
        metadata = replace(metadata, recovery_action=recovery_action)
    return attach_operation_metadata(exc, metadata)


def reconciliation_report(
    candidate_ids: tuple[str, ...] | list[str],
    unresolved_inputs: tuple[str, ...] | list[str],
    *,
    reason: str = "outcome could not be correlated",
) -> ReconciliationReport:
    """Build the bounded, redaction-safe report used by migrated producers."""

    return ReconciliationReport(
        candidates=tuple(
            ReconciliationCandidate(str(candidate)[:200]) for candidate in candidate_ids[:20]
        ),
        unresolved_inputs=tuple(str(item)[:200] for item in unresolved_inputs[:20]),
        reason=reason[:200],
    )


@dataclass
class GenerationRetryBinding:
    """Private helper-owned generation journal retained across retry sleeps."""

    owner_task: asyncio.Task[object]
    journal: OperationJournal
    entries: list[JournalEntry] = field(default_factory=list)
    linked_entries: list[JournalEntry] = field(default_factory=list)
    ancestors: tuple[GenerationRetryBinding, ...] = ()
    semantic_key: str | None = None
    replay_disabled: bool = False


_GENERATION_BINDINGS: ContextVar[tuple[GenerationRetryBinding, ...]] = ContextVar(
    "notebooklm_generation_retry_bindings", default=()
)


def new_generation_retry_binding() -> GenerationRetryBinding:
    """Create a helper binding and invalidate any inherited ancestor replay."""

    task = asyncio.current_task()
    if task is None:  # pragma: no cover - async helper invariant
        raise RuntimeError("generation retry binding requires an asyncio task")
    ancestors = _GENERATION_BINDINGS.get()
    for ancestor in ancestors:
        ancestor.replay_disabled = True
    return GenerationRetryBinding(
        owner_task=task,
        journal=OperationJournal("artifacts.generate"),
        ancestors=ancestors,
    )


@contextmanager
def activate_generation_retry_binding(
    binding: GenerationRetryBinding,
) -> Iterator[GenerationRetryBinding]:
    """Expose the helper binding only while its generation callable runs."""

    stack = _GENERATION_BINDINGS.get()
    token: Token[tuple[GenerationRetryBinding, ...]] = _GENERATION_BINDINGS.set((*stack, binding))
    try:
        yield binding
    finally:
        _GENERATION_BINDINGS.reset(token)


def claim_generation_entry(*, method: str, semantic_key: str) -> JournalEntry:
    """Claim or allocate the semantic send used by one backend generation."""

    task = asyncio.current_task()
    stack = _GENERATION_BINDINGS.get()
    binding = stack[-1] if stack and stack[-1].owner_task is task else None
    if binding is None:
        journal = OperationJournal("artifacts.generate")
        return journal.new_entry(method=method)
    if binding.semantic_key is None:
        binding.semantic_key = semantic_key
        entry = binding.journal.new_entry(method=method)
        binding.entries.append(entry)
        for ancestor in binding.ancestors:
            ancestor.linked_entries.append(entry)
        return entry
    if binding.semantic_key == semantic_key and binding.entries:
        return binding.entries[0]
    binding.replay_disabled = True
    entry = binding.journal.new_entry(method=method)
    binding.entries.append(entry)
    for ancestor in binding.ancestors:
        ancestor.linked_entries.append(entry)
    return entry


def settle_generation_failure(
    binding: GenerationRetryBinding,
    exc: _E,
) -> _E:
    """Attach helper-owned evidence and prevent retries after any uncertain send."""

    if not binding.entries and not binding.linked_entries:
        return exc
    entry = (binding.entries or binding.linked_entries)[0]
    has_confirmed_descendant = any(
        item.commit_state is CommitState.CONFIRMED
        for item in (*binding.entries, *binding.linked_entries)
    )
    attach_operation_journal(
        exc,
        binding.journal,
        primary=entry,
        recovery_action=(
            RecoveryAction.INSPECT_AND_RECONCILE if has_confirmed_descendant else None
        ),
        extra_entries=tuple(binding.linked_entries),
    )
    if any(
        item.commit_state in (CommitState.CONFIRMED, CommitState.UNKNOWN)
        for item in (*binding.entries, *binding.linked_entries)
    ):
        binding.replay_disabled = True
    return exc


class ReplayGrant(str, Enum):
    """Private semantic permission supplied by the operation owner."""

    REFUSAL_RETRY_AUTHORIZED = "refusal_retry_authorized"
    NO_REPLAY = "no_replay"
    REPLAY_SAFE = "replay_safe"


def replay_allowed(
    exc: BaseException | None,
    *,
    grant: ReplayGrant,
    disabled: bool,
    remaining: float | None,
) -> bool:
    """Return whether canonical evidence and operation semantics permit replay."""
    if disabled or (remaining is not None and remaining <= 0):
        return False
    if grant is ReplayGrant.NO_REPLAY:
        return False
    if grant is ReplayGrant.REPLAY_SAFE:
        return True
    state = getattr(exc, "commit_state", CommitState.UNKNOWN)
    return state in (CommitState.REJECTED, CommitState.NOT_SENT)


# The translated exception types that ``rpc_call`` raises when the
# request fails in a way that *might* have committed the write on the
# server. With ``disable_internal_retries=True``, the middleware retry loop
# inside ``RuntimeTransport.perform_authed_post`` does not replay these;
# instead ``rpc_call`` translates the underlying ``TransportServerError`` /
# network failure into ``ServerError`` / ``NetworkError`` / ``RateLimitError``
# and surfaces it here. Anything else (auth, validation, decoding) propagates
# unchanged unless a producer has attached more precise evidence.
#
# Note: ``RPCTimeoutError`` inherits from ``NetworkError`` so it is
# already covered by the ``NetworkError`` catch.
_RETRYABLE_TRANSPORT_ERRORS: tuple[type[BaseException], ...] = (
    RateLimitError,
    ServerError,
    NetworkError,
)

AMBIGUOUS_WRITE_ERRORS = _RETRYABLE_TRANSPORT_ERRORS


def mark_commit_state(
    exc: _E,
    state: CommitState,
    *,
    operation: str | None = None,
    source_id: str | None = None,
    stage: str | None = None,
    recovery_action: RecoveryAction = RecoveryAction.NONE,
) -> _E:
    """Attach positive commit evidence without overwriting earlier evidence."""
    metadata = getattr(exc, "operation_metadata", None)
    current = None if metadata is None else metadata.commit_state
    carrier = metadata or OperationMetadata()
    return attach_operation_metadata(
        exc,
        replace(
            carrier,
            commit_state=(state if current is None else current),
            operation=carrier.operation or operation,
            source_id=carrier.source_id or source_id,
            stage=carrier.stage or stage,
            recovery_action=(recovery_action if current is None else carrier.recovery_action),
        ),
    )


def mark_unconfirmed(
    exc: _E,
    *,
    force_unknown: bool = False,
    operation: str | None = None,
    source_id: str | None = None,
    stage: str | None = None,
    recovery_action: RecoveryAction = RecoveryAction.INSPECT_AND_RECONCILE,
) -> _E:
    """Tag an error as *"the write may have committed and we cannot confirm it"*.

    Raised by a probe that could not answer (#2220). This is a genuinely
    distinct outcome from both "the create was rejected" and "the create
    failed", and consumers must be able to tell it apart **programmatically** —
    the two mistakes it prevents are concrete:

    "Could not answer" covers every way a probe fails to settle the question,
    not just an exception while listing. All of these carry the marker:

    * the probe's list raised — a decode failure (wrapped) or a transport /
      auth failure (re-raised unchanged, marker set on the original);
    * the probe listed fine but found a match it **cannot attribute**, because
      the pre-create baseline was unavailable;
    * the probe found **several** new matches and cannot choose;
    * a create RPC returned success but with no trustworthy id, and the
      recovery probe then failed or found nothing unambiguous.

    The last three are the easy ones to miss: nothing threw, so they look like
    ordinary rejections — but the server may hold a row either way, which is
    exactly the state this marker names.

    * ``_app.errors`` classifies a :class:`SourceAddError` by inspecting its
      ``cause``, and a bare ``RPCError`` cause carrying a 5xx / gRPC-14
      ``rpc_code`` maps to :attr:`~notebooklm._app.errors.ErrorCategory.SERVER`
      — *retriable*, hint "retry after a short delay". A probe's own decode
      failure can carry exactly such a code, which would advertise "please
      retry" for the one error whose entire message says the create must not be
      retried. That is the duplicate this whole change prevents, re-introduced
      one layer up.
    * A batch add isolates non-fatal per-item errors and continues. An
      unconfirmed create must instead stop the batch, or a drifted backend turns
      one unconfirmed write into one per item.

    Read it back with ``getattr(exc, "unconfirmed", False)`` — a plain literal
    at the call site, matching how ``source_id`` / ``stage`` are read after
    ``raise_partial_upload_failure`` (#2179). A shared constant was tried and
    rejected: it belongs on the public exception surface for ``_app`` to import
    (the ``_app`` boundary guardrail forbids reaching into private runtime
    siblings), and putting it there pushed ``exceptions.py`` past its
    module-size ratchet for a single string.

    Set as an attribute on the real exception rather than introducing a wrapper
    or sibling type — the same shape ``raise_partial_upload_failure`` uses for
    ``source_id`` / ``stage``, and for the same reason (#2179): a new type in the
    hierarchy silently changes which ``except`` clauses match at existing call
    sites. Every ``except SourceAddError`` / ``except RPCError`` keeps matching
    exactly as before; only code that asks for the marker sees a difference.
    """
    metadata = getattr(exc, "operation_metadata", None)
    current = None if metadata is None else metadata.commit_state
    if not force_unknown and current in (
        CommitState.NOT_SENT,
        CommitState.REJECTED,
        CommitState.CONFIRMED,
    ):
        assert metadata is not None
        return attach_operation_metadata(
            exc,
            replace(
                metadata,
                operation=metadata.operation or operation,
                source_id=metadata.source_id or source_id,
                stage=metadata.stage or stage,
            ),
        )
    return attach_operation_metadata(
        exc,
        replace(
            metadata or OperationMetadata(),
            commit_state=CommitState.UNKNOWN,
            operation=operation or (None if metadata is None else metadata.operation),
            source_id=source_id or (None if metadata is None else metadata.source_id),
            stage=stage or (None if metadata is None else metadata.stage),
            recovery_action=recovery_action,
        ),
    )


def attach_reconciliation_report(
    exc: _E,
    report: ReconciliationReport,
    *,
    operation: str | None = None,
    commit_state: CommitState = CommitState.UNKNOWN,
    recovery_action: RecoveryAction = RecoveryAction.INSPECT_AND_RECONCILE,
) -> _E:
    """Attach typed candidate evidence without promoting it to a known ID."""

    metadata = (
        getattr(exc, "operation_metadata", None)
        or getattr(exc, "_operation_metadata", None)
        or OperationMetadata()
    )
    return attach_operation_metadata(
        exc,
        replace(
            metadata,
            commit_state=commit_state,
            operation=operation or metadata.operation,
            recovery_action=recovery_action,
            reconciliation=report,
        ),
    )


def attach_batch_outcome(
    exc: _E,
    outcome: BatchOutcome,
    *,
    preserve_commit_state: bool = False,
) -> _E:
    """Retain ordered batch settlement on the original escaping exception."""

    metadata = (
        getattr(exc, "operation_metadata", None)
        or getattr(exc, "_operation_metadata", None)
        or OperationMetadata()
    )
    states = tuple(item.commit_state for item in outcome.items)
    state = (
        CommitState.UNKNOWN
        if CommitState.UNKNOWN in states
        else CommitState.CONFIRMED
        if CommitState.CONFIRMED in states
        else CommitState.REJECTED
        if CommitState.REJECTED in states
        else CommitState.NOT_SENT
    )
    recovery_action = (
        RecoveryAction.INSPECT_AND_RECONCILE
        if state is CommitState.UNKNOWN
        else RecoveryAction.NONE
        if metadata.commit_state is CommitState.UNKNOWN and not preserve_commit_state
        else metadata.recovery_action
    )
    return attach_operation_metadata(
        exc,
        replace(
            metadata,
            commit_state=(metadata.commit_state if preserve_commit_state else state),
            recovery_action=recovery_action,
            batch_outcome=outcome,
        ),
    )


def attach_prerequisite_ids(exc: _E, *resource_ids: str) -> _E:
    """Retain prerequisite recovery handles on a public error carrier."""

    metadata = getattr(exc, "operation_metadata", None) or OperationMetadata()
    return attach_operation_metadata(
        exc,
        replace(
            metadata,
            prerequisite_ids=tuple(
                dict.fromkeys(
                    (*metadata.prerequisite_ids, *(item for item in resource_ids if item))
                )
            ),
        ),
    )


class _MethodIdentifier(Protocol):
    """Structural method identity shared by web enums and Android strings."""

    @property
    def value(self) -> str: ...


_Method = _MethodIdentifier | str


def _method_id(method: _Method) -> str:
    # ``RPCMethod`` is also a ``str`` subclass. Resolve its enum value before
    # the generic string case so exception metadata never retains an enum
    # instance where callers expect a built-in ``str``.
    value = getattr(method, "value", None)
    return str(value) if isinstance(value, str) else str(method)


def unresolved_commit_error(
    method: _Method,
    what: str,
    exc: _E,
    *,
    preserve_exception: bool = False,
    force_unknown: bool = False,
    operation: str | None = None,
) -> _E | RPCError:
    """Build or tag an error for a write whose commit outcome is unknown.

    ``preserve_exception=True`` explicitly preserves an already-rendered
    domain-specific exception type and guidance. Transport exceptions receive
    the shared generic ``RPCError`` used by web call sites that do not have a
    more specific domain wrapper. Exception text is deliberately not used to
    select between those contracts: upstream transport messages are untrusted.
    """

    if preserve_exception:
        return mark_unconfirmed(exc, force_unknown=force_unknown, operation=operation)

    rpc_code = exc.rpc_code if isinstance(exc, RPCError) else None
    return mark_unconfirmed(
        RPCError(
            f"UNRESOLVED — {what} may have committed before its response was lost. "
            "Do not blindly retry; list the notebook's sources and reconcile first. "
            f"No automatic retry was attempted. {exc}",
            method_id=_method_id(method),
            rpc_code=rpc_code,
        ),
        force_unknown=force_unknown,
        operation=operation,
    )


async def call_unconfirmed_on_transport_loss(
    call: Callable[[], Awaitable[T]],
    *,
    method: _Method,
    what: str,
    chain: Literal["exc"] | None = "exc",
    force_unknown: bool = False,
    operation: str | None = None,
    journal_entry: JournalEntry | None = None,
) -> T:
    """Run one non-replayed write and mark transport-loss ambiguity.

    The original exception object, class, and message are preserved. ``method``
    and ``what`` make the write identity explicit at every call site and are
    consumed by guardrails. Web callers retain normal exception context;
    Android callers pass ``chain=None`` so bearer-owning transport frames stay
    outside the escaping exception chain.
    """

    if chain not in ("exc", None):
        raise ValueError("chain must be 'exc' or None")
    failure: BaseException | None = None
    try:
        return await call()
    except AMBIGUOUS_WRITE_ERRORS as exc:
        if journal_entry is not None:
            attach_journal_entry(
                exc,
                journal_entry,
                recovery_action=(
                    RecoveryAction.INSPECT_AND_RECONCILE
                    if journal_entry.commit_state is CommitState.UNKNOWN
                    else None
                ),
            )
        else:
            mark_unconfirmed(exc, force_unknown=force_unknown, operation=operation)
        if chain == "exc":
            del call, method, what
            raise
        failure = exc
    except RPCError as exc:
        if not force_unknown:
            raise
        if journal_entry is not None:
            attach_journal_entry(
                exc,
                journal_entry,
                recovery_action=(
                    RecoveryAction.INSPECT_AND_RECONCILE
                    if journal_entry.commit_state is CommitState.UNKNOWN
                    else None
                ),
            )
        else:
            mark_unconfirmed(exc, force_unknown=True, operation=operation)
        if chain == "exc":
            del call, method, what
            raise
        failure = exc

    assert failure is not None
    captured = failure.__traceback__
    failure.__traceback__ = None
    failure.__cause__ = None
    failure.__context__ = None
    failure.__suppress_context__ = True
    completed = captured
    while (
        completed is not None
        and completed.tb_frame.f_code is call_unconfirmed_on_transport_loss.__code__
    ):
        completed = completed.tb_next
    if completed is not None:
        traceback.clear_frames(completed)
    del call, method, what
    del captured, completed
    raise failure from None


__all__ = [
    "AMBIGUOUS_WRITE_ERRORS",
    "AttemptRecord",
    "GenerationRetryBinding",
    "JournalEntry",
    "OperationJournal",
    "ReplayGrant",
    "SendIdentity",
    "activate_generation_retry_binding",
    "attach_batch_outcome",
    "attach_journal_entry",
    "attach_operation_metadata",
    "attach_operation_journal",
    "attach_prerequisite_ids",
    "attach_reconciliation_report",
    "call_unconfirmed_on_transport_loss",
    "claim_generation_entry",
    "mark_commit_state",
    "mark_unconfirmed",
    "new_generation_retry_binding",
    "reconciliation_report",
    "replay_allowed",
    "settle_generation_failure",
    "unresolved_commit_error",
]
