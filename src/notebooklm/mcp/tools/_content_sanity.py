"""Advisory content-sanity checks for READY web-page sources.

A dead link / soft-404 / paywalled page is often ingested as a READY source with
little/no extractable text (or a full-bodied "broken link" boilerplate) — a
"ghost source" that add-time status can't catch because a soft-404 serves HTTP
200. :func:`_thin_content_warning` returns a non-blocking, advisory ``warning``
for such a source; :func:`_annotate_thin_warnings` runs it concurrently over the
ready web-page views and attaches the warning in place.

Extracted from ``sources.py`` (it stayed under the ADR-0008 module-size budget):
the logic is a self-contained, reusable unit consumed by both ``source_wait``
(:func:`._sources._aggregate_wait_outcomes`) and the ``source_add`` batch
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

#: A READY ``web_page`` source whose indexed text is shorter than this many
#: characters gets a non-blocking content-sanity ``warning`` on ``source_wait``
#: (likely a dead link / soft-404 / paywall "ghost source"). Deliberately
#: conservative — advisory only, never a rejection. See :func:`_thin_content_warning`.
_THIN_SOURCE_CHAR_THRESHOLD = 100

#: Per-source budget for the advisory thin-content body fetch. The check is
#: best-effort and must not let ``source_wait`` overrun its own ``timeout`` waiting
#: on a slow ``GET_SOURCE`` — a fetch that exceeds this degrades to no warning.
_THIN_SOURCE_FETCH_TIMEOUT_SECONDS = 5.0

#: A READY ``web_page`` source can ingest a soft-404 / dead link as a full-bodied
#: page (HTTP 200) whose body is the site's "broken link" boilerplate — too long to
#: trip the char-thin rule above. Only bodies SHORTER than this are scanned for a
#: dead-link phrase below: a real error-page body is almost always small, and the
#: gate keeps a large healthy page from being lowercased + substring-scanned (and
#: narrows the false-positive window for the weaker phrases). The reported case was
#: 1,766 chars; 2000 leaves a ~13% margin.
_SOFT_404_BODY_SCAN_LIMIT = 2000

#: Multi-word / anchored dead-link & error-page markers scanned (casefolded) in a
#: sub-:data:`_SOFT_404_BODY_SCAN_LIMIT` ``web_page`` body. Deliberately anchored —
#: NO bare ``"404"`` / ``"oops"`` / ``"not found"`` — and, because they only fire on
#: a short body, even the weaker markers ("broken link") match only a soft-404-shaped
#: page. Advisory only; misses non-English / non-matching error pages. See
#: :func:`_thin_content_warning`.
_SOFT_404_PHRASES = frozenset(
    {
        "broken link",
        "page not found",
        "page isn't available",
        "page does not exist",
        "page no longer available",
        "no longer available",
        "error 404",
        "404 not found",
        "whoops!",
    }
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
        char_count = result.fulltext.char_count
        if char_count < _THIN_SOURCE_CHAR_THRESHOLD:
            return (
                f"little/no text extracted ({char_count} chars) — may be empty, "
                "not-yet-indexed, a soft-404/dead link, blocked, or paywalled; "
                'verify with source_read (detail="full").'
            )
        # ponytail: a short multi-word phrase scan over a length-gated body — no
        # liveness probe, no classifier; misses non-English / long-bodied error pages.
        if char_count < _SOFT_404_BODY_SCAN_LIMIT:
            # ``content`` is typed ``str`` but guard against a backend that reports a
            # non-thin ``char_count`` yet a ``None`` body — make the intent explicit
            # rather than lean on the outer ``except`` silently swallowing it.
            body = (result.fulltext.content or "").casefold()
            if any(phrase in body for phrase in _SOFT_404_PHRASES):
                return (
                    f"ingested as ready ({char_count} chars) but the body matches a "
                    "dead-link / error-page pattern (e.g. 'broken link') — likely a "
                    'soft-404; verify with source_read (detail="full").'
                )
    except Exception:  # noqa: BLE001 - sanity check must never break a wait
        return None
    return None
