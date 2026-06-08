"""Transport-neutral artifact-download business logic.

This is the Click-free core of ``cli/services/download.py``: it owns the
behaviour the 9 leaf ``download <type>`` commands share — flag validation,
artifact lookup, single-vs-``--all`` dispatch, dry-run preview, conflict
resolution — and returns a **typed** :class:`DownloadResult` instead of the
adapter-shaped envelope dict. Each adapter (the Click CLI today, the FastMCP
server / future HTTP surface tomorrow) renders the typed result into its own
vocabulary; the CLI adapter rebuilds its historical ``--json`` envelope in
``cli/services/download.py::build_download_envelope`` from the typed result.

Public API: :class:`DownloadTypeSpec` (per-leaf metadata), :class:`DownloadPlan`
(one validated invocation), :class:`DownloadResult` (the typed outcome),
:func:`build_download_plan` (sync validation + assembly), :func:`execute_download`
(the download coroutine), :data:`FORMAT_EXTENSIONS`, and the pure
:func:`select_artifact` / :func:`artifact_title_to_filename` helpers re-exported
by ``cli/download_helpers.py`` for its established import seam.

The notebook-id and partial-artifact-id resolvers are **injected** as callables
(``notebook_resolver`` / ``artifact_resolver``) so this module never imports the
Click-coupled ``cli.resolve`` helpers; the CLI adapter supplies the live
resolvers and keeps its ``resolve_notebook_id`` patch seam.

This module is transport-neutral — no ``click`` / ``rich`` / ``cli`` /
``fastmcp`` imports (enforced by ``tests/_guardrails/test_app_boundary.py``).
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from functools import partial
from pathlib import Path
from typing import Any, Protocol, TypedDict

from ..exceptions import ValidationError
from ..types import Artifact, ArtifactType
from .events import ProgressEvent, ProgressSink

# Reserve space for " (999)" suffix when handling duplicate filenames.
DUPLICATE_SUFFIX_RESERVE = 7

# Format → extension map shared with the runtime extension-override path
# and the registry layer. Quiz/flashcards expose all three formats;
# slide-deck only swaps the extension between pdf and pptx via a dedicated
# mapping defined inline in its spec row.
FORMAT_EXTENSIONS: dict[str, str] = {
    "json": ".json",
    "markdown": ".md",
    "html": ".html",
}


class ArtifactDict(TypedDict):
    """Artifact structure projected from the ``client.artifacts.list`` API."""

    id: str
    title: str
    created_at: int  # Unix timestamp


@dataclass(frozen=True)
class DownloadTypeSpec:
    """Static metadata for one ``download <name>`` leaf command.

    The only axes of variation across the 9 leaf commands: ``name`` /
    ``kind`` / default ``extension`` / ``default_dir`` / the
    ``client.artifacts`` ``download_attr`` to bind, ``--help`` text, and the
    optional ``--format`` wiring (``format_choices`` / ``format_default`` /
    ``format_help`` / ``format_extension_map`` extension override /
    ``format_kwarg`` to forward / ``format_param_name`` adapter flag name /
    ``forward_format_only_if_set`` slide-deck "pptx-only" forwarding).

    ``frozen=True`` freezes only the *reference*; the ``format_extension_map``
    contents stay mutable — the registry rows are module constants treated as
    read-only by convention.
    """

    name: str
    kind: ArtifactType
    extension: str
    default_dir: str
    download_attr: str
    help_summary: str
    help_examples: str
    format_choices: tuple[str, ...] = ()
    format_default: str = ""
    format_help: str = ""
    format_extension_map: dict[str, str] = field(default_factory=dict)
    format_kwarg: str = ""
    format_param_name: str = "output_format"
    forward_format_only_if_set: bool = False


class _DownloadFacade(Protocol):
    """Subset of :class:`~notebooklm.NotebookLMClient` the executor needs.

    Kept narrow on purpose: the executor only touches ``client.artifacts``
    methods. The Protocol is structural so tests can pass a ``MagicMock``
    that mirrors the same shape without subclassing.
    """

    @property
    def artifacts(self) -> Any: ...  # ArtifactsAPI with .list + .download_<x>


# Type alias for the bound download coroutine returned by
# ``getattr(client.artifacts, attr)``.
_DownloadFn = Callable[..., Awaitable[str | None]]

#: Resolves a (possibly partial) notebook id to its full id. The CLI adapter
#: supplies its ``cli.resolve.resolve_notebook_id``-backed implementation.
NotebookResolver = Callable[[str], Awaitable[str]]

#: Resolves a (possibly partial) artifact id against a pre-fetched list to its
#: full id, raising ``ValueError`` on no-match / ambiguity. The CLI adapter
#: supplies its ``download_helpers.resolve_partial_artifact_id`` implementation.
ArtifactResolver = Callable[[list[ArtifactDict], str], str]


class DownloadPlanValidationError(ValidationError):
    """Plan-validation error raised synchronously by :func:`build_download_plan`.

    Subclasses :class:`~notebooklm.exceptions.ValidationError` so
    ``_app.errors.classify`` covers it uniformly across adapters (it classifies
    as :attr:`~notebooklm._app.errors.ErrorCategory.VALIDATION`). It keeps its
    ``message`` / ``code`` attributes so the CLI ``download_cmd`` adapter can
    project them onto the historical ``VALIDATION_ERROR`` ``--json`` code +
    exit-code policy unchanged.
    """

    def __init__(self, message: str, code: str = "VALIDATION_ERROR") -> None:
        self.message = message
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class DownloadPlan:
    """One validated download invocation.

    Built by :func:`build_download_plan` from raw adapter args; consumed by
    :func:`execute_download`. The plan carries everything the executor needs so
    the adapter layer can stay thin.

    Notes on field semantics:

    - ``notebook_id`` is the post-``require_notebook`` raw value (may still be
      a partial prefix; the executor resolves it via ``notebook_resolver``).
    - ``output_path`` is the user-supplied path, or ``None`` to derive one
      from the artifact title.
    - ``file_extension`` is the post-``--format``-adjustment extension; the
      adapter applies the override before calling :func:`build_download_plan`.
    - ``format_choice`` is the literal ``--format`` value the user picked
      (e.g. ``"pptx"``, ``"markdown"``, or ``""`` for leaves without
      ``--format``). The executor forwards it via the spec's
      ``format_kwarg`` when applicable.
    """

    spec: DownloadTypeSpec
    notebook_id: str
    output_path: str | None
    file_extension: str
    latest: bool
    earliest: bool
    download_all: bool
    name: str | None
    artifact_id: str | None
    dry_run: bool
    force: bool
    no_clobber: bool
    format_choice: str = ""
    warnings: tuple[str, ...] = ()
    # Captured at plan-build time so the executor doesn't have to re-derive it;
    # ``Path.cwd()`` at executor time would be wrong if the caller changed
    # directories between ``build_download_plan`` and the awaited
    # ``execute_download``. Defaults to the build-time cwd.
    cwd: Path = field(default_factory=Path.cwd)


class DownloadOutcome(Enum):
    """Which envelope shape a :class:`DownloadResult` represents.

    Each value maps 1:1 to one of the dict envelopes the historical
    ``execute_download`` produced; the CLI adapter's
    ``build_download_envelope`` rebuilds the exact dict for each.
    """

    #: No completed artifacts of the requested kind exist.
    NO_ARTIFACTS = "no_artifacts"
    #: A pre-download error (selection failure, ``--all --name`` no-match,
    #: single-file conflict, or a download exception).
    ERROR = "error"
    #: ``--all --dry-run`` preview.
    ALL_DRY_RUN = "all_dry_run"
    #: ``--all`` executed (one entry per artifact under ``artifacts``).
    ALL_EXECUTED = "all_executed"
    #: Single-artifact ``--dry-run`` preview.
    SINGLE_DRY_RUN = "single_dry_run"
    #: Single-artifact download succeeded.
    SINGLE_DOWNLOADED = "single_downloaded"


@dataclass(frozen=True)
class DownloadResult:
    """Typed outcome of :func:`execute_download`.

    Carries every field the historical envelope dict held so an adapter can
    rebuild it byte-for-byte (the CLI does so in its download adapter's
    ``build_download_envelope``) or project it onto its own vocabulary
    (MCP / HTTP). :attr:`outcome` discriminates the shape; only the fields
    relevant to that outcome are populated.

    This is a typed-fields-only dataclass: it exposes no envelope/``--json``
    dict builder (§11). The adapter owns the dict construction so the ``_app``
    core stays transport-neutral.
    """

    outcome: DownloadOutcome
    error: str | None = None
    suggestion: str | None = None
    artifact: dict[str, Any] | None = None
    output_path: str | None = None
    output_dir: str | None = None
    count: int | None = None
    total: int | None = None
    succeeded_count: int = 0
    failed_count: int = 0
    skipped_count: int = 0
    is_failure: bool = False
    artifacts: tuple[dict[str, Any], ...] = ()

    @property
    def has_error(self) -> bool:
        """Whether the result carries a top-level error (drives non-zero exit).

        ``True`` for the ``NO_ARTIFACTS`` / ``ERROR`` outcomes and for an
        ``ALL_EXECUTED`` outcome that had at least one per-item failure — i.e.
        exactly when the historical envelope grew a top-level ``"error"`` key.
        """
        return self.error is not None or self.is_failure


# ---------------------------------------------------------------------------
# Pure artifact-selection / filename helpers (re-exported by download_helpers).
# ---------------------------------------------------------------------------


def select_artifact(
    artifacts: list[ArtifactDict],
    latest: bool = True,
    earliest: bool = False,
    name: str | None = None,
    artifact_id: str | None = None,
) -> tuple[ArtifactDict, str]:
    """Select an artifact from a list based on criteria.

    CRITICAL: Implements Filter -> Count -> Select logic:
    1. Filter artifacts by name/artifact_id if provided
    2. Count matches (0/1/many)
    3. Apply latest/earliest to remaining matches

    Args:
        artifacts: List of artifact dicts with 'id', 'title', 'created_at'.
        latest: Select most recent (default: True).
        earliest: Select oldest (overrides latest if True).
        name: Filter by title (case-insensitive substring match).
        artifact_id: Select by exact artifact ID.

    Returns:
        Tuple of (selected_artifact, selection_reason).

    Raises:
        ValueError: If no match, invalid criteria, or both latest+earliest.
    """
    if not artifacts:
        raise ValueError("No artifacts found")

    if latest and earliest:
        raise ValueError("Cannot specify both --latest and --earliest")

    filtered = artifacts

    if artifact_id:
        filtered = [a for a in artifacts if a["id"] == artifact_id]
        if not filtered:
            raise ValueError(f"Artifact {artifact_id} not found")
        return filtered[0], f"matched by ID: {artifact_id}"

    if name:
        name_lower = name.lower()
        filtered = [a for a in artifacts if name_lower in a["title"].lower()]
        if not filtered:
            raise ValueError(
                f"No artifacts matching '{name}'. "
                f"Available: {', '.join(a['title'] for a in artifacts)}"
            )

    count = len(filtered)

    if count == 1:
        reason = "matched by name" if name else "only artifact"
        return filtered[0], reason

    if earliest:
        selected = min(filtered, key=lambda a: a["created_at"])
        return selected, f"earliest of {count} artifacts"
    selected = max(filtered, key=lambda a: a["created_at"])
    return selected, f"latest of {count} artifacts"


def artifact_title_to_filename(
    title: str,
    extension: str,
    existing_files: set[str],
    max_length: int = 240,  # Leave room for extension and (N) suffix
) -> str:
    """Convert artifact title to a safe, unique filename.

    Args:
        title: Artifact title.
        extension: File extension (with leading dot, e.g., ".mp3").
        existing_files: Set of filenames already used.
        max_length: Maximum filename length before extension.

    Returns:
        Sanitized filename with extension.
    """
    # Sanitize: replace invalid chars (/ \ : * ? " < > |) with underscore.
    sanitized = re.sub(r'[/\\:*?"<>|]', "_", title)
    sanitized = sanitized.strip(". ")

    if not sanitized:
        sanitized = "untitled"

    effective_max = max_length - DUPLICATE_SUFFIX_RESERVE
    if len(sanitized) > effective_max:
        sanitized = sanitized[:effective_max].rstrip(". ")

    base = sanitized
    filename = f"{base}{extension}"

    counter = 2
    while filename in existing_files:
        filename = f"{base} ({counter}){extension}"
        counter += 1

    return filename


# ---------------------------------------------------------------------------
# Plan building.
# ---------------------------------------------------------------------------


def _resolve_format_extension(
    spec: DownloadTypeSpec,
    output_path: str | None,
    format_choice: str,
    *,
    download_all: bool = False,
) -> tuple[str, tuple[str, ...]]:
    """Compute the effective extension given the spec + user's ``--format``.

    Matches the historical wiring exactly:

    - slide-deck pdf → ``.pdf``, slide-deck pptx → ``.pptx`` (emits the
      "output path does not end with .pptx" warning on mismatch).
    - quiz/flashcards json → ``.json``, markdown → ``.md``, html → ``.html``
      (emits the corresponding warning on mismatch with the user-supplied
      ``output_path``).
    - leaves with no ``--format`` flag → ``spec.extension`` unchanged.

    A mismatch warning is returned with the extension so the adapter can render
    it. The warning is suppressed when ``download_all`` is true because the
    user-supplied path then names a destination *directory* (not a file), so an
    extension check is meaningless and the warning would be a false positive.
    """
    if not spec.format_choices:
        return spec.extension, ()
    effective_ext = spec.format_extension_map.get(format_choice, spec.extension)
    if output_path and not download_all and not output_path.endswith(effective_ext):
        return (
            effective_ext,
            (
                f"Warning: output path '{output_path}' does not end with "
                f"'{effective_ext}' but --format {format_choice} was requested.",
            ),
        )
    return effective_ext, ()


def _identity_notebook(notebook_id: str | None) -> str:
    """Default ``notebook_required`` hook: pass the value straight through.

    Neutral adapters (MCP/HTTP) supply ``notebook_id`` explicitly, so no
    env-var / active-context fallback applies; the CLI injects
    ``require_notebook`` for that fallback (and its no-notebook diagnostic).

    Raises:
        DownloadPlanValidationError: if no notebook id was supplied — the
            neutral path has no context to fall back on.
    """
    if not notebook_id:
        raise DownloadPlanValidationError("notebook_id is required")
    return notebook_id


def build_download_plan(
    config: DownloadTypeSpec,
    args: dict[str, Any],
    cwd: Path | None = None,
    *,
    notebook_required: Callable[[str | None], str] = _identity_notebook,
) -> DownloadPlan:
    """Validate + assemble a :class:`DownloadPlan` from raw adapter args.

    Synchronous: rejects flag conflicts with :class:`DownloadPlanValidationError`
    and captures the (possibly still-partial) notebook id. Does NOT perform any
    async resolution — that runs inside :func:`execute_download` via the
    injected resolvers.

    Args:
        config: One ``DownloadTypeSpec`` row from the registry.
        args: Raw adapter kwargs (``output_path``, ``notebook_id``, ``latest``,
            ``earliest``, ``download_all``, ``name``, ``artifact_id``,
            ``dry_run``, ``force``, ``no_clobber``,
            optionally ``slide_format`` / ``output_format``). A ``json_output``
            key is ignored — JSON routing is the adapter's concern, not the
            plan's.
        cwd: The working directory to capture for derived-output-path
            resolution. ``None`` falls back to ``Path.cwd()`` at call time.
        notebook_required: Hook applied to ``args["notebook_id"]`` **after** the
            flag-conflict checks (order preserved from the historical CLI path).
            The CLI injects ``require_notebook`` so the env-var / active-context
            fallback + no-notebook diagnostic still fire; the default passes the
            value through unchanged.

    Returns:
        Frozen ``DownloadPlan`` ready for :func:`execute_download`.

    Raises:
        DownloadPlanValidationError: when flag combinations conflict.
    """
    if args.get("force") and args.get("no_clobber"):
        raise DownloadPlanValidationError("Cannot specify both --force and --no-clobber")
    if args.get("latest") and args.get("earliest"):
        raise DownloadPlanValidationError("Cannot specify both --latest and --earliest")
    if args.get("download_all") and args.get("artifact_id"):
        raise DownloadPlanValidationError("Cannot specify both --all and --artifact")

    nb_id = notebook_required(args.get("notebook_id"))

    # Format-choice extraction. The adapter param name is data-driven via
    # ``spec.format_param_name`` (default ``"output_format"``, slide-deck
    # overrides to ``"slide_format"``). Leaves with no ``--format`` flag have
    # empty ``format_choices``.
    format_choice = ""
    if config.format_choices:
        format_choice = (
            args.get(config.format_param_name, config.format_default) or config.format_default
        )
        # Fail fast on an unknown format rather than silently falling back to the
        # default extension (the CLI validates via a Click ``Choice``; a non-CLI
        # adapter has no such guard).
        if format_choice not in config.format_choices:
            raise DownloadPlanValidationError(
                f"Invalid {config.format_param_name} {format_choice!r}; "
                f"expected one of {list(config.format_choices)}"
            )

    file_extension, warnings = _resolve_format_extension(
        config,
        output_path=args.get("output_path"),
        format_choice=format_choice,
        download_all=bool(args.get("download_all", False)),
    )

    return DownloadPlan(
        spec=config,
        notebook_id=nb_id,
        output_path=args.get("output_path"),
        file_extension=file_extension,
        latest=bool(args.get("latest", False)),
        earliest=bool(args.get("earliest", False)),
        download_all=bool(args.get("download_all", False)),
        name=args.get("name"),
        artifact_id=args.get("artifact_id"),
        dry_run=bool(args.get("dry_run", False)),
        force=bool(args.get("force", False)),
        no_clobber=bool(args.get("no_clobber", False)),
        format_choice=format_choice,
        warnings=warnings,
        cwd=cwd if cwd is not None else Path.cwd(),
    )


# ---------------------------------------------------------------------------
# Execution.
# ---------------------------------------------------------------------------


async def _get_completed_artifacts_as_dicts(
    facade: _DownloadFacade, notebook_id: str, spec: DownloadTypeSpec
) -> list[ArtifactDict]:
    """Fetch artifacts, filter by kind + completion, project to ArtifactDict.

    The ``isinstance(a, Artifact)`` guard mirrors the legacy implementation and
    protects against a heterogeneous list with stub entries that don't expose
    the ``kind`` / ``is_completed`` properties.
    """
    all_artifacts = await facade.artifacts.list(notebook_id)
    return [
        {
            "id": a.id,
            "title": a.title,
            "created_at": int(a.created_at.timestamp()) if a.created_at else 0,
        }
        for a in all_artifacts
        if isinstance(a, Artifact) and a.kind == spec.kind and a.is_completed
    ]


def _resolve_conflict(
    path: Path, *, force: bool, no_clobber: bool
) -> tuple[Path | None, dict[str, Any] | None]:
    """Resolve a per-file conflict per the user's --force / --no-clobber choice.

    Returns ``(final_path, skip_info)`` where exactly one of the two is non-None.
    """
    if not path.exists():
        return path, None
    if no_clobber:
        return None, {"status": "skipped", "reason": "file exists", "path": str(path)}
    if not force:
        # Auto-rename: append " (2)", " (3)", … until free.
        counter = 2
        base_name = path.stem
        parent = path.parent
        ext = path.suffix
        while path.exists():
            path = parent / f"{base_name} ({counter}){ext}"
            counter += 1
    return path, None


def _bind_download_fn(plan: DownloadPlan, facade: _DownloadFacade) -> _DownloadFn:
    """Bind the spec's ``download_attr`` coroutine, partialing the format kwarg
    when the spec requests it.

    Three branches:

    - No ``format_kwarg`` (audio/video/report/mind-map/data-table/infographic
      and the slide-deck ``--format pdf`` default): use the bare attr.
    - ``forward_format_only_if_set`` AND the user picked the non-default
      (slide-deck pptx): partial-bind with the format kwarg.
    - Always-forward (quiz/flashcards): partial-bind regardless.
    """
    spec = plan.spec
    base_fn = getattr(facade.artifacts, spec.download_attr, None)
    if base_fn is None:
        raise ValueError(f"Unknown artifact download method: {spec.download_attr}")
    if not spec.format_kwarg:
        return base_fn
    if spec.forward_format_only_if_set:
        if plan.format_choice == spec.format_default:
            return base_fn
        return partial(base_fn, **{spec.format_kwarg: plan.format_choice})
    return partial(base_fn, **{spec.format_kwarg: plan.format_choice})


async def _execute_download_all(
    plan: DownloadPlan,
    facade: _DownloadFacade,
    type_artifacts: list[ArtifactDict],
    nb_id_resolved: str,
    download_fn: _DownloadFn,
    *,
    progress: ProgressSink | None = None,
) -> DownloadResult:
    """Execute the ``--all`` branch: filter by name, dry-run preview, download.

    Per-artifact progress (``Downloading 1/N: <title>``) is emitted into the
    optional :class:`ProgressSink` so the adapter renders it in its own
    surface. The adapter owns JSON routing: it passes ``progress=None`` when it
    wants a clean JSON stream, so this core never inspects a presentation flag.

    Relative output paths (both the user-supplied ``plan.output_path`` and the
    spec's ``default_dir`` fallback like ``"./audio"``) are resolved against
    ``plan.cwd`` — the directory the user invoked the CLI from — not the process
    cwd at executor-await time. Absolute paths pass through unchanged.
    """
    raw = Path(plan.output_path) if plan.output_path else Path(plan.spec.default_dir)
    output_dir = raw if raw.is_absolute() else plan.cwd / raw

    # --name filter (case-insensitive substring) applied before previewing.
    if plan.name:
        name_lower = plan.name.lower()
        filtered = [a for a in type_artifacts if name_lower in a["title"].lower()]
        if not filtered:
            return DownloadResult(
                outcome=DownloadOutcome.ERROR,
                error=(
                    f"No artifacts matching '{plan.name}'. "
                    f"Available: {', '.join(a['title'] for a in type_artifacts)}"
                ),
            )
        type_artifacts = filtered

    # Pre-compute filenames so dry-run and execution agree on duplicates.
    planned_filenames: list[str] = []
    existing_names: set[str] = set()
    for artifact in type_artifacts:
        item_name = artifact_title_to_filename(
            artifact["title"],
            plan.file_extension,
            existing_names,
        )
        existing_names.add(item_name)
        planned_filenames.append(item_name)

    if plan.dry_run:
        return DownloadResult(
            outcome=DownloadOutcome.ALL_DRY_RUN,
            count=len(type_artifacts),
            output_dir=str(output_dir),
            artifacts=tuple(
                {"id": a["id"], "title": a["title"], "filename": item_name}
                for a, item_name in zip(type_artifacts, planned_filenames, strict=True)
            ),
        )

    output_dir.mkdir(parents=True, exist_ok=True)

    artifacts_results: list[dict[str, Any]] = []
    total = len(type_artifacts)
    succeeded_count = 0
    failed_count = 0
    skipped_count = 0

    for i, (artifact, item_name) in enumerate(
        zip(type_artifacts, planned_filenames, strict=True), 1
    ):
        if progress is not None:
            progress.emit(
                ProgressEvent(
                    message=f"[dim]Downloading {i}/{total}:[/dim] {artifact['title']}",
                    kind="download",
                    pct=i / total if total else None,
                )
            )

        item_path = output_dir / item_name
        resolved_path, skip_info = _resolve_conflict(
            item_path, force=plan.force, no_clobber=plan.no_clobber
        )
        if skip_info or resolved_path is None:
            artifacts_results.append(
                {
                    "id": artifact["id"],
                    "title": artifact["title"],
                    "filename": item_name,
                    **(skip_info or {"status": "skipped", "reason": "conflict resolution failed"}),
                }
            )
            skipped_count += 1
            continue

        item_path = resolved_path
        item_name = item_path.name

        try:
            await download_fn(nb_id_resolved, str(item_path), artifact_id=str(artifact["id"]))
            artifacts_results.append(
                {
                    "id": artifact["id"],
                    "title": artifact["title"],
                    "filename": item_name,
                    "path": str(item_path),
                    "status": "downloaded",
                }
            )
            succeeded_count += 1
        except Exception as e:
            artifacts_results.append(
                {
                    "id": artifact["id"],
                    "title": artifact["title"],
                    "filename": item_name,
                    "status": "failed",
                    "error": str(e),
                }
            )
            failed_count += 1

    # ANY per-item failure surfaces a non-zero exit. The adapter keys exit-code
    # policy on ``has_error``; the historical envelope grew a top-level "error"
    # only when there were failures.
    return DownloadResult(
        outcome=DownloadOutcome.ALL_EXECUTED,
        output_dir=str(output_dir),
        total=total,
        succeeded_count=succeeded_count,
        failed_count=failed_count,
        skipped_count=skipped_count,
        is_failure=failed_count > 0,
        artifacts=tuple(artifacts_results),
    )


async def _execute_download_single(
    plan: DownloadPlan,
    facade: _DownloadFacade,
    type_artifacts: list[ArtifactDict],
    nb_id_resolved: str,
    download_fn: _DownloadFn,
    artifact_resolver: ArtifactResolver,
) -> DownloadResult:
    """Execute the single-artifact branch: select → dry-run | conflict | download."""
    try:
        resolved_artifact_id = (
            artifact_resolver(type_artifacts, plan.artifact_id) if plan.artifact_id else None
        )
        selected, reason = select_artifact(
            type_artifacts,
            latest=plan.latest,
            earliest=plan.earliest,
            name=plan.name,
            artifact_id=resolved_artifact_id,
        )
    except ValueError as e:
        return DownloadResult(outcome=DownloadOutcome.ERROR, error=str(e))

    if not plan.output_path:
        safe_name = artifact_title_to_filename(
            str(selected["title"]),
            plan.file_extension,
            set(),
        )
        final_path = plan.cwd / safe_name
    else:
        # Resolve relative paths against plan.cwd so the build-time directory
        # wins over the process cwd at executor-await time. Absolute paths pass
        # through unchanged.
        raw = Path(plan.output_path)
        final_path = raw if raw.is_absolute() else plan.cwd / raw

    selected_envelope = {
        "id": selected["id"],
        "title": selected["title"],
        "selection_reason": reason,
    }

    if plan.dry_run:
        return DownloadResult(
            outcome=DownloadOutcome.SINGLE_DRY_RUN,
            artifact=selected_envelope,
            output_path=str(final_path),
        )

    resolved_path, _skip_info = _resolve_conflict(
        final_path, force=plan.force, no_clobber=plan.no_clobber
    )
    if resolved_path is None:
        # Preserve the legacy "File exists: <path>" error text byte-for-byte.
        # The single-file caller's contract is the plain-string error key plus
        # the *raw selected dict* under ``artifact`` (not the envelope shape),
        # kept stable for scripts parsing ``--json`` envelopes.
        return DownloadResult(
            outcome=DownloadOutcome.ERROR,
            error=f"File exists: {final_path}",
            artifact=dict(selected),
            suggestion="Use --force to overwrite or choose a different path",
        )

    final_path = resolved_path

    try:
        result_path = await download_fn(
            nb_id_resolved, str(final_path), artifact_id=str(selected["id"])
        )
        return DownloadResult(
            outcome=DownloadOutcome.SINGLE_DOWNLOADED,
            artifact=selected_envelope,
            output_path=result_path or str(final_path),
        )
    except Exception as e:
        return DownloadResult(outcome=DownloadOutcome.ERROR, error=str(e), artifact=dict(selected))


async def execute_download(
    plan: DownloadPlan,
    facade: _DownloadFacade,
    *,
    notebook_resolver: NotebookResolver,
    artifact_resolver: ArtifactResolver,
    progress: ProgressSink | None = None,
) -> DownloadResult:
    """Run the validated plan against the live (or mocked) client facade.

    Returns the typed :class:`DownloadResult` the adapter then renders. The
    notebook-id and partial-artifact-id resolvers are injected so this module
    stays free of the Click-coupled ``cli.resolve`` helpers.

    Args:
        plan: Output of :func:`build_download_plan`. The plan carries ``cwd``
            captured at build time; the executor uses it to derive the
            single-artifact output path when the user didn't supply one.
        facade: A live :class:`~notebooklm.NotebookLMClient` (or any object
            exposing ``client.artifacts`` with ``.list`` and
            ``.download_<spec.download_attr>``).
        notebook_resolver: Async callable resolving ``plan.notebook_id`` to its
            full id (the full-id fast-path lives inside it, preserving the RPC
            call set).
        artifact_resolver: Sync callable resolving a partial ``-a/--artifact``
            id against the pre-fetched list, raising ``ValueError`` on
            no-match / ambiguity.
        progress: Optional :class:`ProgressSink` for the ``--all`` per-artifact
            progress events. ``None`` skips them.
    """
    nb_id_resolved = await notebook_resolver(plan.notebook_id)

    download_fn = _bind_download_fn(plan, facade)

    type_artifacts = await _get_completed_artifacts_as_dicts(facade, nb_id_resolved, plan.spec)
    if not type_artifacts:
        return DownloadResult(
            outcome=DownloadOutcome.NO_ARTIFACTS,
            error=f"No completed {plan.spec.name} artifacts found",
            suggestion=f"Generate one with: notebooklm generate {plan.spec.name}",
        )

    if plan.download_all:
        return await _execute_download_all(
            plan,
            facade,
            type_artifacts,
            nb_id_resolved,
            download_fn,
            progress=progress,
        )

    return await _execute_download_single(
        plan,
        facade,
        type_artifacts,
        nb_id_resolved,
        download_fn,
        artifact_resolver,
    )
