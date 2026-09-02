"""Cassette-backed coverage for ``client.sources.list_play_books`` (#2292).

Exercises the ``LIST_EXPERT_INTELLIGENCE_CONTENT`` (``mVtEUb``) list path and
the ``ADD_SOURCES_ASYNC`` (``X1snv``) add path against recorded wire data. The
mutation recording creates and deletes a disposable notebook outside the VCR
boundary.

Recording
---------
Needs an account with a Google Play Books library (the ``teng-lin-9414`` profile
holds a five-title library)::

    NOTEBOOKLM_PROFILE=teng-lin-9414 NOTEBOOKLM_VCR_RECORD=1 \\
        uv run pytest tests/integration/test_play_books_vcr.py -v

After recording, re-run the repo's cassette sanitizer (cookies/tokens).
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from notebooklm import NotebookLMClient
from notebooklm.rpc.types import RPCMethod
from notebooklm.types import PlayBookExportReason, SourceType
from tests._helpers.play_book_http_cassette import PlayBookHttpCassetteScrubber
from tests.integration.conftest import _vcr_record_mode, get_vcr_auth, skip_no_cassettes
from tests.vcr_config import notebooklm_vcr

pytestmark = [pytest.mark.vcr, skip_no_cassettes]

CASSETTE_NAME = "sources_play_books.yaml"
CASSETTE_PATH = Path(__file__).parent.parent / "cassettes" / "web" / CASSETTE_NAME
ADD_CASSETTE_NAME = "sources_play_book_add.yaml"
ADD_CASSETTE_PATH = Path(__file__).parent.parent / "cassettes" / "web" / ADD_CASSETTE_NAME
_PLAY_BOOKS = PlayBookHttpCassetteScrubber()


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


class TestAddPlayBookCassette:
    @pytest.mark.asyncio
    async def test_add_play_book_records_expert_intelligence_add(self) -> None:
        """Record the book-specific ``mVtEUb`` -> ``X1snv`` public workflow."""

        auth = await get_vcr_auth()
        async with NotebookLMClient(auth) as client:
            scratch_id = "00000000-0000-4000-8000-000000000001"
            if _vcr_record_mode:
                scratch = await client.notebooks.create(
                    f"play-book-vcr-scratch-{uuid.uuid4().hex[:12]}"
                )
                scratch_id = scratch.id
            try:
                with notebooklm_vcr.use_cassette(
                    ADD_CASSETTE_NAME,
                    before_record_request=_PLAY_BOOKS.scrub_request,
                    before_record_response=_PLAY_BOOKS.scrub_response,
                ):
                    books = await client.sources.list_play_books()
                    exportable = next((book for book in books if not book.export_disabled), None)
                    assert exportable is not None, (
                        "recording account needs one exportable Play Book"
                    )
                    source = await client.sources.add_play_book(
                        scratch_id,
                        exportable.content_id,
                        wait=False,
                    )
            finally:
                if _vcr_record_mode:
                    await client.notebooks.delete(scratch_id)

        assert source.id
        assert source.kind is SourceType.EXPERT_INTELLIGENCE
        assert source.title

    @pytest.mark.skipif(
        _vcr_record_mode,
        reason="cassette assertions run after the record-mode test writes the file",
    )
    def test_add_cassette_records_list_and_add_rpcs(self) -> None:
        body = ADD_CASSETTE_PATH.read_text(encoding="utf-8")
        assert RPCMethod.LIST_EXPERT_INTELLIGENCE_CONTENT.value in body
        assert RPCMethod.ADD_SOURCES_ASYNC.value in body
