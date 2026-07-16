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
  sources via the timeout-tolerant ``client.research.import_sources_with_verification``
  (optionally narrowed by ``cited_only`` / bounded by ``max_sources``).
* ``research_cancel`` preflights the run via ``poll_and_classify`` and sends the
  fire-and-forget cancel unless the run is already terminal (``completed`` /
  ``failed``); a transiently-absent just-started run (replication lag) is still
  cancelled, and ``cancel_requested`` + ``run_status_before`` report honestly.

One id value threads the whole flow — the ``poll_task_id`` surfaced by
``research_start`` (deep's ``report_id`` / fast's ``task_id``). Every downstream
tool now accepts it under the SAME name, ``poll_task_id`` (issue #1789), so the
value copied from one tool's output pastes verbatim into the next. The original
per-tool names — ``research_status``/``research_import``'s ``task_id`` and
``research_cancel``'s ``run_id`` — remain accepted as deprecated aliases for one
release (they emit a ``DeprecationWarning`` and a ``deprecation`` note in the
result); see ``docs/deprecations.md``.

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
from ..._deprecation import warn_deprecated
from ...exceptions import ValidationError
from ...research import select_cited_sources
from .._confirm import READ_ONLY
from .._context import get_cancelled_research, get_client
from .._errors import mcp_errors
from .._resolve import resolve_notebook

# One release of overlap: the ``task_id`` / ``run_id`` aliases are accepted
# through the v0.8.0 line and removed in v0.9.0 (issue #1789). Named once so the
# warning text and the ``docs/deprecations.md`` row stay in lock-step.
_POLL_ID_ALIAS_REMOVAL = "0.9.0"


def _resolve_poll_task_id(
    tool: str,
    old_name: str,
    poll_task_id: str | None,
    alias: str | None,
) -> tuple[str | None, str | None]:
    """Fold a deprecated id alias into the canonical ``poll_task_id`` (issue #1789).

    Returns ``(resolved, deprecation_note)``. ``resolved`` is ``poll_task_id``
    when supplied, else the ``alias`` value. When only the alias was used with a
    substantive value it emits a gated ``DeprecationWarning`` (via
    :func:`warn_deprecated`) and returns a caller-visible ``deprecation_note``
    (else ``None``) so the MCP client — which never sees the Python warning — is
    nudged toward the new name. Passing both with different (stripped) values is
    rejected as ``ValidationError``; passing both with the same value is allowed
    (the canonical wins, no warning). A blank/whitespace-only alias is handed back
    unwarned so the caller's own empty-id guard rejects it — no deprecation signal
    is spent on a value that is about to be refused.
    """
    if poll_task_id is not None and alias is not None:
        if poll_task_id.strip() != alias.strip():
            raise ValidationError(
                f"pass either poll_task_id or the deprecated {old_name} to "
                f"{tool}, not both with different values"
            )
        return poll_task_id, None
    if alias is not None and alias.strip():
        # ``stacklevel=4``: warn_deprecated (1) → _resolve_poll_task_id (2) → the
        # tool coroutine (3) → the caller (4), so the warning points past this
        # helper at the tool boundary rather than at the helper's own line.
        warn_deprecated(
            f"{tool}({old_name}=...) is deprecated; pass poll_task_id instead (the same value).",
            removal=_POLL_ID_ALIAS_REMOVAL,
            stacklevel=4,
        )
        note = (
            f"'{old_name}' is deprecated; pass 'poll_task_id' instead (the same "
            f"value). '{old_name}' will be removed in v{_POLL_ID_ALIAS_REMOVAL}."
        )
        return alias, note
    # No canonical value, and the alias (if any) is blank → return whatever was
    # given (``poll_task_id`` is ``None`` here) so the tool's empty-id guard runs.
    return (poll_task_id if poll_task_id is not None else alias), None


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
        ``research_status`` / ``research_import`` / ``research_cancel`` — the
        single id that drives polling (it resolves deep vs fast for you). Poll
        ``research_status`` until ``completed``, then ``research_import`` to add
        the sources.

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
            # Surface only ``poll_task_id`` as the id to carry forward (#1909).
            # The raw ``task_id`` / ``report_id`` are mode-specific internals
            # (deep's ``report_id`` / fast's ``task_id``) — leaking both plus
            # ``poll_task_id`` gave three id fields for one concept, so we drop
            # them from the wire shape. ``poll_task_id`` is placed AFTER the
            # spread so a future ``ResearchStart`` field can never clobber it.
            start_fields = to_jsonable(result)
            start_fields.pop("task_id", None)
            start_fields.pop("report_id", None)
            return {"notebook_id": nb_id, **start_fields, "poll_task_id": poll_task_id}

    @mcp.tool(annotations=READ_ONLY)
    async def research_status(
        ctx: Context,
        notebook: str,
        poll_task_id: str | None = None,
        task_id: str | None = None,
        include_report: bool = False,
        report_max_chars: int = 20000,
        source_limit: int | None = None,
        source_offset: int = 0,
    ) -> dict[str, Any]:
        """Check a notebook's research status. Accepts a notebook name or ID.

        Returns ``status`` (no_research|in_progress|completed|failed|not_found),
        ``poll_task_id``, the ``sources``, and report metadata. Poll until
        ``completed``, then pass ``poll_task_id`` to ``research_import``.

        ``report`` and each source's ``report_markdown`` are omitted by default;
        set ``include_report=True`` (optionally ``report_max_chars``) to include
        them, truncated to that length. ``report_char_count`` is the full size;
        ``report_truncated`` flags an omitted/truncated ``report``.
        ``source_limit`` / ``source_offset`` page ``sources``.

        ``poll_task_id`` (optional) pins one of several in-flight tasks; omit it
        for a single task (ambiguous with two+ running). An unmatched pin reports
        ``not_found``. ``task_id`` is a deprecated alias (removed in v0.9.0).
        """
        client = get_client(ctx)
        with mcp_errors():
            # Fold the deprecated ``task_id`` pin into ``poll_task_id`` (#1789).
            poll_task_id, deprecation = _resolve_poll_task_id(
                "research_status", "task_id", poll_task_id, task_id
            )
            # Validate windowing bounds BEFORE the poll so a bad request never
            # spends a read-only RPC. Reject an explicit empty/whitespace pin too:
            # ``poll`` treats a falsy id as an UNFILTERED poll, so ``""`` must not
            # silently degrade into "any task" (``None`` stays the legitimate
            # unfiltered path).
            if report_max_chars < 1:
                raise ValidationError("report_max_chars must be >= 1")
            if source_limit is not None and source_limit < 0:
                raise ValidationError("source_limit must be >= 0")
            if source_offset < 0:
                raise ValidationError("source_offset must be >= 0")
            if poll_task_id is not None and not poll_task_id.strip():
                raise ValidationError(
                    "poll_task_id must be a non-empty id (omit it to poll a single task)"
                )
            # Normalize a padded pin so surrounding whitespace never reaches the
            # backend as a spurious mismatch.
            poll_task_id = poll_task_id.strip() if poll_task_id is not None else None

            nb_id = await resolve_notebook(client, notebook)
            result = await research_core.poll_and_classify(client, nb_id, poll_task_id)

            # F9 (#1922): a user-cancelled run surfaces as a generic ``failed``
            # with no distinct wire code, so consult the client-side cancel-intent
            # tracker recorded by ``research_cancel``. Match on the pinned id AND
            # the polled task_id (an unfiltered poll resolves the id only in the
            # result), keyed by notebook so ids never cross notebooks. On ANY
            # terminal poll (failed / completed) evict the intent so the
            # process-scoped tracker cannot grow without bound — a later poll of
            # the same terminal run won't be re-annotated, which is acceptable
            # (the agent already saw ``cancelled`` on the first terminal poll).
            cancelled = False
            if result.status in ("failed", "completed"):
                intents = get_cancelled_research(ctx)
                candidates = [(nb_id, result.task_id)]
                if poll_task_id is not None and poll_task_id != result.task_id:
                    candidates.append((nb_id, poll_task_id))
                hit = any(key in intents for key in candidates)
                cancelled = hit and result.status == "failed"
                for key in candidates:
                    intents.discard(key)

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

            payload: dict[str, Any] = {
                "notebook_id": nb_id,
                "task_id": result.task_id,
                "poll_task_id": result.task_id,
                "kind": result.kind,
                "status": result.status,
                # Raw backend status code preserved from the poll (F10, #1922);
                # ``None`` when the poll carried no code. Lets an agent tell a
                # "no matches" failure sub-code from a genuine error.
                "status_code": result.status_code,
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
            # Only annotate a failure known to be user-cancelled (F9, #1922);
            # absence means "not a tracked cancel", so a genuine failure stays
            # un-annotated.
            if cancelled:
                payload["cancelled"] = True
            if deprecation is not None:
                payload["deprecation"] = deprecation
            return payload

    @mcp.tool
    async def research_cancel(
        ctx: Context,
        notebook: str,
        poll_task_id: str | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        """Cancel an in-flight research run in a notebook.

        Accepts a notebook name or ID and the ``poll_task_id`` to cancel — the
        value from ``research_start`` / ``research_status``. ``run_id`` is a
        deprecated alias (removed in v0.9.0).

        Sends the cancel unless the run is already TERMINAL (``completed`` /
        ``failed``), which returns ``cancel_requested: false`` with the observed
        ``status`` and no RPC. Otherwise returns ``cancel_requested: true`` with
        ``run_status_before``; a just-started run reading ``not_found`` /
        ``no_research`` (replication lag) is cancelled too. Fire-and-forget; poll
        ``research_status`` afterward to confirm.
        """
        client = get_client(ctx)
        with mcp_errors():
            # Fold the deprecated ``run_id`` alias into ``poll_task_id`` (#1789).
            poll_task_id, deprecation = _resolve_poll_task_id(
                "research_cancel", "run_id", poll_task_id, run_id
            )
            # Reject an empty/whitespace/absent id BEFORE preflight: ``poll``
            # treats a falsy id as an UNFILTERED poll, so an empty id would match
            # some other in-flight task and cancel the wrong run.
            if not poll_task_id or not poll_task_id.strip():
                raise ValidationError("poll_task_id is required to cancel a research run")
            poll_task_id = poll_task_id.strip()
            nb_id = await resolve_notebook(client, notebook)
            # Preflight (``poll_task_id`` as the discriminator → typed NOT_FOUND
            # sentinel, never raises). Only an already-TERMINAL run
            # (``completed`` / ``failed``) is left alone — those states are stable,
            # so cancelling is a meaningless no-op we can honestly skip.
            status = await research_core.poll_and_classify(client, nb_id, poll_task_id)
            if status.status in ("completed", "failed"):
                result: dict[str, Any] = {
                    "status": status.status,
                    "notebook_id": nb_id,
                    "poll_task_id": poll_task_id,
                    "run_id": poll_task_id,
                    "cancel_requested": False,
                }
                if deprecation is not None:
                    result["deprecation"] = deprecation
                return result
            # Otherwise send the fire-and-forget cancel. For ``in_progress`` this
            # is the obvious path; for ``not_found`` / ``no_research`` we STILL send
            # it, because a poll immediately after ``research_start`` can transiently
            # miss a valid just-started run (replication lag — the research wait path
            # treats this as lag, not terminal absence). Suppressing the cancel here
            # would silently leave that run running; the RPC is a harmless no-op for
            # a genuinely unknown id. ``run_status_before`` surfaces what the
            # preflight actually observed so a caller can tell a confirmed-running
            # cancel from an unconfirmed (lag-or-unknown) one.
            await client.research.cancel(nb_id, poll_task_id)
            # Record the cancel intent (F9, #1922) so a later ``research_status``
            # poll can annotate the resulting generic ``failed`` as ``cancelled``
            # (the backend surfaces a cancelled run as FAILED with no distinct
            # wire code). Keyed by notebook so ids never cross notebooks; the
            # tracker is bounded (evict-on-terminal + hard FIFO cap).
            get_cancelled_research(ctx).record((nb_id, poll_task_id))
            result = {
                "status": "cancel_requested",
                "notebook_id": nb_id,
                "poll_task_id": poll_task_id,
                "run_id": poll_task_id,
                "cancel_requested": True,
                "run_status_before": status.status,
            }
            if deprecation is not None:
                result["deprecation"] = deprecation
            return result

    @mcp.tool
    async def research_import(
        ctx: Context,
        notebook: str,
        poll_task_id: str | None = None,
        task_id: str | None = None,
        max_sources: int | None = None,
        cited_only: bool = False,
    ) -> dict[str, Any]:
        """Import a completed research task's sources into the notebook.

        Accepts a notebook name or ID and the ``poll_task_id`` to import — the
        value from ``research_start`` / ``research_status``. ``task_id`` is a
        deprecated alias (removed in v0.9.0).

        The id pins the task: only its sources import (else ``not_found``).
        Timeout-tolerant: a timed-out import reconciles against what the server
        actually committed. Returns the imported sources (verify with
        ``source_list``).

        ``cited_only`` imports only report-cited sources (all, if none resolve).
        ``max_sources`` caps the count (after ``cited_only``).
        """
        client = get_client(ctx)
        with mcp_errors():
            # Fold the deprecated ``task_id`` alias into ``poll_task_id`` (#1789).
            poll_task_id, deprecation = _resolve_poll_task_id(
                "research_import", "task_id", poll_task_id, task_id
            )
            # Reject an empty/whitespace/absent id: ``poll`` treats a falsy id as
            # an UNFILTERED poll, which would let an empty pin import whatever task
            # the notebook happens to have in flight (cross-wire).
            if not poll_task_id or not poll_task_id.strip():
                raise ValidationError("poll_task_id is required to import a research task")
            # Validate the bound BEFORE the poll so a bad request never spends a
            # read-only RPC (mirrors research_status's up-front bounds checks).
            if max_sources is not None and max_sources < 1:
                raise ValidationError("max_sources must be >= 1 (omit it to import all)")
            poll_task_id = poll_task_id.strip()
            nb_id = await resolve_notebook(client, notebook)
            # Poll FOR THE REQUESTED task (via the shared importable-state guard,
            # which forwards ``poll_task_id`` to ``poll`` as the discriminator) so
            # the polled sources belong to it and every non-importable state
            # (not_found / failed / non-completed / empty) is refused before we
            # import — we never fall back to importing whatever the notebook's
            # current task happens to be. The report is returned alongside the
            # sources so cited-only selection can match citations without a second
            # poll. The same guard backs the REST import route so it cannot drift.
            sources, report = await research_core.poll_importable_research(
                client, nb_id, poll_task_id
            )
            # Selection/bounding, in that order: narrow to report-cited sources
            # first, then cap the count. Both are opt-in; without them every
            # completed source is imported (unchanged default behavior).
            # ``sources_to_import`` widens to ``ResearchSourceInput`` because
            # ``select_cited_sources`` may hand back typed sources, not just dicts.
            sources_to_import: list[Any] = list(sources)
            cited_fallback = False
            if cited_only:
                selection = select_cited_sources(sources_to_import, report)
                sources_to_import = list(selection.sources)
                cited_fallback = selection.used_fallback
            if max_sources is not None:
                sources_to_import = sources_to_import[:max_sources]
            # Import via the timeout-tolerant variant (as the CLI does): the
            # underlying IMPORT_RESEARCH RPC commonly runs >30 s on deep payloads
            # and a one-shot call times out client-side even after the server
            # committed. On timeout this probes sources.list and reconciles against
            # what actually landed instead of raising as if nothing imported.
            #
            # TOCTOU note: the sources come from the poll snapshot above rather
            # than an atomic re-fetch, so a concurrent/external change between poll
            # and import could theoretically race. Acceptable: research tasks are
            # user-driven, and the pinned id prevents cross-task wiring.
            imported = await client.research.import_sources_with_verification(
                nb_id, poll_task_id, sources_to_import
            )
            result: dict[str, Any] = {
                "status": "imported",
                "notebook_id": nb_id,
                "poll_task_id": poll_task_id,
                "task_id": poll_task_id,
                "imported": to_jsonable(imported),
                # ``sources_found`` keeps its cross-surface meaning: the total the
                # research run discovered (pre-narrowing), matching the REST route
                # and CLI. ``sources_selected`` is the post-``cited_only`` /
                # ``max_sources`` count actually handed to the importer, so a
                # caller sees both the found total and how many were imported
                # without a second ``research_status`` poll.
                "sources_found": len(sources),
                "sources_selected": len(sources_to_import),
            }
            if cited_only and cited_fallback:
                result["cited_only_fallback"] = True
            if deprecation is not None:
                result["deprecation"] = deprecation
            return result
