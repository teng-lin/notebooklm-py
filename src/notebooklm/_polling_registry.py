"""Polling registry owned by feature APIs that share leader tasks."""

from __future__ import annotations

import asyncio
from typing import Any

PollKey = tuple[str, str]
PendingPoll = tuple[asyncio.Future[Any], asyncio.Task[Any] | None]
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
        # A reserved key has no leader task until the awaited admitted spawn
        # returns. Keep the reserving waiter visible to drain during that
        # window so close cannot snapshot ``task=None`` and miss the leader
        # that would otherwise attach immediately afterwards.
        self._reservations: dict[PollKey, asyncio.Task[Any]] = {}

    def get(self, key: PollKey) -> PendingPoll | None:
        """Return the shared poll entry for ``key``, if one exists."""
        return self._pending.get(key)

    def register(
        self,
        key: PollKey,
        future: asyncio.Future[Any],
        task: asyncio.Task[Any] | None,
    ) -> None:
        """Register the leader future and poll task for ``key``."""
        self._pending[key] = (future, task)
        if task is None and (reservation := asyncio.current_task()) is not None:
            self._reservations[key] = reservation
        else:
            self._reservations.pop(key, None)

    def attach_task(self, key: PollKey, task: asyncio.Task[Any]) -> None:
        """Attach the admitted leader task to an already-reserved key."""
        future, existing = self._pending[key]
        if existing is not None:
            raise RuntimeError(f"poll task already attached for {key!r}")
        self._pending[key] = (future, task)
        self._reservations.pop(key, None)

    def pop(self, key: PollKey) -> PendingPoll | None:
        """Remove and return the shared poll entry for ``key``, if present."""
        self._reservations.pop(key, None)
        return self._pending.pop(key, None)

    def active_tasks(self) -> list[asyncio.Task[Any]]:
        """Return pending leaders and waiters reserving a leader slot.

        Used by close-time drain hooks to cancel in-flight artifact polls before
        the HTTP transport is torn down. Without this, a leader task can wake
        mid-aclose and issue a request against an already-closed client,
        surfacing as a confusing httpx error in the user's logs.

        Returns a snapshot list (not a live view) so a caller can iterate and
        cancel without mutating the underlying pending mapping mid-loop.
        A reserving waiter is included until its admitted child task attaches,
        closing the only interval in which the registry contains
        ``(future, None)``. Already-completed tasks are filtered out: they have
        nothing left to cancel, and gathering them is harmless but noisy.
        """
        leaders = [
            task for _future, task in self._pending.values() if task is not None and not task.done()
        ]
        reservations = [task for task in self._reservations.values() if not task.done()]
        return list(dict.fromkeys((*leaders, *reservations)))


__all__ = [
    "PendingPoll",
    "PendingPolls",
    "PollKey",
    "PollRegistry",
]
