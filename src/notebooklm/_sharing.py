"""Backend-neutral sharing namespace contract."""

import contextlib
import logging
from abc import ABC, abstractmethod

from ._runtime.call_supervisor import OperationLease
from ._types.enums import SharePermission, ShareViewLevel
from .types import ShareStatus

logger = logging.getLogger(__name__)


class SharingAPI(ABC):
    """Operations for notebook sharing."""

    def _operation_scope(
        self, label: str
    ) -> contextlib.AbstractAsyncContextManager[OperationLease | None]:
        """Return the backend's scope for one multi-call workflow."""

        return contextlib.nullcontext(None)

    @abstractmethod
    async def get_status(self, notebook_id: str) -> ShareStatus:
        """Get the current sharing configuration."""

    @abstractmethod
    async def set_public(self, notebook_id: str, public: bool) -> ShareStatus:
        """Enable or disable public link sharing."""

    @abstractmethod
    async def set_view_level(
        self,
        notebook_id: str,
        level: ShareViewLevel,
    ) -> ShareStatus:
        """Set what viewers can access."""

    async def add_user(
        self,
        notebook_id: str,
        email: str,
        permission: SharePermission = SharePermission.VIEWER,
        notify: bool = True,
        welcome_message: str = "",
    ) -> ShareStatus:
        """Share notebook with a user.

        Intent wrapper over :meth:`set_users`. The underlying operation is an
        upsert, so this also updates a user who already has access.

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
        return await self.set_users(
            notebook_id,
            [(email, permission)],
            notify=notify,
            welcome_message=welcome_message,
        )

    @abstractmethod
    async def set_users(
        self,
        notebook_id: str,
        grants: list[tuple[str, SharePermission]],
        notify: bool = True,
        welcome_message: str = "",
    ) -> ShareStatus:
        """Upsert several user permissions in one request."""

    async def update_user(
        self,
        notebook_id: str,
        email: str,
        permission: SharePermission,
    ) -> ShareStatus:
        """Update a user's permission level.

        Intent wrapper over :meth:`set_users`. The underlying operation is an
        upsert, so this adds a user who does not have access yet.

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
        return await self.set_users(notebook_id, [(email, permission)], notify=False)

    @abstractmethod
    async def remove_user(self, notebook_id: str, email: str) -> ShareStatus:
        """Remove one user's access to a notebook."""


__all__ = ["SharingAPI"]
