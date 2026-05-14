"""Research API for NotebookLM web/drive research.

Provides operations for starting research sessions, polling for results,
and importing discovered sources into notebooks.
"""

import logging
from typing import Any

from . import research as _research_pub
from ._core import ClientCore
from .exceptions import ValidationError
from .rpc import RPCMethod
from .types import CitedSourceSelection

__all__ = ["CitedSourceSelection", "ResearchAPI"]

logger = logging.getLogger(__name__)

_RESEARCH_RESULT_TYPE_ALIASES = {
    "web": 1,
    "drive": 2,
    "report": 5,
}


class ResearchAPI:
    """Operations for research sessions (web/drive search).

    Provides methods for starting research, polling for results, and
    importing discovered sources into notebooks.

    Usage:
        async with NotebookLMClient.from_storage() as client:
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

    def __init__(self, core: ClientCore):
        """Initialize the research API.

        Args:
            core: The core client infrastructure.
        """
        self._core = core

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

        result = await self._core.rpc_call(
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

    async def poll(self, notebook_id: str) -> dict[str, Any]:
        """Poll for research results.

        Args:
            notebook_id: The notebook ID.

        Returns:
            Dictionary representing the latest parsed research task for the
            notebook. Includes:
            - ``task_id``: task/report identifier for the latest task
            - ``status``: ``in_progress``, ``completed``, or ``no_research``
            - ``query``: original research query text
            - ``sources``: parsed source dictionaries for the latest task
            - ``summary``: summary text when present
            - ``report``: extracted deep-research report markdown when present
            - ``tasks``: additive list of all parsed research tasks, each with
              the same shape as the top-level latest-task fields

            Each source dictionary may include:
            - ``url`` and ``title``
            - ``result_type``
            - ``research_task_id``: task/report ID that produced the source
            - ``report_markdown`` for deep-research report entries
        """
        logger.debug("Polling research status for notebook %s", notebook_id)
        params = [None, None, notebook_id]
        result = await self._core.rpc_call(
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
            if not isinstance(task_data, list) or len(task_data) < 2:
                continue

            task_id = task_data[0]
            task_info = task_data[1]

            if not isinstance(task_id, str) or not isinstance(task_info, list):
                continue

            query_info = task_info[1] if len(task_info) > 1 else None
            sources_and_summary = task_info[3] if len(task_info) > 3 else []
            status_code = task_info[4] if len(task_info) > 4 else None

            query_text = query_info[0] if query_info else ""
            sources_data = []
            summary = ""

            if isinstance(sources_and_summary, list) and len(sources_and_summary) >= 1:
                sources_data = (
                    sources_and_summary[0] if isinstance(sources_and_summary[0], list) else []
                )
                if len(sources_and_summary) >= 2 and isinstance(sources_and_summary[1], str):
                    summary = sources_and_summary[1]

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
                        "research_task_id": task_id,
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
                    "task_id": task_id,
                    "status": status,
                    "query": query_text,
                    "sources": parsed_sources,
                    "summary": summary,
                    "report": report,
                }
            )

        if parsed_tasks:
            latest_task = parsed_tasks[0]
            return {
                **latest_task,
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

        result = await self._core.rpc_call(
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
