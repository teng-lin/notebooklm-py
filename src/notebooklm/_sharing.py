"""Sharing operations API."""

import logging

from ._core import ClientCore
from .rpc import RPCMethod
from .rpc.types import ShareAccess, SharePermission, ShareViewLevel
from .types import ShareStatus

logger = logging.getLogger(__name__)


class SharingAPI:
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

    def __init__(self, core: ClientCore):
        """Initialize the sharing API.

        Args:
            core: The core client infrastructure.
        """
        self._core = core

    async def get_status(self, notebook_id: str) -> ShareStatus:
        """Get current sharing configuration.

        Args:
            notebook_id: The notebook ID.

        Returns:
            ShareStatus with current sharing state and user list.
        """
        logger.debug("Getting share status for notebook: %s", notebook_id)
        params = [notebook_id, [2]]
        result = await self._core.rpc_call(
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
        await self._core.rpc_call(
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
    ) -> None:
        """Set what viewers can access.

        Args:
            notebook_id: The notebook ID.
            level: FULL_NOTEBOOK or CHAT_ONLY.
        """
        logger.debug("Setting notebook %s view level to %s", notebook_id, level.name)
        params = [
            notebook_id,
            [[None, None, None, None, None, None, None, None, [[level.value]]]],
        ]
        await self._core.rpc_call(
            RPCMethod.RENAME_NOTEBOOK,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )

    async def add_user(
        self,
        notebook_id: str,
        email: str,
        permission: SharePermission = SharePermission.VIEWER,
        notify: bool = True,
        welcome_message: str = "",
    ) -> ShareStatus:
        """Share notebook with a user.

        Args:
            notebook_id: The notebook ID.
            email: User's email address.
            permission: EDITOR or VIEWER (cannot assign OWNER).
            notify: Send email notification to user.
            welcome_message: Optional welcome message for the user.

        Returns:
            Updated ShareStatus.

        Raises:
            ValueError: If permission is OWNER or _REMOVE.
        """
        if permission == SharePermission.OWNER:
            raise ValueError("Cannot assign OWNER permission")
        if permission == SharePermission._REMOVE:
            raise ValueError("Use remove_user() instead")

        logger.debug(
            "Adding user %s to notebook %s with permission %s",
            email,
            notebook_id,
            permission.name,
        )

        message_flag = 0 if welcome_message else 1
        notify_flag = 1 if notify else 0

        params = [
            [
                [
                    notebook_id,
                    [[email, None, permission.value]],
                    None,
                    [message_flag, welcome_message],
                ]
            ],
            notify_flag,
            None,
            [2],
        ]
        await self._core.rpc_call(
            RPCMethod.SHARE_NOTEBOOK,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )
        return await self.get_status(notebook_id)

    async def update_user(
        self,
        notebook_id: str,
        email: str,
        permission: SharePermission,
    ) -> ShareStatus:
        """Update a user's permission level.

        Args:
            notebook_id: The notebook ID.
            email: User's email address.
            permission: New permission level (EDITOR or VIEWER).

        Returns:
            Updated ShareStatus.
        """
        logger.debug(
            "Updating user %s permission to %s in notebook %s",
            email,
            permission.name,
            notebook_id,
        )
        # Same RPC as add_user, just updates existing user
        return await self.add_user(notebook_id, email, permission, notify=False)

    async def remove_user(
        self,
        notebook_id: str,
        email: str,
    ) -> ShareStatus:
        """Remove a user's access to the notebook.

        Args:
            notebook_id: The notebook ID.
            email: User's email address to remove.

        Returns:
            Updated ShareStatus.
        """
        logger.debug("Removing user %s from notebook %s", email, notebook_id)
        params = [
            [[notebook_id, [[email, None, SharePermission._REMOVE.value]], None, [0, ""]]],
            0,
            None,
            [2],
        ]
        await self._core.rpc_call(
            RPCMethod.SHARE_NOTEBOOK,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )
        return await self.get_status(notebook_id)
