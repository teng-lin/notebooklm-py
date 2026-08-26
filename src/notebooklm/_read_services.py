"""Transport-neutral semantic services for notebook and source reads."""

from __future__ import annotations

import builtins
from typing import TYPE_CHECKING

from ._backend import BackendAdapter
from ._deadline import RuntimeDeadline
from ._projectors import project_notebook, project_source
from ._records import (
    NOTEBOOK_GET_DEF,
    NOTEBOOK_LIST_DEF,
    SOURCE_GET_DEF,
    SOURCE_LIST_DEF,
    NotebookGetInput,
    NotebookListInput,
    SourceGetInput,
    SourceIdDiagnostics,
    SourceListInput,
)

if TYPE_CHECKING:
    from .types import Notebook, Source


class NotebookReadService:
    """Invoke semantic notebook reads and project their neutral records."""

    __slots__ = ("_backend",)

    def __init__(self, backend: BackendAdapter) -> None:
        self._backend = backend

    async def list(self, *, deadline: RuntimeDeadline | None = None) -> list[Notebook]:
        result = await self._backend.invoke(
            NOTEBOOK_LIST_DEF,
            NotebookListInput(),
            deadline=deadline,
        )
        return [project_notebook(record) for record in result.notebooks]

    async def get(
        self,
        notebook_id: str,
        *,
        deadline: RuntimeDeadline | None = None,
    ) -> Notebook | None:
        result = await self._backend.invoke(
            NOTEBOOK_GET_DEF,
            NotebookGetInput(notebook_id),
            deadline=deadline,
        )
        return None if result.notebook is None else project_notebook(result.notebook)

    async def get_source_ids(
        self,
        notebook_id: str,
        *,
        diagnostics: SourceIdDiagnostics = SourceIdDiagnostics.GUARDED,
        deadline: RuntimeDeadline | None = None,
    ) -> builtins.list[str]:
        """Return embedded source ids from one semantic notebook snapshot.

        ``diagnostics`` selects what the decode says about a snapshot whose
        source slot it cannot read: the Studio generation families differ only
        in that report, and the mode reaches the decoder on the neutral input.
        """
        result = await self._backend.invoke(
            NOTEBOOK_GET_DEF,
            NotebookGetInput(notebook_id, include_notebook=False, source_diagnostics=diagnostics),
            deadline=deadline,
        )
        return list(result.source_ids)

    def source_lister(self) -> SourceReadService:
        """Build a semantic source lister sharing this service's backend.

        Direct facade construction uses this narrow composition seam for
        notebook metadata. Production still injects its client-owned source
        facade explicitly.
        """
        return SourceReadService(self._backend)


class SourceReadService:
    """Invoke semantic source reads and project their neutral records."""

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
    ) -> list[Source]:
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
        return [project_source(record) for record in result.sources]

    async def get(
        self,
        notebook_id: str,
        source_id: str,
        *,
        deadline: RuntimeDeadline | None = None,
    ) -> Source | None:
        result = await self._backend.invoke(
            SOURCE_GET_DEF,
            SourceGetInput(notebook_id, source_id),
            deadline=deadline,
        )
        return None if result.source is None else project_source(result.source)


__all__ = ["NotebookReadService", "SourceReadService"]
