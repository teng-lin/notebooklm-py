"""VCR tests for SHARE_ARTIFACT.

Records / replays the ``client.notebooks.share()`` call which drives the
``RGP97b`` SHARE_ARTIFACT RPC. ``share()`` is a NOTEBOOK-level toggle
(public on / off) — it does NOT take a recipient email, so there is no
human user identity to worry about leaking. The only sensitive material
that could appear is auth/cookie material, which is handled by the
standard scrubbers configured in ``tests/vcr_config.py``.

Recording:

    export NOTEBOOKLM_GENERATION_NOTEBOOK_ID=<uuid>
    export NOTEBOOKLM_VCR_RECORD=1
    uv run pytest tests/integration/test_sharing_vcr.py -v

Replay (default):

    uv run pytest tests/integration/test_sharing_vcr.py -v

The test is automatically skipped when no cassettes are available
(see ``tests/integration/conftest.py``).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Match the import shim used by ``test_vcr_comprehensive.py``: VCR config
# and the local conftest helper both live above this file's package root.
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from conftest import get_vcr_auth, skip_no_cassettes  # noqa: E402
from notebooklm import NotebookLMClient  # noqa: E402
from vcr_config import notebooklm_vcr  # noqa: E402

pytestmark = [pytest.mark.vcr, skip_no_cassettes]

# Generation notebook is the one we may mutate.  ``share()`` is reversible
# (we flip it back off after enabling) so this notebook is safe to use.
# Default mirrors ``test_vcr_comprehensive.py`` for cassette-replay parity:
# the matcher ignores the body for SHARE_ARTIFACT (no ``f.req`` envelope),
# but keeping a known UUID here makes the recorded URL deterministic and
# the test honest about what notebook the cassette describes.
MUTABLE_NOTEBOOK_ID = os.environ.get(
    "NOTEBOOKLM_GENERATION_NOTEBOOK_ID",
    "bb00c9e3-656c-4fd2-b890-2b71e1cf3814",
)


class TestSharingVCR:
    """Records the SHARE_ARTIFACT RPC against the real API."""

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("notebooks_share.yaml")
    async def test_share_notebook_public(self):
        """Toggle a notebook to public via ``notebooks.share()``.

        ``share()`` returns a dict ``{"public": bool, "url": str|None,
        "artifact_id": str|None}``. The cassette records exactly one
        SHARE_ARTIFACT RPC, and we assert the client surfaces the expected
        shape on success.
        """
        auth = await get_vcr_auth()
        async with NotebookLMClient(auth) as client:
            result = await client.notebooks.share(MUTABLE_NOTEBOOK_ID, public=True)

        assert isinstance(result, dict)
        assert result["public"] is True
        assert result["url"] is not None
        assert MUTABLE_NOTEBOOK_ID in result["url"]
        # ``share(public=True)`` without an artifact_id returns the bare
        # notebook URL — no ``?artifactId=`` suffix.
        assert "artifactId=" not in result["url"]
        assert result["artifact_id"] is None
