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

import contextlib

import pytest

from notebooklm.types import MindMapKind

# Live CREATE_ARTIFACT coverage — monitored by the nightly generation coverage
# floor so a fully-throttled run (every generation skipped) reds the nightly
# instead of passing hollow-green. See tests/e2e/conftest.py (#1819).
pytestmark = pytest.mark.live_generation


@pytest.fixture
async def swept_interactive_mind_maps(client, generation_notebook_id):
    """Guarantee no interactive mind map is orphaned in the live notebook.

    ``_install_generation_rate_limit_skip`` (conftest, #1819) wraps the WHOLE
    ``client.mind_maps.generate`` method, turning a quota ``RateLimitError``
    into ``pytest.skip``. That skip can fire on *any* RPC inside ``generate``
    that runs after ``CREATE_ARTIFACT`` has already created the artifact — the
    completion poll (``wait=True``) OR the settling ``_find_interactive``
    ``LIST_ARTIFACTS`` — before ``generate`` returns the id to the test. When
    it does, the test never binds ``mind_map`` and its own ``finally`` delete
    is skipped, orphaning the artifact (#1937).

    This teardown runs regardless of test outcome (pass / fail / skip) and
    deletes every interactive mind map left in the generation notebook, so no
    post-create skip location can leak one. A create-time skip raises before
    any artifact exists, so the sweep simply finds nothing. Best-effort: a
    throttled sweep can't clean up, but the next session's pre-test
    ``_cleanup_generation_notebook`` deletes all artifacts anyway.

    Enumerates via the *unfiltered* ``client.artifacts.list`` and matches
    ``is_interactive_mind_map`` OR ``is_unclassified_type4`` — mirroring
    ``_find_interactive(allow_unclassified=True)``. The strict
    ``client.mind_maps.list`` filter would exclude a still-settling type-4 row
    whose ``variant`` slot has not populated (``variant=None``), which is
    exactly the state a throttled settling ``_find_interactive`` LIST leaves
    behind — so the sweep must tolerate it too, or that narrowest window leaks.
    """
    yield
    with contextlib.suppress(Exception):
        for art in await client.artifacts.list(generation_notebook_id):
            if art.is_interactive_mind_map or art.is_unclassified_type4:
                with contextlib.suppress(Exception):
                    await client.artifacts.delete(generation_notebook_id, art.id)


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_interactive_mind_map_full_lifecycle(
    client, generation_notebook_id, swept_interactive_mind_maps
):
    nb_id = generation_notebook_id
    source_ids = await client.notebooks.get_source_ids(nb_id)
    assert source_ids, "generation notebook must have at least one source"

    # --- generate (CREATE_ARTIFACT, type 4 / variant 4) + poll to completion ---
    # A quota RateLimitError from anywhere inside generate (create, the
    # completion poll, or the settling _find_interactive list) is turned into
    # pytest.skip by the conftest wrapper (#1819) — potentially before this
    # returns and binds mind_map. The swept_interactive_mind_maps fixture is
    # the leak guard for that case; the try/finally below cleans up promptly on
    # the normal path and on an assertion failure (#1937).
    mind_map = await client.mind_maps.generate(
        nb_id, source_ids, kind=MindMapKind.INTERACTIVE, wait=True
    )
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
