"""Private source readiness polling service."""

from __future__ import annotations

import asyncio
import builtins
import logging
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any

from .._deadline import RuntimeDeadline
from .._types.enums import SourceStatus
from ..types import Source, SourceNotFoundError, SourceProcessingError, SourceTimeoutError

# Source type codes where status=3 (ERROR) is transient rather than terminal.
# Audio/media (10) and unclassified (None / 0) sources can briefly report
# status=3 during transcription/classification before settling at status=2. All
# other types (PDFs, web, YouTube, etc.) treat status=3 as a terminal failure.
# New unknown types default to terminal - fail fast rather than silently looping
# until timeout. See #391.
_TRANSIENT_ERROR_TYPES: tuple[int | None, ...] = (10, 0, None)


#: How many *consecutive* ERROR observations, ending at the deadline, count as
#: evidence that a tolerated transient error is really terminal. One is not
#: enough: a caller may pass a short ``timeout`` and get a single look at a
#: source that is legitimately mid-transcription, and a deadline proves only
#: that the wait budget expired — never that the backend state is final.
_MIN_SUSTAINED_ERROR_POLLS = 2


def _expiry_error(
    source_id: str,
    timeout: float,
    last_status: int | None,
    *,
    error_streak: int = 0,
) -> SourceProcessingError | SourceTimeoutError:
    """Report the failure actually observed at the deadline, not merely "time ran out".

    Tolerating a transient ERROR (see ``_TRANSIENT_ERROR_TYPES``) is a
    *hypothesis*: an audio or still-unclassified source may report status=3
    briefly while it is being transcribed/classified, so the poll keeps going
    rather than failing fast. Sustained ERROR right up to the deadline is what
    disproves it — the source did not fail to answer, it answered ERROR, over
    and over, until we gave up. Calling that a timeout misdiagnoses it.

    This is the #2138 ``.wav`` route. A WAV uploads cleanly (bytes transfer
    fine) and processing then ends at ``status=ERROR`` with ``type_code=0``,
    which is in the transient list — so the poll tolerated it to the deadline
    and raised :class:`SourceTimeoutError`, and nothing anywhere named the
    processing failure. Callers reasonably read a timeout as "still working, ask
    again later" and retry forever.

    **"Sustained" is load-bearing, and ``error_streak`` is what measures it.**
    A deadline is caller-chosen and can be short: ``wait_until_ready(timeout=2)``
    on a source that is legitimately mid-transcription may get exactly one look,
    see ERROR, and expire — and that source may still reach READY seconds later.
    Reporting it as a terminal processing failure would be a fabricated verdict,
    the mirror image of the bug this fixes. So the conversion requires
    :data:`_MIN_SUSTAINED_ERROR_POLLS` *consecutive* ERROR observations ending
    at the deadline; anything less stays a timeout, which is the honest answer
    when all we know is that the budget ran out. The streak resets on any
    non-ERROR observation, so a source that was PROCESSING and only flipped to
    ERROR on the final tick is a timeout too.

    Both types are :class:`~notebooklm.exceptions.SourceError`, and every
    consumer in this repo already handles them side by side (``_app``'s
    ``SourceWaitOutcome`` buckets, the MCP ``_waitagg`` mapper, ``notebooklm
    source wait``'s exit codes), so this changes which bucket the ``.wav`` case
    lands in — from "timed out, try again" to "failed" — rather than adding an
    unhandled shape.

    The elapsed budget stays in the message: "ERROR throughout a 120 s poll" and
    "ERROR across two ticks of a 3 s poll" are different claims, and the reader
    needs to know which one this is.
    """
    if last_status == SourceStatus.ERROR and error_streak >= _MIN_SUSTAINED_ERROR_POLLS:
        return SourceProcessingError(
            source_id,
            last_status,
            message=(
                f"Source {source_id} failed to process: still reporting ERROR after "
                f"{timeout:.1f}s ({error_streak} consecutive polls). Its type is one for "
                "which a brief ERROR is treated as transient, so the poll waited it out; "
                "it never resolved, so the failure is treated as terminal. The source row "
                "is retained server-side — list the notebook's sources to find it."
            ),
        )
    return SourceTimeoutError(source_id, timeout, last_status)


GetSource = Callable[[str, str], Awaitable[Source | None]]
ListSources = Callable[[str], Awaitable[builtins.list[Source]]]
WaitUntilReady = Callable[..., Coroutine[Any, Any, Source]]
Sleep = Callable[[float], Awaitable[Any]]
Monotonic = Callable[[], float]

# One neutral per-source result from ``wait_all_until_ready``. Terminal failures
# are RETURNED (not raised) so one bad source never discards its siblings'
# progress; ``_app`` maps each to a typed ``SourceWaitOutcome``.
SourceWaitResult = Source | SourceNotFoundError | SourceProcessingError | SourceTimeoutError


class SourcePoller:
    """Source readiness and registration polling behavior.

    Facade behavior that must remain patchable on ``SourcesAPI`` is supplied as
    call-time callbacks instead of being captured during construction.
    """

    async def wait_until_ready(
        self,
        notebook_id: str,
        source_id: str,
        *,
        timeout: float = 120.0,
        initial_interval: float = 1.0,
        max_interval: float = 10.0,
        backoff_factor: float = 1.5,
        transient_error_types: tuple[int | None, ...] | None = None,
        get_source: GetSource,
        sleep: Sleep,
        monotonic: Monotonic,
        logger: logging.Logger,
    ) -> Source:
        """Wait for a source to become ready."""
        deadline = RuntimeDeadline.start(timeout, monotonic=monotonic)
        interval = initial_interval
        last_status: int | None = None
        # Consecutive ERROR observations ending at the current tick. Reset by any
        # non-ERROR look, so it measures *sustained* error rather than "the last
        # thing we happened to see" — see ``_expiry_error``.
        error_streak = 0
        transient_errors = (
            _TRANSIENT_ERROR_TYPES if transient_error_types is None else transient_error_types
        )

        while True:
            # Check timeout before each poll.
            if deadline.expired():
                raise _expiry_error(source_id, timeout, last_status, error_streak=error_streak)

            source = await get_source(notebook_id, source_id)

            if source is None:
                raise SourceNotFoundError(source_id)

            last_status = source.status
            error_streak = error_streak + 1 if source.is_error else 0

            if source.is_ready:
                return source

            if source.is_error:
                if source._type_code not in transient_errors:
                    raise SourceProcessingError(source_id, source.status)
                logger.debug(
                    "Source %s reported transient ERROR status for type %s; continuing poll",
                    source_id,
                    source._type_code,
                )

            # Don't sleep longer than remaining time.
            if deadline.expired():
                raise _expiry_error(source_id, timeout, last_status, error_streak=error_streak)

            sleep_time = deadline.clamp_sleep(interval)
            await sleep(sleep_time)
            interval = min(interval * backoff_factor, max_interval)

    async def wait_until_registered(
        self,
        notebook_id: str,
        source_id: str,
        *,
        timeout: float = 30.0,
        initial_interval: float = 0.5,
        max_interval: float = 5.0,
        backoff_factor: float = 1.5,
        transient_error_types: tuple[int | None, ...] | None = None,
        get_source: GetSource,
        sleep: Sleep,
        monotonic: Monotonic,
        logger: logging.Logger,
    ) -> Source:
        """Wait for a source to be registered server-side."""
        deadline = RuntimeDeadline.start(timeout, monotonic=monotonic)
        interval = initial_interval
        last_status: int | None = None
        # Consecutive ERROR observations ending at the current tick. Reset by any
        # non-ERROR look, so it measures *sustained* error rather than "the last
        # thing we happened to see" — see ``_expiry_error``.
        error_streak = 0
        transient_errors = (
            _TRANSIENT_ERROR_TYPES if transient_error_types is None else transient_error_types
        )

        while True:
            if deadline.expired():
                raise _expiry_error(source_id, timeout, last_status, error_streak=error_streak)

            source = await get_source(notebook_id, source_id)

            if source is not None:
                last_status = source.status
                error_streak = error_streak + 1 if source.is_error else 0

                if source.is_error:
                    if source._type_code not in transient_errors:
                        raise SourceProcessingError(source_id, source.status)
                    logger.debug(
                        "Source %s reported transient ERROR status for type %s; "
                        "continuing registration poll",
                        source_id,
                        source._type_code,
                    )
                else:
                    # Any non-error status (PROCESSING, READY, PREPARING)
                    # means the source is registered server-side.
                    return source

            if deadline.expired():
                raise _expiry_error(source_id, timeout, last_status, error_streak=error_streak)

            sleep_time = deadline.clamp_sleep(interval)
            await sleep(sleep_time)
            interval = min(interval * backoff_factor, max_interval)

    async def wait_all_until_ready(
        self,
        notebook_id: str,
        source_ids: builtins.list[str],
        *,
        timeout: float = 120.0,
        initial_interval: float = 1.0,
        max_interval: float = 10.0,
        backoff_factor: float = 1.5,
        transient_error_types: tuple[int | None, ...] | None = None,
        list_sources: ListSources,
        sleep: Sleep,
        monotonic: Monotonic,
        logger: logging.Logger,
    ) -> builtins.list[SourceWaitResult]:
        """Wait for many sources with ONE notebook snapshot per poll tick.

        Fetches the whole notebook source list once per tick and resolves every
        still-pending source against that single snapshot, instead of fanning
        out one whole-notebook poll per source (O(N) GETs per tick -> O(N^2)).

        Returns one :data:`SourceWaitResult` per input id, in input order.
        Terminal per-source failures (missing / processing error / timeout) are
        RETURNED, not raised, so a slow/failed source never discards the
        progress of its siblings. Only an UNEXPECTED transport error (e.g.
        ``RPCError`` from ``list_sources``) propagates out of the single await.
        """
        deadline = RuntimeDeadline.start(timeout, monotonic=monotonic)
        interval = initial_interval
        transient_errors = (
            _TRANSIENT_ERROR_TYPES if transient_error_types is None else transient_error_types
        )

        results: builtins.list[SourceWaitResult | None] = [None] * len(source_ids)
        pending: dict[int, str] = dict(enumerate(source_ids))
        # Per-source (keyed by pending index) last observed status, so a timed-out
        # source reports its OWN last status rather than a sibling's.
        last_status: dict[int, int | None] = {}
        # Per-source consecutive-ERROR counters; see ``_expiry_error``. Kept per
        # index for the same reason ``last_status`` is: one source's streak must
        # never decide another's verdict.
        error_streak: dict[int, int] = {}

        while pending:
            if deadline.expired():
                self._fill_timeouts(results, pending, last_status, error_streak, timeout)
                break

            # ONE whole-notebook snapshot per tick, shared across all pending ids.
            snapshot = await list_sources(notebook_id)
            by_id = {source.id: source for source in snapshot}

            resolved: builtins.list[int] = []
            for index, sid in pending.items():
                source = by_id.get(sid)

                if source is None:
                    # Parity with wait_until_ready: absent id is not-found.
                    results[index] = SourceNotFoundError(sid)
                    resolved.append(index)
                    continue

                last_status[index] = source.status
                error_streak[index] = error_streak.get(index, 0) + 1 if source.is_error else 0

                if source.is_ready:
                    results[index] = source
                    resolved.append(index)
                    continue

                if source.is_error:
                    if source._type_code not in transient_errors:
                        results[index] = SourceProcessingError(sid, source.status)
                        resolved.append(index)
                        continue
                    logger.debug(
                        "Source %s reported transient ERROR status for type %s; continuing poll",
                        sid,
                        source._type_code,
                    )
                # PROCESSING (or transient ERROR): stays pending for the next tick.

            for index in resolved:
                del pending[index]

            if not pending:
                break

            if deadline.expired():
                self._fill_timeouts(results, pending, last_status, error_streak, timeout)
                break

            sleep_time = deadline.clamp_sleep(interval)
            await sleep(sleep_time)
            interval = min(interval * backoff_factor, max_interval)

        final: builtins.list[SourceWaitResult] = []
        for result in results:
            if result is None:
                raise AssertionError("wait_all_until_ready left a source unresolved")
            final.append(result)
        return final

    @staticmethod
    def _fill_timeouts(
        results: builtins.list[SourceWaitResult | None],
        pending: dict[int, str],
        last_status: dict[int, int | None],
        error_streak: dict[int, int],
        timeout: float,
    ) -> None:
        """Resolve every still-pending source from its OWN final observed status.

        Not necessarily a :class:`SourceTimeoutError`: a source whose ERROR was
        sustained to the deadline resolves as :class:`SourceProcessingError`
        instead (#2138). Both dicts are read per-index so one source's history
        can never decide another's verdict.
        """
        for index, sid in pending.items():
            results[index] = _expiry_error(
                sid,
                timeout,
                last_status.get(index),
                error_streak=error_streak.get(index, 0),
            )

    async def wait_for_sources(
        self,
        notebook_id: str,
        source_ids: builtins.list[str],
        *,
        timeout: float = 120.0,
        wait_until_ready: WaitUntilReady,
        logger: logging.Logger,
        **kwargs: Any,
    ) -> builtins.list[Source]:
        """Wait for multiple sources to become ready in parallel."""
        # A bare ``asyncio.gather(*coros)`` propagates the first
        # exception before sibling pollers have necessarily finished their
        # cleanup, and it does not cancel still-running siblings for us.
        # Drive the fan-out as explicit tasks so any failure cancels and
        # drains every pending sibling before re-raising.
        tasks: builtins.list[asyncio.Task[Source]] = [
            asyncio.create_task(wait_until_ready(notebook_id, sid, timeout=timeout, **kwargs))
            for sid in source_ids
        ]
        try:
            return list(await asyncio.gather(*tasks))
        except BaseException:
            logger.debug("wait_for_sources: cancelling sibling source pollers", exc_info=True)
            for task in tasks:
                if not task.done():
                    task.cancel()
            # Drain cancelled (and any already-failed) siblings before
            # surfacing the original exception. ``return_exceptions=True``
            # swallows the cancellations and concurrent failures so the
            # outer ``raise`` still raises the first task's exception.
            await asyncio.gather(*tasks, return_exceptions=True)
            raise


__all__ = ["SourcePoller", "SourceWaitResult"]
