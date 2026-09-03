"""Google Play Books ("Expert Intelligence") MCP tools (#2292).

Two discrete verbs — ``source_list_play_books`` (read-only) and
``source_add_play_book`` — kept in their own module (with their own
``register``) so the ceiling'd ``mcp/tools/sources.py`` doesn't grow
(ADR-0025 discrete-verb rationale). Web backend only: on an Android-backed
server the client raises ``UnsupportedOperationError``, rendered as a tool
error by ``mcp_errors``.

This module imports NO ``click`` / ``rich`` / ``cli``.
"""

from __future__ import annotations

from typing import Any

from fastmcp import Context

from ..._app.serialize import play_book_summary, to_jsonable
from ..._app.source_play_books import (
    SourceAddPlayBookPlan,
    execute_source_add_play_book,
    fetch_play_books,
)
from ..._app.views import source_view as _source_view
from .._confirm import READ_ONLY
from .._context import get_client
from .._errors import mcp_errors
from .._paginate import DEFAULT_LIMIT, paginate
from .._resolve import resolve_notebook


def register(mcp: Any) -> None:
    """Register the Play Books tools on ``mcp``."""

    @mcp.tool(annotations=READ_ONLY)
    async def source_list_play_books(
        ctx: Context,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
    ) -> dict[str, Any]:
        """List Google Play Books eligible to be added as sources (Expert Intelligence).

        Returns the account's purchased ebooks NotebookLM can ingest (US only, 18+).
        Each row has ``content_id`` (pass it to ``source_add_play_book``), ``title``,
        ``authors``, ``export_disabled`` and ``reason`` (a non-exportable title cannot
        be added), plus ``cover_url`` / ``store_url``. Empty for an account with no
        Play Books library. Web backend only.
        """
        with mcp_errors():
            client = await get_client(ctx)
            books = await fetch_play_books(client)
            page, meta = paginate([play_book_summary(b) for b in books], limit, offset)
            return {"play_books": page, **meta}

    @mcp.tool
    async def source_add_play_book(
        ctx: Context,
        notebook: str,
        content_id: str,
        wait: bool = False,
    ) -> dict[str, Any]:
        """Add a Google Play Book as a source by its content id (Expert Intelligence).

        ``content_id`` is a volume id from ``source_list_play_books``. The title must
        be exportable — a publisher-blocked one is refused up front. Accepts a notebook
        name or ID. Reads back as an ``expert_intelligence`` source. Processed
        asynchronously; pass ``wait=true`` to block until READY. Web backend only.
        """
        with mcp_errors():
            client = await get_client(ctx)
            nb_id = await resolve_notebook(client, notebook)
            result = await execute_source_add_play_book(
                client,
                SourceAddPlayBookPlan(
                    notebook_id=nb_id,
                    content_id=content_id,
                    wait=wait,
                ),
            )
            payload = to_jsonable(result)
            payload["status"] = "added"
            payload["notebook_id"] = nb_id
            payload["content_id"] = result.content_id
            payload["source"] = _source_view(result.source)
            return payload
