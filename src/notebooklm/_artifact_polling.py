"""Private artifact polling service."""

from __future__ import annotations

import asyncio
import builtins
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from ._callbacks import maybe_await_callback
from ._capabilities import PollRegistryProvider, TransportOperationProvider
from .rpc import (
    ArtifactStatus,
    ArtifactTypeCode,
    NetworkError,
    RPCTimeoutError,
    ServerError,
    artifact_status_to_str,
)
from .types import GenerationStatus, _extract_artifact_url

logger = logging.getLogger(__name__)

# Maximum number of retries for transient errors during artifact polling.
POLL_MAX_RETRIES = 3

# Media artifact types that require URL availability before reporting completion.
_MEDIA_ARTIFACT_TYPES = frozenset(
    {
        ArtifactTypeCode.AUDIO.value,
        ArtifactTypeCode.VIDEO.value,
        ArtifactTypeCode.INFOGRAPHIC.value,
        ArtifactTypeCode.SLIDE_DECK.value,
    }
)

ListRawCallback = Callable[[str], Awaitable[builtins.list[Any]]]
PollStatusCallback = Callable[[str, str], Awaitable[GenerationStatus]]
MediaReadyCallback = Callable[[builtins.list[Any], int], bool]
ArtifactTypeNameCallback = Callable[[int], str]
ArtifactErrorCallback = Callable[[builtins.list[Any]], str | None]
StatusChangeCallback = Callable[[GenerationStatus], object]


class ArtifactPollingCapabilities(PollRegistryProvider, TransportOperationProvider, Protocol):
    """Capabilities required by the artifact polling boundary."""


class ArtifactPollingService:
    """Leader/follower artifact polling boundary.

    The service owns lifecycle and bookkeeping for shared artifact poll tasks.
    Facade behavior that must remain patchable on ``ArtifactsAPI`` is supplied
    as call-time callbacks instead of being captured during construction.
    """

    def __init__(self, capabilities: ArtifactPollingCapabilities) -> None:
        self._capabilities = capabilities
        self._completion_tasks: set[asyncio.Task[None]] = set()

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
        for art in artifacts_data:
            if len(art) > 0 and art[0] == task_id:
                status_code = art[4] if len(art) > 4 else 0
                artifact_type = art[2] if len(art) > 2 else 0

                # For media artifacts, verify URL availability before reporting completion.
                # The API may set status=COMPLETED before media URLs are populated.
                if status_code == ArtifactStatus.COMPLETED:
                    if not is_media_ready(art, artifact_type):
                        type_name = get_artifact_type_name(artifact_type)
                        logger.debug(
                            "Artifact %s (type=%s) status=COMPLETED but media not ready, "
                            "continuing poll",
                            task_id,
                            type_name,
                        )
                        # Downgrade to PROCESSING to continue polling.
                        status_code = ArtifactStatus.PROCESSING

                status = artifact_status_to_str(status_code)

                # Extract error details from failed artifacts. The API may
                # embed an error reason string at art[3] when the artifact
                # fails (e.g. daily quota exceeded).
                error_msg: str | None = None
                if status == "failed":
                    error_msg = extract_artifact_error(art)
                url = _extract_artifact_url(art, artifact_type)

                return GenerationStatus(
                    task_id=task_id,
                    status=status,
                    url=url,
                    error=error_msg,
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
        poll_interval: float | None = None,
        max_not_found: int = 5,
        min_not_found_window: float = 10.0,
        poll_status: PollStatusCallback,
        on_status_change: StatusChangeCallback | None = None,
        deprecation_warning_stacklevel: int = 2,
    ) -> GenerationStatus:
        """Wait for a generation task to complete using a shared poll loop."""
        # Backward compatibility: poll_interval overrides initial_interval.
        if poll_interval is not None:
            import warnings

            warnings.warn(
                "poll_interval is deprecated, use initial_interval instead",
                DeprecationWarning,
                stacklevel=deprecation_warning_stacklevel,
            )
            initial_interval = poll_interval

        pending = self._capabilities.poll_registry.pending
        key = (notebook_id, task_id)

        existing = pending.get(key)
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
            self._run_poll_loop(
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
        pending[key] = (future, poll_task)
        try:
            poll_operation_token = await self._capabilities.begin_transport_task(
                poll_task,
                f"artifact wait {task_id}",
            )
        except BaseException as begin_exc:
            pending.pop(key, None)
            poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await poll_task
            if not future.done():
                if isinstance(begin_exc, asyncio.CancelledError):
                    future.cancel()
                else:
                    future.set_exception(begin_exc)
            raise

        async def _finish_poll_operation() -> None:
            try:
                await self._capabilities.finish_transport_post(poll_operation_token)
            except Exception as cleanup_exc:  # noqa: BLE001 - cleanup should not mask poll result
                logger.warning("Artifact poll drain bookkeeping failed: %s", cleanup_exc)

        def _resolve_poll(task: asyncio.Task[GenerationStatus]) -> None:
            # Pop the registry entry before resolving the future so a waiter
            # arriving concurrently with completion either attaches to this
            # result or starts a fresh poll for a later generation.
            pending.pop(key, None)
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
            completion_task = asyncio.create_task(_finish_poll_operation())
            self._completion_tasks.add(completion_task)
            completion_task.add_done_callback(self._completion_tasks.discard)
            _resolve_poll(task)

        poll_task.add_done_callback(_on_poll_done)

        # Leader awaits via ``asyncio.shield`` so that the leader's
        # cancellation unwinds locally without taking down the shared poll.
        # Remaining followers still receive the result.
        return await asyncio.shield(future)

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
        start_time = asyncio.get_running_loop().time()
        current_interval = initial_interval
        consecutive_not_found = 0
        total_not_found = 0
        poll_retry_count = 0
        first_not_found_time: float | None = None
        last_status: str | None = None
        last_emitted_status: str | None = None

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
                remaining = timeout - (asyncio.get_running_loop().time() - start_time)
                if remaining <= 0:
                    raise
                poll_retry_count += 1
                backoff = min(2**poll_retry_count, 8.0, remaining)
                logger.warning(
                    "wait_for_completion: transient %s on poll #%d, retrying in %.1fs",
                    e.__class__.__name__,
                    poll_retry_count,
                    backoff,
                )
                await asyncio.sleep(backoff)
                continue

            poll_retry_count = 0  # reset on success
            last_status = status.status
            if status.status != last_emitted_status:
                last_emitted_status = status.status
                if on_status_change is not None:
                    await maybe_await_callback(on_status_change, status)

            if status.is_complete or status.is_failed:
                return status

            # Track consecutive and total "not found" responses. The API may
            # remove quota-rejected artifacts from the list entirely instead
            # of setting them to FAILED.
            if status.status == "not_found":
                consecutive_not_found += 1
                total_not_found += 1
                now = asyncio.get_running_loop().time()
                if first_not_found_time is None:
                    first_not_found_time = now
                not_found_elapsed = now - first_not_found_time

                consecutive_trigger = (
                    consecutive_not_found >= max_not_found
                    and not_found_elapsed >= min_not_found_window
                )
                total_trigger = total_not_found >= max_not_found * 2

                if consecutive_trigger or total_trigger:
                    trigger = (
                        f"consecutive={consecutive_not_found}"
                        if consecutive_trigger
                        else f"total={total_not_found}"
                    )
                    logger.warning(
                        "Artifact %s disappeared from list (%s not-found polls, "
                        "%s) — treating as failed",
                        task_id,
                        trigger,
                        f"elapsed={not_found_elapsed:.1f}s",
                    )
                    failed_status = GenerationStatus(
                        task_id=task_id,
                        status="failed",
                        error=(
                            "Generation failed: artifact was removed by the server. "
                            "This may indicate a daily quota/rate limit was exceeded, "
                            "an invalid notebook ID, or a transient API issue. "
                            "Try again later."
                        ),
                    )
                    if on_status_change is not None and last_emitted_status != "failed":
                        await maybe_await_callback(on_status_change, failed_status)
                    return failed_status
            else:
                consecutive_not_found = 0

            elapsed = asyncio.get_running_loop().time() - start_time
            if elapsed > timeout:
                raise TimeoutError(
                    f"Task {task_id} timed out after {timeout}s (last status: {last_status})"
                )

            remaining_time = timeout - elapsed
            sleep_duration = min(current_interval, remaining_time)
            if sleep_duration > 0:
                await asyncio.sleep(sleep_duration)

            current_interval = min(current_interval * 2, max_interval)


def _extract_artifact_error(art: builtins.list[Any]) -> str | None:
    """Try to extract a human-readable error from a failed artifact."""
    try:
        # art[3] — simple string error reason.
        if len(art) > 3 and isinstance(art[3], str) and art[3].strip():
            return art[3].strip()

        # art[5] — nested structure that may contain error text. This
        # position is protocol-dependent and may change without notice.
        if len(art) > 5 and isinstance(art[5], list):
            logger.debug(
                "Falling back to art[5] for error extraction (art[3]=%r)",
                art[3] if len(art) > 3 else "<missing>",
            )
            for item in art[5]:
                if isinstance(item, str) and item.strip():
                    return item.strip()
                if isinstance(item, list):
                    for sub in item:
                        if isinstance(sub, str) and sub.strip():
                            return sub.strip()

        return None
    except Exception:
        logger.warning(
            "Failed to extract error from artifact data: %r",
            art[:6] if len(art) > 6 else art,
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
        if artifact_type in _MEDIA_ARTIFACT_TYPES:
            return _extract_artifact_url(art, artifact_type) is not None

        # Non-media artifacts (Report, Quiz, Flashcard, Data Table, Mind Map):
        # Status code alone is sufficient for these types.
        return True

    except (IndexError, TypeError) as e:
        # Defensive: if structure is unexpected, be conservative for media
        # types. Media types need URLs, so return False to continue polling.
        is_media = artifact_type in _MEDIA_ARTIFACT_TYPES
        logger.debug(
            "Unexpected artifact structure for type %s (media=%s): %s",
            artifact_type,
            is_media,
            e,
        )
        return not is_media
