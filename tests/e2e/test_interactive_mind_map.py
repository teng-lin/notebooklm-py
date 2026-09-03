"""E2E: the interactive (studio-artifact) mind map lifecycle.

Exercises the real NotebookLM API end-to-end through the public
``client.mind_maps`` surface for the new interactive mind map (type 4 /
variant 4, created via CREATE_ARTIFACT): generate -> poll -> read tree
(GET_INTERACTIVE_HTML) -> rename (RENAME_ARTIFACT) -> delete (DELETE_ARTIFACT).
Marked ``e2e``, so it only runs with real auth and ``-m e2e``. The wire
lifecycle was validated live while authoring #1256.

Run: ``uv run pytest tests/e2e/test_interactive_mind_map.py -m e2e``
"""

from __future__ import annotations

import asyncio

import pytest

from notebooklm.types import MindMapKind

from ._generation_helpers import _TYPED_RATE_LIMIT_ATTR
from .conftest import _RATE_LIMIT_METHOD_ATTR

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
    baseline = {
        art.id
        for art in await client.artifacts.list(generation_notebook_id)
        if art.is_interactive_mind_map or art.is_unclassified_type4
    }
    state = {
        "baseline": baseline,
        "operation": None,
        "typed_quota": False,
        "pre_accept_rejected": False,
    }
    yield state
    operation = state["operation"]
    attempts = 5 if state["typed_quota"] and not state["pre_accept_rejected"] else 1
    current = []
    for attempt in range(attempts):
        current = [
            art
            for art in await client.artifacts.list(generation_notebook_id)
            if art.is_interactive_mind_map or art.is_unclassified_type4
        ]
        if any(art.id not in baseline for art in current) or attempt == attempts - 1:
            break
        await asyncio.sleep(2)
    created = [art for art in current if art.id not in baseline]
    if (
        operation is not None
        and operation.last_event == "started"
        and state["typed_quota"]
        and not created
    ):
        operation.quota_no_commit_observed()
    multiple = operation is not None and len(created) > 1
    for art in created:
        if (
            operation is not None
            and operation.last_event == "started"
            and state["typed_quota"]
            and not multiple
        ):
            operation.discovered_accepted(art.id, reason="post_create_quota")
        await client.artifacts.delete(generation_notebook_id, art.id)
        remaining = {row.id for row in await client.artifacts.list(generation_notebook_id)}
        assert art.id not in remaining
        if (
            operation is not None
            and not multiple
            and operation.last_event
            in {
                "accepted",
                "persisted",
                "completed",
                "discovered_accepted",
            }
        ):
            operation.delete_confirmed(
                art.id,
                reason="post_create_quota" if state["typed_quota"] else "test_teardown",
            )
    if multiple:
        pytest.fail("interactive mind-map reconciliation found multiple new rows")
    if state["pre_accept_rejected"] and created:
        pytest.fail("pre-acceptance quota rejection unexpectedly created an artifact")


@pytest.mark.e2e
@pytest.mark.asyncio
@pytest.mark.timeout(360)
async def test_interactive_mind_map_full_lifecycle(
    client, generation_notebook_id, swept_interactive_mind_maps, generation_journal
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
    operation = generation_journal.operation(
        notebook_id=nb_id,
        family="mind_map",
        surface="client",
        id_kind="studio_task",
        lifecycle="test_owned",
    )
    swept_interactive_mind_maps["operation"] = operation
    try:
        mind_map = await client.mind_maps.generate(
            nb_id, source_ids, kind=MindMapKind.INTERACTIVE, wait=True
        )
    except BaseException as exc:
        typed_quota = bool(getattr(exc, _TYPED_RATE_LIMIT_ATTR, False))
        method_id = getattr(exc, _RATE_LIMIT_METHOD_ATTR, None)
        pre_accept_rejected = typed_quota and (
            method_id == "R7cb6c"
            or (isinstance(method_id, str) and method_id.endswith("/CreateArtifact"))
        )
        swept_interactive_mind_maps["typed_quota"] = typed_quota and not pre_accept_rejected
        swept_interactive_mind_maps["pre_accept_rejected"] = pre_accept_rejected
        if pre_accept_rejected:
            operation.rate_limited_rejected()
        raise
    operation.accepted(mind_map.id)
    try:
        assert mind_map.kind == MindMapKind.INTERACTIVE
        assert mind_map.id, "generate() must return a non-empty interactive artifact id"

        # --- recognition ---
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
        operation.completed(mind_map.id)
    finally:
        # --- delete (DELETE_ARTIFACT) ---
        if mind_map.id:
            await client.mind_maps.delete(nb_id, mind_map.id, kind=MindMapKind.INTERACTIVE)
            remaining = [
                m.id
                for m in await client.mind_maps.list(nb_id)
                if m.kind == MindMapKind.INTERACTIVE
            ]
            assert mind_map.id not in remaining
            operation.delete_confirmed(mind_map.id, reason="test_teardown")

    remaining = [
        m.id for m in await client.mind_maps.list(nb_id) if m.kind == MindMapKind.INTERACTIVE
    ]
    assert mind_map.id not in remaining
