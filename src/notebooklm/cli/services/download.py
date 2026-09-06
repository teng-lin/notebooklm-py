"""CLI adapter over the transport-neutral download core (ADR-0008 service for
``cli/download_cmd.py``).

The download **business logic** lives in :mod:`notebooklm._app.download` (the
plan/result types, validation, artifact selection, single-vs-``--all`` dispatch,
conflict resolution). This module is the thin CLI adapter on top of it: it

* re-exports the neutral plan types under their historical service names so
  ``cli/_download_specs.py`` and the unit suite keep importing them from here,
* injects the CLI's ``cli.resolve``-backed notebook / partial-artifact-id
  resolvers (kept patchable at ``services.download.resolve_notebook_id`` for the
  service-layer tests), and
* projects the typed :class:`~notebooklm._app.download.DownloadResult` back onto
  the historical envelope **dict** the Click handler renders / serialises (via
  :func:`build_download_envelope`), so the ``--json`` output stays byte-stable.

The split keeps no Click decorators here — Click integration lives in
:mod:`notebooklm.cli.download_cmd`, which builds each leaf from a
:class:`DownloadTypeSpec`.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..._app.download import (
    FORMAT_EXTENSIONS,
    ArtifactDict,
    DownloadEvent,
    DownloadEventSink,
    DownloadFailure,
    DownloadOutcome,
    DownloadPlan,
    DownloadPlanValidationError,
    DownloadResult,
    DownloadTypeSpec,
)
from ..._app.download import build_download_plan as _build_download_plan_core
from ..._app.download import execute_download as _execute_download_core
from ...types import Artifact
from ..download_helpers import resolve_partial_artifact_id
from ..resolve import require_notebook, resolve_notebook_id

# Re-exported for the historical import surface
# (``cli/_download_specs.py`` and the unit suite still import these from here).
__all__ = [
    "FORMAT_EXTENSIONS",
    "Artifact",
    "ArtifactDict",
    "DownloadPlan",
    "DownloadPlanValidationError",
    "DownloadResult",
    "DownloadTypeSpec",
    "build_download_envelope",
    "build_download_plan",
    "download_validation_message",
    "execute_download",
    "require_notebook",
    "resolve_notebook_id",
]


def download_validation_message(exc: DownloadPlanValidationError) -> str:
    """Render a semantic validation reason using the established CLI wording."""
    if exc.reason == "missing_notebook":
        return "notebook_id is required"
    if exc.reason == "conflicting_overwrite_policy":
        return "Cannot specify both --force and --no-clobber"
    if exc.reason == "conflicting_selection_order":
        return "Cannot specify both --latest and --earliest"
    if exc.reason == "all_with_artifact":
        return "Cannot specify both --all and --artifact"
    if exc.reason == "unsupported_format":
        return (
            f"Invalid {exc.format_parameter} {exc.format_choice!r}; "
            f"expected one of {list(exc.supported_formats)}"
        )
    raise AssertionError(f"Unhandled download validation reason: {exc.reason}")


def _download_failure_message(failure: DownloadFailure) -> str:
    """Render a semantic failure using the legacy CLI error string."""
    if failure.reason == "no_artifacts":
        return f"No completed {failure.artifact_type} artifacts found"
    if failure.reason == "name_not_found":
        return (
            f"No artifacts matching '{failure.name}'. "
            f"Available: {', '.join(failure.available_titles)}"
        )
    if failure.reason == "file_exists":
        return f"File exists: {failure.path}"
    if failure.reason == "authentication":
        return f"Authentication error: {failure.detail}"
    return failure.detail or failure.reason.replace("_", " ")


def build_download_envelope(result: DownloadResult) -> dict[str, Any]:
    """Project a typed :class:`DownloadResult` onto the historical ``--json`` envelope.

    The transport-neutral ``_app`` core returns the typed result; this CLI
    adapter owns the dict shaping (§11: no envelope builders in ``_app``). Key
    ordering and value types match the dicts the pre-relocation
    ``execute_download`` returned byte-for-byte, so the CLI ``--json`` output
    stays stable for scripts that parse it.
    """
    if result.outcome is DownloadOutcome.NO_ARTIFACTS:
        failure = result.failure
        assert failure is not None
        return {
            "error": _download_failure_message(failure),
            "suggestion": f"Generate one with: notebooklm generate {failure.artifact_type}",
        }

    if result.outcome is DownloadOutcome.ERROR:
        failure = result.failure
        assert failure is not None
        envelope: dict[str, Any] = {"error": _download_failure_message(failure)}
        if result.artifact is not None:
            envelope["artifact"] = result.artifact
        if failure.reason == "file_exists":
            envelope["suggestion"] = "Use --force to overwrite or choose a different path"
        return envelope

    if result.outcome is DownloadOutcome.ALL_DRY_RUN:
        return {
            "dry_run": True,
            "operation": "download_all",
            "count": result.count,
            "output_dir": result.output_dir,
            "artifacts": [dict(a) for a in result.artifacts],
        }

    if result.outcome is DownloadOutcome.ALL_EXECUTED:
        envelope = {
            "operation": "download_all",
            "output_dir": result.output_dir,
            "total": result.total,
            "succeeded_count": result.succeeded_count,
            "failed_count": result.failed_count,
            "skipped_count": result.skipped_count,
            "artifacts": [dict(a) for a in result.artifacts],
        }
        if result.is_failure:
            envelope["error"] = True
        if result.failure is not None and result.failure.reason == "authentication":
            envelope["code"] = "AUTH_ERROR"
            envelope["message"] = _download_failure_message(result.failure)
            envelope["hint"] = "Run 'notebooklm login' to re-authenticate."
        return envelope

    if result.outcome is DownloadOutcome.SINGLE_DRY_RUN:
        return {
            "dry_run": True,
            "operation": "download_single",
            "artifact": result.artifact,
            "output_path": result.output_path,
        }

    # SINGLE_DOWNLOADED
    return {
        "operation": "download_single",
        "artifact": result.artifact,
        "output_path": result.output_path,
        "status": "downloaded",
    }


def build_download_plan(
    config: DownloadTypeSpec,
    args: dict[str, Any],
    cwd: Path | None = None,
) -> DownloadPlan:
    """CLI ``build_download_plan``: inject ``require_notebook`` then delegate.

    Wraps :func:`notebooklm._app.download.build_download_plan`, supplying the
    CLI's ``require_notebook`` as the ``notebook_required`` hook so the
    notebook-id env-var / active-context fallback (and the no-notebook
    diagnostic) still apply — exactly as the pre-relocation service did. The
    flag-conflict checks run first inside the core, preserving the historical
    error precedence (a flag conflict surfaces before the no-notebook error).
    """
    return _build_download_plan_core(config, args, cwd, notebook_required=require_notebook)


class _TextProgressSink(DownloadEventSink):
    """Adapt semantic download events to the established CLI progress line.

    The Click handler injects ``console.print``; the service-layer tests inject
    a ``list.append`` to capture the rendered progress lines. Both consume the
    adapter owns the Rich markup and wording; the neutral loop emits only the
    item index, total, and title.
    """

    def __init__(self, sink: Callable[[str], None]) -> None:
        self._sink = sink

    def emit(self, event: DownloadEvent) -> None:
        if event.kind != "ITEM_STARTED":  # pragma: no cover - exhaustive today
            raise AssertionError(f"Unhandled download event: {event.kind}")
        self._sink(f"[dim]Downloading {event.index}/{event.total}:[/dim] {event.title}")


async def execute_download(
    plan: DownloadPlan,
    facade: Any,
    *,
    json_output: bool = False,
    text_progress_sink: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Run the validated plan and return the historical envelope dict.

    Thin CLI adapter around :func:`notebooklm._app.download.execute_download`:
    it supplies the CLI's notebook / partial-artifact-id resolvers (looked up at
    call time so ``services.download.resolve_notebook_id`` stays patchable) and
    a :class:`DownloadEventSink` wrapping the optional ``text_progress_sink``, then
    projects the typed result back to the envelope dict via
    :func:`build_download_envelope`.

    JSON routing is owned here, not in the neutral core: in ``--json`` mode the
    adapter passes ``progress=None`` so the per-artifact progress lines are
    suppressed (keeping the JSON stream clean), and forwards ``json_output`` to
    the notebook resolver so its "Matched: ..." diagnostic routes to stderr.

    Args:
        plan: Output of :func:`build_download_plan`.
        facade: A live :class:`~notebooklm.NotebookLMClient` (or any object
            exposing ``client.artifacts`` with ``.list`` and
            ``.download_<spec.download_attr>``).
        json_output: When true, suppresses the ``--all`` progress lines
            (``progress=None``) and routes the resolver diagnostic to stderr.
        text_progress_sink: Callback invoked once per artifact in the ``--all``
            text-mode path. ``None`` (default) skips the progress line; the live
            Click handler injects ``console.print``.

    Returns:
        The download envelope dict the Click layer renders / serialises.
    """

    async def _notebook_resolver(partial_id: str) -> str:
        # Looked up via the module global so a test monkeypatching
        # ``services.download.resolve_notebook_id`` is honoured.
        return await resolve_notebook_id(facade, partial_id, json_output=json_output)

    # The adapter owns JSON routing: suppress the progress sink in --json mode so
    # the JSON stream stays clean (the neutral core no longer inspects a flag).
    progress = (
        _TextProgressSink(text_progress_sink)
        if text_progress_sink is not None and not json_output
        else None
    )

    result = await _execute_download_core(
        plan,
        facade,
        notebook_resolver=_notebook_resolver,
        artifact_resolver=resolve_partial_artifact_id,
        progress=progress,
    )
    return build_download_envelope(result)
