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

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from fastmcp.exceptions import ToolError

from .._app.errors import CATEGORY_HINTS, ErrorCategory, classify, did_you_mean_hint
from .._redact import redact
from ..exceptions import NotebookLMError

__all__ = [
    "CATEGORY_TABLE",
    "ERROR_CODES",
    "mcp_errors",
    "redact",
    "to_tool_error",
    "tool_error_payload",
]

#: Generic message returned for an UNEXPECTED (non-library) exception. A bug's
#: ``str(exc)`` can carry anything (arbitrary paths, env-derived detail), and
#: :func:`redact` is a *denylist* of known credential/path shapes — so the raw text
#: of an unexpected error is never echoed. Mirrors the REST server's
#: ``server/_errors._UNEXPECTED_MESSAGE`` policy (the ``code``/``retriable`` flags
#: are still preserved so agents branch correctly).
_UNEXPECTED_MESSAGE = "An unexpected internal error occurred."

#: The MCP manifest ``code`` for each neutral :class:`ErrorCategory`. The hint
#: half lives in the shared :data:`notebooklm._app.errors.CATEGORY_HINTS` (reused
#: by the REST error body too, so the two surfaces cannot drift);
#: :data:`CATEGORY_TABLE` pairs each code with that shared hint.
_CATEGORY_CODES: dict[ErrorCategory, str] = {
    ErrorCategory.NOT_FOUND: "NOT_FOUND",
    ErrorCategory.AUTH: "AUTH",
    ErrorCategory.RATE_LIMITED: "RATE_LIMITED",
    ErrorCategory.VALIDATION: "VALIDATION",
    ErrorCategory.CONFIG: "CONFIG",
    ErrorCategory.NETWORK: "NETWORK",
    ErrorCategory.NOTEBOOK_LIMIT: "NOTEBOOK_LIMIT",
    ErrorCategory.ARTIFACT_TIMEOUT: "ARTIFACT_TIMEOUT",
    ErrorCategory.TIMEOUT: "TIMEOUT",
    ErrorCategory.SERVER: "SERVER",
    ErrorCategory.RPC: "RPC",
    ErrorCategory.SOURCE_MUTATION: "SOURCE_MUTATION",
    ErrorCategory.LIBRARY: "ERROR",
    ErrorCategory.UNEXPECTED: "UNEXPECTED",
}

#: The MCP projection of each neutral :class:`ErrorCategory`: ``(code, hint)``.
#: Covers EVERY ``ErrorCategory`` value (pinned by ``test_errors.py``). ``hint``
#: is a short remediation string for the actionable categories (from the shared
#: :data:`CATEGORY_HINTS`), or ``None`` when no useful action exists beyond
#: reading the message.
CATEGORY_TABLE: dict[ErrorCategory, tuple[str, str | None]] = {
    category: (code, CATEGORY_HINTS[category]) for category, code in _CATEGORY_CODES.items()
}

#: Stable set of codes the server can emit (pinned by the manifest test).
ERROR_CODES: frozenset[str] = frozenset(code for code, _ in CATEGORY_TABLE.values())


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
    # A failed *name* lookup may carry near-miss candidates (issue #1787). Surface
    # them structurally AND replace the generic NOT_FOUND hint with a "Did you
    # mean …" one that names the titles inline — the FastMCP wire flattens this
    # payload to a string, so the inline titles are what a flat-string client sees.
    candidates = list(getattr(exc, "candidates", ()) or ())
    if candidates:
        payload["candidates"] = candidates
        hint = did_you_mean_hint(candidates)
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
