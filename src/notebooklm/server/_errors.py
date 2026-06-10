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
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .._app.errors import ErrorCategory, classify
from .._logging import scrub_secrets
from ..exceptions import NotebookLMError

__all__ = [
    "CATEGORY_STATUS",
    "error_response",
    "http_error_response",
    "install_exception_handlers",
    "safe_detail",
]

#: Maximum wire length for an error message before it is truncated.
_MAX_MESSAGE = 300

#: Category label for an ``HTTPException`` raised explicitly by a route or the
#: auth dependency, keyed by HTTP status. Keeps the ``{"error": {...}}`` envelope
#: uniform across *both* classified library errors and hand-raised
#: ``HTTPException``s (the R9 single-shape contract), instead of letting FastAPI
#: emit its default ``{"detail": ...}`` for the latter. Statuses not listed fall
#: back to a coarse class label (see :func:`_http_category`).
_STATUS_CATEGORY: dict[int, str] = {
    400: ErrorCategory.VALIDATION.value,
    401: ErrorCategory.AUTH.value,
    403: ErrorCategory.AUTH.value,
    404: ErrorCategory.NOT_FOUND.value,
    409: "conflict",
    410: "gone",
    413: ErrorCategory.VALIDATION.value,
    422: ErrorCategory.VALIDATION.value,
    429: ErrorCategory.RATE_LIMITED.value,
    500: ErrorCategory.UNEXPECTED.value,
    502: ErrorCategory.SERVER.value,
    503: ErrorCategory.SERVER.value,
    504: ErrorCategory.TIMEOUT.value,
}

#: Generic message returned for an unexpected (non-library) exception — a bug's
#: ``str(exc)`` could carry anything, so it is never echoed to the client.
_UNEXPECTED_MESSAGE = "Internal server error"

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


def _redact(message: object) -> str:
    """Scrub secrets, collapse whitespace, and length-cap a message for the wire.

    SDK exception messages are already designed to be secret-free (raw responses
    are truncated at construction, per ADR-0019), but the server runs every
    wire-bound message through :func:`notebooklm._logging.scrub_secrets` as
    defense-in-depth (so a stray ``Authorization``/``Cookie`` fragment in any
    exception or upstream error string is masked), then collapses whitespace and
    caps the length so a schema-drift dump cannot bloat or over-disclose the body.
    """
    scrubbed = " ".join(scrub_secrets(message).split())
    if len(scrubbed) > _MAX_MESSAGE:
        scrubbed = scrubbed[:_MAX_MESSAGE] + "…"
    return scrubbed


def safe_detail(message: object) -> str:
    """Scrub + cap an upstream message for use as an ``HTTPException`` detail.

    Route handlers that raise ``HTTPException`` with upstream-derived text
    (e.g. an artifact ``view.error``) must run it through this so the detail
    cannot leak a credential or a multi-kilobyte dump.
    """
    return _redact(message)


def _http_category(status: int) -> str:
    """Map an HTTP status to its envelope ``category`` label.

    Uses the explicit :data:`_STATUS_CATEGORY` table, falling back to a coarse
    class label so an unanticipated status still yields a non-empty category.
    """
    label = _STATUS_CATEGORY.get(status)
    if label is not None:
        return label
    if 400 <= status < 500:
        return ErrorCategory.VALIDATION.value
    return ErrorCategory.SERVER.value


def _validation_summary(exc: RequestValidationError) -> str:
    """Render a request-validation error as a compact ``field: message`` summary.

    Uses the structured :meth:`RequestValidationError.errors` (never ``str(exc)``,
    which embeds server file paths under pydantic v2). The leading ``body`` /
    ``query`` location segment is dropped for readability, and the per-error
    ``input`` value is intentionally omitted so client data is not echoed back.
    """
    parts: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ()) if p not in ("body", "query"))
        msg = str(err.get("msg", "invalid"))
        parts.append(f"{loc}: {msg}" if loc else msg)
    return "; ".join(parts) or "invalid request body"


def http_error_response(status: int, detail: object) -> JSONResponse:
    """Build the typed envelope for a hand-raised ``HTTPException``.

    Renders ``HTTPException``s (the auth dependency's 401/403, an artifact poll's
    404/409/410, an oversized-upload 413) through the same
    ``{"error": {"category": ..., "message": ...}}`` shape as classified library
    errors, so the wire contract is uniform. The ``detail`` is scrubbed +
    length-capped via :func:`_redact`.
    """
    return JSONResponse(
        status_code=status,
        content={"error": {"category": _http_category(status), "message": _redact(detail)}},
    )


def error_response(exc: BaseException) -> JSONResponse:
    """Build the typed JSON error response for ``exc``.

    Calls :func:`classify` exactly once and looks up the status from
    :data:`CATEGORY_STATUS`; the category is never re-derived. The message is the
    scrubbed ``str(exc)`` for library errors, and a fixed generic string for an
    unexpected (non-library) bug — whose ``str(exc)`` is never echoed.
    """
    category = classify(exc).category
    status = CATEGORY_STATUS[category]
    message = _UNEXPECTED_MESSAGE if category is ErrorCategory.UNEXPECTED else _redact(str(exc))
    return JSONResponse(
        status_code=status,
        content={"error": {"category": category.value, "message": message}},
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
    for genuine bugs. An ``HTTPException`` raised explicitly by a handler (the
    auth dependency's 401/403, an artifact poll's 404/409/410) and a
    request-body ``RequestValidationError`` (422) are re-projected onto the same
    ``{"error": {...}}`` envelope (R9 single-shape contract) instead of FastAPI's
    default ``{"detail": ...}``.
    """

    @app.exception_handler(NotebookLMError)
    async def _handle_library(_request: Request, exc: NotebookLMError) -> JSONResponse:
        return error_response(exc)

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return http_error_response(exc.status_code, exc.detail)

    @app.exception_handler(RequestValidationError)
    async def _handle_validation(_request: Request, exc: RequestValidationError) -> JSONResponse:
        # Build a compact field-level summary from the STRUCTURED errors — never
        # ``str(exc)``, which under pydantic v2 embeds server source-file paths
        # and frame info (information disclosure). ``input`` is omitted so we
        # don't echo arbitrary request data back.
        return http_error_response(422, f"Request validation failed: {_validation_summary(exc)}")

    @app.exception_handler(Exception)
    async def _handle_unexpected(_request: Request, exc: Exception) -> JSONResponse:
        return error_response(exc)
