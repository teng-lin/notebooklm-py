"""Research MCP tools.

Thin adapters over the research surface:

* ``research_start`` calls ``client.research.start`` directly (web/drive source,
  fast/deep mode) and returns the started task. The neutral
  ``_app.source_research`` core bundles a CLI-shaped start→wait→import workflow
  (rich-coupled importer injection, flag validation); the MCP tool exposes the
  three steps as separate, agent-pollable tools instead, so it drives the client
  API directly.
* ``research_status`` drives the neutral ``_app.research.poll_and_classify`` core
  (a single non-blocking poll classified into render fields).
* ``research_import`` polls the notebook's completed research, then imports its
  sources via ``client.research.import_sources``.

Although the design sketch lists ``research_start(query, …)`` without a notebook
argument, ``client.research.start`` is notebook-scoped (it needs a
``notebook_id``), so the tool takes a ``notebook`` reference — a deliberate
follow-the-code accommodation (the design also routes name/id resolution through
the notebook list).

This module imports NO ``click`` / ``rich`` / ``cli``.
"""

from __future__ import annotations

from typing import Any, Literal

from fastmcp import Context

from ..._app import research as research_core
from ..._app.serialize import to_jsonable
from ...exceptions import ValidationError
from .._confirm import READ_ONLY
from .._context import get_client
from .._errors import mcp_errors
from .._resolve import resolve_notebook


def register(mcp: Any) -> None:
    """Register the research tools on ``mcp``."""

    @mcp.tool
    async def research_start(
        ctx: Context,
        notebook: str,
        query: str,
        source: Literal["web", "drive"] = "web",
        mode: Literal["fast", "deep"] = "fast",
    ) -> dict[str, Any]:
        """Start a research session in a notebook. Accepts a notebook name or ID.

        Non-blocking: returns the started task; poll ``research_status(notebook)``
        until it reports ``completed``, then ``research_import(notebook, task_id)``
        to add the found sources.

        ``source`` is ``web`` (default) or ``drive``. ``mode`` is ``fast``
        (default) or ``deep`` (deep is web-only).
        """
        client = get_client(ctx)
        with mcp_errors():
            # ``deep`` mode is web-only — reject the invalid combination at the tool
            # boundary (the independent Literals can't express this cross-field rule).
            if source == "drive" and mode == "deep":
                raise ValidationError("mode 'deep' is web-only; use source 'web' for deep research")
            nb_id = await resolve_notebook(client, notebook)
            result = await client.research.start(nb_id, query, source, mode)
            return {"notebook_id": nb_id, **to_jsonable(result)}

    @mcp.tool(annotations=READ_ONLY)
    async def research_status(
        ctx: Context, notebook: str, task_id: str | None = None
    ) -> dict[str, Any]:
        """Check a notebook's research status. Accepts a notebook name or ID.

        Returns ``status`` (no_research|in_progress|completed|not_found), the
        polled ``task_id``, plus the found ``sources`` and any ``report`` once
        complete. Poll until ``completed``, then pass the returned ``task_id`` to
        ``research_import``.

        ``task_id`` (optional) pins a specific task when several research tasks
        are in flight in the notebook — pass the value from ``research_start``.
        Omit it for a single in-flight task; when omitted with two or more tasks
        running, the poll is ambiguous and errors (pass ``task_id`` to select).
        A pinned ``task_id`` that is not among the polled tasks reports
        ``status="not_found"``.
        """
        client = get_client(ctx)
        with mcp_errors():
            nb_id = await resolve_notebook(client, notebook)
            result = await research_core.poll_and_classify(client, nb_id, task_id)
            return {
                "notebook_id": nb_id,
                "task_id": result.task_id,
                "kind": result.kind,
                "status": result.status,
                "query": result.query,
                "sources": to_jsonable(result.sources),
                "summary": result.summary,
                "report": result.report,
            }

    @mcp.tool
    async def research_cancel(ctx: Context, notebook: str, run_id: str) -> dict[str, Any]:
        """Cancel an in-flight research run in a notebook.

        Accepts a notebook name or ID and the ``run_id`` to cancel: pass the
        ``task_id`` reported by ``research_status``. (For a **deep** run that is
        the ``report_id`` returned by ``research_start``, NOT its ``task_id``,
        which is a sessionId — so prefer the ``research_status`` value to avoid
        the trap.)

        Fire-and-forget: the server returns nothing to confirm the cancel and
        does not validate ``run_id``, so this always reports
        ``{"cancelled": true}`` without asserting the run existed. Poll
        ``research_status`` afterward to confirm — a cancelled in-progress run
        surfaces as ``failed``.

        The ``notebook`` is routing context only, not a scoping boundary: the
        server keys the cancel on ``run_id`` alone, so a valid ``run_id`` is
        cancelled regardless of which notebook is named.
        """
        client = get_client(ctx)
        with mcp_errors():
            nb_id = await resolve_notebook(client, notebook)
            await client.research.cancel(nb_id, run_id)
            return {
                "status": "cancelled",
                "notebook_id": nb_id,
                "run_id": run_id,
                "cancelled": True,
            }

    @mcp.tool
    async def research_import(ctx: Context, notebook: str, task_id: str) -> dict[str, Any]:
        """Import a completed research task's sources into the notebook.

        Accepts a notebook name or ID and the ``task_id`` from ``research_start``
        / ``research_status``.

        The supplied ``task_id`` is the task discriminator: the notebook is
        polled FOR THAT TASK so only its found sources are imported — never the
        notebook's current (possibly different) research task's sources. If the
        requested task is not among the notebook's polled tasks, the import
        fails cleanly (``not_found``) rather than silently importing the wrong
        task's sources. Returns the imported sources (verify with ``source_list``).
        """
        client = get_client(ctx)
        with mcp_errors():
            nb_id = await resolve_notebook(client, notebook)
            # Poll FOR THE REQUESTED task so the polled sources belong to it.
            # ``poll`` returns the typed ``NOT_FOUND`` sentinel (status
            # ``not_found``) when the pinned task is not among the polled
            # results — guard against that here so we never fall back to
            # importing whatever the notebook's current task happens to be.
            status = await research_core.poll_and_classify(client, nb_id, task_id)
            if status.status == "not_found":
                raise ValidationError(
                    f"Research task {task_id!r} is not among notebook {nb_id}'s "
                    "research tasks; nothing to import. Check research_status."
                )
            # Only a COMPLETED task has a final source set. Importing an
            # in_progress/no_research/failed snapshot would import a partial/empty
            # set as a "success" — refuse with an action-appropriate message.
            if status.status == "failed":
                raise ValidationError(
                    f"Research task {task_id!r} failed; it will not complete — "
                    "start a new research session rather than polling."
                )
            if status.status != "completed":
                raise ValidationError(
                    f"Research task {task_id!r} is not complete (status "
                    f"{status.status!r}); poll research_status until 'completed' "
                    "before importing."
                )
            if not status.sources:
                raise ValidationError(
                    f"Research task {task_id!r} completed with no sources to import."
                )
            # TOCTOU note: ``import_sources`` imports the sources from THIS
            # ``poll_and_classify`` snapshot rather than re-fetching atomically, so
            # a concurrent/external change to the task between the poll above and
            # the import below could theoretically race. Acceptable here: research
            # tasks are user-driven (no high-frequency concurrent mutation), and
            # the per-source ``task_id`` guard above prevents cross-task wiring —
            # we never import a *different* task's sources.
            imported = await client.research.import_sources(nb_id, task_id, status.sources)
            return {
                "status": "imported",
                "notebook_id": nb_id,
                "task_id": task_id,
                "imported": to_jsonable(imported),
                "sources_found": len(status.sources),
            }
