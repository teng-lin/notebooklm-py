"""Web workflow binding for data-table generation compatibility.

Since P9.3 the Drive export leaf (``ARTIFACT_EXPORT``) is a codec row in
``_web/bindings/studio.py``, and since P9.4b ``ARTIFACT_GENERATE_MIND_MAP`` is a
custom row in ``_web/bindings/mind_maps.py``; only the input-defaulting data-table
generate composite remains here.
"""

from __future__ import annotations

from .._artifact.payloads import build_data_table_artifact_params
from .._deadline import RuntimeDeadline
from .._env import get_default_language
from .._notebook_payloads import build_get_notebook_params
from .._operations import Operation
from .._records import (
    DataTableGenerateInput,
    DataTableGenerateResult,
)
from ..rpc import RPCMethod
from .studio_media import StudioMediaWebHandlers


class StudioDataWebHandlers(StudioMediaWebHandlers):
    """Reusable data-view and Drive-export handlers mixed into the web backend."""

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


__all__ = ["StudioDataWebHandlers"]
