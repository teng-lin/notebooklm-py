"""CLI authentication and command runtime helpers.

``notebooklm.cli.helpers`` remains a compatibility facade for historical
imports and tests. The functions here intentionally look up selected
collaborators through that facade at call time so established patch seams such
as ``notebooklm.cli.helpers.get_auth_tokens`` and
``notebooklm.cli.helpers.run_async`` continue to affect runtime behavior.
"""

import logging
import os
import time
from collections.abc import Awaitable, Callable
from functools import wraps
from pathlib import Path
from typing import Any, NoReturn, TypeVar, cast

import click

from ..auth import AuthTokens

logger = logging.getLogger(__name__)
T = TypeVar("T")
_RESOLVED_AUTH_STORAGE_PATH_CTX_KEY = "_notebooklm_resolved_auth_storage_path"


def _helpers_facade():
    """Return the backward-compatible helpers facade without a top-level cycle."""
    from . import helpers

    return helpers


def _auth_context(ctx) -> tuple[Path | str | None, str | None]:
    """Return explicit storage and profile values from a Click context."""
    storage_path = ctx.obj.get("storage_path") if ctx.obj else None
    profile = ctx.obj.get("profile") if ctx.obj else None
    return storage_path, profile


def _resolve_auth_storage_path(
    storage_path: Path | str | None, profile: str | None
) -> Path | str | None:
    """Resolve storage unless auth is supplied directly by environment."""
    if storage_path is not None:
        return storage_path
    if os.environ.get("NOTEBOOKLM_AUTH_JSON"):
        return None

    from ..paths import get_storage_path

    return get_storage_path(profile=profile)


def _resolved_auth_storage_path(ctx) -> Path | str | None:
    """Return the per-command resolved auth storage path."""
    if ctx.obj is not None and _RESOLVED_AUTH_STORAGE_PATH_CTX_KEY in ctx.obj:
        return cast(Path | str | None, ctx.obj[_RESOLVED_AUTH_STORAGE_PATH_CTX_KEY])

    storage_path, profile = _auth_context(ctx)
    resolved_storage_path = _resolve_auth_storage_path(storage_path, profile)
    if ctx.obj is not None:
        ctx.obj[_RESOLVED_AUTH_STORAGE_PATH_CTX_KEY] = resolved_storage_path
    return resolved_storage_path


def get_client(ctx) -> tuple[dict, str, str]:
    """Get auth components from context.

    Args:
        ctx: Click context with optional storage_path in obj

    Returns:
        Tuple of (cookies, csrf_token, session_id)

    Raises:
        FileNotFoundError: If auth storage not found
    """
    helpers = _helpers_facade()
    _, profile = _auth_context(ctx)
    resolved_storage_path = _resolved_auth_storage_path(ctx)
    typed_storage_path = cast(Path | None, resolved_storage_path)

    # Load from storage (which respects NOTEBOOKLM_AUTH_JSON if resolved path is None).
    cookies = helpers.load_auth_from_storage(resolved_storage_path)

    from ..auth import fetch_tokens_with_domains

    csrf, session_id = helpers.run_async(fetch_tokens_with_domains(typed_storage_path, profile))
    return cookies, csrf, session_id


def get_auth_tokens(ctx) -> AuthTokens:
    """Get AuthTokens object from context.

    Args:
        ctx: Click context

    Returns:
        AuthTokens ready for client construction
    """
    helpers = _helpers_facade()
    cookies, csrf, session_id = helpers.get_client(ctx)
    storage_path, _ = _auth_context(ctx)
    resolved_storage_path = _resolved_auth_storage_path(ctx)
    typed_storage_path = cast(Path | None, resolved_storage_path)

    if os.environ.get("NOTEBOOKLM_AUTH_JSON") and storage_path is None:
        from ..auth import build_httpx_cookies_from_storage

        jar = build_httpx_cookies_from_storage(None)
    else:
        jar = helpers.build_cookie_jar(cookies=cookies, storage_path=resolved_storage_path)

    # Read persisted account routing so RPC URLs target the same Google
    # account the tokens were minted for.
    from ..auth import get_account_email_for_storage, get_authuser_for_storage

    return AuthTokens(
        cookies=cookies,
        csrf_token=csrf,
        session_id=session_id,
        storage_path=typed_storage_path,
        cookie_jar=jar,
        authuser=get_authuser_for_storage(typed_storage_path),
        account_email=get_account_email_for_storage(typed_storage_path),
    )


def handle_auth_error(json_output: bool = False) -> NoReturn:
    """Handle authentication errors with helpful context."""
    from ..paths import get_path_info, get_storage_path

    helpers = _helpers_facade()
    ctx = click.get_current_context(silent=True)
    profile = ctx.obj.get("profile") if ctx and ctx.obj else None
    storage_override = helpers._current_storage_override()
    path_info = get_path_info(profile=profile, storage_path=storage_override)
    storage_path = (
        storage_override if storage_override is not None else get_storage_path(profile=profile)
    )
    storage_path = Path(storage_path).expanduser().resolve()
    has_env_var = bool(os.environ.get("NOTEBOOKLM_AUTH_JSON"))
    has_home_env = bool(os.environ.get("NOTEBOOKLM_HOME"))
    storage_source = path_info["home_source"]

    if json_output:
        helpers.json_error_response(
            "AUTH_REQUIRED",
            "Auth not found. Run 'notebooklm login' first.",
            extra={
                "checked_paths": {
                    "storage_file": str(storage_path),
                    "storage_source": storage_source,
                    "env_var": "NOTEBOOKLM_AUTH_JSON" if has_env_var else None,
                },
                "help": "Run 'notebooklm login' or set NOTEBOOKLM_AUTH_JSON",
            },
        )
        raise SystemExit(1)
    else:
        helpers.console.print("[red]Not logged in.[/red]\n")
        helpers.console.print("[dim]Checked locations:[/dim]")
        helpers.console.print(f"  • Storage file: [cyan]{storage_path}[/cyan]")
        if has_home_env:
            helpers.console.print("    [dim](via $NOTEBOOKLM_HOME)[/dim]")
        env_status = "[yellow]set but invalid[/yellow]" if has_env_var else "[dim]not set[/dim]"
        helpers.console.print(f"  • NOTEBOOKLM_AUTH_JSON: {env_status}")
        helpers.console.print("\n[bold]Options to authenticate:[/bold]")
        helpers.console.print("  1. Run: [green]notebooklm login[/green]")
        helpers.console.print("  2. Set [cyan]NOTEBOOKLM_AUTH_JSON[/cyan] env var (for CI/CD)")
        helpers.console.print("  3. Use [cyan]--storage /path/to/file.json[/cyan] flag")
        raise SystemExit(1)


def with_auth_and_errors(
    ctx: click.Context,
    *,
    command_name: str,
    json_output: bool,
    body: Callable[[AuthTokens], Awaitable[T]],
    auth_loader: Callable[[click.Context], AuthTokens] | None = None,
) -> T:
    """Run a CLI command body with shared auth bootstrap and error handling."""
    from .error_handler import handle_errors

    helpers = _helpers_facade()
    start = time.monotonic()
    logger.debug("CLI command starting: %s", command_name)

    # Verbose is captured on the root group via Click ``--verbose`` count.
    # Use ``find_root`` so nested subcommand contexts still see it.
    try:
        verbose_count = int(ctx.find_root().params.get("verbose", 0) or 0)
    except (AttributeError, TypeError, ValueError):
        verbose_count = 0
    verbose = verbose_count >= 1

    def log_result(status: str, detail: str = "") -> None:
        elapsed = time.monotonic() - start
        if detail:
            logger.debug(
                "CLI command %s: %s (%.3fs) - %s",
                status,
                command_name,
                elapsed,
                detail,
            )
        else:
            logger.debug("CLI command %s: %s (%.3fs)", status, command_name, elapsed)

    with handle_errors(verbose=verbose, json_output=json_output):
        # Auth bootstrap: FileNotFoundError here means the storage file is
        # missing -- it has a dedicated rich UX via ``handle_auth_error``.
        # The narrow ``except FileNotFoundError`` ensures a FileNotFoundError
        # raised *inside* the command body (e.g., a missing ``--source-file``
        # argument; see issue #153) is NOT misclassified as an auth error --
        # it propagates to ``handle_errors``' UNEXPECTED_ERROR branch instead.
        # Any OTHER exception from the auth bootstrap (malformed storage JSON,
        # AuthError during token extraction, etc.) also reaches ``handle_errors``
        # so users get typed hints rather than a raw traceback.
        try:
            loader = auth_loader or helpers.get_auth_tokens
            auth = loader(ctx)
        except FileNotFoundError:
            log_result("failed", "not authenticated")
            return helpers.handle_auth_error(json_output)
        except Exception as e:
            # Non-FileNotFoundError bootstrap failures (AuthError, malformed
            # storage JSON, etc.) still need the structured debug-log entry;
            # ``handle_errors`` will translate the exception to a typed hint.
            log_result("failed", str(e))
            raise

        try:
            result = helpers.run_async(body(auth))
        except Exception as e:
            log_result("failed", str(e))
            raise
        log_result("completed")
        return result


def with_client(f):
    """Decorator that handles auth, async execution, and errors for CLI commands.

    This decorator eliminates boilerplate from commands that need:
    - Authentication (get AuthTokens from context)
    - Async execution (run coroutine with asyncio.run)
    - Error handling (auth errors, general exceptions)

    The decorated function stays SYNC (Click doesn't support async) but returns
    a coroutine. The decorator runs the coroutine and handles errors.

    Usage:
        @cli.command("list")
        @click.option("--json", "json_output", is_flag=True)
        @with_client
        def list_notebooks(ctx, json_output, client_auth):
            async def _run():
                async with NotebookLMClient(client_auth) as client:
                    notebooks = await client.notebooks.list()
                    output_notebooks(notebooks, json_output)

            return _run()

    Args:
        f: Function that accepts client_auth (AuthTokens) and returns a coroutine

    Returns:
        Decorated function with Click pass_context
    """

    @wraps(f)
    @click.pass_context
    def wrapper(ctx, *args, **kwargs):
        cmd_name = f.__name__
        json_output = kwargs.get("json_output", False)

        def body(auth: AuthTokens) -> Awaitable[Any]:
            return f(ctx, *args, client_auth=auth, **kwargs)

        return with_auth_and_errors(
            ctx,
            command_name=cmd_name,
            json_output=json_output,
            body=body,
        )

    return wrapper
