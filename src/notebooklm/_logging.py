"""Logging configuration and credential redaction for notebooklm-py.

The package logger is configured at import time via configure_logging(). Every
record reaching the package handler passes through a RedactingFilter that
mutates the record in place, scrubbing CSRF tokens, session cookies, and other
credential-shaped substrings from record.msg / record.exc_text. The handler's
RedactingFormatter is a decorator that wraps any inner formatter and post-
scrubs the rendered output as belt-and-suspenders.

propagate is left True so records flow to root for caplog / basicConfig users;
the in-place mutation ensures downstream handlers see scrubbed data.
"""

from __future__ import annotations

import logging
import os
import re
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Any

__all__ = [
    "RedactingFilter",
    "RedactingFormatter",
    "apply_redaction",
    "configure_logging",
    "correlation_id",
    "get_request_id",
    "install_redaction",
    "reset_request_id",
    "set_request_id",
]


# Per-asyncio-Task correlation id. Set by rpc_call() (and any other entry
# point that wants its records tagged); read by RedactingFilter when each
# record is processed.
_current_request_id: ContextVar[str | None] = ContextVar("notebooklm_request_id", default=None)


def set_request_id(req_id: str | None = None) -> Token[str | None]:
    """Set the correlation id for this Task / context. Returns a token.

    Callers MUST pass the token to ``reset_request_id`` in a ``finally``
    block — otherwise the id leaks into later same-Task logs.

    Pass ``req_id=None`` to generate a fresh 8-char hex id.
    """
    if req_id is None:
        req_id = uuid.uuid4().hex[:8]
    return _current_request_id.set(req_id)


def reset_request_id(token: Token[str | None]) -> None:
    """Restore the correlation id to its previous value."""
    _current_request_id.reset(token)


def get_request_id() -> str | None:
    """Return the current correlation id, or None if unset."""
    return _current_request_id.get()


@contextmanager
def correlation_id(req_id: str | None = None) -> Iterator[str]:
    """Scope log records and RPC telemetry under a caller-chosen correlation id.

    Pass ``req_id=None`` to generate a fresh 8-character id. Nested scopes
    restore the previous id on exit.
    """
    token = set_request_id(req_id)
    try:
        current = get_request_id()
        # ``set_request_id(None)`` always generates a string; the fallback keeps
        # static checkers happy if that implementation ever changes.
        yield current or ""
    finally:
        reset_request_id(token)


# Patterns are immutable. Adding a new pattern requires a unit test.
# Order matters: longer / more-specific cookie names first within the cookie
# group so `SID` doesn't shadow `SAPISID`. Patterns are applied in sequence.
_REDACT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # CSRF / form-body auth tokens (Google batchexecute)
    (re.compile(r"(\bat=)[^&\s\"'<>]+"), r"\1***"),
    # session-id query param
    (re.compile(r"(\bf\.sid=)[^&\s\"'<>]+"), r"\1***"),
    # OAuth-shaped credentials (refresh / access / authorization code)
    (
        re.compile(
            r"(\b(?:refresh_token|access_token|id_token|code)=)[^&\s\"'<>]+",
            re.IGNORECASE,
        ),
        r"\1***",
    ),
    # Google session cookies — preserve name, redact value. Longer/more-
    # specific names first so SAPISID/SIDCC/SSID don't get partially shadowed
    # by a bare-SID match.
    (
        re.compile(
            r"(__Secure-1PSIDCC|__Secure-3PSIDCC|__Secure-1PSID|__Secure-3PSID"
            r"|SAPISID|APISID|SIDCC|HSID|SSID|LSID|SID)=([^;\s,\"'<>]+)"
        ),
        r"\1=***",
    ),
    # Authorization: Bearer <token> (case-insensitive header name)
    (
        re.compile(r"(Authorization:\s*Bearer\s+)[^\s\"'<>]+", re.IGNORECASE),
        r"\1***",
    ),
    # Cookie: <whole jar> (request header) and Set-Cookie: (response header)
    (re.compile(r"(Cookie:\s*)[^\r\n]+", re.IGNORECASE), r"\1***"),
    (re.compile(r"(Set-Cookie:\s*)[^\r\n]+", re.IGNORECASE), r"\1***"),
)

_HANDLER_MARKER = "_notebooklm_redacting"
_DEFAULT_FMT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
_DEFAULT_DATEFMT = "%H:%M:%S"


def _scrub(text: object) -> str:
    # Defensive: record.msg / stack_info can be non-string in unusual setups
    # (Exception instance, custom __str__ object). Coerce before regex.
    if not isinstance(text, str):
        text = str(text)
    for pattern, replacement in _REDACT_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _has_redacting_filter(filters: Iterable[Any]) -> bool:
    # filters is logging.Handler.filters: list[Filter | _FilterCallable | ...].
    # Iterable[Any] sidesteps the typeshed union without losing type checking.
    return any(isinstance(f, RedactingFilter) for f in filters)


def _has_marked_handler(handlers: list[logging.Handler]) -> bool:
    return any(getattr(h, _HANDLER_MARKER, False) for h in handlers)


def _make_default_handler() -> logging.StreamHandler:
    """Create a StreamHandler with the package's default format, wrapped for redaction."""
    handler = logging.StreamHandler()
    handler.setLevel(logging.NOTSET)
    handler.setFormatter(logging.Formatter(_DEFAULT_FMT, _DEFAULT_DATEFMT))
    apply_redaction(handler)
    return handler


class RedactingFilter(logging.Filter):
    """Mutates LogRecord in place so downstream processing sees scrubbed data.

    Attached to a Handler. Runs for every record reaching that handler,
    including records from child loggers reaching the handler via propagation.

    - Sets record.msg to the scrubbed interpolated message.
    - Sets record.args = () so re-formatting does not re-introduce secrets.
    - If record.exc_info is set, pre-renders the traceback into a scrubbed
      record.exc_text. PRESERVES record.exc_info — handlers that inspect the
      live exception (Sentry) still see it; standard formatters prefer
      exc_text and won't re-render.
    - The live exception object is never mutated.

    Always returns True. The filter mutates; it does not reject.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            rendered = record.getMessage()
        except (TypeError, ValueError):
            rendered = str(record.msg)
        record.msg = _scrub(rendered)
        record.args = ()

        if record.exc_info and not record.exc_text:
            exc_text = logging.Formatter().formatException(record.exc_info)
            record.exc_text = _scrub(exc_text)
        elif record.exc_text:
            record.exc_text = _scrub(record.exc_text)

        # stack_info from logger.<level>(..., stack_info=True) — rarely used
        # but technically a leak vector.
        if record.stack_info:
            record.stack_info = _scrub(record.stack_info)

        # Correlation prefix. AFTER scrub so an 8-hex id can never be
        # accidentally scrubbed by a future pattern. Marker attribute
        # prevents double-prefix when a record is processed by multiple
        # handlers each with this filter.
        req_id = _current_request_id.get()
        if req_id and not getattr(record, "_notebooklm_reqid_applied", False):
            record.msg = f"[req={req_id}] {record.msg}"
            record._notebooklm_reqid_applied = True

        return True


class RedactingFormatter(logging.Formatter):
    """Decorator-pattern formatter. Wraps any inner formatter, post-scrubs output.

    Preserves the inner formatter's style ('%', '{', '$'), datefmt, custom
    formatException / formatStack, and subclass features. The Filter is the
    primary security mechanism; this formatter is a final-rendered-output
    pass for belt-and-suspenders.
    """

    def __init__(self, inner: logging.Formatter | None = None) -> None:
        super().__init__()
        self._inner = (
            inner
            if inner is not None
            else logging.Formatter(
                _DEFAULT_FMT,
                _DEFAULT_DATEFMT,
            )
        )

    def format(self, record: logging.LogRecord) -> str:
        rendered = _scrub(self._inner.format(record))
        # logging.Formatter.format() caches the rendered traceback on
        # record.exc_text as a side effect when exc_info is set and exc_text
        # was None. If we were called without the Filter pre-setting exc_text
        # (direct formatter usage, test code, future code paths), inner.format
        # may have just stored an UNSCRUBBED traceback on the record. Re-scrub
        # so the record cannot leak via a subsequent handler.
        if record.exc_text:
            record.exc_text = _scrub(record.exc_text)
        return rendered

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        return self._inner.formatTime(record, datefmt)

    def formatException(self, ei: logging._SysExcInfoType | tuple[None, None, None]) -> str:
        return _scrub(self._inner.formatException(ei))

    def formatStack(self, stack_info: str) -> str:
        return _scrub(self._inner.formatStack(stack_info))


def apply_redaction(handler: logging.Handler) -> logging.Handler:
    """Ensure a Handler has the RedactingFilter and a RedactingFormatter wrap.

    Idempotent. Preserves the handler's existing formatter (style, datefmt,
    custom subclass) by wrapping it via RedactingFormatter. Marks the handler
    with the package-private _notebooklm_redacting attribute.

    Use when attaching your own handler to the `notebooklm` logger so that
    handler also benefits from credential scrubbing.
    """
    if not _has_redacting_filter(handler.filters):
        handler.addFilter(RedactingFilter())

    existing = handler.formatter
    if not isinstance(existing, RedactingFormatter):
        handler.setFormatter(RedactingFormatter(existing))

    setattr(handler, _HANDLER_MARKER, True)
    return handler


def configure_logging() -> None:
    """Configure the `notebooklm` package logger with credential redaction.

    Defensive: enforces invariants on every call. Pre-existing handlers
    attached by an application before we got here get the RedactingFilter
    and decorator-wrapped RedactingFormatter — we do not silently skip them.

    Honors NOTEBOOKLM_LOG_LEVEL and NOTEBOOKLM_DEBUG_RPC.

    propagate is left True so records flow to root (caplog, basicConfig).
    The in-place filter mutation ensures downstream handlers see scrubbed
    data. Applications that want isolated notebooklm logs should set
    logging.getLogger("notebooklm").propagate = False themselves.
    """
    logger = logging.getLogger("notebooklm")

    for h in logger.handlers:
        apply_redaction(h)

    if not _has_marked_handler(logger.handlers):
        level_name = os.environ.get("NOTEBOOKLM_LOG_LEVEL", "WARNING").upper()
        if os.environ.get("NOTEBOOKLM_DEBUG_RPC", "").lower() in ("1", "true", "yes"):
            level_name = "DEBUG"
        logger.setLevel(getattr(logging, level_name, logging.WARNING))
        logger.addHandler(_make_default_handler())

    logger.propagate = True


def install_redaction(*logger_names: str) -> None:
    """Apply RedactingFilter + RedactingFormatter to additional loggers.

    Use for third-party libraries that emit credentials at DEBUG level
    (httpx, urllib3, asyncio). Records from child loggers (httpx._client,
    urllib3.connectionpool) reach the named-logger's handler via propagation,
    where the filter scrubs them en route.

    If a third-party library sets propagate=False on its internal loggers
    (rare), pass child names explicitly:

        install_redaction("httpx._client", "urllib3.connectionpool")

    Does NOT touch the root logger.
    """
    for name in logger_names:
        ext_logger = logging.getLogger(name)
        for h in ext_logger.handlers:
            apply_redaction(h)
        if not _has_marked_handler(ext_logger.handlers):
            ext_logger.addHandler(_make_default_handler())
