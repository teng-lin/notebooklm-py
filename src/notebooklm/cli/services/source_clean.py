"""CLI adapter for ``source clean`` — thin wrapper over ``_app``.

The junk-source classification, batched-deletion orchestration, and the typed
:class:`SourceCleanResult` now live in the transport-neutral
:mod:`notebooklm._app.source_clean`. This module re-exports those names under
their historical service-layer home so existing
``from ...source_clean import ...`` imports — and the call-time
``source_clean_service.classify_junk_sources`` lookup in
``cli/_source_render.py`` — keep resolving.

Presentation (Rich text vs. JSON envelope), confirmation prompting, and
exit-code policy live in the Click command layer
(:mod:`notebooklm.cli.source_cmd`) per ADR-0008.
"""

from __future__ import annotations

from ..._app.source_clean import (
    CleanCandidate,
    CleanFailure,
    CleanStatus,
    SourceCleanResult,
    candidates_payload,
    classify_junk_sources,
    normalize_url_for_dedup,
    run_source_clean,
)

__all__ = [
    "CleanCandidate",
    "CleanFailure",
    "CleanStatus",
    "SourceCleanResult",
    "candidates_payload",
    "classify_junk_sources",
    "normalize_url_for_dedup",
    "run_source_clean",
]
