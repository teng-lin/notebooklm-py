"""Regression tests for the `__aexit__` exception arbitration + close-leak repair.

Audit items:
- §25: `NotebookLMClient.__aexit__` lacked try/except, so a `close()` exception
  masked the body's exception (and could leave the transport open).
- §7: `client.close()` did not shield `aclose()`, so a `CancelledError` arriving
  mid-close could leak the underlying httpx client.

Coverage:
1. Body raises + close raises → body exception propagates, close logged at
   WARNING, transport closed.
2. Body succeeds + close raises → close exception propagates.
3. Cancel mid-close → transport still closed.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import patch

import pytest

from tests._helpers.client_factory import build_client_shell_for_tests

# mock-based __aexit__ arbitration tests; no HTTP, no cassette.
# Opt out of the tier-enforcement hook in tests/integration/conftest.py.
pytestmark = pytest.mark.allow_no_vcr


async def test_body_raises_and_close_raises_body_wins(
    auth_tokens,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Body's ValueError must propagate; close's RuntimeError logged + suppressed.

    Also asserts the underlying httpx transport is closed even though
    close() raised.
    """
    client = build_client_shell_for_tests(auth_tokens)

    # Capture the http client reference BEFORE entering the cm — successful
    # close sets `client._web_runtime.kernel.http_client = None`, so we need our own ref.
    await client.__aenter__()
    try:
        http_client_ref = client._web_runtime.kernel.get_http_client()
        epoch = client._collaborators.lifecycle._epoch
        generation = client._collaborators.call_supervisor._current
        assert generation is not None and generation.epoch == epoch
        assert client._web_runtime.web_transport._active_epoch == epoch
        assert client._web_runtime.kernel._active_epoch == epoch
        assert client._web_runtime.auth_coord._active_epoch == epoch

        # Patch client.close to raise after closing the transport, so we
        # exercise the exception-arbitration path. Forward to the original
        # close so the leak-shield path also runs.
        original_close = client.close

        async def _close_then_raise() -> None:
            await original_close()
            raise RuntimeError("synthetic close failure")

        with (
            patch.object(client, "close", _close_then_raise),
            caplog.at_level(logging.WARNING),
            pytest.raises(ValueError, match="user error"),
        ):
            async with client:
                # Sanity: client is open here.
                assert client._web_runtime.kernel.http_client is not None
                raise ValueError("user error")
    finally:
        await client.close()

    # 1. The body's ValueError propagated (verified by pytest.raises above).
    # 2. The close error was logged at WARNING with the suppression text.
    assert any(
        "Suppressing close() error to preserve original exception" in rec.message
        for rec in caplog.records
    ), f"expected suppression-warning in caplog; got {[r.message for r in caplog.records]}"

    # 3. The transport reference we captured before the second cm exit
    # should now be closed (the first close in our patched _close_then_raise
    # ran to completion before the synthetic raise).
    assert http_client_ref.is_closed, (
        "underlying httpx transport should be closed even when close() raised"
    )


async def test_body_succeeds_and_close_raises_close_propagates(
    auth_tokens,
) -> None:
    """No body exception → close() failure propagates as the cm exit exception."""
    client = build_client_shell_for_tests(auth_tokens)

    async def _bad_close() -> None:
        raise RuntimeError("close failed")

    try:
        with (
            patch.object(client, "close", _bad_close),
            pytest.raises(RuntimeError, match="close failed"),
        ):
            async with client:
                pass
    finally:
        await client.close()


async def test_close_process_exit_beats_body_exception(auth_tokens) -> None:
    """A cleanup process-exit signal must never be suppressed by the body error."""
    client = build_client_shell_for_tests(auth_tokens)
    process_exit = SystemExit("shutdown")

    async def _exit_close() -> None:
        raise process_exit

    try:
        with (
            patch.object(client, "close", _exit_close),
            pytest.raises(SystemExit, match="shutdown") as raised,
        ):
            await client.__aexit__(ValueError, ValueError("body failed"), None)
    finally:
        await client.close()

    assert raised.value is process_exit


async def test_cancel_mid_close_does_not_leak_transport(
    auth_tokens,
) -> None:
    """`asyncio.shield` in ``NotebookLMClient.close`` / ``ClientLifecycle.close`` keeps `aclose()` running through cancel.

    Strategy: open a client, capture the http_client ref, then call
    `client.close()` from within an outer task and cancel that outer task
    immediately. Assert the underlying transport ends up closed despite
    the cancel.
    """
    client = build_client_shell_for_tests(auth_tokens)
    await client.__aenter__()
    http_client_ref = client._web_runtime.kernel.get_http_client()
    try:
        # Wrap close() in a task so we can cancel it.
        close_task = asyncio.create_task(client.close())
        # Yield once so close() can start, then cancel.
        await asyncio.sleep(0)
        close_task.cancel()

        # The cancel may or may not propagate, depending on whether the shielded
        # aclose was already in flight. Either way the transport must end up
        # closed.
        try:
            await close_task
        except asyncio.CancelledError:
            pass

        # Give the shielded aclose bounded time to finalize. asyncio.shield
        # raises CancelledError in the outer task immediately, but the inner
        # aclose() future keeps running — poll for completion rather than
        # rely on a fixed sleep that could flake on a slow CI runner.
        for _ in range(50):  # up to ~0.5s total
            if http_client_ref.is_closed:
                break
            await asyncio.sleep(0.01)

        assert http_client_ref.is_closed, (
            "transport leaked: cancel during close left the httpx client open"
        )
    finally:
        await client.close()
