"""NotebookLM CLI package.

This package provides the command-line interface for NotebookLM automation.

Re-exports from helpers for backward compatibility with tests.
"""

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
