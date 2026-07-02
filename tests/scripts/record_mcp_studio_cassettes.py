"""Maintainer helper: record the MCP Studio mutating-op cassettes against live API.

``studio_delete`` and ``studio_rename`` are both cross-type (note∩artifact): they
resolve ``item`` over the merged list, then route by resolved type. That issues
``GET_NOTES_AND_MIND_MAPS`` (``cFji9``) + ``LIST_ARTIFACTS`` (``gArtLc``) several
times — a merged-list resolve preflight plus, on the artifact path, a kind-aware
``mind_maps`` probe — BEFORE the mutation RPC, a sequence no CLI cassette holds.
This script drives the **MCP server** (real ``NotebookLMClient``, auth from your
``~/.notebooklm`` profile) under VCR record mode so the recorded ``f.req`` shapes
are exactly what the Studio tools emit.

It is SELF-CONTAINED and DESTRUCTIVE-SAFE: it creates a throwaway scratch notebook
(one text source, one text note, one generated report), records the three cassettes
against THAT notebook, then deletes the scratch notebook in a ``finally``. It never
touches any pre-existing notebook.

Records (into ``tests/cassettes``):
  * ``mcp_studio_rename.yaml``        — studio_rename(report) → cFji9×4 + gArtLc×3 + rc3d8d
  * ``mcp_studio_delete_note.yaml``   — studio_delete(note)   → cFji9×2 + gArtLc + AH0mwd
  * ``mcp_studio_delete_artifact.yaml`` — studio_delete(report) → cFji9×3 + gArtLc + V5N4be

Usage (maintainer, one Google account; consumes a little generation quota)::

    uv run python tests/scripts/record_mcp_studio_cassettes.py

After recording, verify the cassettes are clean:

    uv run python tests/scripts/check_cassettes_clean.py --strict --recursive

CI never runs this script. Replay uses scrubbed cassettes + mock auth.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

# Make the repo root importable so ``tests.vcr_config`` resolves under a plain
# ``python`` run (pytest sets pythonpath=["."]; a direct run does not).
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# Record mode + keepalive-poke disable MUST be set before importing the VCR config
# (its record_mode is computed at import time) and before any client opens (so the
# RotateCookies poke never records a stray interaction into a cassette).
os.environ["NOTEBOOKLM_VCR_RECORD"] = "1"
os.environ["NOTEBOOKLM_DISABLE_KEEPALIVE_POKE"] = "1"

from fastmcp import Client  # noqa: E402
from tests.vcr_config import notebooklm_vcr  # noqa: E402

from notebooklm.mcp.server import create_server  # noqa: E402

SCRATCH_TITLE = "zzz-mcp-studio-vcr-scratch"
SOURCE_TEXT = (
    "The mitochondrion is the powerhouse of the cell. It generates most of the "
    "cell's supply of adenosine triphosphate (ATP), used as a source of chemical "
    "energy. This is scratch content for a throwaway VCR recording notebook."
)


def _sc(result: object) -> dict:
    """Pull the structured_content dict off a FastMCP call_tool result."""
    sc = getattr(result, "structured_content", None)
    assert isinstance(sc, dict), f"expected structured_content dict, got {result!r}"
    return sc


async def main() -> None:
    server = create_server()
    async with Client(server) as mcp:
        nb_id: str | None = None
        try:
            # --- SETUP (outside any cassette → live, NOT recorded) ---------------
            nb_id = _sc(await mcp.call_tool("notebook_create", {"title": SCRATCH_TITLE}))[
                "notebook_id"
            ]
            print(f"scratch notebook: {nb_id}")

            await mcp.call_tool(
                "source_add",
                {
                    "notebook": nb_id,
                    "source_type": "text",
                    "text": SOURCE_TEXT,
                    "title": "Scratch source",
                },
            )
            await mcp.call_tool("source_wait", {"notebook": nb_id, "timeout": 180})

            note_id = _sc(
                await mcp.call_tool(
                    "note_save",
                    {"notebook": nb_id, "title": "Scratch note", "content": "Delete me."},
                )
            )["note_id"]
            print(f"scratch note: {note_id}")

            gen = _sc(
                await mcp.call_tool(
                    "studio_generate", {"notebook": nb_id, "artifact_type": "report"}
                )
            )
            print(f"generating report: task_id={gen.get('task_id')}")

            # The mutations resolve `item` over the MERGED notes+artifacts list, so
            # both the note and the report must be stably present there before we
            # record — otherwise resolve_studio_item misses and the op degrades to the
            # full-UUID carve-out (type "unknown"), which is NOT the cross-type route
            # these cassettes exist to pin. Settle until BOTH resolve to their expected
            # type via studio_list(item=…) on the SAME listing, and grab the report's
            # own listed id there (the generation task_id may differ from it).
            art_id: str | None = None
            for _ in range(40):
                items = _sc(await mcp.call_tool("studio_list", {"notebook": nb_id}))["items"]
                reports = [it for it in items if it.get("type") == "report"]
                has_note = any(it.get("id") == note_id and it.get("type") == "note" for it in items)
                if reports and has_note:
                    art_id = str(reports[0]["id"])
                    break
                await asyncio.sleep(3)
            if art_id is None:
                raise SystemExit("note+report never co-appeared in studio_list")
            print(f"report artifact: {art_id}")

            # --- RECORD (each op in its own cassette) ---------------------------
            with notebooklm_vcr.use_cassette("mcp_studio_rename.yaml"):
                r = _sc(
                    await mcp.call_tool(
                        "studio_rename",
                        {"notebook": nb_id, "item": art_id, "new_title": "Renamed by VCR"},
                    )
                )
                print("rename ->", r)

            with notebooklm_vcr.use_cassette("mcp_studio_delete_note.yaml"):
                r = _sc(
                    await mcp.call_tool(
                        "studio_delete", {"notebook": nb_id, "item": note_id, "confirm": True}
                    )
                )
                print("delete note ->", r)

            with notebooklm_vcr.use_cassette("mcp_studio_delete_artifact.yaml"):
                r = _sc(
                    await mcp.call_tool(
                        "studio_delete", {"notebook": nb_id, "item": art_id, "confirm": True}
                    )
                )
                print("delete artifact ->", r)

            print("\nRECORDED IDS (for the replay tests):")
            print(f"  RENAME/DELETE_ARTIFACT nb={nb_id} art={art_id}")
            print(f"  DELETE_NOTE            nb={nb_id} note={note_id}")
        finally:
            if nb_id is not None:
                await mcp.call_tool("notebook_delete", {"notebook": nb_id, "confirm": True})
                print(f"cleaned up scratch notebook {nb_id}")


if __name__ == "__main__":
    asyncio.run(main())
