"""Sharing operations API."""

from ._backend import BackendAdapter
from ._backend_compat import project_backend_call
from ._deadline import RuntimeDeadlineFactory
from ._projectors import project_share_status
from ._records import SharePermissionLevel, ShareViewScope
from ._sharing_service import SharingService
from .rpc.types import SharePermission, ShareViewLevel
from .types import ShareStatus

#: Public permission enum to the neutral semantic vocabulary. Total over
#: ``SharePermission``, including the write-only removal sentinel: the sentinel
#: is rejected by :meth:`SharingAPI.set_users`, and rejecting it there rather
#: than here is what keeps the error the caller sees unchanged.
_PERMISSION_LEVELS: dict[SharePermission, SharePermissionLevel] = {
    SharePermission.OWNER: SharePermissionLevel.OWNER,
    SharePermission.EDITOR: SharePermissionLevel.EDITOR,
    SharePermission.VIEWER: SharePermissionLevel.VIEWER,
    SharePermission._REMOVE: SharePermissionLevel.REMOVE,
}
_VIEW_SCOPES: dict[ShareViewLevel, ShareViewScope] = {
    ShareViewLevel.FULL_NOTEBOOK: ShareViewScope.FULL_NOTEBOOK,
    ShareViewLevel.CHAT_ONLY: ShareViewScope.CHAT_ONLY,
}


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

    def __init__(
        self,
        *,
        _backend: BackendAdapter,
        _deadline_factory: RuntimeDeadlineFactory | None = None,
    ):
        """Initialize the sharing API.

        Args:
            _backend: Private semantic backend supplied by the client
                composition root. Every sharing operation is dispatched through
                it; this facade owns public signatures, the public enum to
                neutral vocabulary translation, and error compatibility only.
        """
        self._service = SharingService(_backend, deadline_factory=_deadline_factory)

    @staticmethod
    def _permission_level(permission: SharePermission) -> SharePermissionLevel:
        """Translate one public permission into the neutral vocabulary.

        Fails closed on a value outside the public enum. The pre-migration path
        reached ``permission.name`` on the same input and raised
        ``AttributeError``; this states the contract instead of tripping over
        it, and no in-contract caller can reach either.
        """
        try:
            return _PERMISSION_LEVELS[permission]
        except (KeyError, TypeError) as exc:
            raise ValueError(f"Unknown share permission: {permission!r}") from exc

    async def get_status(self, notebook_id: str) -> ShareStatus:
        """Get current sharing configuration.

        Args:
            notebook_id: The notebook ID.

        Returns:
            ShareStatus with current sharing state and user list.
        """
        return project_share_status(
            await project_backend_call(self._service.get_status(notebook_id))
        )

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
        return project_share_status(
            await project_backend_call(self._service.set_public(notebook_id, public))
        )

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
            than fetching it from the API. Every other field still comes
            from the post-mutation read: an older form rebuilt the status
            field by field and silently dropped the collaborator cap and the
            public-sharing policy gate the read had just decoded (#2130).
        """
        scope = _VIEW_SCOPES.get(level)
        if scope is None:
            raise ValueError(f"Unknown share view level: {level!r}")
        return project_share_status(
            await project_backend_call(self._service.set_view_level(notebook_id, scope))
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
        return project_share_status(
            await project_backend_call(
                self._service.set_users(
                    notebook_id,
                    [(email, self._permission_level(permission)) for email, permission in grants],
                    notify=notify,
                    welcome_message=welcome_message,
                )
            )
        )

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
        return project_share_status(
            await project_backend_call(
                self._service.update_user(
                    notebook_id,
                    email,
                    self._permission_level(permission),
                )
            )
        )

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
        return project_share_status(
            await project_backend_call(self._service.remove_user(notebook_id, email))
        )
