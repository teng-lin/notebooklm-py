"""Web workflow bindings for the remaining Source variants.

Since P9.3 the source leaves are codec rows and since P9.4b the source-add family
are custom rows, both in ``_web/bindings/sources.py``; only ``SOURCE_UPDATE`` and
the composite snapshot helper it hydrates through remain here until its P9.2 hoist.
"""

from __future__ import annotations

import logging
from types import MappingProxyType
from typing import Any

from .._backend import BackendError, BackendErrorReason
from .._deadline import RuntimeDeadline
from .._operations import Operation
from .._records import (
    SourceRecord,
    SourceUpdateInput,
    SourceUpdateResult,
)
from ..rpc import RPCMethod
from .codec.sources import (
    decode_source_record,
    decode_source_snapshot,
    encode_source_snapshot,
    encode_update_source,
)
from .studio_facade import StudioFacadeWebHandlers

source_logger = logging.getLogger("notebooklm").getChild("_sources")


class SourceVariantWebHandlers(StudioFacadeWebHandlers):
    """Remaining Source workflows mixed into the composed web backend."""

    _executor: Any

    async def _source_snapshot_records(
        self,
        notebook_id: str,
        *,
        operation: Operation,
        deadline: RuntimeDeadline | None,
        strict: bool = False,
        outcome_unknown_on_expiry: bool = False,
    ) -> tuple[SourceRecord, ...]:
        """Fetch and decode one recency-writing notebook source snapshot."""

        payload = await self._rpc_call(
            RPCMethod.GET_NOTEBOOK,
            encode_source_snapshot(notebook_id),
            operation=operation,
            deadline=deadline,
            source_path=f"/notebook/{notebook_id}",
            outcome_unknown_on_expiry=outcome_unknown_on_expiry,
        )
        return decode_source_snapshot(
            notebook_id,
            payload,
            strict=strict,
            logger=source_logger,
        )

    async def _source_select_record(
        self,
        notebook_id: str,
        source_id: str,
        *,
        operation: Operation,
        deadline: RuntimeDeadline | None,
        outcome_unknown_on_expiry: bool = False,
    ) -> SourceRecord | None:
        """Select one exact-id record from a composite's own snapshot read.

        The ``source.get`` leaf is a codec row since P9.3; this helper exists
        because composites attribute the read to themselves and thread
        ``outcome_unknown_on_expiry`` through it, which a codec row cannot.
        """
        records = await self._source_snapshot_records(
            notebook_id,
            operation=operation,
            deadline=deadline,
            outcome_unknown_on_expiry=outcome_unknown_on_expiry,
        )
        return next((source for source in records if source.id == source_id), None)

    async def _source_update(
        self,
        value: SourceUpdateInput,
        *,
        deadline: RuntimeDeadline | None,
    ) -> SourceUpdateResult:
        payload = await self._rpc_call(
            RPCMethod.UPDATE_SOURCE,
            encode_update_source(value.source_id, value.new_title),
            operation=Operation.SOURCE_UPDATE,
            deadline=deadline,
            source_path=f"/notebook/{value.notebook_id}",
            allow_null=True,
        )
        if payload:
            return SourceUpdateResult(
                decode_source_record(payload, method=RPCMethod.UPDATE_SOURCE)
                if value.return_object
                else None
            )

        hydrated = await self._source_select_record(
            value.notebook_id,
            value.source_id,
            deadline=deadline,
            operation=Operation.SOURCE_UPDATE,
            outcome_unknown_on_expiry=True,
        )
        if hydrated is None:
            raise BackendError(
                message=f"Source not found: {value.source_id}",
                operation=Operation.SOURCE_UPDATE,
                diagnostics=MappingProxyType(
                    {
                        "source_id": value.source_id,
                        "method_id": RPCMethod.UPDATE_SOURCE.value,
                        "raw_response": None,
                    }
                ),
                reason=BackendErrorReason.SOURCE_NOT_FOUND,
            )
        return SourceUpdateResult(hydrated if value.return_object else None)
