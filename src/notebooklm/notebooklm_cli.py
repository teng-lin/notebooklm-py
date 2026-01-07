"""CLI interface for NotebookLM automation.

Command structure:
  notebooklm login                    # Authenticate
  notebooklm use <notebook_id>        # Set current notebook context
  notebooklm status                   # Show current context
  notebooklm list                     # List notebooks
  notebooklm create <title>           # Create notebook
  notebooklm ask <question>           # Ask the current notebook a question

  notebooklm source <command>         # Source operations
  notebooklm artifact <command>       # Artifact management
  notebooklm generate <type>          # Generate content
  notebooklm download <type>          # Download content
  notebooklm note <command>           # Note operations

LLM-friendly design:
  # Set context once, then use simple commands
  notebooklm use nb123
  notebooklm generate video "a funny explainer for kids"
  notebooklm generate audio "deep dive focusing on chapter 3"
  notebooklm ask "what are the key themes?"
"""

from pathlib import Path

import click

from . import __version__
from .auth import DEFAULT_STORAGE_PATH

# Import command groups from cli package
from .cli import (
    source,
    artifact,
    generate,
    download,
    note,
    # Register functions for top-level commands
    register_session_commands,
    register_notebook_commands,
    register_chat_commands,
    register_insights_commands,
)
from .cli.grouped import SectionedGroup

# Import helpers needed for backward compatibility with tests
from .cli.helpers import (
    console,
    get_artifact_type_display,
    detect_source_type,
    ARTIFACT_TYPE_DISPLAY,
    ARTIFACT_TYPE_MAP,
)


# =============================================================================
# MAIN CLI GROUP
# =============================================================================


@click.group(cls=SectionedGroup)
@click.version_option(version=__version__, prog_name="NotebookLM CLI")
@click.option(
    "--storage",
    type=click.Path(exists=False),
    default=None,
    help=f"Path to storage_state.json (default: {DEFAULT_STORAGE_PATH})",
)
@click.pass_context
def cli(ctx, storage):
    """NotebookLM automation CLI.

    \b
    Quick start:
      notebooklm login              # Authenticate first
      notebooklm list               # List your notebooks
      notebooklm create "My Notes"  # Create a notebook
      notebooklm ask "Hi"           # Ask the current notebook a question

    \b
    Tip: Use partial notebook IDs (e.g., 'notebooklm use abc' matches 'abc123...')
    """
    ctx.ensure_object(dict)
    ctx.obj["storage_path"] = Path(storage) if storage else None


# =============================================================================
# REGISTER COMMANDS
# =============================================================================

# Register top-level commands from modules
register_session_commands(cli)
register_notebook_commands(cli)
register_chat_commands(cli)
register_insights_commands(cli)

# Register command groups (subcommand style)
cli.add_command(source)
cli.add_command(artifact)
cli.add_command(generate)
cli.add_command(download)
cli.add_command(note)


# =============================================================================
# MAIN ENTRY POINT
# =============================================================================


def main():
    cli()


if __name__ == "__main__":
    main()
