"""Transport-neutral semantic services for notebook and source reads."""

from __future__ import annotations

import builtins

from ._backend import BackendAdapter
from ._deadline import RuntimeDeadline
from ._records import (
    NOTEBOOK_GET_DEF,
    NOTEBOOK_LIST_DEF,
    SOURCE_GET_DEF,
    SOURCE_LIST_DEF,
    NotebookGetInput,
    NotebookListInput,
    NotebookRecord,
    SourceGetInput,
    SourceListInput,
    SourceRecord,
)


class NotebookReadService:
    """Invoke semantic notebook reads and return their neutral records."""

    __slots__ = ("_backend",)

    def __init__(self, backend: BackendAdapter) -> None:
        self._backend = backend

    async def list(self, *, deadline: RuntimeDeadline | None = None) -> list[NotebookRecord]:
        result = await self._backend.invoke(
            NOTEBOOK_LIST_DEF,
            NotebookListInput(),
            deadline=deadline,
        )
        return list(result.notebooks)

    async def get(
        self,
        notebook_id: str,
        *,
        deadline: RuntimeDeadline | None = None,
    ) -> NotebookRecord | None:
        result = await self._backend.invoke(
            NOTEBOOK_GET_DEF,
            NotebookGetInput(notebook_id),
            deadline=deadline,
        )
        return result.notebook

    async def get_source_ids(
        self,
        notebook_id: str,
        *,
        deadline: RuntimeDeadline | None = None,
    ) -> builtins.list[str]:
        """Return embedded source ids from one semantic notebook snapshot."""
        result = await self._backend.invoke(
            NOTEBOOK_GET_DEF,
            NotebookGetInput(notebook_id, include_notebook=False),
            deadline=deadline,
        )
        return list(result.source_ids)


class SourceReadService:
    """Invoke semantic source reads and return their neutral records."""

    __slots__ = ("_backend",)

    def __init__(self, backend: BackendAdapter) -> None:
        self._backend = backend

    async def list(
        self,
        notebook_id: str,
        *,
        strict: bool = False,
        statuses: frozenset[str] | None = None,
        kinds: frozenset[str] | None = None,
        deadline: RuntimeDeadline | None = None,
    ) -> list[SourceRecord]:
        result = await self._backend.invoke(
            SOURCE_LIST_DEF,
            SourceListInput(
                notebook_id,
                strict=strict,
                statuses=statuses,
                kinds=kinds,
            ),
            deadline=deadline,
        )
        return list(result.sources)

    async def get(
        self,
        notebook_id: str,
        source_id: str,
        *,
        deadline: RuntimeDeadline | None = None,
    ) -> SourceRecord | None:
        result = await self._backend.invoke(
            SOURCE_GET_DEF,
            SourceGetInput(notebook_id, source_id),
            deadline=deadline,
        )
        return result.source


__all__ = ["NotebookReadService", "SourceReadService"]
