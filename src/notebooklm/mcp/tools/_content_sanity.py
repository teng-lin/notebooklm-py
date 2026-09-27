"""Advisory content-sanity checks for READY web-page sources.

A dead link / soft-404 / paywalled page is often ingested as a READY source with
little/no extractable text (or a full-bodied "broken link" boilerplate) — a
"ghost source" that add-time status can't catch because a soft-404 serves HTTP
200. :func:`_thin_content_warning` returns a non-blocking, advisory ``warning``
for such a source; :func:`_annotate_thin_warnings` runs it concurrently over the
ready web-page views and attaches the warning in place.

Extracted from ``sources.py`` (it stayed under the ADR-0008 module-size budget):
the logic is a self-contained, reusable unit consumed by both the wait aggregate
(:func:`._waitagg._aggregate_wait_outcomes`, behind ``source_wait`` /
``source_add(wait=True)``) and the ``source_add`` batch
(:func:`._sources._add_url_batch`). Reads only ``_app.source_content`` — imports
NO ``click`` / ``rich`` / ``cli`` (MCP-layer boundary).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from ..._app import source_content as content_core
from ...types import SourceType

if TYPE_CHECKING:
    from ...client import NotebookLMClient
    from ...types import Source

# Keep historical imports used by advisory callers/tests.
from ..._app.content_sanity import (
    _BOT_CHALLENGE_BODY_SCAN_LIMIT as _BOT_CHALLENGE_BODY_SCAN_LIMIT,
)
from ..._app.content_sanity import (
    _SOFT_404_BODY_SCAN_LIMIT as _SOFT_404_BODY_SCAN_LIMIT,
)
from ..._app.content_sanity import (
    _THIN_SOURCE_CHAR_THRESHOLD as _THIN_SOURCE_CHAR_THRESHOLD,
)
from ..._app.content_sanity import (
    _THIN_SOURCE_FETCH_TIMEOUT_SECONDS as _THIN_SOURCE_FETCH_TIMEOUT_SECONDS,
)
from ..._app.content_sanity import (
    text_content_warning,
)


async def _annotate_thin_warnings(
    client: NotebookLMClient,
    notebook_id: str,
    ready_pairs: list[tuple[dict[str, Any], Source]],
) -> None:
    """Attach a thin-content ``warning`` to each ready web-page view, in place.

    Fetches the indexed body for the ready web-page sources concurrently (reads,
    capped by the client's RPC semaphore); non-web-page sources are filtered out up
    front so they never schedule a no-op task. Drives explicit tasks and, on any
    escape (e.g. a propagating ``CancelledError``), cancels + drains the still-running
    sibling fetches before re-raising — no leaked coroutine. Mirrors
    ``_sources._wait_all_sources``.
    """
    web_page_pairs = [
        (view, source) for view, source in ready_pairs if source.kind == SourceType.WEB_PAGE
    ]
    if not web_page_pairs:
        return
    tasks = [
        asyncio.create_task(_thin_content_warning(client, notebook_id, source))
        for _view, source in web_page_pairs
    ]
    try:
        warnings = await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    for (view, _source), warning in zip(web_page_pairs, warnings, strict=True):
        if warning is not None:
            # ``setdefault``: never clobber a warning the caller already set (e.g. the
            # batch's import-failed signal) — though ready ⟹ not is_error, so today no
            # ready pair carries one. Cheap future-proofing.
            view.setdefault("warning", warning)


async def _thin_content_warning(
    client: NotebookLMClient, notebook_id: str, source: Source
) -> str | None:
    """Return a content-sanity warning for a READY web-page source, else ``None``.

    A dead link / soft-404 / paywalled page is often ingested as a READY source
    with little/no extractable text — a "ghost source" add-time status can't catch
    (a soft-404 serves HTTP 200). Two body-only signals (the title is **never**
    scanned — a generic "Article | …" title carries no signal and matching it would
    false-positive on legit CMS pages):

    1. **char-thin** — fewer than :data:`_THIN_SOURCE_CHAR_THRESHOLD` chars of
       indexed text (empty / near-empty page).
    2. **dead-link boilerplate** — a body SHORTER than
       :data:`_SOFT_404_BODY_SCAN_LIMIT` chars that contains a
       :data:`_SOFT_404_PHRASES` marker (a full-bodied "Whoops! broken link" page
       that sails past the char-thin rule). The length gate runs BEFORE the body is
       casefolded, so a large healthy page is never lowercased + scanned.
    3. **bot-challenge / WAF interstitial** — a body SHORTER than
       :data:`_BOT_CHALLENGE_BODY_SCAN_LIMIT` chars (a wider cap than the dead-link
       pass) that contains a :data:`_BOT_CHALLENGE_PHRASES` marker (a Cloudflare
       "Just a moment…" or Akamai "Access Denied" page that ingests as ready but is
       not the real content). Dead-link takes precedence when a body trips both.

    **web-page only** (short pasted text / transcripts are legitimate, never flagged;
    callers also pre-filter). **best-effort**: the body fetch reuses
    ``source_read``'s (detail="full") ``GET_SOURCE``, bounded by
    :data:`_THIN_SOURCE_FETCH_TIMEOUT_SECONDS`; ANY failure (timeout, transport,
    unexpected shape) degrades to ``None`` so it can never break a wait
    (``except Exception`` — ``CancelledError`` still propagates). **Never rejects.**
    """
    if not source.is_ready or source.kind != SourceType.WEB_PAGE:
        return None
    try:
        result = await asyncio.wait_for(
            content_core.execute_source_fulltext(
                client,
                content_core.SourceFulltextPlan(
                    notebook_id=notebook_id, source_id=source.id, output_format="text"
                ),
            ),
            timeout=_THIN_SOURCE_FETCH_TIMEOUT_SECONDS,
        )
        return text_content_warning(
            result.fulltext.content or "", char_count=result.fulltext.char_count
        )
    except Exception:  # noqa: BLE001 - sanity check must never break a wait
        return None
    return None
