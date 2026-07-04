"""Project the notebooklm exception hierarchy onto HTTP status + JSON envelope.

The REST server surfaces every failure as an HTTP status plus a typed body::

    {"error": {"category": "<category>", "message": "<scrubbed>",
               "retriable": <bool>, "hint"?: "<remediation>"}}

The ``retriable`` flag and optional ``hint`` are present on EVERY response whose
status maps to a neutral :class:`~notebooklm._app.errors.ErrorCategory` — both
classified library errors and hand-raised ``HTTPException``s (401/404/411/413/
422/…) — drawn from the shared ``_app.errors`` tables so the two paths cannot
drift. HTTP-protocol-only statuses (409/410) carry just ``category`` + ``message``.

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

from .._app.errors import (
    CATEGORY_HINTS,
    ClassifiedError,
    ErrorCategory,
    classify,
    did_you_mean_hint,
    is_retriable,
)
from .._redact import redact as _shared_redact
from ..exceptions import NotebookLMError

__all__ = [
    "CATEGORY_STATUS",
    "error_item",
    "error_response",
    "http_error_response",
    "install_exception_handlers",
    "safe_detail",
]

#: Maximum wire length for an error message before it is truncated.
_MAX_MESSAGE = 300

#: The neutral :class:`ErrorCategory` an ``HTTPException`` raised explicitly by a
#: route or the auth dependency (keyed by HTTP status) projects onto. Keeps the
#: ``{"error": {...}}`` envelope uniform across *both* classified library errors
#: and hand-raised ``HTTPException``s (the R9 single-shape contract), instead of
#: letting FastAPI emit its default ``{"detail": ...}`` for the latter. Because
#: the status resolves to a neutral category, the hand-raised body ALSO carries
#: the SAME ``retriable`` + ``hint`` enrichment as a classified library error
#: (drawn from the shared ``_app.errors`` tables — never re-derived here), so the
#: wire contract is uniform. Statuses not listed here get a coarse label and no
#: enrichment (see :data:`_STATUS_LABEL` / :func:`_http_category_label`).
_STATUS_CATEGORY: dict[int, ErrorCategory] = {
    400: ErrorCategory.VALIDATION,
    401: ErrorCategory.AUTH,
    403: ErrorCategory.AUTH,
    404: ErrorCategory.NOT_FOUND,
    411: ErrorCategory.VALIDATION,
    413: ErrorCategory.VALIDATION,
    422: ErrorCategory.VALIDATION,
    429: ErrorCategory.RATE_LIMITED,
    500: ErrorCategory.UNEXPECTED,
    502: ErrorCategory.SERVER,
    503: ErrorCategory.SERVER,
    504: ErrorCategory.TIMEOUT,
}

#: HTTP-protocol-only statuses with no neutral :class:`ErrorCategory`. They get a
#: coarse label for the envelope ``category`` and NO ``retriable`` / ``hint``
#: enrichment (there is no category to draw those from).
_STATUS_LABEL: dict[int, str] = {
    409: "conflict",
    410: "gone",
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
    """Scrub secrets + local paths, collapse whitespace, and length-cap for the wire.

    Delegates to the shared, transport-neutral
    :func:`notebooklm._redact.redact` — the SAME chokepoint the MCP projector
    uses — so a home-directory path (``/home/<user>/…``) or a signed ``/files/*``
    URL token in an exception string is masked on BOTH surfaces, not just MCP.
    SDK exception messages are already designed to be secret-free (raw responses
    are truncated at construction, per ADR-0019); this is defense-in-depth plus
    the length cap so a schema-drift dump cannot bloat or over-disclose the body.
    """
    return _shared_redact(message, max_length=_MAX_MESSAGE)


def safe_detail(message: object) -> str:
    """Scrub + cap an upstream message for use as an ``HTTPException`` detail.

    Route handlers that raise ``HTTPException`` with upstream-derived text
    (e.g. an artifact ``view.error``) must run it through this so the detail
    cannot leak a credential or a multi-kilobyte dump.
    """
    return _redact(message)


def _http_category_label(status: int) -> str:
    """Map an HTTP status to its envelope ``category`` label.

    Prefers the neutral :data:`_STATUS_CATEGORY` category (its ``.value``), then
    the coarse :data:`_STATUS_LABEL`, falling back to a class label so an
    unanticipated status still yields a non-empty category.
    """
    category = _STATUS_CATEGORY.get(status)
    if category is not None:
        return category.value
    label = _STATUS_LABEL.get(status)
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
    404/409/410, an oversized-upload 413, a chunked-multipart 411, a
    request-body 422, a content-route 404) through the same
    ``{"error": {"category": ..., "message": ..., "retriable": ..., "hint"?: ...}}``
    shape as classified library errors, so the wire contract is uniform across
    EVERY REST error response. When the status maps to a neutral
    :class:`ErrorCategory` (:data:`_STATUS_CATEGORY`), the body carries the SAME
    ``retriable`` flag and (where present) ``hint`` as a classified error — both
    read from the shared ``_app.errors`` tables (:func:`is_retriable` +
    :data:`CATEGORY_HINTS`), never re-derived here. Statuses with only a coarse
    label (:data:`_STATUS_LABEL`, e.g. 409/410) omit both. The ``detail`` is
    scrubbed + length-capped via :func:`_redact`.
    """
    body: dict[str, object] = {
        "category": _http_category_label(status),
        "message": _redact(detail),
    }
    category = _STATUS_CATEGORY.get(status)
    if category is not None:
        body["retriable"] = is_retriable(category)
        hint = CATEGORY_HINTS.get(category)
        if hint is not None:
            body["hint"] = hint
    return JSONResponse(status_code=status, content={"error": body})


def _project_classified(exc: BaseException, classified: ClassifiedError) -> dict[str, object]:
    """Build the inner ``{category, message, retriable, hint?}`` body from an
    ALREADY-computed classification — so a caller that also needs the status can
    classify once and reuse the result rather than re-running :func:`classify`.
    """
    category = classified.category
    message = _UNEXPECTED_MESSAGE if category is ErrorCategory.UNEXPECTED else _redact(str(exc))
    body: dict[str, object] = {
        "category": category.value,
        "message": message,
        "retriable": classified.retriable,
    }
    hint = CATEGORY_HINTS.get(category)
    # A failed *name* lookup may carry near-miss candidates (issue #1787); surface
    # them and swap the generic NOT_FOUND hint for a "Did you mean …" one.
    candidates = list(getattr(exc, "candidates", ()) or ())
    if candidates:
        body["candidates"] = candidates
        hint = did_you_mean_hint(candidates)
    if hint is not None:
        body["hint"] = hint
    return body


def error_item(exc: BaseException) -> dict[str, object]:
    """Project ``exc`` onto the inner ``{category, message, retriable, hint?}`` body.

    The single classify-once projection shared by :func:`error_response` (which
    wraps it in the ``{"error": ...}`` envelope + HTTP status) and any route that
    needs a per-item error shape WITHOUT aborting the request — e.g. the batch
    ``POST .../sources/batch`` route, which reports one such item per failed URL
    (mirroring the MCP ``source_add`` batch contract) while the batch as a whole
    still returns 201. The message is the scrubbed ``str(exc)`` for library
    errors and a fixed generic string for an unexpected (non-library) bug.
    """
    return _project_classified(exc, classify(exc))


def error_response(exc: BaseException) -> JSONResponse:
    """Build the typed JSON error response for ``exc``.

    Calls :func:`classify` exactly once — reusing that single result for BOTH the
    body projection and the :data:`CATEGORY_STATUS` status lookup; the category
    is never re-derived. The message is the scrubbed ``str(exc)`` for library
    errors, and a fixed generic string for an unexpected (non-library) bug —
    whose ``str(exc)`` is never echoed.

    The body also carries the neutral ``retriable`` flag (from :func:`classify`,
    so an agent client can branch a back-off) and, where the category has one, a
    ``hint`` — both drawn from the SAME shared tables the MCP surface uses
    (:func:`classify` + :data:`CATEGORY_HINTS`), never re-derived here.
    """
    classified = classify(exc)
    body = _project_classified(exc, classified)
    status = CATEGORY_STATUS[classified.category]
    return JSONResponse(status_code=status, content={"error": body})


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
