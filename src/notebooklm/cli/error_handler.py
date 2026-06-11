"""Centralized CLI error handling.

This module provides a context manager for consistent error handling
across all CLI commands.
"""

import json
import logging
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any, NoReturn

import click

from ..exceptions import (
    ArtifactTimeoutError,
    AuthError,
    ConfigurationError,
    NetworkError,
    NotebookLimitError,
    NotebookLMError,
    NotFoundError,
    RateLimitError,
    RPCError,
    ValidationError,
)
from ._encoding import safe_echo

logger = logging.getLogger(__name__)

# NOTE: ``click.ClickException`` / raw ``raise SystemExit`` sites outside this
# module are governed by inline marker comments
# (``# cli-input-validation: <reason>`` / ``# cli-raw-exit: <reason>``), checked
# by ``tests/_guardrails/test_error_handler_allowlist.py``. The previous
# ``ALLOWED_*_SITES`` line-number allowlists were removed in issue #1298 because
# any edit above a site shifted its line and failed CI with no behavior change.


def current_json_output(default: bool = False) -> bool:
    """Infer the active Click command's JSON-output flag, if any."""
    ctx = click.get_current_context(silent=True)
    if ctx is None:
        return default
    try:
        current: click.Context | None = ctx
        while current is not None:
            for key in ("json_output", "json"):
                value = current.params.get(key)
                if isinstance(value, bool):
                    return value
            current = current.parent
    except (AttributeError, RuntimeError):
        return default
    return default


def exit_with_code(exit_code: int = 1) -> NoReturn:
    """Canonical raw exit path for callers that already emitted their payload."""
    raise SystemExit(exit_code)


# Per-``*NotFoundError`` resource-id attribute names, in MRO order. Each
# concrete subclass stores its missing-id under exactly one of these (e.g.
# ``SourceNotFoundError.source_id``, ``NotebookNotFoundError.notebook_id``),
# so the central handler can surface the id in the ``NOT_FOUND`` JSON envelope
# the same way the per-command sites do — without importing every subclass.
_NOT_FOUND_ID_ATTRS = (
    "notebook_id",
    "source_id",
    "artifact_id",
    "note_id",
    "mind_map_id",
    "label_id",
)


def _not_found_extra(error: NotFoundError) -> dict[str, Any]:
    """Build the JSON ``extra`` block for a ``*NotFoundError`` envelope.

    Surfaces whichever resource-id attribute the concrete subclass carries
    (``source_id`` / ``artifact_id`` / ``note_id`` / ``mind_map_id`` /
    ``label_id`` / ``notebook_id``) under both its native key and a generic
    ``id`` key, so
    automation can read the id without knowing the exact not-found subtype —
    mirroring the per-command ``source``/``artifact``/``note get`` payloads.
    Returns an empty dict when no known id attribute is present (e.g. a future
    ``*NotFoundError`` subclass); the caller drops an empty ``extra``.
    """
    extra: dict[str, Any] = {}
    for attr in _NOT_FOUND_ID_ATTRS:
        value = getattr(error, attr, None)
        if value is not None:
            extra[attr] = value
            extra["id"] = value
            break
    return extra


def _generation_status_extra(status: Any) -> dict[str, Any]:
    """Serialize a GenerationStatus-like object for JSON error payloads."""
    return {
        "task_id": getattr(status, "task_id", None),
        "status": getattr(status, "status", None),
        "url": getattr(status, "url", None),
        "error": getattr(status, "error", None),
        "error_code": getattr(status, "error_code", None),
        "metadata": getattr(status, "metadata", None),
    }


def _output_error(
    message: str,
    code: str,
    json_output: bool,
    exit_code: int,
    extra: dict[str, Any] | None = None,
    hint: str | None = None,
) -> NoReturn:
    """Output error message in text or JSON format and exit.

    Args:
        message: Human-readable error message
        code: Error code for JSON output (e.g., "RATE_LIMITED", "AUTH_ERROR")
        json_output: If True, output as JSON; otherwise as text
        exit_code: Exit code to use
        extra: Additional fields to include in JSON output
        hint: Additional hint to show in text mode

    Note:
        Also exported as the public alias :func:`output_error`. The leading
        underscore name pre-dates the public-CLI-boundary contract enforced by
        ``tests/_guardrails/test_cli_boundary.py``; sibling ``cli/*`` modules may
        import the private name directly (intra-package, level-1 relative
        import), but ``cli/services/*`` and any other layer that crosses up
        through ``..error_handler`` must use the public alias to stay on the
        public side of that contract.
    """
    if json_output:
        response: dict = {"error": True, "code": code, "message": message}
        if extra:
            response.update(extra)
        click.echo(json.dumps(response, indent=2, default=str, ensure_ascii=False))
    else:
        safe_echo(message, err=True)
        if hint:
            safe_echo(hint, err=True)
    raise SystemExit(exit_code)


#: Public alias for :func:`_output_error` — see the function docstring for the
#: rationale. ``cli/services/*`` and other layers that must cross the CLI
#: package boundary import this name to stay on the public side of the
#: boundary contract enforced by ``tests/_guardrails/test_cli_boundary.py``.
output_error = _output_error


def emit_cancelled_and_exit(
    resume_hint: str | None = None,
    *,
    json_output: bool = False,
    extra: dict[str, Any] | None = None,
) -> NoReturn:
    """Emit a Ctrl-C cancellation message with an optional resume hint and exit 130.

    Used by the long-running ``--wait`` paths so SIGINT during a poll
    surfaces a friendly resume hint instead of a Python traceback. The hint
    follows the canonical phrasing from the audit:

        Cancelled. Resume with: notebooklm artifact poll <task_id>

    For ``source wait`` the parallel hint is ``notebooklm source wait <id>``
    (no separate poll command exists for sources).

    Args:
        resume_hint: Free-form resume command string. When ``None`` the helper
            emits a plain ``Cancelled.`` line, matching the generic
            KeyboardInterrupt branch in ``handle_errors``.
        json_output: When True, emit a structured envelope on stdout
            (``{"error": true, "code": "CANCELLED", ...}``) so automation can
            still parse the cancellation. When False, write to stderr.
        extra: Optional dict merged into the JSON envelope (e.g. ``{"task_id":
            "abc"}``). Ignored in text mode — the resume hint already names
            the resource.

    Always raises ``SystemExit(130)`` (128 + signal 2 / SIGINT).
    """
    if json_output:
        response: dict[str, Any] = {
            "error": True,
            "code": "CANCELLED",
            "message": "Cancelled by user",
        }
        if resume_hint:
            response["resume_hint"] = resume_hint
        if extra:
            response.update(extra)
        click.echo(json.dumps(response, indent=2, default=str, ensure_ascii=False))
    else:
        if resume_hint:
            safe_echo(f"\nCancelled. Resume with: {resume_hint}", err=True)
        else:
            safe_echo("\nCancelled.", err=True)
    raise SystemExit(130)


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
            safe_echo("\nCancelled.", err=True)
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
    except NotebookLimitError as e:
        _output_error(
            str(e),
            "NOTEBOOK_LIMIT",
            json_output,
            1,
            extra=e.to_error_response_extra(),
        )
    except ArtifactTimeoutError as e:
        extra_data = {
            "notebook_id": e.notebook_id,
            "task_id": e.task_id,
            "timeout_seconds": e.timeout_seconds,
            "last_status": e.last_status,
            "status_history": list(e.status_history),
            "status_transitions": [
                _generation_status_extra(status) for status in e.status_transitions
            ],
            "stalled_phase": e.stalled_phase,
        }
        _output_error(
            f"Artifact timeout: {e}",
            "ARTIFACT_TIMEOUT",
            json_output,
            1,
            extra=extra_data,
        )
    except NotFoundError as e:
        # The ``NotFoundError`` umbrella catches every concrete
        # ``*NotFoundError`` (notebook / source / artifact / note / mind map),
        # which all derive ``(NotFoundError, RPCError, <Domain>Error)``. Placed
        # AFTER the more-specific branches (none of which are ancestors of a
        # ``*NotFoundError`` — verified by the handler's exception MRO) and
        # BEFORE the generic ``NotebookLMError`` catch-all so a missing resource
        # emits the typed ``NOT_FOUND`` envelope (matching the per-command
        # ``source``/``artifact``/``note get`` convention) instead of the
        # generic ``NOTEBOOKLM_ERROR``. This makes the central handler faithful
        # to the v0.8.0 raise-sites (e.g. ``get()`` -> raise,
        # ``rename``/``update`` on a missing target).
        nf_extra = _not_found_extra(e)
        if verbose and isinstance(e, RPCError) and e.method_id:
            nf_extra["method_id"] = e.method_id
        _output_error(
            f"Error: {e}",
            "NOT_FOUND",
            json_output,
            1,
            extra=nf_extra or None,
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
        # Emit only the exception's primary message (``args[0]``) to
        # the user. ``str(e)`` would walk Python's default representation,
        # which for some third-party exceptions includes repr of every arg
        # — surfacing whatever the raise site put in (potentially full
        # subprocess output, response bodies, etc.). Pinning to ``args[0]``
        # keeps the contract: raise sites are responsible for producing a
        # safe message; the handler does not re-render.
        # Third-party exceptions can put non-string
        # objects in ``args[0]`` (e.g. ``ValueError(42)``, ``SomeErr({"code":
        # 404})``). The f-string below would call ``str()`` implicitly anyway,
        # but the explicit cast makes the contract obvious and avoids surprises
        # if the f-string is ever replaced with a different formatter.
        primary = str(e.args[0]) if e.args else type(e).__name__
        # Route the full exception (with cause chain + traceback) to the
        # redacting DEBUG logger so ``-vv`` users can still diagnose.
        logger.debug("Unexpected CLI exception", exc_info=True)
        _output_error(
            f"Unexpected error: {primary}",
            "UNEXPECTED_ERROR",
            json_output,
            2,
            hint="This may be a bug. Please report at https://github.com/teng-lin/notebooklm-py/issues",
        )
