"""Skill management commands.

Commands for managing the Claude Code skill integration.
"""

import re
from importlib import resources
from pathlib import Path

import click

from .helpers import console

# Skill paths
SKILL_DEST_DIR = Path.home() / ".claude" / "skills" / "notebooklm"
SKILL_DEST = SKILL_DEST_DIR / "SKILL.md"


def get_skill_source_content() -> str | None:
    """Read the skill source file from package data."""
    try:
        # Python 3.9+ way to read package data (use / operator for path traversal)
        return (resources.files("notebooklm") / "data" / "SKILL.md").read_text()
    except (FileNotFoundError, TypeError):
        return None


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

    with open(skill_path) as f:
        content = f.read(500)  # Read first 500 chars

    match = re.search(r"notebooklm-py v([\d.]+)", content)
    return match.group(1) if match else None


@click.group()
def skill():
    """Manage Claude Code skill integration."""
    pass


@skill.command()
def install():
    """Install or update the NotebookLM skill for Claude Code.

    Copies the skill file to ~/.claude/skills/notebooklm/SKILL.md
    and embeds the current package version for tracking.
    """
    # Read skill content from package data
    content = get_skill_source_content()
    if content is None:
        console.print("[red]Error:[/red] Skill source not found in package data.")
        console.print("This may indicate an incomplete or corrupted installation.")
        console.print("Try reinstalling: pip install --force-reinstall notebooklm-py")
        raise SystemExit(1)

    # Create destination directory
    SKILL_DEST_DIR.mkdir(parents=True, exist_ok=True)

    # Embed version in skill file (after frontmatter)
    version = get_package_version()
    version_comment = f"<!-- notebooklm-py v{version} -->\n"

    # Insert after the closing --- of frontmatter
    if "---" in content:
        parts = content.split("---", 2)
        if len(parts) >= 3:
            content = f"---{parts[1]}---\n{version_comment}{parts[2]}"
        else:
            content = version_comment + content
    else:
        content = version_comment + content

    # Write to destination
    with open(SKILL_DEST, "w") as f:
        f.write(content)

    console.print(f"[green]Installed[/green] NotebookLM skill to {SKILL_DEST}")
    console.print(f"  Version: {version}")
    console.print("")
    console.print("Claude Code will now recognize NotebookLM commands.")
    console.print("Try: [cyan]/notebooklm[/cyan] or ask Claude to 'create a podcast about X'")


@skill.command()
def status():
    """Check if the skill is installed and show version info."""
    cli_version = get_package_version()
    skill_version = get_skill_version(SKILL_DEST)

    if not SKILL_DEST.exists():
        console.print("[yellow]Not installed[/yellow]")
        console.print(f"  CLI version: {cli_version}")
        console.print("")
        console.print("Run [cyan]notebooklm skill install[/cyan] to install the skill.")
        return

    console.print(f"[green]Installed[/green] at {SKILL_DEST}")
    console.print(f"  Skill version: {skill_version or 'unknown'}")
    console.print(f"  CLI version:   {cli_version}")

    if skill_version and skill_version != cli_version:
        console.print("")
        console.print(
            "[yellow]Version mismatch![/yellow] Run [cyan]notebooklm skill install[/cyan] to update."
        )


@skill.command()
def uninstall():
    """Remove the NotebookLM skill from Claude Code."""
    if not SKILL_DEST.exists():
        console.print("[yellow]Skill not installed[/yellow]")
        return

    # Remove the skill file
    SKILL_DEST.unlink()

    # Remove the directory if empty
    try:
        SKILL_DEST_DIR.rmdir()
    except OSError:
        pass  # Directory not empty or other error

    console.print("[green]Uninstalled[/green] NotebookLM skill")
    console.print("Claude Code will no longer recognize NotebookLM commands.")


@skill.command()
def show():
    """Display the skill file content."""
    if not SKILL_DEST.exists():
        console.print("[yellow]Skill not installed[/yellow]")
        console.print("Run [cyan]notebooklm skill install[/cyan] first.")
        return

    with open(SKILL_DEST) as f:
        content = f.read()

    console.print(content)
