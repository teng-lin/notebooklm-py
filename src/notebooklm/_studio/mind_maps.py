"""Transport-neutral interactive Studio mind-map behavior."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable

from .._backend import BackendAdapter
from .._deadline import RuntimeDeadline
from .._records import (
    MIND_MAP_DELETE_DEF,
    MIND_MAP_GENERATE_INTERACTIVE_DEF,
    MIND_MAP_GET_DEF,
    MIND_MAP_UPDATE_DEF,
    ArtifactRecord,
    MindMapDeleteInput,
    MindMapGenerateInteractiveInput,
    MindMapGenerateInteractiveResult,
    MindMapGenerateOutcomeRecord,
    MindMapGetInput,
    MindMapUpdateInput,
)
from .catalog import StudioCatalog

WaitForCompletion = Callable[[str, str], Awaitable[object]]


class MindMapFamilyService:
    """Interactive mind-map discovery and mutations over the Studio port."""

    __slots__ = ("_backend", "_catalog", "_wait_for_completion")

    def __init__(
        self,
        backend: BackendAdapter,
        catalog: StudioCatalog,
        *,
        wait_for_completion: WaitForCompletion,
    ) -> None:
        self._backend = backend
        self._catalog = catalog
        self._wait_for_completion = wait_for_completion

    async def list_mind_maps(
        self,
        notebook_id: str,
        *,
        deadline: RuntimeDeadline | None = None,
    ) -> list[ArtifactRecord]:
        records = await self._catalog.list_records(notebook_id, "mind_map", deadline=deadline)
        return [record for record in records if record.variant == "interactive_mind_map"]

    async def get_or_none(
        self,
        notebook_id: str,
        mind_map_id: str,
        *,
        deadline: RuntimeDeadline | None = None,
    ) -> ArtifactRecord | None:
        record = await self._catalog.get_record(notebook_id, mind_map_id, deadline=deadline)
        if record is None or record.variant != "interactive_mind_map":
            return None
        return record

    async def generate(
        self,
        notebook_id: str,
        source_ids: list[str] | None,
        instructions: str | None,
        *,
        wait: bool,
        deadline: RuntimeDeadline | None = None,
    ) -> MindMapGenerateOutcomeRecord:
        created: MindMapGenerateInteractiveResult = await self._backend.invoke(
            MIND_MAP_GENERATE_INTERACTIVE_DEF,
            MindMapGenerateInteractiveInput(
                notebook_id,
                None if source_ids is None else tuple(source_ids),
                instructions,
            ),
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
        # authoritative on this post-create path, so the facade retains real
        # catalog metadata only when it is confirmed here, falling back to the
        # allocated identity otherwise (``record=None``).
        resolved = (
            record
            if record is not None
            and (record.variant == "interactive_mind_map" or record.interactive_variant_pending)
            else None
        )
        return MindMapGenerateOutcomeRecord(created.mind_map_id, resolved, tree)

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
    ) -> dict | None:
        # Bare ``dict`` (no type args): P10 I1's permitted-return-atom
        # vocabulary only whitelists the built-in name itself, not "object"
        # as a parameter — see ``I1_PERMITTED_RETURN_BUILTINS``.
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
