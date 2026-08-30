"""Live-recorded regression for notebook copying through ``CopyProject``."""

from __future__ import annotations

import os

import pytest

from tests.integration.conftest import skip_no_cassettes
from tests.vcr_config import ResourceIdCassetteScrubber, notebooklm_vcr

from ._vcr_helpers import vcr_client

pytestmark = [pytest.mark.vcr, skip_no_cassettes]

# The fallback is the deterministic placeholder in the scrubbed recording.
# Contributors can point live recording at any readable notebook; CopyProject
# does not mutate the source and the cassette hook removes its real UUID.
SOURCE_NOTEBOOK_ID = os.environ.get(
    "NOTEBOOKLM_READ_ONLY_NOTEBOOK_ID",
    "00000000-0000-4000-8000-000000000005",
)
_RESOURCE_IDS = ResourceIdCassetteScrubber()


@notebooklm_vcr.use_cassette(
    "notebooks_copy.yaml",
    before_record_request=_RESOURCE_IDS.scrub_request,
    before_record_response=_RESOURCE_IDS.scrub_response,
)
@pytest.mark.asyncio
async def test_live_copy_notebook_returns_distinct_project_and_cleans_up() -> None:
    """Copy an existing notebook, validate the result, and delete the scratch copy.

    Record with::

        NOTEBOOKLM_READ_ONLY_NOTEBOOK_ID=<source-id> NOTEBOOKLM_VCR_RECORD=1 \
          uv run pytest tests/integration/test_notebook_copy_vcr.py -v

    The source notebook is left untouched. The copied notebook exists only for
    the duration of the test, including during live cassette recording.
    """
    title = "VCR CopyProject Regression"
    async with vcr_client() as client:
        copied = await client.notebooks.copy(SOURCE_NOTEBOOK_ID, title)
        try:
            assert copied.id
            assert copied.id != SOURCE_NOTEBOOK_ID
            assert copied.title == title
        finally:
            await client.notebooks.delete(copied.id)
