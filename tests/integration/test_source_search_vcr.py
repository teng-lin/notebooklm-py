"""Recorded Web coverage for ``sources.search`` (``ASU5Oe``, #2283)."""

from __future__ import annotations

import os

import pytest

from notebooklm.types import RelevantChunk
from tests.integration.conftest import skip_no_cassettes
from tests.vcr_config import ResourceIdCassetteScrubber, notebooklm_vcr

from ._vcr_helpers import vcr_client

pytestmark = [pytest.mark.vcr, skip_no_cassettes]

READ_ONLY_NOTEBOOK_ID = os.environ.get(
    "NOTEBOOKLM_READ_ONLY_NOTEBOOK_ID", "00000000-0000-4000-8000-000000000001"
)
_RESOURCE_IDS = ResourceIdCassetteScrubber()


@notebooklm_vcr.use_cassette(
    "sources_search.yaml",
    before_record_request=_RESOURCE_IDS.scrub_request,
    before_record_response=_RESOURCE_IDS.scrub_response,
)
@pytest.mark.asyncio
async def test_source_search_ranks_limits_and_filters() -> None:
    async with vcr_client() as client:
        chunks = await client.sources.search(
            READ_ONLY_NOTEBOOK_ID,
            "Python functions",
            limit=2,
        )
        assert chunks
        source_id = chunks[0].source_id
        filtered = await client.sources.search(
            READ_ONLY_NOTEBOOK_ID,
            "Python functions",
            source_ids=[source_id],
        )

    assert all(isinstance(chunk, RelevantChunk) for chunk in chunks)
    assert [(chunk.source_id, chunk.rank, chunk.start, chunk.end) for chunk in chunks] == [
        ("00000000-0000-4000-8000-000000000005", 1, 0, 360),
        ("00000000-0000-4000-8000-000000000006", 2, 0, 342),
    ]
    assert chunks[0].text.startswith("### Python Programming Fundamentals\n")
    assert chunks[1].text.startswith("### Web Development Essentials\n")
    assert filtered == [chunks[0]]
