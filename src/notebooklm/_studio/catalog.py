"""Semantic Studio catalog over the neutral backend port.

A complete Studio listing is two reads, not one: interactive artifacts come
from ``artifact.catalog`` and note-backed mind maps live in the note
collection behind ``mind_map.list``, which ``note.list`` deliberately filters
out.  This module owns that merge and, with it, ADR-0019 Rule 3's
partial-availability policy: an ordinary RPC failure in the *supplemental*
mind-map read leaves the Studio artifacts that did load, while a decoding
failure or a transport family the policy never covered still surfaces.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .._backend import (
    BackendAdapter,
    BackendError,
    BackendErrorReason,
    rebind_operation,
    require_leaves,
)
from .._deadline import RuntimeDeadline, RuntimeDeadlineFactory
from .._operations import Operation
from .._projectors import project_artifact
from .._records import (
    ARTIFACT_CATALOG_DEF,
    MIND_MAP_LIST_DEF,
    SUPPLEMENTAL_TRANSPORT_FAILURE,
    ArtifactCatalogInput,
    ArtifactRecord,
    MindMapListInput,
    MindMapRecord,
)
from .classifiers import matches_artifact_family

if TYPE_CHECKING:
    from ..types import Artifact

# The listing surface's historical logger name: the partial-availability
# warning is observable and tests capture it on this exact logger.
logger = logging.getLogger("notebooklm._artifact.listing")

#: Reasons the supplemental mind-map read may fail with while the catalog still
#: returns what loaded.  This is the neutral spelling of the net that guarded
#: the merge below the port — every ``RPCError`` except the decoding family —
#: so ``DECODING`` and ``UNKNOWN_RPC_METHOD`` are absent (schema drift is not a
#: transient outage, #1344) and so are ``NETWORK`` and ``TIMEOUT``, which
#: reached the caller as ``NetworkError``/``RPCTimeoutError`` and were never
#: swallowed.  The one raw transport leaf the net did cover arrives tagged with
#: :data:`SUPPLEMENTAL_TRANSPORT_FAILURE` instead, so recognising it does not
#: require widening this set to every network failure.
_PARTIAL_MIND_MAP_FAILURE_REASONS = frozenset(
    {
        BackendErrorReason.AUTH,
        BackendErrorReason.CLIENT,
        BackendErrorReason.RATE_LIMIT,
        BackendErrorReason.RESPONSE_TOO_LARGE,
        BackendErrorReason.RPC,
        BackendErrorReason.SERVER,
    }
)


def _mind_map_artifact(record: MindMapRecord) -> ArtifactRecord:
    """Present one note-backed mind map as a catalog row.

    Note-backed mind maps have no lifecycle of their own: the row exists only
    once its content is written, so the listing has always reported them as
    completed.
    """

    return ArtifactRecord(
        id=record.id,
        title=record.title,
        family="mind_map",
        status="completed",
        created_at=record.created_at,
    )


class StudioCatalog:
    """List and select complete heterogeneous Studio records."""

    __slots__ = ("_backend", "_deadline_factory")

    def __init__(
        self,
        backend: BackendAdapter,
        *,
        deadline_factory: RuntimeDeadlineFactory | None = None,
    ) -> None:
        self._backend = backend
        self._deadline_factory = deadline_factory

    async def list(
        self,
        notebook_id: str,
        family: str | None = None,
        *,
        deadline: RuntimeDeadline | None = None,
    ) -> list[Artifact]:
        records = await self.list_records(notebook_id, family, deadline=deadline)
        return [project_artifact(record) for record in records]

    async def list_records(
        self,
        notebook_id: str,
        family: str | None = None,
        *,
        deadline: RuntimeDeadline | None = None,
    ) -> tuple[ArtifactRecord, ...]:
        """Return neutral records for family services without a second fetch."""

        records = await self._catalog_records(
            notebook_id,
            Operation.ARTIFACT_LIST,
            deadline=deadline,
            # A request for one non-mind-map family cannot be answered by the
            # note collection, so the merge is skipped entirely rather than
            # fetched and filtered away.
            include_mind_maps=family in {None, "mind_map"},
        )
        return tuple(record for record in records if matches_artifact_family(record, family))

    async def get_record(
        self,
        notebook_id: str,
        artifact_id: str,
        *,
        deadline: RuntimeDeadline | None = None,
    ) -> ArtifactRecord | None:
        """Return one complete neutral row without public projection."""

        records = await self._catalog_records(
            notebook_id, Operation.ARTIFACT_GET, deadline=deadline, include_mind_maps=True
        )
        return next((record for record in records if record.id == artifact_id), None)

    async def get_or_none(
        self,
        notebook_id: str,
        artifact_id: str,
        *,
        deadline: RuntimeDeadline | None = None,
    ) -> Artifact | None:
        record = await self.get_record(notebook_id, artifact_id, deadline=deadline)
        return None if record is None else project_artifact(record)

    async def _catalog_records(
        self,
        notebook_id: str,
        workflow: Operation,
        *,
        deadline: RuntimeDeadline | None,
        include_mind_maps: bool,
    ) -> tuple[ArtifactRecord, ...]:
        """One Studio catalog read plus the conditional note-backed merge.

        A leaf failure is re-attributed to ``workflow`` so the public exception
        identity is the one the composite row published — most visibly a
        deadline expiry, whose message names its operation.
        """

        try:
            return await self._merged_records(
                notebook_id, deadline=deadline, include_mind_maps=include_mind_maps
            )
        except BackendError as error:
            raise rebind_operation(error, workflow) from error.__cause__

    async def _merged_records(
        self,
        notebook_id: str,
        *,
        deadline: RuntimeDeadline | None,
        include_mind_maps: bool,
    ) -> tuple[ArtifactRecord, ...]:
        require_leaves(self._backend, ARTIFACT_CATALOG_DEF.key)
        # The reads share one budget, the same way the row that owned this merge
        # received a single client-timeout deadline for both its natives.
        deadline = self._start_deadline(deadline)
        catalog = await self._backend.invoke(
            ARTIFACT_CATALOG_DEF,
            ArtifactCatalogInput(notebook_id),
            deadline=deadline,
        )
        records = list(catalog.artifacts)
        if not include_mind_maps:
            return tuple(records)
        require_leaves(self._backend, MIND_MAP_LIST_DEF.key)
        try:
            listed = await self._backend.invoke(
                MIND_MAP_LIST_DEF,
                MindMapListInput(notebook_id, supplemental=True),
                deadline=deadline,
            )
        except BackendError as error:
            if not _is_partial_mind_map_failure(error):
                raise
            logger.warning("Failed to fetch mind maps: %s", error)
            return tuple(records)
        records.extend(_mind_map_artifact(record) for record in listed.mind_maps)
        return tuple(records)

    def _start_deadline(self, deadline: RuntimeDeadline | None) -> RuntimeDeadline | None:
        """Mint one workflow deadline unless the caller supplied its own."""

        if deadline is not None or self._deadline_factory is None:
            return deadline
        return self._deadline_factory.start()


def _is_partial_mind_map_failure(error: BackendError) -> bool:
    """Whether ADR-0019 Rule 3 covers this supplemental-read failure."""

    if error.reason in _PARTIAL_MIND_MAP_FAILURE_REASONS:
        return True
    diagnostics = error.diagnostics or {}
    return bool(diagnostics.get(SUPPLEMENTAL_TRANSPORT_FAILURE))


__all__ = ["StudioCatalog"]
