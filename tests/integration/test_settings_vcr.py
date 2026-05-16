"""VCR-backed integration tests for SettingsAPI.GET_USER_TIER.

These tests replay a recorded ``settings_get_user_tier.yaml`` cassette to
exercise the response parser at ``_settings.py:239`` against real-shape
batchexecute output without hitting the network.

Record with::

    NOTEBOOKLM_GENERATION_NOTEBOOK_ID=<uuid> NOTEBOOKLM_VCR_RECORD=1 \\
        uv run pytest tests/integration/test_settings_vcr.py -v

GET_USER_TIER is a homepage-scoped call (``source_path="/"``) so the notebook
ID is only relevant for ensuring the recording session is logged in.
"""

import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

# Add tests directory to path for vcr_config import (mirrors test_vcr_comprehensive.py).
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from conftest import get_vcr_auth, skip_no_cassettes  # noqa: E402
from notebooklm import NotebookLMClient  # noqa: E402
from vcr_config import notebooklm_vcr  # noqa: E402

pytestmark = [pytest.mark.vcr, skip_no_cassettes]


@asynccontextmanager
async def _vcr_client():
    """Context manager creating an authenticated VCR client (record or replay)."""
    auth = await get_vcr_auth()
    async with NotebookLMClient(auth) as client:
        yield client


@pytest.mark.vcr
@pytest.mark.asyncio
@notebooklm_vcr.use_cassette("settings_get_user_tier.yaml")
async def test_get_account_tier_replays_cassette() -> None:
    """Replay GET_USER_TIER and assert subscription-tier parsing.

    The recorded response should map cleanly through
    :func:`notebooklm._settings.extract_account_tier`. Even if Google flips
    this account between Standard/Pro/Plus/etc. on a later re-recording, the
    parser is expected to return a known tier identifier from the
    ``NOTEBOOKLM_TIER_*`` family and an accompanying friendly plan name.
    """
    async with _vcr_client() as client:
        tier = await client.settings.get_account_tier()

    # Parser contract: either a recognized tier+plan, or both None on shape
    # drift. We assert the recorded cassette returns a sensible (non-None)
    # value so the test exercises the happy-path parser branch.
    assert tier is not None
    assert tier.tier is not None, (
        "Recorded GET_USER_TIER cassette should yield a NOTEBOOKLM_TIER_* string"
    )
    assert tier.tier.startswith("NOTEBOOKLM_TIER_"), f"Unexpected tier string shape: {tier.tier!r}"
    # plan_name is None for unknown tiers (future-proofing) but should be a
    # string for any tier in the _TIER_PLAN_NAMES table.
    assert tier.plan_name is None or isinstance(tier.plan_name, str)
