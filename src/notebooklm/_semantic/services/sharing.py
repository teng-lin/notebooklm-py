"""Transport-neutral semantic service for the P6.5 sharing slice.

Owns the sharing domain's policy — which permissions a caller may send, whether
a grant batch is coherent, and what each intent logs — and invokes only typed
operation definitions through :class:`~notebooklm._semantic.backend.BackendAdapter`. It
holds no wire vocabulary: the ``SHARE_NOTEBOOK`` / ``GET_SHARE_STATUS`` /
``MutateProject`` grammar lives in ``_web/codec/sharing.py``.

Since P9.2 all three sharing composites are service-owned. Public-link and user
grant writes use ``sharing.mutate``; viewer scope uses
``sharing.patch_view_level``. Each workflow then invokes ``sharing.get`` under
one deadline and rebinds leaf failures while retaining the blocked leaf in
diagnostics.

Since P10 R6.3 every method speaks ``ShareStatusRecord`` only. The public
``ShareStatus`` — and with it the neutral-to-public access, view-level and
permission mappings that decide who can see a notebook — is built by
:class:`~notebooklm._sharing.SharingAPI`, per invariant I1. The ``Legacy*``
share records in ``_sharing_records.py`` are an I9 mapping exemption for
``_backend_compat``/the projectors and never appear here.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import replace

from ..._deadline import RuntimeDeadline, RuntimeDeadlineFactory
from ..backend import (
    BackendAdapter,
    BackendDeadlineExceededError,
    BackendError,
    mark_backend_outcome_unknown,
    rebind_operation,
    require_leaves,
)
from ..operations import Operation
from ..records import (
    SHARING_GET_DEF,
    SHARING_MUTATE_DEF,
    SHARING_PATCH_VIEW_LEVEL_DEF,
    SHARING_SET_PUBLIC_DEF,
    SHARING_SET_VIEW_LEVEL_DEF,
    SHARING_UPDATE_USERS_DEF,
    SharePermissionLevel,
    ShareStatusRecord,
    ShareViewScope,
    SharingGetInput,
    SharingGrants,
    SharingMutateInput,
    SharingPatchViewLevelInput,
    SharingSetPublicInput,
    SharingSetViewLevelInput,
    SharingUpdateUsersInput,
    SharingUserGrant,
    SharingVisibility,
)

# Explicitly the pre-migration logger name rather than ``__name__``. These
# DEBUG lines are the only record of which grantee a call actually addressed —
# a typoed address is exactly what they get read for — so the migration keeps
# them reachable under the logger operators already configure. The same
# deliberate re-homing is applied to the notebook/source loggers in
# ``_web/backend.py``.
logger = logging.getLogger("notebooklm._sharing")


class SharingService:
    """Validate sharing intents and invoke their typed backend operations."""

    __slots__ = ("_backend", "_deadline_factory")

    def __init__(
        self,
        backend: BackendAdapter,
        *,
        deadline_factory: RuntimeDeadlineFactory | None = None,
    ) -> None:
        self._backend = backend
        self._deadline_factory = deadline_factory

    async def get_status(
        self,
        notebook_id: str,
        *,
        deadline: RuntimeDeadline | None = None,
    ) -> ShareStatusRecord:
        logger.debug("Getting share status for notebook: %s", notebook_id)
        result = await self._backend.invoke(
            SHARING_GET_DEF,
            SharingGetInput(notebook_id),
            deadline=deadline,
        )
        return result.status

    async def set_public(
        self,
        notebook_id: str,
        public: bool,
        *,
        deadline: RuntimeDeadline | None = None,
    ) -> ShareStatusRecord:
        logger.debug("Setting notebook %s public=%s", notebook_id, public)
        value = SharingSetPublicInput(notebook_id, public)
        return await self._mutate_then_read_status(
            value.notebook_id,
            SharingVisibility(value.public),
            workflow=SHARING_SET_PUBLIC_DEF.key,
            deadline=deadline,
        )

    def _start_deadline(self, deadline: RuntimeDeadline | None) -> RuntimeDeadline | None:
        """Mint the workflow deadline unless the caller already supplied one."""
        if deadline is not None or self._deadline_factory is None:
            return deadline
        return self._deadline_factory.start()

    async def _mutate_then_read_status(
        self,
        notebook_id: str,
        mutation: SharingVisibility | SharingGrants,
        *,
        workflow: Operation,
        deadline: RuntimeDeadline | None,
    ) -> ShareStatusRecord:
        """Run one sharing mutation and its mandatory status readback.

        The mutation echo carries no useful status. A successful write followed
        by a readback deadline therefore leaves the requested final outcome
        unconfirmed, even when the read itself expired before dispatch.
        """
        require_leaves(self._backend, SHARING_MUTATE_DEF.key, SHARING_GET_DEF.key)
        deadline = self._start_deadline(deadline)
        write_completed = False
        try:
            await self._backend.invoke(
                SHARING_MUTATE_DEF,
                SharingMutateInput(notebook_id, mutation),
                deadline=deadline,
            )
            write_completed = True
            result = await self._backend.invoke(
                SHARING_GET_DEF,
                SharingGetInput(notebook_id),
                deadline=deadline,
            )
            return result.status
        except BackendError as error:
            if error.operation is workflow:
                raise
            if write_completed and isinstance(error, BackendDeadlineExceededError):
                error = mark_backend_outcome_unknown(error)
            raise rebind_operation(error, workflow) from error.__cause__

    async def set_view_level(
        self,
        notebook_id: str,
        view_level: ShareViewScope,
        *,
        deadline: RuntimeDeadline | None = None,
    ) -> ShareStatusRecord:
        logger.debug("Setting notebook %s view level to %s", notebook_id, view_level.name)
        value = SharingSetViewLevelInput(notebook_id, view_level)
        status = await self._patch_view_level_then_read_status(
            value,
            deadline=deadline,
        )
        # ``GET_SHARE_STATUS`` does not report viewer scope, so the record
        # carries the scope this workflow just set. Every other decoded field
        # is kept (#2130).
        return replace(status, view_level=value.view_level)

    async def _patch_view_level_then_read_status(
        self,
        value: SharingSetViewLevelInput,
        *,
        deadline: RuntimeDeadline | None,
    ) -> ShareStatusRecord:
        """Patch viewer scope, then read status under the same workflow deadline."""
        require_leaves(
            self._backend,
            SHARING_PATCH_VIEW_LEVEL_DEF.key,
            SHARING_GET_DEF.key,
        )
        deadline = self._start_deadline(deadline)
        write_completed = False
        try:
            await self._backend.invoke(
                SHARING_PATCH_VIEW_LEVEL_DEF,
                SharingPatchViewLevelInput(value.notebook_id, value.view_level),
                deadline=deadline,
            )
            write_completed = True
            result = await self._backend.invoke(
                SHARING_GET_DEF,
                SharingGetInput(value.notebook_id),
                deadline=deadline,
            )
            return result.status
        except BackendError as error:
            if error.operation is SHARING_SET_VIEW_LEVEL_DEF.key:
                raise
            if write_completed and isinstance(error, BackendDeadlineExceededError):
                error = mark_backend_outcome_unknown(error)
            raise rebind_operation(error, SHARING_SET_VIEW_LEVEL_DEF.key) from error.__cause__

    async def set_users(
        self,
        notebook_id: str,
        grants: Sequence[tuple[str, SharePermissionLevel]],
        *,
        notify: bool = True,
        welcome_message: str = "",
        deadline: RuntimeDeadline | None = None,
    ) -> ShareStatusRecord:
        """Upsert several individual-user permissions in one request.

        Rejects an empty batch, an owner assignment, a removal smuggled in as a
        grant, and a repeated grantee — the backend answers a batch containing
        the same address twice with success while silently leaving that user's
        permission unchanged, so there is no first-wins or last-wins rule to
        honour. The duplicate comparison is exact: RFC 5321 makes the local
        part case-sensitive, so folding case here would reject two addresses
        the server may well treat as distinct identities.
        """
        if not grants:
            raise ValueError("Must provide at least one user grant")
        seen: set[str] = set()
        for email, permission in grants:
            if permission is SharePermissionLevel.OWNER:
                raise ValueError("Cannot assign OWNER permission")
            if permission is SharePermissionLevel.REMOVE:
                raise ValueError("Use remove_user() instead")
            if email in seen:
                raise ValueError(
                    f"Duplicate email in grants: {email!r}. The backend silently "
                    "ignores a repeated grantee instead of applying either entry; "
                    "send one grant per user."
                )
            seen.add(email)

        logger.debug(
            "Setting %d user permission(s) on notebook %s: %s",
            len(grants),
            notebook_id,
            [(email, permission.name) for email, permission in grants],
        )
        value = SharingUpdateUsersInput(
            notebook_id,
            tuple(SharingUserGrant(email, permission) for email, permission in grants),
            notify=notify,
            welcome_message=welcome_message,
        )
        return await self._mutate_then_read_status(
            value.notebook_id,
            SharingGrants(value.grants, value.notify, value.welcome_message),
            workflow=SHARING_UPDATE_USERS_DEF.key,
            deadline=deadline,
        )

    async def update_user(
        self,
        notebook_id: str,
        email: str,
        permission: SharePermissionLevel,
        *,
        deadline: RuntimeDeadline | None = None,
    ) -> ShareStatusRecord:
        """Replace one user's permission through the shared upsert."""
        logger.debug(
            "Updating user %s permission to %s in notebook %s",
            email,
            permission.name,
            notebook_id,
        )
        return await self.set_users(
            notebook_id,
            [(email, permission)],
            notify=False,
            deadline=deadline,
        )

    async def remove_user(
        self,
        notebook_id: str,
        email: str,
        *,
        deadline: RuntimeDeadline | None = None,
    ) -> ShareStatusRecord:
        """Revoke one user's access.

        Singular by design. A batch of removals only works when every target is
        currently shared: if any requested address is already absent the backend
        silently drops the whole request — including the users that *are*
        present — and reports no failure. A plural removal therefore needs a
        share-status preflight and post-verification rather than a wider entry
        list; see ``docs/rpc-reference.md``.
        """
        logger.debug("Removing user %s from notebook %s", email, notebook_id)
        value = SharingUpdateUsersInput(
            notebook_id,
            (SharingUserGrant(email, SharePermissionLevel.REMOVE),),
            notify=False,
        )
        return await self._mutate_then_read_status(
            value.notebook_id,
            SharingGrants(value.grants, value.notify, value.welcome_message),
            workflow=SHARING_UPDATE_USERS_DEF.key,
            deadline=deadline,
        )


__all__ = ["SharingService"]
