"""NotebookLM CLI package.

This package provides the command-line interface for NotebookLM automation.

Command groups are organized into separate modules:
- source.py: Source management commands (includes add-research)
- artifact.py: Artifact management commands
- generate.py: Content generation commands
- download.py: Download commands
- note.py: Note management commands
- session.py: Session and context commands (login, use, status, clear)
- notebook.py: Notebook management commands (list, create, delete, rename, share, summary)
- chat.py: Chat commands (ask, configure, history)

Re-exports from helpers for backward compatibility with tests.
"""

# Command groups (subcommand style)
from .artifact import artifact
from .chat import register_chat_commands
from .download import download
from .generate import generate
from .helpers import (
    # Display
    ARTIFACT_TYPE_MAP,
    BROWSER_PROFILE_DIR,
    # Context
    CONTEXT_FILE,
    clear_context,
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
from .language import get_language, language
from .note import note
from .notebook import register_notebook_commands
from .options import (
    artifact_option,
    generate_options,
    json_option,
    # Individual option decorators
    notebook_option,
    output_option,
    source_option,
    # Composite decorators
    standard_options,
    wait_option,
)
from .research import research

# Register functions (top-level command style)
from .session import register_session_commands
from .skill import skill
from .source import source

__all__ = [
    # Command groups (subcommand style)
    "source",
    "artifact",
    "generate",
    "download",
    "note",
    "skill",
    "research",
    "language",
    # Language config
    "get_language",
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
    "standard_options",
    "generate_options",
    # Output
    "json_output_response",
    "json_error_response",
    # Display
    "ARTIFACT_TYPE_MAP",
    "get_artifact_type_display",
    "get_source_type_display",
]
