"""Web workflow binding for the note-backed mind-map generation compatibility row.

Since P9.3 the Drive export leaf (``ARTIFACT_EXPORT``) is a codec row in
``_web/bindings/studio.py``; since P9.4b every other generate family is a
``CustomBinding`` row there as well.  Only ``ARTIFACT_GENERATE_MIND_MAP`` — the
compatibility composite that persists the generated tree through the legacy
note seam (gate table §3.15) — remains a handler here, and this class is the
root of what is left of the handler chain.
"""

from __future__ import annotations

import json
from datetime import datetime
from types import MappingProxyType
from typing import Any

from .._artifact.payloads import build_mind_map_params
from .._backend import BackendError, BackendErrorReason
from .._deadline import RuntimeDeadline
from .._env import get_default_language
from .._operations import Operation
from .._records import MindMapGenerateInput, MindMapGenerateResult
from .._row_adapters.artifacts import MIND_MAP_LEAF_ABSENT, unwrap_mind_map_generation_leaf
from ..rpc import RPCMethod
from .codec.source_ids import (
    SourceIdDiagnostics,
    decode_notebook_source_ids,
    encode_notebook_source_read,
)


class StudioDataWebHandlers:
    """Mind-map generation compatibility handler mixed into the web backend."""

    async def _rpc_call(
        self,
        method: RPCMethod,
        params: list[Any],
        *,
        operation: Operation,
        deadline: RuntimeDeadline | None,
        source_path: str = "/",
        allow_null: bool = False,
        _is_retry: bool = False,
        disable_internal_retries: bool = False,
        operation_variant: str | None = None,
        raise_on_null_status: bool = False,
        outcome_unknown_on_expiry: bool = False,
        attempt_timeout: float | None = None,
    ) -> Any:
        """Invoke one native RPC; implemented by the composed web backend."""

        raise NotImplementedError

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

    @staticmethod
    def _artifact_feature_unavailable(
        operation: Operation,
        artifact_type: str,
    ) -> BackendError:
        """Closed unavailable error for the interactive mind-map kickoff on the head."""
        return BackendError(
            message=f"{artifact_type.replace('_', ' ').capitalize()} generation is unavailable",
            operation=operation,
            diagnostics=MappingProxyType(
                {
                    "artifact_type": artifact_type,
                    "method_id": RPCMethod.CREATE_ARTIFACT.value,
                    "raw_response": None,
                }
            ),
            reason=BackendErrorReason.ARTIFACT_FEATURE_UNAVAILABLE,
        )

    @staticmethod
    def _audio_source_ids(notebook: object) -> tuple[str, ...]:
        """Silent source-id resolution the head's mind-map kickoffs still reach."""
        return decode_notebook_source_ids(
            notebook, notebook_id="", diagnostics=SourceIdDiagnostics.SILENT
        )

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
        payload = encode_notebook_source_read(notebook_id)
        notebook = await self._rpc_call(
            RPCMethod.GET_NOTEBOOK,
            payload.params,
            operation=operation,
            deadline=deadline,
            source_path=payload.source_path,
        )
        return decode_notebook_source_ids(
            notebook, notebook_id=notebook_id, diagnostics=SourceIdDiagnostics.WARN
        )

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
