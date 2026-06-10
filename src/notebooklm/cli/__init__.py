"""NotebookLM CLI package.

This package provides the command-line interface for NotebookLM automation.

Command groups are organized into separate ``*_cmd`` modules (named to
break Python's package-attribute shadowing — see
``tests/_guardrails/test_no_module_shadowing.py`` for the invariant this protects):

- ``source_cmd``: Source management commands (includes add-research)
- ``artifact_cmd``: Artifact management commands
- ``agent_cmd``: Agent integration helpers
- ``generate_cmd``: Content generation commands
- ``download_cmd``: Download commands
- ``note_cmd``: Note management commands
- ``session_cmd``: Session and context commands (login, use, status, clear)
- ``notebook_cmd``: Notebook management commands (list, create, delete, rename, share, summary)
- ``chat_cmd``: Chat commands (ask, configure, history)
- ``doctor_cmd``: Diagnostic and migration commands
- ``profile_cmd``: Profile management commands

The click groups themselves are still exported here under their historical
names (``source``, ``artifact``, …) so ``from notebooklm.cli import source``
keeps working for the public CLI assembler in ``notebooklm_cli.py`` and any
external importer.
"""

# Command groups (subcommand style)
from .agent_cmd import agent
from .artifact_cmd import artifact
from .chat_cmd import register_chat_commands
from .doctor_cmd import register_doctor_command
from .download_cmd import download
from .generate_cmd import generate
from .helpers import (
    clear_context,
    cli_name_to_artifact_type,
    # Console
    console,
    get_artifact_type_display,
    get_auth_tokens,
    # Auth
    get_client,
    get_current_conversation,
    get_current_notebook,
    get_source_type_display,
    handle_auth_error,
    # Errors
    handle_error,
    json_error_response,
    # Output
    json_output_response,
    require_notebook,
    resolve_artifact_id,
    resolve_notebook_id,
    resolve_source_id,
    # Async
    run_async,
    set_current_conversation,
    set_current_notebook,
    # Decorators
    with_client,
)
from .label_cmd import label
from .language_cmd import get_language, language
from .mcp_cmd import mcp
from .note_cmd import note
from .notebook_cmd import register_notebook_commands
from .options import (
    artifact_option,
    json_option,
    # Individual option decorators
    notebook_option,
    output_option,
    source_option,
    wait_option,
)
from .profile_cmd import profile
from .research_cmd import research

# Register functions (top-level command style)
from .session_cmd import register_session_commands
from .share_cmd import share
from .skill_cmd import skill
from .source_cmd import source

__all__ = [
    # Command groups (subcommand style)
    "source",
    "artifact",
    "agent",
    "generate",
    "download",
    "note",
    "label",
    "share",
    "skill",
    "research",
    "language",
    "profile",
    "mcp",
    # Language config
    "get_language",
    # Register functions (top-level command style)
    "register_session_commands",
    "register_notebook_commands",
    "register_chat_commands",
    "register_doctor_command",
    # Console
    "console",
    # Async
    "run_async",
    # Auth
    "get_client",
    "get_auth_tokens",
    # Context
    "get_current_notebook",
    "set_current_notebook",
    "clear_context",
    "get_current_conversation",
    "set_current_conversation",
    "require_notebook",
    "resolve_notebook_id",
    "resolve_source_id",
    "resolve_artifact_id",
    # Errors
    "handle_error",
    "handle_auth_error",
    # Decorators
    "with_client",
    # Option Decorators
    "notebook_option",
    "json_option",
    "wait_option",
    "source_option",
    "artifact_option",
    "output_option",
    # Output
    "json_output_response",
    "json_error_response",
    # Display
    "cli_name_to_artifact_type",
    "get_artifact_type_display",
    "get_source_type_display",
]
