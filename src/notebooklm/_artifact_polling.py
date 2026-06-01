"""Private artifact polling service."""

from __future__ import annotations

import asyncio
import builtins
import logging
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol

from ._artifact_listing import find_artifact_row_by_id
from ._backoff import compute_backoff_delay
from ._callbacks import maybe_await_callback
from ._deadline import Monotonic, RuntimeDeadline, Sleep
from ._polling_registry import PollRegistry
from ._row_adapters_artifacts import ArtifactRow
from ._runtime_contracts import LoopGuard
from .exceptions import ArtifactInProgressTimeoutError, ArtifactPendingTimeoutError
from .rpc import (
    ArtifactStatus,
    ArtifactTypeCode,
    NetworkError,
    RPCTimeoutError,
    ServerError,
    artifact_status_to_str,
)
from .types import GenerationStatus

logger = logging.getLogger(__name__)

# Maximum number of retries for transient errors during artifact polling.
POLL_MAX_RETRIES = 3
_IN_PROGRESS_STATUS = "in_progress"

ListRawCallback = Callable[[str], Awaitable[builtins.list[Any]]]
PollStatusCallback = Callable[[str, str], Awaitable[GenerationStatus]]
MediaReadyCallback = Callable[[builtins.list[Any], int], bool]
ArtifactTypeNameCallback = Callable[[int], str]
ArtifactErrorCallback = Callable[[builtins.list[Any]], str | None]
StatusChangeCallback = Callable[[GenerationStatus], object]


class OperationScopeProvider(Protocol):
    """``operation_scope`` async-context-manager surface for feature APIs.

    Inlined from ``_runtime_contracts`` in issue #1327: artifact polling
    is the only consumer, so this single-consumer Protocol lives local to
    its owner per the ADR-013 ≥2-feature promotion bar.
    """

    def operation_scope(self, label: str) -> AbstractAsyncContextManager[None]: ...


class ArtifactPollingService:
    """Leader/follower artifact polling boundary.

    The service owns lifecycle and bookkeeping for shared artifact poll tasks.
    Facade behavior that must remain patchable on ``ArtifactsAPI`` is supplied
    as call-time callbacks instead of being captured during construction.
    """

    def __init__(
        self,
        *,
        loop_guard: LoopGuard,
        op_scope: OperationScopeProvider,
        poll_registry: PollRegistry | None = None,
        sleep: Sleep | None = None,
        monotonic: Monotonic | None = None,
    ) -> None:
        self._loop_guard = loop_guard
        self._op_scope = op_scope
        self._poll_registry = poll_registry if poll_registry is not None else PollRegistry()
        self._sleep = sleep
        self._monotonic = monotonic

    @property
    def poll_registry(self) -> PollRegistry:
        """Return the feature-owned polling registry."""
        return self._poll_registry

    def _resolve_sleep(self) -> Sleep:
        return asyncio.sleep if self._sleep is None else self._sleep

    def _resolve_monotonic(self) -> Monotonic:
        return asyncio.get_running_loop().time if self._monotonic is None else self._monotonic

    async def drain(self) -> None:
        """Cancel active leader poll tasks and await polling bookkeeping."""
        poll_tasks = self._poll_registry.active_tasks()
        for task in poll_tasks:
            task.cancel()
        if poll_tasks:
            await asyncio.gather(*poll_tasks, return_exceptions=True)

    async def poll_status(
        self,
        notebook_id: str,
        task_id: str,
        *,
        list_raw: ListRawCallback,
        is_media_ready: MediaReadyCallback,
        get_artifact_type_name: ArtifactTypeNameCallback,
        extract_artifact_error: ArtifactErrorCallback,
    ) -> GenerationStatus:
        """Poll the status of a generation task."""
        # List all artifacts and find by ID (no poll-by-ID RPC exists).
        artifacts_data = await list_raw(notebook_id)
        row = find_artifact_row_by_id(artifacts_data, task_id)
        if row is not None:
            status_code = row.status
            artifact_type = row.type_code
            raw_status = artifact_status_to_str(status_code)
            metadata: dict[str, Any] | None = None

            # For media artifacts, verify URL availability before reporting completion.
            # The API may set status=COMPLETED before media URLs are populated.
            if status_code == ArtifactStatus.COMPLETED:
                if not is_media_ready(row.raw, artifact_type):
                    type_name = get_artifact_type_name(artifact_type)
                    metadata = {
                        "artifact_type": type_name,
                        "artifact_type_code": artifact_type,
                        "media_ready": False,
                        "normalized_status": _IN_PROGRESS_STATUS,
                        "raw_status": raw_status,
                    }
                    logger.debug(
                        "Artifact %s (type=%s) status=COMPLETED but media not ready, "
                        "continuing poll",
                        task_id,
                        type_name,
                    )
                    # Downgrade to PROCESSING to continue polling.
                    status_code = ArtifactStatus.PROCESSING

            status = artifact_status_to_str(status_code)

            error_msg: str | None = None
            if status == "failed":
                error_msg = extract_artifact_error(row.raw)
            url = row.artifact_url(artifact_type, suppress_drift=True)

            return GenerationStatus(
                task_id=task_id,
                status=status,
                url=url,
                error=error_msg,
                metadata=metadata,
            )

        # Artifact not found in the list. Use a distinct status so
        # wait_for_completion can differentiate from genuine "pending".
        return GenerationStatus(task_id=task_id, status="not_found")

    async def wait_for_completion(
        self,
        notebook_id: str,
        task_id: str,
        *,
        initial_interval: float = 2.0,
        max_interval: float = 10.0,
        timeout: float = 300.0,
        max_not_found: int = 5,
        min_not_found_window: float = 10.0,
        poll_status: PollStatusCallback,
        on_status_change: StatusChangeCallback | None = None,
    ) -> GenerationStatus:
        """Wait for a generation task to complete using a shared poll loop."""
        # Catch cross-loop wait_for_completion before touching the
        # poll registry (which holds futures bound to the registering
        # loop) or spawning a poll task on a foreign loop.
        self._loop_guard.assert_bound_loop()

        key = (notebook_id, task_id)

        existing = self._poll_registry.get(key)
        if existing is not None:
            # Follower path. ``asyncio.shield`` ensures that *this* caller's
            # cancellation does not propagate into the shared future; the
            # leader's poll task continues on behalf of every other follower.
            result = await asyncio.shield(existing[0])
            if on_status_change is not None:
                await maybe_await_callback(on_status_change, result)
            return result

        # Leader path. Create the shared future, spawn the poll task, and
        # register the pair so any follower can attach. The task reference
        # anchors the running poll against GC until the completion callback
        # resolves the shared future.
        loop = asyncio.get_running_loop()
        future: asyncio.Future[GenerationStatus] = loop.create_future()

        # Consume any exception set on the future if no caller ever retrieves
        # it (e.g. leader cancelled with no followers). Without this,
        # ``set_exception`` on an unawaited future logs at GC time.
        def _consume_orphan_exception(fut: asyncio.Future[GenerationStatus]) -> None:
            if not fut.cancelled():
                # ``exception()`` clears the _log_traceback flag inside the
                # future. We intentionally drop the value.
                fut.exception()

        future.add_done_callback(_consume_orphan_exception)

        poll_task = asyncio.create_task(
            self._run_poll_loop_in_scope(
                notebook_id,
                task_id,
                initial_interval=initial_interval,
                max_interval=max_interval,
                timeout=timeout,
                max_not_found=max_not_found,
                min_not_found_window=min_not_found_window,
                poll_status=poll_status,
                on_status_change=on_status_change,
            ),
            name=f"artifact-poll-{notebook_id}-{task_id}",
        )
        self._poll_registry.register(key, future, poll_task)

        def _resolve_poll(task: asyncio.Task[GenerationStatus]) -> None:
            # Pop the registry entry before resolving the future so a waiter
            # arriving concurrently with completion either attaches to this
            # result or starts a fresh poll for a later generation.
            self._poll_registry.pop(key)
            if future.done():
                raise RuntimeError("BUG: future resolved before poll task done-callback")
            if task.cancelled():
                future.cancel()
                return
            poll_exc = task.exception()
            if poll_exc is not None:
                future.set_exception(poll_exc)
                return
            future.set_result(task.result())

        def _on_poll_done(task: asyncio.Task[GenerationStatus]) -> None:
            _resolve_poll(task)

        poll_task.add_done_callback(_on_poll_done)

        # Leader awaits via ``asyncio.shield`` so that the leader's
        # cancellation unwinds locally without taking down the shared poll.
        # Remaining followers still receive the result.
        return await asyncio.shield(future)

    async def _run_poll_loop_in_scope(
        self,
        notebook_id: str,
        task_id: str,
        *,
        initial_interval: float,
        max_interval: float,
        timeout: float,
        max_not_found: int,
        min_not_found_window: float,
        poll_status: PollStatusCallback,
        on_status_change: StatusChangeCallback | None,
    ) -> GenerationStatus:
        async with self._op_scope.operation_scope(f"artifact wait {task_id}"):
            return await self._run_poll_loop(
                notebook_id,
                task_id,
                initial_interval=initial_interval,
                max_interval=max_interval,
                timeout=timeout,
                max_not_found=max_not_found,
                min_not_found_window=min_not_found_window,
                poll_status=poll_status,
                on_status_change=on_status_change,
            )

    async def _run_poll_loop(
        self,
        notebook_id: str,
        task_id: str,
        *,
        initial_interval: float,
        max_interval: float,
        timeout: float,
        max_not_found: int,
        min_not_found_window: float,
        poll_status: PollStatusCallback,
        on_status_change: StatusChangeCallback | None,
    ) -> GenerationStatus:
        """The actual polling loop. Driven by the leader's shielded task."""
        deadline = RuntimeDeadline.start(timeout, monotonic=self._resolve_monotonic())
        current_interval = initial_interval
        consecutive_not_found = 0
        poll_retry_count = 0
        first_not_found_time: float | None = None
        last_status: str | None = None
        last_emitted_status: str | None = None
        status_transitions: list[GenerationStatus] = []

        while True:
            try:
                status = await poll_status(notebook_id, task_id)
            except (NetworkError, RPCTimeoutError, ServerError) as e:
                # Transient — retry up to POLL_MAX_RETRIES times with
                # exponential backoff capped at 8s. Also clamp by remaining
                # timeout budget so retries never extend past the caller's
                # `timeout` parameter.
                if poll_retry_count >= POLL_MAX_RETRIES:
                    raise
                if deadline.expired():
                    raise _artifact_timeout_error(
                        notebook_id,
                        task_id,
                        timeout,
                        last_status,
                        status_transitions,
                    ) from e
                poll_retry_count += 1
                # No jitter here: tests assert exact 2.0/4.0/8.0 sleeps and
                # the remaining-timeout clamp owns thundering-herd avoidance.
                backoff = deadline.clamp_sleep(
                    compute_backoff_delay(
                        poll_retry_count,
                        base=1.0,
                        cap=8.0,
                        jitter_ratio=0.0,
                    )
                )
                logger.warning(
                    "wait_for_completion: transient %s on poll #%d, retrying in %.1fs",
                    e.__class__.__name__,
                    poll_retry_count,
                    backoff,
                )
                await self._resolve_sleep()(backoff)
                if deadline.expired():
                    raise _artifact_timeout_error(
                        notebook_id,
                        task_id,
                        timeout,
                        last_status,
                        status_transitions,
                    ) from e
                continue

            poll_retry_count = 0  # reset on success
            last_status = status.status
            if status.status != last_emitted_status:
                last_emitted_status = status.status
                status_transitions.append(status)
                if on_status_change is not None:
                    await maybe_await_callback(on_status_change, status)

            if status.is_complete or status.is_failed:
                return status

            # Track the *current* run of consecutive "not found" responses. The
            # API may remove quota-rejected artifacts from the list entirely
            # instead of setting them to FAILED. A *sustained* absence is
            # reported with a distinct ``"removed"`` status (see below) rather
            # than ``"failed"`` so callers can tell a delisted artifact apart
            # from one the server actually marked terminal-FAILED.
            #
            # The counter resets the moment the artifact reappears (see the
            # ``else`` branch). This is what makes ``"removed"`` mean *stayed
            # absent*: a transient/flapping omission — where the artifact keeps
            # coming back and may still complete — never accumulates toward a
            # spurious terminal ``"removed"`` (issue #1198).
            if status.status == "not_found":
                consecutive_not_found += 1
                now = deadline.now()
                if first_not_found_time is None:
                    first_not_found_time = now
                not_found_elapsed = now - first_not_found_time

                # Two ways to declare a sustained absence terminal:
                #  - time-gated: enough consecutive misses AND enough wall-clock
                #    elapsed (avoids a fast burst of polls firing prematurely);
                #  - window-independent: a long consecutive run (2x the
                #    threshold) trips removal even when ``min_not_found_window``
                #    has not yet elapsed.
                consecutive_trigger = (
                    consecutive_not_found >= max_not_found
                    and not_found_elapsed >= min_not_found_window
                )
                window_independent_trigger = consecutive_not_found >= max_not_found * 2

                if consecutive_trigger or window_independent_trigger:
                    trigger = (
                        f"consecutive={consecutive_not_found}"
                        if consecutive_trigger
                        else f"consecutive={consecutive_not_found} (window-independent)"
                    )
                    logger.warning(
                        "Artifact %s disappeared from list (%s not-found polls, "
                        "%s) — treating as removed",
                        task_id,
                        trigger,
                        f"elapsed={not_found_elapsed:.1f}s",
                    )
                    # Report removal with a distinct ``"removed"`` status, not
                    # ``"failed"``. The artifact vanished from the listing
                    # (commonly a daily-quota rejection, possibly a transient
                    # omission) — that is not the same as the server marking it
                    # terminal-FAILED, and conflating the two would mask a real
                    # failure or fabricate one. The error text is retained so
                    # exception-free callers still get an actionable message.
                    removed_status = GenerationStatus(
                        task_id=task_id,
                        status="removed",
                        error=(
                            "Generation incomplete: artifact was removed from the "
                            "list by the server. This may indicate a daily "
                            "quota/rate limit was exceeded, an invalid notebook "
                            "ID, or a transient API issue. Try again later."
                        ),
                    )
                    if on_status_change is not None and last_emitted_status != "removed":
                        await maybe_await_callback(on_status_change, removed_status)
                    return removed_status
            else:
                # The artifact is back in the listing. Reset the not-found
                # tracking so removal requires a single sustained absence run
                # rather than cumulative absences spread across an otherwise-
                # healthy poll (issue #1198). An artifact that keeps reappearing
                # is not "removed"; it keeps polling until it completes or the
                # timeout fires.
                consecutive_not_found = 0
                first_not_found_time = None

            if deadline.exceeded():
                raise _artifact_timeout_error(
                    notebook_id,
                    task_id,
                    timeout,
                    last_status,
                    status_transitions,
                )

            sleep_duration = deadline.clamp_sleep(current_interval)
            if sleep_duration > 0.0:
                await self._resolve_sleep()(sleep_duration)
            elif current_interval > 0.0:
                raise _artifact_timeout_error(
                    notebook_id,
                    task_id,
                    timeout,
                    last_status,
                    status_transitions,
                )

            current_interval = min(current_interval * 2, max_interval)


def _artifact_timeout_error(
    notebook_id: str,
    task_id: str,
    timeout: float,
    last_status: str | None,
    status_transitions: list[GenerationStatus],
) -> ArtifactPendingTimeoutError | ArtifactInProgressTimeoutError:
    history = tuple(status.status for status in status_transitions)
    transitions = tuple(status_transitions)
    if _IN_PROGRESS_STATUS in history or last_status == _IN_PROGRESS_STATUS:
        return ArtifactInProgressTimeoutError(
            notebook_id,
            task_id,
            timeout,
            last_status=last_status,
            status_history=history,
            status_transitions=transitions,
        )
    return ArtifactPendingTimeoutError(
        notebook_id,
        task_id,
        timeout,
        last_status=last_status,
        status_history=history,
        status_transitions=transitions,
    )


def _extract_artifact_error(art: builtins.list[Any]) -> str | None:
    """Try to extract a human-readable error from a failed artifact."""
    try:
        if not isinstance(art, list):
            return None
        return ArtifactRow(art).failed_error_text
    except Exception:
        preview = art[:6] if isinstance(art, list) else art
        logger.warning(
            "Failed to extract error from artifact data: %r",
            preview,
            exc_info=True,
        )
        return None


def _get_artifact_type_name(artifact_type: int) -> str:
    """Get human-readable name for an artifact type."""
    try:
        return ArtifactTypeCode(artifact_type).name
    except ValueError:
        return str(artifact_type)


def _is_media_ready(art: builtins.list[Any], artifact_type: int) -> bool:
    """Check if media artifact has URLs populated."""
    try:
        if not isinstance(art, list):
            return artifact_type not in ArtifactRow._MEDIA_ARTIFACT_TYPES
        return ArtifactRow(art).is_media_ready(artifact_type)

    except (IndexError, TypeError) as e:
        # Defensive: if structure is unexpected, be conservative for media
        # types. Media types need URLs, so return False to continue polling.
        is_media = artifact_type in ArtifactRow._MEDIA_ARTIFACT_TYPES
        logger.debug(
            "Unexpected artifact structure for type %s (media=%s): %s",
            artifact_type,
            is_media,
            e,
        )
        return not is_media
