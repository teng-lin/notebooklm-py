"""Multi-interaction mind-map chain cassette.

``ArtifactsAPI.generate_mind_map`` is one of the few public-API entry points
that emits **multiple sequential RPCs** in a single call. The flow is:

1. ``GENERATE_MIND_MAP`` (``yyryJe``) — generates the mind-map JSON but does
   not persist it server-side.
2. ``CREATE_NOTE`` (``CYK0Xb``) — creates an empty note row to hold the
   mind-map content.
3. ``UPDATE_NOTE`` (``cYAfTb``) — writes the mind-map JSON and the title
   (derived from the mind-map ``name`` field) into the note row.

A single-RPC cassette per call would not exercise the chain wiring (note-id
plumbed from CREATE_NOTE's response into UPDATE_NOTE's params). This module
records ALL THREE RPCs into one cassette so the integration test replays the
full chain in order — closing the multi-interaction coverage gap.

Recording
---------
Pre-condition: the generation notebook (``NOTEBOOKLM_GENERATION_NOTEBOOK_ID``)
must have at least one ready source attached. A Wikipedia page
("NotebookLM - Wikipedia") was added to ``bb00c9e3-656c-4fd2-b890-2b71e1cf3814``; the
``source_ids`` list passed below is the single source UUID from that page.

To re-record this cassette::

    export NOTEBOOKLM_GENERATION_NOTEBOOK_ID=bb00c9e3-656c-4fd2-b890-2b71e1cf3814
    export NOTEBOOKLM_VCR_RECORD=1
    uv run pytest tests/integration/test_mind_map_chain_vcr.py -v

Replay
------
The default VCR matcher includes ``rpcids`` which deterministically
disambiguates the three batchexecute POSTs by the RPC ID in the URL query
string. No per-cassette ``match_on`` override is needed.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
import yaml

# Add tests directory to path for vcr_config import (parity with the rest of
# tests/integration/test_vcr_*.py — these files are imported by pytest with
# the repo root NOT on sys.path).
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from conftest import get_vcr_auth, skip_no_cassettes  # noqa: E402
from notebooklm import NotebookLMClient  # noqa: E402
from notebooklm.rpc import RPCMethod  # noqa: E402
from vcr_config import notebooklm_vcr  # noqa: E402

pytestmark = [pytest.mark.vcr, skip_no_cassettes]

# Canonical recording notebook (carries the Wikipedia "NotebookLM"
# page added during fixture seeding). The env var override is only
# consulted when recording —
# during replay the cassette drives the response regardless of notebook ID,
# but we still need a stable value so the URL ``source-path`` query param
# matches what was recorded.
MUTABLE_NOTEBOOK_ID = os.environ.get(
    "NOTEBOOKLM_GENERATION_NOTEBOOK_ID",
    "bb00c9e3-656c-4fd2-b890-2b71e1cf3814",
)

# Source ID for the Wikipedia "NotebookLM" page attached to the generation
# notebook. Passing this explicitly skips the implicit ``GET_NOTEBOOK`` call
# that ``generate_mind_map`` would otherwise issue to enumerate sources —
# keeping the cassette to the three RPCs the chain itself emits.
_WIKIPEDIA_SOURCE_ID = "466b9ee3-c1ce-45ef-861c-1d4bfcd939ad"

CASSETTE_NAME = "generate_mind_map_chain.yaml"
CASSETTE_PATH = Path(__file__).parent.parent / "cassettes" / CASSETTE_NAME


class TestMindMapChain:
    """Records and replays the GENERATE_MIND_MAP → CREATE_NOTE → UPDATE_NOTE chain."""

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette(CASSETTE_NAME)
    async def test_generate_mind_map_chain(self) -> None:
        """End-to-end mind-map chain produces a persisted note.

        Asserts the public API contract: callers receive a dict with the
        parsed ``mind_map`` payload and the ``note_id`` that holds it.
        """
        auth = await get_vcr_auth()
        async with NotebookLMClient(auth) as client:
            result = await client.artifacts.generate_mind_map(
                MUTABLE_NOTEBOOK_ID,
                source_ids=[_WIKIPEDIA_SOURCE_ID],
            )

        # Final note is created with mind-map content.
        assert isinstance(result, dict)
        assert result.get("note_id"), "generate_mind_map must persist a note"
        assert isinstance(result["note_id"], str)
        # Mind-map JSON should be present and shaped like a tree
        # (either ``children`` or ``nodes`` key — both shapes are valid;
        # mirror the heuristic used by ``_mind_map.list_mind_maps``).
        assert result.get("mind_map") is not None
        mind_map = result["mind_map"]
        assert isinstance(mind_map, dict)
        assert "children" in mind_map or "nodes" in mind_map, (
            f"mind_map payload missing tree keys: {list(mind_map)[:5]}"
        )

    def test_cassette_records_three_rpc_chain(self) -> None:
        """The cassette captures all three sequential RPCs in order.

        This guards against two regression classes:

        1. **Chain shortening** — a refactor that drops UPDATE_NOTE (e.g.
           because the caller assumed CREATE_NOTE persists the title) would
           reduce the cassette to two RPCs. The rpcids order check catches
           it before the replay test silently masks the change.
        2. **Cassette drift** — if a future re-record accidentally pins the
           wrong source list (e.g. ``source_ids=None``), the cassette would
           sprout an extra GET_NOTEBOOK interaction. Asserting the exact
           ordered rpcids sequence rejects that shape too.

        The cassette is the source of truth here — we parse it directly
        rather than relying on the replay test's side effects so the
        assertion is independent of the client implementation.
        """
        assert CASSETTE_PATH.exists(), (
            f"cassette missing: {CASSETTE_PATH}. "
            "Re-record with NOTEBOOKLM_VCR_RECORD=1 — see module docstring."
        )

        with CASSETTE_PATH.open(encoding="utf-8") as fh:
            cassette = yaml.safe_load(fh)

        interactions = cassette.get("interactions", [])
        # Extract the rpcids query param from every batchexecute interaction
        # in the order they were recorded. Non-batchexecute interactions
        # (e.g. the homepage GET that bootstraps the CSRF token) are skipped.
        from urllib.parse import parse_qs, urlparse

        rpcids_sequence: list[str] = []
        for interaction in interactions:
            uri = interaction.get("request", {}).get("uri", "")
            if "/batchexecute" not in uri:
                continue
            qs = parse_qs(urlparse(uri).query)
            for rpc_id in qs.get("rpcids", []):
                rpcids_sequence.append(rpc_id)

        expected = [
            RPCMethod.GENERATE_MIND_MAP.value,  # yyryJe
            RPCMethod.CREATE_NOTE.value,  # CYK0Xb
            RPCMethod.UPDATE_NOTE.value,  # cYAfTb
        ]
        assert rpcids_sequence == expected, (
            f"Mind-map chain shape drift. Expected {expected}, got "
            f"{rpcids_sequence}. The chain MUST be exactly GENERATE_MIND_MAP "
            "→ CREATE_NOTE → UPDATE_NOTE; any other shape (extra RPC, "
            "missing UPDATE_NOTE, reordering) is a regression."
        )
