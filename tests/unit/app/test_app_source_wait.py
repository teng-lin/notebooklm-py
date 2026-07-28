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
from notebooklm.exceptions import ValidationError
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


@pytest.mark.asyncio
async def test_wait_all_sources_maps_results_to_outcomes_in_order() -> None:
    """wait_all_sources maps each neutral ``wait_all_until_ready`` result to its typed
    outcome, in input order: a Source -> Ready, and the three RETURNED failures ->
    NotFound / ProcessingError / Timeout (#1870, single-snapshot loop)."""
    from notebooklm._app.source_wait import wait_all_sources

    ready = Source(id="s0", title="s0")
    timed_out = SourceTimeoutError("s1", timeout=1.0)
    processing = SourceProcessingError("s2", status=4)
    not_found = SourceNotFoundError("s3")

    client = MagicMock()
    client.sources.wait_all_until_ready = AsyncMock(
        return_value=[ready, timed_out, processing, not_found]
    )
    ids = ["s0", "s1", "s2", "s3"]

    outcomes = await wait_all_sources(client, "nb-1", ids, timeout=1.0, interval=0.01)

    assert isinstance(outcomes[0], SourceWaitReady) and outcomes[0].source is ready
    assert isinstance(outcomes[1], SourceWaitTimeout) and outcomes[1].error is timed_out
    assert isinstance(outcomes[2], SourceWaitProcessingError) and outcomes[2].error is processing
    assert isinstance(outcomes[3], SourceWaitNotFound) and outcomes[3].error is not_found
    client.sources.wait_all_until_ready.assert_awaited_once_with(
        "nb-1", ids, timeout=1.0, initial_interval=0.01
    )


@pytest.mark.asyncio
async def test_wait_all_sources_empty_returns_empty() -> None:
    from notebooklm._app.source_wait import wait_all_sources

    client = MagicMock()
    assert await wait_all_sources(client, "nb-1", [], timeout=1.0, interval=0.01) == []


@pytest.mark.asyncio
async def test_wait_all_sources_rejects_over_cap() -> None:
    """The shared fan-out backstop rejects > MAX_WAIT_SOURCE_IDS ids so no adapter
    path (explicit subset OR wait-all) can drift into an unbounded wait (#1871)."""
    from notebooklm._app.source_wait import MAX_WAIT_SOURCE_IDS, wait_all_sources

    client = MagicMock()
    client.sources.wait_all_until_ready = AsyncMock()
    ids = [f"s{i}" for i in range(MAX_WAIT_SOURCE_IDS + 1)]

    with pytest.raises(ValidationError, match=str(MAX_WAIT_SOURCE_IDS)):
        await wait_all_sources(client, "nb-1", ids, timeout=1.0, interval=0.01)
    # Rejected before starting any poll — the cap is a guard, not a partial run.
    client.sources.wait_all_until_ready.assert_not_awaited()


@pytest.mark.asyncio
async def test_wait_all_sources_propagates_unexpected_error() -> None:
    """An UNEXPECTED escape (e.g. an RPCError from the snapshot poll) is not one of the
    three RETURNED per-source failures, so it propagates out of the single await for the
    adapter's classify-once handler (the snapshot loop is one coroutine — no siblings to
    leak; #1870)."""
    from notebooklm._app.source_wait import wait_all_sources
    from notebooklm.exceptions import RPCError

    client = MagicMock()
    client.sources.wait_all_until_ready = AsyncMock(side_effect=RPCError("unexpected boom"))

    with pytest.raises(RPCError):
        await wait_all_sources(client, "nb-1", ["slow", "boom"], timeout=1.0, interval=0.01)


def test_validate_wait_bounds_accepts_valid() -> None:
    from notebooklm._app.source_wait import validate_wait_bounds

    validate_wait_bounds(30.0, 2.0)  # no raise
    validate_wait_bounds(0.0, 0.5)  # timeout == 0 is allowed (>= 0)


@pytest.mark.parametrize(
    ("timeout", "interval", "needle"),
    [
        (float("nan"), 2.0, "finite"),
        (float("inf"), 2.0, "finite"),
        (30.0, float("nan"), "finite"),
        (30.0, float("inf"), "finite"),
        (-1.0, 2.0, ">= 0"),
        (10_000.0, 2.0, "<="),  # over MAX_WAIT_TIMEOUT
        (30.0, 0.0, "> 0"),
        (30.0, -1.0, "> 0"),
    ],
)
def test_validate_wait_bounds_rejects(timeout: float, interval: float, needle: str) -> None:
    """Non-finite / out-of-range knobs are rejected — isfinite BEFORE the range
    guards so a NaN (which slips through every comparison) can't leak through."""
    from notebooklm._app.source_wait import validate_wait_bounds

    with pytest.raises(ValidationError, match=needle):
        validate_wait_bounds(timeout, interval)
