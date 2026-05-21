"""Research API for NotebookLM web/drive research.

Provides operations for starting research sessions, polling for results,
and importing discovered sources into notebooks.
"""

from __future__ import annotations

import asyncio
import logging
import time
import warnings
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit, urlunsplit

from . import research as _research_pub
from ._notebook_metadata import NotebookSourceLister, create_default_source_lister
from ._session_contracts import RpcCaller
from .exceptions import (
    NetworkError,
    ResearchTaskMismatchError,
    RPCError,
    RPCTimeoutError,
    ValidationError,
)
from .rpc import RPCMethod, safe_index
from .types import CitedSourceSelection

if TYPE_CHECKING:
    from .types import Source

__all__ = ["CitedSourceSelection", "ResearchAPI"]

logger = logging.getLogger(__name__)


_RESEARCH_RESULT_TYPE_ALIASES = {
    "web": 1,
    "drive": 2,
    "report": 5,
}


# ---------------------------------------------------------------------------
# IMPORT_RESEARCH timeout-verification helpers
#
# IMPORT_RESEARCH is classified NON_IDEMPOTENT_NO_RETRY in IDEMPOTENCY_REGISTRY
# (see #808): the executor will surface the first 5xx/timeout to the caller
# rather than retry blindly, because the wire protocol has no client-token
# slot and a naive retry duplicates every source. ``ResearchAPI``'s
# verification path sidesteps that constraint by snapshotting baseline
# sources before the call and matching post-call ``sources.list`` URLs
# against the request — disambiguating "server already committed but the
# response was lost" from "request truly failed". These helpers mirror the
# CLI-only logic that originally landed in PR #321 / #327; they live in the
# library now so Python API consumers get the same deep-research fix the
# CLI does (issue #315).
# ---------------------------------------------------------------------------


def _normalize_import_url(url: str) -> str:
    """Lowercase scheme + host and strip a trailing slash for comparison.

    Distinct from ``notebooklm.research.normalize_url`` (used for matching
    URLs cited inside report markdown): this variant drops the URL fragment
    because the server stores fragments stripped, and skips the
    trailing-punctuation strip because these URLs come from a structured
    ``sources.list`` payload rather than free-form markdown.
    """
    parsed = urlsplit(url)
    return urlunsplit(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path.rstrip("/"),
            parsed.query,
            "",
        )
    )


def _source_url_norm(source: dict[str, Any]) -> str | None:
    url = source.get("url")
    if not isinstance(url, str) or not url:
        return None
    return _normalize_import_url(url)


def _requested_urls_norm(sources: list[dict[str, Any]]) -> set[str]:
    return {url for source in sources if (url := _source_url_norm(source))}


def _no_url_entry_count(sources: list[dict[str, Any]]) -> int:
    return sum(1 for source in sources if _source_url_norm(source) is None)


def _imported_source_entry(source: Source) -> dict[str, str]:
    return {"id": source.id, "title": source.title or source.url or ""}


def _merge_imported_sources(
    imported: list[dict[str, str]],
    verified_imported: list[dict[str, str]],
    verified_imported_ids: set[str],
) -> list[dict[str, str]]:
    if not verified_imported:
        return imported
    return [
        *verified_imported,
        *(entry for entry in imported if entry.get("id") not in verified_imported_ids),
    ]


# ---------------------------------------------------------------------------
# Poll-payload extractors
#
# These private helpers name the positional slots of a ``POLL_RESEARCH`` task
# entry so the ``ResearchAPI.poll`` body stays readable when Google shifts
# fields around. Deep numeric indexing is delegated to ``safe_index`` so a
# single drift point (env-flag-controlled) governs whether we soft-warn or
# hard-fail. Each helper returns a sentinel (``None``, ``""``, or empty
# tuple) on shape drift rather than raising, so callers can keep parsing
# the rest of the payload.
#
# Observed shape of a single ``task_data`` entry::
#
#     task_data = [
#         task_id,                            # 0: str
#         task_info = [                       # 1: list
#             _,                              #   0: unused
#             query_info = [query_text, ...], #   1: list of [str, ...]
#             _,                              #   2: unused
#             sources_and_summary = [         #   3: list
#                 sources_data,               #     0: list of source rows
#                 summary,                    #     1: str (optional)
#             ],
#             status_code,                    #   4: int (1=in_progress, 2/6=completed)
#             ...
#         ],
#         ...
#     ]
# ---------------------------------------------------------------------------

_POLL_SOURCE = "_research.poll"
_POLL_METHOD_ID = RPCMethod.POLL_RESEARCH.value


def _extract_task_id(task_data: Any) -> str | None:
    """Return ``task_data[0]`` as a string when present, else ``None``.

    ``task_data`` is expected to be a list whose first element is the
    task/report identifier. Returns ``None`` and logs via ``safe_index`` if
    the entry is shorter than 1 element or the value is not a string.
    """
    value = safe_index(task_data, 0, method_id=_POLL_METHOD_ID, source=_POLL_SOURCE)
    if isinstance(value, str):
        return value
    if value is not None:
        logger.warning(
            "task_data[0] is not a string (method_id=%r, source=%r): %r",
            _POLL_METHOD_ID,
            _POLL_SOURCE,
            type(value).__name__,
        )
    return None


def _extract_task_info(task_data: Any) -> list[Any] | None:
    """Return ``task_data[1]`` as a list when present, else ``None``.

    The ``task_info`` slot carries the per-task metadata: query, sources,
    summary, and status. Returns ``None`` if the entry is too short or the
    value is not a list.
    """
    value = safe_index(task_data, 1, method_id=_POLL_METHOD_ID, source=_POLL_SOURCE)
    if isinstance(value, list):
        return value
    if value is not None:
        logger.warning(
            "task_data[1] is not a list (method_id=%r, source=%r): %r",
            _POLL_METHOD_ID,
            _POLL_SOURCE,
            type(value).__name__,
        )
    return None


def _extract_query_text(task_info: Any) -> str | None:
    """Return ``task_info[1][0]`` as the original query text, else ``None``.

    Returns ``None`` on missing slots or non-string contents.
    """
    value = safe_index(task_info, 1, 0, method_id=_POLL_METHOD_ID, source=_POLL_SOURCE)
    if isinstance(value, str):
        return value
    if value is not None:
        logger.warning(
            "task_info[1][0] is not a string (method_id=%r, source=%r): %r",
            _POLL_METHOD_ID,
            _POLL_SOURCE,
            type(value).__name__,
        )
    return None


def _extract_status_code(task_info: Any) -> int | None:
    """Return ``task_info[4]`` as an int status code, else ``None``.

    Research status codes observed: ``1`` (in progress), ``2`` (completed),
    ``6`` (completed deep-research). Returns ``None`` on shape drift or a
    non-int value (booleans are rejected too).
    """
    value = safe_index(task_info, 4, method_id=_POLL_METHOD_ID, source=_POLL_SOURCE)
    if isinstance(value, bool):
        # bool is a subclass of int; reject explicitly so callers don't get
        # surprising truthy comparisons against status codes 1/2/6.
        logger.warning(
            "task_info[4] is bool, not int (method_id=%r, source=%r)",
            _POLL_METHOD_ID,
            _POLL_SOURCE,
        )
        return None
    if isinstance(value, int):
        return value
    if value is not None:
        logger.warning(
            "task_info[4] is not an int (method_id=%r, source=%r): %r",
            _POLL_METHOD_ID,
            _POLL_SOURCE,
            type(value).__name__,
        )
    return None


def _extract_sources_and_summary(task_info: Any) -> tuple[list[Any], str | None]:
    """Return ``(sources_data, summary)`` from ``task_info[3]``.

    ``sources_data`` is the list of raw source rows (each later parsed by
    ``ResearchAPI.poll``). ``summary`` is the optional summary string.
    Returns ``([], None)`` if the slot is missing, not a list, or empty.
    Returns ``(sources_data, None)`` if no summary string is present.
    """
    bundle = safe_index(task_info, 3, method_id=_POLL_METHOD_ID, source=_POLL_SOURCE)
    if not isinstance(bundle, list) or not bundle:
        if bundle is not None and not isinstance(bundle, list):
            logger.warning(
                "task_info[3] is not a list (method_id=%r, source=%r): %r",
                _POLL_METHOD_ID,
                _POLL_SOURCE,
                type(bundle).__name__,
            )
        return [], None

    sources_data = bundle[0] if isinstance(bundle[0], list) else []
    if bundle[0] is not None and not isinstance(bundle[0], list):
        logger.warning(
            "task_info[3][0] is not a list (method_id=%r, source=%r): %r",
            _POLL_METHOD_ID,
            _POLL_SOURCE,
            type(bundle[0]).__name__,
        )

    summary: str | None = None
    if len(bundle) >= 2 and isinstance(bundle[1], str):
        summary = bundle[1]

    return sources_data, summary


class ResearchAPI:
    """Operations for research sessions (web/drive search).

    Provides methods for starting research, polling for results, and
    importing discovered sources into notebooks.

    Usage:
        async with await NotebookLMClient.from_storage() as client:
            # Start research
            task = await client.research.start(notebook_id, "quantum computing")

            # Poll for results
            result = await client.research.poll(notebook_id)
            if result["status"] == "completed":
                # Import selected sources
                imported = await client.research.import_sources(
                    notebook_id, task["task_id"], result["sources"][:5]
                )
    """

    def __init__(
        self,
        rpc: RpcCaller,
        *,
        source_lister: NotebookSourceLister | None = None,
    ):
        """Initialize the research API.

        Args:
            rpc: RPC dispatch surface (typically the shared client session).
            source_lister: Optional :class:`NotebookSourceLister` used by
                :meth:`import_sources_with_verification` to snapshot baseline
                source IDs before the import call and probe sources on
                timeout. When omitted, a default lister is built from
                ``rpc`` — mirrors the ``NotebooksAPI`` wiring pattern, so
                ``ResearchAPI(rpc)`` works standalone with no cross-API
                dependency.
        """
        self._rpc = rpc
        self._source_lister = source_lister or create_default_source_lister(self._rpc_call)

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

        Mirrors :meth:`NotebooksAPI._rpc_call` so the default source-lister
        built in ``__init__`` picks up post-construction ``rpc`` swaps
        (advanced tests / instrumentation).
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
    def _parse_result_type(value: Any) -> int | str:
        """Normalize known research source type tags while keeping unknown tags intact."""
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            return _RESEARCH_RESULT_TYPE_ALIASES.get(value.lower(), value)
        return 1

    @staticmethod
    def _build_report_import_entry(title: str, markdown: str) -> list[Any]:
        """Build the special deep-research report entry used by IMPORT_RESEARCH."""
        return [None, [title, markdown], None, 3, None, None, None, None, None, None, 3]

    @staticmethod
    def _build_web_import_entry(url: str, title: str) -> list[Any]:
        """Build a standard web-source import entry used by IMPORT_RESEARCH."""
        return [None, None, [url, title], None, None, None, None, None, None, None, 2]

    @staticmethod
    def _extract_legacy_report_chunks(src: list[Any]) -> str:
        """Join legacy deep-research report chunks stored in ``src[6]``.

        Legacy deep-research payloads store report markdown as a list of one or
        more string chunks at index 6. Non-string values are ignored. Returns an
        empty string when the field is missing, malformed, or contains no
        string chunks.
        """
        if len(src) <= 6 or not isinstance(src[6], list):
            return ""
        chunks = [chunk for chunk in src[6] if isinstance(chunk, str) and chunk]
        return "\n\n".join(chunks)

    @staticmethod
    def _normalize_url(url: str) -> str:
        """Normalize source/report URLs for citation matching.

        Thin wrapper retained for backward compatibility. Delegates to
        :func:`notebooklm.research.normalize_url`.
        """
        return _research_pub.normalize_url(url)

    @classmethod
    def extract_report_urls(cls, report: str) -> set[str]:
        """Extract normalized URLs from research report markdown/text.

        Thin wrapper retained for backward compatibility. Delegates to
        :func:`notebooklm.research.extract_report_urls`.
        """
        return _research_pub.extract_report_urls(report)

    @classmethod
    def select_cited_sources(
        cls,
        sources: list[dict[str, Any]],
        report: str,
    ) -> CitedSourceSelection:
        """Return research sources cited by the completed report.

        Thin wrapper retained for backward compatibility. Delegates to
        :func:`notebooklm.research.select_cited_sources`.
        """
        return _research_pub.select_cited_sources(sources, report)

    async def start(
        self,
        notebook_id: str,
        query: str,
        source: str = "web",
        mode: str = "fast",
    ) -> dict[str, Any] | None:
        """Start a research session.

        Args:
            notebook_id: The notebook ID.
            query: The research query.
            source: "web" or "drive".
            mode: "fast" or "deep" (deep only available for web).

        Returns:
            Dictionary with task_id, report_id, and metadata.

        Raises:
            ValidationError: If source/mode combination is invalid.
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

        # 1 = Web, 2 = Drive
        source_type = 1 if source_lower == "web" else 2

        if mode_lower == "fast":
            params = [[query, source_type], None, 1, notebook_id]
            rpc_id = RPCMethod.START_FAST_RESEARCH
        else:
            params = [None, [1], [query, source_type], 5, notebook_id]
            rpc_id = RPCMethod.START_DEEP_RESEARCH

        result = await self._rpc.rpc_call(
            rpc_id,
            params,
            source_path=f"/notebook/{notebook_id}",
        )

        if result and isinstance(result, list) and len(result) > 0:
            task_id = result[0]
            report_id = result[1] if len(result) > 1 else None
            return {
                "task_id": task_id,
                "report_id": report_id,
                "notebook_id": notebook_id,
                "query": query,
                "mode": mode_lower,
            }
        return None

    async def poll(
        self,
        notebook_id: str,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        """Poll for research results.

        Args:
            notebook_id: The notebook ID.
            task_id: Optional discriminator selecting a specific research task
                when more than one is in flight against the same notebook.
                When set, the returned ``task_id`` / ``status`` / ``query`` /
                ``sources`` / ``summary`` / ``report`` fields describe the
                matched task, and ``tasks`` contains only that task. When
                ``None`` and multiple tasks are in flight, a
                :class:`DeprecationWarning` is emitted and the *latest* task
                is returned (preserving legacy behavior). When ``None`` and a
                single task is in flight, behavior is unchanged and no
                warning fires.

                Migration: callers that started research via
                :meth:`start` and held onto the returned ``task_id`` should
                pass it here on every subsequent ``poll`` to remove
                ambiguity. The ``None`` default will be removed in a future
                major release.

        Returns:
            Dictionary representing the parsed research task for the
            notebook. Includes:
            - ``task_id``: task/report identifier for the selected task
            - ``status``: ``in_progress``, ``completed``, or ``no_research``
            - ``query``: original research query text
            - ``sources``: parsed source dictionaries for the selected task
            - ``summary``: summary text when present
            - ``report``: extracted deep-research report markdown when present
            - ``tasks``: list of all parsed research tasks visible at this
              poll (filtered to the matched task when ``task_id`` is set),
              each with the same shape as the top-level fields

            Each source dictionary may include:
            - ``url`` and ``title``
            - ``result_type``
            - ``research_task_id``: task/report ID that produced the source
            - ``report_markdown`` for deep-research report entries

            When ``task_id`` is supplied but no in-flight task matches, the
            return is ``{"status": "no_research", "tasks": []}`` — the same
            shape as the empty-poll case.
        """
        logger.debug("Polling research status for notebook %s", notebook_id)
        params = [None, None, notebook_id]
        result = await self._rpc.rpc_call(
            RPCMethod.POLL_RESEARCH,
            params,
            source_path=f"/notebook/{notebook_id}",
        )

        if not result or not isinstance(result, list) or len(result) == 0:
            return {"status": "no_research", "tasks": []}

        # Unwrap if needed
        if isinstance(result[0], list) and len(result[0]) > 0 and isinstance(result[0][0], list):
            result = result[0]

        parsed_tasks = []
        for task_data in result:
            if not isinstance(task_data, list):
                continue

            # Distinct from the ``task_id`` parameter (the caller's
            # discriminator); name them differently to avoid the obvious
            # shadowing trap.
            parsed_task_id = _extract_task_id(task_data)
            task_info = _extract_task_info(task_data)
            if parsed_task_id is None or task_info is None:
                continue

            query_text = _extract_query_text(task_info) or ""
            sources_data, summary_opt = _extract_sources_and_summary(task_info)
            summary = summary_opt or ""
            status_code = _extract_status_code(task_info)

            parsed_sources = []
            report = ""
            for src in sources_data:
                if not isinstance(src, list) or len(src) < 2:
                    continue

                title = ""
                url = ""
                source_report = ""
                parsed_source = None

                # Fast research: [url, title, desc, type, ...]
                # Deep research (legacy): [None, title, None, type, ..., [report_markdown]]
                # Deep research (current): [None, [title, report_markdown], None, type, ...]
                # src[3] is the authoritative result_type when present.
                # Legacy payloads use string tags such as "web"/"drive".
                result_type = self._parse_result_type(src[3]) if len(src) > 3 else 1
                if src[0] is None and len(src) > 1:
                    if (
                        isinstance(src[1], list)
                        and len(src[1]) >= 2
                        and isinstance(src[1][0], str)
                        and isinstance(src[1][1], str)
                    ):
                        title = src[1][0]
                        source_report = src[1][1]
                        url = ""
                        if result_type == 1:
                            result_type = 5  # deep research report entry (fallback)
                    elif isinstance(src[1], str):
                        title = src[1]
                        url = ""
                        if result_type == 1:
                            result_type = 5  # deep research report entry (fallback)
                elif isinstance(src[0], str) or len(src) >= 3:
                    url = src[0] if isinstance(src[0], str) else ""
                    title = src[1] if len(src) > 1 and isinstance(src[1], str) else ""

                if title or url:
                    parsed_source = {
                        "url": url,
                        "title": title,
                        "result_type": result_type,
                        "research_task_id": parsed_task_id,
                    }
                    if source_report:
                        parsed_source["report_markdown"] = source_report
                    parsed_sources.append(parsed_source)

                # Current payloads inline report markdown in src[1][1].
                # Legacy payloads keep it in src[6] as one or more chunks.
                if not report and source_report:
                    report = source_report
                elif not report:
                    report = self._extract_legacy_report_chunks(src)
                    if report and parsed_source is not None:
                        parsed_source["report_markdown"] = report

            # NOTE: Research status codes differ from artifact status codes
            # Research: 1=in_progress, 2=completed, 6=completed (deep research)
            # Artifacts: 1=in_progress, 2=pending, 3=completed
            status = "completed" if status_code in (2, 6) else "in_progress"

            parsed_tasks.append(
                {
                    "task_id": parsed_task_id,
                    "status": status,
                    "query": query_text,
                    "sources": parsed_sources,
                    "summary": summary,
                    "report": report,
                }
            )

        # Task-id discriminator: when supplied, filter parsed_tasks
        # down to the matched task so callers iterating ``tasks`` don't see
        # un-asked-for siblings. When omitted but multiple tasks are in
        # flight, surface the latent cross-wire hazard via a
        # DeprecationWarning — old behavior (return latest) is preserved to
        # avoid breaking legacy single-task callers.
        if task_id is not None:
            parsed_tasks = [t for t in parsed_tasks if t.get("task_id") == task_id]
        elif len(parsed_tasks) > 1:
            warnings.warn(
                (
                    f"ResearchAPI.poll(notebook_id={notebook_id!r}) returned "
                    f"{len(parsed_tasks)} in-flight tasks but no task_id "
                    f"discriminator was supplied. The latest task is "
                    f"returned for back-compat, but this is ambiguous and "
                    f"may surface results for the wrong task. Pass "
                    f"task_id=<id> (from research.start) to select "
                    f"explicitly. The None default will be removed in a "
                    f"future major release."
                ),
                DeprecationWarning,
                stacklevel=2,
            )

        if parsed_tasks:
            selected_task = parsed_tasks[0]
            return {
                **selected_task,
                "tasks": parsed_tasks,
            }

        return {"status": "no_research", "tasks": []}

    async def import_sources(
        self,
        notebook_id: str,
        task_id: str,
        sources: list[dict[str, Any]],
    ) -> list[dict[str, str]]:
        """Import selected research sources into the notebook.

        Args:
            notebook_id: The notebook ID.
            task_id: The research task ID.
            sources: List of sources to import, each with 'url' and 'title'.
                Deep research results from poll() may also include a report
                entry with 'report_markdown' and 'research_task_id'.

        Returns:
            List of imported sources with 'id' and 'title'.

        Note:
            The API response can be incomplete - it may return fewer items than
            were actually imported. All requested sources typically get imported
            successfully, but the return value may not reflect all of them.
            To reliably verify imports, check the notebook's source list using
            `client.sources.list(notebook_id)` after calling this method.
        """
        logger.debug("Importing %d research sources into notebook %s", len(sources), notebook_id)
        if not sources:
            return []

        # Per-source ``research_task_id`` must match the caller's
        # ``task_id`` when both are present. A mismatch is the wire-crossing
        # bug — importing under the wrong task would mis-attribute
        # provenance. We do this scan BEFORE the multi-task batch check so
        # callers get the precise diagnostic (which mismatched source +
        # which task) instead of the generic "multiple tasks" message.
        for source in sources:
            source_task_id = source.get("research_task_id")
            if isinstance(source_task_id, str) and source_task_id and source_task_id != task_id:
                raise ResearchTaskMismatchError(
                    task_id=task_id,
                    source_research_task_id=source_task_id,
                )

        research_task_ids = {
            research_task_id
            for source in sources
            if isinstance((research_task_id := source.get("research_task_id")), str)
            and research_task_id
        }
        if len(research_task_ids) > 1:
            raise ValidationError(
                "Cannot import sources from multiple research tasks in one batch."
            )
        effective_task_id = next(iter(research_task_ids), task_id)

        report_sources = [
            source
            for source in sources
            if source.get("result_type") == 5
            and isinstance(source.get("title"), str)
            and isinstance(source.get("report_markdown"), str)
            and source.get("report_markdown")
        ]
        report_source_ids = {id(source) for source in report_sources}
        valid_sources = [s for s in sources if s.get("url") and id(s) not in report_source_ids]
        skipped_count = len(sources) - len(valid_sources) - len(report_sources)
        if skipped_count > 0:
            logger.warning(
                "Skipping %d source(s) that cannot be imported (missing URLs or report entries)",
                skipped_count,
            )
        if not valid_sources and not report_sources:
            return []

        source_array = []
        for report_source in report_sources:
            source_array.append(
                self._build_report_import_entry(
                    report_source["title"], report_source["report_markdown"]
                )
            )
        source_array.extend(
            self._build_web_import_entry(src["url"], src.get("title", "Untitled"))
            for src in valid_sources
        )

        params = [None, [1], effective_task_id, notebook_id, source_array]

        result = await self._rpc.rpc_call(
            RPCMethod.IMPORT_RESEARCH,
            params,
            source_path=f"/notebook/{notebook_id}",
        )

        imported = []
        if result and isinstance(result, list):
            if (
                len(result) > 0
                and isinstance(result[0], list)
                and len(result[0]) > 0
                and isinstance(result[0][0], list)
            ):
                result = result[0]

            for src_data in result:
                if isinstance(src_data, list) and len(src_data) >= 2:
                    src_id = (
                        src_data[0][0] if src_data[0] and isinstance(src_data[0], list) else None
                    )
                    if src_id:
                        imported.append({"id": src_id, "title": src_data[1]})

        return imported

    async def import_sources_with_verification(
        self,
        notebook_id: str,
        task_id: str,
        sources: list[dict[str, Any]],
        *,
        max_elapsed: float = 1800,
        initial_delay: float = 5,
        backoff_factor: float = 2,
        max_delay: float = 60,
    ) -> list[dict[str, str]]:
        """Import sources with timeout-tolerant verification.

        Use this in preference to :meth:`import_sources` for deep research:
        the underlying ``IMPORT_RESEARCH`` RPC commonly responds in >30 s on
        deep-research payloads and a one-shot call times out at the client
        even when the server has already committed.

        Lifecycle:

        1. Snapshot baseline source IDs via ``client.sources.list``.
        2. Call :meth:`import_sources`.
        3. On :class:`RPCTimeoutError`, probe ``client.sources.list`` again:
           - If every requested URL appears among *new* (post-baseline)
             sources, treat as success and return the imported entries
             without retrying — the server committed before the response
             was lost.
           - Otherwise filter out URLs that are already present (the
             server committed *some* of the batch) and retry only the
             remaining sources.
        4. Bound total elapsed time by ``max_elapsed``; back off between
           retries (capped by ``max_delay``).
        5. Report-only imports (no URLs to verify) cap retries at one
           attempt to bound duplicate-inflation worst case.

        This method preserves the #808 ``NON_IDEMPOTENT_NO_RETRY``
        classification of the raw ``IMPORT_RESEARCH`` RPC: the executor
        still refuses to retry internally; the safe retry happens here,
        anchored on the pre-call snapshot, which is the disambiguation
        the #808 analysis said was unavailable to the executor.

        Raises:
            RPCTimeoutError: If retries exhaust the ``max_elapsed`` budget.
        """
        if not sources:
            return []

        started_at = time.monotonic()
        delay = initial_delay
        attempt = 1
        verified_imported: list[dict[str, str]] = []
        verified_imported_ids: set[str] = set()

        requested_urls_norm = _requested_urls_norm(sources)
        # Track how many non-URL entries (research reports, pasted text) the
        # request includes so concurrent no-URL additions cannot inflate the
        # synthesized return after a timeout.
        requested_no_url_count = _no_url_entry_count(sources)

        # Anchor verified-success on URLs of *new* sources (not on a
        # baseline→current URL delta) so concurrent additions from another
        # session and pre-existing URLs cannot satisfy the check.
        baseline_ids: set[str] | None
        try:
            baseline = await self._source_lister.list(notebook_id, strict=True)
            baseline_ids = {src.id for src in baseline}
        except (NetworkError, RPCError) as snapshot_exc:
            logger.warning(
                "Pre-import sources.list snapshot failed for %s: %s; "
                "verified-success path disabled for this call",
                notebook_id,
                snapshot_exc,
            )
            baseline_ids = None

        while True:
            try:
                imported = await self.import_sources(notebook_id, task_id, sources)
                return _merge_imported_sources(imported, verified_imported, verified_imported_ids)
            except RPCTimeoutError:
                elapsed = time.monotonic() - started_at
                remaining = max_elapsed - elapsed

                if requested_urls_norm:
                    try:
                        current = await self._source_lister.list(notebook_id, strict=True)
                        new_sources = (
                            [src for src in current if src.id not in baseline_ids]
                            if baseline_ids is not None
                            else []
                        )
                        new_urls_norm = {
                            _normalize_import_url(src.url) for src in new_sources if src.url
                        }
                        current_urls_norm = {
                            _normalize_import_url(src.url) for src in current if src.url
                        }
                        if baseline_ids is not None and requested_urls_norm.issubset(new_urls_norm):
                            logger.warning(
                                "IMPORT_RESEARCH timed out for notebook %s but "
                                "sources.list shows all %d requested URLs among "
                                "new sources; treating as success and skipping "
                                "retry to avoid duplicate inflation",
                                notebook_id,
                                len(requested_urls_norm),
                            )
                            timeout_verified: list[dict[str, str]] = []
                            remaining_no_url = requested_no_url_count
                            for src in new_sources:
                                if (
                                    src.url
                                    and _normalize_import_url(src.url) in requested_urls_norm
                                ):
                                    timeout_verified.append(_imported_source_entry(src))
                                elif not src.url and remaining_no_url > 0:
                                    timeout_verified.append(_imported_source_entry(src))
                                    remaining_no_url -= 1
                            return _merge_imported_sources(
                                timeout_verified, verified_imported, verified_imported_ids
                            )
                        source_norms = [(source, _source_url_norm(source)) for source in sources]
                        removed_urls_norm = {
                            url
                            for _, url in source_norms
                            if url is not None and url in current_urls_norm
                        }
                        # Filter for retry: drop already-present URLs.
                        # Additionally, when *any* URL was verified
                        # committed, drop no-URL entries (deep-research
                        # reports): reports are appended FIRST in the
                        # IMPORT_RESEARCH payload (see
                        # ``_build_report_import_entry`` usage in
                        # ``import_sources``), so a committed URL implies
                        # the report committed too. Without this guard,
                        # each retry duplicates the report server-side.
                        # When no URL committed, keep no-URL entries —
                        # the report's fate is unknown and the
                        # report-only attempt cap further down bounds
                        # the worst case.
                        drop_no_url_entries = bool(removed_urls_norm)
                        filtered_sources = [
                            source
                            for source, url in source_norms
                            if url not in current_urls_norm
                            and not (drop_no_url_entries and url is None)
                        ]
                        if len(filtered_sources) != len(sources):
                            removed_count = len(sources) - len(filtered_sources)
                            for src in new_sources:
                                if (
                                    src.url
                                    and _normalize_import_url(src.url) in removed_urls_norm
                                    and src.id not in verified_imported_ids
                                ):
                                    verified_imported.append(_imported_source_entry(src))
                                    verified_imported_ids.add(src.id)
                            sources = filtered_sources
                            requested_urls_norm = _requested_urls_norm(sources)
                            requested_no_url_count = _no_url_entry_count(sources)
                            if not sources:
                                logger.warning(
                                    "IMPORT_RESEARCH timed out for notebook %s "
                                    "but sources.list shows all requested URLs "
                                    "already present; treating as success and "
                                    "skipping retry to avoid duplicate inflation",
                                    notebook_id,
                                )
                                return _merge_imported_sources(
                                    [], verified_imported, verified_imported_ids
                                )
                            logger.warning(
                                "IMPORT_RESEARCH timed out for notebook %s after "
                                "%d requested source(s) were already present; "
                                "retrying with %d remaining source(s)",
                                notebook_id,
                                removed_count,
                                len(sources),
                            )
                    except (NetworkError, RPCError) as probe_exc:
                        # CancelledError is a BaseException, not Exception, and
                        # is not in this tuple — it propagates naturally for
                        # callers that need to cancel the operation cleanly.
                        logger.warning(
                            "Failed to probe server state after timeout: %s; falling back to retry",
                            probe_exc,
                        )

                if remaining <= 0:
                    raise

                # Report-only imports (no URLs to verify) can't use the success
                # check above. Cap retries at one attempt to bound worst-case
                # duplicate inflation for report entries when timeouts persist.
                if not requested_urls_norm and attempt >= 2:
                    logger.warning(
                        "IMPORT_RESEARCH timed out for notebook %s with no URLs "
                        "to verify; giving up after %d attempts to bound "
                        "duplicate inflation",
                        notebook_id,
                        attempt,
                    )
                    raise

                sleep_for = min(delay, max_delay, remaining)
                logger.warning(
                    "IMPORT_RESEARCH timed out for notebook %s; retrying in "
                    "%.1fs (attempt %d, %.1fs elapsed)",
                    notebook_id,
                    sleep_for,
                    attempt + 1,
                    elapsed,
                )
                await asyncio.sleep(sleep_for)
                delay = min(delay * backoff_factor, max_delay)
                attempt += 1
