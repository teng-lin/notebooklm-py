"""Polling collaborators for :mod:`notebooklm._core`."""

from __future__ import annotations

import asyncio
from typing import Any

PollKey = tuple[str, str]
PendingPoll = tuple[asyncio.Future[Any], asyncio.Task[Any]]
PendingPolls = dict[PollKey, PendingPoll]


class PollRegistry:
    """Leader/follower polling-dedupe registry for artifact waits.

    Keys are ``(notebook_id, task_id)`` pairs. Values stay in the legacy
    ``(future, task)`` shape because ``ArtifactsAPI.wait_for_completion`` still
    owns the poll loop and cleanup behavior in this phase.

    The first waiter for a key is the leader and stores the shared future plus
    the running poll task. Followers attach to that future via
    ``asyncio.shield`` so per-caller cancellation does not cancel the shared
    poll. The task reference is retained alongside the future so the running
    poll cannot be garbage-collected if the leader is cancelled before
    followers attach. This registry is per ``ClientCore`` instance, never
    module-global.
    """

    def __init__(self, pending: PendingPolls | None = None) -> None:
        self.pending: PendingPolls = pending if pending is not None else {}
