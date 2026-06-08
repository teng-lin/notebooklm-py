"""Unit tests for the transport-neutral ``notebooklm._app.source_wait`` core.

These pin the relocated ``source wait`` business logic at the ``_app`` boundary
(independent of the Click adapter): :func:`execute_source_wait` runs the
readiness-poll and maps the three ``SourceWaitError`` subclasses into the
discriminated :class:`SourceWaitOutcome`:

* :class:`SourceWaitReady`           — source reached READY before timeout.
* :class:`SourceWaitNotFound`        — :class:`SourceNotFoundError`.
* :class:`SourceWaitProcessingError` — :class:`SourceProcessingError`.
* :class:`SourceWaitTimeout`         — :class:`SourceTimeoutError`.

The optional ``wait_context`` async context manager is exercised too (the CLI
passes a Rich elapsed-time spinner; the neutral default is a no-op).

Pure-service tests (no Click / CliRunner): the command-layer rendering +
exit-code policy is exercised in
``tests/unit/cli/test_source.py::TestSourceWait``.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import pytest

from notebooklm._app.source_wait import (
    SourceWaitNotFound,
    SourceWaitPlan,
    SourceWaitProcessingError,
    SourceWaitReady,
    SourceWaitTimeout,
    execute_source_wait,
)
from notebooklm.types import (
    Source,
    SourceNotFoundError,
    SourceProcessingError,
    SourceTimeoutError,
)


def _client() -> MagicMock:
    client = MagicMock()
    client.sources = MagicMock()
    return client


def _plan() -> SourceWaitPlan:
    return SourceWaitPlan(notebook_id="nb_1", source_id="src_1", timeout=30.0, interval=2.0)


@pytest.mark.asyncio
async def test_ready_outcome() -> None:
    client = _client()
    src = Source(id="src_1", title="Ready One")
    client.sources.wait_until_ready = AsyncMock(return_value=src)
    outcome = await execute_source_wait(client, _plan())
    assert isinstance(outcome, SourceWaitReady)
    assert outcome.source is src
    client.sources.wait_until_ready.assert_awaited_once_with(
        "nb_1", "src_1", timeout=30.0, initial_interval=2.0
    )


@pytest.mark.asyncio
async def test_not_found_outcome() -> None:
    client = _client()
    err = SourceNotFoundError("src_1")
    client.sources.wait_until_ready = AsyncMock(side_effect=err)
    outcome = await execute_source_wait(client, _plan())
    assert isinstance(outcome, SourceWaitNotFound)
    assert outcome.error is err


@pytest.mark.asyncio
async def test_processing_error_outcome() -> None:
    client = _client()
    err = SourceProcessingError("src_1", status=4, message="bad")
    client.sources.wait_until_ready = AsyncMock(side_effect=err)
    outcome = await execute_source_wait(client, _plan())
    assert isinstance(outcome, SourceWaitProcessingError)
    assert outcome.error is err


@pytest.mark.asyncio
async def test_timeout_outcome() -> None:
    client = _client()
    err = SourceTimeoutError("src_1", timeout=30.0)
    client.sources.wait_until_ready = AsyncMock(side_effect=err)
    outcome = await execute_source_wait(client, _plan())
    assert isinstance(outcome, SourceWaitTimeout)
    assert outcome.error is err


@pytest.mark.asyncio
async def test_wait_context_wraps_the_poll() -> None:
    client = _client()
    client.sources.wait_until_ready = AsyncMock(return_value=Source(id="src_1", title="R"))
    events: list[str] = []

    @contextlib.asynccontextmanager
    async def spinner() -> AsyncIterator[None]:
        events.append("enter")
        try:
            yield
        finally:
            events.append("exit")

    await execute_source_wait(client, _plan(), wait_context=spinner)
    # The context spans the real I/O: enter before the await, exit after.
    assert events == ["enter", "exit"]


@pytest.mark.asyncio
async def test_wait_context_exits_even_on_error() -> None:
    client = _client()
    client.sources.wait_until_ready = AsyncMock(side_effect=SourceNotFoundError("src_1"))
    events: list[str] = []

    @contextlib.asynccontextmanager
    async def spinner() -> AsyncIterator[None]:
        events.append("enter")
        try:
            yield
        finally:
            events.append("exit")

    outcome = await execute_source_wait(client, _plan(), wait_context=spinner)
    # The error is still classified, and the context still exits cleanly.
    assert isinstance(outcome, SourceWaitNotFound)
    assert events == ["enter", "exit"]
