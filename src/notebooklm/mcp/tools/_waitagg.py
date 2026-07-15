"""Source-wait outcome aggregation for the source MCP tools.

The shared projection behind ``source_wait`` and ``source_add(wait=True)``: fan a
per-source wait out concurrently (:func:`_wait_all_sources`) and fold the typed
``SourceWaitOutcome`` values onto the unified aggregate wire shape
(:func:`_aggregate_wait_outcomes`) — ``{notebook_id, ok, ready, timed_out,
failed, not_found, *_count, total_count}`` — so every wait mode reports partial
progress with one contract (the ``*_count`` scalars mirror the bucket lengths;
see #1822). READY web-page rows pick up the advisory thin/soft-404 ``warning`` via
:func:`._content_sanity._annotate_thin_warnings`.

Extracted from ``sources.py`` (ADR-0008 module-size budget); reads only
``_app.source_wait`` + ``_app.views`` + ``_content_sanity`` — imports NO
``click`` / ``rich`` / ``cli`` (MCP-layer boundary).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..._app import source_wait as wait_core
from ..._app.views import source_view as _source_view
from ...exceptions import (
    SourceNotFoundError,
    SourceProcessingError,
    SourceTimeoutError,
)
from ._content_sanity import _annotate_thin_warnings

if TYPE_CHECKING:
    from ...client import NotebookLMClient
    from ...types import Source


async def _wait_all_sources(
    client: NotebookLMClient,
    notebook_id: str,
    source_ids: list[str],
    *,
    timeout: float,
    interval: float,
) -> list[wait_core.SourceWaitOutcome]:
    """Wait for many sources, one typed outcome each — via the shared snapshot loop.

    Delegates to :func:`notebooklm._app.source_wait.wait_all_sources`, the single
    implementation the REST route also uses: it polls the whole notebook source
    list ONCE per tick and resolves every pending source against that single
    snapshot (previously one whole-notebook poll per source — O(N^2), #1870),
    maps the three handled ``SourceWait*`` failures to typed outcomes so a
    slow/failed source never discards its siblings' progress, and lets any
    UNEXPECTED escape propagate (it then flows through ``mcp_errors()``).
    """
    return await wait_core.wait_all_sources(
        client, notebook_id, source_ids, timeout=timeout, interval=interval
    )


def _wait_bucket_entry(
    error: SourceNotFoundError | SourceProcessingError | SourceTimeoutError,
) -> dict[str, str]:
    """Project a handled wait failure onto its ``{source_id, error}`` bucket entry."""
    return {"source_id": error.source_id, "error": str(error)}


async def _aggregate_wait_outcomes(
    client: NotebookLMClient,
    notebook_id: str,
    outcomes: list[wait_core.SourceWaitOutcome],
) -> dict[str, Any]:
    """Project per-source wait outcomes onto the unified aggregate wire shape.

    Both ``source_wait`` modes (single source, all sources) — and
    ``source_add(wait=True)`` — share this contract: ready sources are returned
    alongside the ones that timed out / failed / went missing, so the all-sources
    mode reports partial progress instead of discarding the sources that did become
    ready. ``ok`` is ``True`` iff nothing landed in an error bucket.

    READY web-page entries are additionally annotated with a non-blocking
    content-sanity ``warning`` when their indexed text is suspiciously thin (a
    likely dead link / soft-404 / paywall ghost source) — see
    :func:`_annotate_thin_warnings`. The warning is purely advisory: a thin source
    is still READY and the wait is still ``ok``.
    """
    ready: list[dict[str, Any]] = []
    # Pair each ready view with its Source so the thin-content sanity check can
    # read the kind + fetch the body without re-resolving.
    ready_pairs: list[tuple[dict[str, Any], Source]] = []
    timed_out: list[dict[str, str]] = []
    failed: list[dict[str, str]] = []
    not_found: list[dict[str, str]] = []
    for outcome in outcomes:
        if isinstance(outcome, wait_core.SourceWaitReady):
            view = _source_view(outcome.source)
            ready.append(view)
            ready_pairs.append((view, outcome.source))
        elif isinstance(outcome, wait_core.SourceWaitTimeout):
            timed_out.append(_wait_bucket_entry(outcome.error))
        elif isinstance(outcome, wait_core.SourceWaitProcessingError):
            failed.append(_wait_bucket_entry(outcome.error))
        elif isinstance(outcome, wait_core.SourceWaitNotFound):
            not_found.append(_wait_bucket_entry(outcome.error))
        else:  # exhaustive over the closed SourceWaitOutcome union
            # mypy narrows ``outcome`` to ``Never`` here; a future outcome variant
            # would surface as a type error AND fail loudly at runtime rather than
            # being silently dropped from every bucket.
            raise AssertionError(f"unhandled SourceWaitOutcome: {outcome!r}")
    await _annotate_thin_warnings(client, notebook_id, ready_pairs)
    # Explicit counts alongside the buckets (#1822): clients read simple totals
    # without recomputing ``len()`` on every array. ``total_count`` folds all four
    # buckets, so it equals the number of sources the wait fanned out over.
    ready_count = len(ready)
    timed_out_count = len(timed_out)
    failed_count = len(failed)
    not_found_count = len(not_found)
    return {
        "notebook_id": notebook_id,
        "ok": not (timed_out or failed or not_found),
        "ready": ready,
        "timed_out": timed_out,
        "failed": failed,
        "not_found": not_found,
        "ready_count": ready_count,
        "timed_out_count": timed_out_count,
        "failed_count": failed_count,
        "not_found_count": not_found_count,
        "total_count": ready_count + timed_out_count + failed_count + not_found_count,
    }


# ``_wait_bucket_entry`` is intentionally NOT exported — it is a module-internal
# helper for ``_aggregate_wait_outcomes``; the sibling-imported surface is the two
# functions ``sources.py`` drives.
__all__ = ["_aggregate_wait_outcomes", "_wait_all_sources"]
