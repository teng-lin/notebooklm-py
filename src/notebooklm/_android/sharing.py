"""Android backend implementation of the public sharing surface."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, cast

from .._idempotency import mark_unconfirmed
from .._sharing import SharingAPI
from .._types.enums import SharePermission, ShareViewLevel
from ..exceptions import (
    NotebookNotFoundError,
    RPCError,
)
from ..types import ShareStatus
from .codecs.sharing import decode_share_status
from .session import AndroidSession
from .write_safety import call_unconfirmed_on_transport_loss

_SERVICE = "labs.language.tailwind.sharing.LabsTailwindSharingService"
GET_PROJECT_DETAILS_METHOD = f"/{_SERVICE}/GetProjectDetails"
SHARE_PROJECT_METHOD = f"/{_SERVICE}/ShareProject"


def _proto() -> Any:
    from .proto.labs.language.tailwind.sharing import sharing_pb2

    return cast(Any, sharing_pb2)


def _wire_proto() -> Any:
    from .proto.notebooklm.android.wire.v1 import sharing_pb2

    return cast(Any, sharing_pb2)


def _map_notebook_error(notebook_id: str, error: RPCError, *, method_id: str) -> RPCError:
    if error.rpc_code != 5:
        return error
    return NotebookNotFoundError(
        notebook_id,
        method_id=method_id,
        raw_response=error.raw_response,
        rpc_code=error.rpc_code,
        found_ids=error.found_ids,
        detail=str(error),
    )


SetViewLevel = Callable[[str, ShareViewLevel], Awaitable[ShareStatus]]


class AndroidSharingAPI(SharingAPI):
    """Native sharing except for the unrecovered view-level mutation."""

    def __init__(
        self,
        session: AndroidSession,
        *,
        set_view_level: SetViewLevel,
    ) -> None:
        self._transport = session
        self._set_view_level_compat = set_view_level

    async def get_status(self, notebook_id: str) -> ShareStatus:
        """Read the complete public status, including collaborators."""
        async with self._transport.operation_scope("sharing.get_status") as lease:
            return await self._get_status(notebook_id, expected_epoch=lease.epoch)

    async def _get_status(
        self,
        notebook_id: str,
        *,
        expected_epoch: int | None = None,
    ) -> ShareStatus:
        proto = _proto()
        wire = _wire_proto()
        from .upload import android_request_context

        request = proto.GetProjectDetailsRequest(
            project_id=notebook_id,
            request_context=android_request_context(),
        )
        try:
            response = await self._transport.unary(
                GET_PROJECT_DETAILS_METHOD,
                request,
                replay_safe=True,
                response_type=wire.GetProjectDetailsResponse,
                expected_epoch=expected_epoch,
            )
        except RPCError as exc:
            mapped = _map_notebook_error(
                notebook_id,
                exc,
                method_id=GET_PROJECT_DETAILS_METHOD,
            )
            if mapped is exc:
                raise
            raise mapped from exc
        return decode_share_status(
            response,
            notebook_id,
            method_id=GET_PROJECT_DETAILS_METHOD,
        )

    async def set_public(self, notebook_id: str, public: bool) -> ShareStatus:
        """Set public readability once and return a fresh status read."""
        proto = _proto()
        from .upload import android_request_context

        request = proto.ShareProjectRequest(
            project=[
                proto.ShareProjectRequest.ProjectToShare(
                    project_id=notebook_id,
                    public_document_settings=proto.ShareProjectRequest.PublicDocumentSettings(
                        is_publicly_readable=public,
                        is_discoverable=False,
                    ),
                )
            ],
            request_context=android_request_context(),
        )
        async with self._transport.operation_scope("sharing.set_public") as lease:
            try:
                await call_unconfirmed_on_transport_loss(
                    lambda: self._transport.unary(
                        SHARE_PROJECT_METHOD,
                        request,
                        replay_safe=False,
                        response_type=proto.ShareProjectResponse,
                        expected_epoch=lease.epoch,
                    )
                )
            except RPCError as exc:
                mapped = _map_notebook_error(notebook_id, exc, method_id=SHARE_PROJECT_METHOD)
                if mapped is exc:
                    raise
                raise mapped from exc
            try:
                return await self._get_status(notebook_id, expected_epoch=lease.epoch)
            except Exception as error:
                raise mark_unconfirmed(error) from None

    async def set_view_level(
        self,
        notebook_id: str,
        level: ShareViewLevel,
    ) -> ShareStatus:
        return await self._set_view_level_compat(notebook_id, level)

    async def set_users(
        self,
        notebook_id: str,
        grants: list[tuple[str, SharePermission]],
        notify: bool = True,
        welcome_message: str = "",
    ) -> ShareStatus:
        if not grants:
            raise ValueError("Must provide at least one user grant")
        seen: set[str] = set()
        for email, permission in grants:
            if permission == SharePermission.OWNER:
                raise ValueError("Cannot assign OWNER permission")
            if permission == SharePermission._REMOVE:
                raise ValueError("Use remove_user() instead")
            if email in seen:
                raise ValueError(
                    f"Duplicate email in grants: {email!r}. The backend silently "
                    "ignores a repeated grantee instead of applying either entry; "
                    "send one grant per user."
                )
            seen.add(email)
        return await self._mutate_users(
            notebook_id,
            grants,
            notify=notify,
            omit_message=not bool(welcome_message),
            welcome_message=welcome_message,
            operation="sharing.set_users",
        )

    async def remove_user(self, notebook_id: str, email: str) -> ShareStatus:
        return await self._mutate_users(
            notebook_id,
            [(email, SharePermission._REMOVE)],
            notify=False,
            omit_message=False,
            welcome_message="",
            operation="sharing.remove_user",
        )

    async def _mutate_users(
        self,
        notebook_id: str,
        grants: list[tuple[str, SharePermission]],
        *,
        notify: bool,
        omit_message: bool,
        welcome_message: str,
        operation: str,
    ) -> ShareStatus:
        proto = _proto()
        from .upload import android_request_context

        project = proto.ShareProjectRequest.ProjectToShare(
            project_id=notebook_id,
            user_permissions=[
                proto.ShareProjectRequest.UserPermission(
                    email=email,
                    permission=permission.value,
                )
                for email, permission in grants
            ],
            share_message=proto.ShareProjectRequest.ShareMessage(
                omit_message=omit_message,
                message=welcome_message,
            ),
        )
        request = proto.ShareProjectRequest(
            project=[project],
            notify=notify,
            request_context=android_request_context(),
        )
        async with self._transport.operation_scope(operation) as lease:
            try:
                await call_unconfirmed_on_transport_loss(
                    lambda: self._transport.unary(
                        SHARE_PROJECT_METHOD,
                        request,
                        replay_safe=False,
                        response_type=proto.ShareProjectResponse,
                        expected_epoch=lease.epoch,
                    )
                )
            except RPCError as exc:
                mapped = _map_notebook_error(notebook_id, exc, method_id=SHARE_PROJECT_METHOD)
                if mapped is exc:
                    raise
                raise mapped from exc
            try:
                return await self._get_status(notebook_id, expected_epoch=lease.epoch)
            except Exception as error:
                # Even a confirmed ShareProject response followed by a failed
                # status read must not look safe to retry: notify=True may
                # already have delivered invitation email.
                raise mark_unconfirmed(error) from None


__all__ = [
    "AndroidSharingAPI",
    "GET_PROJECT_DETAILS_METHOD",
    "SHARE_PROJECT_METHOD",
    "SetViewLevel",
]
