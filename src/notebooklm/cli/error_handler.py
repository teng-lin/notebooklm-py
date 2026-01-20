"""Centralized CLI error handling.

This module provides a context manager for consistent error handling
across all CLI commands.
"""

import json
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

import click

from ..exceptions import (
    AuthError,
    ConfigurationError,
    NetworkError,
    NotebookLMError,
    RateLimitError,
    RPCError,
    ValidationError,
)


def _output_error(
    message: str,
    code: str,
    json_output: bool,
    exit_code: int,
    extra: dict[str, Any] | None = None,
    hint: str | None = None,
) -> None:
    """Output error message in text or JSON format and exit.

    Args:
        message: Human-readable error message
        code: Error code for JSON output (e.g., "RATE_LIMITED", "AUTH_ERROR")
        json_output: If True, output as JSON; otherwise as text
        exit_code: Exit code to use
        extra: Additional fields to include in JSON output
        hint: Additional hint to show in text mode
    """
    if json_output:
        response: dict = {"error": True, "code": code, "message": message}
        if extra:
            response.update(extra)
        click.echo(json.dumps(response, indent=2))
    else:
        click.echo(message, err=True)
        if hint:
            click.echo(hint, err=True)
    raise SystemExit(exit_code)


@contextmanager
def handle_errors(verbose: bool = False, json_output: bool = False) -> Generator[None, None, None]:
    """Context manager for consistent CLI error handling.

    Catches library exceptions and converts them to user-friendly
    error messages with appropriate exit codes.

    Exit codes:
        1: User/application error (validation, auth, rate limit, etc.)
        2: System/unexpected error (bugs, unhandled exceptions)
        130: Keyboard interrupt (128 + signal 2)

    Args:
        verbose: If True, show additional debug info (method_id, etc.)
        json_output: If True, output errors as JSON

    Example:
        @click.command()
        def my_command():
            with handle_errors():
                # ... command logic ...
    """
    try:
        yield
    except KeyboardInterrupt:
        if json_output:
            _output_error("Cancelled by user", "CANCELLED", True, 130)
        else:
            click.echo("\nCancelled.", err=True)
            raise SystemExit(130) from None
    except RateLimitError as e:
        retry_msg = f" Retry after {e.retry_after}s." if e.retry_after else ""
        extra_data: dict[str, Any] = {}
        if e.retry_after:
            extra_data["retry_after"] = e.retry_after
        if verbose and e.method_id:
            extra_data["method_id"] = e.method_id
        _output_error(
            f"Error: Rate limited.{retry_msg}",
            "RATE_LIMITED",
            json_output,
            1,
            extra=extra_data,
        )
    except AuthError as e:
        _output_error(
            f"Authentication error: {e}",
            "AUTH_ERROR",
            json_output,
            1,
            hint="Run 'notebooklm login' to re-authenticate.",
        )
    except ValidationError as e:
        _output_error(f"Validation error: {e}", "VALIDATION_ERROR", json_output, 1)
    except ConfigurationError as e:
        _output_error(f"Configuration error: {e}", "CONFIG_ERROR", json_output, 1)
    except NetworkError as e:
        _output_error(
            f"Network error: {e}",
            "NETWORK_ERROR",
            json_output,
            1,
            hint="Check your internet connection and try again.",
        )
    except NotebookLMError as e:
        extra_info: dict[str, Any] | None = None
        if verbose and isinstance(e, RPCError) and e.method_id:
            extra_info = {"method_id": e.method_id}
        _output_error(f"Error: {e}", "NOTEBOOKLM_ERROR", json_output, 1, extra=extra_info)
    except click.ClickException:
        # Let Click handle its own exceptions (--help, bad args, etc.)
        raise
    except Exception as e:
        _output_error(
            f"Unexpected error: {e}",
            "UNEXPECTED_ERROR",
            json_output,
            2,
            hint="This may be a bug. Please report at https://github.com/teng-lin/notebooklm-py/issues",
        )
