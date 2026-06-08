"""Transport-neutral ``source wait`` business logic.

This is the Click-free core behind ``source wait`` (imported directly by the
``cli/source_cmd.py`` / ``cli/_source_render.py`` command layer): it owns the
source-readiness polling loop and the translation of the three
``SourceWaitError`` subclasses into a discriminated :class:`SourceWaitOutcome`.
Every transport adapter (the Click CLI today, the FastMCP server / future HTTP
later) drives this core and renders the typed outcome into its own envelope
vocabulary + exit-code policy.

The long-running wait is wrapped in a caller-supplied ``wait_context`` async
context manager so the adapter can render its own progress surface (the CLI
passes a Rich elapsed-time spinner); the neutral default is a no-op. The
caller is responsible for resolving ``plan.source_id`` to a full UUID BEFORE
calling this executor, so the adapter's progress message and JSON envelope
carry the resolved id consistently.

Typed-outcome contract (the exit policy is owned by the adapter):

* :class:`SourceWaitReady`           — source reached READY before timeout (CLI exits 0).
* :class:`SourceWaitNotFound`        — :class:`SourceNotFoundError` (CLI exits 1).
* :class:`SourceWaitProcessingError` — :class:`SourceProcessingError` (CLI exits 1).
* :class:`SourceWaitTimeout`         — :class:`SourceTimeoutError` (CLI exits 2).

This module is transport-neutral — no ``click`` / ``rich`` / ``cli`` /
``fastmcp`` imports (enforced by ``tests/_guardrails/test_app_boundary.py``).
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..types import (
    Source,
    SourceNotFoundError,
    SourceProcessingError,
    SourceTimeoutError,
)

if TYPE_CHECKING:
    from ..client import NotebookLMClient


@dataclass(frozen=True)
class SourceWaitPlan:
    """Prepared inputs for ``execute_source_wait``."""

    notebook_id: str
    source_id: str
    timeout: float
    interval: float


@dataclass(frozen=True)
class SourceWaitReady:
    """Source reached READY before timeout. Caller exits 0."""

    source: Source


@dataclass(frozen=True)
class SourceWaitNotFound:
    """``client.sources.wait_until_ready`` raised :class:`SourceNotFoundError`."""

    error: SourceNotFoundError


@dataclass(frozen=True)
class SourceWaitProcessingError:
    """``client.sources.wait_until_ready`` raised :class:`SourceProcessingError`."""

    error: SourceProcessingError


@dataclass(frozen=True)
class SourceWaitTimeout:
    """``client.sources.wait_until_ready`` raised :class:`SourceTimeoutError`."""

    error: SourceTimeoutError


SourceWaitOutcome = (
    SourceWaitReady | SourceWaitNotFound | SourceWaitProcessingError | SourceWaitTimeout
)


async def execute_source_wait(
    client: NotebookLMClient,
    plan: SourceWaitPlan,
    *,
    wait_context: Callable[[], AbstractAsyncContextManager[None]] | None = None,
) -> SourceWaitOutcome:
    """Run the ``source wait`` workflow and return a typed outcome.

    The caller is responsible for resolving ``plan.source_id`` to a full
    UUID BEFORE calling this executor (so the spinner message and the
    caller's JSON envelope carry the resolved id consistently).

    Presentation and exit-code policy live in the caller — this executor
    only owns the polling loop and exception-to-outcome mapping. The
    optional ``wait_context`` lets the adapter wrap the wait in its own
    progress surface; the neutral default is a no-op context.
    """
    try:
        context = wait_context or contextlib.nullcontext
        async with context():
            source = await client.sources.wait_until_ready(
                plan.notebook_id,
                plan.source_id,
                timeout=plan.timeout,
                initial_interval=plan.interval,
            )
    except SourceNotFoundError as exc:
        return SourceWaitNotFound(error=exc)
    except SourceProcessingError as exc:
        return SourceWaitProcessingError(error=exc)
    except SourceTimeoutError as exc:
        return SourceWaitTimeout(error=exc)
    return SourceWaitReady(source=source)


__all__ = [
    "SourceWaitNotFound",
    "SourceWaitOutcome",
    "SourceWaitPlan",
    "SourceWaitProcessingError",
    "SourceWaitReady",
    "SourceWaitTimeout",
    "execute_source_wait",
]
