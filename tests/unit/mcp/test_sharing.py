"""Unit tests for the sharing MCP tools.

Drives ``share_*`` through the in-memory FastMCP ``Client`` against the mocked
``NotebookLMClient.sharing``, asserting the serialized ``structured_content``.
Covers the string-label projection, the ``view_level`` read-limitation (surfaced
ONLY when ``share_set_access`` set it, omitted everywhere else), the ``set_access``
fold ordering, the ``set_user`` upsert, the confirm-gated remove flow, and
schema-boundary rejection of out-of-enum inputs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

# Skip cleanly when the `mcp` extra (fastmcp) is absent; see conftest.py.
pytest.importorskip("fastmcp")

from fastmcp.exceptions import ToolError  # noqa: E402 - after importorskip guard

from notebooklm.mcp.tools.sharing import _label  # noqa: E402 - after importorskip guard
from notebooklm.rpc.types import (  # noqa: E402 - after importorskip guard
    ShareAccess,
    SharePermission,
    ShareViewLevel,
)

from .conftest import AsyncMock  # noqa: E402 - after importorskip guard

NB_ID = "11111111-1111-1111-1111-111111111111"


@dataclass
class FakeSharedUser:
    email: str
    permission: Any = SharePermission.VIEWER
    display_name: str | None = None
    avatar_url: str | None = None


@dataclass
class FakeShareStatus:
    notebook_id: str = NB_ID
    is_public: bool = False
    access: Any = ShareAccess.RESTRICTED
    view_level: Any = ShareViewLevel.FULL_NOTEBOOK
    shared_users: list = field(default_factory=list)
    share_url: str | None = None
    # #2130. Defaults mirror ``ShareStatus``'s own: ``None`` = "the backend made
    # no claim", which is what a short response yields.
    max_individuals_share_limit: int | None = None
    is_public_sharing_allowed: bool | None = None

    @property
    def is_public_sharing_denied(self) -> bool:
        """Mirrors the real ``ShareStatus`` property, deliberately not hardcoded.

        Re-deriving it from ``is_public_sharing_allowed`` here (rather than
        returning a canned ``False``) keeps the double honest: a fake that
        always said ``False`` would let a projection bug through on the deny
        case.
        """
        return self.is_public_sharing_allowed is False


# ---------------------------------------------------------------------------
# _label helper
# ---------------------------------------------------------------------------


def test_label_maps_enum_and_int() -> None:
    assert _label({0: "restricted", 1: "anyone_with_link"}, ShareAccess.ANYONE_WITH_LINK) == (
        "anyone_with_link"
    )
    assert _label({1: "owner", 2: "editor", 3: "viewer"}, SharePermission.EDITOR) == "editor"


def test_label_unknown_value_degrades_to_str() -> None:
    """An unexpected int (e.g. SharePermission._REMOVE=4 or drift) never KeyErrors."""
    assert _label({1: "owner", 2: "editor", 3: "viewer"}, SharePermission._REMOVE) == "4"
    assert _label({0: "restricted"}, 99) == "99"


# ---------------------------------------------------------------------------
# share_status
# ---------------------------------------------------------------------------


async def test_share_status_labels_enums_and_omits_view_level(mcp_call, mock_client) -> None:
    mock_client.sharing.get_status = AsyncMock(
        return_value=FakeShareStatus(
            is_public=True,
            access=ShareAccess.ANYONE_WITH_LINK,
            view_level=ShareViewLevel.CHAT_ONLY,  # would be a LIE if surfaced from get_status
            share_url="https://nb/share",
            shared_users=[FakeSharedUser(email="a@b.com", permission=SharePermission.EDITOR)],
            max_individuals_share_limit=1000,
            is_public_sharing_allowed=True,
        )
    )
    result = await mcp_call("share_status", {"notebook": NB_ID})
    sc = result.structured_content
    assert sc["is_public"] is True
    assert sc["access"] == "anyone_with_link"  # string, not int
    assert sc["share_url"] == "https://nb/share"
    # #2130 — the collaborator cap and the tenant policy gate reach MCP.
    assert sc["max_individuals_share_limit"] == 1000
    assert sc["is_public_sharing_allowed"] is True
    assert sc["shared_users"] == [
        {"email": "a@b.com", "permission": "editor", "display_name": None, "avatar_url": None}
    ]
    # view_level is NOT reported by the read API => must be omitted, not shipped as "full".
    assert "view_level" not in sc
    mock_client.sharing.get_status.assert_awaited_once_with(NB_ID)


async def test_share_status_reports_absent_capacity_as_null(mcp_call, mock_client) -> None:
    """The no-claim case reaches MCP as ``null``, not a fabricated 0/false (#2130).

    Both keys stay present so an agent can tell "this server does not report the
    field" from "this notebook has no value for it", and the verdict is ``false``
    because nothing was denied — not because a denial was inferred from silence.
    """
    mock_client.sharing.get_status = AsyncMock(return_value=FakeShareStatus())

    result = await mcp_call("share_status", {"notebook": NB_ID})
    sc = result.structured_content

    assert sc["max_individuals_share_limit"] is None
    assert sc["is_public_sharing_allowed"] is None
    assert sc["is_public_sharing_denied"] is False


async def test_share_status_surfaces_a_policy_denial(mcp_call, mock_client) -> None:
    """An explicit wire ``False`` reaches MCP as a denial verdict."""
    mock_client.sharing.get_status = AsyncMock(
        return_value=FakeShareStatus(is_public_sharing_allowed=False)
    )

    result = await mcp_call("share_status", {"notebook": NB_ID})
    sc = result.structured_content

    assert sc["is_public_sharing_allowed"] is False
    assert sc["is_public_sharing_denied"] is True


async def test_share_status_resolves_notebook_by_name(mcp_call, mock_client) -> None:
    mock_client.notebooks.list = AsyncMock(
        return_value=[type("NB", (), {"id": NB_ID, "title": "My NB"})()]
    )
    mock_client.sharing.get_status = AsyncMock(return_value=FakeShareStatus())
    await mcp_call("share_status", {"notebook": "My NB"})
    mock_client.sharing.get_status.assert_awaited_once_with(NB_ID)


# ---------------------------------------------------------------------------
# share_set_access — folds set_public + set_view_level
# ---------------------------------------------------------------------------


async def test_share_set_access_public_only(mcp_call, mock_client) -> None:
    # confirm=True bypasses the widening gate → straight to set_public (no get_status).
    mock_client.sharing.set_public = AsyncMock(
        return_value=FakeShareStatus(is_public=True, access=ShareAccess.ANYONE_WITH_LINK)
    )
    mock_client.sharing.set_view_level = AsyncMock()
    mock_client.sharing.get_status = AsyncMock()
    result = await mcp_call(
        "share_set_access", {"notebook": NB_ID, "public": True, "confirm": True}
    )
    assert result.structured_content["access"] == "anyone_with_link"
    assert "view_level" not in result.structured_content  # not set => omitted
    mock_client.sharing.set_public.assert_awaited_once_with(NB_ID, True)
    mock_client.sharing.set_view_level.assert_not_called()
    # confirm=True skips the widening state-check read entirely.
    mock_client.sharing.get_status.assert_not_called()


async def test_share_set_access_view_level_only_returns_value(mcp_call, mock_client) -> None:
    """Guards the read-limitation trap: view_level echoes the value it set."""
    mock_client.sharing.set_public = AsyncMock()
    mock_client.sharing.set_view_level = AsyncMock(
        return_value=FakeShareStatus(view_level=ShareViewLevel.CHAT_ONLY)
    )
    result = await mcp_call("share_set_access", {"notebook": NB_ID, "view_level": "chat"})
    assert result.structured_content["view_level"] == "chat"
    mock_client.sharing.set_public.assert_not_called()
    mock_client.sharing.set_view_level.assert_awaited_once_with(NB_ID, ShareViewLevel.CHAT_ONLY)


async def test_share_set_access_both_fail_closed_order_returns_view_level(
    mcp_call, mock_client
) -> None:
    """Both fields: view_level (the restriction) is applied FIRST (fail-closed), and the
    response echoes the just-set view_level from set_view_level, not set_public's
    FULL-hardcoded status."""
    mock_client.sharing.set_public = AsyncMock(
        return_value=FakeShareStatus(is_public=True, view_level=ShareViewLevel.FULL_NOTEBOOK)
    )
    mock_client.sharing.set_view_level = AsyncMock(
        return_value=FakeShareStatus(view_level=ShareViewLevel.CHAT_ONLY)  # authoritative
    )
    result = await mcp_call(
        "share_set_access",
        {"notebook": NB_ID, "public": True, "view_level": "chat", "confirm": True},
    )
    sc = result.structured_content
    assert sc["view_level"] == "chat"  # from set_view_level, not set_public's FULL
    assert sc["is_public"] is True  # from set_public (applied last, authoritative)
    mock_client.sharing.set_public.assert_awaited_once_with(NB_ID, True)
    mock_client.sharing.set_view_level.assert_awaited_once_with(NB_ID, ShareViewLevel.CHAT_ONLY)
    # Fail-closed: the restricting view_level is applied BEFORE toggling public.
    call_names = [c[0] for c in mock_client.sharing.mock_calls]
    assert call_names.index("set_view_level") < call_names.index("set_public")


async def test_share_set_access_requires_a_field(mcp_call, mock_client) -> None:
    mock_client.sharing.set_public = AsyncMock()
    with pytest.raises(ToolError):
        await mcp_call("share_set_access", {"notebook": NB_ID})
    mock_client.sharing.set_public.assert_not_called()


async def test_share_set_access_rejects_bad_view_level(mcp_call, mock_client) -> None:
    mock_client.sharing.set_view_level = AsyncMock()
    with pytest.raises(ToolError):
        await mcp_call("share_set_access", {"notebook": NB_ID, "view_level": "sources"})
    mock_client.sharing.set_view_level.assert_not_called()


# --- confirm gate on public widening (restricted -> public) ---


async def test_share_set_access_widening_needs_confirmation(mcp_call, mock_client) -> None:
    """public=True on a currently-restricted notebook, no confirm => preview, no mutation."""
    mock_client.sharing.get_status = AsyncMock(return_value=FakeShareStatus(is_public=False))
    mock_client.sharing.set_public = AsyncMock()
    result = await mcp_call("share_set_access", {"notebook": NB_ID, "public": True})
    assert result.structured_content == {
        "status": "needs_confirmation",
        "preview": {
            "action": "share_set_access",
            "notebook_id": NB_ID,
            "change": "restricted -> anyone_with_link",
        },
    }
    mock_client.sharing.set_public.assert_not_called()
    mock_client.sharing.get_status.assert_awaited_once_with(NB_ID)


async def test_share_set_access_widening_preview_includes_view_level(mcp_call, mock_client) -> None:
    """A widening call that also sets view_level surfaces it in the preview."""
    mock_client.sharing.get_status = AsyncMock(return_value=FakeShareStatus(is_public=False))
    mock_client.sharing.set_public = AsyncMock()
    mock_client.sharing.set_view_level = AsyncMock()
    result = await mcp_call(
        "share_set_access", {"notebook": NB_ID, "public": True, "view_level": "chat"}
    )
    assert result.structured_content["status"] == "needs_confirmation"
    assert result.structured_content["preview"]["view_level"] == "chat"
    mock_client.sharing.set_public.assert_not_called()
    mock_client.sharing.set_view_level.assert_not_called()


async def test_share_set_access_already_public_needs_no_confirmation(mcp_call, mock_client) -> None:
    """public=True on an already-public notebook is NOT a widening => applies directly."""
    mock_client.sharing.get_status = AsyncMock(return_value=FakeShareStatus(is_public=True))
    mock_client.sharing.set_public = AsyncMock(
        return_value=FakeShareStatus(is_public=True, access=ShareAccess.ANYONE_WITH_LINK)
    )
    result = await mcp_call("share_set_access", {"notebook": NB_ID, "public": True})
    assert result.structured_content["status"] == "updated"
    mock_client.sharing.set_public.assert_awaited_once_with(NB_ID, True)
    # The single state-read that discovered "already public" is pinned so a refactor
    # that skips it (and would then wrongly gate) is caught.
    mock_client.sharing.get_status.assert_awaited_once_with(NB_ID)


async def test_share_set_access_restricting_needs_no_confirmation(mcp_call, mock_client) -> None:
    """public=False (restricting) is never gated — no state read, applies directly."""
    mock_client.sharing.get_status = AsyncMock()
    mock_client.sharing.set_public = AsyncMock(
        return_value=FakeShareStatus(is_public=False, access=ShareAccess.RESTRICTED)
    )
    result = await mcp_call("share_set_access", {"notebook": NB_ID, "public": False})
    assert result.structured_content["access"] == "restricted"
    mock_client.sharing.set_public.assert_awaited_once_with(NB_ID, False)
    mock_client.sharing.get_status.assert_not_called()


async def test_share_set_access_view_level_only_needs_no_confirmation(
    mcp_call, mock_client
) -> None:
    """view_level-only (public is None) is not gated — no state read."""
    mock_client.sharing.get_status = AsyncMock()
    mock_client.sharing.set_view_level = AsyncMock(
        return_value=FakeShareStatus(view_level=ShareViewLevel.CHAT_ONLY)
    )
    result = await mcp_call("share_set_access", {"notebook": NB_ID, "view_level": "chat"})
    assert result.structured_content["view_level"] == "chat"
    mock_client.sharing.get_status.assert_not_called()


# ---------------------------------------------------------------------------
# share_set_user — upsert over add_user (add + update are the same RPC)
# ---------------------------------------------------------------------------


async def test_share_set_user_needs_confirmation(mcp_call, mock_client) -> None:
    """Every grant is gated: no confirm => preview (with has_message), no mutation."""
    mock_client.sharing.add_user = AsyncMock()
    result = await mcp_call(
        "share_set_user", {"notebook": NB_ID, "email": "a@b.com", "message": "hi"}
    )
    assert result.structured_content == {
        "status": "needs_confirmation",
        "preview": {
            "action": "share_set_user",
            "notebook_id": NB_ID,
            "email": "a@b.com",
            "permission": "viewer",
            "notify": False,
            "has_message": True,
        },
    }
    mock_client.sharing.add_user.assert_not_called()


async def test_share_set_user_defaults_viewer(mcp_call, mock_client) -> None:
    # confirm=True applies the grant; notify defaults to False (no email spam, #1742).
    mock_client.sharing.add_user = AsyncMock(
        return_value=FakeShareStatus(
            shared_users=[FakeSharedUser(email="a@b.com", permission=SharePermission.VIEWER)]
        )
    )
    result = await mcp_call(
        "share_set_user", {"notebook": NB_ID, "email": "a@b.com", "confirm": True}
    )
    assert result.structured_content["shared_users"][0]["permission"] == "viewer"
    assert "view_level" not in result.structured_content
    mock_client.sharing.add_user.assert_awaited_once_with(
        NB_ID, "a@b.com", permission=SharePermission.VIEWER, notify=False, welcome_message=""
    )


async def test_share_set_user_editor_with_message(mcp_call, mock_client) -> None:
    mock_client.sharing.add_user = AsyncMock(return_value=FakeShareStatus())
    await mcp_call(
        "share_set_user",
        {
            "notebook": NB_ID,
            "email": "a@b.com",
            "permission": "editor",
            "notify": True,
            "message": "welcome",
            "confirm": True,
        },
    )
    mock_client.sharing.add_user.assert_awaited_once_with(
        NB_ID, "a@b.com", permission=SharePermission.EDITOR, notify=True, welcome_message="welcome"
    )


async def test_share_set_user_rejects_owner(mcp_call, mock_client) -> None:
    """OWNER is not a valid input (Literal editor|viewer) => schema rejection, no RPC."""
    mock_client.sharing.add_user = AsyncMock()
    with pytest.raises(ToolError):
        await mcp_call(
            "share_set_user", {"notebook": NB_ID, "email": "a@b.com", "permission": "owner"}
        )
    mock_client.sharing.add_user.assert_not_called()


# ---------------------------------------------------------------------------
# share_remove_user — confirm-gated
# ---------------------------------------------------------------------------


async def test_share_remove_user_needs_confirmation(mcp_call, mock_client) -> None:
    mock_client.sharing.remove_user = AsyncMock()
    result = await mcp_call("share_remove_user", {"notebook": NB_ID, "email": "a@b.com"})
    sc = result.structured_content
    assert sc["status"] == "needs_confirmation"
    assert sc["preview"] == {
        "action": "remove_share_user",
        "notebook_id": NB_ID,
        "email": "a@b.com",
    }
    mock_client.sharing.remove_user.assert_not_called()


async def test_share_remove_user_confirmed(mcp_call, mock_client) -> None:
    # The tool discards remove_user's return value, so no return_value is set here.
    mock_client.sharing.remove_user = AsyncMock()
    result = await mcp_call(
        "share_remove_user", {"notebook": NB_ID, "email": "a@b.com", "confirm": True}
    )
    assert result.structured_content == {
        "status": "removed",
        "notebook_id": NB_ID,
        "email": "a@b.com",
    }
    mock_client.sharing.remove_user.assert_awaited_once_with(NB_ID, "a@b.com")
