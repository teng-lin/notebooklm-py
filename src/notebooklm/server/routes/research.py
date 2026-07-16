"""Deep-research routes — ``/v1/notebooks/{id}/research`` start / status / cancel / import.

Thin adapters over the research surface, following the MCP ``research_*`` tools'
**split-tool** shape (``mcp/tools/research.py``) rather than the CLI-shaped
``_app.source_research`` bundle (a start→wait→import workflow with an injected
importer that does not decompose into four independent HTTP routes):

* ``POST   .../research``               → ``client.research.start`` (202).
* ``GET    .../research/{run_id}``      → ``_app.research.poll_and_classify``.
* ``DELETE .../research/{run_id}``      → ``_app.research.cancel_research``.
* ``POST   .../research/{run_id}/import`` → guard the poll, then
  ``client.research.import_sources``.

**The ``run_id`` land mine.** ``client.research.start`` returns BOTH a
``task_id`` and (for **deep** mode) a ``report_id``; poll / cancel / import must
key off the ``report_id`` for a deep run (deep's ``task_id`` is a sessionId that
``poll`` reports as ``not_found`` and ``cancel`` silently no-ops). So the 202
start response surfaces an unambiguous ``poll_id`` — the ``report_id`` for a
**deep** run and the ``task_id`` for a **fast** run (gated on ``mode``
explicitly, NOT a ``report_id or task_id`` fallback that could emit a deep
``task_id`` that polls as ``not_found``; a deep start missing its ``report_id``
fails loud instead). The ``{run_id}`` path segment on the status / cancel /
import routes IS that ``poll_id`` — forwarded verbatim as the poll/cancel/import
discriminator. A caller that pins ``poll_id`` is correct for both modes.

This module imports NO ``click`` / ``rich`` / ``cli``.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ..._app import research as research_core
from ..._app.serialize import to_jsonable
from ...client import NotebookLMClient
from ...exceptions import DecodingError, ValidationError
from .._context import get_client, get_pending, limit_research
from .._pending import PendingRegistry

__all__ = ["router"]

router = APIRouter(prefix="/notebooks/{notebook_id}/research", tags=["research"])

ClientDep = Annotated[NotebookLMClient, Depends(get_client)]
PendingDep = Annotated[PendingRegistry, Depends(get_pending)]


class ResearchStartBody(BaseModel):
    """Request body for starting a research session."""

    query: str
    source: Literal["web", "drive"] = "web"
    mode: Literal["fast", "deep"] = "fast"


@router.post("", status_code=202, dependencies=[Depends(limit_research)])
async def start_research(
    notebook_id: str, body: ResearchStartBody, client: ClientDep
) -> dict[str, Any]:
    """Start a research session (non-blocking, returns 202).

    ``mode`` is ``fast`` (default) or ``deep``; ``source`` is ``web`` (default)
    or ``drive``. Deep research is web-only (the ``drive`` + ``deep`` combination
    is rejected with 400).

    The response carries ``poll_id`` — the unambiguous id to hand to the status /
    cancel / import routes as ``{run_id}``. For a deep run this is the
    ``report_id`` (NOT the ``task_id``, which is a sessionId the poll cannot see);
    for a fast run it is the ``task_id``.
    """
    # ``deep`` is web-only — the independent Literals cannot express this
    # cross-field rule, so reject it here (mirrors the MCP ``research_start``
    # tool). ``client.research.start`` also validates this, but catching it up
    # front keeps the message action-appropriate.
    if body.source == "drive" and body.mode == "deep":
        raise ValidationError("mode 'deep' is web-only; use source 'web' for deep research")
    result = await client.research.start(notebook_id, body.query, body.source, body.mode)
    # Poll/cancel/import discriminator contract, gated on mode EXPLICITLY (not a
    # ``report_id or task_id`` fallback): a DEEP run keys off ``report_id`` — its
    # ``task_id`` is a sessionId that ``poll`` reports as ``not_found`` and
    # ``cancel`` silently no-ops — while a FAST run keys off ``task_id``. A deep
    # start that returns no ``report_id`` cannot form a pollable id, so fail loud
    # rather than emit a ``task_id`` that will immediately poll as ``not_found``.
    if body.mode == "deep":
        if not result.report_id:
            raise DecodingError(
                "Deep research start returned no report_id; cannot form a pollable run "
                "id (the deep task_id is a sessionId the poll/cancel cannot use)."
            )
        poll_id = result.report_id
    else:
        # Fast mode keys off ``task_id``; guard its emptiness the same way deep
        # guards ``report_id`` so a fast start that returns no ``task_id`` fails
        # loud instead of emitting an empty, unpollable ``poll_id``.
        if not result.task_id:
            raise DecodingError(
                "Fast research start returned no task_id; cannot form a pollable run id."
            )
        poll_id = result.task_id
    # The explicit discriminators (``notebook_id`` / ``poll_id``) go AFTER the
    # spread so a ``to_jsonable(result)`` field (e.g. ``task_id`` / ``report_id``)
    # can never clobber the computed ``poll_id``.
    return {**to_jsonable(result), "notebook_id": notebook_id, "poll_id": poll_id}


@router.get("/{run_id}")
async def research_status(notebook_id: str, run_id: str, client: ClientDep) -> dict[str, Any]:
    """Poll a research run's status.

    ``run_id`` is the ``poll_id`` from the start response. Returns ``status``
    (``no_research`` | ``in_progress`` | ``completed`` | ``failed`` |
    ``not_found``), the found ``sources``, and any ``report`` once complete. Poll
    until ``completed``, then ``POST .../{run_id}/import``.
    """
    result = await research_core.poll_and_classify(client, notebook_id, run_id)
    return {
        "notebook_id": notebook_id,
        "run_id": run_id,
        "task_id": result.task_id,
        "kind": result.kind,
        "status": result.status,
        "query": result.query,
        "sources": to_jsonable(result.sources),
        "summary": result.summary,
        "report": result.report,
    }


@router.delete("/{run_id}", dependencies=[Depends(limit_research)])
async def cancel_research(notebook_id: str, run_id: str, client: ClientDep) -> dict[str, Any]:
    """Cancel an in-flight research run (fire-and-forget).

    The server returns nothing to confirm the cancel and does not validate
    ``run_id``, so this always reports ``cancelled: true`` without asserting the
    run existed. Poll ``GET .../{run_id}`` afterward to confirm — a cancelled
    in-progress run surfaces as ``failed``.
    """
    await research_core.cancel_research(client, notebook_id, run_id)
    return {"status": "cancelled", "notebook_id": notebook_id, "run_id": run_id, "cancelled": True}


@router.post("/{run_id}/import", status_code=201, dependencies=[Depends(limit_research)])
async def import_research(
    notebook_id: str, run_id: str, client: ClientDep, pending: PendingDep
) -> dict[str, Any]:
    """Import a completed research run's found sources into the notebook.

    ``run_id`` is the ``poll_id`` from the start response. The run is polled FOR
    THAT id so only its sources are imported. A run that is not found / failed /
    still in progress, or that completed with no sources, is rejected (400) — an
    unfinished or empty run is never imported as a partial success (mirrors the
    MCP ``research_import`` guard).

    Each imported source id is recorded in the pending registry (same provenance
    contract as the ``POST .../sources`` create routes), so a ``GET
    .../sources/{id}`` poll for a just-imported id resolves to a ``200`` pending
    rather than a spurious ``404`` during the not-yet-listable window.
    """
    # Poll FOR THE REQUESTED run and guard every non-importable state before
    # touching ``import_sources``. The same shared helper backs the MCP
    # ``research_import`` tool so the importable-state ladder cannot drift.
    sources = await research_core.poll_sources_for_import(client, notebook_id, run_id)
    # The MCP ``research_import`` tool imports via the timeout-tolerant
    # ``import_sources_with_verification`` (#1920), but this synchronous REST route
    # deliberately stays on the one-shot ``import_sources``: a web request must not
    # block on a multi-minute reconcile-and-retry loop. A REST caller that hits a
    # timeout reconciles by polling ``GET .../sources`` for what actually committed.
    imported = await client.research.import_sources(notebook_id, run_id, sources)
    # Record each new source id in the pending registry so the source poll route
    # can answer 200-pending (not 404) for the not-yet-listable window.
    for item in imported:
        source_id = item.get("id")
        if source_id:
            pending.record(notebook_id, source_id)
    return {
        "status": "imported",
        "notebook_id": notebook_id,
        "run_id": run_id,
        "imported": to_jsonable(imported),
        "sources_found": len(sources),
    }
