"""Pass-through resolvers shared by the MCP tool adapters.

The transport-neutral ``_app`` executors take injected ``resolve_notebook_id`` /
``resolve_note_id`` / ``resolve_source_id`` callables shaped for the CLI (which
turns a human ``<id|name>`` reference into a canonical id). MCP tools resolve
every reference up front via :mod:`.._resolve`, so they hand the executors a
trivial resolver that returns the already-resolved id unchanged.

Two shapes recur across the tool modules and live here:

* :func:`passthrough_notebook_id` — ``(client, notebook_id, *, json_output) -> id``
  (used by ``notebook_*`` / ``note_*`` / ``artifact_*``).
* :func:`passthrough_child_id` — ``(client, notebook_id, child_id, *, json_output)
  -> child_id`` for a notebook-scoped child id (used by ``source_*`` / ``note_*``).

Resolvers with a one-off shape (e.g. a source-id *list*, or the download core's
resolver signatures) stay co-located with their single caller.

This module imports NO ``click`` / ``rich`` / ``cli``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ...client import NotebookLMClient

__all__ = ["passthrough_child_id", "passthrough_notebook_id"]


async def passthrough_notebook_id(
    _client: NotebookLMClient, notebook_id: str, *, json_output: bool = False
) -> str:
    """Return ``notebook_id`` unchanged (MCP resolves refs before the executor)."""
    return notebook_id


async def passthrough_child_id(
    _client: NotebookLMClient,
    _notebook_id: str,
    child_id: str,
    *,
    json_output: bool = False,
) -> str:
    """Return ``child_id`` unchanged (MCP resolves notebook-scoped refs up front)."""
    return child_id
