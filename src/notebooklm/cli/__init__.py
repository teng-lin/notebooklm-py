"""NotebookLM CLI package.

This package provides the command-line interface for NotebookLM automation.

Command groups are organized into separate modules:
- source.py: Source management commands (includes add-research)
- artifact.py: Artifact management commands
- generate.py: Content generation commands
- download.py: Download commands
- note.py: Note management commands
- session.py: Session and context commands (login, use, status, clear)
- notebook.py: Notebook management commands (list, create, delete, rename, share, featured, summary, analytics)
- chat.py: Chat commands (ask, configure, history)

Re-exports from helpers for backward compatibility with tests.
"""

# Command groups (subcommand style)
from .source import source
from .artifact import artifact
from .generate import generate
from .download import download
from .note import note

# Register functions (top-level command style)
from .session import register_session_commands
from .notebook import register_notebook_commands
from .chat import register_chat_commands

from .helpers import (
    # Console
    console,
    # Async
    run_async,
    # Auth
    get_client,
    get_auth_tokens,
    # Context
    CONTEXT_FILE,
    BROWSER_PROFILE_DIR,
    get_current_notebook,
    set_current_notebook,
    clear_context,
    get_current_conversation,
    set_current_conversation,
    require_notebook,
    resolve_notebook_id,
    resolve_source_id,
    # Errors
    handle_error,
    handle_auth_error,
    # Decorators
    with_client,
    # Output
    json_output_response,
    json_error_response,
    # Display
    ARTIFACT_TYPE_DISPLAY,
    ARTIFACT_TYPE_MAP,
    get_artifact_type_display,
    detect_source_type,
    get_source_type_display,
)

from .options import (
    # Individual option decorators
    notebook_option,
    json_option,
    wait_option,
    source_option,
    artifact_option,
    output_option,
    # Composite decorators
    standard_options,
    generate_options,
)

__all__ = [
    # Command groups (subcommand style)
    "source",
    "artifact",
    "generate",
    "download",
    "note",
    # Register functions (top-level command style)
    "register_session_commands",
    "register_notebook_commands",
    "register_chat_commands",
    # Console
    "console",
    # Async
    "run_async",
    # Auth
    "get_client",
    "get_auth_tokens",
    # Context
    "CONTEXT_FILE",
    "BROWSER_PROFILE_DIR",
    "get_current_notebook",
    "set_current_notebook",
    "clear_context",
    "get_current_conversation",
    "set_current_conversation",
    "require_notebook",
    "resolve_notebook_id",
    "resolve_source_id",
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
    "standard_options",
    "generate_options",
    # Output
    "json_output_response",
    "json_error_response",
    # Display
    "ARTIFACT_TYPE_DISPLAY",
    "ARTIFACT_TYPE_MAP",
    "get_artifact_type_display",
    "detect_source_type",
    "get_source_type_display",
]
