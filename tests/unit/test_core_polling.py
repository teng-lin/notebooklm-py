"""Unit tests for the core polling collaborator."""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from typing import Any

import pytest

from notebooklm._core import ClientCore
from notebooklm._core_polling import PendingPolls, PollRegistry
from notebooklm.auth import AuthTokens


def _auth_tokens() -> AuthTokens:
    return AuthTokens(
        cookies={"SID": "test"},
        csrf_token="csrf",
        session_id="session",
    )


async def _never() -> None:
    await asyncio.Event().wait()


def test_poll_registry_owns_pending_mapping() -> None:
    registry = PollRegistry()

    assert registry.pending == {}


def test_poll_registry_preserves_seeded_pending_mapping_identity() -> None:
    pending: PendingPolls = {}
    registry = PollRegistry(pending)

    assert registry.pending is pending


def test_client_core_exposes_poll_registry_and_pending_polls_bridge() -> None:
    core = ClientCore(_auth_tokens())

    assert isinstance(core.poll_registry, PollRegistry)
    assert core._pending_polls is core.poll_registry.pending


def test_client_core_pending_polls_assignment_replaces_registry_backing_mapping() -> None:
    core = ClientCore(_auth_tokens())
    registry = core.poll_registry
    pending: PendingPolls = {}

    core._pending_polls = pending

    assert core.poll_registry is registry
    assert core.poll_registry.pending is pending
    assert core._pending_polls is pending


def test_client_core_pending_polls_bridge_reflects_poll_registry_mutations() -> None:
    core = ClientCore(_auth_tokens())
    pending: PendingPolls = {}

    core.poll_registry.pending = pending

    assert core._pending_polls is pending


@pytest.mark.asyncio
async def test_client_core_pending_polls_bridge_preserves_entry_shape() -> None:
    core = ClientCore(_auth_tokens())
    loop = asyncio.get_running_loop()
    future: asyncio.Future[Any] = loop.create_future()
    task = asyncio.create_task(_never())
    key = ("notebook-1", "task-1")

    try:
        core._pending_polls[key] = (future, task)

        assert core.poll_registry.pending[key] == (future, task)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


def test_core_polling_does_not_import_client_core_at_runtime() -> None:
    source = (Path(__file__).resolve().parents[2] / "src/notebooklm/_core_polling.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)

    forbidden_imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in {"_core", "notebooklm._core"}:
            forbidden_imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            forbidden_imports.extend(
                alias.name for alias in node.names if alias.name == "notebooklm._core"
            )

    assert forbidden_imports == []
