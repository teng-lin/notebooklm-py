"""VCR-backed integration test for SourcesAPI.discover (DISCOVER_SOURCES).

Replays a recorded ``discover_sources.yaml`` cassette to exercise the
synchronous ``DiscoverSources`` (``Es3dTe``) RPC end-to-end through the public
``client.sources.discover`` path — request encoding, decode, and the
``DiscoveredSource`` parser — without hitting the network.

Record with::

    NOTEBOOKLM_GENERATION_NOTEBOOK_ID=<uuid> NOTEBOOKLM_VCR_RECORD=1 \\
        uv run pytest tests/integration/test_discover_sources_vcr.py -v

``DiscoverSources`` is read-only: it returns candidate web sources for a topic
without adding them to the notebook, so the generation notebook stays pristine.
"""

import os
from contextlib import asynccontextmanager

import pytest

from notebooklm import DiscoveredSource, NotebookLMClient
from tests.integration.conftest import get_vcr_auth, skip_no_cassettes
from tests.vcr_config import notebooklm_vcr

pytestmark = [pytest.mark.vcr, skip_no_cassettes]


@asynccontextmanager
async def _vcr_client():
    """Context manager creating an authenticated VCR client (record or replay)."""
    auth = await get_vcr_auth()
    async with NotebookLMClient(auth) as client:
        yield client


@pytest.mark.vcr
@pytest.mark.asyncio
@notebooklm_vcr.use_cassette("discover_sources.yaml")
async def test_discover_sources_replays_cassette() -> None:
    """Replay DISCOVER_SOURCES and assert candidate-source parsing."""
    notebook_id = os.environ.get(
        "NOTEBOOKLM_GENERATION_NOTEBOOK_ID",
        # Replay-only placeholder; matched against the recorded request body.
        "bb00c9e3-656c-4fd2-b890-2b71e1cf3814",
    )
    async with _vcr_client() as client:
        candidates = await client.sources.discover(notebook_id, "quantum computing advances")

    assert isinstance(candidates, list)
    assert candidates, "Recorded discover cassette should yield candidate sources"
    for candidate in candidates:
        assert isinstance(candidate, DiscoveredSource)
        assert candidate.url.startswith("http")
        assert candidate.title
        assert isinstance(candidate.source_type, int)
