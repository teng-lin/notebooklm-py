"""Regression test for the cancel sibling pollers on first failure.

Audit item §5: ``SourcesAPI.wait_for_sources`` fanned out to ``wait_until_ready``
calls with a bare ``asyncio.gather(*coroutines)``. When one source poll raised
immediately (e.g. ``SourceProcessingError`` on a bad ID), ``gather`` propagated
the exception but did not *await* the sibling tasks it cancelled. The cancelled
siblings were left in a "cancellation requested but not yet observed" state:
their ``CancelledError`` handlers (e.g. cleanup ``finally`` blocks) had not yet
run by the time ``wait_for_sources`` returned to its caller.

Post-fix:
- ``wait_for_sources`` creates explicit tasks via ``asyncio.create_task``.
- On any exception, it cancels every pending task and then drains them with
  ``await asyncio.gather(*tasks, return_exceptions=True)`` before re-raising.
- The public signature is unchanged.

Acceptance invariant:
  invoke ``wait_for_sources(nb, ["bad-id", "slow-id"])`` against a mock that
  errors ``bad-id`` immediately and would poll ``slow-id`` for 60 s; assert the
  slow poll is fully cancelled (its ``CancelledError`` handler has executed)
  by the time ``wait_for_sources`` returns control to the test, and the whole
  thing happens well under 1 s of wall clock.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import patch

import pytest

from notebooklm import NotebookLMClient
from notebooklm.types import Source, SourceProcessingError

# mock-based wait-for-sources cancellation tests; no HTTP, no
# cassette. Opt out of the tier-enforcement hook in tests/integration/conftest.py.
pytestmark = pytest.mark.allow_no_vcr


@pytest.mark.asyncio
async def test_wait_for_sources_cancels_sibling_on_first_failure(auth_tokens):
    """A failing poll must cancel AND drain sibling pollers before propagating.

    Bare ``asyncio.gather(*coros)`` cancels siblings on first exception but does
    NOT await them; ``gather`` returns before the cancelled siblings reach their
    ``except CancelledError`` blocks. The fix makes ``wait_for_sources`` await the
    drained siblings before re-raising, so the slow task's cancellation handler
    runs synchronously with respect to the caller.
    """
    slow_cancelled = asyncio.Event()
    slow_entered = asyncio.Event()

    async def fake_wait_until_ready(notebook_id, source_id, **kwargs):
        if source_id == "bad-id":
            # Let the slow task get scheduled before we blow up — otherwise the
            # bad task raises before slow even enters its body, and there is no
            # sibling to cancel.
            await slow_entered.wait()
            raise SourceProcessingError(source_id, status=3)

        if source_id == "slow-id":
            slow_entered.set()
            try:
                # In production this would be the long poll loop. Use 60 s to
                # mirror the spec; in practice we should never sleep this long
                # because the sibling failure must cancel us in milliseconds.
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                slow_cancelled.set()
                raise
            return Source(id=source_id, title="slow")

        raise AssertionError(f"unexpected source_id: {source_id!r}")

    async with NotebookLMClient(auth_tokens) as client:
        with patch.object(client.sources, "wait_until_ready", side_effect=fake_wait_until_ready):
            start = time.monotonic()
            with pytest.raises(SourceProcessingError):
                await asyncio.wait_for(
                    client.sources.wait_for_sources("nb_123", ["bad-id", "slow-id"]),
                    timeout=5.0,
                )
            elapsed = time.monotonic() - start

    # The bad-id failure must cancel the slow-id poll and drain it.
    assert slow_entered.is_set(), "slow-id task never started — test setup is wrong"
    assert slow_cancelled.is_set(), (
        "slow-id was not drained before wait_for_sources returned: its "
        "CancelledError handler did not run. wait_for_sources must "
        "cancel + await siblings before re-raising the first exception."
    )
    # The whole thing must complete in much less than the 60 s slow-poll budget.
    # 1 s is the spec ceiling; well under in practice.
    assert elapsed < 1.0, (
        f"wait_for_sources took {elapsed:.3f}s — siblings were not cancelled "
        "promptly on first failure"
    )
