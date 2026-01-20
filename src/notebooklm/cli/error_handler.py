"""Centralized CLI error handling.

This module provides a context manager for consistent error handling
across all CLI commands.
"""

from collections.abc import Generator
from contextlib import contextmanager

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


@contextmanager
def handle_errors(verbose: bool = False) -> Generator[None, None, None]:
    """Context manager for consistent CLI error handling.

    Catches library exceptions and converts them to user-friendly
    error messages with appropriate exit codes.

    Exit codes:
        1: User/application error (validation, auth, rate limit, etc.)
        2: System/unexpected error (bugs, unhandled exceptions)
        130: Keyboard interrupt (128 + signal 2)

    Args:
        verbose: If True, show additional debug info (method_id, etc.)

    Example:
        @click.command()
        def my_command():
            with handle_errors():
                # ... command logic ...
    """
    try:
        yield
    except KeyboardInterrupt:
        click.echo("\nCancelled.", err=True)
        raise SystemExit(130) from None
    except RateLimitError as e:
        retry_msg = f" Retry after {e.retry_after}s." if e.retry_after else ""
        click.echo(f"Error: Rate limited.{retry_msg}", err=True)
        if verbose and e.method_id:
            click.echo(f"  RPC Method: {e.method_id}", err=True)
        raise SystemExit(1) from None
    except AuthError as e:
        click.echo(f"Authentication error: {e}", err=True)
        click.echo("Run 'notebooklm login' to re-authenticate.", err=True)
        raise SystemExit(1) from None
    except ValidationError as e:
        click.echo(f"Validation error: {e}", err=True)
        raise SystemExit(1) from None
    except ConfigurationError as e:
        click.echo(f"Configuration error: {e}", err=True)
        raise SystemExit(1) from None
    except NetworkError as e:
        click.echo(f"Network error: {e}", err=True)
        click.echo("Check your internet connection and try again.", err=True)
        raise SystemExit(1) from None
    except NotebookLMError as e:
        click.echo(f"Error: {e}", err=True)
        if verbose and isinstance(e, RPCError) and e.method_id:
            click.echo(f"  RPC Method: {e.method_id}", err=True)
        raise SystemExit(1) from None
    except click.ClickException:
        # Let Click handle its own exceptions (--help, bad args, etc.)
        raise
    except Exception as e:
        click.echo(f"Unexpected error: {e}", err=True)
        click.echo(
            "This may be a bug. Please report at https://github.com/teng-lin/notebooklm-py/issues",
            err=True,
        )
        raise SystemExit(2) from None
