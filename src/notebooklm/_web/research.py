"""Research API for NotebookLM web/drive research.

Provides operations for starting research sessions, polling for results,
and importing discovered sources into notebooks.
"""

from __future__ import annotations

import contextlib
import logging
from typing import TYPE_CHECKING, Any

from .._idempotency import (
    bound_operation_journal_entry,
    call_unconfirmed_on_transport_loss,
    mark_unconfirmed,
)
from .._notebook_metadata import NotebookSourceLister
from .._research import BaseResearchAPI, validate_discover
from .._research_import import (
    _WEB_RESEARCH_IMPORT_POLICY,
    _import_research_read_timeout,
    _ResearchImportBatch,
)
from .._runtime.config import (
    AUTO_READ_TIMEOUT,
    DEFAULT_TIMEOUT,
)
from .._types.research import (
    RESEARCH_SOURCE_TYPE_DRIVE,
    RESEARCH_SOURCE_TYPE_WEB,
    ResearchSource,
    ResearchStart,
    ResearchStatus,
    ResearchTask,
)
from ..exceptions import (
    AuthError,
    DecodingError,
    NetworkError,
    RateLimitError,
    ResearchStartUnavailableError,
    RPCError,
    ServerError,
    ValidationError,
)
from ..outcomes import CommitState
from ..rpc import RPCMethod
from ..types import CitedSourceSelection
from .contracts import RpcCaller
from .notebooks import create_default_source_lister
from .rows.research import ImportedSourceRow, ResearchStartRow, unwrap_import_rows
from .rows.research_task import parse_discover_task, parse_research_task_models

if TYPE_CHECKING:
    from .._runtime.call_supervisor import CallSupervisor, OperationLease

__all__ = [
    "CitedSourceSelection",
    "ResearchAPI",
    "ResearchSource",
    "ResearchStart",
    "ResearchStatus",
    "ResearchTask",
    "WebResearchAPI",
]

# Preserve the historical logger key across the whole-module move.
logger = logging.getLogger("notebooklm._research")


def _is_deep_start_null_result_error(exc: RPCError) -> bool:
    method_id = RPCMethod.START_DEEP_RESEARCH.value
    # The decoder raises one of two stable messages for a wrb.fr null payload,
    # with or without an attached status code (see ``_web/wire/decoder.py``). We match
    # on those stable phrases rather than the obfuscated method id / raw status
    # code, which the decoder deliberately keeps OUT of the human-readable
    # message (#1921). If the wording drifts, fall through and re-raise the
    # original RPCError rather than overclassifying unrelated failures.
    null_result_markers = ("rejected this request", "returned an empty result")
    return (
        exc.method_id == method_id
        and method_id in exc.found_ids
        and any(marker in str(exc).lower() for marker in null_result_markers)
    )


class WebResearchAPI(BaseResearchAPI):
    """Operations for research sessions (web/drive search).

    Provides methods for starting research, polling for results, and
    importing discovered sources into notebooks.

    Usage:
        async with NotebookLMClient.from_storage() as client:
            # Start research
            task = await client.research.start(notebook_id, "quantum computing")

            # Poll for results (typed attribute access; ``== "completed"``
            # still works because ResearchStatus is a str enum)
            result = await client.research.poll(notebook_id)
            if result.status == "completed":
                # Import selected sources
                imported = await client.research.import_sources(
                    notebook_id, task.task_id, result.sources[:5]
                )
    """

    _import_policy = _WEB_RESEARCH_IMPORT_POLICY

    def _operation_scope(
        self, label: str
    ) -> contextlib.AbstractAsyncContextManager[OperationLease]:
        """Keep neutral research workflows under the Web supervisor."""
        return self._supervisor.operation_scope(label)

    def __init__(
        self,
        rpc: RpcCaller,
        *,
        supervisor: CallSupervisor,
        source_lister: NotebookSourceLister | None = None,
        base_timeout: float | None = DEFAULT_TIMEOUT,
        import_research_timeout: float | None = AUTO_READ_TIMEOUT,
    ):
        """Initialize the research API.

        Args:
            rpc: RPC dispatch surface (typically the shared client session).
            supervisor: Owning client's call supervisor. Standalone construction
                must supply a real supervisor so multi-call workflows participate
                in lifecycle admission and shutdown fencing.
            base_timeout: The owning client's configured ``timeout=``. The
                batch-scaled IMPORT_RESEARCH window is floored at it so a
                caller's larger explicit budget is never silently shortened
                (#2205). Standalone ``WebResearchAPI(rpc, supervisor=supervisor)``
                keeps the historical behavior via the shared 30 s default.
            import_research_timeout: Per-attempt read window for
                IMPORT_RESEARCH, read exactly like ``chat_timeout``: unset
                (default) keeps the batch-scaled, ``base_timeout``-floored
                window; a value replaces both; ``None`` inherits
                ``base_timeout`` verbatim.
            source_lister: Optional :class:`NotebookSourceLister` used by
                :meth:`import_sources_with_verification` to snapshot baseline
                source IDs before the import call and probe sources on
                timeout. When omitted, a default lister is built from
                ``rpc`` — mirrors the ``WebNotebooksAPI`` wiring pattern and
                avoids a cross-API dependency.
        """
        self._rpc = rpc
        self._supervisor = supervisor
        super().__init__(
            source_lister=source_lister or create_default_source_lister(self._rpc),
            base_timeout=base_timeout,
            import_research_timeout=import_research_timeout,
        )

    async def _rpc_call(
        self,
        method: RPCMethod,
        params: list[Any],
        source_path: str = "/",
        allow_null: bool = False,
        _is_retry: bool = False,
        *,
        disable_internal_retries: bool = False,
        operation_variant: str | None = None,
    ) -> Any:
        """Delegate through the current RPC caller for late-bound overrides.

        Mirrors :meth:`WebNotebooksAPI._rpc_call` so direct WebResearchAPI RPC paths
        pick up post-construction changes to the underlying caller's
        ``rpc_call`` method (advanced tests / instrumentation).
        """
        return await self._rpc.rpc_call(
            method,
            params,
            source_path=source_path,
            allow_null=allow_null,
            _is_retry=_is_retry,
            disable_internal_retries=disable_internal_retries,
            operation_variant=operation_variant,
        )

    @staticmethod
    def _build_report_import_entry(title: str, markdown: str) -> list[Any]:
        """Build the special deep-research report entry used by IMPORT_RESEARCH."""
        return [None, [title, markdown], None, 3, None, None, None, None, None, None, 3]

    @staticmethod
    def _build_web_import_entry(url: str, title: str) -> list[Any]:
        """Build a standard web-source import entry used by IMPORT_RESEARCH."""
        return [None, None, [url, title], None, None, None, None, None, None, None, 2]

    async def _poll_task_models(self, notebook_id: str) -> list[ResearchTask]:
        params = [None, None, notebook_id]
        result = await self._rpc.rpc_call(
            RPCMethod.POLL_RESEARCH,
            params,
            source_path=f"/notebook/{notebook_id}",
        )
        return parse_research_task_models(result)

    async def start(
        self,
        notebook_id: str,
        query: str,
        source: str = "web",
        mode: str = "fast",
    ) -> ResearchStart:
        """Start a research session.

        Args:
            notebook_id: The notebook ID.
            query: The research query.
            source: "web" or "drive".
            mode: "fast" or "deep" (deep is web-only).

        Returns:
            A :class:`~notebooklm._types.research.ResearchStart` (``task_id`` /
            ``report_id`` / ``notebook_id`` / ``query`` / ``mode``).

        Raises:
            ValidationError: If source/mode combination is invalid.
            ResearchStartUnavailableError: If deep research returns no run.
            DecodingError: On a "couldn't-start" payload — an empty/non-list
                result or a falsey ``task_id`` (no task created); #1342.

        .. versionchanged:: 0.8.0
            **Breaking change:** a "couldn't-start" payload now raises
            :class:`DecodingError` instead of returning ``None``, and the return
            type narrows from ``ResearchStart | None`` to ``ResearchStart``
            (#1342).
        """
        logger.debug(
            "Starting %s research in notebook %s: %s",
            mode,
            notebook_id,
            query[:50] if query else "",
        )
        source_lower = source.lower()
        mode_lower = mode.lower()

        if source_lower not in ("web", "drive"):
            raise ValidationError(f"Invalid source '{source}'. Use 'web' or 'drive'.")
        if mode_lower not in ("fast", "deep"):
            raise ValidationError(f"Invalid mode '{mode}'. Use 'fast' or 'deep'.")
        if mode_lower == "deep" and source_lower == "drive":
            raise ValidationError("Deep Research only supports Web sources.")

        # Same constants the read side decodes ``task_info[1][1]`` with, so the
        # round trip has one definition of the tag rather than two (#1964).
        source_type = (
            RESEARCH_SOURCE_TYPE_WEB if source_lower == "web" else RESEARCH_SOURCE_TYPE_DRIVE
        )

        # The whole research feature is Google's "DiscoverSources" pipeline:
        # fast -> DiscoverSourcesManifold, deep -> DiscoverSourcesAsync,
        # POLL_RESEARCH -> ListDiscoverSourcesJob, IMPORT_RESEARCH ->
        # FinishDiscoverSourcesRun. "Research" is our label for that pipeline.
        if mode_lower == "fast":
            params = [[query, source_type], None, 1, notebook_id]
            rpc_id = RPCMethod.START_FAST_RESEARCH
        else:
            params = [None, [1], [query, source_type], 5, notebook_id]
            rpc_id = RPCMethod.START_DEEP_RESEARCH

        try:
            result = await call_unconfirmed_on_transport_loss(
                lambda: self._rpc.rpc_call(
                    rpc_id,
                    params,
                    source_path=f"/notebook/{notebook_id}",
                ),
                method=rpc_id,
                what=f"research.start ({mode_lower})",
            )
        except (AuthError, RateLimitError, ServerError, NetworkError):
            raise
        except RPCError as exc:
            if mode_lower == "deep" and _is_deep_start_null_result_error(exc):
                raise ResearchStartUnavailableError(
                    notebook_id,
                    mode_lower,
                    method_id=exc.method_id,
                    raw_response=exc.raw_response,
                    rpc_code=exc.rpc_code,
                    found_ids=exc.found_ids,
                ) from exc
            raise

        if result and isinstance(result, list) and len(result) > 0:
            start_row = ResearchStartRow(result)
            task_id = start_row.task_id_raw
            # v0.8.0 (#1342): a falsey ``task_id`` means no task was created —
            # raise (mirrors ``_parse_generation_result``'s missing id).
            if not task_id:
                raise DecodingError(
                    f"research.start returned no task id: {result!r}", method_id=rpc_id.value
                )
            report_id = start_row.report_id
            return ResearchStart(
                task_id=task_id,
                report_id=report_id,
                notebook_id=notebook_id,
                query=query,
                mode=mode_lower,
            )
        # v0.8.0 (#1342): an empty / non-list payload is couldn't-start — raise.
        raise DecodingError(
            "research.start returned an empty / non-list payload", method_id=rpc_id.value
        )

    async def discover(
        self,
        notebook_id: str,
        query: str,
        *,
        mode: str = "default",
    ) -> ResearchTask:
        """Discover web sources synchronously (the "Discover sources" dialog call).

        One blocking ``DISCOVER_SOURCES`` round trip (about 8 s live) that
        returns the ranked sources and an overview, instead of the
        :meth:`start` → :meth:`poll` cycle. The backend also records the call
        as a completed job, so the returned ``task_id`` works with
        :meth:`import_sources` and :meth:`cancel` exactly like a polled run.

        Args:
            notebook_id: The notebook ID.
            query: What to look for. Required for ``default`` / ``raw``;
                ignored (sent empty) for the curious modes.
            mode: ``"default"`` (LLM-ranked search), ``"raw"`` (plain search),
                ``"curious"`` / ``"curious_raw"`` (the backend picks a topic;
                ``query`` is dropped and the returned task's ``query`` is
                ``""``). Web sources only — the Drive corpus fails
                server-side on this route.

        Returns:
            A completed :class:`~notebooklm._types.research.ResearchTask`
            (``status`` ``COMPLETED``, ``sources``, ``summary`` = the overview,
            ``discovery_mode`` = the mode sent).

        Raises:
            ValidationError: On an unknown ``mode`` or an empty ``query``
                outside the curious modes.
            DecodingError: If the response carries no job id.
        """
        query, _mode_label, discovery_mode = validate_discover(query, mode)
        logger.debug(
            "Discovering sources (%s) in notebook %s: %s",
            _mode_label,
            notebook_id,
            query[:50] if query else "",
        )
        # Same request message as START_FAST_RESEARCH (``DiscoverSourcesRequest``):
        # [DiscoveryContext{context, corpus}, RequestContext, DiscoveryMode, project_id].
        params = [[query, RESEARCH_SOURCE_TYPE_WEB], None, int(discovery_mode), notebook_id]
        try:
            result = await self._rpc_call(
                RPCMethod.DISCOVER_SOURCES,
                params,
                source_path=f"/notebook/{notebook_id}",
            )
        except (NetworkError, ServerError) as exc:
            # The request may have reached the server and recorded a job
            # before its response was lost; internal retries are already off
            # (NON_IDEMPOTENT_NO_RETRY), and the marker stops callers from
            # blindly re-running a quota-bearing search. Same outcome the
            # Android adapter's ``call_unconfirmed_on_transport_loss`` gives.
            raise mark_unconfirmed(exc) from None
        try:
            return parse_discover_task(result, query=query, discovery_mode=discovery_mode)
        except DecodingError as error:
            # The call already ran (and recorded a job) — a payload we cannot
            # read is an unconfirmed write, same as the Android adapter.
            raise mark_unconfirmed(error) from None

    async def poll(
        self,
        notebook_id: str,
        task_id: str | None = None,
    ) -> ResearchTask:
        """Poll for research results.

        Args:
            notebook_id: The notebook ID.
            task_id: Optional discriminator selecting a specific research task
                when more than one is in flight against the same notebook.
                When set, the returned ``task_id`` / ``status`` / ``query`` /
                ``sources`` / ``summary`` / ``report`` fields describe the
                matched task, and ``tasks`` contains only that task. When
                ``None`` and two or more tasks are in flight, the selection is
                ambiguous and an
                :class:`~notebooklm.exceptions.AmbiguousResearchTaskError` is
                raised — pass the ``task_id`` from :meth:`start` to select
                explicitly. A single in-flight task is returned silently.

        .. versionchanged:: 0.8.0
            ``task_id=None`` with two or more in-flight tasks now raises
            ``AmbiguousResearchTaskError`` instead of warning and returning the
            latest task (signature unchanged; single task still returned).

        Returns:
            A :class:`~notebooklm._types.research.ResearchTask` for the selected
            task. Use attribute access:
            - ``task.task_id``: task/report identifier for the selected task
            - ``task.status``: a :class:`~notebooklm._types.research.ResearchStatus`
              (``IN_PROGRESS`` / ``COMPLETED`` / ``FAILED`` / ``NO_RESEARCH`` /
              ``NOT_FOUND``); equals the historical strings
            - ``task.query``: original research query text
            - ``task.sources``: tuple of ``ResearchSource`` (each exposes ``url``,
              ``title``, ``result_type``, ``research_task_id``, ``report_markdown``,
              ``source_ordinal``)
            - ``task.summary``: summary text when present
            - ``task.report``: extracted deep-research report markdown, if present
            - ``task.tasks``: all parsed research tasks visible at this poll
              (filtered to the matched task when ``task_id`` is set)

            Use attribute access (``result.status``).

            When a non-empty ``task_id`` is supplied but no in-flight task
            matches, the return is ``ResearchTask.not_found(task_id)`` (status
            ``NOT_FOUND``, empty ``tasks``) — the *poll-observed absence* of that
            task (a typed lifecycle sentinel, not a raise; ADR-0019 Rule 4),
            distinct from the unfiltered empty poll, which stays ``NO_RESEARCH``.
        """
        logger.debug("Polling research status for notebook %s", notebook_id)
        parsed_tasks = self._select_polled_tasks(
            await self._poll_task_models(notebook_id),
            notebook_id=notebook_id,
            task_id=task_id,
            # Ambiguity raise applies only to the unfiltered (task_id is None)
            # path; a pinned discriminator filters before the raise. Matches
            # wait_for_completion.
            raise_on_ambiguous=task_id is None,
        )

        if parsed_tasks:
            # ``parsed_tasks`` is a typed ``list[ResearchTask]``; the unpack avoids
            # a ``name[int]`` positional read on a decoded payload.
            first_task, *_ = parsed_tasks
            return self._public_poll_result(first_task, parsed_tasks)

        # A pinned ``task_id`` that matched nothing is a poll-observed absence —
        # a typed ``NOT_FOUND`` sentinel carrying the id. A falsy ``task_id``
        # (``None`` or empty string) is no discriminator, so it stays
        # ``NO_RESEARCH`` and preserves the legacy empty-poll shape (ADR-0019
        # Rule 4, #1346).
        if task_id:
            return ResearchTask.not_found(task_id)

        return ResearchTask.empty()

    def _wait_observed_status(self, result: ResearchTask) -> ResearchStatus:
        """Preserve Web wait's pre-neutralization pinned-absence status."""
        if result.status is ResearchStatus.NOT_FOUND:
            return ResearchStatus.NO_RESEARCH
        return result.status

    async def cancel(self, notebook_id: str, run_id: str) -> None:
        """Cancel an in-flight research (DiscoverSources) run.

        Fire-and-forget. An IN_PROGRESS run transitions to a terminal
        ``FAILED`` state shortly after this call; cancelling an
        already-terminal run is a silent no-op.

        Args:
            notebook_id: Routing context only (sets the request ``source-path``).
                **Not a scoping or authorization boundary**: the server keys the
                cancel solely on ``run_id`` — live-verified that a valid
                ``run_id`` is cancelled even when ``notebook_id`` names a
                different / non-existent notebook (or is empty). Pass the run's
                real notebook for correct routing, but do not rely on it to
                prevent cancelling a run from the "wrong" notebook.
            run_id: The **poll-level** run id — i.e. ``task.task_id`` from
                :meth:`poll` (equivalently ``ResearchTask.task_id``). For a
                **deep** research run started via :meth:`start`, this is the
                ``report_id`` returned by ``start`` — live-verified: deep's
                ``start().task_id`` is a *sessionId* that :meth:`poll` reports as
                ``NOT_FOUND``, and cancelling with it is a silent no-op (the run
                keeps running); only ``report_id`` actually stops a deep run. For
                a **fast** run it is ``start().task_id`` (fast returns no
                ``report_id``). When in doubt, pass the ``task_id`` surfaced by
                :meth:`poll` — for both modes that is the value the server
                accepts.

        Returns:
            ``None``. This is **fire-and-forget**: the server returns an empty
            payload (``[]``) unconditionally and does **not** validate ``run_id``
            (an unknown / garbage id also yields ``[]``), so the response carries
            no success signal and this method never raises on an unknown id. The
            only way to confirm a cancel took effect is to :meth:`poll`
            afterward — live-verified that a cancelled IN_PROGRESS run surfaces
            as ``FAILED`` within a few seconds, and that re-cancelling an
            already-terminal run is a silent no-op.
        """
        logger.debug("Cancelling research run %s in notebook %s", run_id, notebook_id)
        # Field 3 carries the run id; the optional field-1 client context is
        # omitted to match ``_poll_task_models`` (``[None, None, <id>]``). Routed
        # through ``self._rpc_call`` so a post-construction override of the RPC
        # caller (advanced tests / instrumentation) is honoured.
        await self._rpc_call(
            RPCMethod.CANCEL_RESEARCH,
            [None, None, run_id],
            source_path=f"/notebook/{notebook_id}",
        )

    async def _send_import(
        self,
        notebook_id: str,
        batch: _ResearchImportBatch,
        *,
        _remaining_budget: float | None,
    ) -> list[dict[str, str]]:
        journal_entry = bound_operation_journal_entry()
        source_array = [
            self._build_report_import_entry(item.source.title, item.source.report_markdown)
            if item.kind == "report"
            else self._build_web_import_entry(item.source.url, item.source.title)
            for item in batch.items
        ]

        result = await self._rpc.rpc_call(
            RPCMethod.IMPORT_RESEARCH,
            [None, [1], batch.task_id, notebook_id, source_array],
            source_path=f"/notebook/{notebook_id}",
            read_timeout=_import_research_read_timeout(
                len(source_array),
                base_timeout=self._base_timeout,
                override=self._import_research_timeout,
                remaining_budget=_remaining_budget,
            ),
        )
        imported = []
        # ``unwrap_import_rows`` centralises the ``[[src1, ...]]`` envelope probe
        # behind the research row adapter; an unrecognised shape → ``[]``.
        for src_data in unwrap_import_rows(result):
            row = ImportedSourceRow(src_data)
            if not row.is_well_formed:
                continue
            # An absent / non-list id envelope legitimately means "skip" (id None).
            src_id = row.source_id
            if src_id:
                imported.append({"id": src_id, "title": row.title_slot})

        if journal_entry is not None:
            journal_entry.record(
                CommitState.CONFIRMED,
                "decoded research import",
                known_resource_ids=tuple(item["id"] for item in imported),
            )
        return imported


# Backward-compatible private Web-module spelling. Composition imports the
# explicit backend class; existing direct imports keep resolving to the Web
# implementation without making the neutral base import this module.
ResearchAPI = WebResearchAPI
