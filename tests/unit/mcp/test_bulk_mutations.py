"""Batch Studio deletion and sharing through the real MCP boundary."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("fastmcp")

from fastmcp.exceptions import ToolError  # noqa: E402

from notebooklm.exceptions import RPCError  # noqa: E402
from notebooklm.mcp.tools import sharing as sharing_tools  # noqa: E402
from notebooklm.mcp.tools import studio as studio_tools  # noqa: E402
from notebooklm.types import (  # noqa: E402
    ArtifactListing,
    ArtifactListingComponent,
    ArtifactListingFailure,
    ArtifactType,
    ShareAccess,
    SharePermission,
    ShareStatus,
    ShareViewLevel,
)

NB = "11111111-1111-1111-1111-111111111111"
NOTE_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
NOTE_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
MAP = "cccccccc-cccc-cccc-cccc-cccccccccccc"
MISSING = "dddddddd-dddd-dddd-dddd-dddddddddddd"


@pytest.fixture
def studio_client(mock_client):
    """Expose two text notes and a mind map for cross-kind batch scenarios."""
    mock_client.notes.list = AsyncMock(
        return_value=[
            SimpleNamespace(id=NOTE_A, title="First", content="one"),
            SimpleNamespace(id=NOTE_B, title="Second", content="two"),
        ]
    )
    mock_client.artifacts.list = AsyncMock(
        return_value=[SimpleNamespace(id=MAP, title="Map", kind=ArtifactType.MIND_MAP)]
    )
    mock_client.mind_maps.list_note_backed = AsyncMock(return_value=[])
    mock_client.notes.delete = AsyncMock()
    mock_client.artifacts.delete = AsyncMock()
    return mock_client


async def test_studio_batch_preview_resolves_once_and_lists_only_explicit_items(
    mcp_call, studio_client
):
    """Preview deduplicates selected references and reports misses without writing."""
    result = await mcp_call(
        "studio_delete", {"notebook": NB, "items": ["First", NOTE_A, "Second", MISSING]}
    )
    assert result.structured_content["status"] == "needs_confirmation"
    preview = result.structured_content["preview"]
    assert preview["count"] == 2
    assert preview["items"] == [
        {"item_id": NOTE_A, "type": "note", "title": "First"},
        {"item_id": NOTE_B, "type": "note", "title": "Second"},
    ]
    assert preview["not_found"][0]["item_id"] == MISSING
    studio_client.notes.list.assert_awaited_once_with(NB)
    studio_client.artifacts.list.assert_awaited_once_with(NB)
    studio_client.notes.delete.assert_not_awaited()
    studio_client.artifacts.delete.assert_not_awaited()


async def test_studio_batch_deletes_notes_once_and_reports_missing_ids(mcp_call, studio_client):
    """Canonical note IDs share one write while missing IDs remain separate outcomes."""
    result = await mcp_call(
        "studio_delete",
        {"notebook": NB, "items": [NOTE_A.upper(), NOTE_B, NOTE_A, MISSING], "confirm": True},
    )
    payload = result.structured_content
    assert payload["deleted"] == [
        {"item_id": NOTE_A, "type": "note", "was_note_backed": False},
        {"item_id": NOTE_B, "type": "note", "was_note_backed": False},
    ]
    assert payload["not_found"][0]["item_id"] == MISSING
    assert (payload["deleted_count"], payload["not_found_count"], payload["total_count"]) == (
        2,
        1,
        3,
    )
    studio_client.notes.delete.assert_awaited_once_with(NB, [NOTE_A, NOTE_B])
    studio_client.artifacts.delete.assert_not_awaited()


@pytest.mark.parametrize("note_backed", [False, True])
async def test_studio_batch_routes_explicit_mind_maps_by_backing(
    mcp_call, studio_client, note_backed
):
    """Only text notes enter the batch; each selected map uses its backing's delete path."""
    if note_backed:
        studio_client.mind_maps.list_note_backed.return_value = [SimpleNamespace(id=MAP)]
    result = await mcp_call(
        "studio_delete", {"notebook": NB, "items": [MAP, NOTE_A, NOTE_B], "confirm": True}
    )
    assert result.structured_content["deleted"][0] == {
        "item_id": MAP,
        "type": "mind-map",
        "was_note_backed": note_backed,
    }
    assert studio_client.notes.delete.await_args_list[0].args == (NB, [NOTE_A, NOTE_B])
    if note_backed:
        assert studio_client.notes.delete.await_args_list[1].args == (NB, MAP)
        studio_client.artifacts.delete.assert_not_awaited()
    else:
        studio_client.notes.delete.assert_awaited_once()
        studio_client.artifacts.delete.assert_awaited_once_with(NB, MAP)


@pytest.mark.parametrize("items", [[MISSING], MISSING, f'["{MISSING}"]'])
async def test_studio_batch_all_missing_is_a_noop(mcp_call, studio_client, items):
    """Every supported batch encoding preserves idempotency for absent IDs."""
    result = await mcp_call("studio_delete", {"notebook": NB, "items": items, "confirm": True})
    assert result.structured_content["deleted"] == []
    assert result.structured_content["not_found_count"] == 1
    studio_client.notes.delete.assert_not_awaited()
    studio_client.artifacts.delete.assert_not_awaited()


@pytest.mark.parametrize("confirm", [False, True])
async def test_studio_batch_deduplicates_missing_ids(mcp_call, studio_client, confirm):
    """Preview and confirmed counts use unique targets even when targets are absent."""
    result = await mcp_call(
        "studio_delete",
        {
            "notebook": NB,
            "items": [NOTE_A, NOTE_A.upper(), MISSING, MISSING.upper()],
            "confirm": confirm,
        },
    )
    payload = result.structured_content
    if confirm:
        assert (payload["deleted_count"], payload["not_found_count"], payload["total_count"]) == (
            1,
            1,
            2,
        )
        studio_client.notes.delete.assert_awaited_once_with(NB, [NOTE_A])
    else:
        payload = payload["preview"]
        assert payload["count"] == 1
        studio_client.notes.delete.assert_not_awaited()
    assert [item["item_id"] for item in payload["not_found"]] == [MISSING]


async def test_source_batch_canonicalizes_ids_and_counts_unique_misses(mcp_call, mock_client):
    """Confirmed source deletion resolves uppercase UUIDs and deduplicates both buckets."""
    mock_client.sources.list = AsyncMock(return_value=[SimpleNamespace(id=NOTE_A, title="Source")])
    mock_client.sources.delete_many = AsyncMock()
    result = await mcp_call(
        "source_delete",
        {
            "notebook": NB,
            "sources": [NOTE_A.upper(), NOTE_A, MISSING, MISSING.upper()],
            "confirm": True,
        },
    )
    payload = result.structured_content
    assert (payload["deleted_count"], payload["not_found_count"], payload["total_count"]) == (
        1,
        1,
        2,
    )
    mock_client.sources.delete_many.assert_awaited_once_with(NB, [NOTE_A])


@pytest.mark.parametrize(
    "args",
    [
        {},
        {"item": NOTE_A, "items": [NOTE_B]},
        {"items": []},
        {"items": [""]},
        {"items": [NOTE_A] * 101},
        {"items": ["First"], "confirm": True},
    ],
)
async def test_studio_batch_rejects_invalid_input_before_client_open(mcp_call, monkeypatch, args):
    """Invalid or unsafe selections fail before opening a client."""
    get_client = AsyncMock(side_effect=AssertionError("unexpected client open"))
    monkeypatch.setattr(studio_tools, "get_client", get_client)
    with pytest.raises(ToolError, match="VALIDATION"):
        await mcp_call("studio_delete", {"notebook": "missing notebook", **args})
    get_client.assert_not_awaited()


async def test_studio_batch_ambiguous_title_aborts_before_deleting(mcp_call, studio_client):
    """A cross-kind title collision prevents the entire preview from resolving."""
    studio_client.artifacts.list.return_value[0].title = "Second"
    with pytest.raises(ToolError, match="(?i)ambiguous"):
        await mcp_call("studio_delete", {"notebook": NB, "items": ["First", "Second"]})
    studio_client.notes.delete.assert_not_awaited()
    studio_client.artifacts.delete.assert_not_awaited()


@pytest.mark.parametrize("confirm", [False, True])
async def test_studio_batch_incomplete_listing_aborts_before_reporting_or_deleting(
    mcp_call, studio_client, confirm
):
    """An unavailable backing cannot become a false missing-item result or partial write."""
    studio_client.artifacts.list_with_status = AsyncMock(
        return_value=ArtifactListing(
            items=(),
            is_complete=False,
            failures=(
                ArtifactListingFailure(
                    component=ArtifactListingComponent.NOTE_BACKED_MIND_MAPS,
                    error_type="RPCError",
                    message="The note-backed mind-map listing is unavailable",
                ),
            ),
        )
    )
    with pytest.raises(ToolError, match="Artifact lookup is incomplete.*note_backed_mind_maps"):
        await mcp_call(
            "studio_delete", {"notebook": NB, "items": [NOTE_A, MAP], "confirm": confirm}
        )
    studio_client.artifacts.list_with_status.assert_awaited_once_with(NB)
    studio_client.notes.delete.assert_not_awaited()
    studio_client.artifacts.delete.assert_not_awaited()


async def test_studio_batch_failure_does_not_report_deletes_or_continue(mcp_call, studio_client):
    """A failed note write stops later artifact writes and propagates the error."""
    studio_client.notes.delete.side_effect = RPCError("denied")
    with pytest.raises(ToolError, match="denied"):
        await mcp_call(
            "studio_delete", {"notebook": NB, "items": [NOTE_A, NOTE_B, MAP], "confirm": True}
        )
    studio_client.notes.delete.assert_awaited_once()
    studio_client.artifacts.delete.assert_not_awaited()


async def test_studio_batch_strict_ids_rejects_titles_before_open(mcp_call, monkeypatch):
    """Strict-ID mode rejects fuzzy selections before notebook resolution."""
    monkeypatch.setenv("NOTEBOOKLM_MCP_STRICT_IDS", "1")
    get_client = AsyncMock(side_effect=AssertionError("unexpected client open"))
    monkeypatch.setattr(studio_tools, "get_client", get_client)
    with pytest.raises(ToolError, match="Strict mode"):
        await mcp_call("studio_delete", {"notebook": NB, "items": ["First"]})
    get_client.assert_not_awaited()


GRANTS = [
    {"email": "editor@example.test", "permission": "editor"},
    {"email": "viewer@example.test", "permission": "viewer"},
]


async def test_share_batch_preview_includes_every_grant_and_call_flags(mcp_call, mock_client):
    """Sharing previews expose all grantees and batch-wide settings without writing."""
    mock_client.notebooks.list = AsyncMock(return_value=[SimpleNamespace(id=NB, title="Team")])
    result = await mcp_call(
        "share_set_user", {"notebook": NB, "grants": GRANTS, "notify": True, "message": "Welcome"}
    )
    assert result.structured_content == {
        "status": "needs_confirmation",
        "preview": {
            "action": "share_set_user",
            "notebook_id": NB,
            "title": "Team",
            "grants": GRANTS,
            "notify": True,
            "has_message": True,
        },
    }
    mock_client.sharing.add_user.assert_not_called()
    mock_client.sharing.set_users.assert_not_called()


async def test_share_batch_mixed_permissions_use_one_call(mcp_call, mock_client):
    """Confirmed mixed-role grants delegate to one sharing call with common email flags."""
    mock_client.sharing.set_users = AsyncMock(
        return_value=ShareStatus(NB, False, ShareAccess.RESTRICTED, ShareViewLevel.FULL_NOTEBOOK)
    )
    result = await mcp_call(
        "share_set_user",
        {"notebook": NB, "grants": GRANTS, "notify": True, "message": "Welcome", "confirm": True},
    )
    assert result.structured_content["status"] == "updated"
    mock_client.sharing.set_users.assert_awaited_once_with(
        NB,
        [
            ("editor@example.test", SharePermission.EDITOR),
            ("viewer@example.test", SharePermission.VIEWER),
        ],
        notify=True,
        welcome_message="Welcome",
    )
    mock_client.sharing.add_user.assert_not_called()


async def test_share_batch_defaults_each_grant_to_viewer_and_no_email(mcp_call, mock_client):
    """Omitted grant settings retain the least-privilege role and silent notification mode."""
    mock_client.sharing.set_users = AsyncMock(
        return_value=ShareStatus(NB, False, ShareAccess.RESTRICTED, ShareViewLevel.FULL_NOTEBOOK)
    )
    await mcp_call(
        "share_set_user", {"notebook": NB, "grants": [{"email": "a@example.test"}], "confirm": True}
    )
    mock_client.sharing.set_users.assert_awaited_once_with(
        NB, [("a@example.test", SharePermission.VIEWER)], notify=False, welcome_message=""
    )


@pytest.mark.parametrize("confirm", [False, True])
async def test_share_batch_limit_rejects_before_client_open(mcp_call, monkeypatch, confirm):
    """An oversized recipient list cannot reach preview or mutation client acquisition."""
    get_client = AsyncMock(side_effect=AssertionError("unexpected client open"))
    monkeypatch.setattr(sharing_tools, "get_client", get_client)
    grants = [{"email": f"recipient-{index}@example.test"} for index in range(101)]
    with pytest.raises(ToolError):
        await mcp_call("share_set_user", {"notebook": NB, "grants": grants, "confirm": confirm})
    get_client.assert_not_awaited()


@pytest.mark.parametrize("confirm", [False, True])
async def test_share_batch_accepts_the_recipient_limit(mcp_call, mock_client, confirm):
    """The maximum supported subset previews in full and confirms with one write."""
    mock_client.notebooks.list = AsyncMock(return_value=[SimpleNamespace(id=NB, title="Team")])
    grants = [
        {"email": f"recipient-{index}@example.test", "permission": "viewer"} for index in range(100)
    ]
    mock_client.sharing.set_users = AsyncMock(
        return_value=ShareStatus(NB, False, ShareAccess.RESTRICTED, ShareViewLevel.FULL_NOTEBOOK)
    )
    result = await mcp_call(
        "share_set_user", {"notebook": NB, "grants": grants, "confirm": confirm}
    )
    if confirm:
        assert result.structured_content["status"] == "updated"
        mock_client.sharing.set_users.assert_awaited_once_with(
            NB,
            [(grant["email"], SharePermission.VIEWER) for grant in grants],
            notify=False,
            welcome_message="",
        )
    else:
        assert result.structured_content["preview"]["grants"] == grants
        mock_client.sharing.set_users.assert_not_awaited()


@pytest.mark.parametrize(
    "args",
    [
        {},
        {"email": "a@example.test", "grants": GRANTS},
        {"grants": []},
        {"grants": [GRANTS[0], GRANTS[0]]},
        {"grants": [{"email": " "}]},
        {"grants": [{"email": "a@example.test", "permission": "owner"}]},
        {"grants": [{"email": "a@example.test", "notify": True}]},
        {"grants": GRANTS, "permission": "editor"},
    ],
)
async def test_share_batch_invalid_input_never_opens_client(
    mcp_call, mock_client, monkeypatch, args
):
    """Malformed, conflicting, or duplicate grants fail before any client access."""
    get_client = AsyncMock(side_effect=AssertionError("unexpected client open"))
    monkeypatch.setattr(sharing_tools, "get_client", get_client)
    with pytest.raises(ToolError):
        await mcp_call("share_set_user", {"notebook": "missing notebook", **args})
    get_client.assert_not_awaited()
    mock_client.sharing.add_user.assert_not_called()
    mock_client.sharing.set_users.assert_not_called()


# --- single-item rename and confirmed delete resolve strictly ---------------


async def test_studio_delete_preview_still_resolves_a_title_prefix(mcp_call, studio_client):
    """The preview stays loose and hands back the canonical id to confirm with."""
    result = await mcp_call("studio_delete", {"notebook": NB, "item": "Fir"})
    assert result.structured_content["status"] == "needs_confirmation"
    assert result.structured_content["preview"]["item_id"] == NOTE_A
    studio_client.notes.delete.assert_not_awaited()


@pytest.mark.parametrize("title", ["First", "İstanbul notları"])
async def test_studio_delete_confirmed_loose_title_is_rejected(mcp_call, studio_client, title):
    """A confirmed delete by title prefix or accent-insensitive title deletes nothing."""
    studio_client.notes.list.return_value[0].title = title
    ref = "Fir" if title == "First" else "istanbul notları"
    with pytest.raises(ToolError, match="exact title or the id"):
        await mcp_call("studio_delete", {"notebook": NB, "item": ref, "confirm": True})
    studio_client.notes.delete.assert_not_awaited()
    studio_client.artifacts.delete.assert_not_awaited()


async def test_studio_rename_title_prefix_is_rejected(mcp_call, studio_client):
    """Rename has no preview, so a title prefix never picks the item to rename."""
    studio_client.notes.update = AsyncMock()
    studio_client.artifacts.rename = AsyncMock()
    with pytest.raises(ToolError, match="exact title or the id"):
        await mcp_call("studio_rename", {"notebook": NB, "item": "Fir", "new_title": "New"})
    studio_client.notes.update.assert_not_awaited()
    studio_client.artifacts.rename.assert_not_awaited()


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("studio_delete", {"item": "Map", "confirm": True}),
        ("studio_rename", {"item": "Map", "new_title": "New"}),
    ],
)
async def test_single_item_mutation_by_title_needs_a_complete_listing(
    mcp_call, studio_client, tool, args
):
    """A hidden backing cannot make a title look unique to a rename or delete."""
    studio_client.artifacts.rename = AsyncMock()
    studio_client.artifacts.list_with_status = AsyncMock(
        return_value=ArtifactListing(
            items=(),
            is_complete=False,
            failures=(
                ArtifactListingFailure(
                    component=ArtifactListingComponent.NOTE_BACKED_MIND_MAPS,
                    error_type="RPCError",
                    message="The note-backed mind-map listing is unavailable",
                ),
            ),
        )
    )
    with pytest.raises(ToolError, match="Artifact lookup is incomplete"):
        await mcp_call(tool, {"notebook": NB, **args})
    studio_client.notes.delete.assert_not_awaited()
    studio_client.artifacts.delete.assert_not_awaited()
    studio_client.artifacts.rename.assert_not_awaited()


async def test_studio_delete_confirmed_exact_title_still_deletes(mcp_call, studio_client):
    result = await mcp_call("studio_delete", {"notebook": NB, "item": "first", "confirm": True})
    assert result.structured_content["status"] == "deleted"
    assert result.structured_content["item_id"] == NOTE_A
    studio_client.notes.delete.assert_awaited()


async def test_studio_delete_confirmed_full_id_skips_the_completeness_check(
    mcp_call, studio_client
):
    """A canonical id never needs the full listing, so delete-by-id stays available."""
    studio_client.artifacts.list_with_status = AsyncMock()
    result = await mcp_call("studio_delete", {"notebook": NB, "item": NOTE_A, "confirm": True})
    assert result.structured_content["status"] == "deleted"
    studio_client.artifacts.list_with_status.assert_not_awaited()
