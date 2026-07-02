"""Notebook-sharing MCP tools.

Thin adapters over ``client.sharing`` (SharingAPI): resolve the notebook ref once
via :func:`resolve_notebook`, call the sharing method directly, and project the
typed :class:`~notebooklm._types.sharing.ShareStatus` to the wire with
**string-labeled enums**. The share enums are ``int, Enum`` (``rpc/types.py``), so
a raw :func:`to_jsonable` pass would leak integers (``access=1`` etc.) — the
projection here maps them to stable labels instead.

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

from typing import Any, Literal

from fastmcp import Context

from ..._types.sharing import ShareStatus
from ...exceptions import ValidationError
from ...rpc.types import SharePermission, ShareViewLevel
from .._confirm import DESTRUCTIVE, READ_ONLY, needs_confirmation
from .._context import get_client
from .._errors import mcp_errors
from .._resolve import resolve_notebook

#: ``int, Enum`` value → wire label. Unknown values (e.g. ``SharePermission._REMOVE``
#: or protocol drift) degrade to ``str(value)`` so the projection never KeyErrors.
_ACCESS_LABELS = {0: "restricted", 1: "anyone_with_link"}
_VIEW_LEVEL_LABELS = {0: "full", 1: "chat"}
_PERMISSION_LABELS = {1: "owner", 2: "editor", 3: "viewer"}

#: Wire input → enum. OWNER is intentionally absent (cannot be assigned via share).
_PERMISSION_INPUT = {"editor": SharePermission.EDITOR, "viewer": SharePermission.VIEWER}
_VIEW_LEVEL_INPUT = {"full": ShareViewLevel.FULL_NOTEBOOK, "chat": ShareViewLevel.CHAT_ONLY}


def _label(mapping: dict[int, str], value: int) -> str:
    """Map an ``int, Enum`` member (or raw int) to its label; unknown → ``str``."""
    key = int(value)
    return mapping.get(key, str(key))


def _status_payload(status: ShareStatus, *, include_view_level: bool = False) -> dict[str, Any]:
    """Project ``ShareStatus`` to a wire dict with string-labeled enums.

    ``view_level`` is included only when ``include_view_level`` is set (i.e. the
    status came from ``set_view_level``, the only source with a trustworthy value).
    """
    payload: dict[str, Any] = {
        "notebook_id": status.notebook_id,
        "is_public": status.is_public,
        "access": _label(_ACCESS_LABELS, status.access),
        "share_url": status.share_url,
        "shared_users": [
            {
                "email": user.email,
                "permission": _label(_PERMISSION_LABELS, user.permission),
                "display_name": user.display_name,
                "avatar_url": user.avatar_url,
            }
            for user in status.shared_users
        ],
    }
    if include_view_level:
        payload["view_level"] = _label(_VIEW_LEVEL_LABELS, status.view_level)
    return payload


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
        client = get_client(ctx)
        with mcp_errors():
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
        preview unless ``confirm=True``; restricting and ``view_level`` changes are
        not gated.

        Returns the updated status (or preview). ``view_level`` is echoed only when
        set here (the read API can't report it); with both fields it is applied
        before ``public`` so a partial failure fails closed.
        """
        client = get_client(ctx)
        with mcp_errors():
            nb_id = await resolve_notebook(client, notebook)
            if public is True and not confirm:
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
                        "change": "restricted -> anyone_with_link",
                    }
                    if view_level is not None:
                        # The confirmed call also sets view_level — surface it so the
                        # preview describes every side effect it would apply.
                        preview["view_level"] = view_level
                    return needs_confirmation(preview)
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
            return {"status": "updated", **payload}

    @mcp.tool
    async def share_set_user(
        ctx: Context,
        notebook: str,
        email: str,
        permission: Literal["editor", "viewer"] = "viewer",
        notify: bool = False,
        message: str = "",
        confirm: bool = False,
    ) -> dict[str, Any]:
        """Grant or change a user's access to a notebook. Accepts a notebook name or ID.

        Confirm-gated: every grant/regrade returns a ``needs_confirmation`` preview
        unless ``confirm=True``. Upsert by email (one backend op for an add or a
        permission change). ``permission``: ``editor`` or ``viewer`` (not OWNER).
        ``notify`` (default ``False``) emails the user on grant/re-grade; ``message``
        is an optional welcome note. Returns the updated status (or a preview).
        """
        client = get_client(ctx)
        with mcp_errors():
            nb_id = await resolve_notebook(client, notebook)
            if not confirm:
                return needs_confirmation(
                    {
                        "action": "share_set_user",
                        "notebook_id": nb_id,
                        "email": email,
                        "permission": permission,
                        "notify": notify,
                        "has_message": bool(message),
                    }
                )
            status = await client.sharing.add_user(
                nb_id,
                email,
                permission=_PERMISSION_INPUT[permission],
                notify=notify,
                welcome_message=message,
            )
            return {"status": "updated", **_status_payload(status)}

    @mcp.tool(annotations=DESTRUCTIVE)
    async def share_remove_user(
        ctx: Context, notebook: str, email: str, confirm: bool = False
    ) -> dict[str, Any]:
        """Remove a user's access to a notebook. Accepts a notebook name or ID.

        Confirm-gated: called with ``confirm=False`` (default) it does NOT mutate —
        it returns a ``needs_confirmation`` preview. Call with ``confirm=True`` to
        actually remove the user.
        """
        client = get_client(ctx)
        with mcp_errors():
            nb_id = await resolve_notebook(client, notebook)
            if not confirm:
                return needs_confirmation(
                    {"action": "remove_share_user", "notebook_id": nb_id, "email": email}
                )
            await client.sharing.remove_user(nb_id, email)
            return {"status": "removed", "notebook_id": nb_id, "email": email}
