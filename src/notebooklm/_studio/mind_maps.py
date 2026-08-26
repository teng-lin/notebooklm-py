"""Transport-neutral interactive Studio mind-map behavior."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable

from .._backend import BackendAdapter
from .._deadline import RuntimeDeadline, RuntimeDeadlineFactory
from .._projectors import project_artifact
from .._read_services import NotebookReadService
from .._records import (
    MIND_MAP_DELETE_DEF,
    MIND_MAP_GENERATE_INTERACTIVE_DEF,
    MIND_MAP_GET_DEF,
    MIND_MAP_UPDATE_DEF,
    ArtifactRecord,
    MindMapDeleteInput,
    MindMapGenerateInteractiveInput,
    MindMapGenerateInteractiveResult,
    MindMapGetInput,
    MindMapUpdateInput,
    SourceIdDiagnostics,
)
from ..types import MindMap, MindMapKind
from .catalog import StudioCatalog

WaitForCompletion = Callable[[str, str], Awaitable[object]]


class MindMapFamilyService:
    """Interactive mind-map discovery and mutations over the Studio port."""

    __slots__ = (
        "_backend",
        "_catalog",
        "_deadline_factory",
        "_notebooks",
        "_wait_for_completion",
    )

    def __init__(
        self,
        backend: BackendAdapter,
        catalog: StudioCatalog,
        *,
        wait_for_completion: WaitForCompletion,
        deadline_factory: RuntimeDeadlineFactory | None = None,
    ) -> None:
        self._backend = backend
        self._catalog = catalog
        self._wait_for_completion = wait_for_completion
        self._deadline_factory = deadline_factory
        # The default source scope is resolved here, above the port: the
        # generation operation itself takes an already-resolved input record.
        self._notebooks = NotebookReadService(backend)

    @staticmethod
    def _project(
        record: ArtifactRecord,
        notebook_id: str,
        *,
        tree: dict[str, object] | None = None,
    ) -> MindMap:
        # Keep the public Artifact compatibility projection in this boundary:
        # P0 pins its causal contribution to adapter MindMap envelopes even
        # though wire decoding and family selection use neutral records.
        artifact = project_artifact(record)
        return MindMap(
            id=artifact.id,
            notebook_id=notebook_id,
            title=artifact.title,
            kind=MindMapKind.INTERACTIVE,
            created_at=artifact.created_at,
            tree=tree,
        )

    async def list_mind_maps(
        self,
        notebook_id: str,
        *,
        deadline: RuntimeDeadline | None = None,
    ) -> list[MindMap]:
        records = await self._catalog.list_records(notebook_id, "mind_map", deadline=deadline)
        return [
            self._project(record, notebook_id)
            for record in records
            if record.variant == "interactive_mind_map"
        ]

    async def get_or_none(
        self,
        notebook_id: str,
        mind_map_id: str,
        *,
        deadline: RuntimeDeadline | None = None,
    ) -> MindMap | None:
        record = await self._catalog.get_record(notebook_id, mind_map_id, deadline=deadline)
        if record is None or record.variant != "interactive_mind_map":
            return None
        return self._project(record, notebook_id)

    async def generate(
        self,
        notebook_id: str,
        source_ids: list[str] | None,
        instructions: str | None,
        *,
        wait: bool,
        deadline: RuntimeDeadline | None = None,
    ) -> MindMap:
        """Generate over a source scope, defaulting to the whole notebook.

        ``source_ids=None`` is this service's documented default for "every
        source in the notebook": it costs one extra ``NOTEBOOK_GET`` read, which
        shares the creation call's budget so the pair spends one client timeout.
        An explicit list — the empty one included — is used verbatim. The read
        decodes with :attr:`SourceIdDiagnostics.SILENT`, which is what this
        family has always reported about a snapshot it cannot read: nothing.
        """
        if deadline is None and self._deadline_factory is not None:
            # Captured once, before the read: both natives spend one budget.
            deadline = self._deadline_factory.start()
        resolved = await self._resolve_scope(notebook_id, source_ids, deadline=deadline)
        created: MindMapGenerateInteractiveResult = await self._backend.invoke(
            MIND_MAP_GENERATE_INTERACTIVE_DEF,
            MindMapGenerateInteractiveInput(notebook_id, resolved, instructions),
            deadline=deadline,
        )
        if wait:
            await self._wait_for_completion(notebook_id, created.mind_map_id)
        record = await self._catalog.get_record(
            notebook_id,
            created.mind_map_id,
            deadline=deadline,
        )
        tree = (
            await self.get_tree(notebook_id, created.mind_map_id, deadline=deadline)
            if wait
            else None
        )
        # A newly allocated type-4 row can briefly lack its variant.  The id is
        # authoritative on this post-create path, so retain its real metadata.
        if record is not None and (
            record.variant == "interactive_mind_map" or record.interactive_variant_pending
        ):
            return self._project(record, notebook_id, tree=tree)
        return MindMap(
            id=created.mind_map_id,
            notebook_id=notebook_id,
            title="Mind Map",
            kind=MindMapKind.INTERACTIVE,
            tree=tree,
        )

    async def _resolve_scope(
        self,
        notebook_id: str,
        source_ids: list[str] | None,
        *,
        deadline: RuntimeDeadline | None,
    ) -> tuple[str, ...]:
        """Expand an omitted scope into the notebook's full embedded source set."""
        if source_ids is not None:
            return tuple(source_ids)
        return tuple(
            await self._notebooks.get_source_ids(
                notebook_id,
                diagnostics=SourceIdDiagnostics.SILENT,
                deadline=deadline,
            )
        )

    async def rename(
        self,
        notebook_id: str,
        mind_map_id: str,
        new_title: str,
        *,
        deadline: RuntimeDeadline | None = None,
    ) -> None:
        await self._backend.invoke(
            MIND_MAP_UPDATE_DEF,
            MindMapUpdateInput(notebook_id, mind_map_id, new_title),
            deadline=deadline,
        )

    async def delete(
        self,
        notebook_id: str,
        mind_map_id: str,
        *,
        deadline: RuntimeDeadline | None = None,
    ) -> None:
        await self._backend.invoke(
            MIND_MAP_DELETE_DEF,
            MindMapDeleteInput(notebook_id, mind_map_id),
            deadline=deadline,
        )

    async def get_tree(
        self,
        notebook_id: str,
        mind_map_id: str,
        *,
        deadline: RuntimeDeadline | None = None,
    ) -> dict[str, object] | None:
        result = await self._backend.invoke(
            MIND_MAP_GET_DEF,
            MindMapGetInput(notebook_id, mind_map_id),
            deadline=deadline,
        )
        if result.tree_json is None:
            return None
        try:
            tree = json.loads(result.tree_json)
        except (json.JSONDecodeError, TypeError):
            return None
        return tree if isinstance(tree, dict) else None


__all__ = ["MindMapFamilyService"]
