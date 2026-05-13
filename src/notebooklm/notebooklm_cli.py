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
  notebooklm research <command>       # Research status/wait

LLM-friendly design:
  # Set context once, then use simple commands
  notebooklm use nb123
  notebooklm generate video "a funny explainer for kids"
  notebooklm generate audio "deep dive focusing on chapter 3"
  notebooklm ask "what are the key themes?"
"""

# Runtime Python version guard (must run before any PEP 604 syntax is evaluated)
import sys

from ._version_check import check_python_version as _check_python_version

_check_python_version()
del _check_python_version

import asyncio
import logging
import os
from pathlib import Path

# =============================================================================
# WINDOWS COMPATIBILITY FIXES (issue #75, #79, #80, #318)
# Must be applied before any async code runs
# =============================================================================


def _reconfigure_output_stream(stream) -> None:
    """Use UTF-8 with replacement for active Windows text streams."""
    if stream is None:
        return
    reconfigure = getattr(stream, "reconfigure", None)
    if not callable(reconfigure):
        return
    try:
        reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, TypeError, ValueError):
        pass


def _configure_windows_runtime() -> None:
    """Apply Windows runtime fixes before Click and Rich command modules load."""
    if sys.platform != "win32":
        return

    # Fix #79: Windows asyncio ProactorEventLoop can hang indefinitely at IOCP layer
    # (GetQueuedCompletionStatus) in certain environments like Sandboxie.
    # SelectorEventLoop avoids this issue.
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    # Fix #80/#318: changing PYTHONUTF8 after startup does not update the already
    # created stdout/stderr TextIOWrappers. Reconfigure the live streams so Rich's
    # legacy Windows renderer can write emoji and other Unicode output safely.
    os.environ.setdefault("PYTHONUTF8", "1")
    _reconfigure_output_stream(sys.stdout)
    _reconfigure_output_stream(sys.stderr)


_configure_windows_runtime()

import click

from . import __version__

# Import command groups from cli package
from .cli import (
    agent,
    artifact,
    download,
    generate,
    language,
    note,
    profile,
    register_chat_commands,
    register_doctor_command,
    register_notebook_commands,
    # Register functions for top-level commands
    register_session_commands,
    research,
    share,
    skill,
    source,
)
from .cli.grouped import SectionedGroup

# Import helpers needed for backward compatibility with tests


# =============================================================================
# MAIN CLI GROUP
# =============================================================================


@click.group(cls=SectionedGroup)
@click.version_option(version=__version__, prog_name="NotebookLM CLI")
@click.option(
    "--storage",
    type=click.Path(exists=False),
    default=None,
    help="Path to storage_state.json (default: ~/.notebooklm/profiles/<profile>/storage_state.json)",
)
@click.option(
    "-p",
    "--profile",
    default=None,
    help="Profile name (default: from config or 'default'). Use 'notebooklm profile list' to see profiles.",
)
@click.option(
    "-v",
    "--verbose",
    count=True,
    help="Increase verbosity (-v for INFO, -vv for DEBUG)",
)
@click.pass_context
def cli(ctx, storage, profile, verbose):
    """NotebookLM CLI.

    \b
    Quick start:
      notebooklm login              # Authenticate first
      notebooklm list               # List your notebooks
      notebooklm create "My Notes"  # Create a notebook
      notebooklm ask "Hi"           # Ask the current notebook a question

    \b
    Tip: Use partial notebook IDs (e.g., 'notebooklm use abc' matches 'abc123...')
    """
    # Configure logging based on verbosity: -v for INFO, -vv+ for DEBUG
    if verbose >= 2:
        logging.getLogger("notebooklm").setLevel(logging.DEBUG)
    elif verbose == 1:
        logging.getLogger("notebooklm").setLevel(logging.INFO)

    # Set up profile system
    from .paths import set_active_profile

    # Always reset to prevent leaking across CliRunner invocations
    set_active_profile(profile)

    # Only set up profiles dir when not using an explicit auth source.
    # --storage and NOTEBOOKLM_AUTH_JSON bypass the profile system entirely
    # and must not require a writable NOTEBOOKLM_HOME.
    if not storage and not os.environ.get("NOTEBOOKLM_AUTH_JSON"):
        try:
            from .migration import ensure_profiles_dir

            ensure_profiles_dir()
        except ValueError as e:
            # Invalid profile name (e.g., path traversal in env var or config)
            import click as _click

            raise _click.ClickException(str(e)) from None

    ctx.ensure_object(dict)
    ctx.obj["storage_path"] = Path(storage) if storage else None
    ctx.obj["profile"] = profile


# =============================================================================
# REGISTER COMMANDS
# =============================================================================

# Register top-level commands from modules
register_session_commands(cli)
register_notebook_commands(cli)
register_chat_commands(cli)
register_doctor_command(cli)

# Register command groups (subcommand style)
cli.add_command(source)
cli.add_command(artifact)
cli.add_command(agent)
cli.add_command(generate)
cli.add_command(download)
cli.add_command(note)
cli.add_command(share)
cli.add_command(skill)
cli.add_command(research)
cli.add_command(language)
cli.add_command(profile)


# =============================================================================
# MAIN ENTRY POINT
# =============================================================================


def main():
    cli()


if __name__ == "__main__":
    main()
