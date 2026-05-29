"""Command-layer UI helpers for long-running polling operations."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator, Iterator

from .error_handler import emit_cancelled_and_exit
from .rendering import console


@contextlib.asynccontextmanager
async def status_with_elapsed(
    message: str,
    *,
    json_output: bool = False,
    resume_hint: str | None = None,
) -> AsyncIterator[None]:
    """Show a transient Rich status spinner with an elapsed-seconds counter.

    The context manager is a no-op in JSON mode so stdout remains parseable. If
    ``resume_hint`` is provided, ``KeyboardInterrupt`` is converted to the CLI's
    structured cancellation response; otherwise the interrupt propagates.
    """

    @contextlib.contextmanager
    def _sigint_guard() -> Iterator[None]:
        try:
            yield
        except KeyboardInterrupt:
            if resume_hint is None:
                raise
            emit_cancelled_and_exit(resume_hint, json_output=json_output)

    if json_output:
        with _sigint_guard():
            yield
        return

    start = time.monotonic()
    with console.status(message) as status:

        async def _ticker() -> None:
            while True:
                await asyncio.sleep(1.0)
                elapsed = int(time.monotonic() - start)
                status.update(f"{message} [{elapsed}s elapsed]")

        ticker_task = asyncio.create_task(_ticker())
        try:
            with _sigint_guard():
                yield
        finally:
            ticker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await ticker_task


__all__ = ["status_with_elapsed"]
