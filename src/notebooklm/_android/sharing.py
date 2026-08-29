"""Android backend implementation of the evidence-qualified B6 sharing surface."""

from __future__ import annotations

from typing import Any, NoReturn, cast

from .._sharing import SharingAPI
from .._types.enums import SharePermission, ShareViewLevel
from ..exceptions import RPCError
from ..types import ShareStatus
from .codecs.notebooks import map_get_project_error
from .codecs.sharing import decode_share_status
from .errors import unsupported_operation
from .proto.labs.language.tailwind.sharing import sharing_pb2 as exact_sharing_pb2
from .proto.notebooklm.android.wire.v1 import sharing_pb2
from .session import AndroidSession

_PROTO = cast(Any, exact_sharing_pb2)
_WIRE = cast(Any, sharing_pb2)

_SERVICE = "labs.language.tailwind.sharing.LabsTailwindSharingService"
GET_PROJECT_DETAILS_METHOD = f"/{_SERVICE}/GetProjectDetails"
SHARE_PROJECT_METHOD = f"/{_SERVICE}/ShareProject"


def _reject(operation: str) -> NoReturn:
    unsupported_operation(operation)
    raise AssertionError("unsupported_operation returned")  # pragma: no cover


class AndroidSharingAPI(SharingAPI):
    """Android public-link reads and mutations for the directly tested graph."""

    def __init__(self, session: AndroidSession) -> None:
        self._transport = session

    async def get_status(self, notebook_id: str) -> ShareStatus:
        """Read only the byte-proven public settings, cap, and policy fields."""
        return await self._get_status(notebook_id)

    async def _get_status(
        self,
        notebook_id: str,
        *,
        expected_epoch: int | None = None,
    ) -> ShareStatus:
        request = _PROTO.GetProjectDetailsRequest(project_id=notebook_id)
        try:
            response = await self._transport.unary(
                GET_PROJECT_DETAILS_METHOD,
                request,
                replay_safe=True,
                response_type=_WIRE.GetProjectDetailsResponse,
                expected_epoch=expected_epoch,
            )
        except RPCError as exc:
            mapped = map_get_project_error(
                notebook_id,
                exc,
                method_id=GET_PROJECT_DETAILS_METHOD,
            )
            if mapped is exc:
                raise
            raise mapped from exc
        return decode_share_status(response, notebook_id)

    async def set_public(self, notebook_id: str, public: bool) -> ShareStatus:
        """Set public readability once and return a fresh status read."""
        request = _PROTO.ShareProjectRequest(
            project=[
                _PROTO.ShareProjectRequest.ProjectToShare(
                    project_id=notebook_id,
                    public_document_settings=_PROTO.ShareProjectRequest.PublicDocumentSettings(
                        is_publicly_readable=public,
                        is_discoverable=False,
                    ),
                )
            ]
        )
        async with self._transport.operation_scope("sharing.set_public") as lease:
            try:
                await self._transport.unary(
                    SHARE_PROJECT_METHOD,
                    request,
                    replay_safe=False,
                    response_type=_WIRE.EmptyResponse,
                    expected_epoch=lease.epoch,
                )
            except RPCError as exc:
                mapped = map_get_project_error(notebook_id, exc, method_id=SHARE_PROJECT_METHOD)
                if mapped is exc:
                    raise
                raise mapped from exc
            return await self._get_status(notebook_id, expected_epoch=lease.epoch)

    async def set_view_level(
        self,
        notebook_id: str,
        level: ShareViewLevel,
    ) -> ShareStatus:
        _reject("sharing.set_view_level")

    async def set_users(
        self,
        notebook_id: str,
        grants: list[tuple[str, SharePermission]],
        notify: bool = True,
        welcome_message: str = "",
    ) -> ShareStatus:
        _reject("sharing.set_users")

    async def remove_user(self, notebook_id: str, email: str) -> ShareStatus:
        _reject("sharing.remove_user")


__all__ = [
    "AndroidSharingAPI",
    "GET_PROJECT_DETAILS_METHOD",
    "SHARE_PROJECT_METHOD",
]
