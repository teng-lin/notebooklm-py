"""Concrete batchexecute sharing backend."""

import logging
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from .._sharing import SharingAPI
from .._types.enums import ShareAccess, SharePermission, ShareViewLevel
from ..rpc import RPCMethod
from ..types import ShareStatus
from .contracts import RpcCaller

logger = logging.getLogger("notebooklm._sharing")


class WebSharingAPI(SharingAPI):
    """Operations for notebook sharing.

    Provides methods for querying and modifying notebook sharing settings,
    including public link access and user-specific sharing.

    Usage:
        async with NotebookLMClient.from_storage() as client:
            # Get current status
            status = await client.sharing.get_status(notebook_id)

            # Enable public sharing
            await client.sharing.set_public(notebook_id, True)

            # Share with user
            await client.sharing.add_user(
                notebook_id,
                "user@example.com",
                SharePermission.VIEWER,
                notify=True,
                welcome_message="Welcome to my notebook!"
            )
    """

    def __init__(self, rpc: RpcCaller):
        """Initialize the sharing API.

        Args:
            rpc: RPC dispatch surface (typically the shared client session).
        """
        self._rpc = rpc

    async def get_status(self, notebook_id: str) -> ShareStatus:
        """Get current sharing configuration.

        Args:
            notebook_id: The notebook ID.

        Returns:
            ShareStatus with current sharing state and user list.
        """
        logger.debug("Getting share status for notebook: %s", notebook_id)
        params = [notebook_id, [2]]
        result = await self._rpc.rpc_call(
            RPCMethod.GET_SHARE_STATUS,
            params,
            source_path=f"/notebook/{notebook_id}",
        )
        return ShareStatus.from_api_response(result, notebook_id)

    async def set_public(
        self,
        notebook_id: str,
        public: bool,
    ) -> ShareStatus:
        """Enable or disable public link sharing.

        Args:
            notebook_id: The notebook ID.
            public: True for anyone with link, False for restricted.

        Returns:
            Updated ShareStatus.

        Note:
            This method makes two sequential RPC calls. The returned status
            reflects the state immediately after the operation but may not
            include concurrent changes from other clients.
        """
        logger.debug("Setting notebook %s public=%s", notebook_id, public)
        access = ShareAccess.ANYONE_WITH_LINK if public else ShareAccess.RESTRICTED
        params = [
            [[notebook_id, None, [access.value], [access.value, ""]]],
            1,
            None,
            [2],
        ]
        await self._rpc.rpc_call(
            RPCMethod.SHARE_NOTEBOOK,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )
        return await self.get_status(notebook_id)

    async def set_view_level(
        self,
        notebook_id: str,
        level: ShareViewLevel,
    ) -> ShareStatus:
        """Set what viewers can access.

        Args:
            notebook_id: The notebook ID.
            level: FULL_NOTEBOOK or CHAT_ONLY.

        Returns:
            Updated ShareStatus with the new view_level.

        Note:
            The GET_SHARE_STATUS API does not return view_level, so the
            returned status includes the view_level we just set rather
            than fetching it from the API.
        """
        logger.debug("Setting notebook %s view level to %s", notebook_id, level.name)
        params = [
            notebook_id,
            [[None, None, None, None, None, None, None, None, [[level.value]]]],
        ]
        await self._rpc.rpc_call(
            RPCMethod.RENAME_NOTEBOOK,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
            # #2290: a status-tagged null is a server rejection, not an empty success.
            raise_on_null_status=True,
        )
        # Fetch current status and override view_level with what we just set
        # (GET_SHARE_STATUS doesn't return view_level)
        status = await self.get_status(notebook_id)
        # ``replace`` rather than a field-by-field rebuild: the old form listed
        # six fields explicitly and silently dropped every field added to
        # ``ShareStatus`` afterwards, so this path reported ``None`` for the
        # collaborator cap and the public-sharing policy gate that
        # ``get_status`` had just decoded (#2130). Copying by construction ties
        # the set of preserved fields to the dataclass itself, so a future field
        # cannot regress the same way.
        return replace(status, view_level=level)

    @staticmethod
    def _share_params(
        notebook_id: str,
        entries: list[tuple[str, SharePermission]],
        *,
        notify: bool,
        message_block: list,
    ) -> list:
        """Build the ``SHARE_NOTEBOOK`` envelope for a batch of permission entries.

        The single wire-shape site for every permission mutation — grants and
        removals alike, since a removal is just ``_REMOVE`` in the same entry
        slot. Position-sensitive: see ``docs/rpc-reference.md``.

        ``message_block`` is passed in rather than derived from a welcome message,
        because the two callers do not agree on it: the grant path sends
        ``[0, msg]`` with a message and ``[1, ""]`` without, while removal has
        always sent ``[0, ""]``. Deriving it here would silently rewrite the
        removal payload that ``test_remove_user`` pins.

        Policy (which permissions a caller may send, and whether the batch is
        coherent) lives with the callers, not here, so a future ``remove_users()``
        can reuse the encoder without inheriting grant-side validation.
        """
        return [
            [
                [
                    notebook_id,
                    [[email, None, permission.value] for email, permission in entries],
                    None,
                    message_block,
                ]
            ],
            1 if notify else 0,
            None,
            [2],
        ]

    async def set_users(
        self,
        notebook_id: str,
        grants: list[tuple[str, SharePermission]],
        notify: bool = True,
        welcome_message: str = "",
    ) -> ShareStatus:
        """Set several users' permissions on a notebook in one request.

        This is an **upsert**, not an add: an email that is not shared yet is
        added, and an email that already has access has its permission replaced.
        That is the backend's own behaviour — confirmed live — and it is why the
        singular :meth:`add_user` and :meth:`update_user` are both wrappers over
        this method rather than distinct operations.

        ``notify`` and ``welcome_message`` apply to the whole call, not per grant.

        Duplicate emails are rejected before the request is issued. The backend
        answers a batch containing the same grantee twice with success while
        silently leaving that user's permission unchanged, so there is no
        first-wins or last-wins rule to honour — sending one would be a lie.

        The comparison is **exact**, matching what was actually probed (the same
        address twice). Addresses differing only in case are left alone: RFC 5321
        makes the local part case-sensitive, only the domain is not, so folding
        case here would reject two addresses the server may well treat as
        distinct identities. If they do resolve to one account they hit the same
        silent no-op the backend already has; establishing that needs a live
        probe, not a guess in the client.

        Args:
            notebook_id: The notebook ID.
            grants: Email and permission pairs. Permissions must be EDITOR or VIEWER.
            notify: Send email notifications to all users.
            welcome_message: Optional welcome message sent to all users.

        Returns:
            Updated ShareStatus.

        Raises:
            ValueError: If grants is empty, contains a duplicate email, or a
                permission is OWNER or _REMOVE.
        """
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

        # Keep the grantee detail the single-user path used to log: a typoed
        # address is exactly what this line gets read for.
        logger.debug(
            "Setting %d user permission(s) on notebook %s: %s",
            len(grants),
            notebook_id,
            [(email, permission.name) for email, permission in grants],
        )

        await self._rpc.rpc_call(
            RPCMethod.SHARE_NOTEBOOK,
            self._share_params(
                notebook_id,
                grants,
                notify=notify,
                message_block=[0 if welcome_message else 1, welcome_message],
            ),
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )
        return await self.get_status(notebook_id)

    async def remove_user(
        self,
        notebook_id: str,
        email: str,
    ) -> ShareStatus:
        """Remove a user's access to the notebook.

        Singular by design. A batch of ``_REMOVE`` entries only works when every
        target is currently shared: if any requested email is already absent the
        backend silently drops the whole request — including the users that *are*
        present — and reports no failure. A plural removal therefore needs a
        share-status preflight and post-verification rather than a wider entry
        list; see ``docs/rpc-reference.md``.

        Args:
            notebook_id: The notebook ID.
            email: User's email address to remove.

        Returns:
            Updated ShareStatus.
        """
        logger.debug("Removing user %s from notebook %s", email, notebook_id)
        # ``[0, ""]``, not the grant path's ``[1, ""]`` — the captured removal
        # payload has always sent flag 0 with an empty message.
        params = self._share_params(
            notebook_id,
            [(email, SharePermission._REMOVE)],
            notify=False,
            message_block=[0, ""],
        )
        await self._rpc.rpc_call(
            RPCMethod.SHARE_NOTEBOOK,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )
        return await self.get_status(notebook_id)


class ShareManager:
    """Legacy ``SHARE_ARTIFACT`` manager kept behind share URL internals."""

    def __init__(
        self,
        rpc: RpcCaller,
        *,
        share_url_builder: Callable[[str, str | None], str],
    ) -> None:
        self._rpc = rpc
        self._share_url_builder = share_url_builder

    async def share(
        self, notebook_id: str, public: bool = True, artifact_id: str | None = None
    ) -> dict[str, Any]:
        """Set/update legacy public share-link state through ``SHARE_ARTIFACT``."""
        share_options = [1] if public else [0]
        params: list[Any] = [share_options, notebook_id]
        if artifact_id:
            params.append(artifact_id)

        await self._rpc.rpc_call(
            RPCMethod.SHARE_ARTIFACT,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )

        return {
            "public": public,
            "url": self.get_share_url(notebook_id, artifact_id) if public else None,
            "artifact_id": artifact_id,
        }

    def get_share_url(self, notebook_id: str, artifact_id: str | None = None) -> str:
        """Return the legacy share URL without toggling server-side sharing."""
        return self._share_url_builder(notebook_id, artifact_id)


__all__ = ["ShareManager", "WebSharingAPI"]
