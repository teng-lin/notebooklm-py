"""CLI runtime primitives."""

import asyncio


def run_async(coro):
    """Run async coroutine in sync context.

    Guards against being called from inside an already-running event loop.
    ``asyncio.run`` raises ``RuntimeError`` in that case ("asyncio.run() cannot
    be called from a running event loop"); we re-raise with a CLI-shaped
    message and explicitly close the coroutine first so the caller does not
    see a ``RuntimeWarning: coroutine '...' was never awaited``.

    Nested event loops are intentionally not supported (no ``nest_asyncio``,
    no ``loop.run_until_complete`` fallback): the CLI assumes a single
    top-level ``asyncio.run`` invariant.
    """
    try:
        return asyncio.run(coro)
    except RuntimeError as exc:
        # Distinguish "loop already running" from other RuntimeErrors (e.g.,
        # programmer errors inside the coroutine that surface as RuntimeError).
        # Only the running-loop case requires us to close the coroutine -- in
        # every other case ``asyncio.run`` has already driven it to completion
        # or cancellation, and calling ``close()`` would be a no-op at best
        # (and could mask a still-pending state at worst).
        if "running event loop" not in str(exc):
            raise
        coro.close()
        raise RuntimeError(
            "Cannot run sync CLI command from within an existing event loop. "
            "Use the async API (``async with NotebookLMClient(...)``) directly "
            "instead of invoking the sync CLI helper from async code."
        ) from exc
