"""Service for ``source wait`` — long-running source-readiness poll.

Owns the dataclass + executor pair so ``cli/source_cmd.py`` stays a thin
Click handler. The executor wraps the underlying
``client.sources.wait_until_ready`` call in the caller-provided wait context
and translates the three ``SourceWaitError`` subclasses into a typed
:class:`SourceWaitOutcome`. The caller renders the outcome and decides the
exit code.

Typed-outcome contract (matches ``source_cmd.source_wait`` pre-extraction
exit policy, now owned by the caller):

* :class:`SourceWaitReady`           — source reached READY before timeout (caller exits 0).
* :class:`SourceWaitNotFound`        — :class:`SourceNotFoundError` (caller exits 1).
* :class:`SourceWaitProcessingError` — :class:`SourceProcessingError` (caller exits 1).
* :class:`SourceWaitTimeout`         — :class:`SourceTimeoutError` (caller exits 2).
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ...types import (
    Source,
    SourceNotFoundError,
    SourceProcessingError,
    SourceTimeoutError,
)

if TYPE_CHECKING:
    from ...client import NotebookLMClient


@dataclass(frozen=True)
class SourceWaitPlan:
    """Prepared inputs for ``execute_source_wait``."""

    notebook_id: str
    source_id: str
    timeout: float
    interval: float
    json_output: bool


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
    only owns the polling loop and exception-to-outcome mapping.
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
