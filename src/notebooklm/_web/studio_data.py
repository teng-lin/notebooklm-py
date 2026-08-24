"""Web workflow bindings for mind-map and data-table generation compatibility.

Since P9.3 the Drive export leaf (``ARTIFACT_EXPORT``) is a codec row in
``_web/bindings/studio.py``; the two input-defaulting generate composites remain here.
"""

from __future__ import annotations

import json
from datetime import datetime

from .._artifact.payloads import build_data_table_artifact_params, build_mind_map_params
from .._deadline import RuntimeDeadline
from .._env import get_default_language
from .._notebook_payloads import build_get_notebook_params
from .._operations import Operation
from .._records import (
    DataTableGenerateInput,
    DataTableGenerateResult,
    MindMapGenerateInput,
    MindMapGenerateResult,
)
from .._row_adapters.artifacts import MIND_MAP_LEAF_ABSENT, unwrap_mind_map_generation_leaf
from ..rpc import RPCMethod
from .studio_media import StudioMediaWebHandlers


class StudioDataWebHandlers(StudioMediaWebHandlers):
    """Reusable data-view and Drive-export handlers mixed into the web backend."""

    async def _persist_generated_mind_map(
        self,
        notebook_id: str,
        *,
        title: str,
        content: str,
        operation: Operation,
        deadline: RuntimeDeadline | None,
    ) -> tuple[str | None, datetime | None]:
        """Persist generated JSON through the legacy note seam implemented by the backend."""

        raise NotImplementedError

    async def _data_source_ids(
        self,
        notebook_id: str,
        source_ids: tuple[str, ...] | None,
        *,
        operation: Operation,
        deadline: RuntimeDeadline | None,
    ) -> tuple[str, ...]:
        if source_ids is not None:
            return source_ids
        notebook = await self._rpc_call(
            RPCMethod.GET_NOTEBOOK,
            build_get_notebook_params(notebook_id),
            operation=operation,
            deadline=deadline,
            source_path=f"/notebook/{notebook_id}",
        )
        return self._generation_source_ids(notebook_id, notebook)

    async def _data_table_generate(
        self,
        value: DataTableGenerateInput,
        *,
        deadline: RuntimeDeadline | None,
    ) -> DataTableGenerateResult:
        operation = Operation.ARTIFACT_GENERATE_DATA_TABLE
        source_ids = await self._data_source_ids(
            value.notebook_id,
            value.source_ids,
            operation=operation,
            deadline=deadline,
        )
        result = await self._rpc_call(
            RPCMethod.CREATE_ARTIFACT,
            build_data_table_artifact_params(
                value.notebook_id,
                list(source_ids),
                language=(get_default_language() if value.language is None else value.language),
                instructions=value.instructions,
            ),
            operation=operation,
            deadline=deadline,
            source_path=f"/notebook/{value.notebook_id}",
            allow_null=True,
            operation_variant=None,
            raise_on_null_status=True,
        )
        if result is None:
            raise self._artifact_feature_unavailable(operation, "data table")
        return DataTableGenerateResult(self._generation_status(result, operation))

    async def _mind_map_generate(
        self,
        value: MindMapGenerateInput,
        *,
        deadline: RuntimeDeadline | None,
    ) -> MindMapGenerateResult:
        operation = Operation.ARTIFACT_GENERATE_MIND_MAP
        source_ids = await self._data_source_ids(
            value.notebook_id,
            value.source_ids,
            operation=operation,
            deadline=deadline,
        )
        result = await self._rpc_call(
            RPCMethod.GENERATE_MIND_MAP,
            build_mind_map_params(
                list(source_ids),
                language=(get_default_language() if value.language is None else value.language),
                instructions=value.instructions,
            ),
            operation=operation,
            deadline=deadline,
            source_path=f"/notebook/{value.notebook_id}",
            allow_null=True,
            operation_variant=None,
        )
        mind_map_json = unwrap_mind_map_generation_leaf(
            result,
            method_id=RPCMethod.GENERATE_MIND_MAP.value,
            source="ArtifactsAPI",
        )
        if mind_map_json is MIND_MAP_LEAF_ABSENT:
            return MindMapGenerateResult()

        if isinstance(mind_map_json, str):
            try:
                mind_map_data: object = json.loads(mind_map_json)
            except json.JSONDecodeError:
                mind_map_data = mind_map_json
        else:
            mind_map_data = mind_map_json
            mind_map_json = json.dumps(mind_map_json)

        title = "Mind Map"
        if isinstance(mind_map_data, dict):
            name = mind_map_data.get("name")
            if isinstance(name, str) and name:
                title = name

        note_id, created_at = await self._persist_generated_mind_map(
            value.notebook_id,
            title=title,
            content=mind_map_json,
            operation=operation,
            deadline=deadline,
        )
        return MindMapGenerateResult(
            mind_map=mind_map_data,
            note_id=note_id,
            created_at=created_at,
        )


__all__ = ["StudioDataWebHandlers"]
