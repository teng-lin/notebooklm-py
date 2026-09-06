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

from functools import partial
from typing import Any

from ..._app.research import (
    ResearchValidationError,
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


def research_validation_message(exc: ResearchValidationError) -> str:
    """Render a neutral research conflict using the established CLI wording."""
    if exc.reason == "cited_requires_import":
        return "--cited-only requires --import-all"
    if exc.reason == "import_requires_wait":
        return (
            "--import-all requires --wait (the default), or after --no-wait a "
            "separate 'research wait --import-all' (blocks) or 'research import' "
            "(imports an already-completed run, never blocks)."
        )
    raise AssertionError(f"Unhandled research validation reason: {exc.reason}")


async def execute_research_wait(
    plan: ResearchWaitPlan,
    *,
    client: Any,
    wait_context=_null_wait_context,
    resolve_id=None,
    import_sources=None,
    json_output: bool = False,
) -> ResearchWaitResult:
    """Resolve, wait, and optionally import — injecting the CLI collaborators.

    Thin adapter over the neutral :func:`notebooklm._app.research.execute_research_wait`
    that binds the Click ``resolve_notebook_id`` and the rich-coupled
    ``import_research_sources`` defaults. The defaults are resolved from **this
    module's** globals at call time (``None`` sentinels) so the historical
    ``patch.object(services.research, "resolve_notebook_id" / "import_research_sources", ...)``
    seams land; callers may still pass explicit overrides.
    """
    bound_importer = (
        partial(
            import_research_sources if import_sources is None else import_sources,
            json_output=True,
        )
        if json_output
        else partial(
            import_research_sources if import_sources is None else import_sources,
            status_message="Importing sources...",
        )
    )
    return await _execute_research_wait(
        plan,
        client=client,
        wait_context=wait_context,
        resolve_id=partial(
            resolve_notebook_id if resolve_id is None else resolve_id,
            json_output=json_output,
        ),
        import_sources=bound_importer,
    )


__all__ = [
    "ResearchImportResult",
    "ResearchWaitOutcome",
    "ResearchWaitPlan",
    "ResearchWaitResult",
    "ResearchValidationError",
    "execute_research_wait",
    "import_research_sources",
    "resolve_notebook_id",
    "research_validation_message",
]
