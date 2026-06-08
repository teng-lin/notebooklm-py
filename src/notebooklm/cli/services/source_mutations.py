"""CLI adapter for source-mutation commands — thin wrapper over ``_app``.

The ``delete`` / ``delete-by-title`` / ``rename`` / ``refresh`` / ``add-drive``
workflows, the mutation-specific source-id resolvers, the typed
:class:`SourceMutationError`, and the typed result dataclasses now live in the
transport-neutral :mod:`notebooklm._app.source_mutations`. This module is the
CLI-side adapter that:

* re-exports the typed plan/result/error/helper names so existing
  ``from ...source_mutations import ...`` imports (the command layer in
  ``cli/source_cmd.py`` and ``cli/_source_render.py``) keep resolving, and
* injects the Click-coupled :func:`validate_id` (raises ``click.ClickException``
  on empty) and the ``rich``-coupled :func:`resolve_source_id` (its
  "Matched: ..." diagnostic) into the neutral executors.

Both injected resolvers are read off **this module's** namespace at call time,
so the historical ``monkeypatch.setattr(source_mutations, "resolve_source_id",
...)`` test seam keeps landing. Command-layer rendering + exit codes live in
``cli/_source_render.py`` per ADR-0008.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from ..._app.source_mutations import (
    DriveMimeChoice,
    SourceAddDrivePlan,
    SourceAddDriveResult,
    SourceDeleteByTitlePlan,
    SourceDeleteByTitleResult,
    SourceDeletePlan,
    SourceDeleteResult,
    SourceIdResolution,
    SourceMutationError,
    SourceRefreshPlan,
    SourceRefreshResult,
    SourceRenamePlan,
    SourceRenameResult,
    build_id_ambiguity_error,
    execute_source_add_drive,
    looks_like_full_source_id,
    require_yes_in_json,
)
from ..._app.source_mutations import (
    execute_source_delete as _execute_source_delete,
)
from ..._app.source_mutations import (
    execute_source_delete_by_title as _execute_source_delete_by_title,
)
from ..._app.source_mutations import (
    execute_source_refresh as _execute_source_refresh,
)
from ..._app.source_mutations import (
    execute_source_rename as _execute_source_rename,
)
from ..._app.source_mutations import (
    resolve_source_by_exact_title as _resolve_source_by_exact_title,
)
from ..._app.source_mutations import (
    resolve_source_for_delete as _resolve_source_for_delete,
)
from ..resolve import resolve_source_id, validate_id

if TYPE_CHECKING:
    from ...client import NotebookLMClient


async def resolve_source_for_delete(
    client: NotebookLMClient, notebook_id: str, source_id: str
) -> SourceIdResolution:
    """Resolve a delete source-id input, injecting the Click ``validate_id``.

    Thin adapter over the neutral resolver that passes this module's
    :func:`validate_id` (read at call time so a ``monkeypatch.setattr`` lands).
    """
    return await _resolve_source_for_delete(client, notebook_id, source_id, validate_id=validate_id)


async def resolve_source_by_exact_title(client: NotebookLMClient, notebook_id: str, title: str):
    """Resolve a source by exact title, injecting the Click ``validate_id``."""
    return await _resolve_source_by_exact_title(client, notebook_id, title, validate_id=validate_id)


async def execute_source_delete(
    client: NotebookLMClient,
    plan: SourceDeletePlan,
    *,
    confirmer: Callable[[str], bool],
) -> SourceDeleteResult:
    """Resolve + confirm + delete a single source, injecting the Click ``validate_id``."""
    return await _execute_source_delete(client, plan, confirmer=confirmer, validate_id=validate_id)


async def execute_source_delete_by_title(
    client: NotebookLMClient,
    plan: SourceDeleteByTitlePlan,
    *,
    confirmer: Callable[[str], bool],
) -> SourceDeleteByTitleResult:
    """Resolve + confirm + delete by exact title, injecting the Click ``validate_id``."""
    return await _execute_source_delete_by_title(
        client, plan, confirmer=confirmer, validate_id=validate_id
    )


async def execute_source_rename(
    client: NotebookLMClient,
    plan: SourceRenamePlan,
) -> SourceRenameResult:
    """Resolve + rename a source, injecting the CLI ``resolve_source_id``.

    The resolver is read off this module at call time so the
    ``monkeypatch.setattr(source_mutations, "resolve_source_id", ...)`` seam
    keeps landing.
    """
    return await _execute_source_rename(client, plan, resolve_source_id=resolve_source_id)


async def execute_source_refresh(
    client: NotebookLMClient,
    plan: SourceRefreshPlan,
) -> SourceRefreshResult:
    """Resolve + refresh a source, injecting the CLI ``resolve_source_id``."""
    return await _execute_source_refresh(client, plan, resolve_source_id=resolve_source_id)


__all__ = [
    "DriveMimeChoice",
    "SourceAddDrivePlan",
    "SourceAddDriveResult",
    "SourceDeleteByTitlePlan",
    "SourceDeleteByTitleResult",
    "SourceDeletePlan",
    "SourceDeleteResult",
    "SourceIdResolution",
    "SourceMutationError",
    "SourceRefreshPlan",
    "SourceRefreshResult",
    "SourceRenamePlan",
    "SourceRenameResult",
    "build_id_ambiguity_error",
    "execute_source_add_drive",
    "execute_source_delete",
    "execute_source_delete_by_title",
    "execute_source_refresh",
    "execute_source_rename",
    "looks_like_full_source_id",
    "require_yes_in_json",
    "resolve_source_by_exact_title",
    "resolve_source_for_delete",
    "resolve_source_id",
    "validate_id",
]
