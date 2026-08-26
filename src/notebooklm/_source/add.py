"""The source-add family's post-create title helper.

What is left of the pre-P10 URL/YouTube/text/Drive service after P10 R3.2–R3.4
hoisted ``add_text``, ``add_url`` and ``add_drive`` into ``SourceService`` over
the ``source.register`` leaf: one best-effort rename, reached only by the
``SOURCE_ADD_FILE`` row, which stays adapter-owned under decision D4 and
therefore keeps a public-``Source`` helper permanently.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import replace

from ..exceptions import NetworkError
from ..rpc import RPCError
from ..types import Source

RenameSource = Callable[[str, str, str], Awaitable[Source | None]]


async def honor_requested_title(
    rename: RenameSource,
    notebook_id: str,
    source: Source,
    requested_title: str | None,
    logger: logging.Logger,
) -> Source:
    """Best-effort post-add rename so an explicit ``title`` survives backend
    re-derivation (#1960).

    YouTube, native Google Drive, and web-page imports re-derive the display
    title server-side (from the video / Drive / page metadata), silently
    discarding the ``title`` sent with the add. Live-verified (URL, YouTube, and
    Drive): the backend derives the title *synchronously* — the added source comes
    back already carrying the re-derived title — so a follow-up ``rename`` lands
    after that derivation and sticks. When an explicit ``title`` differs from the
    one the add returned, issue the rename so the requested title wins.

    Non-fatal by contract: the add already succeeded, so a rename failure keeps
    the added source (with its upstream title) and logs a warning rather than
    raising — callers detect the miss by comparing the returned ``source.title``
    against the title they requested (the MCP tool surfaces this).
    """
    if not requested_title:
        return source
    requested = requested_title.strip()
    if not requested or source.title == requested:
        return source
    try:
        renamed = await rename(notebook_id, source.id, requested)
    except (RPCError, NetworkError):
        logger.warning(
            "Source %s added but rename to %r failed; keeping upstream title %r",
            source.id,
            requested,
            source.title,
            exc_info=True,
        )
        return source
    # UPDATE_SOURCE's echo can be sparse (id + title only), so returning it wholesale
    # would drop url / kind / status. Keep the fully-hydrated added source and swap in
    # just the new title — mirrors the file-upload rename (``_source/upload.py``).
    return replace(source, title=(renamed.title if renamed else None) or requested)


__all__ = ["honor_requested_title"]
