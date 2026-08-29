"""Android backend implementation of the public sharing surface."""

from __future__ import annotations

from typing import Any, cast

from .._sharing import SharingAPI
from .._types.enums import SharePermission, ShareViewLevel
from ..exceptions import NotebookNotFoundError, RPCError
from ..types import ShareStatus
from .codecs.sharing import decode_share_status
from .session import AndroidSession

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


class AndroidSharingAPI(SharingAPI):
    """Android public-link mutations with Web compatibility for omitted fields."""

    def __init__(self, session: AndroidSession, *, compatibility: SharingAPI) -> None:
        self._transport = session
        self._compatibility = compatibility

    async def get_status(self, notebook_id: str) -> ShareStatus:
        """Read the complete public status, including collaborators."""
        return await self._compatibility.get_status(notebook_id)

    async def _get_status(
        self,
        notebook_id: str,
        *,
        expected_epoch: int | None = None,
    ) -> ShareStatus:
        proto = _proto()
        wire = _wire_proto()
        request = proto.GetProjectDetailsRequest(project_id=notebook_id)
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
        return decode_share_status(response, notebook_id)

    async def set_public(self, notebook_id: str, public: bool) -> ShareStatus:
        """Set public readability once and return a fresh status read."""
        proto = _proto()
        request = proto.ShareProjectRequest(
            project=[
                proto.ShareProjectRequest.ProjectToShare(
                    project_id=notebook_id,
                    public_document_settings=proto.ShareProjectRequest.PublicDocumentSettings(
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
                    response_type=proto.ShareProjectResponse,
                    expected_epoch=lease.epoch,
                )
            except RPCError as exc:
                mapped = _map_notebook_error(notebook_id, exc, method_id=SHARE_PROJECT_METHOD)
                if mapped is exc:
                    raise
                raise mapped from exc
            # GetProjectDetails' recovered Android descriptor omits its
            # collaborator row. The compatibility read preserves the complete
            # public ShareStatus contract after the native public-link write.
            self._transport.assert_epoch(lease.epoch)
            return await self._compatibility.get_status(notebook_id)

    async def set_view_level(
        self,
        notebook_id: str,
        level: ShareViewLevel,
    ) -> ShareStatus:
        return await self._compatibility.set_view_level(notebook_id, level)

    async def set_users(
        self,
        notebook_id: str,
        grants: list[tuple[str, SharePermission]],
        notify: bool = True,
        welcome_message: str = "",
    ) -> ShareStatus:
        return await self._compatibility.set_users(
            notebook_id,
            grants,
            notify=notify,
            welcome_message=welcome_message,
        )

    async def remove_user(self, notebook_id: str, email: str) -> ShareStatus:
        return await self._compatibility.remove_user(notebook_id, email)


__all__ = [
    "AndroidSharingAPI",
    "GET_PROJECT_DETAILS_METHOD",
    "SHARE_PROJECT_METHOD",
]
