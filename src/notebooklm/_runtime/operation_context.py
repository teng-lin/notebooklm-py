"""Supervisor-qualified operation lifetime, deadline, and mutation evidence."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any

from .._deadline import RuntimeDeadline
from .._idempotency import (
    JournalEntry,
    OperationJournal,
    attach_operation_journal,
    collect_operation_journal_entries,
    detached_operation_journal_context,
)
from ..exceptions import OperationTimeoutError
from ..outcomes import OperationMetadata


@dataclass
class OperationContext:
    """One admitted workflow's task-local lifetime and evidence carrier."""

    supervisor: object = field(repr=False)
    loop: asyncio.AbstractEventLoop = field(repr=False)
    epoch: int
    owner_task: asyncio.Task[Any] = field(repr=False)
    label: str
    absolute_deadline: float | None
    journal: OperationJournal = field(repr=False)
    entries: list[JournalEntry] = field(default_factory=list, repr=False)
    expired: bool = False

    def collect(self, entry: JournalEntry) -> None:
        """Retain an entry once without changing its owning journal."""

        if not any(entry is current for current in self.entries):
            self.entries.append(entry)

    def remaining(self) -> float | None:
        """Return the non-negative operation budget left on the bound loop."""

        if self.absolute_deadline is None:
            return None
        return max(0.0, self.absolute_deadline - self.loop.time())

    def runtime_deadline(self) -> RuntimeDeadline | None:
        """Re-anchor the absolute workflow deadline at the current instant."""

        remaining = self.remaining()
        if remaining is None:
            return None
        now = self.loop.time()
        return RuntimeDeadline(timeout=remaining, started_at=now, monotonic=self.loop.time)

    def metadata_for(self, error: BaseException) -> OperationMetadata | None:
        """Attach the complete workflow snapshot without erasing richer evidence."""

        existing = getattr(error, "operation_metadata", None)
        if self.entries:
            attach_operation_journal(
                error,
                self.journal,
                extra_entries=tuple(self.entries),
            )
        elif existing is None:
            attach_operation_journal(error, self.journal)
        return getattr(error, "operation_metadata", None)


_OPERATION_CONTEXTS: ContextVar[tuple[OperationContext, ...]] = ContextVar(
    "notebooklm_operation_contexts", default=()
)


def current_operation_context(supervisor: object) -> OperationContext | None:
    """Return the active context only for its supervisor and admitted task."""

    task = asyncio.current_task()
    if task is None:
        return None
    for context in reversed(_OPERATION_CONTEXTS.get()):
        if context.supervisor is supervisor and context.owner_task is task:
            return context
    return None


def operation_deadline_expired() -> bool:
    """Return whether this task is handling an owned operation timer request."""

    task = asyncio.current_task()
    return task is not None and any(
        context.owner_task is task and context.expired for context in _OPERATION_CONTEXTS.get()
    )


def adopt_operation_journal_entry(
    supervisor: object,
    *,
    method: str,
    operation: str,
) -> JournalEntry | None:
    """Allocate one conservative terminal mutation in the active workflow."""

    context = current_operation_context(supervisor)
    if context is None:
        return None
    entry = context.journal.new_entry(method=method, operation=operation)
    context.collect(entry)
    return entry


def fork_operation_context(
    parent: OperationContext,
    owner_task: asyncio.Task[Any],
) -> OperationContext:
    """Give one registered exclusive child the parent's workflow identity."""

    return OperationContext(
        supervisor=parent.supervisor,
        loop=parent.loop,
        epoch=parent.epoch,
        owner_task=owner_task,
        label=parent.label,
        absolute_deadline=parent.absolute_deadline,
        journal=parent.journal,
        entries=parent.entries,
    )


@contextmanager
def detached_operation_context() -> Iterator[None]:
    """Clear copied waiter deadline and replay state for shared work."""

    token = _OPERATION_CONTEXTS.set(())
    try:
        with detached_operation_journal_context():
            yield
    finally:
        _OPERATION_CONTEXTS.reset(token)


def earlier_deadline(
    supplied: RuntimeDeadline | None,
    context: OperationContext | None,
) -> RuntimeDeadline | None:
    """Return a freshly anchored minimum of an inner and workflow deadline."""

    workflow = None if context is None else context.runtime_deadline()
    if supplied is None:
        return workflow
    if workflow is None:
        return supplied
    now = supplied.now()
    remaining = min(supplied.remaining(), workflow.remaining())
    return RuntimeDeadline(timeout=remaining, started_at=now, monotonic=supplied.monotonic)


def _absolute_deadline(
    loop: asyncio.AbstractEventLoop,
    timeout: float | None,
    parent: OperationContext | None,
    supplied: float | None,
) -> float | None:
    candidate = None if timeout is None else loop.time() + timeout
    if supplied is not None:
        candidate = supplied if candidate is None else min(candidate, supplied)
    if parent is None or parent.absolute_deadline is None:
        return candidate
    if candidate is None:
        return parent.absolute_deadline
    return min(parent.absolute_deadline, candidate)


def create_operation_context(
    supervisor: object,
    *,
    epoch: int,
    label: str,
    timeout: float | None,
    parent: OperationContext | None = None,
    absolute_deadline: float | None = None,
) -> OperationContext:
    """Create a same-task context, sharing evidence with an explicit parent."""

    if timeout is not None and (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        raise ValueError(f"timeout must be a positive, finite number or None (got {timeout!r})")
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    if task is None:  # pragma: no cover - async context invariant
        raise RuntimeError("client operation has no owning task")
    if parent is not None:
        if parent.loop is not loop or parent.owner_task is not task:
            raise RuntimeError("nested operation context belongs to another task or event loop")
        journal = parent.journal
        entries = parent.entries
    else:
        journal = OperationJournal(label)
        entries = []
    return OperationContext(
        supervisor=supervisor,
        loop=loop,
        epoch=epoch,
        owner_task=task,
        label=label,
        absolute_deadline=_absolute_deadline(loop, timeout, parent, absolute_deadline),
        journal=journal,
        entries=entries,
    )


def operation_timeout_error(context: OperationContext) -> OperationTimeoutError:
    """Build the public timeout and attach all evidence captured so far."""

    error = OperationTimeoutError(f"{context.label} exceeded its operation deadline")
    context.metadata_for(error)
    return error


def _generation_retired(context: OperationContext) -> bool:
    return getattr(context.supervisor, "active_epoch", lambda: None)() != context.epoch


@contextmanager
def activate_operation_context(context: OperationContext) -> Iterator[OperationContext]:
    """Bind a context and own same-task deadline cancellation attribution."""

    stack = _OPERATION_CONTEXTS.get()
    token: Token[tuple[OperationContext, ...]] = _OPERATION_CONTEXTS.set((*stack, context))
    fired = False

    def _expire() -> None:
        nonlocal fired
        fired = True
        context.expired = True
        context.owner_task.cancel()

    timer = (
        None
        if context.absolute_deadline is None
        else context.loop.call_at(context.absolute_deadline, _expire)
    )
    caught: BaseException | None = None
    collector = collect_operation_journal_entries(context.collect, context.journal)
    collector.__enter__()
    owned_cancel_removed = False
    try:
        try:
            yield context
        except BaseException as exc:
            caught = exc
            if (
                isinstance(exc, asyncio.CancelledError)
                and fired
                and not _generation_retired(context)
            ):
                remaining_cancels = 0
                uncancel = getattr(context.owner_task, "uncancel", None)
                if callable(uncancel):
                    remaining_cancels = uncancel()
                    owned_cancel_removed = True
                if remaining_cancels == 0:
                    raise operation_timeout_error(context) from None
            if context.entries or fired:
                context.metadata_for(exc)
            raise
    finally:
        if timer is not None:
            timer.cancel()
        collector.__exit__(None, None, None)
        _OPERATION_CONTEXTS.reset(token)
        # Settlement may consume the timer's CancelledError while preserving a
        # body exception. Remove exactly our request so TaskGroup/timeout does
        # not observe a phantom external cancellation on Python 3.11+.
        remaining_after_owned_cancel: int | None = None
        if fired and not owned_cancel_removed:
            uncancel = getattr(context.owner_task, "uncancel", None)
            if callable(uncancel):
                remaining_after_owned_cancel = uncancel()
        if fired and caught is None:
            if _generation_retired(context):
                raise asyncio.CancelledError
            if remaining_after_owned_cancel is not None and remaining_after_owned_cancel > 0:
                raise asyncio.CancelledError
            raise operation_timeout_error(context)


__all__ = [
    "OperationContext",
    "adopt_operation_journal_entry",
    "activate_operation_context",
    "create_operation_context",
    "current_operation_context",
    "detached_operation_context",
    "earlier_deadline",
    "fork_operation_context",
    "operation_deadline_expired",
    "operation_timeout_error",
]
