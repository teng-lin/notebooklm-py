"""Both-mode confirmation + tool-annotation helpers for MCP tools.

Destructive tools (deletes) follow a **both-mode confirmation** contract: when
called with ``confirm=False`` (the default), the tool does NOT mutate — it
returns a :func:`needs_confirmation` envelope describing what *would* happen, so
the agent (or a human in the loop) can decide. Called with ``confirm=True`` it
performs the mutation. This keeps a careless agent from deleting data on a first
pass while still allowing a one-shot ``confirm=True`` call.

The :data:`READ_ONLY` / :data:`DESTRUCTIVE` :class:`ToolAnnotations` constants
are attached to tool registration (``@mcp.tool(annotations=...)``) so MCP hosts
can surface the right UX hints (``readOnlyHint`` / ``destructiveHint``). The
manifest guardrail pins which tools carry which annotation.

This module imports NO ``click`` / ``rich`` / ``cli`` — only the MCP SDK types.
"""

from __future__ import annotations

from typing import Any

from mcp.types import ToolAnnotations

__all__ = ["DESTRUCTIVE", "READ_ONLY", "needs_confirmation"]

#: Annotation for tools that only read state (``*_list`` / ``*_describe`` /
#: ``*_status`` / ``server_info``). ``readOnlyHint`` lets a host skip a
#: confirmation prompt; explicitly not destructive.
READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False)

#: Annotation for tools that can irreversibly remove data (the deletes). Paired
#: with the ``confirm`` parameter + :func:`needs_confirmation` both-mode flow.
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True)


def needs_confirmation(preview: dict[str, Any]) -> dict[str, Any]:
    """Return the ``needs_confirmation`` envelope for a not-yet-confirmed mutation.

    Args:
        preview: A JSON-able description of the mutation that *would* run (e.g.
            the resolved id + title of the resource to delete), so the caller can
            decide whether to re-invoke with ``confirm=True``.

    Returns:
        ``{"status": "needs_confirmation", "preview": preview}``.
    """
    return {"status": "needs_confirmation", "preview": preview}
