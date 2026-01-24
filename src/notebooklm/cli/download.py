"""Download content CLI commands.

Commands:
    audio        Download audio file
    video        Download video file
    slide-deck   Download slide deck PDF
    infographic  Download infographic image
    report       Download report as markdown
    mind-map     Download mind map as JSON
    data-table   Download data table as CSV
    quiz         Download quiz questions
    flashcards   Download flashcard deck

Also supports downloading by artifact UUID:
    notebooklm download <uuid1> [uuid2] ...
"""

import json
from pathlib import Path
from typing import Any, TypedDict

import click

from ..auth import AuthTokens, fetch_tokens, load_auth_from_storage
from ..client import NotebookLMClient
from ..types import Artifact, ArtifactType
from .download_helpers import ArtifactDict, artifact_title_to_filename, select_artifact
from .helpers import (
    console,
    handle_error,
    require_notebook,
    run_async,
)


async def _get_auth_from_context(ctx) -> AuthTokens:
    """Get authentication tokens from CLI context."""
    storage_path = ctx.obj.get("storage_path") if ctx.obj else None
    cookies = load_auth_from_storage(storage_path)
    csrf, session_id = await fetch_tokens(cookies)
    return AuthTokens(cookies=cookies, csrf_token=csrf, session_id=session_id)


class ArtifactConfig(TypedDict):
    """Configuration for an artifact type."""

    kind: ArtifactType
    extension: str
    default_dir: str


# Artifact type configurations for download commands
ARTIFACT_CONFIGS: dict[str, ArtifactConfig] = {
    "audio": {"kind": ArtifactType.AUDIO, "extension": ".mp3", "default_dir": "./audio"},
    "video": {"kind": ArtifactType.VIDEO, "extension": ".mp4", "default_dir": "./video"},
    "report": {"kind": ArtifactType.REPORT, "extension": ".md", "default_dir": "./reports"},
    "mind-map": {"kind": ArtifactType.MIND_MAP, "extension": ".json", "default_dir": "./mind-maps"},
    "infographic": {
        "kind": ArtifactType.INFOGRAPHIC,
        "extension": ".png",
        "default_dir": "./infographic",
    },
    "slide-deck": {
        "kind": ArtifactType.SLIDE_DECK,
        "extension": ".pdf",
        "default_dir": "./slide-decks",
    },
    "data-table": {
        "kind": ArtifactType.DATA_TABLE,
        "extension": ".csv",
        "default_dir": "./data-tables",
    },
}

# Complete type mapping for UUID downloads (includes quiz/flashcards)
# Maps ArtifactType -> (download_method_name, extension, cli_type_name)
ARTIFACT_TYPE_DOWNLOAD_MAP: dict[ArtifactType, tuple[str, str, str]] = {
    ArtifactType.AUDIO: ("download_audio", ".mp3", "audio"),
    ArtifactType.VIDEO: ("download_video", ".mp4", "video"),
    ArtifactType.SLIDE_DECK: ("download_slide_deck", ".pdf", "slide-deck"),
    ArtifactType.INFOGRAPHIC: ("download_infographic", ".png", "infographic"),
    ArtifactType.REPORT: ("download_report", ".md", "report"),
    ArtifactType.MIND_MAP: ("download_mind_map", ".json", "mind-map"),
    ArtifactType.DATA_TABLE: ("download_data_table", ".csv", "data-table"),
    ArtifactType.QUIZ: ("download_quiz", ".json", "quiz"),
    ArtifactType.FLASHCARDS: ("download_flashcards", ".json", "flashcards"),
}

# Shared options for standard artifact download commands
STANDARD_DOWNLOAD_OPTIONS = [
    click.argument("output_path", required=False, type=click.Path()),
    click.option("-n", "--notebook", help="Notebook ID (uses current context if not set)"),
    click.option("--latest", is_flag=True, help="Download latest (default behavior)"),
    click.option("--earliest", is_flag=True, help="Download earliest"),
    click.option("--all", "download_all", is_flag=True, help="Download all artifacts"),
    click.option("--name", help="Filter by artifact title (fuzzy match)"),
    click.option("-a", "--artifact", "artifact_id", help="Select by artifact ID"),
    click.option("--json", "json_output", is_flag=True, help="Output JSON instead of text"),
    click.option("--dry-run", is_flag=True, help="Preview without downloading"),
    click.option("--force", is_flag=True, help="Overwrite existing files"),
    click.option("--no-clobber", is_flag=True, help="Skip if file exists"),
]


class DownloadGroup(click.Group):
    """Custom group that handles both subcommands and direct UUID arguments."""

    def parse_args(self, ctx, args):
        # Check if first arg looks like a subcommand
        if args and args[0] in self.commands:
            return super().parse_args(ctx, args)

        # Otherwise, treat remaining positional args as artifact IDs
        # Extract options first, then positional args
        artifact_ids = []
        remaining_args = []
        i = 0
        while i < len(args):
            arg = args[i]
            if arg.startswith("-"):
                # It's an option
                remaining_args.append(arg)
                # Check if this option takes a value
                if arg in ("-o", "--output", "-n", "--notebook") and i + 1 < len(args):
                    i += 1
                    remaining_args.append(args[i])
            else:
                # Positional argument - treat as artifact ID
                artifact_ids.append(arg)
            i += 1

        # Store artifact_ids for later use
        ctx.ensure_object(dict)
        ctx.obj["_artifact_ids"] = tuple(artifact_ids)

        # Parse remaining args (options only)
        return super().parse_args(ctx, remaining_args)


@click.group(cls=DownloadGroup, invoke_without_command=True)
@click.option("-o", "--output", type=click.Path(), help="Output directory (for UUID mode)")
@click.option("-n", "--notebook", "group_notebook", help="Notebook ID (for UUID mode)")
@click.option("--json", "group_json", is_flag=True, help="Output JSON (for UUID mode)")
@click.option("--dry-run", "group_dry_run", is_flag=True, help="Preview (for UUID mode)")
@click.option("--force", "group_force", is_flag=True, help="Overwrite (for UUID mode)")
@click.option(
    "--no-clobber", "group_no_clobber", is_flag=True, help="Skip if exists (for UUID mode)"
)
@click.pass_context
def download(ctx, output, group_notebook, group_json, group_dry_run, group_force, group_no_clobber):
    """Download generated content.

    \b
    Download by type:
      notebooklm download audio
      notebooklm download video --all
      notebooklm download slide-deck my-slides.pdf

    \b
    Download by UUID (auto-detects type):
      notebooklm download <uuid1> [uuid2] ...
      notebooklm download <uuid> -o ./downloads/
    """
    if ctx.invoked_subcommand is None:
        # Check for artifact IDs from custom parsing
        artifact_ids = ctx.obj.get("_artifact_ids", ())
        if not artifact_ids:
            click.echo(ctx.get_help())
            return
        # Handle UUID download mode
        _run_uuid_download(
            ctx,
            artifact_ids,
            output,
            group_notebook,
            group_json,
            group_dry_run,
            group_force,
            group_no_clobber,
        )


async def _download_artifacts_generic(
    ctx,
    artifact_type_name: str,
    artifact_kind: ArtifactType,
    file_extension: str,
    default_output_dir: str,
    output_path: str | None,
    notebook: str | None,
    latest: bool,
    earliest: bool,
    download_all: bool,
    name: str | None,
    artifact_id: str | None,
    json_output: bool,
    dry_run: bool,
    force: bool,
    no_clobber: bool,
) -> dict:
    """
    Generic artifact download implementation.

    Handles all artifact types (audio, video, infographic, slide-deck)
    with the same logic, only varying by extension and type filters.

    Args:
        ctx: Click context
        artifact_type_name: Human-readable type name ("audio", "video", etc.)
        artifact_kind: ArtifactType enum value to filter by
        file_extension: File extension (".mp3", ".mp4", ".png", ".pdf")
        default_output_dir: Default output directory for --all flag
        output_path: User-specified output path
        notebook: Notebook ID
        latest: Download latest artifact
        earliest: Download earliest artifact
        download_all: Download all artifacts
        name: Filter by artifact title
        artifact_id: Select by exact artifact ID
        json_output: Output JSON instead of text
        dry_run: Preview without downloading
        force: Overwrite existing files
        no_clobber: Skip if file exists

    Returns:
        Result dictionary with operation details
    """
    # Validate conflicting flags
    if force and no_clobber:
        raise click.UsageError("Cannot specify both --force and --no-clobber")
    if latest and earliest:
        raise click.UsageError("Cannot specify both --latest and --earliest")
    if download_all and artifact_id:
        raise click.UsageError("Cannot specify both --all and --artifact")

    # Get notebook and auth
    nb_id = require_notebook(notebook)
    auth = await _get_auth_from_context(ctx)

    async def _download() -> dict[str, Any]:
        async with NotebookLMClient(auth) as client:
            # Setup download method dispatch
            download_methods = {
                "audio": client.artifacts.download_audio,
                "video": client.artifacts.download_video,
                "infographic": client.artifacts.download_infographic,
                "slide-deck": client.artifacts.download_slide_deck,
                "report": client.artifacts.download_report,
                "mind-map": client.artifacts.download_mind_map,
                "data-table": client.artifacts.download_data_table,
            }
            download_fn = download_methods.get(artifact_type_name)
            if not download_fn:
                raise ValueError(f"Unknown artifact type: {artifact_type_name}")

            # Fetch artifacts
            all_artifacts = await client.artifacts.list(nb_id)

            # Filter by type and completed status
            completed_artifacts = [
                a
                for a in all_artifacts
                if isinstance(a, Artifact) and a.kind == artifact_kind and a.is_completed
            ]

            if not completed_artifacts:
                return {
                    "error": f"No completed {artifact_type_name} artifacts found",
                    "suggestion": f"Generate one with: notebooklm generate {artifact_type_name}",
                }

            # Convert to dict format for selection logic
            type_artifacts: list[ArtifactDict] = [
                {
                    "id": a.id,
                    "title": a.title,
                    "created_at": int(a.created_at.timestamp()) if a.created_at else 0,
                }
                for a in completed_artifacts
            ]

            # Helper for file conflict resolution
            def _resolve_conflict(path: Path) -> tuple[Path | None, dict | None]:
                if not path.exists():
                    return path, None

                if no_clobber:
                    return None, {
                        "status": "skipped",
                        "reason": "file exists",
                        "path": str(path),
                    }

                if not force:
                    # Auto-rename
                    counter = 2
                    base_name = path.stem
                    parent = path.parent
                    ext = path.suffix
                    while path.exists():
                        path = parent / f"{base_name} ({counter}){ext}"
                        counter += 1

                return path, None

            # Handle --all flag
            if download_all:
                output_dir = Path(output_path) if output_path else Path(default_output_dir)

                if dry_run:
                    return {
                        "dry_run": True,
                        "operation": "download_all",
                        "count": len(type_artifacts),
                        "output_dir": str(output_dir),
                        "artifacts": [
                            {
                                "id": a["id"],
                                "title": a["title"],
                                "filename": artifact_title_to_filename(
                                    str(a["title"]),
                                    file_extension,
                                    set(),
                                ),
                            }
                            for a in type_artifacts
                        ],
                    }

                output_dir.mkdir(parents=True, exist_ok=True)

                results = []
                existing_names: set[str] = set()
                total = len(type_artifacts)

                for i, artifact in enumerate(type_artifacts, 1):
                    # Progress indicator
                    if not json_output:
                        console.print(f"[dim]Downloading {i}/{total}:[/dim] {artifact['title']}")

                    # Generate safe name
                    item_name = artifact_title_to_filename(
                        str(artifact["title"]),
                        file_extension,
                        existing_names,
                    )
                    existing_names.add(item_name)
                    item_path = output_dir / item_name

                    # Resolve conflicts
                    resolved_path, skip_info = _resolve_conflict(item_path)
                    if skip_info or resolved_path is None:
                        results.append(
                            {
                                "id": artifact["id"],
                                "title": artifact["title"],
                                "filename": item_name,
                                **(
                                    skip_info
                                    or {"status": "skipped", "reason": "conflict resolution failed"}
                                ),
                            }
                        )
                        continue

                    # Update if auto-renamed
                    item_path = resolved_path
                    item_name = item_path.name

                    # Download
                    try:
                        # Download using dispatch
                        await download_fn(nb_id, str(item_path), artifact_id=str(artifact["id"]))

                        results.append(
                            {
                                "id": artifact["id"],
                                "title": artifact["title"],
                                "filename": item_name,
                                "path": str(item_path),
                                "status": "downloaded",
                            }
                        )
                    except Exception as e:
                        results.append(
                            {
                                "id": artifact["id"],
                                "title": artifact["title"],
                                "filename": item_name,
                                "status": "failed",
                                "error": str(e),
                            }
                        )

                return {
                    "operation": "download_all",
                    "output_dir": str(output_dir),
                    "total": total,
                    "results": results,
                }

            # Single artifact selection
            try:
                selected, reason = select_artifact(
                    type_artifacts,
                    latest=latest,
                    earliest=earliest,
                    name=name,
                    artifact_id=artifact_id,
                )
            except ValueError as e:
                return {"error": str(e)}

            # Determine output path
            if not output_path:
                safe_name = artifact_title_to_filename(
                    str(selected["title"]),
                    file_extension,
                    set(),
                )
                final_path = Path.cwd() / safe_name
            else:
                final_path = Path(output_path)

            # Dry run
            if dry_run:
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

            # Resolve conflicts
            resolved_path, skip_error = _resolve_conflict(final_path)
            if skip_error or resolved_path is None:
                return {
                    "error": f"File exists: {final_path}",
                    "artifact": selected,
                    "suggestion": "Use --force to overwrite or choose a different path",
                }

            final_path = resolved_path

            # Download
            try:
                # Download using dispatch
                result_path = await download_fn(
                    nb_id, str(final_path), artifact_id=str(selected["id"])
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

    return await _download()


def _display_download_result(result: dict, artifact_type: str) -> None:
    """Display download results in user-friendly format."""
    if "error" in result:
        console.print(f"[red]Error:[/red] {result['error']}")
        if "suggestion" in result:
            console.print(f"[dim]{result['suggestion']}[/dim]")
        return

    # Dry run
    if result.get("dry_run"):
        if result["operation"] == "download_all":
            console.print(
                f"[yellow]DRY RUN:[/yellow] Would download {result['count']} {artifact_type} files to: {result['output_dir']}"
            )
            console.print("\n[bold]Preview:[/bold]")
            for art in result["artifacts"]:
                console.print(f"  {art['filename']} <- {art['title']}")
        else:
            console.print("[yellow]DRY RUN:[/yellow] Would download:")
            console.print(f"  Artifact: {result['artifact']['title']}")
            console.print(f"  Reason: {result['artifact']['selection_reason']}")
            console.print(f"  Output: {result['output_path']}")
        return

    # Download all results
    if result.get("operation") == "download_all":
        downloaded = [r for r in result["results"] if r.get("status") == "downloaded"]
        skipped = [r for r in result["results"] if r.get("status") == "skipped"]
        failed = [r for r in result["results"] if r.get("status") == "failed"]

        console.print(
            f"[bold]Downloaded {len(downloaded)}/{result['total']} {artifact_type} files to:[/bold] {result['output_dir']}"
        )

        if downloaded:
            console.print("\n[green]Downloaded:[/green]")
            for r in downloaded:
                console.print(f"  {r['filename']} <- {r['title']}")

        if skipped:
            console.print("\n[yellow]Skipped:[/yellow]")
            for r in skipped:
                console.print(f"  {r['filename']} ({r.get('reason', 'unknown')})")

        if failed:
            console.print("\n[red]Failed:[/red]")
            for r in failed:
                console.print(f"  {r['filename']}: {r.get('error', 'unknown error')}")

    # Single download
    else:
        console.print(
            f"[green]{artifact_type.capitalize()} saved to:[/green] {result['output_path']}"
        )
        console.print(
            f"[dim]Artifact: {result['artifact']['title']} ({result['artifact']['selection_reason']})[/dim]"
        )


def _run_artifact_download(ctx, artifact_type: str, **kwargs) -> None:
    """Execute download for a specific artifact type.

    Handles the common pattern across all artifact download commands.
    """
    config = ARTIFACT_CONFIGS[artifact_type]
    json_output = kwargs.get("json_output", False)

    try:
        result = run_async(
            _download_artifacts_generic(
                ctx=ctx,
                artifact_type_name=artifact_type,
                artifact_kind=config["kind"],
                file_extension=config["extension"],
                default_output_dir=config["default_dir"],
                **kwargs,
            )
        )

        if json_output:
            console.print(json.dumps(result, indent=2))
            return

        _display_download_result(result, artifact_type)
        if "error" in result:
            raise SystemExit(1)

    except Exception as e:
        handle_error(e)


# =============================================================================
# UUID-BASED DOWNLOAD
# =============================================================================


async def _download_by_uuids(
    ctx,
    artifact_ids: tuple[str, ...],
    output_dir: str | None,
    notebook: str | None,
    json_output: bool,
    dry_run: bool,
    force: bool,
    no_clobber: bool,
) -> dict:
    """Download artifacts by UUID with auto-detected types."""
    if force and no_clobber:
        raise click.UsageError("Cannot specify both --force and --no-clobber")

    nb_id = require_notebook(notebook)
    auth = await _get_auth_from_context(ctx)

    async with NotebookLMClient(auth) as client:
        # Fetch all artifacts to resolve IDs and get types
        all_artifacts = await client.artifacts.list(nb_id)
        artifact_map = {a.id: a for a in all_artifacts if isinstance(a, Artifact)}

        # Resolve each ID (support partial matching)
        resolved: list[Artifact] = []
        not_found: list[str] = []
        for partial_id in artifact_ids:
            # Skip empty IDs (would match everything via startswith(""))
            if not partial_id or not partial_id.strip():
                not_found.append(partial_id or "(empty)")
                continue
            matches = [a for aid, a in artifact_map.items() if aid.startswith(partial_id)]
            if len(matches) == 0:
                not_found.append(partial_id)
            elif len(matches) > 1:
                match_ids = [m.id[:8] for m in matches]
                raise click.UsageError(
                    f"Ambiguous ID '{partial_id}', matches: {', '.join(match_ids)}"
                )
            else:
                resolved.append(matches[0])

        if not resolved:
            return {"error": "No artifacts found matching provided IDs", "not_found": not_found}

        # Determine output directory
        out_path = Path(output_dir) if output_dir else Path.cwd()

        if dry_run:
            artifacts_preview = []
            for a in resolved:
                info = ARTIFACT_TYPE_DOWNLOAD_MAP.get(a.kind, (None, ".bin", "unknown"))
                artifacts_preview.append(
                    {
                        "id": a.id,
                        "title": a.title,
                        "type": info[2],
                        "extension": info[1],
                    }
                )
            return {
                "dry_run": True,
                "output_dir": str(out_path),
                "artifacts": artifacts_preview,
                "not_found": not_found,
            }

        out_path.mkdir(parents=True, exist_ok=True)

        # Download each artifact
        results: list[dict[str, Any]] = []
        existing_names: set[str] = set()

        for artifact in resolved:
            type_info = ARTIFACT_TYPE_DOWNLOAD_MAP.get(artifact.kind)
            if not type_info:
                results.append(
                    {
                        "id": artifact.id,
                        "title": artifact.title,
                        "status": "failed",
                        "error": f"Unknown artifact type: {artifact.kind}",
                    }
                )
                continue

            method_name, extension, type_name = type_info

            # Check if artifact is completed
            if not artifact.is_completed:
                results.append(
                    {
                        "id": artifact.id,
                        "title": artifact.title,
                        "type": type_name,
                        "status": "skipped",
                        "reason": "still generating",
                    }
                )
                continue

            # Generate filename
            filename = artifact_title_to_filename(artifact.title, extension, existing_names)
            existing_names.add(filename)
            file_path = out_path / filename

            # Handle file conflicts
            if file_path.exists():
                if no_clobber:
                    results.append(
                        {
                            "id": artifact.id,
                            "title": artifact.title,
                            "type": type_name,
                            "status": "skipped",
                            "reason": "file exists",
                            "path": str(file_path),
                        }
                    )
                    continue
                elif not force:
                    # Auto-rename
                    counter = 2
                    base = file_path.stem
                    while file_path.exists():
                        file_path = out_path / f"{base} ({counter}){extension}"
                        counter += 1

            # Get download method and execute
            try:
                download_fn = getattr(client.artifacts, method_name)
                # Quiz/flashcards need output_format param - default to json
                if artifact.kind in (ArtifactType.QUIZ, ArtifactType.FLASHCARDS):
                    await download_fn(
                        nb_id, str(file_path), artifact_id=artifact.id, output_format="json"
                    )
                else:
                    await download_fn(nb_id, str(file_path), artifact_id=artifact.id)

                results.append(
                    {
                        "id": artifact.id,
                        "title": artifact.title,
                        "type": type_name,
                        "status": "downloaded",
                        "path": str(file_path),
                    }
                )
            except Exception as e:
                results.append(
                    {
                        "id": artifact.id,
                        "title": artifact.title,
                        "type": type_name,
                        "status": "failed",
                        "error": str(e),
                    }
                )

        return {
            "output_dir": str(out_path),
            "results": results,
            "not_found": not_found,
        }


def _display_uuid_download_result(result: dict) -> None:
    """Display UUID download results."""
    if "error" in result:
        console.print(f"[red]Error:[/red] {result['error']}")
        if result.get("not_found"):
            console.print(f"[dim]Not found: {', '.join(result['not_found'])}[/dim]")
        return

    if result.get("dry_run"):
        console.print(
            f"[yellow]DRY RUN:[/yellow] Would download {len(result['artifacts'])} "
            f"artifacts to: {result['output_dir']}"
        )
        for art in result["artifacts"]:
            console.print(f"  {art['title']}{art['extension']} ({art['type']})")
        if result.get("not_found"):
            console.print(f"\n[yellow]Not found:[/yellow] {', '.join(result['not_found'])}")
        return

    # Group results by status
    results_by_status = _group_results_by_status(result["results"])
    downloaded = results_by_status.get("downloaded", [])
    skipped = results_by_status.get("skipped", [])
    failed = results_by_status.get("failed", [])

    console.print(f"[bold]Downloaded to:[/bold] {result['output_dir']}")

    if downloaded:
        console.print("\n[green]Downloaded:[/green]")
        for r in downloaded:
            console.print(f"  [green]\u2713[/green] {Path(r['path']).name} ({r['type']})")

    if skipped:
        console.print("\n[yellow]Skipped:[/yellow]")
        for r in skipped:
            console.print(f"  [yellow]\u26a0[/yellow] {r['title']}: {r.get('reason', 'unknown')}")

    if failed:
        console.print("\n[red]Failed:[/red]")
        for r in failed:
            console.print(f"  [red]\u2717[/red] {r['title']}: {r.get('error', 'unknown')}")

    not_found = result.get("not_found", [])
    if not_found:
        console.print(f"\n[red]Not found:[/red] {', '.join(not_found)}")

    # Summary line
    total_failed = len(failed) + len(not_found)
    console.print(
        f"\n[dim]Downloaded: {len(downloaded)}, Skipped: {len(skipped)}, "
        f"Failed: {total_failed}[/dim]"
    )


def _group_results_by_status(results: list[dict]) -> dict[str, list[dict]]:
    """Group results by their status field."""
    grouped: dict[str, list[dict]] = {}
    for r in results:
        status = r.get("status", "unknown")
        if status not in grouped:
            grouped[status] = []
        grouped[status].append(r)
    return grouped


def _run_uuid_download(
    ctx, artifact_ids, output, notebook, json_output, dry_run, force, no_clobber
) -> None:
    """Execute UUID-based download."""
    try:
        result = run_async(
            _download_by_uuids(
                ctx, artifact_ids, output, notebook, json_output, dry_run, force, no_clobber
            )
        )

        if json_output:
            click.echo(json.dumps(result, indent=2))
            return

        _display_uuid_download_result(result)

        # Exit with error if complete failure
        if "error" in result:
            raise SystemExit(1)

        # Count outcomes from results
        results = result.get("results", [])
        status_counts = _count_results_by_status(results)
        status_counts["not_found"] = len(result.get("not_found", []))

        # Exit with error only if nothing succeeded
        total_failed = status_counts.get("failed", 0) + status_counts.get("not_found", 0)
        if total_failed > 0 and status_counts.get("downloaded", 0) == 0:
            raise SystemExit(1)

    except Exception as e:
        handle_error(e)


def _count_results_by_status(results: list[dict]) -> dict[str, int]:
    """Count results grouped by status field."""
    counts: dict[str, int] = {}
    for r in results:
        status = r.get("status", "unknown")
        counts[status] = counts.get(status, 0) + 1
    return counts


# =============================================================================
# DYNAMIC COMMAND REGISTRATION
# =============================================================================


def _make_download_docstring(artifact_type: str, config: ArtifactConfig) -> str:
    """Generate docstring for a download command."""
    ext = config["extension"]
    return f"""Download {artifact_type} file(s).

    \\b
    Examples:
      # Download latest {artifact_type} to default filename
      notebooklm download {artifact_type}

      # Download to specific path
      notebooklm download {artifact_type} my-file{ext}

      # Download all {artifact_type} files to directory
      notebooklm download {artifact_type} --all ./output/

      # Download specific artifact by name
      notebooklm download {artifact_type} --name "chapter 3"

      # Preview without downloading
      notebooklm download {artifact_type} --all --dry-run
    """


def _register_download_commands():
    """Register download subcommands for all artifact types in ARTIFACT_CONFIGS."""
    for artifact_type, _config in ARTIFACT_CONFIGS.items():
        # Create handler with closure to capture artifact_type
        def make_handler(atype: str):
            def handler(ctx, **kwargs):
                _run_artifact_download(ctx, atype, **kwargs)

            handler.__name__ = f"download_{atype.replace('-', '_')}"
            handler.__doc__ = _make_download_docstring(atype, ARTIFACT_CONFIGS[atype])
            return handler

        cmd = make_handler(artifact_type)

        # Apply options in reverse order (decorators are applied bottom-up)
        for opt in reversed(STANDARD_DOWNLOAD_OPTIONS):
            cmd = opt(cmd)
        cmd = click.pass_context(cmd)

        # Register with the download group
        download.add_command(click.command(artifact_type)(cmd))


# Register all standard download commands
_register_download_commands()


# =============================================================================
# QUIZ AND FLASHCARDS (different options pattern)
# =============================================================================

FORMAT_EXTENSIONS = {"json": ".json", "markdown": ".md", "html": ".html"}


async def _download_interactive(
    ctx,
    artifact_type: str,
    output_path: str | None,
    notebook: str | None,
    output_format: str,
    artifact_id: str | None,
) -> str:
    """Download quiz or flashcard artifact.

    Args:
        ctx: Click context.
        artifact_type: Either "quiz" or "flashcards".
        output_path: User-specified output path.
        notebook: Notebook ID.
        output_format: Output format - json, markdown, or html.
        artifact_id: Specific artifact ID.

    Returns:
        Path to downloaded file.
    """
    nb_id = require_notebook(notebook)
    auth = await _get_auth_from_context(ctx)

    async with NotebookLMClient(auth) as client:
        ext = FORMAT_EXTENSIONS[output_format]
        path = output_path or f"{artifact_type}{ext}"

        if artifact_type == "quiz":
            return await client.artifacts.download_quiz(
                nb_id, path, artifact_id=artifact_id, output_format=output_format
            )
        return await client.artifacts.download_flashcards(
            nb_id, path, artifact_id=artifact_id, output_format=output_format
        )


@download.command("quiz")
@click.argument("output_path", required=False, type=click.Path())
@click.option("-n", "--notebook", help="Notebook ID (uses current context if not set)")
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["json", "markdown", "html"]),
    default="json",
    help="Output format",
)
@click.option("-a", "--artifact", "artifact_id", help="Select by artifact ID")
@click.pass_context
def download_quiz_cmd(ctx, output_path, notebook, output_format, artifact_id):
    """Download quiz questions.

    \b
    Examples:
      notebooklm download quiz quiz.json
      notebooklm download quiz --format markdown quiz.md
      notebooklm download quiz --format html quiz.html
    """
    try:
        result = run_async(
            _download_interactive(ctx, "quiz", output_path, notebook, output_format, artifact_id)
        )
        console.print(f"[green]Downloaded quiz to:[/green] {result}")
    except Exception as e:
        handle_error(e)


@download.command("flashcards")
@click.argument("output_path", required=False, type=click.Path())
@click.option("-n", "--notebook", help="Notebook ID (uses current context if not set)")
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["json", "markdown", "html"]),
    default="json",
    help="Output format",
)
@click.option("-a", "--artifact", "artifact_id", help="Select by artifact ID")
@click.pass_context
def download_flashcards_cmd(ctx, output_path, notebook, output_format, artifact_id):
    """Download flashcard deck.

    \b
    Examples:
      notebooklm download flashcards cards.json
      notebooklm download flashcards --format markdown cards.md
      notebooklm download flashcards --format html cards.html
    """
    try:
        result = run_async(
            _download_interactive(
                ctx, "flashcards", output_path, notebook, output_format, artifact_id
            )
        )
        console.print(f"[green]Downloaded flashcards to:[/green] {result}")
    except Exception as e:
        handle_error(e)
