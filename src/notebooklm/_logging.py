"""Logging configuration and credential redaction for notebooklm-py.

Phase 0 of the gap remediation plan. See .sisyphus/plans/phase-0-implementation.md
for design rationale.

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
from collections.abc import Iterable
from typing import Any

__all__ = [
    "RedactingFilter",
    "RedactingFormatter",
    "apply_redaction",
    "configure_logging",
    "install_redaction",
]

_REDACT_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # CSRF / form-body auth tokens
    (re.compile(r"(\bat=)[^&\s\"'<>]+"), r"\1***"),
    # session-id query param
    (re.compile(r"(\bf\.sid=)[^&\s\"'<>]+"), r"\1***"),
    # Google session cookies — preserve name, redact value
    (
        re.compile(r"(SAPISID|__Secure-1PSID|__Secure-3PSID|HSID|SSID|SID)=([^;\s,\"'<>]+)"),
        r"\1=***",
    ),
    # Authorization: Bearer <token>
    (
        re.compile(r"(Authorization:\s*Bearer\s+)[^\s\"'<>]+", re.IGNORECASE),
        r"\1***",
    ),
    # Cookie: <whole jar> header
    (re.compile(r"(Cookie:\s*)[^\r\n]+", re.IGNORECASE), r"\1***"),
]

_HANDLER_MARKER = "_notebooklm_redacting"
_DEFAULT_FMT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
_DEFAULT_DATEFMT = "%H:%M:%S"


def _scrub(text: str) -> str:
    for pattern, replacement in _REDACT_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _has_redacting_filter(filters: Iterable[Any]) -> bool:
    # filters is logging.Handler.filters: list[Filter | _FilterCallable | ...].
    # Iterable[Any] sidesteps the typeshed union without losing type checking.
    return any(isinstance(f, RedactingFilter) for f in filters)


def _has_marked_handler(handlers: list[logging.Handler]) -> bool:
    return any(getattr(h, _HANDLER_MARKER, False) for h in handlers)


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
        return _scrub(self._inner.format(record))

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        return self._inner.formatTime(record, datefmt)

    def formatException(self, ei) -> str:
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

        handler = logging.StreamHandler()
        handler.setLevel(logging.NOTSET)
        handler.setFormatter(logging.Formatter(_DEFAULT_FMT, _DEFAULT_DATEFMT))
        apply_redaction(handler)
        logger.addHandler(handler)

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
            handler = logging.StreamHandler()
            handler.setLevel(logging.NOTSET)
            handler.setFormatter(logging.Formatter(_DEFAULT_FMT, _DEFAULT_DATEFMT))
            apply_redaction(handler)
            ext_logger.addHandler(handler)
