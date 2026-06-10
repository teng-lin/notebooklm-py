"""Project the notebooklm exception hierarchy onto HTTP status + JSON envelope.

The REST server surfaces every failure as an HTTP status plus a typed body::

    {"error": {"category": "<category>", "message": "<scrubbed>"}}

The **category** decision is delegated to
:func:`notebooklm._app.errors.classify` (the single neutral source of truth
shared with the CLI ``error_handler`` and the MCP server); this module only
*projects* that category onto an HTTP status via :data:`CATEGORY_STATUS`. The
classification runs exactly once per request — the handler never re-derives the
category.

The ``message`` is passed through :func:`_redact` (whitespace-collapsed and
length-capped) so a multi-kilobyte schema-drift ``str(exc)`` (which can expose
RPC ``method_id`` / ``path`` / ``found_ids``) cannot bloat or over-disclose the
envelope; it stays the already-scrubbed SDK string (no raw payloads, no
credentials). The status-5 ``ClientError`` account-routing hint is preserved
verbatim in the 404 body.

This module imports NO ``click`` / ``rich`` / ``cli`` — only ``fastapi`` and the
``_app`` classification core.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .._app.errors import ErrorCategory, classify
from ..exceptions import NotebookLMError

__all__ = ["CATEGORY_STATUS", "error_response", "install_exception_handlers"]

#: Maximum wire length for an error message before it is truncated.
_MAX_MESSAGE = 300

#: The HTTP status each neutral :class:`ErrorCategory` projects onto. Covers
#: EVERY ``ErrorCategory`` value (pinned by
#: ``tests/_guardrails/test_server_classify_consistency.py``).
CATEGORY_STATUS: dict[ErrorCategory, int] = {
    ErrorCategory.NOT_FOUND: 404,
    ErrorCategory.AUTH: 401,
    ErrorCategory.RATE_LIMITED: 429,
    ErrorCategory.VALIDATION: 400,
    ErrorCategory.CONFIG: 500,
    ErrorCategory.NETWORK: 502,
    ErrorCategory.NOTEBOOK_LIMIT: 409,
    ErrorCategory.ARTIFACT_TIMEOUT: 504,
    ErrorCategory.TIMEOUT: 504,
    ErrorCategory.SERVER: 502,
    ErrorCategory.RPC: 502,
    ErrorCategory.SOURCE_MUTATION: 422,
    ErrorCategory.LIBRARY: 500,
    ErrorCategory.UNEXPECTED: 500,
}


def _redact(message: str) -> str:
    """Collapse whitespace and length-cap a message for the wire.

    SDK exception messages are already designed to be secret-free (raw responses
    are truncated at construction, per ADR-0019); we additionally collapse
    whitespace and cap the length so an unexpectedly long body cannot bloat the
    error envelope or over-disclose a schema-drift dump.
    """
    message = " ".join(message.split())
    if len(message) > _MAX_MESSAGE:
        message = message[:_MAX_MESSAGE] + "…"
    return message


def error_response(exc: BaseException) -> JSONResponse:
    """Build the typed JSON error response for ``exc``.

    Calls :func:`classify` exactly once and looks up the status from
    :data:`CATEGORY_STATUS`; the category is never re-derived. The message is the
    scrubbed ``str(exc)``.
    """
    category = classify(exc).category
    status = CATEGORY_STATUS[category]
    return JSONResponse(
        status_code=status,
        content={"error": {"category": category.value, "message": _redact(str(exc))}},
    )


def install_exception_handlers(app: FastAPI) -> None:
    """Install the exception handlers that project failures via :func:`classify`.

    A :class:`~notebooklm.exceptions.NotebookLMError` escaping a route handler is
    translated into the typed ``{"error": {...}}`` envelope with the
    classified status. A non-library exception (a bug) is also caught and
    projected as ``UNEXPECTED`` -> 500, so a handler crash never leaks a raw
    stack trace to the client.

    The ``NotebookLMError`` handler is registered on the library base class (not
    the broad ``Exception``) so Starlette's ``ExceptionMiddleware`` handles it
    without re-raising; the broad ``Exception`` handler is the last-resort net
    for genuine bugs. ``HTTPException`` raised explicitly by a handler (e.g. the
    auth dependency's 401/403) is left to FastAPI's default handler so its
    status/detail are preserved.
    """

    @app.exception_handler(NotebookLMError)
    async def _handle_library(_request: Request, exc: NotebookLMError) -> JSONResponse:
        return error_response(exc)

    @app.exception_handler(Exception)
    async def _handle_unexpected(_request: Request, exc: Exception) -> JSONResponse:
        return error_response(exc)
