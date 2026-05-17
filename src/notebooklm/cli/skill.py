"""Skill management commands."""

import re
from dataclasses import dataclass
from pathlib import Path

import click

from .agent_templates import get_agent_source_content
from .rendering import console


@dataclass(frozen=True)
class SkillTarget:
    """Install target metadata."""

    label: str
    relative_path: Path


TARGETS = {
    "claude": SkillTarget("Claude Code", Path(".claude") / "skills" / "notebooklm" / "SKILL.md"),
    "agents": SkillTarget("Agent Skills", Path(".agents") / "skills" / "notebooklm" / "SKILL.md"),
}
SCOPES = ("user", "project")


def get_skill_source_content() -> str | None:
    """Read the skill source file from package data."""
    return get_agent_source_content("claude")


def get_package_version() -> str:
    """Get the current package version."""
    try:
        from .. import __version__

        return __version__
    except ImportError:
        return "unknown"


def get_skill_version(skill_path: Path) -> str | None:
    """Extract version from skill file header comment."""
    if not skill_path.exists():
        return None

    with open(skill_path, encoding="utf-8") as f:
        content = f.read(500)  # Read first 500 chars

    match = re.search(r"notebooklm-py v([\d.]+)", content)
    return match.group(1) if match else None


def get_scope_root(scope: str) -> Path:
    """Resolve the root directory for a given install scope."""
    return Path.home() if scope == "user" else Path.cwd()


def get_skill_path(target: str, scope: str) -> Path:
    """Resolve the installed skill path for a target and scope."""
    return get_scope_root(scope) / TARGETS[target].relative_path


def iter_targets(target: str) -> list[str]:
    """Expand 'all' into concrete targets."""
    return list(TARGETS) if target == "all" else [target]


def add_version_comment(content: str, version: str) -> str:
    """Embed the CLI version into a skill file."""
    version_comment = f"<!-- notebooklm-py v{version} -->\n"

    if "---" in content:
        parts = content.split("---", 2)
        if len(parts) >= 3:
            return f"---{parts[1]}---\n{version_comment}{parts[2].lstrip()}"

    return version_comment + content


def remove_empty_parents(skill_path: Path, scope: str) -> None:
    """Remove empty skill directories without touching the scope root."""
    stop_at = get_scope_root(scope)
    current = skill_path.parent
    while current != stop_at:
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


def get_installed_content(target: str, scope: str) -> str | None:
    """Read an installed skill file."""
    skill_path = get_skill_path(target, scope)
    if not skill_path.exists():
        return None
    return skill_path.read_text(encoding="utf-8")


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
def install(scope: str, target_name: str):
    """Install or update the NotebookLM skill for supported agent directories."""
    # Read skill content from package data
    content = get_skill_source_content()
    if content is None:
        console.print("[red]Error:[/red] Skill source not found in package data.")
        console.print("This may indicate an incomplete or corrupted installation.")
        console.print("Try reinstalling: pip install --force-reinstall notebooklm-py")
        raise SystemExit(1)

    version = get_package_version()
    stamped_content = add_version_comment(content, version)
    installed_paths = []
    failed_targets = []

    for target in iter_targets(target_name):
        skill_path = get_skill_path(target, scope)
        try:
            skill_path.parent.mkdir(parents=True, exist_ok=True)
            skill_path.write_text(stamped_content, encoding="utf-8")
            installed_paths.append((target, skill_path))
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

    for target, err in failed_targets:
        console.print(f"[red]Failed[/red] to install {TARGETS[target].label}: {err}")

    if failed_targets:
        raise SystemExit(1)


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
def status(scope: str, target_name: str):
    """Check installed skill targets and version info."""
    cli_version = get_package_version()
    selected_targets = iter_targets(target_name)
    any_installed = False

    console.print(f"NotebookLM skill status ({scope} scope)")
    console.print(f"  CLI version: {cli_version}")

    for target in selected_targets:
        skill_path = get_skill_path(target, scope)
        skill_version = get_skill_version(skill_path)
        status_label = (
            "[green]Installed[/green]" if skill_path.exists() else "[yellow]Not installed[/yellow]"
        )
        console.print(f"  {TARGETS[target].label}: {status_label}")
        console.print(f"    Path: {skill_path}")
        if skill_path.exists():
            any_installed = True
            console.print(f"    Skill version: {skill_version or 'unknown'}")
            if skill_version and skill_version != cli_version:
                console.print(
                    "    [yellow]Version mismatch[/yellow] - run [cyan]notebooklm skill install[/cyan]"
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
            raise SystemExit(1)
        console.print(content)
        return

    content = get_installed_content(target_name, scope)
    if content is None:
        console.print("[yellow]Skill not installed[/yellow]")
        console.print("Run [cyan]notebooklm skill install[/cyan] first.")
        return

    console.print(content)
