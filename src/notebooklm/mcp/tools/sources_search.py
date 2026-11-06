"""Source passage-search MCP tool.

Discrete read-only verb ``source_search`` over ``client.sources.search``, kept in
its own module so the ceiling-capped ``mcp/tools/sources.py`` does not grow
(ADR-0025 discrete-verb rationale). Supported on both Web and Android backends.

This module imports NO ``click`` / ``rich`` / ``cli``.
"""

from __future__ import annotations

from typing import Any

from fastmcp import Context

from ..._app.serialize import relevant_chunk_summary
from ...exceptions import ValidationError
from .._coerce import coerce_list
from .._confirm import READ_ONLY
from .._context import get_client
from .._errors import mcp_errors
from .._resolve import resolve_notebook, resolve_sources


def register(mcp: Any) -> None:
    """Register the ``source_search`` tool on ``mcp``."""

    @mcp.tool(annotations=READ_ONLY)
    async def source_search(
        ctx: Context,
        notebook: str,
        query: str,
        source_ids: list[str] | str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Search indexed passages across a notebook's sources by relevance.

        Accepts a notebook name or ID. ``source_ids`` optionally narrows the search
        to specific sources (name/prefix/ID; list, JSON-array string, or comma
        string; omit for all). ``limit`` caps the globally ranked passages returned.
        Each ``chunks`` row has ``source_id``, ``text``, ``rank``, and ``start`` /
        ``end`` offsets. Supported on Web and Android.
        """
        with mcp_errors():
            normalized_query = query.strip()
            if not normalized_query:
                raise ValidationError("query must be a non-empty string.")
            if limit is not None and (isinstance(limit, bool) or limit <= 0):
                raise ValidationError("limit must be a positive integer.")
            client = await get_client(ctx)
            nb_id = await resolve_notebook(client, notebook)
            refs = coerce_list(source_ids)
            resolved_source_ids = await resolve_sources(client, nb_id, refs) if refs else None
            chunks = await client.sources.search(
                nb_id,
                normalized_query,
                source_ids=resolved_source_ids,
                limit=limit,
            )
            payload: dict[str, Any] = {
                "notebook_id": nb_id,
                "query": normalized_query,
                "count": len(chunks),
                "chunks": [relevant_chunk_summary(chunk) for chunk in chunks],
            }
            if resolved_source_ids is not None:
                payload["source_ids"] = resolved_source_ids
            return payload
