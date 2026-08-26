"""Regression coverage for the source multi-wait orchestration boundary.

``SourcesAPI.wait_for_sources`` once spawned one waiter task per source. The
semantic Source migration removes that fan-out: one facade-owned poller invokes
one ``source.wait`` snapshot read per tick and resolves every ID from that
shared notebook snapshot. With no sibling tasks, a terminal outcome cannot
leave an orphan poller behind.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from notebooklm._operations import Operation
from notebooklm._semantic.records import (
    SOURCE_WAIT_DEF,
    SourceRecord,
    SourceWaitSnapshotResult,
)
from notebooklm._sources import SourcesAPI
from notebooklm.types import SourceProcessingError
from tests._fixtures.recording_backend import RecordingBackend

pytestmark = pytest.mark.allow_no_vcr


@pytest.mark.asyncio
async def test_wait_for_sources_has_one_shared_wait_and_no_sibling_tasks() -> None:
    """One terminal row fails promptly without polling its PROCESSING sibling."""
    backend = RecordingBackend()
    backend.set_result(
        SOURCE_WAIT_DEF,
        SourceWaitSnapshotResult(
            (
                SourceRecord("bad-id", "Bad", kind="pdf", status="error"),
                SourceRecord("slow-id", "Slow", kind="pdf", status="processing"),
            )
        ),
    )
    sources = SourcesAPI(MagicMock(), uploader=MagicMock(), _backend=backend)

    single_wait = AsyncMock(side_effect=AssertionError("single-source fan-out is forbidden"))
    with patch.object(sources, "wait_until_ready", single_wait):
        started = time.monotonic()
        with pytest.raises(SourceProcessingError):
            await sources.wait_for_sources("nb_123", ["bad-id", "slow-id"])
        elapsed = time.monotonic() - started

    assert [invocation.operation for invocation in backend.invocations] == [Operation.SOURCE_WAIT]
    single_wait.assert_not_awaited()
    assert elapsed < 1.0
