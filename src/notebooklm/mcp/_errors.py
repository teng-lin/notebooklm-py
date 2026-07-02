"""Project the notebooklm exception hierarchy onto a structured MCP error.

The MCP server surfaces every tool failure as a FastMCP
:class:`~fastmcp.exceptions.ToolError` carrying a structured payload::

    {"code": str, "message": str, "retriable": bool, "hint"?: str}

The **category** decision is delegated to :func:`notebooklm._app.errors.classify`
(the single neutral source of truth shared with the CLI ``error_handler``); this
module only *projects* that category onto the MCP code vocabulary via
:data:`CATEGORY_TABLE`. The ``retriable`` flag is taken verbatim from the
classification — never re-derived here — so the two ladders cannot disagree
(pinned by ``tests/_guardrails/test_mcp_classify_consistency.py``).

Agents branch on ``code`` (back off on ``RATE_LIMITED`` / ``SERVER`` /
``TIMEOUT`` / ``ARTIFACT_TIMEOUT`` / ``NETWORK``, re-auth on ``AUTH``, stop on
``NOT_FOUND`` / ``VALIDATION``) and on the boolean ``retriable``; the optional
``hint`` carries a short remediation string for the actionable categories. The
``message`` is passed through :func:`redact` — the shared package secret-scrubber
(:func:`notebooklm._logging.scrub_secrets`, which masks bearer tokens / session
cookies / Google credential shapes) plus two MCP-specific patterns (signed
``/files/*`` URL tokens and local home-directory paths) — then whitespace-collapsed
and length-capped for the wire, while ``code`` and ``retriable`` are always
preserved.

This module imports NO ``click`` / ``rich`` / ``cli`` — only ``fastmcp``, the
``_app`` classification core, and the package secret-scrubber (``_logging``).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from fastmcp.exceptions import ToolError

from .._app.errors import ErrorCategory, classify
from .._logging import scrub_secrets
from ..exceptions import NotebookLMError

__all__ = [
    "CATEGORY_TABLE",
    "ERROR_CODES",
    "mcp_errors",
    "redact",
    "to_tool_error",
    "tool_error_payload",
]

#: Maximum wire length for a tool-error message before it is truncated.
_MAX_MESSAGE = 300

#: Generic message returned for an UNEXPECTED (non-library) exception. A bug's
#: ``str(exc)`` can carry anything (arbitrary paths, env-derived detail), and
#: :func:`redact` is a *denylist* of known credential/path shapes — so the raw text
#: of an unexpected error is never echoed. Mirrors the REST server's
#: ``server/_errors._UNEXPECTED_MESSAGE`` policy (the ``code``/``retriable`` flags
#: are still preserved so agents branch correctly).
_UNEXPECTED_MESSAGE = "An unexpected internal error occurred."

#: MCP-specific redaction patterns applied AFTER the shared ``scrub_secrets`` pass
#: (which already covers bearer tokens / session cookies / Google credential
#: shapes). Two surfaces only the MCP transport produces:
#:
#: 1. **Signed file-transfer URL tokens.** The ``/files/(dl|ul)/<token>``
#:    side-channel (ADR-0024) carries an HMAC token ``b64url(body).b64url(mac)``;
#:    the route prefix is kept as a shape hint and the whole token segment is
#:    dropped. The dot-inclusive class redacts the entire segment regardless of dot
#:    count (a malformed ``/files/dl/a.b.c`` leaves no ``.c`` tail), and it stops at
#:    ``/``, ``?``, whitespace, or end — so a token inside a full
#:    ``https://host/files/dl/<tok>?x=1`` URL is redacted in place.
#: 2. **Home-directory paths** (the OS username is PII / host disclosure). The
#:    username matcher (:data:`_HOME_USER`) is a word token (alphanumerics /
#:    underscore with INTERNAL dots/hyphens — ``john.doe``, ``web-admin`` — but no
#:    leading/trailing punctuation, so a trailing ``.``/``:``/``)`` of surrounding
#:    prose is never eaten). Two cases:
#:      * a single-word username is redacted ANYWHERE (no trailing separator
#:        needed — it has no space, so it cannot greedily cross prose), so a bare
#:        terminal ``/home/alice`` and ``/home/alice: denied`` are both masked
#:        while the prose survives;
#:      * a ``First Last`` username (one space) is only redacted when followed by a
#:        path separator, so the space cannot swallow following prose across
#:        multiple paths (``/home/a or /home/b/x`` must not become ``/home/***/x``).
#:    Deliberate fail-safe bounds: a two-word username NOT followed by a separator
#:    masks only its first word; ``/Users/Shared/…`` over-redacts; both err toward
#:    not mangling the message. Generic absolute paths (``/var``/``/tmp``) are
#:    intentionally NOT redacted (would mangle id-shaped ``NOT_FOUND``/RPC text for
#:    little gain). The token alternation is anchored on disjoint character classes
#:    (no overlapping quantifiers), so there is no catastrophic-backtracking risk.
_HOME_USER = r"\w+(?:[.-]+\w+)*"
_EXTRA_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(/files/(?:dl|ul)/)[A-Za-z0-9._-]+"), r"\1***"),
    (re.compile(rf"(/(?:home|Users)/)(?:{_HOME_USER} {_HOME_USER}(?=/)|{_HOME_USER})"), r"\1***"),
    (
        re.compile(
            rf"([A-Za-z]:[\\/]Users[\\/])(?:{_HOME_USER} {_HOME_USER}(?=[\\/])|{_HOME_USER})",
            re.IGNORECASE,
        ),
        r"\1***",
    ),
)

#: The MCP projection of each neutral :class:`ErrorCategory`: ``(code, hint)``.
#: Covers EVERY ``ErrorCategory`` value (pinned by ``test_errors.py``). ``hint``
#: is a short remediation string for the actionable categories, or ``None`` when
#: no useful action exists beyond reading the message.
CATEGORY_TABLE: dict[ErrorCategory, tuple[str, str | None]] = {
    ErrorCategory.NOT_FOUND: (
        "NOT_FOUND",
        "Check the id/name with the matching *_list tool; the resource may have been deleted.",
    ),
    ErrorCategory.AUTH: (
        "AUTH",
        "Re-authenticate (run `notebooklm login`) and retry.",
    ),
    ErrorCategory.RATE_LIMITED: (
        "RATE_LIMITED",
        "Back off and retry after a short delay.",
    ),
    ErrorCategory.VALIDATION: (
        "VALIDATION",
        "Fix the invalid argument and retry; this will not succeed unchanged.",
    ),
    ErrorCategory.CONFIG: (
        "CONFIG",
        "Check the auth profile / storage configuration.",
    ),
    ErrorCategory.NETWORK: (
        "NETWORK",
        "Transient connectivity issue; retry.",
    ),
    ErrorCategory.NOTEBOOK_LIMIT: (
        "NOTEBOOK_LIMIT",
        "Notebook quota is exhausted; delete an existing notebook first.",
    ),
    ErrorCategory.ARTIFACT_TIMEOUT: (
        "ARTIFACT_TIMEOUT",
        "Generation is still running; poll studio_status with the task_id.",
    ),
    ErrorCategory.TIMEOUT: (
        "TIMEOUT",
        "The operation did not finish in time; retry or poll for completion.",
    ),
    ErrorCategory.SERVER: (
        "SERVER",
        "Upstream NotebookLM error; retry after a short delay.",
    ),
    ErrorCategory.RPC: ("RPC", None),
    ErrorCategory.SOURCE_MUTATION: (
        "SOURCE_MUTATION",
        "Resolve the source reference (it was missing, ambiguous, or needs confirmation).",
    ),
    ErrorCategory.LIBRARY: ("ERROR", None),
    ErrorCategory.UNEXPECTED: ("UNEXPECTED", None),
}

#: Stable set of codes the server can emit (pinned by the manifest test).
ERROR_CODES: frozenset[str] = frozenset(code for code, _ in CATEGORY_TABLE.values())


def redact(message: str) -> str:
    """Scrub secrets, collapse whitespace, and length-cap a message for the wire.

    SDK exception messages are already designed to be secret-free (raw responses
    are truncated at construction, per ADR-0019), but a tool-error message can also
    carry upstream text or local paths. As defense-in-depth this runs the shared
    package scrubber (:func:`notebooklm._logging.scrub_secrets`, masking bearer
    tokens / session cookies / Google credential shapes) and the MCP-specific
    :data:`_EXTRA_PATTERNS` (signed ``/files/*`` URL tokens + local home-directory
    paths), THEN collapses whitespace and caps the length. Redaction runs **before**
    the length cap so a secret sitting near the cap can never be partially revealed.

    The single chokepoint is reused by both :func:`tool_error_payload` (MCP
    JSON-RPC tool errors) and the ``/files/*`` route handlers (:mod:`._fileroutes`).
    """
    text = scrub_secrets(message)
    for pattern, replacement in _EXTRA_PATTERNS:
        text = pattern.sub(replacement, text)
    text = " ".join(text.split())
    if len(text) > _MAX_MESSAGE:
        text = text[:_MAX_MESSAGE] + "…"
    return text


def tool_error_payload(exc: BaseException) -> dict[str, Any]:
    """Return the structured ``{code, message, retriable, hint?}`` for ``exc``.

    The category + retriability come from :func:`_app.errors.classify`; the code
    and hint come from :data:`CATEGORY_TABLE`. ``hint`` is omitted entirely when
    the category has no remediation string. For the UNEXPECTED category (a
    non-library bug, whose ``str(exc)`` could carry anything ``redact`` does not
    know to scrub) the message is the fixed :data:`_UNEXPECTED_MESSAGE` rather than
    the redacted exception text.
    """
    classified = classify(exc)
    code, hint = CATEGORY_TABLE[classified.category]
    message = (
        _UNEXPECTED_MESSAGE if classified.category is ErrorCategory.UNEXPECTED else redact(str(exc))
    )
    payload: dict[str, Any] = {
        "code": code,
        "message": message,
        "retriable": classified.retriable,
    }
    if hint is not None:
        payload["hint"] = hint
    return payload


def to_tool_error(exc: BaseException) -> ToolError:
    """Build a :class:`ToolError` carrying the structured payload for ``exc``.

    FastMCP serializes the ``ToolError`` message to the client. We encode the
    structured contract into the message as ``"<CODE>: <message>
    (retriable=<bool>)"`` so a client that only reads the flat message can still
    branch on the leading ``CODE:`` token and the ``retriable`` flag; the full
    payload (including ``hint``) is available via :func:`tool_error_payload` for
    structured consumers.
    """
    payload = tool_error_payload(exc)
    suffix = f" hint: {payload['hint']}" if "hint" in payload else ""
    return ToolError(
        f"{payload['code']}: {payload['message']} "
        f"(retriable={str(payload['retriable']).lower()}){suffix}"
    )


@contextmanager
def mcp_errors() -> Iterator[None]:
    """Translate any exception raised inside the block into a structured ``ToolError``.

    A ``NotebookLMError`` maps onto its classified ``code``; any other
    ``Exception`` is projected as ``UNEXPECTED`` (via ``classify`` + the table) so
    the advertised structured contract holds even for a bug in a tool body —
    nothing escapes ``mcp_errors()`` as a raw exception.

    ``asyncio.CancelledError`` / ``KeyboardInterrupt`` / ``SystemExit`` subclass
    ``BaseException`` (not ``Exception``), so ``except Exception`` deliberately
    lets them propagate uncaught — cancellation and shutdown are never swallowed
    into a ToolError.

    A context manager (not a decorator) is used deliberately so tool function
    signatures are preserved for FastMCP schema generation.
    """
    try:
        yield
    except NotebookLMError as exc:  # noqa: BLE001 - deliberate boundary translation
        raise to_tool_error(exc) from exc
    except Exception as exc:  # noqa: BLE001 - project unexpected bugs as UNEXPECTED
        raise to_tool_error(exc) from exc
