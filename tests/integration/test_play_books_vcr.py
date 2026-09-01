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
from tests.integration.conftest import get_vcr_auth, skip_no_cassettes
from tests.vcr_config import notebooklm_vcr

pytestmark = [pytest.mark.vcr, skip_no_cassettes]

CASSETTE_NAME = "sources_play_books.yaml"
CASSETTE_PATH = Path(__file__).parent.parent / "cassettes" / "web" / CASSETTE_NAME


class TestListPlayBooksCassette:
    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette(CASSETTE_NAME)
    async def test_list_play_books_decodes_library(self) -> None:
        auth = await get_vcr_auth()
        async with NotebookLMClient(auth) as client:
            books = await client.sources.list_play_books()

        assert books, "expected a non-empty Play Books library in the cassette"
        # Every row decodes to a content id + an export-eligibility verdict.
        for book in books:
            assert book.content_id
            assert isinstance(book.export_disabled, bool)
        # At least one exportable title (the ones add_play_book can ingest).
        assert any(not book.export_disabled for book in books)

    def test_cassette_records_the_list_rpc(self) -> None:
        """The cassette must contain the LIST_EXPERT_INTELLIGENCE_CONTENT RPC id."""
        body = CASSETTE_PATH.read_text(encoding="utf-8")
        assert RPCMethod.LIST_EXPERT_INTELLIGENCE_CONTENT.value in body
