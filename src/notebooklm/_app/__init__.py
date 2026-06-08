"""Transport-neutral application layer for notebooklm-py.

``_app`` holds the **business logic** that is shared between transport
adapters — the Click CLI, the FastMCP server, and any future HTTP surface.
Code in this package MUST stay free of any transport dependency: no
``click``, no ``rich``, no ``notebooklm.cli`` import, no ``fastmcp`` (the
boundary is enforced by ``tests/_guardrails/test_app_boundary.py``).

Each adapter is a thin shell that:

* parses its own inputs into the typed request/plan objects defined here,
* calls the neutral logic (raising the public ``notebooklm.exceptions``
  hierarchy on failure), and
* renders the typed result into its own envelope vocabulary.

The Wave-0 surface is the four foundation primitives every adapter needs:

* :func:`~notebooklm._app.serialize.to_jsonable` — recursive JSON-able
  conversion of dataclasses / enums / datetimes / bytes / containers.
* :func:`~notebooklm._app.errors.classify` — class-sensitive
  exception → :class:`~notebooklm._app.errors.ClassifiedError` mapping that
  each adapter projects onto its own code table.
* :func:`~notebooklm._app.resolve.validate_id` and
  :func:`~notebooklm._app.resolve.resolve_ref` — Click-free id validation
  and partial-id resolution.
* :class:`~notebooklm._app.events.ProgressEvent` /
  :class:`~notebooklm._app.events.ProgressSink` — a transport-neutral
  progress-reporting seam for long-running operations.
"""

from __future__ import annotations

from .download import (
    FORMAT_EXTENSIONS,
    ArtifactDict,
    DownloadOutcome,
    DownloadPlan,
    DownloadPlanValidationError,
    DownloadResult,
    DownloadTypeSpec,
    artifact_title_to_filename,
    build_download_plan,
    execute_download,
    select_artifact,
)
from .errors import ClassifiedError, ErrorCategory, classify
from .events import ProgressEvent, ProgressSink
from .resolve import AmbiguousIdError, Resolution, resolve_ref, validate_id
from .serialize import to_jsonable
from .source_listing import fetch_sources
from .source_mutations import (
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
    execute_source_delete,
    execute_source_delete_by_title,
    execute_source_refresh,
    execute_source_rename,
    looks_like_full_source_id,
    require_yes_in_json,
    resolve_source_by_exact_title,
    resolve_source_for_delete,
)
from .source_wait import (
    SourceWaitNotFound,
    SourceWaitOutcome,
    SourceWaitPlan,
    SourceWaitProcessingError,
    SourceWaitReady,
    SourceWaitTimeout,
    execute_source_wait,
)

__all__ = [
    "FORMAT_EXTENSIONS",
    "AmbiguousIdError",
    "ArtifactDict",
    "ClassifiedError",
    "DownloadOutcome",
    "DownloadPlan",
    "DownloadPlanValidationError",
    "DownloadResult",
    "DownloadTypeSpec",
    "DriveMimeChoice",
    "ErrorCategory",
    "ProgressEvent",
    "ProgressSink",
    "Resolution",
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
    "SourceWaitNotFound",
    "SourceWaitOutcome",
    "SourceWaitPlan",
    "SourceWaitProcessingError",
    "SourceWaitReady",
    "SourceWaitTimeout",
    "artifact_title_to_filename",
    "build_download_plan",
    "build_id_ambiguity_error",
    "classify",
    "execute_download",
    "execute_source_add_drive",
    "execute_source_delete",
    "execute_source_delete_by_title",
    "execute_source_refresh",
    "execute_source_rename",
    "execute_source_wait",
    "fetch_sources",
    "looks_like_full_source_id",
    "require_yes_in_json",
    "resolve_ref",
    "resolve_source_by_exact_title",
    "resolve_source_for_delete",
    "select_artifact",
    "to_jsonable",
    "validate_id",
]
