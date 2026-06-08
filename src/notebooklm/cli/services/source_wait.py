"""CLI adapter for ``source wait`` — thin re-export over ``_app``.

The source-readiness polling loop, the discriminated
:class:`SourceWaitOutcome`, and its typed-exception mapping now live in the
transport-neutral :mod:`notebooklm._app.source_wait`. This module re-exports
the plan / outcome / executor names so existing
``from ...source_wait import ...`` imports (the command layer in
``cli/source_cmd.py`` and ``cli/_source_render.py``) keep resolving.

The CLI injects the ``rich``-coupled elapsed-time spinner as the
``wait_context`` at the call site; the neutral core defaults to a no-op
context. Command-layer rendering + exit codes (0=ready / 1=missing or
processing failed / 2=timeout) live in ``cli/_source_render.py`` per ADR-0008.
"""

from __future__ import annotations

from ..._app.source_wait import (
    SourceWaitNotFound,
    SourceWaitOutcome,
    SourceWaitPlan,
    SourceWaitProcessingError,
    SourceWaitReady,
    SourceWaitTimeout,
    execute_source_wait,
)

__all__ = [
    "SourceWaitNotFound",
    "SourceWaitOutcome",
    "SourceWaitPlan",
    "SourceWaitProcessingError",
    "SourceWaitReady",
    "SourceWaitTimeout",
    "execute_source_wait",
]
