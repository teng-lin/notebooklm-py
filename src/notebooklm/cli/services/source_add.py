"""CLI adapter for ``source add`` — thin wrapper over ``_app``.

The source-add input detection + validation (URL SSRF guard, upload-path
checks, type detection), the typed :class:`SourceAddPlan` /
:class:`SourceAddResult`, and the add workflow now live in the
transport-neutral :mod:`notebooklm._app.source_add`. This module re-exports
those names under their historical service-layer home so existing
``from ...source_add import ...`` imports — and the call-time attribute
lookups in ``cli/source_cmd.py`` (``source_add_service.build_source_add_plan``)
and ``cli/_source_render.py`` (``source_add_service.looks_like_path`` /
``.validate_upload_path`` / ``.SourceAddValidationError``) — keep resolving.

Command-layer rendering + exit codes live in ``cli/source_cmd.py`` /
``cli/_source_render.py`` per ADR-0008.
"""

from __future__ import annotations

from ..._app.source_add import (
    SourceAddExecutionPlan,
    SourceAddFacade,
    SourceAddPlan,
    SourceAddResult,
    SourceAddType,
    SourceAddValidationError,
    add_source,
    build_source_add_plan,
    execute_source_add,
    looks_like_path,
    validate_upload_path,
    validate_url,
)

__all__ = [
    "SourceAddExecutionPlan",
    "SourceAddFacade",
    "SourceAddPlan",
    "SourceAddResult",
    "SourceAddType",
    "SourceAddValidationError",
    "add_source",
    "build_source_add_plan",
    "execute_source_add",
    "looks_like_path",
    "validate_upload_path",
    "validate_url",
]
