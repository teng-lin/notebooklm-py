"""Research API for NotebookLM web/drive research.

Provides operations for starting research sessions, polling for results,
and importing discovered sources into notebooks.
"""

import logging
from typing import Any

from ._core import ClientCore
from .rpc import RPCMethod

logger = logging.getLogger(__name__)


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
            ValueError: If source/mode combination is invalid.
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
            raise ValueError(f"Invalid source '{source}'. Use 'web' or 'drive'.")
        if mode_lower not in ("fast", "deep"):
            raise ValueError(f"Invalid mode '{mode}'. Use 'fast' or 'deep'.")
        if mode_lower == "deep" and source_lower == "drive":
            raise ValueError("Deep Research only supports Web sources.")

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
            Dictionary with status, query, sources, and summary.
        """
        logger.debug("Polling research status for notebook %s", notebook_id)
        params = [None, None, notebook_id]
        result = await self._core.rpc_call(
            RPCMethod.POLL_RESEARCH,
            params,
            source_path=f"/notebook/{notebook_id}",
        )

        if not result or not isinstance(result, list) or len(result) == 0:
            return {"status": "no_research"}

        # Unwrap if needed
        if isinstance(result[0], list) and len(result[0]) > 0 and isinstance(result[0][0], list):
            result = result[0]

        # Find most recent task
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
            for src in sources_data:
                if not isinstance(src, list) or len(src) < 2:
                    continue

                title = ""
                url = ""

                # Fast research: [url, title, desc, type, ...]
                # Deep research: [None, title, None, type, ..., [report]]
                if src[0] is None and len(src) > 1 and isinstance(src[1], str):
                    title = src[1]
                    url = ""
                elif isinstance(src[0], str) or len(src) >= 3:
                    url = src[0] if isinstance(src[0], str) else ""
                    title = src[1] if len(src) > 1 and isinstance(src[1], str) else ""

                if title or url:
                    parsed_sources.append({"url": url, "title": title})

            # NOTE: Research status codes differ from artifact status codes
            # Research: 1=in_progress, 2=completed
            # Artifacts: 1=in_progress, 2=pending, 3=completed
            status = "completed" if status_code == 2 else "in_progress"

            return {
                "task_id": task_id,
                "status": status,
                "query": query_text,
                "sources": parsed_sources,
                "summary": summary,
            }

        return {"status": "no_research"}

    async def import_sources(
        self,
        notebook_id: str,
        task_id: str,
        sources: list[dict[str, str]],
    ) -> list[dict[str, str]]:
        """Import selected research sources into the notebook.

        Args:
            notebook_id: The notebook ID.
            task_id: The research task ID.
            sources: List of sources to import, each with 'url' and 'title'.

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

        # Filter out sources without URLs - these cause the entire batch to fail
        valid_sources = [s for s in sources if s.get("url")]
        skipped_count = len(sources) - len(valid_sources)
        if skipped_count > 0:
            logger.warning("Skipping %d source(s) without URLs (cannot be imported)", skipped_count)
        if not valid_sources:
            return []

        source_array = [
            [
                None,
                None,
                [src["url"], src.get("title", "Untitled")],
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                2,
            ]
            for src in valid_sources
        ]

        params = [None, [1], task_id, notebook_id, source_array]

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
