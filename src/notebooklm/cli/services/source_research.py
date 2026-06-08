"""CLI adapter for ``source add-research`` — thin wrapper over ``_app``.

The research **start → wait → optional import** workflow, the discriminated
:class:`SourceAddResearchResult`, and the flag-combination validation now live
in the transport-neutral :mod:`notebooklm._app.source_research`. This module is
the CLI-side adapter that:

* re-exports the typed plan/result/outcome names so existing
  ``from ...source_research import ...`` imports keep resolving, and
* injects the ``rich``-coupled :func:`import_research_sources` importer (which
  the ``_app`` core cannot import without breaking its boundary) into the
  neutral executor.

The importer is read off this module **at call time**, so the historical
``monkeypatch.setattr(source_research, "import_research_sources", ...)`` test
seam keeps landing. Command-layer rendering + exit codes live in
``cli/source_cmd.py`` / ``cli/_source_render.py`` per ADR-0008.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..._app.source_research import (
    SearchMode,
    SearchSource,
    SourceAddResearchOutcome,
    SourceAddResearchPlan,
    SourceAddResearchResult,
    validate_add_research_flags,
)
from ..._app.source_research import (
    execute_source_add_research as _execute_source_add_research,
)
from ..research_import import import_research_sources

if TYPE_CHECKING:
    from ...client import NotebookLMClient


async def execute_source_add_research(
    client: NotebookLMClient, plan: SourceAddResearchPlan
) -> SourceAddResearchResult:
    """Run the neutral add-research workflow with the CLI source importer.

    Thin adapter over
    :func:`notebooklm._app.source_research.execute_source_add_research` that
    injects this module's :func:`import_research_sources`. The importer is
    resolved from the module namespace on every call so a
    ``monkeypatch.setattr`` against it takes effect.
    """
    return await _execute_source_add_research(client, plan, import_sources=import_research_sources)


__all__ = [
    "SearchMode",
    "SearchSource",
    "SourceAddResearchOutcome",
    "SourceAddResearchPlan",
    "SourceAddResearchResult",
    "execute_source_add_research",
    "import_research_sources",
    "validate_add_research_flags",
]
