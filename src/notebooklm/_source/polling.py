"""Private source readiness polling service."""

from __future__ import annotations

import asyncio
import builtins
import logging
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any

from .._deadline import RuntimeDeadline
from ..types import Source, SourceNotFoundError, SourceProcessingError, SourceTimeoutError

# Source type codes where status=3 (ERROR) is transient rather than terminal.
# Audio/media (10) and unclassified (None / 0) sources can briefly report
# status=3 during transcription/classification before settling at status=2. All
# other types (PDFs, web, YouTube, etc.) treat status=3 as a terminal failure.
# New unknown types default to terminal - fail fast rather than silently looping
# until timeout. See #391.
_TRANSIENT_ERROR_TYPES: tuple[int | None, ...] = (10, 0, None)

GetSource = Callable[[str, str], Awaitable[Source | None]]
WaitUntilReady = Callable[..., Coroutine[Any, Any, Source]]
Sleep = Callable[[float], Awaitable[Any]]
Monotonic = Callable[[], float]


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
        transient_errors = (
            _TRANSIENT_ERROR_TYPES if transient_error_types is None else transient_error_types
        )

        while True:
            # Check timeout before each poll.
            if deadline.expired():
                raise SourceTimeoutError(source_id, timeout, last_status)

            source = await get_source(notebook_id, source_id)

            if source is None:
                raise SourceNotFoundError(source_id)

            last_status = source.status

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
                raise SourceTimeoutError(source_id, timeout, last_status)

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
        transient_errors = (
            _TRANSIENT_ERROR_TYPES if transient_error_types is None else transient_error_types
        )

        while True:
            if deadline.expired():
                raise SourceTimeoutError(source_id, timeout, last_status)

            source = await get_source(notebook_id, source_id)

            if source is not None:
                last_status = source.status

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
                raise SourceTimeoutError(source_id, timeout, last_status)

            sleep_time = deadline.clamp_sleep(interval)
            await sleep(sleep_time)
            interval = min(interval * backoff_factor, max_interval)

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


__all__ = ["SourcePoller"]
