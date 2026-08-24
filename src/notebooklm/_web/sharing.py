"""Web workflow bindings for notebook sharing and access control.

Since P9.3 the two leaves (``SHARING_GET``, ``LEGACY_SHARE_ARTIFACT``) are codec rows in
``_web/bindings/sharing.py``; only the three mutate-then-readback composites remain here.
"""

from __future__ import annotations

from .._deadline import RuntimeDeadline
from .._operations import Operation
from .._records import (
    ShareStatusRecord,
    ShareViewScope,
    SharingSetPublicInput,
    SharingSetPublicResult,
    SharingSetViewLevelInput,
    SharingSetViewLevelResult,
    SharingUpdateUsersInput,
    SharingUpdateUsersResult,
)
from ..rpc import RPCMethod
from .codec.sharing import (
    build_get_share_status_params,
    build_share_grants_params,
    build_share_view_level_params,
    build_share_visibility_params,
    decode_share_status,
)
from .labels import LabelSetWebHandlers


class SharingWebHandlers(LabelSetWebHandlers):
    """Reusable notebook-sharing handlers mixed into the web backend."""

    async def _sharing_status(
        self,
        notebook_id: str,
        *,
        operation: Operation,
        deadline: RuntimeDeadline | None,
        view_level: ShareViewScope | None = None,
        outcome_unknown_on_expiry: bool = False,
    ) -> ShareStatusRecord:
        result = await self._rpc_call(
            RPCMethod.GET_SHARE_STATUS,
            build_get_share_status_params(notebook_id),
            operation=operation,
            deadline=deadline,
            source_path=f"/notebook/{notebook_id}",
            outcome_unknown_on_expiry=outcome_unknown_on_expiry,
        )
        return decode_share_status(result, notebook_id, view_level=view_level)

    async def _sharing_set_public(
        self,
        value: SharingSetPublicInput,
        *,
        deadline: RuntimeDeadline | None,
    ) -> SharingSetPublicResult:
        await self._rpc_call(
            RPCMethod.SHARE_NOTEBOOK,
            build_share_visibility_params(value.notebook_id, value.public),
            operation=Operation.SHARING_SET_PUBLIC,
            deadline=deadline,
            source_path=f"/notebook/{value.notebook_id}",
            allow_null=True,
        )
        return SharingSetPublicResult(
            status=await self._sharing_status(
                value.notebook_id,
                operation=Operation.SHARING_SET_PUBLIC,
                deadline=deadline,
                outcome_unknown_on_expiry=True,
            )
        )

    async def _sharing_set_view_level(
        self,
        value: SharingSetViewLevelInput,
        *,
        deadline: RuntimeDeadline | None,
    ) -> SharingSetViewLevelResult:
        await self._rpc_call(
            RPCMethod.RENAME_NOTEBOOK,
            build_share_view_level_params(value.notebook_id, value.view_level),
            operation=Operation.SHARING_SET_VIEW_LEVEL,
            deadline=deadline,
            source_path=f"/notebook/{value.notebook_id}",
            allow_null=True,
        )
        return SharingSetViewLevelResult(
            status=await self._sharing_status(
                value.notebook_id,
                operation=Operation.SHARING_SET_VIEW_LEVEL,
                deadline=deadline,
                view_level=value.view_level,
                outcome_unknown_on_expiry=True,
            )
        )

    async def _sharing_update_users(
        self,
        value: SharingUpdateUsersInput,
        *,
        deadline: RuntimeDeadline | None,
    ) -> SharingUpdateUsersResult:
        await self._rpc_call(
            RPCMethod.SHARE_NOTEBOOK,
            build_share_grants_params(
                value.notebook_id,
                value.grants,
                notify=value.notify,
                welcome_message=value.welcome_message,
            ),
            operation=Operation.SHARING_UPDATE_USERS,
            deadline=deadline,
            source_path=f"/notebook/{value.notebook_id}",
            allow_null=True,
        )
        return SharingUpdateUsersResult(
            status=await self._sharing_status(
                value.notebook_id,
                operation=Operation.SHARING_UPDATE_USERS,
                deadline=deadline,
                outcome_unknown_on_expiry=True,
            )
        )


__all__ = ["SharingWebHandlers"]
