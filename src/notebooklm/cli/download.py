"""Download content CLI commands.

Commands:
    audio        Download audio file
    video        Download video file
    slide-deck   Download slide deck PDF
    infographic  Download infographic image
    report       Download report as markdown
    mind-map     Download mind map as JSON
    data-table   Download data table as CSV
"""

import json
from pathlib import Path
from typing import Any

import click

from ..auth import AuthTokens, fetch_tokens, load_auth_from_storage
from ..client import NotebookLMClient
from ..types import Artifact
from .download_helpers import ArtifactDict, artifact_title_to_filename, select_artifact
from .helpers import (
    console,
    handle_error,
    require_notebook,
    run_async,
)


@click.group()
def download():
    """Download generated content.

    \b
    Types:
      audio        Download audio file
      video        Download video file
      slide-deck   Download slide deck PDF
      infographic  Download infographic image
      report       Download report as markdown
      mind-map     Download mind map as JSON
      data-table   Download data table as CSV
    """
    pass


async def _download_artifacts_generic(
    ctx,
    artifact_type_name: str,
    artifact_type_id: int,
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
        artifact_type_id: RPC type ID (1=audio, 3=video, 7=infographic, 8=slide-deck)
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
    storage_path = ctx.obj.get("storage_path") if ctx.obj else None
    cookies = load_auth_from_storage(storage_path)
    csrf, session_id = await fetch_tokens(cookies)
    auth = AuthTokens(cookies=cookies, csrf_token=csrf, session_id=session_id)

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
                if isinstance(a, Artifact)
                and a.artifact_type == artifact_type_id
                and a.is_completed
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


def _execute_download(
    ctx,
    artifact_type_name: str,
    artifact_type_id: int,
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
) -> None:
    """Execute download and handle output display."""
    try:
        result = run_async(
            _download_artifacts_generic(
                ctx=ctx,
                artifact_type_name=artifact_type_name,
                artifact_type_id=artifact_type_id,
                file_extension=file_extension,
                default_output_dir=default_output_dir,
                output_path=output_path,
                notebook=notebook,
                latest=latest,
                earliest=earliest,
                download_all=download_all,
                name=name,
                artifact_id=artifact_id,
                json_output=json_output,
                dry_run=dry_run,
                force=force,
                no_clobber=no_clobber,
            )
        )

        if json_output:
            console.print(json.dumps(result, indent=2))
            return

        _display_download_result(result, artifact_type_name)

        if "error" in result:
            raise SystemExit(1)

    except Exception as e:
        handle_error(e)


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


@download.command("audio")
@click.argument("output_path", required=False, type=click.Path())
@click.option("-n", "--notebook", help="Notebook ID (uses current context if not set)")
@click.option("--latest", is_flag=True, help="Download latest (default behavior)")
@click.option("--earliest", is_flag=True, help="Download earliest")
@click.option("--all", "download_all", is_flag=True, help="Download all artifacts")
@click.option("--name", help="Filter by artifact title (fuzzy match)")
@click.option("-a", "--artifact", "artifact_id", help="Select by artifact ID")
@click.option("--json", "json_output", is_flag=True, help="Output JSON instead of text")
@click.option("--dry-run", is_flag=True, help="Preview without downloading")
@click.option("--force", is_flag=True, help="Overwrite existing files")
@click.option("--no-clobber", is_flag=True, help="Skip if file exists")
@click.pass_context
def download_audio(
    ctx,
    output_path,
    notebook,
    latest,
    earliest,
    download_all,
    name,
    artifact_id,
    json_output,
    dry_run,
    force,
    no_clobber,
):
    """Download audio overview(s) to file.

    \b
    Examples:
      # Download latest audio to default filename
      notebooklm download audio

      # Download to specific path
      notebooklm download audio my-podcast.mp3

      # Download all audio files to directory
      notebooklm download audio --all ./audio/

      # Download specific artifact by name
      notebooklm download audio --name "chapter 3"

      # Preview without downloading
      notebooklm download audio --all --dry-run
    """
    _execute_download(
        ctx,
        "audio",
        1,
        ".mp3",
        "./audio",
        output_path,
        notebook,
        latest,
        earliest,
        download_all,
        name,
        artifact_id,
        json_output,
        dry_run,
        force,
        no_clobber,
    )


@download.command("video")
@click.argument("output_path", required=False, type=click.Path())
@click.option("-n", "--notebook", help="Notebook ID (uses current context if not set)")
@click.option("--latest", is_flag=True, help="Download latest (default behavior)")
@click.option("--earliest", is_flag=True, help="Download earliest")
@click.option("--all", "download_all", is_flag=True, help="Download all artifacts")
@click.option("--name", help="Filter by artifact title (fuzzy match)")
@click.option("-a", "--artifact", "artifact_id", help="Select by artifact ID")
@click.option("--json", "json_output", is_flag=True, help="Output JSON instead of text")
@click.option("--dry-run", is_flag=True, help="Preview without downloading")
@click.option("--force", is_flag=True, help="Overwrite existing files")
@click.option("--no-clobber", is_flag=True, help="Skip if file exists")
@click.pass_context
def download_video(
    ctx,
    output_path,
    notebook,
    latest,
    earliest,
    download_all,
    name,
    artifact_id,
    json_output,
    dry_run,
    force,
    no_clobber,
):
    """Download video overview(s) to file.

    \b
    Examples:
      # Download latest video to default filename
      notebooklm download video

      # Download to specific path
      notebooklm download video my-video.mp4

      # Download all video files to directory
      notebooklm download video --all ./video/

      # Download specific artifact by name
      notebooklm download video --name "chapter 3"

      # Preview without downloading
      notebooklm download video --all --dry-run
    """
    _execute_download(
        ctx,
        "video",
        3,
        ".mp4",
        "./video",
        output_path,
        notebook,
        latest,
        earliest,
        download_all,
        name,
        artifact_id,
        json_output,
        dry_run,
        force,
        no_clobber,
    )


@download.command("slide-deck")
@click.argument("output_path", required=False, type=click.Path())
@click.option("-n", "--notebook", help="Notebook ID (uses current context if not set)")
@click.option("--latest", is_flag=True, help="Download latest (default behavior)")
@click.option("--earliest", is_flag=True, help="Download earliest")
@click.option("--all", "download_all", is_flag=True, help="Download all artifacts")
@click.option("--name", help="Filter by artifact title (fuzzy match)")
@click.option("-a", "--artifact", "artifact_id", help="Select by artifact ID")
@click.option("--json", "json_output", is_flag=True, help="Output JSON instead of text")
@click.option("--dry-run", is_flag=True, help="Preview without downloading")
@click.option("--force", is_flag=True, help="Overwrite existing files")
@click.option("--no-clobber", is_flag=True, help="Skip if file exists")
@click.pass_context
def download_slide_deck(
    ctx,
    output_path,
    notebook,
    latest,
    earliest,
    download_all,
    name,
    artifact_id,
    json_output,
    dry_run,
    force,
    no_clobber,
):
    """Download slide deck(s) as PDF files.

    \b
    Examples:
      # Download latest slide deck to default filename
      notebooklm download slide-deck

      # Download to specific path
      notebooklm download slide-deck my-slides.pdf

      # Download all slide decks to directory
      notebooklm download slide-deck --all ./slides/

      # Download specific artifact by name
      notebooklm download slide-deck --name "chapter 3"

      # Preview without downloading
      notebooklm download slide-deck --all --dry-run
    """
    _execute_download(
        ctx,
        "slide-deck",
        8,
        ".pdf",
        "./slides",
        output_path,
        notebook,
        latest,
        earliest,
        download_all,
        name,
        artifact_id,
        json_output,
        dry_run,
        force,
        no_clobber,
    )


@download.command("infographic")
@click.argument("output_path", required=False, type=click.Path())
@click.option("-n", "--notebook", help="Notebook ID (uses current context if not set)")
@click.option("--latest", is_flag=True, help="Download latest (default behavior)")
@click.option("--earliest", is_flag=True, help="Download earliest")
@click.option("--all", "download_all", is_flag=True, help="Download all artifacts")
@click.option("--name", help="Filter by artifact title (fuzzy match)")
@click.option("-a", "--artifact", "artifact_id", help="Select by artifact ID")
@click.option("--json", "json_output", is_flag=True, help="Output JSON instead of text")
@click.option("--dry-run", is_flag=True, help="Preview without downloading")
@click.option("--force", is_flag=True, help="Overwrite existing files")
@click.option("--no-clobber", is_flag=True, help="Skip if file exists")
@click.pass_context
def download_infographic(
    ctx,
    output_path,
    notebook,
    latest,
    earliest,
    download_all,
    name,
    artifact_id,
    json_output,
    dry_run,
    force,
    no_clobber,
):
    """Download infographic(s) to file.

    \b
    Examples:
      # Download latest infographic to default filename
      notebooklm download infographic

      # Download to specific path
      notebooklm download infographic my-infographic.png

      # Download all infographic files to directory
      notebooklm download infographic --all ./infographic/

      # Download specific artifact by name
      notebooklm download infographic --name "chapter 3"

      # Preview without downloading
      notebooklm download infographic --all --dry-run
    """
    _execute_download(
        ctx,
        "infographic",
        7,
        ".png",
        "./infographic",
        output_path,
        notebook,
        latest,
        earliest,
        download_all,
        name,
        artifact_id,
        json_output,
        dry_run,
        force,
        no_clobber,
    )


@download.command("report")
@click.argument("output_path", required=False, type=click.Path())
@click.option("-n", "--notebook", help="Notebook ID (uses current context if not set)")
@click.option("--latest", is_flag=True, help="Download latest (default behavior)")
@click.option("--earliest", is_flag=True, help="Download earliest")
@click.option("--all", "download_all", is_flag=True, help="Download all artifacts")
@click.option("--name", help="Filter by artifact title (fuzzy match)")
@click.option("-a", "--artifact", "artifact_id", help="Select by artifact ID")
@click.option("--json", "json_output", is_flag=True, help="Output JSON instead of text")
@click.option("--dry-run", is_flag=True, help="Preview without downloading")
@click.option("--force", is_flag=True, help="Overwrite existing files")
@click.option("--no-clobber", is_flag=True, help="Skip if file exists")
@click.pass_context
def download_report(
    ctx,
    output_path,
    notebook,
    latest,
    earliest,
    download_all,
    name,
    artifact_id,
    json_output,
    dry_run,
    force,
    no_clobber,
):
    """Download report(s) as markdown files.

    \b
    Examples:
      # Download latest report to default filename
      notebooklm download report

      # Download to specific path
      notebooklm download report my-report.md

      # Download all reports to directory
      notebooklm download report --all ./reports/

      # Download specific artifact by name
      notebooklm download report --name "chapter 3"

      # Preview without downloading
      notebooklm download report --all --dry-run
    """
    _execute_download(
        ctx,
        "report",
        2,
        ".md",
        "./reports",
        output_path,
        notebook,
        latest,
        earliest,
        download_all,
        name,
        artifact_id,
        json_output,
        dry_run,
        force,
        no_clobber,
    )


@download.command("mind-map")
@click.argument("output_path", required=False, type=click.Path())
@click.option("-n", "--notebook", help="Notebook ID (uses current context if not set)")
@click.option("--latest", is_flag=True, help="Download latest (default behavior)")
@click.option("--earliest", is_flag=True, help="Download earliest")
@click.option("--all", "download_all", is_flag=True, help="Download all artifacts")
@click.option("--name", help="Filter by artifact title (fuzzy match)")
@click.option("-a", "--artifact", "artifact_id", help="Select by artifact ID")
@click.option("--json", "json_output", is_flag=True, help="Output JSON instead of text")
@click.option("--dry-run", is_flag=True, help="Preview without downloading")
@click.option("--force", is_flag=True, help="Overwrite existing files")
@click.option("--no-clobber", is_flag=True, help="Skip if file exists")
@click.pass_context
def download_mind_map(
    ctx,
    output_path,
    notebook,
    latest,
    earliest,
    download_all,
    name,
    artifact_id,
    json_output,
    dry_run,
    force,
    no_clobber,
):
    """Download mind map(s) as JSON files.

    \b
    Examples:
      # Download latest mind map to default filename
      notebooklm download mind-map

      # Download to specific path
      notebooklm download mind-map my-mindmap.json

      # Download all mind maps to directory
      notebooklm download mind-map --all ./mind-maps/

      # Download specific artifact by name
      notebooklm download mind-map --name "chapter 3"

      # Preview without downloading
      notebooklm download mind-map --all --dry-run
    """
    _execute_download(
        ctx,
        "mind-map",
        5,
        ".json",
        "./mind-maps",
        output_path,
        notebook,
        latest,
        earliest,
        download_all,
        name,
        artifact_id,
        json_output,
        dry_run,
        force,
        no_clobber,
    )


@download.command("data-table")
@click.argument("output_path", required=False, type=click.Path())
@click.option("-n", "--notebook", help="Notebook ID (uses current context if not set)")
@click.option("--latest", is_flag=True, help="Download latest (default behavior)")
@click.option("--earliest", is_flag=True, help="Download earliest")
@click.option("--all", "download_all", is_flag=True, help="Download all artifacts")
@click.option("--name", help="Filter by artifact title (fuzzy match)")
@click.option("-a", "--artifact", "artifact_id", help="Select by artifact ID")
@click.option("--json", "json_output", is_flag=True, help="Output JSON instead of text")
@click.option("--dry-run", is_flag=True, help="Preview without downloading")
@click.option("--force", is_flag=True, help="Overwrite existing files")
@click.option("--no-clobber", is_flag=True, help="Skip if file exists")
@click.pass_context
def download_data_table(
    ctx,
    output_path,
    notebook,
    latest,
    earliest,
    download_all,
    name,
    artifact_id,
    json_output,
    dry_run,
    force,
    no_clobber,
):
    """Download data table(s) as CSV files.

    \b
    Examples:
      # Download latest data table to default filename
      notebooklm download data-table

      # Download to specific path
      notebooklm download data-table my-data.csv

      # Download all data tables to directory
      notebooklm download data-table --all ./data-tables/

      # Download specific artifact by name
      notebooklm download data-table --name "chapter 3"

      # Preview without downloading
      notebooklm download data-table --all --dry-run
    """
    _execute_download(
        ctx,
        "data-table",
        9,
        ".csv",
        "./data-tables",
        output_path,
        notebook,
        latest,
        earliest,
        download_all,
        name,
        artifact_id,
        json_output,
        dry_run,
        force,
        no_clobber,
    )
