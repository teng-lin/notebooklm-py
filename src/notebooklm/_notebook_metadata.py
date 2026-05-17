"""Private notebook metadata composition service."""

from __future__ import annotations

import asyncio
import builtins
import logging
from collections.abc import Awaitable, Callable
from typing import Protocol

from ._source_listing import RpcCall as SourceListingRpcCall
from ._source_listing import SourceLister as SourceListingService
from .types import Notebook, NotebookMetadata, Source, SourceSummary

# Preserve the historical warning channel from NotebooksAPI.get_metadata().
logger = logging.getLogger("notebooklm._notebooks")


class NotebookSourceLister(Protocol):
    """Structural source-listing dependency needed by notebook metadata."""

    async def list(self, notebook_id: str, *, strict: bool = False) -> builtins.list[Source]:
        """List sources for a notebook."""


NotebookGetter = Callable[[str], Awaitable[Notebook]]


def create_default_source_lister(rpc_call: SourceListingRpcCall) -> NotebookSourceLister:
    """Build the direct-construction source lister without constructing SourcesAPI."""
    return SourceListingService(rpc_call)


class NotebookMetadataService:
    """Compose notebook details and source summaries."""

    def __init__(
        self,
        get_notebook: NotebookGetter,
        source_lister: NotebookSourceLister,
    ) -> None:
        self._get_notebook = get_notebook
        self._source_lister = source_lister

    async def get_metadata(self, notebook_id: str) -> NotebookMetadata:
        """Get notebook metadata and simplified sources concurrently."""
        notebook, sources = await asyncio.gather(
            self._get_notebook(notebook_id),
            self._source_lister.list(notebook_id),
        )

        if notebook.sources_count > 0 and len(sources) == 0:
            logger.warning(
                "Notebook %s reports %d sources but listing returned empty",
                notebook_id,
                notebook.sources_count,
            )

        return NotebookMetadata(
            notebook=notebook,
            sources=[
                SourceSummary(
                    kind=source.kind,
                    title=source.title,
                    url=source.url,
                )
                for source in sources
            ],
        )


__all__ = [
    "NotebookMetadataService",
    "NotebookSourceLister",
    "create_default_source_lister",
]
