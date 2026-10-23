"""Notebook-sharing MCP tools.

Thin adapters over ``client.sharing`` (SharingAPI): resolve the notebook ref once
via :func:`resolve_notebook`, call the sharing method directly, and project the
typed :class:`~notebooklm._types.sharing.ShareStatus` to the wire with
**string-labeled enums**. The share enums are transport-neutral ``int, Enum``
definitions in ``_types/enums.py`` (publicly imported from ``notebooklm.types``
and compatibility-re-exported by ``rpc.types``), so a raw :func:`to_jsonable`
pass would leak integers (``access=1`` etc.) — the projection here maps them to
stable labels instead.

Four tools cover the six ``client.sharing`` operations: the notebook-link settings
(``set_public`` + ``set_view_level``) fold into :func:`share_set_access`, and the
per-user grant operations (``add_user`` + ``update_user`` — the *same* backend RPC)
fold into an upsert :func:`share_set_user`. ``share_status`` (read-only) and
``share_remove_user`` (destructive, confirm-gated) stay discrete because their tool
annotations differ.

``view_level`` is deliberately OMITTED from every ``get_status``-derived payload:
``GET_SHARE_STATUS`` does not report it, so ``ShareStatus.from_api_response``
hardcodes ``FULL_NOTEBOOK``. Shipping that would be confidently-wrong data (it
would read ``"full"`` even for a chat-only notebook). The only trustworthy value
is the one ``set_view_level`` overrides into its own return, so ``view_level`` is
surfaced ONLY when :func:`share_set_access` actually set it.

This module imports NO ``click`` / ``rich`` / ``cli``.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastmcp import Context
from pydantic import BaseModel, ConfigDict, Field

from ..._app.views import VIEW_LEVEL_LABELS as _VIEW_LEVEL_LABELS
from ..._app.views import label as _label
from ..._app.views import share_status_view as _status_payload
from ...exceptions import ValidationError
from ...types import SharePermission, ShareStatus, ShareViewLevel
from .._confirm import (
    DESTRUCTIVE,
    READ_ONLY,
    confirmed_name_deprecation,
    needs_confirmation,
    with_confirmation_deprecation,
)
from .._context import get_client
from .._errors import mcp_errors
from .._resolve import resolve_notebook, resolve_notebook_listed
from ._preview import notebook_title_or_none

# The enum→label projection (``_label`` / ``_status_payload``) + its label maps
# now live in the shared, transport-neutral ``_app.views`` so the REST adapter
# emits the identical labeled shape (Option B). Re-imported here under the
# historical private names to keep the tool bodies (and the ``_label`` unit test)
# unchanged. ``_VIEW_LEVEL_LABELS`` is still referenced inline for the
# ``set_view_level``-authoritative echo below.

#: Wire input → enum. OWNER is intentionally absent (cannot be assigned via share).
_PERMISSION_INPUT = {"editor": SharePermission.EDITOR, "viewer": SharePermission.VIEWER}
_VIEW_LEVEL_INPUT = {"full": ShareViewLevel.FULL_NOTEBOOK, "chat": ShareViewLevel.CHAT_ONLY}

#: Keep each confirmed sharing invocation within an explicitly reviewable subset.
MAX_SHARE_GRANTS = 100


class UserGrant(BaseModel):
    """One notebook permission grant."""

    model_config = ConfigDict(extra="forbid")
    email: str
    permission: Literal["editor", "viewer"] = "viewer"


def register(mcp: Any) -> None:
    """Register the sharing tools on ``mcp``."""

    @mcp.tool(annotations=READ_ONLY)
    async def share_status(ctx: Context, notebook: str) -> dict[str, Any]:
        """Get a notebook's sharing status. Accepts a notebook name or ID.

        Returns ``is_public``, ``access`` (``restricted`` | ``anyone_with_link``),
        the ``share_url``, and the list of ``shared_users`` ({email, permission,
        display_name, avatar_url}). ``permission`` / ``access`` are string labels.

        NOTE: ``view_level`` is intentionally NOT returned here — the read API does
        not report it (it would always read ``"full"``). Set it via
        ``share_set_access``, which echoes the value it just set.
        """
        with mcp_errors():
            client = await get_client(ctx)
            nb_id = await resolve_notebook(client, notebook)
            status = await client.sharing.get_status(nb_id)
            return _status_payload(status)

    @mcp.tool
    async def share_set_access(
        ctx: Context,
        notebook: str,
        public: bool | None = None,
        view_level: Literal["full", "chat"] | None = None,
        confirm: bool = False,
    ) -> dict[str, Any]:
        """Set a notebook's link-access settings. Accepts a notebook name or ID.

        Provide ``public`` (``True`` = anyone-with-link, ``False`` = restricted)
        and/or ``view_level`` (``full`` or ``chat``). Public *widening*
        (``public=True`` on a restricted notebook) returns a ``needs_confirmation``
        preview; confirm with its canonical ``notebook_id``. Restricting and
        ``view_level`` changes are not gated.

        Returns the updated status (or preview). ``view_level`` is echoed only when
        set here (the read API can't report it); with both fields it is applied
        before ``public`` so a partial failure fails closed.
        """
        with mcp_errors():
            client = await get_client(ctx)
            # Only an unconfirmed public widening can end in a preview; every other
            # call writes, so it resolves strictly (no accent-insensitive match).
            gated = public is True and not confirm
            nb_id, listed = await resolve_notebook_listed(client, notebook, strict=not gated)
            if gated:
                # Gate only a genuine widening (restricted → anyone-with-link). Read
                # current state to skip a spurious confirm on an already-public
                # notebook. Benign TOCTOU: if the notebook flips to restricted between
                # this read and set_public below, the confirmed-less re-widen is the
                # caller's own explicit public=True intent, so not worth locking.
                current = await client.sharing.get_status(nb_id)
                if not current.is_public:
                    preview: dict[str, Any] = {
                        "action": "share_set_access",
                        "notebook_id": nb_id,
                        "title": await notebook_title_or_none(client, nb_id, items=listed),
                        "change": "restricted -> anyone_with_link",
                    }
                    if view_level is not None:
                        # The confirmed call also sets view_level — surface it so the
                        # preview describes every side effect it would apply.
                        preview["view_level"] = view_level
                    return needs_confirmation(preview)
                # Already public: no preview, so this call writes after all. Hold the
                # ref to the strict match every write needs, and to the notebook whose
                # status was just read: a rename or new notebook between the two list
                # reads could make the strict lookup land on a different one.
                if await resolve_notebook(client, notebook, strict=True) != nb_id:
                    raise ValidationError(
                        f"'{notebook}' resolved to a different notebook on the strict "
                        "re-check, so nothing was changed. Retry with the notebook id."
                    )
            # Apply the (possibly restricting) view_level BEFORE toggling public, so a
            # failure on the public step can never leave the notebook public with a
            # wider view level than intended (fail-closed). set_view_level's return is
            # also the only authoritative source for the echoed view_level
            # (get_status / set_public hardcode FULL_NOTEBOOK). The exhaustive branches
            # keep ``status`` provably assigned (the ``else`` covers the both-None case).
            view_status: ShareStatus | None = None
            if view_level is not None:
                view_status = await client.sharing.set_view_level(
                    nb_id, _VIEW_LEVEL_INPUT[view_level]
                )
                status = view_status
                if public is not None:
                    status = await client.sharing.set_public(nb_id, public)  # authoritative, last
            elif public is not None:
                status = await client.sharing.set_public(nb_id, public)
            else:
                raise ValidationError("Provide at least one of public / view_level.")
            # is_public / access come from ``status`` (public applied last when set);
            # view_level is echoed from set_view_level's authoritative return, only
            # when this call set it.
            payload = _status_payload(status, include_view_level=False)
            if view_status is not None:
                payload["view_level"] = _label(_VIEW_LEVEL_LABELS, view_status.view_level)
            return with_confirmation_deprecation(
                {"status": "updated", **payload},
                confirmed_name_deprecation(notebook) if confirm else None,
            )

    @mcp.tool
    async def share_set_user(
        ctx: Context,
        notebook: str,
        email: str | None = None,
        permission: Literal["editor", "viewer"] = "viewer",
        notify: bool = False,
        message: str = "",
        confirm: bool = False,
        grants: Annotated[list[UserGrant], Field(min_length=1, max_length=MAX_SHARE_GRANTS)]
        | None = None,
    ) -> dict[str, Any]:
        """Upsert access: ``email`` + ``permission`` OR 1–100 ``grants`` [{email, permission}].

        Preview lists every grant; confirm with its canonical ``notebook_id``.
        Batches allow mixed editor/viewer permissions in one write. ``notify``
        and ``message`` apply to all grantees. Returns updated sharing status.
        """
        with mcp_errors():
            if (email is None) == (grants is None):
                raise ValidationError("Provide either 'email' or 'grants', not both")
            if grants is not None:
                if permission != "viewer":
                    raise ValidationError("Set each grant's permission inside 'grants'")
                seen: set[str] = set()
                for grant in grants:
                    if not grant.email.strip() or grant.email != grant.email.strip():
                        raise ValidationError(
                            "Grant emails must be non-empty without outer whitespace"
                        )
                    if grant.email in seen:
                        raise ValidationError(f"Duplicate email in grants: {grant.email!r}")
                    seen.add(grant.email)
            elif email is not None and not email.strip():
                raise ValidationError("'email' must be non-empty")
            client = await get_client(ctx)
            nb_id, listed = await resolve_notebook_listed(client, notebook, strict=confirm)
            if not confirm:
                return needs_confirmation(
                    {
                        "action": "share_set_user",
                        "notebook_id": nb_id,
                        "title": await notebook_title_or_none(client, nb_id, items=listed),
                        **(
                            {"grants": [grant.model_dump() for grant in grants]}
                            if grants is not None
                            else {"email": email, "permission": permission}
                        ),
                        "notify": notify,
                        "has_message": bool(message),
                    }
                )
            if grants is not None:
                status = await client.sharing.set_users(
                    nb_id,
                    [(grant.email, _PERMISSION_INPUT[grant.permission]) for grant in grants],
                    notify=notify,
                    welcome_message=message,
                )
            else:
                if email is None:  # pragma: no cover - validated above
                    raise ValidationError("Provide 'email' or 'grants'")
                status = await client.sharing.add_user(
                    nb_id,
                    email,
                    permission=_PERMISSION_INPUT[permission],
                    notify=notify,
                    welcome_message=message,
                )
            return with_confirmation_deprecation(
                {"status": "updated", **_status_payload(status)},
                confirmed_name_deprecation(notebook),
            )

    @mcp.tool(annotations=DESTRUCTIVE)
    async def share_remove_user(
        ctx: Context, notebook: str, email: str, confirm: bool = False
    ) -> dict[str, Any]:
        """Remove a user's access to a notebook. Accepts a notebook name or ID.

        Confirm-gated: called with ``confirm=False`` (default) it does NOT mutate —
        it returns a preview. Re-submit its canonical ``notebook_id`` and verbatim
        email with ``confirm=True``.
        """
        with mcp_errors():
            client = await get_client(ctx)
            nb_id, listed = await resolve_notebook_listed(client, notebook, strict=confirm)
            if not confirm:
                title = await notebook_title_or_none(client, nb_id, items=listed)
                return needs_confirmation(
                    {
                        "action": "remove_share_user",
                        "notebook_id": nb_id,
                        "title": title,
                        "email": email,
                    }
                )
            await client.sharing.remove_user(nb_id, email)
            return with_confirmation_deprecation(
                {"status": "removed", "notebook_id": nb_id, "email": email},
                confirmed_name_deprecation(notebook),
            )
