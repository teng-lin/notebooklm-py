"""MCP sharing-tool VCR tests (reuse-only).

Full-stack coverage (MCP tool -> ``sharing.py`` adapter -> real
``NotebookLMClient`` -> VCR-replayed RPC) for the four sharing tools, reusing the
SAME cassettes the CLI ``share`` VCR suite recorded. ``NOTEBOOKLM_VCR_RECORD`` is
deliberately NOT set — no cassette is ever re-recorded here.

Closes the gap flagged in #1732: the sharing tools issue **mutating** access-grant
RPCs (``QDyure``) yet were exercised by unit tests with a mocked ``client`` only,
which cannot validate the real ``batchexecute`` request/response shapes — exactly
the position-sensitive-nested-params class of bug (CLAUDE.md pitfall #2) a mocked
test passes and a VCR test catches.

Every tool is invoked with the FULL canonical notebook UUID recorded in each
cassette so :func:`resolve_notebook` takes its full-UUID fast path and never adds
a ``LIST_NOTEBOOKS`` RPC the cassette lacks. The batchexecute body matcher is
shape-only, so the id/email/permission VALUES are decorative — the RPC *sequence*
and body *shape* are what replay.

Sequence map (each mutating op ends with a ``get_status`` re-read, ``JFMDGd``):

* ``share_status``     -> ``get_status``      -> ``JFMDGd``
* ``share_set_user``   -> ``add_user``        -> ``QDyure`` + ``JFMDGd``
* ``share_set_access`` -> ``set_public``      -> ``QDyure`` + ``JFMDGd``
* ``share_remove_user``-> ``remove_user``     -> ``QDyure`` + ``JFMDGd``

(The two RPC ids are distinct, so the ``freq`` matcher pairs them regardless of
cassette order or of extra unused interactions in a multi-op cassette.)
"""

from __future__ import annotations

import pytest

from tests.integration.conftest import skip_no_cassettes
from tests.vcr_config import notebooklm_vcr

from .conftest import build_mcp_client

pytestmark = [pytest.mark.vcr, skip_no_cassettes]

# The single recorded notebook id shared by all four cli_share_* / sharing_*
# cassettes. Decorative for matching — pins the full-UUID resolver fast path so no
# LIST_NOTEBOOKS RPC is issued.
SHARE_NOTEBOOK_ID = "62e5c8db-3dd2-407c-8d19-32ae4ae799db"


@pytest.mark.asyncio
@notebooklm_vcr.use_cassette("cli_share_status.yaml")
async def test_mcp_share_status_over_vcr() -> None:
    """``share_status`` reads sharing state through the real client over VCR.

    End-to-end: tool -> ``resolve_notebook`` (full UUID, no list) ->
    ``client.sharing.get_status`` -> ``GET_SHARE_STATUS`` (``JFMDGd``). Pins the
    string-labeled wire shape (``access`` / ``permission`` are labels, never the
    raw ``int, Enum`` values), and the ``view_level`` OMISSION (the read API
    cannot report it).
    """
    async with build_mcp_client() as mcp_client:
        result = await mcp_client.call_tool("share_status", {"notebook": SHARE_NOTEBOOK_ID})

    structured = result.structured_content
    assert isinstance(structured, dict)
    assert structured["notebook_id"] == SHARE_NOTEBOOK_ID
    assert isinstance(structured["is_public"], bool)
    # Access is a string label, never a raw int (the enums are ``int, Enum``).
    assert structured["access"] in {"restricted", "anyone_with_link"}
    assert isinstance(structured["shared_users"], list)
    for user in structured["shared_users"]:
        assert user["permission"] in {"owner", "editor", "viewer"}
    # share_url is always projected by _status_payload (may be None) — pin its
    # presence so a dropped key regresses loudly.
    assert "share_url" in structured
    # view_level is intentionally NOT surfaced by the read path.
    assert "view_level" not in structured


@pytest.mark.asyncio
@notebooklm_vcr.use_cassette("cli_share_add.yaml")
async def test_mcp_share_set_user_over_vcr() -> None:
    """``share_set_user`` grants access through the real client over VCR.

    End-to-end: tool -> ``client.sharing.add_user`` -> the mutating grant RPC
    (``QDyure``) THEN a ``get_status`` re-read (``JFMDGd``). This is the
    position-sensitive access-grant path #1732 flags — a mocked test cannot
    validate the ``QDyure`` body shape; VCR does.
    """
    async with build_mcp_client() as mcp_client:
        result = await mcp_client.call_tool(
            "share_set_user",
            {
                "notebook": SHARE_NOTEBOOK_ID,
                "email": "collaborator@example.com",
                "permission": "editor",
                # notify is shape-irrelevant (the freq matcher collapses the bool),
                # so False here replays fine against the notify=True recording.
                "notify": False,
                # confirm-gated (#1742): required to reach the mutating grant RPC;
                # confirm=True skips the gate's get_status, so add_user's own
                # QDyure+JFMDGd is the only traffic → matches the recording.
                "confirm": True,
            },
        )

    structured = result.structured_content
    assert isinstance(structured, dict)
    assert structured["status"] == "updated"
    assert structured["notebook_id"] == SHARE_NOTEBOOK_ID
    assert isinstance(structured["shared_users"], list)


@pytest.mark.asyncio
@notebooklm_vcr.use_cassette("sharing_set_public.yaml")
async def test_mcp_share_set_access_public_over_vcr() -> None:
    """``share_set_access(public=True)`` flips link access through VCR.

    End-to-end: tool -> ``client.sharing.set_public`` -> the mutating
    link-visibility RPC (``QDyure``) THEN a ``get_status`` re-read (``JFMDGd``).
    Reuses ``sharing_set_public.yaml`` (recorded from ``set_public``), whose
    ``QDyure`` body shape matches the link-visibility path (distinct from the
    per-user grant body). ``view_level`` stays omitted (this call did not set it).
    """
    async with build_mcp_client() as mcp_client:
        result = await mcp_client.call_tool(
            "share_set_access",
            # confirm-gated (#1742): confirm=True skips the widening get_status probe,
            # so set_public's own QDyure+JFMDGd matches the recorded 2-RPC cassette.
            {"notebook": SHARE_NOTEBOOK_ID, "public": True, "confirm": True},
        )

    structured = result.structured_content
    assert isinstance(structured, dict)
    assert structured["status"] == "updated"
    assert structured["notebook_id"] == SHARE_NOTEBOOK_ID
    assert isinstance(structured["is_public"], bool)
    # Not set on this call -> not echoed.
    assert "view_level" not in structured


@pytest.mark.asyncio
@notebooklm_vcr.use_cassette("cli_share_remove.yaml")
async def test_mcp_share_remove_user_over_vcr() -> None:
    """``share_remove_user(confirm=True)`` revokes access through VCR.

    End-to-end: tool -> ``client.sharing.remove_user`` -> the mutating revoke RPC
    (``QDyure``) THEN a ``get_status`` re-read (``JFMDGd``). Pins the flat
    ``{"status": "removed", "notebook_id", "email"}`` wire shape. ``confirm=True``
    is required — the default confirm-gated preview does not touch the wire.
    """
    async with build_mcp_client() as mcp_client:
        result = await mcp_client.call_tool(
            "share_remove_user",
            {
                "notebook": SHARE_NOTEBOOK_ID,
                "email": "collaborator@example.com",
                "confirm": True,
            },
        )

    structured = result.structured_content
    assert isinstance(structured, dict)
    assert structured["status"] == "removed"
    assert structured["notebook_id"] == SHARE_NOTEBOOK_ID
    assert structured["email"] == "collaborator@example.com"
