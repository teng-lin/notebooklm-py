"""Web bindings for the remaining Artifacts compatibility operations.

Since P9.3 the Studio leaves (``ARTIFACT_EXPORT``, ``ARTIFACT_REVISE_SLIDE``,
``ARTIFACT_RETRY``, ``ARTIFACT_DELETE``, ``ARTIFACT_WAIT``, ``ARTIFACT_DOWNLOAD``)
are codec rows in ``_web/bindings/studio.py``; only the rename composite and the
catalog seam it reads back through remain here.
"""

from __future__ import annotations

from types import MappingProxyType

from .._backend import BackendError, BackendErrorReason
from .._deadline import RuntimeDeadline
from .._operations import Operation
from .._records import (
    ArtifactRecord,
    ArtifactRenameInput,
    ArtifactRenameResult,
)
from ..rpc import RPCMethod
from .settings_suggestions import SettingsSuggestionWebHandlers


class StudioFacadeWebHandlers(SettingsSuggestionWebHandlers):
    """Management composite handler mixed into the web backend."""

    async def _artifact_catalog_records(
        self,
        notebook_id: str,
        *,
        operation: Operation,
        deadline: RuntimeDeadline | None,
        include_mind_maps: bool,
        outcome_unknown_on_expiry: bool = False,
    ) -> tuple[ArtifactRecord, ...]:
        """Return catalog records from the concrete composed backend."""

        raise NotImplementedError

    async def _artifact_rename(
        self,
        value: ArtifactRenameInput,
        *,
        deadline: RuntimeDeadline | None,
    ) -> ArtifactRenameResult:
        await self._rpc_call(
            RPCMethod.RENAME_ARTIFACT,
            [[value.artifact_id, value.new_title], [["title"]]],
            operation=Operation.ARTIFACT_RENAME,
            deadline=deadline,
            source_path=f"/notebook/{value.notebook_id}",
            allow_null=True,
        )
        records = await self._artifact_catalog_records(
            value.notebook_id,
            operation=Operation.ARTIFACT_RENAME,
            deadline=deadline,
            include_mind_maps=False,
            outcome_unknown_on_expiry=True,
        )
        artifact = next((item for item in records if item.id == value.artifact_id), None)
        if artifact is None:
            raise BackendError(
                message=f"Artifact not found: {value.artifact_id}",
                operation=Operation.ARTIFACT_RENAME,
                diagnostics=MappingProxyType(
                    {
                        "artifact_id": value.artifact_id,
                        "artifact_type": None,
                        "method_id": RPCMethod.RENAME_ARTIFACT.value,
                        "raw_response": None,
                    }
                ),
                reason=BackendErrorReason.ARTIFACT_NOT_FOUND,
            )
        return ArtifactRenameResult(artifact=artifact)


__all__ = ["StudioFacadeWebHandlers"]
