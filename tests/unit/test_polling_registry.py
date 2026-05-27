"""Unit tests for the standalone :class:`PollRegistry` class.

The live owner of artifact-polling state is
:class:`notebooklm._artifacts.ArtifactsAPI`, which constructs a
:class:`PollRegistry` directly and threads it into
:class:`notebooklm._artifact_polling.ArtifactPollingService`.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from notebooklm._polling_registry import PendingPolls, PollRegistry


async def _never() -> None:
    await asyncio.Event().wait()


def test_poll_registry_starts_empty() -> None:
    registry = PollRegistry()
    key = ("notebook-1", "task-1")

    assert registry.get(key) is None
    assert registry.pop(key) is None
    assert registry.active_tasks() == []


@pytest.mark.asyncio
async def test_poll_registry_preserves_seeded_pending_mapping_identity() -> None:
    pending: PendingPolls = {}
    registry = PollRegistry(pending)
    loop = asyncio.get_running_loop()
    future: asyncio.Future[Any] = loop.create_future()
    task = asyncio.create_task(_never())
    key = ("notebook-1", "task-1")

    try:
        registry.register(key, future, task)

        assert pending[key] == (future, task)
        assert registry.get(key) == (future, task)
        assert registry.pop(key) == (future, task)
        assert key not in pending
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
