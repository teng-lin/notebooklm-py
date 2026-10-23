"""Tools that change data resolve names with tier 1 only.

Tiered title matching (#2476) lets ``"istanbul"`` find ``"İstanbul"`` and
``"resume"`` find ``"Résumé"``. That is fine for reads and confirmation
previews, but a rename, add, share change or confirmed delete must not act on an
accent-insensitive guess: with a single notebook titled ``"Résumé"``,
``notebook_delete(notebook="resume", confirm=True)`` must not delete it. These
tests pin which tools are strict, that the strict path still keeps the tier-1
title prefix, and that previews and reads keep every tier.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("fastmcp")

from fastmcp.exceptions import ToolError  # noqa: E402 - after importorskip guard

from notebooklm._app.resolve import AmbiguousIdError  # noqa: E402 - after importorskip guard
from notebooklm.exceptions import (  # noqa: E402 - after importorskip guard
    NotebookNotFoundError,
    RPCError,
    ValidationError,
)
from notebooklm.mcp._resolve import _resolve_by_title  # noqa: E402 - after importorskip guard
from notebooklm.mcp.tools._preview import (  # noqa: E402 - after importorskip guard
    notebook_title_or_none,
    title_for_id,
)
from notebooklm.mcp.tools._studio_items import (  # noqa: E402 - after importorskip guard
    _match_studio_ref,
)

from .conftest import AsyncMock  # noqa: E402 - after importorskip guard

NB_ID = "11111111-1111-1111-1111-111111111111"
NB2_ID = "22222222-2222-2222-2222-222222222222"
SRC_ID = "33333333-3333-3333-3333-333333333333"
NOTE_ID = "44444444-4444-4444-4444-444444444444"
ITEM_ID = "55555555-5555-4555-8555-555555555555"
#: Has hex letters, so its uppercase spelling differs from the listed id.
HEX_NB_ID = "abcdef12-3456-4789-abcd-ef1234567890"

ACCENT_ONLY = "only when ignoring accents"


@dataclass
class Titled:
    id: str
    title: str | None


# ---------------------------------------------------------------------------
# The resolver itself
# ---------------------------------------------------------------------------


def test_strict_refuses_an_accent_only_match_and_names_the_candidate() -> None:
    items = [Titled(NB_ID, "Résumé")]
    with pytest.raises(ValidationError) as excinfo:
        _resolve_by_title("resume", items, not_found=NotebookNotFoundError, strict=True)
    assert type(excinfo.value) is ValidationError  # not AmbiguousIdError
    assert ACCENT_ONLY in str(excinfo.value)
    assert NB_ID in str(excinfo.value)


def test_strict_refuses_several_accent_only_matches_without_calling_them_ambiguous() -> None:
    items = [Titled(NB_ID, "Résumé"), Titled(NB2_ID, "Resumé")]
    with pytest.raises(ValidationError) as excinfo:
        _resolve_by_title("resume", items, not_found=NotebookNotFoundError, strict=True)
    assert type(excinfo.value) is ValidationError
    assert NB_ID in str(excinfo.value) and NB2_ID in str(excinfo.value)


def test_strict_keeps_the_tier1_title_prefix() -> None:
    items = [Titled(NB_ID, "Résumé 2026"), Titled(NB2_ID, "Other")]
    assert _resolve_by_title("résumé", items, not_found=NotebookNotFoundError, strict=True) == NB_ID


def test_strict_keeps_reporting_an_ambiguous_tier1_prefix() -> None:
    items = [Titled(NB_ID, "Report Q1"), Titled(NB2_ID, "Report Q2")]
    with pytest.raises(AmbiguousIdError):
        _resolve_by_title("report", items, not_found=NotebookNotFoundError, strict=True)


def test_strict_true_miss_is_still_not_found() -> None:
    items = [Titled(NB_ID, "Résumé")]
    with pytest.raises(NotebookNotFoundError):
        _resolve_by_title("unrelated", items, not_found=NotebookNotFoundError, strict=True)


def test_strict_does_not_change_the_dotted_capital_i_rule() -> None:
    """``"istanbul"`` reaches ``"İstanbul"`` only through tier 2, so strict refuses it."""
    items = [Titled(NB_ID, "İstanbul notları")]
    with pytest.raises(ValidationError, match=ACCENT_ONLY):
        _resolve_by_title("istanbul", items, not_found=NotebookNotFoundError, strict=True)
    assert _resolve_by_title("istanbul", items, not_found=NotebookNotFoundError) == NB_ID


# ---------------------------------------------------------------------------
# Notebook refs on tools that change data
# ---------------------------------------------------------------------------

#: Every tool that writes, with arguments that reach the notebook lookup. A
#: notebook titled "Résumé" is the only one; "resume" matches it only in tier 2.
STRICT_NOTEBOOK_CALLS: list[tuple[str, dict[str, Any]]] = [
    ("notebook_rename", {"new_title": "New"}),
    ("notebook_delete", {"confirm": True}),
    ("share_set_access", {"public": False}),
    ("share_set_user", {"email": "a@example.com", "confirm": True}),
    ("share_remove_user", {"email": "a@example.com", "confirm": True}),
    ("note_save", {"title": "t", "content": "c"}),
    ("source_add", {"source_type": "text", "text": "hello", "title": "t"}),
    ("source_rename", {"source": SRC_ID, "new_title": "New"}),
    ("source_delete", {"source": SRC_ID, "confirm": True}),
    ("source_delete", {"sources": [SRC_ID], "confirm": True}),
    ("studio_generate", {"artifact_type": "report"}),
    ("studio_rename", {"item": ITEM_ID, "new_title": "New"}),
    ("studio_retry", {"artifact": ITEM_ID}),
    ("studio_delete", {"item": ITEM_ID, "confirm": True}),
    ("research_start", {"query": "q"}),
    ("research_import", {"poll_task_id": "task"}),
    ("research_cancel", {"poll_task_id": "task"}),
    ("chat_configure", {"chat_mode": "concise"}),
    ("chat_cancel", {}),
]


@pytest.mark.parametrize(
    ("tool", "args"),
    STRICT_NOTEBOOK_CALLS,
    ids=[f"{t}-{i}" for i, (t, _) in enumerate(STRICT_NOTEBOOK_CALLS)],
)
async def test_write_tools_refuse_an_accent_only_notebook_name(
    mcp_call, mock_client, tool: str, args: dict[str, Any]
) -> None:
    mock_client.notebooks.list = AsyncMock(return_value=[Titled(NB_ID, "Résumé")])
    with pytest.raises(ToolError) as excinfo:
        await mcp_call(tool, {"notebook": "resume", **args})
    message = str(excinfo.value)
    assert "VALIDATION" in message
    assert ACCENT_ONLY in message


async def test_confirmed_notebook_delete_by_accent_only_name_deletes_nothing(
    mcp_call, mock_client
) -> None:
    mock_client.notebooks.list = AsyncMock(return_value=[Titled(NB_ID, "Résumé")])
    mock_client.notebooks.delete = AsyncMock(return_value=None)
    with pytest.raises(ToolError, match=ACCENT_ONLY):
        await mcp_call("notebook_delete", {"notebook": "resume", "confirm": True})
    mock_client.notebooks.delete.assert_not_called()


async def test_notebook_rename_by_accent_only_name_renames_nothing(mcp_call, mock_client) -> None:
    mock_client.notebooks.list = AsyncMock(return_value=[Titled(NB_ID, "Résumé")])
    mock_client.notebooks.rename = AsyncMock(return_value=None)
    with pytest.raises(ToolError, match=ACCENT_ONLY):
        await mcp_call("notebook_rename", {"notebook": "resume", "new_title": "New"})
    mock_client.notebooks.rename.assert_not_called()


async def test_confirmed_share_set_user_by_accent_only_name_shares_nothing(
    mcp_call, mock_client
) -> None:
    mock_client.notebooks.list = AsyncMock(return_value=[Titled(NB_ID, "Résumé")])
    mock_client.sharing.add_user = AsyncMock(return_value=None)
    with pytest.raises(ToolError, match=ACCENT_ONLY):
        await mcp_call(
            "share_set_user",
            {"notebook": "resume", "email": "a@example.com", "confirm": True},
        )
    mock_client.sharing.add_user.assert_not_called()


async def test_write_tool_keeps_the_tier1_title_prefix(mcp_call, mock_client) -> None:
    """Strict is not exact-only: a unique tier-1 prefix still renames (backward compatible)."""
    mock_client.notebooks.list = AsyncMock(
        return_value=[Titled(NB_ID, "Résumé 2026"), Titled(NB2_ID, "Other")]
    )
    mock_client.notebooks.rename = AsyncMock(return_value=SimpleNamespace(id=NB_ID, title="New"))
    await mcp_call("notebook_rename", {"notebook": "Résumé", "new_title": "New"})
    mock_client.notebooks.rename.assert_awaited_once()
    assert mock_client.notebooks.rename.await_args.args[0] == NB_ID


# ---------------------------------------------------------------------------
# Previews and reads keep every tier
# ---------------------------------------------------------------------------


async def test_delete_preview_still_finds_an_accent_only_name(mcp_call, mock_client) -> None:
    mock_client.notebooks.list = AsyncMock(return_value=[Titled(NB_ID, "Résumé")])
    mock_client.notebooks.delete = AsyncMock(return_value=None)
    result = await mcp_call("notebook_delete", {"notebook": "resume"})
    assert result.structured_content["preview"]["notebook_id"] == NB_ID
    mock_client.notebooks.delete.assert_not_called()


async def test_share_set_access_widening_preview_still_finds_an_accent_only_name(
    mcp_call, mock_client
) -> None:
    mock_client.notebooks.list = AsyncMock(return_value=[Titled(NB_ID, "Résumé")])
    mock_client.sharing.get_status = AsyncMock(return_value=SimpleNamespace(is_public=False))
    mock_client.sharing.set_public = AsyncMock()
    result = await mcp_call("share_set_access", {"notebook": "resume", "public": True})
    assert result.structured_content["status"] == "needs_confirmation"
    assert result.structured_content["preview"]["title"] == "Résumé"
    mock_client.sharing.set_public.assert_not_called()


async def test_share_set_access_on_an_already_public_notebook_writes_so_it_is_strict(
    mcp_call, mock_client
) -> None:
    """``public=True`` skips the preview when already public, so it is a write."""
    mock_client.notebooks.list = AsyncMock(return_value=[Titled(NB_ID, "Résumé")])
    mock_client.sharing.get_status = AsyncMock(return_value=SimpleNamespace(is_public=True))
    mock_client.sharing.set_public = AsyncMock()
    mock_client.sharing.set_view_level = AsyncMock()
    with pytest.raises(ToolError, match=ACCENT_ONLY):
        await mcp_call(
            "share_set_access", {"notebook": "resume", "public": True, "view_level": "chat"}
        )
    mock_client.sharing.set_public.assert_not_called()
    mock_client.sharing.set_view_level.assert_not_called()


async def test_share_set_access_refuses_when_the_strict_recheck_finds_another_notebook(
    mcp_call, mock_client
) -> None:
    """The strict re-check must confirm the notebook already read, not just any match.

    A notebook titled "resume" appears between the two list reads, so the strict
    lookup resolves to it while the status read and the writes would target the
    accent-only "Résumé".
    """
    mock_client.notebooks.list = AsyncMock(
        side_effect=[
            [Titled(NB_ID, "Résumé")],
            [Titled(NB_ID, "Résumé"), Titled(NB2_ID, "resume")],
        ]
    )
    mock_client.sharing.get_status = AsyncMock(return_value=SimpleNamespace(is_public=True))
    mock_client.sharing.set_public = AsyncMock()
    mock_client.sharing.set_view_level = AsyncMock()
    with pytest.raises(ToolError, match="different notebook"):
        await mcp_call(
            "share_set_access", {"notebook": "resume", "public": True, "view_level": "chat"}
        )
    mock_client.sharing.set_public.assert_not_called()
    mock_client.sharing.set_view_level.assert_not_called()


# ---------------------------------------------------------------------------
# Source and note refs on tools that change data
# ---------------------------------------------------------------------------


async def test_source_rename_refuses_an_accent_only_source_name(mcp_call, mock_client) -> None:
    mock_client.sources.list = AsyncMock(return_value=[Titled(SRC_ID, "Résumé")])
    mock_client.sources.rename = AsyncMock()
    with pytest.raises(ToolError, match=ACCENT_ONLY):
        await mcp_call("source_rename", {"notebook": NB_ID, "source": "resume", "new_title": "x"})
    mock_client.sources.rename.assert_not_called()


async def test_confirmed_source_delete_refuses_an_accent_only_source_name(
    mcp_call, mock_client
) -> None:
    mock_client.sources.list = AsyncMock(return_value=[Titled(SRC_ID, "Résumé")])
    mock_client.sources.delete = AsyncMock()
    with pytest.raises(ToolError, match=ACCENT_ONLY):
        await mcp_call("source_delete", {"notebook": NB_ID, "source": "resume", "confirm": True})
    mock_client.sources.delete.assert_not_called()


async def test_source_delete_preview_still_finds_an_accent_only_source_name(
    mcp_call, mock_client
) -> None:
    mock_client.sources.list = AsyncMock(return_value=[Titled(SRC_ID, "Résumé")])
    mock_client.sources.delete = AsyncMock()
    result = await mcp_call("source_delete", {"notebook": NB_ID, "source": "resume"})
    assert result.structured_content["preview"]["source_id"] == SRC_ID
    mock_client.sources.delete.assert_not_called()


async def test_note_save_refuses_an_accent_only_note_name(mcp_call, mock_client) -> None:
    mock_client.notes.list = AsyncMock(return_value=[Titled(NOTE_ID, "Résumé")])
    mock_client.notes.update = AsyncMock()
    with pytest.raises(ToolError, match=ACCENT_ONLY):
        await mcp_call("note_save", {"notebook": NB_ID, "note": "resume", "content": "new"})
    mock_client.notes.update.assert_not_called()


# ---------------------------------------------------------------------------
# Sharing previews read the title best-effort
# ---------------------------------------------------------------------------

SHARING_PREVIEWS: list[tuple[str, dict[str, Any], str]] = [
    ("share_set_user", {"email": "a@example.com"}, "add_user"),
    ("share_remove_user", {"email": "a@example.com"}, "remove_user"),
    ("share_set_access", {"public": True}, "set_public"),
]


@pytest.mark.parametrize(("tool", "args", "mutation"), SHARING_PREVIEWS)
async def test_sharing_preview_survives_a_failed_title_read(
    mcp_call, mock_client, tool: str, args: dict[str, Any], mutation: str
) -> None:
    """A full-UUID preview whose title read fails still previews, with ``title: None``."""
    mock_client.notebooks.list = AsyncMock(side_effect=RPCError("list failed"))
    mock_client.sharing.get_status = AsyncMock(return_value=SimpleNamespace(is_public=False))
    setattr(mock_client.sharing, mutation, AsyncMock())
    result = await mcp_call(tool, {"notebook": NB_ID, **args})
    assert result.structured_content["status"] == "needs_confirmation"
    assert result.structured_content["preview"]["notebook_id"] == NB_ID
    assert result.structured_content["preview"]["title"] is None
    getattr(mock_client.sharing, mutation).assert_not_called()


@pytest.mark.parametrize(
    ("tool", "args", "domain"),
    [
        ("notebook_delete", {"notebook": NB_ID}, "notebooks"),
        ("source_delete", {"notebook": NB_ID, "source": SRC_ID}, "sources"),
    ],
)
async def test_delete_preview_survives_a_failed_title_read(
    mcp_call, mock_client, tool: str, args: dict[str, Any], domain: str
) -> None:
    """The delete previews read the title best-effort, like the sharing previews."""
    getattr(mock_client, domain).list = AsyncMock(side_effect=RPCError("list failed"))
    getattr(mock_client, domain).delete = AsyncMock()
    result = await mcp_call(tool, args)
    assert result.structured_content["status"] == "needs_confirmation"
    assert result.structured_content["preview"]["title"] is None
    getattr(mock_client, domain).delete.assert_not_called()


@pytest.mark.parametrize(("tool", "args", "mutation"), SHARING_PREVIEWS)
async def test_sharing_preview_by_name_lists_once(
    mcp_call, mock_client, tool: str, args: dict[str, Any], mutation: str
) -> None:
    """The title comes from the list that resolved the name, not a second read."""
    mock_client.notebooks.list = AsyncMock(return_value=[Titled(NB_ID, "Team Notes")])
    mock_client.sharing.get_status = AsyncMock(return_value=SimpleNamespace(is_public=False))
    setattr(mock_client.sharing, mutation, AsyncMock())
    result = await mcp_call(tool, {"notebook": "Team Notes", **args})
    assert result.structured_content["preview"]["title"] == "Team Notes"
    assert mock_client.notebooks.list.await_count == 1
    getattr(mock_client.sharing, mutation).assert_not_called()


async def test_preview_title_read_propagates_cancellation(mock_client) -> None:
    """Only an ``Exception`` becomes ``title: None``; cancellation still propagates."""
    mock_client.notebooks.list = AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await notebook_title_or_none(mock_client, NB_ID)


@pytest.mark.parametrize(("tool", "args", "mutation"), SHARING_PREVIEWS)
async def test_sharing_preview_finds_the_title_for_an_uppercase_full_uuid(
    mcp_call, mock_client, tool: str, args: dict[str, Any], mutation: str
) -> None:
    """A full UUID is accepted in any case, while the list returns lowercase ids."""
    mock_client.notebooks.list = AsyncMock(return_value=[Titled(HEX_NB_ID, "Résumé")])
    mock_client.sharing.get_status = AsyncMock(return_value=SimpleNamespace(is_public=False))
    setattr(mock_client.sharing, mutation, AsyncMock())
    result = await mcp_call(tool, {"notebook": HEX_NB_ID.upper(), **args})
    assert result.structured_content["status"] == "needs_confirmation"
    assert result.structured_content["preview"]["title"] == "Résumé"
    getattr(mock_client.sharing, mutation).assert_not_called()


async def test_delete_preview_finds_the_title_for_an_uppercase_full_uuid(
    mcp_call, mock_client
) -> None:
    """``title_for_id`` is shared, so the delete preview gets the same fix."""
    mock_client.notebooks.list = AsyncMock(return_value=[Titled(HEX_NB_ID, "Résumé")])
    mock_client.notebooks.delete = AsyncMock(return_value=None)
    result = await mcp_call("notebook_delete", {"notebook": HEX_NB_ID.upper()})
    assert result.structured_content["preview"]["title"] == "Résumé"
    mock_client.notebooks.delete.assert_not_called()


async def test_source_delete_preview_finds_the_title_for_an_uppercase_full_uuid(
    mcp_call, mock_client
) -> None:
    mock_client.sources.list = AsyncMock(return_value=[Titled(HEX_NB_ID, "Résumé")])
    mock_client.sources.delete = AsyncMock()
    result = await mcp_call("source_delete", {"notebook": NB_ID, "source": HEX_NB_ID.upper()})
    assert result.structured_content["preview"]["title"] == "Résumé"
    mock_client.sources.delete.assert_not_called()


async def test_source_delete_preview_prefers_the_exact_id_over_a_case_variant(
    mcp_call, mock_client
) -> None:
    """Two ids that differ only in case are different items; the exact one wins."""
    mock_client.sources.list = AsyncMock(
        return_value=[Titled(HEX_NB_ID.upper(), "Upper"), Titled(HEX_NB_ID, "Lower")]
    )
    mock_client.sources.delete = AsyncMock()
    result = await mcp_call("source_delete", {"notebook": NB_ID, "source": HEX_NB_ID})
    assert result.structured_content["preview"]["title"] == "Lower"
    mock_client.sources.delete.assert_not_called()


def test_title_for_id_prefers_the_exact_id() -> None:
    items = [Titled("src-A", "first"), Titled("src-a", "second")]
    assert title_for_id(items, "src-a") == "second"


def test_title_for_id_compares_a_non_uuid_id_exactly() -> None:
    assert title_for_id([Titled("abc", "x")], "ABC") is None


def test_title_for_id_matches_a_full_uuid_in_any_case() -> None:
    assert title_for_id([Titled(HEX_NB_ID, "x")], HEX_NB_ID.upper()) == "x"


@pytest.mark.parametrize("ref", ["{" + HEX_NB_ID.upper() + "}", HEX_NB_ID.upper().replace("-", "")])
def test_title_for_id_folds_case_only_for_the_hyphenated_uuid_form(ref: str) -> None:
    assert title_for_id([Titled(HEX_NB_ID, "x")], ref) is None


def test_title_for_id_keeps_an_exact_match_without_a_title() -> None:
    items = [Titled(HEX_NB_ID.upper(), "Upper"), Titled(HEX_NB_ID, None)]
    assert title_for_id(items, HEX_NB_ID) is None


# ---------------------------------------------------------------------------
# Studio error wording names the step that matched
# ---------------------------------------------------------------------------


def _studio_item(item_id: str, title: str) -> dict[str, Any]:
    return {"id": item_id, "title": title, "type": "note"}


def test_studio_strict_error_says_a_prefix_matched() -> None:
    items = [_studio_item(ITEM_ID, "Résumé 2026")]
    with pytest.raises(ValidationError, match="only by title prefix"):
        _match_studio_ref(items, "Résumé", None, exact_title_only=True)


def test_studio_strict_error_says_only_ignoring_accents_matched() -> None:
    items = [_studio_item(ITEM_ID, "Résumé")]
    with pytest.raises(ValidationError, match=ACCENT_ONLY):
        _match_studio_ref(items, "resume", None, exact_title_only=True)
