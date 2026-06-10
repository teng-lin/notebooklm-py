"""Golden decoded-row assertions for high-blast-radius RPC cassettes (issue #1494).

Why this file exists
--------------------
VCR cassette replay matches on the ``rpcids`` query param plus a *structural*
``f.req`` body matcher (``tests/vcr_config.py`` — ``_rpcids_matcher`` /
``_freq_body_matcher``). That matcher deliberately compares request **shape**,
**not** leaf values (see the ``_shape_only`` docstring near
``tests/vcr_config.py:420``: it compares structure, "not 'same leaf values'").
Those tolerant matchers earn the suite cross-platform stability — a cassette
recorded against one notebook replays for a different one — and we keep them.

But that tolerance has one blind spot: a green cassette does **not** prove the
*decoder* mapped the recorded bytes onto the right dataclass fields. A
positional mis-map (the exact failure class the ``_row_adapters`` exist to
prevent) or a leaf-shape drift can still replay green because the matcher never
looks at the recorded *response* leaves — only the request shape. The rest of
the VCR suite (``test_vcr_comprehensive.py``) mostly asserts
``isinstance(result, list)`` / ``result.answer is not None``, which a scrambled
decode also satisfies.

What this file adds
-------------------
For the highest-blast-radius read RPCs — **chat** (ask / answer + references),
**artifacts list** (``gArtLc``), **sources list/get** (``rLM1Ne`` /
``CmAJ2c`` guide / ``rcSrr`` fulltext) — we pin the **decoded dataclass field
values** for a representative recorded response. If the decoder ever mis-maps a
column (e.g. reads the artifact *status* column as the *type* column, or
scrambles a chat reference's ``start_char`` / ``citation_number`` ordering), the
golden values below diverge and the test fails **loudly** instead of replaying
green.

These assertions are intentionally **cassette-coupled**: the golden values come
out of the recorded *response* and are expected to change if the cassette is
re-recorded against a different notebook. That is the opposite contract from the
``cli_vcr`` suite (whose ``tests/_guardrails/test_no_pinned_cassette_values.py``
gate forbids pinning recorded ids precisely so a re-record stays green). The two
contracts do not conflict: that gate scopes only to ``tests/integration/cli_vcr/``,
and this file lives outside it on purpose — here, a re-record SHOULD force a
golden refresh, because the whole point is to detect decode drift against a
known-good recording. No cassette is re-recorded by this file
(``NOTEBOOKLM_VCR_RECORD`` stays off); it only adds read-only assertions on the
decoded objects produced from the existing cassettes.
"""

from __future__ import annotations

import os
import reprlib

import pytest
from tests.integration._golden_assert import assert_decoded_equals
from tests.integration._vcr_helpers import vcr_client
from tests.integration.conftest import skip_no_cassettes
from tests.vcr_config import notebooklm_vcr

from notebooklm.types import Artifact, ArtifactType, Source, SourceType

# Skip all tests in this module if cassettes are not available.
pytestmark = [pytest.mark.vcr, skip_no_cassettes]

# Same recording notebook ids as ``test_vcr_comprehensive.py`` — they matter only
# when RECORDING (replay serves the recorded response regardless of id), except
# for the body-aware streaming-chat ``freq`` matcher, which compares slot 7 of
# the decoded ``f.req`` envelope, so the chat tests below send the canonical
# recording notebook UUID.
READONLY_NOTEBOOK_ID = os.environ.get(
    "NOTEBOOKLM_READ_ONLY_NOTEBOOK_ID",
    "c3f6285f-1709-44c4-9cd6-e95cf0ea4f5e",
)
MUTABLE_NOTEBOOK_ID = os.environ.get(
    "NOTEBOOKLM_GENERATION_NOTEBOOK_ID",
    "bb00c9e3-656c-4fd2-b890-2b71e1cf3814",
)

# The streaming-chat endpoint is the one place we opt into the body-aware
# ``freq`` matcher (most endpoints don't send ``f.req``). Same tuple
# ``test_vcr_comprehensive.py::TestChatAPI`` uses.
_CHAT_MATCH_ON = ["method", "scheme", "host", "port", "path", "freq"]


# ``assert_decoded_equals`` and ``vcr_client`` live in
# ``tests/integration/_golden_assert.py`` / ``_vcr_helpers.py`` so the
# expansion module (``test_golden_decoded_vcr_expansion.py``) can share them
# without a cross-test-module import.


# =============================================================================
# Chat: ask / answer + references (rLM1Ne history seed + hPTbtc stream)
# =============================================================================


class TestChatGoldenDecoded:
    """Pin the decoded ``AskResult`` / ``ChatReference`` fields for chat cassettes.

    The streamed-chat decoder is the most positional-mis-decode-prone path in
    the client: it pulls the answer text, the conversation id, and a list of
    references each carrying a source id, a client-assigned citation number, and
    paired source-side character offsets out of a deeply-nested positional
    payload. A column slip would scramble any of these while the cassette still
    replays green under the shape-only matcher.
    """

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("chat_ask.yaml", match_on=_CHAT_MATCH_ON)
    async def test_ask_decoded_golden(self):
        """``chat.ask`` decodes the recorded answer / conversation / references."""
        async with vcr_client() as client:
            result = await client.chat.ask(
                MUTABLE_NOTEBOOK_ID,
                "What is this notebook about?",
            )

        # Answer text: pin the length + leading prose. The head is a stable,
        # non-scrubbed slice; pinning it catches the decoder reading the wrong
        # text slot (e.g. echoing the question, or grabbing an adjacent field).
        assert_decoded_equals(len(result.answer), 1871, field="chat_ask.answer length")
        assert result.answer.startswith(
            "This notebook is about **NotebookLM**, an online research"
        ), f"Unexpected answer head: {reprlib.repr(result.answer)}"

        # Conversation id + turn metadata.
        assert_decoded_equals(
            result.conversation_id,
            "bc0666c8-34b5-4bf8-817f-554867ea6ee8",
            field="chat_ask.conversation_id",
        )
        assert_decoded_equals(result.turn_number, 1, field="chat_ask.turn_number")
        assert_decoded_equals(result.is_follow_up, False, field="chat_ask.is_follow_up")

        # References: pin (citation_number, source_id, start_char, end_char) for
        # every reference, in order. This is the positional-decode canary — a
        # column slip in the reference parser would scramble the offset pairing
        # or the citation ordering while the cassette still replays.
        expected_refs = [
            (1, "466b9ee3-c1ce-45ef-861c-1d4bfcd939ad", 1459, 1610),
            (2, "466b9ee3-c1ce-45ef-861c-1d4bfcd939ad", 2089, 2396),
            (3, "466b9ee3-c1ce-45ef-861c-1d4bfcd939ad", 3905, 4071),
            (4, "466b9ee3-c1ce-45ef-861c-1d4bfcd939ad", 3378, 3679),
            (5, "466b9ee3-c1ce-45ef-861c-1d4bfcd939ad", 4595, 5000),
            (6, "466b9ee3-c1ce-45ef-861c-1d4bfcd939ad", 5247, 5725),
            (7, "466b9ee3-c1ce-45ef-861c-1d4bfcd939ad", 2744, 2751),
        ]
        actual_refs = [
            (r.citation_number, r.source_id, r.start_char, r.end_char) for r in result.references
        ]
        assert_decoded_equals(actual_refs, expected_refs, field="chat_ask.references")
        # Paired-offset invariant: start <= end for every reference (the
        # dataclass __post_init__ enforces this, so a violation would have
        # raised — pinning it documents the contract the golden values satisfy).
        for ref in result.references:
            assert ref.start_char is not None and ref.end_char is not None
            assert ref.start_char <= ref.end_char

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("chat_ask_with_references.yaml", match_on=_CHAT_MATCH_ON)
    async def test_ask_with_references_decoded_golden(self):
        """``chat.ask`` decodes per-reference offsets AND server relevance scores.

        This cassette additionally exercises the ``score`` slot (server-side
        relevance, ~0.6-0.7), which the plain ``chat_ask`` answer omits.
        """
        async with vcr_client() as client:
            result = await client.chat.ask(
                MUTABLE_NOTEBOOK_ID,
                "Summarize the key points with specific citations from the sources.",
            )

        assert_decoded_equals(
            len(result.answer), 2050, field="chat_ask_with_references.answer length"
        )
        assert result.answer.startswith(
            "**NotebookLM** is an online research and note-taking tool"
        ), f"Unexpected answer head: {reprlib.repr(result.answer)}"
        assert_decoded_equals(
            result.conversation_id,
            "bc0666c8-34b5-4bf8-817f-554867ea6ee8",
            field="chat_ask_with_references.conversation_id",
        )

        # (citation_number, source_id, start_char, end_char) — in answer order.
        expected_refs = [
            (1, "466b9ee3-c1ce-45ef-861c-1d4bfcd939ad", 1459, 1610),
            (2, "466b9ee3-c1ce-45ef-861c-1d4bfcd939ad", 2089, 2396),
            (3, "466b9ee3-c1ce-45ef-861c-1d4bfcd939ad", 2744, 2751),
            (4, "466b9ee3-c1ce-45ef-861c-1d4bfcd939ad", 3378, 3679),
            (5, "466b9ee3-c1ce-45ef-861c-1d4bfcd939ad", 4595, 5000),
            (6, "466b9ee3-c1ce-45ef-861c-1d4bfcd939ad", 3905, 4071),
            (7, "466b9ee3-c1ce-45ef-861c-1d4bfcd939ad", 5247, 5725),
            (8, "466b9ee3-c1ce-45ef-861c-1d4bfcd939ad", 9894, 9989),
            (9, "466b9ee3-c1ce-45ef-861c-1d4bfcd939ad", 5967, 5983),
        ]
        actual_refs = [
            (r.citation_number, r.source_id, r.start_char, r.end_char) for r in result.references
        ]
        assert_decoded_equals(
            actual_refs, expected_refs, field="chat_ask_with_references.references"
        )

        # Server relevance scores decode as floats in the documented 0.6-0.7
        # band. Pin them rounded so a column slip onto a non-score slot (which
        # would land outside [0, 1] or come back None) fails here.
        expected_scores = [0.6999, 0.6942, 0.6639, 0.7083, 0.6506, 0.6784, 0.6812, 0.718, 0.6897]
        actual_scores = [
            round(r.score, 4) if r.score is not None else None for r in result.references
        ]
        assert_decoded_equals(
            actual_scores, expected_scores, field="chat_ask_with_references.reference scores"
        )
        for ref in result.references:
            assert ref.score is not None
            assert 0.0 <= ref.score <= 1.0


# =============================================================================
# Artifacts list/get (gArtLc)
# =============================================================================


# Per-row golden: (id, _artifact_type code, ArtifactType kind, status).
# Pinning the (id <-> type_code <-> kind) triple per row is the positional
# canary for the artifact-list decoder: a column slip would flip the kind on a
# given id while the list length and element types still satisfy the existing
# ``isinstance(art, Artifact)`` checks in ``test_vcr_comprehensive.py``.
_ARTIFACTS_LIST_GOLDEN = [
    ("575a9e5d-40fb-44a4-b2d3-21a573bdb547", 1, ArtifactType.AUDIO, 3),
    ("0b9bb718-2749-4d3e-8448-42c94019c6a5", 7, ArtifactType.INFOGRAPHIC, 3),
    ("835a5938-4a60-43a0-b2c3-6f01b435664a", 4, ArtifactType.QUIZ, 3),
    ("fdd20d4a-f422-42b3-896c-60997035f4ca", 2, ArtifactType.REPORT, 3),
    ("173255d8-12b3-4c67-b925-a76ce6c71735", 4, ArtifactType.FLASHCARDS, 3),
    ("7e91cd58-3aff-422d-aebc-d3d6b9f551e6", 9, ArtifactType.DATA_TABLE, 3),
    ("0ad1abb9-f37c-4094-83e9-52c816d69132", 3, ArtifactType.VIDEO, 3),
    ("c26f1079-c5cd-4094-87b7-59fc73d68bd3", 3, ArtifactType.VIDEO, 3),
    ("848df2ec-4916-4dea-aa20-3dc02954cfd0", 8, ArtifactType.SLIDE_DECK, 3),
    ("9ca94236-8568-4b24-bde1-fa7bc48db6de", 1, ArtifactType.AUDIO, 3),
    ("608891fc-71f7-421f-86a6-dcc2a3125633", 5, ArtifactType.MIND_MAP, 3),
]


class TestArtifactsListGoldenDecoded:
    """Pin the decoded ``Artifact`` rows for the unified ``artifacts.list`` cassette."""

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("artifacts_list.yaml")
    async def test_list_decoded_golden(self):
        """Every recorded artifact row decodes to the exact id/type/kind/status."""
        async with vcr_client() as client:
            artifacts = await client.artifacts.list(READONLY_NOTEBOOK_ID)

        assert_decoded_equals(
            len(artifacts), len(_ARTIFACTS_LIST_GOLDEN), field="artifacts_list length"
        )
        for idx, (art, (exp_id, exp_type, exp_kind, exp_status)) in enumerate(
            zip(artifacts, _ARTIFACTS_LIST_GOLDEN, strict=True)
        ):
            assert isinstance(art, Artifact)
            assert_decoded_equals(art.id, exp_id, field=f"artifacts_list[{idx}].id")
            assert_decoded_equals(
                art._artifact_type, exp_type, field=f"artifacts_list[{idx}]._artifact_type"
            )
            assert_decoded_equals(art.kind, exp_kind, field=f"artifacts_list[{idx}].kind")
            # str-enum equality holds both directions.
            assert_decoded_equals(
                art.kind, exp_kind.value, field=f"artifacts_list[{idx}].kind (str)"
            )
            assert_decoded_equals(art.status, exp_status, field=f"artifacts_list[{idx}].status")

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("artifacts_list_reports.yaml")
    async def test_list_reports_decoded_golden(self):
        """The type-filtered ``list_reports`` cassette decodes its one REPORT row."""
        async with vcr_client() as client:
            reports = await client.artifacts.list_reports(READONLY_NOTEBOOK_ID)

        assert_decoded_equals(len(reports), 1, field="artifacts_list_reports length")
        art = reports[0]
        assert isinstance(art, Artifact)
        assert_decoded_equals(
            art.id, "fdd20d4a-f422-42b3-896c-60997035f4ca", field="artifacts_list_reports[0].id"
        )
        assert_decoded_equals(
            art.title,
            'Study Guide for "Learn Claude Code"',
            field="artifacts_list_reports[0].title",
        )
        assert_decoded_equals(
            art._artifact_type, 2, field="artifacts_list_reports[0]._artifact_type"
        )
        assert_decoded_equals(art.kind, ArtifactType.REPORT, field="artifacts_list_reports[0].kind")
        assert_decoded_equals(art.status, 3, field="artifacts_list_reports[0].status")


# =============================================================================
# Sources list/get (rLM1Ne list, CmAJ2c guide, rcSrr fulltext)
# =============================================================================


# Per-row golden: (id, type_code, SourceType kind, status). Titles/urls are
# free-form and partly scrubbed in some cassettes, so the structural decode
# (id <-> type_code <-> kind <-> status) is the re-decode-sensitive part we pin.
_SOURCES_LIST_GOLDEN = [
    ("a474cd35-6c21-4e72-94a0-c38b5491b449", 5, SourceType.WEB_PAGE, 2),
    ("735fcfef-9fbd-4c89-9789-6a9760587bec", 4, SourceType.PASTED_TEXT, 2),
    ("4d3f7b07-e9e6-43d3-ab8b-184fa27a9f1e", 4, SourceType.PASTED_TEXT, 2),
    ("a5ec927b-12eb-45d1-989f-12eb3db4ce53", 4, SourceType.PASTED_TEXT, 2),
    ("c361a555-5c2d-42e2-94d0-a65da95be660", 4, SourceType.PASTED_TEXT, 2),
    ("48f71a82-08d3-46fa-a37f-d657fb2f0723", 4, SourceType.PASTED_TEXT, 2),
    ("d6ce2ec3-f98a-4529-acd5-08bff271cb3b", 4, SourceType.PASTED_TEXT, 2),
    ("ef358221-3904-4dbc-be6f-e1e8dea63954", 4, SourceType.PASTED_TEXT, 2),
]


class TestSourcesGoldenDecoded:
    """Pin the decoded ``Source`` rows + guide/fulltext fields for source cassettes."""

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("sources_list.yaml")
    async def test_list_decoded_golden(self):
        """Every recorded source row decodes to the exact id/type/kind/status."""
        async with vcr_client() as client:
            sources = await client.sources.list(READONLY_NOTEBOOK_ID)

        assert_decoded_equals(len(sources), len(_SOURCES_LIST_GOLDEN), field="sources_list length")
        for idx, (src, (exp_id, exp_type, exp_kind, exp_status)) in enumerate(
            zip(sources, _SOURCES_LIST_GOLDEN, strict=True)
        ):
            assert isinstance(src, Source)
            assert_decoded_equals(src.id, exp_id, field=f"sources_list[{idx}].id")
            assert_decoded_equals(src._type_code, exp_type, field=f"sources_list[{idx}]._type_code")
            assert_decoded_equals(src.kind, exp_kind, field=f"sources_list[{idx}].kind")
            assert_decoded_equals(int(src.status), exp_status, field=f"sources_list[{idx}].status")
        # The first source is the WEB_PAGE row — pin its decoded URL too (the
        # url slot lives at a different metadata position than the title, so a
        # slip there is invisible to id/type pinning).
        assert_decoded_equals(
            sources[0].url,
            "https://github.com/shareAI-lab/learn-claude-code",
            field="sources_list[0].url",
        )

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("sources_get_guide.yaml")
    async def test_get_guide_decoded_golden(self):
        """``sources.get_guide`` decodes the AI summary + keyword tuple."""
        async with vcr_client() as client:
            sources = await client.sources.list(READONLY_NOTEBOOK_ID)
            guide = await client.sources.get_guide(READONLY_NOTEBOOK_ID, sources[0].id)

        assert_decoded_equals(
            sources[0].id,
            "a474cd35-6c21-4e72-94a0-c38b5491b449",
            field="sources_get_guide.first source id",
        )
        assert guide.summary.startswith(
            "This educational repository serves as a progressive tutorial"
        ), f"Unexpected guide summary head: {reprlib.repr(guide.summary)}"
        assert isinstance(guide.keywords, tuple)
        assert_decoded_equals(len(guide.keywords), 5, field="sources_get_guide.keyword count")
        # First four keywords are non-scrubbed and positionally stable; the
        # fifth is scrubbed (a person name) so we only pin the leading four.
        assert_decoded_equals(
            guide.keywords[:4],
            (
                "AI Coding Agents",
                "Agent Development Tutorials",
                "Claude Code",
                "Tool Use Loop",
            ),
            field="sources_get_guide.keywords[:4]",
        )

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("sources_get_fulltext.yaml")
    async def test_get_fulltext_decoded_golden(self):
        """``sources.get_fulltext`` decodes source_id / char_count / content length."""
        async with vcr_client() as client:
            sources = await client.sources.list(READONLY_NOTEBOOK_ID)
            fulltext = await client.sources.get_fulltext(READONLY_NOTEBOOK_ID, sources[0].id)

        assert_decoded_equals(
            fulltext.source_id,
            "a474cd35-6c21-4e72-94a0-c38b5491b449",
            field="sources_get_fulltext.source_id",
        )
        assert fulltext.title.startswith("GitHub - shareAI-lab/learn-claude-code"), (
            f"Unexpected fulltext title head: {reprlib.repr(fulltext.title)}"
        )
        # char_count is a decoded integer that must equal the content length —
        # a mis-decode of the count slot (vs the content slot) breaks this.
        assert_decoded_equals(fulltext.char_count, 11819, field="sources_get_fulltext.char_count")
        assert_decoded_equals(
            len(fulltext.content), 11819, field="sources_get_fulltext.content length"
        )
        assert_decoded_equals(
            fulltext.char_count,
            len(fulltext.content),
            field="sources_get_fulltext.char_count == len(content)",
        )
