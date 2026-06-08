"""CLI adapter for ``research wait`` — thin wrapper over ``_app``.

The ``research wait`` orchestration (resolve → wait-for-completion → optional
import), the typed :class:`ResearchWaitPlan` / :class:`ResearchWaitResult`, and
the wait/status helpers now live in the transport-neutral
:mod:`notebooklm._app.research`. This module:

* re-exports the typed plan/result/outcome names so existing
  ``from notebooklm.cli.services.research import ...`` imports (the command
  layer in ``cli/research_cmd.py`` and ``tests/unit/test_research_service.py``)
  keep resolving, and
* injects the Click-coupled :func:`resolve_notebook_id` and the rich-coupled
  :func:`import_research_sources` as the default collaborators into the neutral
  ``execute_research_wait`` (read off **this module's** namespace at call time
  so the historical ``patch`` seams keep landing).

Task-id pinning lives in ``ResearchAPI.wait_for_completion``; this adapter
delegates the wait loop to the Python API so CLI and library callers share the
same cross-wire guard.
"""

from __future__ import annotations

from typing import Any

from ..._app.research import (
    ResearchWaitOutcome,
    ResearchWaitPlan,
    ResearchWaitResult,
    _null_wait_context,
)
from ..._app.research import (
    execute_research_wait as _execute_research_wait,
)
from ..research_import import ResearchImportResult, import_research_sources
from ..resolve import resolve_notebook_id


async def execute_research_wait(
    plan: ResearchWaitPlan,
    *,
    client: Any,
    wait_context=_null_wait_context,
    resolve_id=None,
    import_sources=None,
) -> ResearchWaitResult:
    """Resolve, wait, and optionally import — injecting the CLI collaborators.

    Thin adapter over the neutral :func:`notebooklm._app.research.execute_research_wait`
    that binds the Click ``resolve_notebook_id`` and the rich-coupled
    ``import_research_sources`` defaults. The defaults are resolved from **this
    module's** globals at call time (``None`` sentinels) so the historical
    ``patch.object(services.research, "resolve_notebook_id" / "import_research_sources", ...)``
    seams land; callers may still pass explicit overrides.
    """
    return await _execute_research_wait(
        plan,
        client=client,
        wait_context=wait_context,
        resolve_id=resolve_notebook_id if resolve_id is None else resolve_id,
        import_sources=import_research_sources if import_sources is None else import_sources,
    )


__all__ = [
    "ResearchImportResult",
    "ResearchWaitOutcome",
    "ResearchWaitPlan",
    "ResearchWaitResult",
    "execute_research_wait",
    "import_research_sources",
    "resolve_notebook_id",
]
