"""Cassette-backed coverage for ``client.sources.list_play_books`` (#2292).

Exercises the ``LIST_EXPERT_INTELLIGENCE_CONTENT`` (``mVtEUb``) list path against
real recorded wire data — a read-only call, so the cassette is minimal and
mutates nothing.

Recording
---------
Needs an account with a Google Play Books library (the ``teng-lin-9414`` profile
holds a five-title library)::

    NOTEBOOKLM_PROFILE=teng-lin-9414 NOTEBOOKLM_VCR_RECORD=1 \\
        uv run pytest tests/integration/test_play_books_vcr.py -v

After recording, re-run the repo's cassette sanitizer (cookies/tokens).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from notebooklm import NotebookLMClient
from notebooklm.rpc.types import RPCMethod
from notebooklm.types import PlayBookExportReason
from tests.integration.conftest import get_vcr_auth, skip_no_cassettes
from tests.vcr_config import notebooklm_vcr

pytestmark = [pytest.mark.vcr, skip_no_cassettes]

CASSETTE_NAME = "sources_play_books.yaml"
CASSETTE_PATH = Path(__file__).parent.parent / "cassettes" / "web" / CASSETTE_NAME


class TestListPlayBooksCassette:
    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("sources_play_books.yaml")
    async def test_list_play_books_decodes_library(self) -> None:
        auth = await get_vcr_auth()
        async with NotebookLMClient(auth) as client:
            books = await client.sources.list_play_books()

        # Pin the exact decoded rows so a wrong field position or a flipped
        # export verdict is caught, not just "some non-empty list". The recorded
        # library is scrubbed to synthetic placeholders (two exportable, one
        # blocked); the third row's reason (code 1) decodes to OPTED_OUT.
        assert [(b.content_id, b.export_disabled) for b in books] == [
            ("EIBOOK00000001", False),
            ("EIBOOK00000002", False),
            ("EIBOOK00000003", True),
        ]
        assert books[0].authors == ("author 001",)
        assert books[0].field_type == pytest.approx(4.5)
        assert books[2].reason is PlayBookExportReason.OPTED_OUT

    def test_cassette_records_the_list_rpc(self) -> None:
        """The cassette must contain the LIST_EXPERT_INTELLIGENCE_CONTENT RPC id."""
        body = CASSETTE_PATH.read_text(encoding="utf-8")
        assert RPCMethod.LIST_EXPERT_INTELLIGENCE_CONTENT.value in body
