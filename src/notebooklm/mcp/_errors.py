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
``message`` is whitespace-collapsed and length-capped for the wire, but ``code``
and ``retriable`` are always preserved.

This module imports NO ``click`` / ``rich`` / ``cli`` — only ``fastmcp`` and the
``_app`` classification core.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from fastmcp.exceptions import ToolError

from .._app.errors import ErrorCategory, classify
from ..exceptions import NotebookLMError

__all__ = [
    "CATEGORY_TABLE",
    "ERROR_CODES",
    "mcp_errors",
    "to_tool_error",
    "tool_error_payload",
]

#: Maximum wire length for a tool-error message before it is truncated.
_MAX_MESSAGE = 300

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
        "Generation is still running; poll artifact_status with the task_id.",
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


def _redact(message: str) -> str:
    """Collapse whitespace and length-cap a message for the wire.

    SDK exception messages are already designed to be secret-free (raw responses
    are truncated at construction, per ADR-0019); we additionally cap the length
    so an unexpectedly long body cannot bloat the tool-error payload.
    """
    message = " ".join(message.split())
    if len(message) > _MAX_MESSAGE:
        message = message[:_MAX_MESSAGE] + "…"
    return message


def tool_error_payload(exc: BaseException) -> dict[str, Any]:
    """Return the structured ``{code, message, retriable, hint?}`` for ``exc``.

    The category + retriability come from :func:`_app.errors.classify`; the code
    and hint come from :data:`CATEGORY_TABLE`. ``hint`` is omitted entirely when
    the category has no remediation string.
    """
    classified = classify(exc)
    code, hint = CATEGORY_TABLE[classified.category]
    payload: dict[str, Any] = {
        "code": code,
        "message": _redact(str(exc)),
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
