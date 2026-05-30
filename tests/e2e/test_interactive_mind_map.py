"""E2E: the interactive (studio-artifact) mind map lifecycle.

Exercises the real NotebookLM API end-to-end for the new interactive mind map
(type 4 / variant 4, created via CREATE_ARTIFACT): generate -> poll -> recognize
-> read tree (GET_INTERACTIVE_HTML) -> rename (RENAME_ARTIFACT) -> delete
(DELETE_ARTIFACT). Marked e2e (auto), so it only runs with real auth and
``-m e2e``. The wire lifecycle was validated live while authoring #1256 Phase 2.

Run: ``uv run pytest tests/e2e/test_interactive_mind_map.py -m e2e``
"""

from __future__ import annotations

import asyncio
import json

import pytest

from notebooklm._artifact_payloads import build_interactive_mind_map_artifact_params
from notebooklm.rpc import RPCMethod, safe_index
from notebooklm.types import ArtifactType


async def _wait_for_interactive(client, notebook_id, *, timeout=240):
    """Poll until the interactive mind map reaches a terminal state; return it."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        for art in await client.artifacts.list(notebook_id, ArtifactType.MIND_MAP):
            if art.is_interactive_mind_map:
                if art.is_completed:
                    return art
                if art.is_failed:
                    pytest.fail(f"interactive mind map generation failed: {art.id}")
        await asyncio.sleep(5)
    pytest.fail("interactive mind map did not complete within timeout")


@pytest.mark.asyncio
async def test_interactive_mind_map_full_lifecycle(client, generation_notebook_id):
    nb_id = generation_notebook_id
    source_ids = await client.notebooks.get_source_ids(nb_id)
    assert source_ids, "generation notebook must have at least one source"

    # --- generate (CREATE_ARTIFACT, type 4 / variant 4) ---
    await client._rpc_executor.rpc_call(
        method=RPCMethod.CREATE_ARTIFACT,
        params=build_interactive_mind_map_artifact_params(nb_id, source_ids),
        source_path=f"/notebook/{nb_id}",
        allow_null=True,
    )

    artifact = await _wait_for_interactive(client, nb_id)
    try:
        # --- recognition (Phase 1) ---
        assert artifact.kind == ArtifactType.MIND_MAP
        assert artifact.is_interactive_mind_map is True

        # --- read tree (GET_INTERACTIVE_HTML returns it at [0][9][3]) ---
        result = await client._rpc_executor.rpc_call(
            method=RPCMethod.GET_INTERACTIVE_HTML,
            params=[artifact.id],
            source_path=f"/notebook/{nb_id}",
            allow_null=True,
        )
        tree_json = safe_index(result, 0, 9, 3)
        tree = json.loads(tree_json)
        assert "name" in tree and "children" in tree

        # --- rename (RENAME_ARTIFACT) ---
        await client.artifacts.rename(nb_id, artifact.id, "E2E Interactive Mind Map")
        renamed = next(
            a
            for a in await client.artifacts.list(nb_id, ArtifactType.MIND_MAP)
            if a.id == artifact.id
        )
        assert renamed.title == "E2E Interactive Mind Map"
    finally:
        # --- delete (DELETE_ARTIFACT) ---
        await client.artifacts.delete(nb_id, artifact.id)

    remaining = [
        a.id
        for a in await client.artifacts.list(nb_id, ArtifactType.MIND_MAP)
        if a.is_interactive_mind_map
    ]
    assert artifact.id not in remaining
