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

from ._secrets import (
    AUTH_TOKEN_SHAPE_PATTERNS,
    COOKIE_VALUE_REPLACEMENT,
    COOKIE_VALUE_SUFFIX,
    SECURE_HOST_UMBRELLA_PATTERNS,
    build_cookie_alternation,
)

__all__ = [
    "RedactingFilter",
    "RedactingFormatter",
    "SECRET_FAST_PATH_TOKENS",
    "apply_redaction",
    "configure_logging",
    "correlation_id",
    "get_request_id",
    "install_redaction",
    "reset_request_id",
    "scrub_secrets",
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


# WIZ_global_data CSRF (SNlM0e) and session-id (FdrFJe) markers. These appear
# in HTML/JSON responses (``"SNlM0e":"AF1_QpN-..."``), in query / form bodies
# (``SNlM0e=...``), and in diagnostic prose (``SNlM0e value is AF1_QpN-...``).
# The marker name (and any quoting) is preserved; only the value is redacted.
# Split into three shape-specific patterns so each value class is anchored
# precisely and the capture groups are directly testable:
#   - QUOTED: ``"marker":"value"`` / ``'marker':'value'`` (JSON / inline JS).
#     Group 2 captures the verbatim run from the key's closing quote through
#     the value's opening quote (``"\s*:\s*"``) so it is reproduced exactly in
#     the replacement; group 3 is the value's quote char, back-referenced as
#     the closing quote. The value uses the escape-aware idiom
#     ``(?:[^"'\\]|\\.)*`` (mirroring the VCR cassette sanitizer in
#     ``tests/cassette_patterns.py``) so a JSON ``\"`` inside the value does
#     not terminate the match early and leak the tail. ``\s*`` around the
#     colon tolerates pretty-printed JSON.
#   - HTML_ESCAPED: ``&quot;marker&quot;:&quot;value&quot;`` (script block
#     rendered inside an HTML attribute). Terminates on the literal
#     ``&quot;``, so embedded entities (``&amp;``) survive into the redaction.
#   - UNQUOTED: ``marker=value`` / ``marker: value`` / ``marker value is value``
#     (query, form, or diagnostic prose). The value class excludes trailing
#     punctuation / brackets (``.?!)]}>``) so benign sentence punctuation and
#     enclosing parens are NOT swallowed into the redacted run.
# Each marker is its own alternation so capture group 1 is always the full
# marker name; ``\b`` left-anchors so it is not matched mid-identifier.
_CSRF_MARKER_QUOTED = re.compile(r"(\b(?:SNlM0e|FdrFJe))([\"']?\s*:\s*)([\"'])(?:[^\"'\\]|\\.)*\3")
_CSRF_MARKER_HTML_ESCAPED = re.compile(
    r"(\b(?:SNlM0e|FdrFJe))((?:&quot;)?\s*:\s*)(&quot;)(?:(?!&quot;).)*&quot;"
)
_CSRF_MARKER_UNQUOTED = re.compile(
    r"(\b(?:SNlM0e|FdrFJe))(\s*(?:value\s+is|[:=])\s*)[^\s\"'<>&;,.?!)\]}]+"
)

# Bare Google CSRF tokens. The CSRF value (``SNlM0e`` / the ``at=`` body param)
# is always emitted with the ``AF1_QpN-`` family prefix, so a standalone token
# is redacted even with no surrounding marker. The prefix is preserved as a
# shape hint; the secret suffix is dropped.
_CSRF_BARE_TOKEN = re.compile(r"(AF1_QpN-)[A-Za-z0-9_-]+")


# Bare session-cookie name alternation, DERIVED from the canonical runtime
# registry (``_secrets.RUNTIME_SESSION_COOKIES``) rather than hand-enumerated
# here. The registry is kept a superset of the cassette sanitizer's must-scrub
# bare session cookies by a parity guardrail test, so a cookie added there (the
# ``NID`` / ``LSOLH`` additions that motivated #1517) flows into this redaction
# alternation without a second edit. Cookie names are canonical mixed-case, so
# this pattern stays case-SENSITIVE.
#
# Two deliberate, fail-SAFE imprecisions documented here so they aren't "fixed"
# into leaks:
#   - A lowercase cookie name (``sid=…``) is NOT matched. This is acceptable:
#     Google emits canonical mixed/upper-case cookie names, and the carrier-
#     agnostic shape catch-alls below still scrub a credential-shaped value
#     under any casing.
#   - There is no left-boundary anchor, so ``BSID=…`` over-redacts to
#     ``BSID=***``. This is acceptable because the logging path errs toward MORE
#     redaction (fail-safe); the cassette scrubber, which must not corrupt
#     fixtures, keeps its own negative-lookbehind anchor separately.
#
# The value is matched by the shared quote-aware ``_secrets.COOKIE_VALUE_SUFFIX``
# so an RFC 6265 double-quoted value (``SID="opaque"``) is redacted too (gemini
# review of #1530). Group 1 is the cookie name; the suffix adds groups 2/3/4
# (opening quote / value / closing quote), redacted via ``COOKIE_VALUE_REPLACEMENT``.
_COOKIE_NAME_ALTERNATION = build_cookie_alternation()
_SESSION_COOKIE = re.compile(rf"({_COOKIE_NAME_ALTERNATION}){COOKIE_VALUE_SUFFIX}")

# ``__Secure-*`` / ``__Host-*`` prefix umbrellas, compiled from the same
# registry. These redact ANY secure/host cookie value by prefix alone, so a
# future cookie name not yet enumerated anywhere (``__Secure-NEWSESSION=…``)
# fails closed by construction (codex review of #1517). Group 1 preserves the
# full cookie name as a shape hint.
_SECURE_HOST_UMBRELLAS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p) for p in SECURE_HOST_UMBRELLA_PATTERNS
)

# Carrier-AGNOSTIC Google credential shapes (``g.a000-`` / ``sidts-`` /
# ``ya29.`` tokens + the ``AIza…`` API key), compiled from the same registry.
# Defense in depth: even when a secret rides under an UNKNOWN carrier name (a
# cookie or field not on the alternation above), the raw credential shape is
# redacted wherever it appears, so disclosure fails closed. The distinctive
# prefix is preserved as a shape hint; the secret tail is dropped.
_AUTH_TOKEN_SHAPES: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p) for p in AUTH_TOKEN_SHAPE_PATTERNS
)


# Patterns are immutable. Adding a new pattern requires a unit test.
# Order matters: longer / more-specific cookie names first within the cookie
# group so `SID` doesn't shadow `SAPISID`. Patterns are applied in sequence.
# Query / form-param value class + replacement. Like the cookie suffix, the
# value may be wrapped in optional double quotes (a logged JSON/dict fragment can
# carry ``at="…"`` / ``f.sid="…"``); the optional ``("?)`` quotes (groups 2 / 3)
# bracket the value so a quoted value is redacted too, not just an unquoted URL
# query param. The value class excludes ``&`` (query-pair delimiter) in addition
# to whitespace / quotes / angle brackets. Group 1 is the ``name=`` prefix; the
# replacement keeps the name + ``=`` + the surrounding quotes (if any) and
# collapses the value to ``***`` (gemini review of #1530).
_QUERY_VALUE_SUFFIX = r"(\"?)[^&\s\"'<>]+(\"?)"
_QUERY_VALUE_REPLACEMENT = r"\1\2***\3"

_REDACT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # CSRF / form-body auth tokens (Google batchexecute)
    (re.compile(r"(\bat=)" + _QUERY_VALUE_SUFFIX), _QUERY_VALUE_REPLACEMENT),
    # WIZ_global_data CSRF / session-id markers (see the pattern docs above).
    # The quoted / HTML-escaped variants run before the unquoted one so the
    # quote-aware value classes win on JSON-shaped input.
    (_CSRF_MARKER_QUOTED, r"\1\2\3***\3"),
    (_CSRF_MARKER_HTML_ESCAPED, r"\1\2\3***&quot;"),
    (_CSRF_MARKER_UNQUOTED, r"\1\2***"),
    # ``csrf=<value>`` form parameter (the CSRF token shows up canonically as
    # ``at=<csrf>``, but a ``csrf=`` alias must not leak the value either).
    (re.compile(r"(\bcsrf=)" + _QUERY_VALUE_SUFFIX, re.IGNORECASE), _QUERY_VALUE_REPLACEMENT),
    # Bare Google CSRF tokens (see the pattern docs above).
    (_CSRF_BARE_TOKEN, r"\1***"),
    # session-id query param
    (re.compile(r"(\bf\.sid=)" + _QUERY_VALUE_SUFFIX), _QUERY_VALUE_REPLACEMENT),
    # resumable-upload session query param
    (re.compile(r"(\bupload_id=)" + _QUERY_VALUE_SUFFIX, re.IGNORECASE), _QUERY_VALUE_REPLACEMENT),
    # OAuth-shaped credentials (refresh / access / authorization code)
    (
        re.compile(
            r"(\b(?:refresh_token|access_token|id_token|code)=)" + _QUERY_VALUE_SUFFIX,
            re.IGNORECASE,
        ),
        _QUERY_VALUE_REPLACEMENT,
    ),
    # Google session cookies — preserve name, redact value. The bare cookie-name
    # alternation is DERIVED from ``_secrets.RUNTIME_SESSION_COOKIES`` (longest-
    # first) rather than enumerated inline, so the cassette-registry parity
    # guardrail keeps it covering ``NID`` / ``LSOLH`` and the rest. ``\1`` is the
    # full cookie name; the shared quote-aware replacement preserves any
    # RFC 6265 double-quotes around the redacted value.
    (_SESSION_COOKIE, COOKIE_VALUE_REPLACEMENT),
    # ``__Secure-*`` / ``__Host-*`` umbrellas — redact ANY secure/host cookie
    # value by prefix alone, so a future name (``__Secure-NEWSESSION``) fails
    # closed without enumeration. ``\1`` is the full cookie name; the value
    # (quoted or not) collapses to ``***`` via the shared replacement.
    *((umbrella, COOKIE_VALUE_REPLACEMENT) for umbrella in _SECURE_HOST_UMBRELLAS),
    # Carrier-agnostic Google credential shapes (``g.a000-`` / ``sidts-`` /
    # ``ya29.`` tokens + the ``AIza…`` API key) as defense in depth. These run
    # AFTER the name-anchored patterns so a recognized pair is already redacted,
    # but a secret riding under an unknown carrier name still fails closed here.
    *((shape, "***") for shape in _AUTH_TOKEN_SHAPES),
    # Authorization: Bearer <token> (case-insensitive header name). Optional
    # surrounding quotes so a logged ``Bearer "token"`` (JSON/prose shape) is
    # redacted too, not just the bare-header form (gemini review of #1530).
    (
        re.compile(r"(Authorization:\s*Bearer\s+)" + _QUERY_VALUE_SUFFIX, re.IGNORECASE),
        _QUERY_VALUE_REPLACEMENT,
    ),
    # Cookie: <whole jar> (request header) and Set-Cookie: (response header)
    (re.compile(r"(Cookie:\s*)[^\r\n]+", re.IGNORECASE), r"\1***"),
    (re.compile(r"(Set-Cookie:\s*)[^\r\n]+", re.IGNORECASE), r"\1***"),
)

_HANDLER_MARKER = "_notebooklm_redacting"
_DEFAULT_FMT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
_DEFAULT_DATEFMT = "%H:%M:%S"

# Third-party loggers that emit notebooklm-py credentials at DEBUG/INFO (full
# request URLs carrying ?f.sid=, Cookie headers, etc.). A logger-level
# RedactingFilter is attached to each at import time so library consumers who
# enable these loggers (e.g. logging.basicConfig(level=DEBUG)) get scrubbed
# output WITHOUT us adding any handler — see _install_thirdparty_redaction.
_THIRD_PARTY_LOGGERS: tuple[str, ...] = ("httpx", "urllib3")

# Fast-path gate for ``scrub_secrets``. If none of these substrings appear in
# the input (compared case-insensitively), no pattern in ``_REDACT_PATTERNS``
# can possibly match, so we skip the full regex sweep. This is a STRICT
# SUPERSET of substrings appearing in any pattern — adding a new pattern to
# ``_REDACT_PATTERNS`` MUST be paired with a token here (or a clear note
# that the pattern's literal anchor is already covered). Order is
# insignificant; the gate is an OR.
#
# Casing: tokens are stored lowercase and the gate compares against the
# lowercased input. This matches the ``re.IGNORECASE`` patterns (OAuth +
# Authorization) so a log line with ``AUTHORIZATION: BEARER ...`` or
# ``Refresh_Token=...`` still triggers the regex sweep and gets redacted.
# The cookie-name pattern is case-SENSITIVE in the regex (cookie names are
# canonical), but ``"sid"`` as a substring of a lowercased input also
# matches lowercase ``sid`` — which would NOT match the case-sensitive
# regex. False positives in the gate (input contains the substring but the
# regex doesn't match) are harmless: we just run the regex sweep unnecessarily.
# False negatives (input is a secret but the gate skips) would shrink the
# redaction surface, so we avoid them by erring toward more triggering.
#
# Coverage map (pattern -> covering token in this set, all lowercase):
#   \bat=<csrf>                                          -> "at="
#   \b(SNlM0e|FdrFJe)<sep><value>                         -> "snlm0e" / "fdrfje"
#   \bcsrf=<csrf> (IGNORECASE)                           -> "csrf"
#   AF1_QpN-<bare csrf token>                            -> "af1_qpn-"
#   \bf\.sid=<sid>                                       -> "f.sid"
#   \bupload_id=<resumable upload token>                  -> "upload_id="
#   (refresh_token|access_token|id_token)= (IGNORECASE)  -> "_token="
#   \bcode= (IGNORECASE)                                 -> "code="
#   __Secure-*PAPISID/PSID(TS|CC)?/SAPISID/APISID/SIDCC/HSID/SSID/LSID/SID= -> "sid"
#   Authorization:\s*Bearer (IGNORECASE)                 -> "authorization"
#   Cookie: (IGNORECASE)                                 -> "cookie"
#   Set-Cookie: (IGNORECASE)                             -> "set-cookie" (also "cookie")
#
# Deviation notes vs. the originating redaction design:
#   - The design's literal token list is mixed-case (``SID``, ``SAPISID``,
#     ``CSRF``, ``Cookie``, ``Authorization``, ``Set-Cookie``). We lowercase
#     the gate to honor ``re.IGNORECASE`` on those patterns. Token VALUES
#     change to lowercase; the COVERAGE story (and the resulting redaction
#     surface) is preserved.
#   - "_token=" and "code=" extend the design's literal token list. The
#     design advertises "superset of substrings in any pattern" but its own
#     list omits OAuth anchors; without them the OAuth pattern would silently
#     stop redacting whenever a message had no other secret marker.
#   - "continue=" and "authuser=" are NOT in ``_REDACT_PATTERNS``. Including
#     them is harmless: they only INCREASE the regex-sweep rate, never the
#     redaction surface, and they hedge against future audit additions.
#   - "csrf" covers the ``csrf=<value>`` form alias. The canonical CSRF
#     token shows up as ``at=<csrf>`` (covered by "at="); the standalone
#     ``AF1_QpN-`` token shape is covered by "af1_qpn-".
#   - "snlm0e" / "fdrfje" cover the WIZ_global_data CSRF / session-id markers
#     in their JSON, query, and prose shapes. They are lowercased to match
#     the lowercased input even though the marker regex is case-sensitive
#     (the markers are canonical mixed-case identifiers); the lowercase
#     substring still triggers the sweep, which is harmless if the regex
#     then misses.
#   - "sapisid" is redundant given "sid", but kept as documentation that we
#     deliberately cover that cookie family.
#   - "nid", "lsolh" cover the ``NID`` / ``LSOLH`` cookie names that "sid" does
#     NOT subsume (issue #1517). The bare cookie-name alternation is derived from
#     ``_secrets.RUNTIME_SESSION_COOKIES``; any name there whose lowercase form
#     is not already a substring of another token must appear here or the fast
#     path would skip a real cookie. The parity guardrail keeps the registry
#     honest; this gate must keep up with it.
#   - "__secure-" / "__host-" cover the prefix umbrellas
#     (``_secrets.SECURE_HOST_UMBRELLA_PATTERNS``) so ANY secure/host cookie —
#     including a future name carrying an opaque (non-token-shaped) value —
#     triggers the regex sweep (codex review of #1517).
#   - "g.a000-", "sidts-", "ya29.", "aiza" cover the carrier-agnostic credential
#     shapes (``_secrets.AUTH_TOKEN_SHAPE_PATTERNS``) so a secret under an
#     UNKNOWN carrier name still triggers the regex sweep. "sidts-" is subsumed
#     by "sid" but kept as documentation of the token-shape coverage.
SECRET_FAST_PATH_TOKENS: tuple[str, ...] = (
    "sid",
    "sapisid",
    "nid",
    "lsolh",
    "__secure-",
    "__host-",
    "csrf",
    "snlm0e",
    "fdrfje",
    "af1_qpn-",
    "g.a000-",
    "sidts-",
    "ya29.",
    "aiza",
    "f.sid",
    "continue=",
    "authuser=",
    "upload_id=",
    "at=",
    "cookie",
    "authorization",
    "set-cookie",
    "_token=",
    "code=",
)


def scrub_secrets(text: object) -> str:
    """Redact credential-shaped substrings (CSRF tokens, session cookies, etc).

    Applies the package's shared redaction patterns to the input. Non-string
    inputs (Exception instances, custom __str__ objects) are coerced via
    ``str()`` before matching so callers can pass log-record fragments
    directly without pre-stringifying.

    Use this when including third-party text (HTML bodies, raw RPC payloads,
    diagnostic previews) in exception messages or other surfaces that escape
    the logging pipeline — the RedactingFilter only catches text that reaches
    a configured handler.

    Performance: a substring fast-path gate (``SECRET_FAST_PATH_TOKENS``)
    short-circuits the full regex sweep for the common case of innocuous
    application logs that contain no credential markers at all. Strings that
    DO contain any token still run the full pattern set, preserving the
    redaction surface exactly.
    """
    # Defensive: record.msg / stack_info can be non-string in unusual setups
    # (Exception instance, custom __str__ object). Coerce before regex.
    if not isinstance(text, str):
        text = str(text)
    # Fast-path: if no credential-shaped substring is present, every regex
    # in _REDACT_PATTERNS will miss. We lowercase once and compare against
    # the lowercase token set so case-insensitive patterns (OAuth + the
    # Authorization/Cookie headers) still trigger the regex sweep when their
    # anchors appear in non-canonical casing (``AUTHORIZATION:`` etc.).
    # Plain `in` on a short literal beats a compiled regex by ~10× on
    # innocuous messages even after paying for the lowercase copy.
    lowered = text.lower()
    if not any(token in lowered for token in SECRET_FAST_PATH_TOKENS):
        return text
    for pattern, replacement in _REDACT_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


# Backwards-compat alias for any in-package code that imported the historical
# private name. New code should use ``scrub_secrets`` directly.
_scrub = scrub_secrets


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
        record.msg = scrub_secrets(rendered)
        record.args = ()

        if record.exc_info and not record.exc_text:
            exc_text = logging.Formatter().formatException(record.exc_info)
            record.exc_text = scrub_secrets(exc_text)
        elif record.exc_text:
            record.exc_text = scrub_secrets(record.exc_text)

        # stack_info from logger.<level>(..., stack_info=True) — rarely used
        # but technically a leak vector.
        if record.stack_info:
            record.stack_info = scrub_secrets(record.stack_info)

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
        rendered = scrub_secrets(self._inner.format(record))
        # logging.Formatter.format() caches the rendered traceback on
        # record.exc_text as a side effect when exc_info is set and exc_text
        # was None. If we were called without the Filter pre-setting exc_text
        # (direct formatter usage, test code, future code paths), inner.format
        # may have just stored an UNSCRUBBED traceback on the record. Re-scrub
        # so the record cannot leak via a subsequent handler.
        if record.exc_text:
            record.exc_text = scrub_secrets(record.exc_text)
        return rendered

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        return self._inner.formatTime(record, datefmt)

    def formatException(self, ei: logging._SysExcInfoType | tuple[None, None, None]) -> str:
        return scrub_secrets(self._inner.formatException(ei))

    def formatStack(self, stack_info: str) -> str:
        return scrub_secrets(self._inner.formatStack(stack_info))


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


def _install_thirdparty_redaction(*logger_names: str) -> None:
    """Attach a logger-level RedactingFilter to third-party loggers.

    Unlike ``install_redaction`` (which adds a default StreamHandler so the
    third-party logger emits somewhere), this only adds a ``RedactingFilter``
    to the *logger* itself and never adds a handler. Logger-level filters run
    in ``Logger.handle`` before records are dispatched to handlers AND before
    propagation to ancestor loggers, so the record is scrubbed in place before
    any downstream handler (root's ``basicConfig`` handler included) renders
    it. This is pure defense-in-depth: a library consumer who never enables
    these loggers sees no behavior change, and one who enables httpx DEBUG via
    ``logging.basicConfig`` no longer leaks ``?f.sid=`` request URLs.

    Scope note: a logger-level filter only runs for records that *originate*
    on the named logger. Records emitted on a child logger (e.g.
    ``httpx._client``) propagate straight to ancestor *handlers* via
    ``callHandlers`` and never re-enter the ancestor's ``Logger.handle``, so
    the filter here does NOT see them. That is fine for issue #1166 because
    httpx emits its request-URL line from ``logging.getLogger("httpx")``
    directly; cover a child logger explicitly only if a future leak path
    emits there.

    Idempotent: re-running does not stack duplicate filters.
    """
    for name in logger_names:
        ext_logger = logging.getLogger(name)
        if not _has_redacting_filter(ext_logger.filters):
            ext_logger.addFilter(RedactingFilter())


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

    Also installs a logger-level RedactingFilter on httpx/urllib3 so library
    consumers who enable those loggers (without going through the CLI's ``-vv``
    path) still get credential-scrubbed request URLs and headers.
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

    _install_thirdparty_redaction(*_THIRD_PARTY_LOGGERS)


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
