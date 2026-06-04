"""Pure-logic download plan + executor (ADR-0008 service for
``cli/download_cmd.py``).

This module hosts the behaviour the 9 leaf ``download <type>`` commands share:
flag validation, artifact lookup, single-vs-``--all`` dispatch, dry-run
preview, conflict resolution, and result-envelope construction. It contains
**no Click decorators** — Click integration lives in
:mod:`notebooklm.cli.download_cmd`, which builds each leaf from a
:class:`~notebooklm.cli._download_specs.DownloadTypeSpec`.

Public API (the three names ADR-0008 requires):

- :class:`DownloadPlan` — frozen dataclass capturing one validated invocation.
- :func:`build_download_plan` — synchronous validation + plan assembly.
- :func:`execute_download` — coroutine that performs the actual download.

The split is deliberate: ``build_download_plan`` rejects flag conflicts
synchronously with :class:`DownloadPlanValidationError`, while
``execute_download`` performs all I/O. The Click handler owns the ADR-0015
JSON envelope vs. text ``UsageError`` translation.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any, Protocol

from ...types import Artifact, ArtifactType
from ..download_helpers import (
    ArtifactDict,
    artifact_title_to_filename,
    resolve_partial_artifact_id,
    select_artifact,
)
from ..resolve import require_notebook, resolve_notebook_id

# Format → extension map shared with the runtime extension-override path
# and the registry layer. Quiz/flashcards expose all three formats;
# slide-deck only swaps the extension between pdf and pptx via a dedicated
# mapping defined inline in its spec row.
FORMAT_EXTENSIONS: dict[str, str] = {
    "json": ".json",
    "markdown": ".md",
    "html": ".html",
}


@dataclass(frozen=True)
class DownloadTypeSpec:
    """Static metadata for one ``download <name>`` leaf command.

    Lives in the service layer (not the registry data file) so the executor
    can depend on the dataclass shape without crossing the
    ``cli/services -> cli/_*`` boundary the CLI lint guards against
    (``tests/unit/test_cli_boundary.py``). The registry data file
    (:mod:`notebooklm.cli._download_specs`) imports this type and supplies
    the concrete rows.

    Attributes:
        name: Click subcommand name (e.g. ``"audio"``). Also used as the
            short identifier in error messages (``"No completed audio ..."``).
        kind: ``ArtifactType`` enum value the leaf operates on.
        extension: Default file extension including leading dot (``".mp3"``).
            Overridden at run time by the ``format_choices`` branch when
            applicable.
        default_dir: Default output directory for ``--all`` invocations
            (``"./audio"``).
        download_attr: Attribute name on ``client.artifacts`` that performs
            the actual download (``"download_audio"``). Bound by reflection
            in :func:`execute_download`.
        help_summary: One-line ``--help`` summary line.
        help_examples: Multi-line ``\\b``-prefixed examples block — appended
            to the docstring as Click sees it.
        format_choices: ``--format`` Click ``Choice`` values, or empty tuple
            if this leaf has no ``--format`` flag.
        format_default: Default ``--format`` value (only meaningful when
            ``format_choices`` is non-empty).
        format_help: Human-readable description of the ``--format`` flag for
            ``--help`` output. Empty when ``format_choices`` is empty.
        format_extension_map: Map ``--format value`` → extension override.
            For slide-deck this is ``{"pdf": ".pdf", "pptx": ".pptx"}``;
            for quiz/flashcards it's ``FORMAT_EXTENSIONS``. Empty otherwise.
        format_kwarg: Keyword argument name to forward the chosen format
            value to ``client.artifacts.download_<x>`` (e.g. ``"output_format"``).
            Empty when the format choice only mutates the local filename.
        format_param_name: Click parameter name for ``--format``. Defaults to
            ``"output_format"`` (used by quiz/flashcards); slide-deck overrides
            to ``"slide_format"`` for legacy compatibility with its historical
            kwarg name.
        forward_format_only_if_set: When ``True`` (slide-deck), forward the
            format kwarg ONLY when the user explicitly picked the non-default
            value (matches the historical "partial only for pptx" wiring).
            When ``False`` (quiz/flashcards) always forward.

    Note on ``format_extension_map``: ``frozen=True`` only freezes the
    *reference* — the dict contents remain mutable at runtime. The registry
    spec rows are module-level constants and not expected to be mutated;
    callers must treat the map as read-only by convention.
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


# Type alias for the bound download coroutine returned by ``getattr(client.artifacts, attr)``.
_DownloadFn = Callable[..., Awaitable[str | None]]


@dataclass(frozen=True)
class DownloadPlanValidationError(Exception):
    """Service-level validation error for command-layer rendering."""

    message: str
    code: str = "VALIDATION_ERROR"

    def __post_init__(self) -> None:
        object.__setattr__(self, "args", (self.message,))


@dataclass(frozen=True)
class DownloadPlan:
    """One validated download invocation.

    Built by :func:`build_download_plan` from raw Click args; consumed by
    :func:`execute_download`. The plan carries everything the executor needs
    so the Click layer can stay decorator-thin.

    Notes on field semantics:

    - ``notebook_id`` is the post-``require_notebook`` raw value (may still be
      a partial prefix; the executor resolves it via :func:`resolve_notebook_id`).
    - ``output_path`` is the user-supplied path, or ``None`` to derive one
      from the artifact title.
    - ``file_extension`` is the post-``--format``-adjustment extension; the
      Click layer applies the override before calling
      :func:`build_download_plan`.
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
    json_output: bool
    dry_run: bool
    force: bool
    no_clobber: bool
    format_choice: str = ""
    warnings: tuple[str, ...] = ()
    # Captured at plan-build time so the executor doesn't have to re-derive
    # it; ``Path.cwd()`` at executor time would be wrong if the caller
    # changed directories between ``build_download_plan`` and the awaited
    # ``execute_download``. Defaults to the build-time cwd.
    cwd: Path = field(default_factory=Path.cwd)


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

    A mismatch warning is returned with the extension so the command layer can
    render it. The warning is suppressed when ``download_all`` is true because
    the user-supplied path then names a destination *directory* (not a file),
    so an extension check is meaningless and the warning would be a false positive.
    """
    if not spec.format_choices:
        return spec.extension, ()
    effective_ext = spec.format_extension_map.get(format_choice, spec.extension)
    # Only warn when the user supplied an output path whose extension doesn't
    # match the chosen --format AND we're in single-file mode (--all uses the
    # path as a directory destination, not a target filename).
    if output_path and not download_all and not output_path.endswith(effective_ext):
        return (
            effective_ext,
            (
                f"Warning: output path '{output_path}' does not end with "
                f"'{effective_ext}' but --format {format_choice} was requested.",
            ),
        )
    return effective_ext, ()


def build_download_plan(
    config: DownloadTypeSpec,
    args: dict[str, Any],
    cwd: Path | None = None,
) -> DownloadPlan:
    """Validate + assemble a :class:`DownloadPlan` from raw Click args.

    Synchronous: rejects flag conflicts with a typed validation exception and
    resolves the notebook id via the shared ``require_notebook`` helper (no
    I/O). Does NOT perform the async :func:`resolve_notebook_id` lookup — that
    runs inside :func:`execute_download`.

    Args:
        config: One ``DownloadTypeSpec`` row from the registry.
        args: Raw Click kwargs (``output_path``, ``notebook_id``, ``latest``,
            ``earliest``, ``download_all``, ``name``, ``artifact_id``,
            ``json_output``, ``dry_run``, ``force``, ``no_clobber``,
            optionally ``slide_format`` / ``output_format``).
        cwd: Reserved for callers that want to inject the working directory
            (used for derived-output-path resolution inside the executor).
            ``None`` is fine — the executor falls back to ``Path.cwd()`` at
            call time.
    Returns:
        Frozen ``DownloadPlan`` ready for :func:`execute_download`.

    Raises:
        DownloadPlanValidationError: when flag combinations conflict. The
            command layer translates this into Click usage text or the
            ADR-0015 JSON envelope.
    """
    if args.get("force") and args.get("no_clobber"):
        raise DownloadPlanValidationError("Cannot specify both --force and --no-clobber")
    if args.get("latest") and args.get("earliest"):
        raise DownloadPlanValidationError("Cannot specify both --latest and --earliest")
    if args.get("download_all") and args.get("artifact_id"):
        raise DownloadPlanValidationError("Cannot specify both --all and --artifact")

    nb_id = require_notebook(args.get("notebook_id"))

    # Format-choice extraction. The Click param name is data-driven via
    # ``spec.format_param_name`` (default ``"output_format"``, slide-deck
    # overrides to ``"slide_format"``). Leaves with no ``--format`` flag have
    # empty ``format_choices``.
    format_choice = ""
    if config.format_choices:
        format_choice = (
            args.get(config.format_param_name, config.format_default) or config.format_default
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
        json_output=bool(args.get("json_output", False)),
        dry_run=bool(args.get("dry_run", False)),
        force=bool(args.get("force", False)),
        no_clobber=bool(args.get("no_clobber", False)),
        format_choice=format_choice,
        warnings=warnings,
        cwd=cwd if cwd is not None else Path.cwd(),
    )


async def _get_completed_artifacts_as_dicts(
    facade: _DownloadFacade, notebook_id: str, spec: DownloadTypeSpec
) -> list[ArtifactDict]:
    """Fetch artifacts, filter by kind + completion, project to ArtifactDict.

    The ``isinstance(a, Artifact)`` guard mirrors the legacy ``download_cmd``
    implementation and protects against the (unlikely but possible) case
    where ``client.artifacts.list`` returns a heterogeneous list with stub
    entries that don't expose the ``kind`` / ``is_completed`` properties.
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
        # slide-deck: only pptx triggers the partial (pdf default keeps bare).
        if plan.format_choice == spec.format_default:
            return base_fn
        return partial(base_fn, **{spec.format_kwarg: plan.format_choice})
    # quiz/flashcards: always forward the chosen format.
    return partial(base_fn, **{spec.format_kwarg: plan.format_choice})


async def _execute_download_all(
    plan: DownloadPlan,
    facade: _DownloadFacade,
    type_artifacts: list[ArtifactDict],
    nb_id_resolved: str,
    download_fn: _DownloadFn,
    *,
    text_progress_sink: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Execute the ``--all`` branch: filter by name, dry-run preview, download.

    The text-mode progress lines (``Downloading 1/N: <title>``) are routed
    through ``text_progress_sink`` so the live Click handler can render them
    via ``console.print`` while tests can inject a no-op.

    Relative output paths (both the user-supplied ``plan.output_path`` and
    the spec's ``default_dir`` fallback like ``"./audio"``) are resolved
    against ``plan.cwd`` — the directory the user invoked the CLI from —
    not the process cwd at executor-await time. Absolute paths pass through
    unchanged.
    """
    raw = Path(plan.output_path) if plan.output_path else Path(plan.spec.default_dir)
    output_dir = raw if raw.is_absolute() else plan.cwd / raw

    # --name filter (case-insensitive substring) applied before previewing.
    if plan.name:
        name_lower = plan.name.lower()
        filtered = [a for a in type_artifacts if name_lower in a["title"].lower()]
        if not filtered:
            return {
                "error": (
                    f"No artifacts matching '{plan.name}'. "
                    f"Available: {', '.join(a['title'] for a in type_artifacts)}"
                ),
            }
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
        return {
            "dry_run": True,
            "operation": "download_all",
            "count": len(type_artifacts),
            "output_dir": str(output_dir),
            "artifacts": [
                {
                    "id": a["id"],
                    "title": a["title"],
                    "filename": item_name,
                }
                for a, item_name in zip(type_artifacts, planned_filenames, strict=True)
            ],
        }

    output_dir.mkdir(parents=True, exist_ok=True)

    artifacts_results: list[dict[str, Any]] = []
    total = len(type_artifacts)
    succeeded_count = 0
    failed_count = 0
    skipped_count = 0

    for i, (artifact, item_name) in enumerate(
        zip(type_artifacts, planned_filenames, strict=True), 1
    ):
        if text_progress_sink and not plan.json_output:
            text_progress_sink(f"[dim]Downloading {i}/{total}:[/dim] {artifact['title']}")

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

    envelope: dict[str, Any] = {
        "operation": "download_all",
        "output_dir": str(output_dir),
        "total": total,
        "succeeded_count": succeeded_count,
        "failed_count": failed_count,
        "skipped_count": skipped_count,
        "artifacts": artifacts_results,
    }
    # ANY per-item failure surfaces a non-zero exit. The Click
    # layer keys exit-code policy on the presence of the top-level "error"
    # field, so add it only when there are failures.
    if failed_count > 0:
        envelope["error"] = True
    return envelope


async def _execute_download_single(
    plan: DownloadPlan,
    facade: _DownloadFacade,
    type_artifacts: list[ArtifactDict],
    nb_id_resolved: str,
    download_fn: _DownloadFn,
) -> dict[str, Any]:
    """Execute the single-artifact branch: select → dry-run | conflict | download."""
    try:
        resolved_artifact_id = (
            resolve_partial_artifact_id(type_artifacts, plan.artifact_id)
            if plan.artifact_id
            else None
        )
        selected, reason = select_artifact(
            type_artifacts,
            latest=plan.latest,
            earliest=plan.earliest,
            name=plan.name,
            artifact_id=resolved_artifact_id,
        )
    except ValueError as e:
        return {"error": str(e)}

    if not plan.output_path:
        safe_name = artifact_title_to_filename(
            str(selected["title"]),
            plan.file_extension,
            set(),
        )
        final_path = plan.cwd / safe_name
    else:
        # Resolve relative paths against plan.cwd so the build-time directory
        # wins over the process cwd at executor-await time. Absolute paths
        # pass through unchanged.
        raw = Path(plan.output_path)
        final_path = raw if raw.is_absolute() else plan.cwd / raw

    if plan.dry_run:
        return {
            "dry_run": True,
            "operation": "download_single",
            "artifact": {
                "id": selected["id"],
                "title": selected["title"],
                "selection_reason": reason,
            },
            "output_path": str(final_path),
        }

    resolved_path, _skip_info = _resolve_conflict(
        final_path, force=plan.force, no_clobber=plan.no_clobber
    )
    if resolved_path is None:
        # Preserve the legacy "File exists: <path>" error text byte-for-byte;
        # ``_skip_info`` carries the structured skip envelope used by the
        # ``--all`` path but the single-file caller's contract is the
        # plain-string error key, kept stable for scripts parsing ``--json``
        # envelopes.
        return {
            "error": f"File exists: {final_path}",
            "artifact": selected,
            "suggestion": "Use --force to overwrite or choose a different path",
        }

    final_path = resolved_path

    try:
        result_path = await download_fn(
            nb_id_resolved, str(final_path), artifact_id=str(selected["id"])
        )
        return {
            "operation": "download_single",
            "artifact": {
                "id": selected["id"],
                "title": selected["title"],
                "selection_reason": reason,
            },
            "output_path": result_path or str(final_path),
            "status": "downloaded",
        }
    except Exception as e:
        return {"error": str(e), "artifact": selected}


async def execute_download(
    plan: DownloadPlan,
    facade: _DownloadFacade,
    *,
    text_progress_sink: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Run the validated plan against the live (or mocked) client facade.

    Returns the envelope dict the Click layer then renders / serialises. The
    envelope shape is the same one tests have always asserted — registries
    don't change the contract, they just centralise the dispatch.

    Args:
        plan: Output of :func:`build_download_plan`. The plan carries
            ``cwd`` captured at build time; the executor uses it to derive
            the single-artifact output path when the user didn't supply one.
        facade: A live :class:`~notebooklm.NotebookLMClient` (or any object
            exposing ``client.artifacts`` with ``.list`` and
            ``.download_<spec.download_attr>``).
        text_progress_sink: Callback invoked once per artifact in the
            ``--all`` text-mode path. ``None`` (default) skips the progress
            line; the live Click handler injects ``console.print``.
    """
    nb_id_resolved = await resolve_notebook_id(
        facade, plan.notebook_id, json_output=plan.json_output
    )

    download_fn = _bind_download_fn(plan, facade)

    type_artifacts = await _get_completed_artifacts_as_dicts(facade, nb_id_resolved, plan.spec)
    if not type_artifacts:
        return {
            "error": f"No completed {plan.spec.name} artifacts found",
            "suggestion": f"Generate one with: notebooklm generate {plan.spec.name}",
        }

    if plan.download_all:
        return await _execute_download_all(
            plan,
            facade,
            type_artifacts,
            nb_id_resolved,
            download_fn,
            text_progress_sink=text_progress_sink,
        )

    return await _execute_download_single(
        plan,
        facade,
        type_artifacts,
        nb_id_resolved,
        download_fn,
    )
