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
    "ErrorCategory",
    "ProgressEvent",
    "ProgressSink",
    "Resolution",
    "artifact_title_to_filename",
    "build_download_plan",
    "classify",
    "execute_download",
    "resolve_ref",
    "select_artifact",
    "to_jsonable",
    "validate_id",
]
