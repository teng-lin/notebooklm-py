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
* ``research_cancel`` preflights the run via ``poll_and_classify`` and sends the
  fire-and-forget cancel unless the run is already terminal (``completed`` /
  ``failed``); a transiently-absent just-started run (replication lag) is still
  cancelled, and ``cancel_requested`` + ``run_status_before`` report honestly.

One id value threads the whole flow — the ``poll_task_id`` surfaced by
``research_start`` (deep's ``report_id`` / fast's ``task_id``). Pass it to
``research_status`` / ``research_import`` (as ``task_id``) and ``research_cancel``
(as ``run_id``); the per-tool param names differ but the value is the same.

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

        Non-blocking. Carry the returned ``poll_task_id`` into
        ``research_status`` / ``research_import`` / ``research_cancel`` — it is
        the single id that drives polling (the tool resolves deep's ``report_id``
        vs fast's ``task_id`` for you). Poll ``research_status`` until
        ``completed``, then ``research_import`` to add the found sources.

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
            # ``poll_task_id`` is the one id status/import/cancel drive off, chosen
            # by mode (NOT ``report_id or task_id`` — that would wrongly pick a
            # fast run's ``report_id`` if the backend ever set one). Deep runs poll
            # under ``report_id`` (its ``task_id`` is an unpollable sessionId), so a
            # deep run without a ``report_id`` is unpollable — fail loud rather than
            # hand back the sessionId trap. Fast runs poll under ``task_id``.
            if mode == "deep":
                if not result.report_id:
                    # The run started server-side but has no pollable/cancellable
                    # handle — surface the raw session id so the caller can still
                    # trace/report it (it can't be polled or cancelled).
                    raise ValidationError(
                        f"deep research start returned no report_id (session "
                        f"{result.task_id!r}); this run cannot be polled or "
                        "cancelled — retry"
                    )
                poll_task_id = result.report_id
            else:
                poll_task_id = result.task_id
            # ``poll_task_id`` is placed AFTER the spread so a future
            # ``ResearchStart`` field can never clobber it.
            return {"notebook_id": nb_id, **to_jsonable(result), "poll_task_id": poll_task_id}

    @mcp.tool(annotations=READ_ONLY)
    async def research_status(
        ctx: Context,
        notebook: str,
        task_id: str | None = None,
        include_report: bool = False,
        report_max_chars: int = 20000,
        source_limit: int | None = None,
        source_offset: int = 0,
    ) -> dict[str, Any]:
        """Check a notebook's research status. Accepts a notebook name or ID.

        Returns ``status`` (no_research|in_progress|completed|failed|not_found),
        the polled ``poll_task_id``, the found ``sources``, and report metadata.
        Poll until ``completed``, then pass ``poll_task_id`` to ``research_import``.

        The large deep ``report`` and each source's ``report_markdown`` are
        omitted by default; set ``include_report=True`` (optionally
        ``report_max_chars``) to include them, truncated to that length.
        ``report_char_count`` is the full size; ``report_truncated`` is true
        whenever the returned ``report`` omits text (truncated OR omitted).
        ``source_limit`` / ``source_offset`` page ``sources``;
        ``sources_total`` / ``sources_returned`` describe the window.

        ``task_id`` (optional) pins one task when several are in flight — pass a
        ``poll_task_id``. Omit it for a single task; omitting it with two or more
        running errors as ambiguous. An unmatched pin reports ``not_found``.
        """
        client = get_client(ctx)
        with mcp_errors():
            # Validate windowing bounds BEFORE the poll so a bad request never
            # spends a read-only RPC. Reject an explicit empty/whitespace pin too:
            # ``poll`` treats a falsy ``task_id`` as an UNFILTERED poll, so ``""``
            # must not silently degrade into "any task" (``None`` stays the
            # legitimate unfiltered path).
            if report_max_chars < 1:
                raise ValidationError("report_max_chars must be >= 1")
            if source_limit is not None and source_limit < 0:
                raise ValidationError("source_limit must be >= 0")
            if source_offset < 0:
                raise ValidationError("source_offset must be >= 0")
            if task_id is not None and not task_id.strip():
                raise ValidationError(
                    "task_id must be a non-empty id (omit it to poll a single task)"
                )
            # Normalize a padded pin so surrounding whitespace never reaches the
            # backend as a spurious mismatch.
            task_id = task_id.strip() if task_id is not None else None

            nb_id = await resolve_notebook(client, notebook)
            result = await research_core.poll_and_classify(client, nb_id, task_id)

            # Report content lives in TWO places — the top-level ``report`` AND
            # each source's ``report_markdown`` — so BOTH are gated by
            # ``include_report`` or a deep report leaks through the source rows.
            all_sources = to_jsonable(result.sources)
            sources_total = len(all_sources)
            end = None if source_limit is None else source_offset + source_limit
            windowed = all_sources[source_offset:end]
            for src in windowed:
                if "report_markdown" not in src:
                    continue
                if include_report:
                    src["report_markdown"] = src["report_markdown"][:report_max_chars]
                else:
                    del src["report_markdown"]

            report_char_count = len(result.report)
            report = result.report[:report_max_chars] if include_report else None
            # ``report_truncated`` means "the returned ``report`` does not contain
            # the full text" — true both when ``include_report`` truncated it AND
            # when it was omitted (``report=None``) yet a report exists. So a caller
            # can trust the flag without special-casing the omitted path.
            report_truncated = len(report or "") < report_char_count

            return {
                "notebook_id": nb_id,
                "task_id": result.task_id,
                "poll_task_id": result.task_id,
                "kind": result.kind,
                "status": result.status,
                "query": result.query,
                "sources": windowed,
                "sources_total": sources_total,
                "sources_returned": len(windowed),
                "sources_offset": source_offset,
                "summary": result.summary,
                "report": report,
                "report_char_count": report_char_count,
                "report_truncated": report_truncated,
            }

    @mcp.tool
    async def research_cancel(ctx: Context, notebook: str, run_id: str) -> dict[str, Any]:
        """Cancel an in-flight research run in a notebook.

        Accepts a notebook name or ID and the ``run_id`` to cancel — pass a
        ``poll_task_id`` from ``research_start`` / ``research_status``.

        Preflights the named notebook and sends the cancel unless the run is
        already TERMINAL (``completed`` / ``failed``), which returns
        ``cancel_requested: false`` with the observed ``status`` and no RPC.
        Otherwise it cancels and returns ``cancel_requested: true`` with
        ``run_status_before`` (the preflight status). A run polled right after
        ``research_start`` can transiently read ``not_found`` / ``no_research``
        (replication lag), so those are cancelled too rather than silently
        suppressed — the RPC is a harmless no-op for a genuinely unknown id. The
        cancel is fire-and-forget; poll ``research_status`` afterward to confirm
        (a cancelled in-progress run surfaces as ``failed``).
        """
        client = get_client(ctx)
        with mcp_errors():
            # Reject an empty/whitespace run_id BEFORE preflight: ``poll`` treats
            # a falsy task_id as an UNFILTERED poll, so ``run_id=""`` would match
            # some other in-flight task and cancel the wrong run.
            if not run_id or not run_id.strip():
                raise ValidationError("run_id is required to cancel a research run")
            run_id = run_id.strip()
            nb_id = await resolve_notebook(client, notebook)
            # Preflight (``run_id`` as the discriminator → typed NOT_FOUND
            # sentinel, never raises). Only an already-TERMINAL run
            # (``completed`` / ``failed``) is left alone — those states are stable,
            # so cancelling is a meaningless no-op we can honestly skip.
            status = await research_core.poll_and_classify(client, nb_id, run_id)
            if status.status in ("completed", "failed"):
                return {
                    "status": status.status,
                    "notebook_id": nb_id,
                    "run_id": run_id,
                    "cancel_requested": False,
                }
            # Otherwise send the fire-and-forget cancel. For ``in_progress`` this
            # is the obvious path; for ``not_found`` / ``no_research`` we STILL send
            # it, because a poll immediately after ``research_start`` can transiently
            # miss a valid just-started run (replication lag — the research wait path
            # treats this as lag, not terminal absence). Suppressing the cancel here
            # would silently leave that run running; the RPC is a harmless no-op for
            # a genuinely unknown id. ``run_status_before`` surfaces what the
            # preflight actually observed so a caller can tell a confirmed-running
            # cancel from an unconfirmed (lag-or-unknown) one.
            await client.research.cancel(nb_id, run_id)
            return {
                "status": "cancel_requested",
                "notebook_id": nb_id,
                "run_id": run_id,
                "cancel_requested": True,
                "run_status_before": status.status,
            }

    @mcp.tool
    async def research_import(ctx: Context, notebook: str, task_id: str) -> dict[str, Any]:
        """Import a completed research task's sources into the notebook.

        Accepts a notebook name or ID and the ``task_id`` to import — pass the
        ``poll_task_id`` from ``research_start`` / ``research_status``.

        The supplied ``task_id`` is the task discriminator: the notebook is
        polled FOR THAT TASK so only its found sources are imported — never the
        notebook's current (possibly different) research task's sources. If the
        requested task is not among the notebook's polled tasks, the import
        fails cleanly (``not_found``) rather than silently importing the wrong
        task's sources. Returns the imported sources (verify with ``source_list``).
        """
        client = get_client(ctx)
        with mcp_errors():
            # Reject an empty/whitespace task_id: ``poll`` treats a falsy id as an
            # UNFILTERED poll, which would let an empty pin import whatever task
            # the notebook happens to have in flight (cross-wire).
            if not task_id or not task_id.strip():
                raise ValidationError("task_id is required to import a research task")
            task_id = task_id.strip()
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
