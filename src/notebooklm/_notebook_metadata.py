"""Private notebook metadata composition service."""

from __future__ import annotations

import asyncio
import builtins
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Protocol, TypeVar

from .exceptions import DecodingError
from .types import Notebook, NotebookMetadata, Source, SourceSummary

# Preserve the historical warning channel from NotebooksAPI.get_metadata().
logger = logging.getLogger("notebooklm._notebooks")


class NotebookSourceLister(Protocol):
    """Structural source-listing dependency shared across feature APIs.

    Consumed by :class:`NotebookMetadataService` for metadata composition
    and by :meth:`ResearchAPI.import_sources_with_verification` for
    snapshot/probe around ``IMPORT_RESEARCH`` (issue #315). Implementations
    are supplied structurally, so feature APIs don't need to depend on
    ``SourcesAPI`` itself. The composition root supplies its shared sources
    API. Concrete backends own any transport-specific default lister construction.
    """

    async def list(self, notebook_id: str, *, strict: bool = False) -> builtins.list[Source]:
        """List sources for a notebook."""


class NotebookSourceIdProvider(Protocol):
    """Structural source-id dependency needed by chat and artifact generation."""

    async def get_source_ids(self, notebook_id: str) -> builtins.list[str]:
        """Return source IDs for a notebook."""


class CreatedChatSessionProvider(Protocol):
    """One-shot CREATE_NOTEBOOK chat-session hint consumed by ChatAPI."""

    def _take_created_chat_session_id(self, notebook_id: str) -> str | None:
        """Return and remove the created notebook's volunteered session id."""


NotebookGetter = Callable[[str], Awaitable[Notebook]]
_CopyMappingItem = TypeVar("_CopyMappingItem")
_ChildResult = TypeVar("_ChildResult")


class SpawnChild(Protocol):
    """Reserve and start one same-generation child operation."""

    async def __call__(
        self,
        label: str,
        factory: Callable[[], Awaitable[_ChildResult]],
    ) -> asyncio.Task[_ChildResult]: ...


def reconcile_copy_mapping(
    requested_ids: Sequence[str],
    items: list[_CopyMappingItem],
    *,
    original_id: Callable[[_CopyMappingItem], str],
    operation: str,
    item_label: str,
    target_notebook_id: str,
    method_id: str,
    malformed_count: int,
    raw_response: str | None,
    empty_error: Exception,
    warning_logger: logging.Logger,
) -> list[_CopyMappingItem]:
    """Apply the shared post-decode policy for committed copy mappings.

    Backend hooks own wire decoding and report malformed-row diagnostics. This
    helper preserves decoded response order, distinguishes an all-malformed
    response from a genuine empty mapping, and warns for the same set-based
    partial result used by source and artifact copy workflows.
    """
    if not items:
        if malformed_count:
            raise DecodingError(
                f"{operation} returned only malformed mapping entries",
                raw_response=raw_response,
                method_id=method_id,
            )
        raise empty_error

    missing = set(requested_ids) - {original_id(item) for item in items}
    if missing:
        warning_logger.warning(
            "%s copied %d of %d %s(s) into %s; not copied: %s",
            operation,
            len(items),
            len(requested_ids),
            item_label,
            target_notebook_id,
            ", ".join(sorted(missing)),
        )
    return items


class NotebookMetadataService:
    """Compose notebook details and source summaries."""

    def __init__(
        self,
        get_notebook: NotebookGetter,
        source_lister: NotebookSourceLister,
        *,
        spawn_child: SpawnChild,
    ) -> None:
        self._get_notebook = get_notebook
        self._source_lister = source_lister
        self._spawn_child = spawn_child

    async def get_metadata(self, notebook_id: str) -> NotebookMetadata:
        """Get notebook metadata and simplified sources concurrently."""
        notebook_task = await self._spawn_child(
            f"notebook-metadata-notebook-{notebook_id}",
            lambda: self._get_notebook(notebook_id),
        )
        try:
            sources_task = await self._spawn_child(
                f"notebook-metadata-sources-{notebook_id}",
                lambda: self._source_lister.list(notebook_id),
            )
        except BaseException:
            notebook_task.cancel()
            await asyncio.gather(notebook_task, return_exceptions=True)
            raise

        tasks = (notebook_task, sources_task)
        try:
            notebook, sources = await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

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
    "CreatedChatSessionProvider",
    "NotebookSourceIdProvider",
    "NotebookSourceLister",
    "SpawnChild",
    "reconcile_copy_mapping",
]
