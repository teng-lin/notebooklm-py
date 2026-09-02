"""Skill management commands.

Thin Click adapter over the transport-neutral
:mod:`notebooklm._app.skill` core. The install-target catalog, path/version
helpers, and the per-target ``create`` / ``up_to_date`` / ``overwrite``
classification live in ``_app``; this module imports those names into its own
namespace (so ``patch.object(skill_cmd, ...)`` test seams and the
``from notebooklm.cli.skill_cmd import ...`` imports keep resolving) and owns
the Click I/O, the atomic file writes, and the packaged-source loader.
"""

import re
import tempfile
from pathlib import Path

import click

from .._app.skill import (
    DEFAULT_ARCHIVE_FILENAME,
    SCOPES,
    SKILL_ARCHIVE_ENTRY,
    TARGET_CREATE,
    TARGET_OVERWRITE,
    TARGET_UP_TO_DATE,
    TARGETS,
    SkillTarget,
    add_version_comment,
    build_skill_archive_bytes,
    classify_target,
    get_installed_content,
    get_package_version,
    get_scope_root,
    get_skill_content_mismatch,
    get_skill_path,
    get_skill_version,
    iter_targets,
    remove_empty_parents,
    report_mixed_no_clobber_up_to_date,
)
from ..io import replace_file_atomically
from .agent_templates import get_agent_source_content
from .error_handler import exit_with_code, output_error
from .rendering import console, emit_status, json_output_response

__all__ = [
    "DEFAULT_ARCHIVE_FILENAME",
    "SCOPES",
    "SKILL_ARCHIVE_ENTRY",
    "TARGET_CREATE",
    "TARGET_OVERWRITE",
    "TARGET_UP_TO_DATE",
    "TARGETS",
    "SkillTarget",
    "add_version_comment",
    "atomic_write_bytes",
    "atomic_write_text",
    "build_skill_archive_bytes",
    "classify_target",
    "get_installed_content",
    "get_package_version",
    "get_scope_root",
    "get_skill_content_mismatch",
    "get_skill_path",
    "get_skill_source_content",
    "get_skill_version",
    "iter_targets",
    "remove_empty_parents",
    "report_mixed_no_clobber_up_to_date",
    "skill",
]

# Documented API ceiling for the SKILL.md frontmatter ``description``; longer
# descriptions may be rejected at upload time.
_DESCRIPTION_LIMIT = 1024


def get_skill_source_content() -> str | None:
    """Read the skill source file from package data."""
    return get_agent_source_content("claude")


def atomic_write_text(path: Path, content: str) -> None:
    """Atomically write ``content`` to ``path`` (temp file + atomic replace).

    Mirrors the same-directory tempfile + ``os.replace`` pattern used by
    :func:`notebooklm.io.atomic_write_json`, including bounded retries for
    transient Windows replace races.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_path = Path(temp_file.name)
            temp_file.write(content)
        replace_file_atomically(temp_path, path)
    except Exception:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except Exception:
                pass
        raise


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Atomically write ``data`` to ``path`` (binary twin of ``atomic_write_text``)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_path = Path(temp_file.name)
            temp_file.write(data)
        replace_file_atomically(temp_path, path)
    except Exception:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except Exception:
                pass
        raise


def _frontmatter_description(content: str) -> str | None:
    """Extract the single-line frontmatter ``description`` value, if parseable.

    Uses the same ``split("---", 2)`` frontmatter shape as
    ``add_version_comment``; returns ``None`` (warning skipped) rather than
    guessing when the block or field is absent.
    """
    parts = content.split("---", 2)
    if len(parts) < 3:
        return None
    match = re.search(r"^description:\s*(.*)$", parts[1], flags=re.MULTILINE)
    return match.group(1).strip() if match else None


def _skill_repair_command(scope: str, target: str) -> str:
    """Return the command that repairs one reported skill target."""
    command = f"notebooklm skill install --scope {scope} --target {target}"
    if scope == "project":
        command += " --force"
    return command


@click.group()
def skill():
    """Manage NotebookLM agent skill integration."""
    pass


@skill.command()
@click.option(
    "--scope",
    type=click.Choice(SCOPES),
    default="user",
    show_default=True,
    help="Install for the current user or into the current project.",
)
@click.option(
    "--target",
    "target_name",
    type=click.Choice(["all", *TARGETS]),
    default="all",
    show_default=True,
    help="Install for Claude Code, universal agent skill directories, or both.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help=(
        "Project scope only. Print what would be written without touching the "
        "filesystem; combine with --force or --no-clobber to preview that mode."
    ),
)
@click.option(
    "--no-clobber",
    is_flag=True,
    default=False,
    help=(
        "Project scope only. Skip target files whose existing content differs "
        "from the packaged skill; still creates targets that do not yet exist."
    ),
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Project scope only. Overwrite differing target files unconditionally.",
)
def install(scope: str, target_name: str, dry_run: bool, no_clobber: bool, force: bool):
    """Install or update the NotebookLM skill for supported agent directories.

    With ``--scope project`` the install is hardened against accidental
    overwrites: by default, if any target file exists with content that differs
    from the packaged skill, no files are written and the command exits 1.
    Pass ``--force`` to overwrite, ``--no-clobber`` to skip differing targets,
    or ``--dry-run`` to preview without writing.
    """
    # Hardening flags are project-scope only -- user scope is per-user setup and
    # keeps the historical "always overwrite" behavior.
    if scope == "user" and (dry_run or no_clobber or force):
        console.print(
            "[red]Error:[/red] --dry-run, --no-clobber, and --force require --scope project."
        )
        exit_with_code(1)

    if force and no_clobber:
        console.print("[red]Error:[/red] --force and --no-clobber are mutually exclusive.")
        exit_with_code(1)

    # Read skill content from package data
    content = get_skill_source_content()
    if content is None:
        console.print("[red]Error:[/red] Skill source not found in package data.")
        console.print("This may indicate an incomplete or corrupted installation.")
        console.print("Try reinstalling: pip install --force-reinstall notebooklm-py")
        exit_with_code(1)

    version = get_package_version()
    stamped_content = add_version_comment(content, version)

    # Per-target classification drives every downstream decision.
    targets = iter_targets(target_name)
    classifications: list[tuple[str, str, Path]] = [
        (target, *classify_target(target, scope, stamped_content)) for target in targets
    ]
    differing = [
        (target, path) for target, status, path in classifications if status == TARGET_OVERWRITE
    ]

    # Default behavior on project scope: refuse to clobber differing files.
    if scope == "project" and differing and not (dry_run or no_clobber or force):
        console.print(
            "[red]Refusing to overwrite[/red] differing skill files (use --force to "
            "overwrite or --no-clobber to skip differing files):"
        )
        for target, path in differing:
            console.print(f"  {TARGETS[target].label}: {path}")
        exit_with_code(1)

    if dry_run:
        console.print("[cyan]Dry run[/cyan] -- no files will be written")
        console.print(f"  Version: {version}")
        console.print(f"  Scope:   {scope}")
        for target, status, path in classifications:
            label = TARGETS[target].label
            if status == TARGET_CREATE:
                console.print(f"  Would create  {label}: {path}")
            elif status == TARGET_UP_TO_DATE:
                console.print(f"  Up to date    {label}: {path}")
            elif status == TARGET_OVERWRITE:
                if no_clobber:
                    console.print(f"  Would skip    {label}: {path} (differs; --no-clobber)")
                else:
                    # Default or --force preview both reach here; --force is the
                    # only mode where writing differing files is possible.
                    action = "Would overwrite" if force else "Would refuse"
                    console.print(f"  {action} {label}: {path}")
        return

    installed_paths: list[tuple[str, Path]] = []
    skipped_up_to_date: list[tuple[str, Path]] = []
    skipped_no_clobber: list[tuple[str, Path]] = []
    failed_targets: list[tuple[str, OSError]] = []

    for target, status, path in classifications:
        if status == TARGET_UP_TO_DATE:
            # Already in sync -- nothing to do, but report it.
            skipped_up_to_date.append((target, path))
            continue
        if status == TARGET_OVERWRITE and no_clobber:
            skipped_no_clobber.append((target, path))
            continue
        # Reaches here for TARGET_CREATE (any mode) or TARGET_OVERWRITE with
        # --force (project) or implicit overwrite (user scope, legacy path).
        try:
            atomic_write_text(path, stamped_content)
            installed_paths.append((target, path))
        except OSError as e:
            failed_targets.append((target, e))

    if installed_paths:
        console.print("[green]Installed[/green] NotebookLM skill")
        console.print(f"  Version: {version}")
        console.print(f"  Scope:   {scope}")
        for target, skill_path in installed_paths:
            console.print(f"  {TARGETS[target].label}: {skill_path}")
        console.print("")
        console.print("NotebookLM commands are now available in the selected skill directories.")

    if skipped_no_clobber:
        console.print(
            f"[yellow]Skipped[/yellow] {len(skipped_no_clobber)} differing target(s) (--no-clobber)"
        )

    report_mixed_no_clobber_up_to_date(
        console.print,
        skipped_up_to_date=skipped_up_to_date,
        skipped_no_clobber=skipped_no_clobber,
        installed_paths=installed_paths,
        failed_targets=failed_targets,
    )

    if not installed_paths and not failed_targets and skipped_up_to_date and not skipped_no_clobber:
        # All targets were already up to date and no writes happened.
        console.print("[green]Up to date[/green] -- no changes needed")
        console.print(f"  Version: {version}")
        console.print(f"  Scope:   {scope}")

    for target, err in failed_targets:
        console.print(f"[red]Failed[/red] to install {TARGETS[target].label}: {err}")

    if failed_targets:
        exit_with_code(1)


@skill.command()
@click.option(
    "--scope",
    type=click.Choice(SCOPES),
    default="user",
    show_default=True,
    help="Inspect user-level or project-level skill installs.",
)
@click.option(
    "--target",
    "target_name",
    type=click.Choice(["all", *TARGETS]),
    default="all",
    show_default=True,
    help="Inspect Claude Code, universal agent skill directories, or both.",
)
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
def status(scope: str, target_name: str, json_output: bool):
    """Check installed skill targets, versions, and content integrity."""
    cli_version = get_package_version()
    try:
        source_content = get_skill_source_content()
    except (OSError, UnicodeError):
        source_content = None
    canonical_content = (
        add_version_comment(source_content, cli_version) if source_content is not None else None
    )
    selected_targets = iter_targets(target_name)
    target_rows = []
    for target in selected_targets:
        skill_path = get_skill_path(target, scope)
        skill_version = get_skill_version(skill_path)
        try:
            installed = skill_path.exists()
        except OSError:
            installed = False
        content_mismatch = get_skill_content_mismatch(skill_path, canonical_content)
        target_rows.append(
            {
                "target": target,
                "label": TARGETS[target].label,
                "installed": installed,
                "path": str(skill_path),
                "skill_version": skill_version if installed else None,
                "version_mismatch": bool(
                    installed and skill_version and skill_version != cli_version
                ),
                "content_mismatch": content_mismatch,
            }
        )
    any_installed = any(row["installed"] for row in target_rows)

    if json_output:
        json_output_response({"scope": scope, "cli_version": cli_version, "targets": target_rows})
        return

    console.print(f"NotebookLM skill status ({scope} scope)")
    console.print(f"  CLI version: {cli_version}")

    for row in target_rows:
        status_label = (
            "[green]Installed[/green]" if row["installed"] else "[yellow]Not installed[/yellow]"
        )
        console.print(f"  {row['label']}: {status_label}")
        console.print(f"    Path: {row['path']}")
        if row["installed"]:
            repair_command = _skill_repair_command(scope, str(row["target"]))
            repair_note = " (overwrites the differing project skill)" if scope == "project" else ""
            console.print(f"    Skill version: {row['skill_version'] or 'unknown'}")
            if row["version_mismatch"]:
                console.print(
                    "    [yellow]Version mismatch[/yellow] - run "
                    f"[cyan]{repair_command}[/cyan]{repair_note}"
                )
            if row["content_mismatch"] is None:
                console.print(
                    "    [yellow]Content comparison unavailable[/yellow] - canonical or "
                    "installed skill could not be read"
                )
            elif row["content_mismatch"] and not row["version_mismatch"]:
                console.print(
                    "    [yellow]Content drift[/yellow] - installed skill differs from this "
                    f"package; run [cyan]{repair_command}[/cyan]{repair_note}"
                )

    if not any_installed:
        console.print("")
        console.print("Run [cyan]notebooklm skill install[/cyan] to install the skill.")


@skill.command()
@click.option(
    "--scope",
    type=click.Choice(SCOPES),
    default="user",
    show_default=True,
    help="Remove user-level or project-level skill installs.",
)
@click.option(
    "--target",
    "target_name",
    type=click.Choice(["all", *TARGETS]),
    default="all",
    show_default=True,
    help="Remove Claude Code, universal agent skill directories, or both.",
)
def uninstall(scope: str, target_name: str):
    """Remove the NotebookLM skill from supported agent directories."""
    removed_targets = []

    for target in iter_targets(target_name):
        skill_path = get_skill_path(target, scope)
        if not skill_path.exists():
            continue
        skill_path.unlink()
        remove_empty_parents(skill_path, scope)
        removed_targets.append(target)

    if not removed_targets:
        console.print("[yellow]Skill not installed[/yellow]")
        return

    console.print("[green]Uninstalled[/green] NotebookLM skill")
    for target in removed_targets:
        console.print(f"  Removed from {TARGETS[target].label}")


@skill.command()
@click.option(
    "--scope",
    type=click.Choice(SCOPES),
    default="user",
    show_default=True,
    help="Read an installed skill from user or project scope.",
)
@click.option(
    "--target",
    "target_name",
    type=click.Choice(["source", *TARGETS]),
    default="source",
    show_default=True,
    help="Show the packaged skill source or an installed target.",
)
def show(scope: str, target_name: str):
    """Display the packaged skill content or an installed target."""
    if target_name == "source":
        content = get_skill_source_content()
        if content is None:
            console.print("[red]Error:[/red] Skill source not found in package data.")
            exit_with_code(1)
        console.print(content)
        return

    content = get_installed_content(target_name, scope)
    if content is None:
        console.print("[yellow]Skill not installed[/yellow]")
        console.print("Run [cyan]notebooklm skill install[/cyan] first.")
        return

    console.print(content)


@skill.command()
@click.option(
    "--output",
    "-o",
    "output",
    type=click.Path(),
    default=None,
    help=(
        f"Archive path to write (default: ./{DEFAULT_ARCHIVE_FILENAME}). "
        "If PATH is an existing directory or ends with a path separator, "
        "the default filename is written inside it."
    ),
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Overwrite an existing archive file.",
)
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
def package(output: str | None, force: bool, json_output: bool):
    """Build a Claude-uploadable skill archive (chat and Cowork).

    Produces a ZIP whose root contains the ``notebooklm/`` skill folder,
    ready for upload via Claude Settings -> Capabilities. Sandboxed agent
    environments (e.g. Claude Cowork) cannot run ``skill install`` against a
    local directory; this archive is the supported hand-off for them.
    """
    content = get_skill_source_content()
    if content is None:
        output_error(
            "Skill source not found in package data. This may indicate an "
            "incomplete or corrupted installation. Try reinstalling: "
            "pip install --force-reinstall notebooklm-py",
            code="SKILL_SOURCE_MISSING",
            json_output=json_output,
            exit_code=1,
        )

    version = get_package_version()
    stamped_content = add_version_comment(content, version)

    if output is None:
        output_path = Path(DEFAULT_ARCHIVE_FILENAME)
    else:
        # A trailing separator signals directory intent even when the
        # directory does not exist yet (``Path`` normalization would drop it).
        dir_intent = output.endswith(("/", "\\"))
        output_path = Path(output)
        if output_path.is_dir() or dir_intent:
            output_path = output_path / DEFAULT_ARCHIVE_FILENAME
    if output_path.exists() and not force:
        output_error(
            f"Refusing to overwrite existing file: {output_path} (use --force to overwrite)",
            code="OUTPUT_EXISTS",
            json_output=json_output,
            exit_code=1,
        )

    description = _frontmatter_description(stamped_content)
    if description is not None and len(description) > _DESCRIPTION_LIMIT:
        emit_status(
            f"Warning: skill description is {len(description)} characters "
            f"(over the {_DESCRIPTION_LIMIT}-character upload limit); "
            "Claude may reject the archive.",
            json_output=json_output,
            style="yellow",
        )

    data = build_skill_archive_bytes(stamped_content)
    try:
        atomic_write_bytes(output_path, data)
    except OSError as e:
        output_error(
            f"Failed to write archive {output_path}: {e}",
            code="WRITE_FAILED",
            json_output=json_output,
            exit_code=1,
        )

    if json_output:
        json_output_response(
            {
                "path": str(output_path),
                "version": version,
                "entries": [SKILL_ARCHIVE_ENTRY],
                "size_bytes": len(data),
            }
        )
        return

    console.print("[green]Packaged[/green] NotebookLM skill archive")
    console.print(f"  Version: {version}")
    console.print(f"  Path:    {output_path}")
    console.print(f"  Entry:   {SKILL_ARCHIVE_ENTRY}")
    console.print("")
    console.print("Upload via Claude Settings -> Capabilities (works in chat and Cowork).")
