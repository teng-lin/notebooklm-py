"""Agent integration commands."""

import click

from .agent_templates import get_agent_source_content
from .error_handler import exit_with_code
from .rendering import console, stderr_console


@click.group()
def agent():
    """Show bundled instructions for supported agent environments."""
    pass


@agent.command("show")
@click.argument("target", type=click.Choice(["codex", "claude"], case_sensitive=False))
def show_agent(target: str):
    """Display instructions for Codex or Claude Code."""
    content = get_agent_source_content(target)
    if content is None:
        stderr_console.print(f"[red]Error:[/red] {target} instructions not found in package data.")
        exit_with_code(1)

    console.print(content)
