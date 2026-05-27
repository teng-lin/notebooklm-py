"""Unit tests for the polling registry collaborator."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from _helpers.session_factory import build_session_for_tests
from notebooklm._polling_registry import PendingPolls, PollRegistry
from notebooklm.auth import AuthTokens


def _auth_tokens() -> AuthTokens:
    return AuthTokens(
        cookies={"SID": "test"},
        csrf_token="csrf",
        session_id="session",
    )


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


def test_session_exposes_poll_registry() -> None:
    core = build_session_for_tests(_auth_tokens())

    assert isinstance(core.poll_registry, PollRegistry)
    assert core.poll_registry.get(("notebook-1", "task-1")) is None
    assert core.poll_registry.active_tasks() == []


def test_session_poll_registry_identity_is_stable() -> None:
    core = build_session_for_tests(_auth_tokens())
    registry = core.poll_registry

    assert core.poll_registry is registry


@pytest.mark.asyncio
async def test_session_poll_registry_preserves_entry_shape_through_methods() -> None:
    core = build_session_for_tests(_auth_tokens())
    loop = asyncio.get_running_loop()
    future: asyncio.Future[Any] = loop.create_future()
    task = asyncio.create_task(_never())
    key = ("notebook-1", "task-1")

    try:
        core.poll_registry.register(key, future, task)

        assert core.poll_registry.get(key) == (future, task)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
