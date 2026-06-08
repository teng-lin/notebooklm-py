"""CLI adapter for read-only source-content commands — thin wrapper over ``_app``.

The data-fetch services behind ``source get`` / ``fulltext`` / ``guide`` /
``stale`` and their typed plan/result pairs now live in the transport-neutral
:mod:`notebooklm._app.source_content`. This module re-exports those names under
their historical service-layer home so existing
``from ...source_content import ...`` imports in ``cli/source_cmd.py`` and
``cli/_source_render.py`` keep resolving.

Command-layer rendering + exit codes live in ``cli/source_cmd.py`` /
``cli/_source_render.py`` per ADR-0008.
"""

from __future__ import annotations

from ..._app.source_content import (
    FulltextFormat,
    SourceFulltextPlan,
    SourceFulltextResult,
    SourceGetPlan,
    SourceGetResult,
    SourceGuidePlan,
    SourceGuideResult,
    SourceStalePlan,
    SourceStaleResult,
    execute_source_fulltext,
    execute_source_get,
    execute_source_guide,
    execute_source_stale,
)

__all__ = [
    "FulltextFormat",
    "SourceFulltextPlan",
    "SourceFulltextResult",
    "SourceGetPlan",
    "SourceGetResult",
    "SourceGuidePlan",
    "SourceGuideResult",
    "SourceStalePlan",
    "SourceStaleResult",
    "execute_source_fulltext",
    "execute_source_get",
    "execute_source_guide",
    "execute_source_stale",
]
