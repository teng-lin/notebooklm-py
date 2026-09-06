"""Android owner for the two-phase positional URL batch workflow."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Sequence
from typing import Any, Protocol, TypeVar, cast

from .._idempotency import (
    JournalEntry,
    OperationJournal,
    attach_batch_outcome,
    attach_journal_entry,
    attach_operation_journal,
    bind_operation_journal_entries,
    reconciliation_report,
)
from .._source.batch import SourceUrlBatchItem, validate_source_batch_occurrences
from ..exceptions import (
    AuthError,
    DecodingError,
    NetworkError,
    RateLimitError,
    ServerError,
    SourceAddError,
)
from ..outcomes import BatchItemOutcome, BatchOutcome, CommitState, RecoveryAction
from ..types import Source
from .source_transfers import (
    ADD_SOURCES_METHOD,
    ADD_TENTATIVE_SOURCES_METHOD,
    correlation_name,
    known_registration_error,
    unresolved_add_error,
)

_ReadbackT = TypeVar("_ReadbackT")


class _BatchOwner(Protocol):
    async def _register_tentative_sources(
        self,
        notebook_id: str,
        names: Sequence[str],
        *,
        expected_epoch: int,
    ) -> Sequence[Any]: ...

    async def _commit_urls(
        self,
        notebook_id: str,
        entries: Sequence[tuple[str, str]],
        *,
        expected_epoch: int,
    ) -> tuple[dict[str, Any], Any]: ...


def record_commit_proofs(
    entries: tuple[JournalEntry, ...] | None,
    source_ids: Sequence[str],
    proofs: dict[str, Any],
    evidence: str = "decoded source commit response",
) -> None:
    """Settle exact decoded commit proofs before any later readback await."""

    if entries is None:
        return
    for entry, source_id in zip(entries, source_ids, strict=True):
        if source_id in proofs and entry.commit_state is not CommitState.CONFIRMED:
            entry.record(
                CommitState.CONFIRMED,
                evidence,
                known_resource_ids=(source_id,),
            )


async def preserve_readback(
    awaitable: Awaitable[_ReadbackT],
    entries: tuple[JournalEntry, ...] | None,
) -> _ReadbackT:
    """Attach the complete decoded-send journal if later readback is cancelled."""

    try:
        return await awaitable
    except asyncio.CancelledError as error:
        if entries:
            primary = next(iter(entries))
            attach_journal_entry(
                error,
                primary,
                recovery_action=(
                    RecoveryAction.INSPECT_AND_RECONCILE
                    if any(entry.commit_state is CommitState.UNKNOWN for entry in entries)
                    else RecoveryAction.NONE
                ),
                workflow=True,
            )
        raise


def _batch_outcome(
    urls: Sequence[str],
    registration_entries: Sequence[JournalEntry],
    commit_entries: Sequence[JournalEntry],
    *,
    errors: Sequence[BaseException | None] | None = None,
) -> BatchOutcome:
    item_errors = errors or (None,) * len(urls)
    items: list[BatchItemOutcome] = []
    for index, (url, registration, commit, error) in enumerate(
        zip(urls, registration_entries, commit_entries, item_errors, strict=True)
    ):
        state = (
            registration.commit_state
            if registration.commit_state is not CommitState.CONFIRMED
            else commit.commit_state
        )
        known_ids = tuple(
            dict.fromkeys((*registration.known_resource_ids, *commit.known_resource_ids))
        )
        report = None
        if state is CommitState.UNKNOWN:
            report = reconciliation_report(
                list(known_ids),
                [url],
                reason="Android batch member commit could not be correlated",
            )
            selected = registration if registration.commit_state is state else commit
            selected.reconciliation = report
            selected.recovery_action = RecoveryAction.INSPECT_AND_RECONCILE
        known_id = next(iter(known_ids), None)
        items.append(
            BatchItemOutcome(
                member=index,
                input=url,
                commit_state=state,
                resource_id=(
                    known_id
                    if known_id is not None
                    and state in (CommitState.CONFIRMED, CommitState.UNKNOWN)
                    else None
                ),
                error=None if state is CommitState.CONFIRMED else error,
                reconciliation=report,
            )
        )
    return BatchOutcome(tuple(items), whole_request_retriable=False)


def _attach_failure(
    error: BaseException,
    journal: OperationJournal,
    registration_entries: tuple[JournalEntry, ...],
    commit_entries: tuple[JournalEntry, ...],
    urls: Sequence[str],
    *,
    primary: JournalEntry,
) -> None:
    outcome = _batch_outcome(
        urls,
        registration_entries,
        commit_entries,
        errors=(error,) * len(urls),
    )
    recovery = (
        RecoveryAction.RETRY
        if all(item.commit_state is CommitState.NOT_SENT for item in outcome.items)
        else RecoveryAction.INSPECT_AND_RECONCILE
        if any(item.commit_state is CommitState.UNKNOWN for item in outcome.items)
        else None
    )
    if recovery is RecoveryAction.RETRY:
        primary.recovery_action = recovery
    attach_operation_journal(
        error,
        journal,
        primary=primary,
        recovery_action=recovery,
    )
    attach_batch_outcome(error, outcome)


class AndroidSourceBatchMixin:
    """Two-phase Android URL batch owner with one journal per occurrence."""

    _transport: Any

    async def _add_urls_batch(
        self,
        notebook_id: str,
        urls: list[str],
    ) -> list[SourceUrlBatchItem]:
        snapshot = tuple(urls)
        validate_source_batch_occurrences(snapshot)
        if not snapshot:
            return []
        correlations = [correlation_name() for _ in snapshot]
        journal = OperationJournal("sources.add_urls")
        invocation_id = journal.invocation_id()
        registration_entries = tuple(
            journal.new_entry(
                method=ADD_TENTATIVE_SOURCES_METHOD,
                phase="registration",
                member=index,
                invocation_id=invocation_id,
            )
            for index in range(len(snapshot))
        )
        commit_entries = tuple(
            journal.new_entry(
                method=ADD_SOURCES_METHOD,
                member=index,
                invocation_id=invocation_id,
            )
            for index in range(len(snapshot))
        )
        owner = cast(_BatchOwner, self)

        async with self._transport.operation_scope("source.add_urls_batch") as lease:
            try:
                with bind_operation_journal_entries(*registration_entries):
                    registrations = await owner._register_tentative_sources(
                        notebook_id,
                        correlations,
                        expected_epoch=lease.epoch,
                    )
            except (asyncio.CancelledError, KeyboardInterrupt, SystemExit, AuthError) as exc:
                primary = next(iter(registration_entries))
                _attach_failure(
                    exc,
                    journal,
                    registration_entries,
                    commit_entries,
                    snapshot,
                    primary=primary,
                )
                raise
            except (RateLimitError, ServerError, NetworkError, DecodingError) as exc:
                errors: list[SourceAddError] = []
                for url, entry in zip(snapshot, registration_entries, strict=True):
                    entry.stage = "register"
                    errors.append(
                        SourceAddError(
                            url,
                            cause=exc,
                            message=(
                                f"Failed to add URL source {url!r}: tentative registration "
                                "stopped before dispatch."
                            ),
                        )
                        if entry.commit_state is CommitState.NOT_SENT
                        else unresolved_add_error(
                            url,
                            stage="tentative registration",
                            cause=exc,
                        )
                    )
                batch = _batch_outcome(
                    snapshot,
                    registration_entries,
                    commit_entries,
                    errors=errors,
                )
                outcomes: list[SourceUrlBatchItem] = []
                for url, entry, error, item in zip(
                    snapshot, registration_entries, errors, batch.items, strict=True
                ):
                    recovery = (
                        RecoveryAction.RETRY
                        if item.commit_state is CommitState.NOT_SENT
                        else RecoveryAction.INSPECT_AND_RECONCILE
                    )
                    attach_journal_entry(error, entry, recovery_action=recovery)
                    attach_batch_outcome(error, batch, preserve_commit_state=True)
                    outcomes.append(
                        SourceUrlBatchItem(
                            url=url,
                            error=error,
                            member=len(outcomes),
                            outcome=item,
                        )
                    )
                return outcomes
            except BaseException as exc:
                primary = next(iter(registration_entries))
                _attach_failure(
                    exc,
                    journal,
                    registration_entries,
                    commit_entries,
                    snapshot,
                    primary=primary,
                )
                raise

            indexed_entries = [
                (index, url, registration.source_id)
                for index, (url, registration) in enumerate(
                    zip(snapshot, registrations, strict=True)
                )
                if registration.source_id is not None and not registration.ambiguous
            ]
            for index, _, registered_id in indexed_entries:
                commit_entries[index].remember_resource_ids(registered_id)
            entries = [(url, source_id) for _, url, source_id in indexed_entries]
            proofs: dict[str, Any] = {}
            if entries:
                try:
                    with bind_operation_journal_entries(
                        *(commit_entries[index] for index, _, _ in indexed_entries)
                    ):
                        proofs, _ = await owner._commit_urls(
                            notebook_id,
                            cast(Sequence[tuple[str, str]], entries),
                            expected_epoch=lease.epoch,
                        )
                except BaseException as exc:
                    first_index, _, _ = next(iter(indexed_entries))
                    _attach_failure(
                        exc,
                        journal,
                        registration_entries,
                        commit_entries,
                        snapshot,
                        primary=commit_entries[first_index],
                    )
                    raise

            outcome_errors: list[SourceAddError | None] = []
            sources: list[Source | None] = []
            selected_entries: list[JournalEntry] = []
            for index, (url, registration) in enumerate(zip(snapshot, registrations, strict=True)):
                if registration.omitted:
                    outcome_errors.append(known_registration_error(url))
                    sources.append(None)
                    selected_entries.append(registration_entries[index])
                    continue
                source_id = registration.source_id
                proof = proofs.get(source_id or "")
                if registration.ambiguous or source_id is None or proof is None:
                    stage = (
                        "tentative registration correlation"
                        if registration.ambiguous or source_id is None
                        else "source commit acceptance"
                    )
                    outcome_errors.append(unresolved_add_error(url, stage=stage))
                    sources.append(None)
                    selected_entries.append(
                        registration_entries[index]
                        if registration.ambiguous or source_id is None
                        else commit_entries[index]
                    )
                else:
                    outcome_errors.append(None)
                    sources.append(proof.source)
                    selected_entries.append(commit_entries[index])

            batch = _batch_outcome(
                snapshot,
                registration_entries,
                commit_entries,
                errors=outcome_errors,
            )
            outcomes = []
            for index, (url, source, outcome_error, selected_entry, item) in enumerate(
                zip(
                    snapshot,
                    sources,
                    outcome_errors,
                    selected_entries,
                    batch.items,
                    strict=True,
                )
            ):
                if outcome_error is not None:
                    item_recovery = (
                        RecoveryAction.RETRY
                        if item.commit_state is CommitState.NOT_SENT
                        else RecoveryAction.INSPECT_AND_RECONCILE
                        if item.commit_state is CommitState.UNKNOWN
                        else None
                    )
                    attach_journal_entry(
                        outcome_error,
                        selected_entry,
                        recovery_action=item_recovery,
                    )
                    attach_batch_outcome(outcome_error, batch, preserve_commit_state=True)
                    outcomes.append(
                        SourceUrlBatchItem(
                            url=url,
                            error=outcome_error,
                            member=index,
                            outcome=item,
                        )
                    )
                else:
                    assert source is not None
                    outcomes.append(
                        SourceUrlBatchItem(url=url, source=source, member=index, outcome=item)
                    )
            return outcomes


__all__ = [
    "AndroidSourceBatchMixin",
    "preserve_readback",
    "record_commit_proofs",
]
