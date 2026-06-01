"""E2E: the interactive (studio-artifact) mind map lifecycle.

Exercises the real NotebookLM API end-to-end through the public
``client.mind_maps`` surface for the new interactive mind map (type 4 /
variant 4, created via CREATE_ARTIFACT): generate -> poll -> read tree
(GET_INTERACTIVE_HTML) -> rename (RENAME_ARTIFACT) -> delete (DELETE_ARTIFACT).
Marked ``e2e``, so it only runs with real auth and ``-m e2e``. The wire
lifecycle was validated live while authoring #1256 Phase 2.

Run: ``uv run pytest tests/e2e/test_interactive_mind_map.py -m e2e``
"""

from __future__ import annotations

import pytest

from notebooklm.types import MindMapKind


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_interactive_mind_map_full_lifecycle(client, generation_notebook_id):
    nb_id = generation_notebook_id
    source_ids = await client.notebooks.get_source_ids(nb_id)
    assert source_ids, "generation notebook must have at least one source"

    # --- generate (CREATE_ARTIFACT, type 4 / variant 4) + poll to completion ---
    mind_map = await client.mind_maps.generate(
        nb_id, source_ids, kind=MindMapKind.INTERACTIVE, wait=True
    )
    # Enter the cleanup guard immediately after create so a failed assertion
    # below never leaks the real interactive artifact in the live notebook.
    try:
        assert mind_map.kind == MindMapKind.INTERACTIVE
        assert mind_map.id, "generate() must return a non-empty interactive artifact id"

        # --- recognition (Phase 1) ---
        listed = {m.id: m for m in await client.mind_maps.list(nb_id)}
        assert mind_map.id in listed
        assert listed[mind_map.id].kind == MindMapKind.INTERACTIVE

        # --- read tree (GET_INTERACTIVE_HTML returns it at [0][9][3]) ---
        tree = await client.mind_maps.get_tree(nb_id, mind_map.id, kind=MindMapKind.INTERACTIVE)
        assert isinstance(tree, dict)
        assert "name" in tree and "children" in tree

        # --- rename (RENAME_ARTIFACT) ---
        await client.mind_maps.rename(
            nb_id, mind_map.id, "E2E Interactive Mind Map", kind=MindMapKind.INTERACTIVE
        )
        renamed = next(m for m in await client.mind_maps.list(nb_id) if m.id == mind_map.id)
        assert renamed.title == "E2E Interactive Mind Map"
    finally:
        # --- delete (DELETE_ARTIFACT) ---
        if mind_map.id:
            await client.mind_maps.delete(nb_id, mind_map.id, kind=MindMapKind.INTERACTIVE)

    remaining = [
        m.id for m in await client.mind_maps.list(nb_id) if m.kind == MindMapKind.INTERACTIVE
    ]
    assert mind_map.id not in remaining
