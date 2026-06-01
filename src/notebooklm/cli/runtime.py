"""CLI runtime primitives."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import click


def is_quiet(ctx: click.Context | None = None) -> bool:
    """Return True iff the active CLI invocation was started with ``--quiet``.

    The root group declared in ``notebooklm_cli.py`` owns the canonical
    ``--quiet`` flag. Every subcommand context that descends from it sees
    the flag on ``ctx.find_root().params["quiet"]``, which is what this
    helper reads.

    Resolution rules:

    * If ``ctx`` is provided, walk to its root and read ``params["quiet"]``.
    * If ``ctx`` is ``None``, fall back to ``click.get_current_context(
      silent=True)`` so call sites that don't already carry a context
      reference still inherit the root flag.
    * If there is no active Click context at all (e.g., a library import
      path or a standalone unit test), return False. Library importers
      must never see surprise output suppression.

    The helper is intentionally defensive: a malformed ``params`` mapping,
    a missing key, or a non-bool value all degrade to ``False`` rather than
    raising. CLI-side suppression is a quality-of-life knob; an exception
    here would convert a misconfiguration into a hard failure of every
    command, which would be far worse than printing one extra status line.
    """
    if ctx is None:
        import click

        ctx = click.get_current_context(silent=True)
        if ctx is None:
            return False
    try:
        root_params = ctx.find_root().params
    except (AttributeError, RuntimeError):
        return False
    value = root_params.get("quiet", False)
    return value if isinstance(value, bool) else False


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
