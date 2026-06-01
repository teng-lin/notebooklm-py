"""Polling registry owned by feature APIs that share leader tasks."""

from __future__ import annotations

import asyncio
from typing import Any

PollKey = tuple[str, str]
PendingPoll = tuple[asyncio.Future[Any], asyncio.Task[Any]]
PendingPolls = dict[PollKey, PendingPoll]


class PollRegistry:
    """Leader/follower polling-dedupe registry for artifact waits.

    Keys are ``(notebook_id, task_id)`` pairs. Values stay in the legacy
    ``(future, task)`` shape because ``ArtifactsAPI.wait_for_completion`` owns
    the poll loop and cleanup behavior.

    The first waiter for a key is the leader and stores the shared future plus
    the running poll task. Followers attach to that future via
    ``asyncio.shield`` so per-caller cancellation does not cancel the shared
    poll. The task reference is retained alongside the future so the running
    poll cannot be garbage-collected if the leader is cancelled before
    followers attach. This registry is per owning feature API, never
    module-global.
    """

    def __init__(self, pending: PendingPolls | None = None) -> None:
        self._pending: PendingPolls = pending if pending is not None else {}

    def get(self, key: PollKey) -> PendingPoll | None:
        """Return the shared poll entry for ``key``, if one exists."""
        return self._pending.get(key)

    def register(
        self,
        key: PollKey,
        future: asyncio.Future[Any],
        task: asyncio.Task[Any],
    ) -> None:
        """Register the leader future and poll task for ``key``."""
        self._pending[key] = (future, task)

    def pop(self, key: PollKey) -> PendingPoll | None:
        """Remove and return the shared poll entry for ``key``, if present."""
        return self._pending.pop(key, None)

    def active_tasks(self) -> list[asyncio.Task[Any]]:
        """Return the currently-pending leader poll tasks.

        Used by close-time drain hooks to cancel in-flight artifact polls before
        the HTTP transport is torn down. Without this, a leader task can wake
        mid-aclose and issue a request against an already-closed client,
        surfacing as a confusing httpx error in the user's logs.

        Returns a snapshot list (not a live view) so a caller can iterate and
        cancel without mutating the underlying pending mapping mid-loop.
        Already-completed tasks are filtered out: they have nothing left to
        cancel, and asking ``asyncio.gather`` to await an already-done task is
        harmless but noisy in the cancellation path.
        """
        return [task for _future, task in self._pending.values() if not task.done()]


__all__ = [
    "PendingPoll",
    "PendingPolls",
    "PollKey",
    "PollRegistry",
]
