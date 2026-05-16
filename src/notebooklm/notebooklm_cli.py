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

Architecture:
    - This module is the entry-point assembler invoked via the ``notebooklm``
      console script (see ``[project.scripts]`` in ``pyproject.toml``).
    - It imports command groups from the ``notebooklm.cli`` package and
      registers them on the top-level Click group ``notebooklm``.
    - The ``cli/`` package contains the actual command implementations
      (one module per command group: ``session``, ``notebook``, ``source``,
      ``artifact``, ``generate``, ``download``, ``chat``, ``note``,
      ``doctor``, ``profile``, ``agent``).
    - Editing CLI behavior: change ``cli/<group>.py``. Editing CLI surface
      (adding a new top-level command): import + register here.

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
        # best-effort: stdout.reconfigure unavailable on this platform.
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
@click.option(
    "--quiet",
    is_flag=True,
    default=False,
    help=(
        "Suppress INFO/WARN log records on stderr (only ERROR survives). "
        "Mutually exclusive with -v/-vv."
    ),
)
@click.pass_context
def cli(ctx, storage, profile, verbose, quiet):
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
    # ``--quiet`` and ``-v/-vv`` resolve to incompatible log-level intents
    # (ERROR vs INFO/DEBUG). Honoring either silently would surprise the
    # other caller; reject the conflict explicitly so the user can drop one
    # flag.
    if quiet and verbose:
        raise click.UsageError("--quiet and -v are mutually exclusive.")

    # Configure logging based on verbosity: -v for INFO, -vv+ for DEBUG.
    # ``--quiet`` raises the floor to ERROR so cron / CI logs stay clean
    # while still surfacing real failures.
    if quiet:
        logging.getLogger("notebooklm").setLevel(logging.ERROR)
    elif verbose >= 2:
        logging.getLogger("notebooklm").setLevel(logging.DEBUG)
        # DEBUG logging on httpx/urllib3 emits full URLs and headers — install
        # redaction so credentials don't leak via third-party loggers.
        from .log import install_redaction

        install_redaction("httpx", "urllib3")
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
    # Canonicalize once at the boundary: ``--storage ~/foo.json`` and
    # ``--storage /Users/x/foo.json`` must map to the same sibling-context
    # namespace (see ``cli.helpers._current_storage_override``).
    ctx.obj["storage_path"] = Path(storage).expanduser().resolve() if storage else None
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
# SHELL COMPLETION
# =============================================================================


@cli.command("completion")
@click.argument("shell", type=click.Choice(["bash", "zsh", "fish"]))
def completion_cmd(shell: str) -> None:
    """Print the shell completion script for SHELL.

    Pipe the output into a file your shell sources at startup. Click handles
    the ``_NOTEBOOKLM_COMPLETE`` env-var protocol automatically once the
    script is sourced; only the script needs to be installed.

    \b
    Install (one-time):
      # bash (~/.bashrc)
      notebooklm completion bash > ~/.notebooklm-complete.bash
      echo 'source ~/.notebooklm-complete.bash' >> ~/.bashrc

      # zsh (anywhere on $fpath)
      notebooklm completion zsh > ~/.zfunc/_notebooklm

      # fish
      notebooklm completion fish > ~/.config/fish/completions/notebooklm.fish

    Then ``notebooklm <cmd> -n <TAB>`` lists notebook IDs from the active
    profile (best-effort — no suggestions when not authenticated).
    """
    # Click ships shell-specific completion classes that emit the script
    # body. We just print whatever ``source()`` returns and let the user
    # redirect it themselves; auto-installing into shell configs would be
    # too magical and would hide the install path from users who care.
    from click.shell_completion import BashComplete, FishComplete, ZshComplete

    cls_map = {"bash": BashComplete, "zsh": ZshComplete, "fish": FishComplete}
    completer_cls = cls_map[shell]
    completer = completer_cls(cli, {}, "notebooklm", "_NOTEBOOKLM_COMPLETE")
    click.echo(completer.source())


# ``completion`` is a one-time install command (like ``login``) so it lives
# in the Session section. The binning is declared in
# ``cli/grouped.py::SectionedGroup.command_sections`` so the no-orphans
# guardrail in ``tests/unit/cli/test_grouped.py`` finds it.


# =============================================================================
# MAIN ENTRY POINT
# =============================================================================


def main():
    cli()


if __name__ == "__main__":
    main()
