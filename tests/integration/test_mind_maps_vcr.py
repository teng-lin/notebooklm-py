"""Cassette-backed coverage for the unified ``client.mind_maps`` API (#1256).

Exercises the new interactive (studio-artifact) path end-to-end against real
recorded wire data: ``mind_maps.list()`` recognizes the interactive mind map
(``LIST_ARTIFACTS`` + ``GET_NOTES_AND_MIND_MAPS``) and ``mind_maps.get_tree(...)``
reads its node tree via ``GET_INTERACTIVE_HTML`` (the tree lives at ``[0][9][3]``).

Recording
---------
Uses an existing notebook that already contains an interactive mind map (no
mutation, minimal cassette). To re-record::

    export NOTEBOOKLM_MINDMAP_NOTEBOOK_ID=f7d1e2b6-2334-4016-b81d-aded7b3fa9b6
    export NOTEBOOKLM_VCR_RECORD=1
    uv run pytest tests/integration/test_mind_maps_vcr.py -v

After recording, re-run the repo's cassette sanitizer (cookies/tokens) — the
interactive HTML blob at ``[0][9][0]`` is stubbed to keep the cassette small;
only the ``[0][9][3]`` tree is needed for replay.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from notebooklm import NotebookLMClient
from notebooklm.rpc.types import RPCMethod
from notebooklm.types import MindMapKind
from tests.integration.conftest import get_vcr_auth, skip_no_cassettes
from tests.vcr_config import notebooklm_vcr

pytestmark = [pytest.mark.vcr, skip_no_cassettes]

NOTEBOOK_ID = os.environ.get(
    "NOTEBOOKLM_MINDMAP_NOTEBOOK_ID",
    "f7d1e2b6-2334-4016-b81d-aded7b3fa9b6",
)

CASSETTE_NAME = "mind_maps_interactive.yaml"
CASSETTE_PATH = Path(__file__).parent.parent / "cassettes" / CASSETTE_NAME


class TestMindMapsInteractive:
    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette(CASSETTE_NAME)
    async def test_list_recognizes_and_reads_interactive_mind_map(self) -> None:
        auth = await get_vcr_auth()
        async with NotebookLMClient(auth) as client:
            maps = await client.mind_maps.list(NOTEBOOK_ID)
            interactive = [m for m in maps if m.kind == MindMapKind.INTERACTIVE]
            assert interactive, "expected at least one interactive mind map in the notebook"

            tree = await client.mind_maps.get_tree(
                NOTEBOOK_ID, interactive[0].id, kind=MindMapKind.INTERACTIVE
            )

        assert isinstance(tree, dict)
        assert "children" in tree or "nodes" in tree, f"missing tree keys: {list(tree)[:5]}"

    def test_cassette_records_interactive_rpcs(self) -> None:
        """The cassette must include the LIST_ARTIFACTS + GET_INTERACTIVE_HTML RPCs."""
        import yaml

        assert CASSETTE_PATH.exists(), (
            f"cassette missing: {CASSETTE_PATH}. Re-record with NOTEBOOKLM_VCR_RECORD=1."
        )
        data = yaml.safe_load(CASSETTE_PATH.read_text())
        uris = " ".join(i["request"]["uri"] for i in data["interactions"])
        # Reference the method-ID source of truth so re-recording with changed
        # RPC IDs can't silently make these assertions tautological.
        assert RPCMethod.LIST_ARTIFACTS.value in uris
        assert RPCMethod.GET_INTERACTIVE_HTML.value in uris
