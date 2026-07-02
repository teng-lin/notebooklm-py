"""Golden decoded-row assertions for the remaining replayable RPC cassette families.

Why this file exists
--------------------
``test_golden_decoded_vcr.py`` (issue #1494) pins decoded dataclass fields for
the four highest-blast-radius read RPCs (chat, artifacts list, sources
list/get), closing the VCR shape-only-matcher blind spot *for those families*:
the tolerant body matcher in ``tests/vcr_config.py`` compares request **shape**,
never response leaves, so a positional mis-decode replays green unless a golden
assertion pins the decoded values. The 2026-06-09 architecture review's top
testing recommendation was to extend that pinning to **every** recorded
response family.

This module is that extension: for every cassette-backed RPC family with a
typed decode path that was not already golden-covered — notebooks, source
mutations, notes, chat history, labels, sharing, research, settings, artifact
generation/export/revision, and both mind-map paths — it pins a small number of
identity/semantic fields (ids, titles, enum codes, counts, status strings) of
the DECODED result. ``tests/_guardrails/test_golden_decode_coverage.py`` is the
standing gate that keeps the corpus classified: every rpcid found in
``tests/cassettes/`` must be golden-covered or carry a reasoned exemption.

Contract
--------
Same cassette-coupled contract as ``test_golden_decoded_vcr.py`` (see its
docstring for the full rationale): golden values come out of the recorded
*responses* and are expected to change if a cassette is re-recorded — a
re-record SHOULD force a golden refresh here, because the whole point is to
detect decode drift against a known-good recording. This is the opposite
contract from ``tests/integration/cli_vcr/`` (whose
``test_no_pinned_cassette_values.py`` gate forbids pinned recorded ids; that
gate scopes only to ``cli_vcr/``, and this file lives outside it on purpose).
No cassette is recorded or modified by this file; it only adds read-only
assertions on the decoded objects produced from the existing cassettes.

Field-selection rules
---------------------
* Pin identity/semantic fields: ids, titles, enum/type codes, counts, language
  codes, status strings, tree node names.
* Values scrubbed by the cassette sanitizer (``SCRUBBED_NAME`` /
  ``SCRUBBED_EMAIL@...``) are NOT pinned — they prove nothing about the decoder
  and would couple the golden to the scrubber instead.
* All assertions go through the public typed decode path (client namespaces /
  row adapters) — never raw positional indexing in the test.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from notebooklm import ReportFormat
from notebooklm.types import (
    MindMapKind,
    ResearchStatus,
    SharePermission,
    SourceStatus,
)
from tests.integration._golden_assert import assert_decoded_equals
from tests.integration._vcr_helpers import vcr_client
from tests.integration.conftest import skip_no_cassettes
from tests.vcr_config import notebooklm_vcr

# Skip all tests in this module if cassettes are not available.
pytestmark = [pytest.mark.vcr, skip_no_cassettes]

# Same recording notebook ids as ``test_vcr_comprehensive.py`` /
# ``test_golden_decoded_vcr.py`` — they matter only when RECORDING (replay
# serves the recorded response regardless of id).
READONLY_NOTEBOOK_ID = os.environ.get(
    "NOTEBOOKLM_READ_ONLY_NOTEBOOK_ID",
    "c3f6285f-1709-44c4-9cd6-e95cf0ea4f5e",
)
MUTABLE_NOTEBOOK_ID = os.environ.get(
    "NOTEBOOKLM_GENERATION_NOTEBOOK_ID",
    "bb00c9e3-656c-4fd2-b890-2b71e1cf3814",
)
# The notebook the interactive mind-map cassette was recorded against
# (``test_mind_maps_vcr.py`` uses the same default).
MINDMAP_NOTEBOOK_ID = os.environ.get(
    "NOTEBOOKLM_MINDMAP_NOTEBOOK_ID",
    "f7d1e2b6-2334-4016-b81d-aded7b3fa9b6",
)
# Source ID for the Wikipedia "NotebookLM" page attached to the generation
# notebook — passing it explicitly keeps ``generate_mind_map`` to the three
# RPCs recorded in ``generate_mind_map_chain.yaml`` (same constant as
# ``test_mind_map_chain_vcr.py``).
_WIKIPEDIA_SOURCE_ID = "466b9ee3-c1ce-45ef-861c-1d4bfcd939ad"


# =============================================================================
# Notebooks (wXbhsf list, rLM1Ne get, VfAZjd summarize, CCqFvf create)
# =============================================================================


# Per-row golden for the first three recorded notebooks:
# (id, title, sources_count, is_owner). The (id <-> title <-> count <-> owner)
# tuple is the positional canary for the notebook-list decoder.
_NOTEBOOKS_LIST_GOLDEN_HEAD = [
    (
        "f66923f0-1df4-4ffe-9822-3ed63c558b1c",
        "GENERATION: Claude Code Deep Dive: Skills, Agents, Commands & Plugins",
        42,
        True,
    ),
    (
        "167481cd-23a3-4331-9a45-c8948900bf91",
        "READ ONLY: Learn Claude Code & AI Agents for High School Students",
        8,
        False,
    ),
    (
        "c3f6285f-1709-44c4-9cd6-e95cf0ea4f5e",
        "TypeScript Fundamentals: A Handbook of Type Systems and Rules",
        2,
        True,
    ),
]


class TestNotebooksGoldenDecoded:
    """Pin decoded ``Notebook`` / summary / description fields."""

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("notebooks_list.yaml")
    async def test_list_decoded_golden(self):
        """``notebooks.list`` decodes id/title/sources_count/is_owner per row."""
        async with vcr_client() as client:
            notebooks = await client.notebooks.list()

        assert_decoded_equals(len(notebooks), 12, field="notebooks_list length")
        actual_head = [(n.id, n.title, n.sources_count, n.is_owner) for n in notebooks[:3]]
        assert_decoded_equals(
            actual_head, _NOTEBOOKS_LIST_GOLDEN_HEAD, field="notebooks_list[:3] rows"
        )
        # The created_at / modified_at slots decode to real timestamps (not
        # fabricated defaults) — pin the first row's to catch a timestamp-column
        # slip. ``created_at`` is the CREATION instant (``data[5][8][0]``) and
        # ``modified_at`` is the LAST-MODIFIED instant (``data[5][5][0]``); the
        # two were historically swapped (created_at exposed the modified time).
        # The decoder renders tz-aware UTC (``fromtimestamp(.., tz=utc)``,
        # #1519), so the round-tripped epoch is identical on every timezone/CI
        # host. We pin the epoch (not a wall-time string) and assert tz-awareness
        # so a regression back to naive host-local time fails.
        first = notebooks[0]
        assert first.created_at is not None
        assert first.created_at.tzinfo is not None
        assert_decoded_equals(
            int(first.created_at.timestamp()),
            1768174413,  # data[5][8][0] — true creation instant
            field="notebooks_list[0].created_at (epoch seconds)",
        )
        assert first.modified_at is not None
        assert first.modified_at.tzinfo is not None
        assert_decoded_equals(
            int(first.modified_at.timestamp()),
            1768311605,  # data[5][5][0] — last-modified instant
            field="notebooks_list[0].modified_at (epoch seconds)",
        )
        # Creation precedes last modification in the recorded row.
        assert first.created_at < first.modified_at

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("notebooks_get.yaml")
    async def test_get_decoded_golden(self):
        """``notebooks.get`` decodes the recorded notebook row."""
        async with vcr_client() as client:
            notebook = await client.notebooks.get(READONLY_NOTEBOOK_ID)

        assert_decoded_equals(
            notebook.id, "c3f6285f-1709-44c4-9cd6-e95cf0ea4f5e", field="notebooks_get.id"
        )
        assert_decoded_equals(
            notebook.title,
            "TypeScript Fundamentals: A Handbook of Type Systems and Rules",
            field="notebooks_get.title",
        )
        assert_decoded_equals(notebook.sources_count, 2, field="notebooks_get.sources_count")
        assert_decoded_equals(notebook.is_owner, True, field="notebooks_get.is_owner")
        # created_at is the CREATION instant (``data[5][8][0]``); modified_at is
        # the LAST-MODIFIED instant (``data[5][5][0]``). Pin both epochs (TZ-
        # invariant per #1511/#1519) to lock the previously-swapped slots.
        assert notebook.created_at is not None
        assert notebook.created_at.tzinfo is not None
        assert_decoded_equals(
            int(notebook.created_at.timestamp()),
            1767921609,  # data[5][8][0] — true creation instant
            field="notebooks_get.created_at (epoch seconds)",
        )
        assert notebook.modified_at is not None
        assert notebook.modified_at.tzinfo is not None
        assert_decoded_equals(
            int(notebook.modified_at.timestamp()),
            1768963937,  # data[5][5][0] — last-modified instant
            field="notebooks_get.modified_at (epoch seconds)",
        )
        assert notebook.created_at < notebook.modified_at

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("notebooks_get_summary.yaml")
    async def test_get_summary_decoded_golden(self):
        """``notebooks.get_summary`` decodes the recorded summary text (VfAZjd)."""
        async with vcr_client() as client:
            summary = await client.notebooks.get_summary(READONLY_NOTEBOOK_ID)

        assert_decoded_equals(len(summary), 1016, field="notebooks_get_summary length")
        assert summary.startswith(
            "The provided sources introduce **Learn Claude Code**, an educational repository"
        ), f"Unexpected summary head: {summary[:120]!r}"

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("notebooks_get_description.yaml")
    async def test_get_description_decoded_golden(self):
        """``notebooks.get_description`` decodes summary + suggested topics (VfAZjd)."""
        async with vcr_client() as client:
            description = await client.notebooks.get_description(READONLY_NOTEBOOK_ID)

        assert description.summary.startswith(
            "The provided sources introduce **learn-claude-code**, an educational GitHub"
        ), f"Unexpected description head: {description.summary[:120]!r}"
        assert_decoded_equals(
            len(description.suggested_topics),
            3,
            field="notebooks_get_description.suggested_topics length",
        )
        assert_decoded_equals(
            description.suggested_topics[0].question,
            "How do the five progressive versions demystify the core mechanics of AI agents?",
            field="notebooks_get_description.suggested_topics[0].question",
        )

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("notebooks_create.yaml")
    async def test_create_decoded_golden(self):
        """``notebooks.create`` decodes the server-assigned notebook id (CCqFvf)."""
        async with vcr_client() as client:
            notebook = await client.notebooks.create("VCR Test Notebook")

        # The id is server-assigned — it can only come from the decoded
        # CREATE_NOTEBOOK response (the title alone could be an input echo).
        assert_decoded_equals(
            notebook.id, "afefc562-f8d1-41ec-a5d5-c197efdf52e1", field="notebooks_create.id"
        )
        assert_decoded_equals(notebook.title, "VCR Test Notebook", field="notebooks_create.title")
        assert_decoded_equals(notebook.sources_count, 0, field="notebooks_create.sources_count")


# =============================================================================
# Source mutations (izAoDd add text/url, o4cbdc add file, b7Wfje rename)
# =============================================================================


class TestSourceMutationsGoldenDecoded:
    """Pin the decoded ``Source`` returned by the add/rename mutation paths."""

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("sources_add_text.yaml")
    async def test_add_text_decoded_golden(self):
        """``sources.add_text`` decodes the server-assigned source id + status."""
        async with vcr_client() as client:
            source = await client.sources.add_text(
                MUTABLE_NOTEBOOK_ID,
                title="VCR Test Source",
                content="This is a test source created by VCR recording.",
            )

        assert_decoded_equals(
            source.id, "467b7f67-1b66-45fb-8cc7-6c04723f152d", field="sources_add_text.id"
        )
        assert_decoded_equals(source.title, "VCR Test Source", field="sources_add_text.title")
        assert_decoded_equals(source.status, SourceStatus.READY, field="sources_add_text.status")

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("sources_add_url.yaml")
    async def test_add_url_decoded_golden(self):
        """``sources.add_url`` decodes the server-extracted page title.

        The title is the strongest pin here: the test passes only the URL, so
        ``"Artificial intelligence - Wikipedia"`` can only come out of the
        decoded ADD_SOURCE response (never an input echo).
        """
        async with vcr_client() as client:
            source = await client.sources.add_url(
                MUTABLE_NOTEBOOK_ID,
                url="https://en.wikipedia.org/wiki/Artificial_intelligence",
            )

        assert_decoded_equals(
            source.id, "20d66b0b-787f-480e-a9c1-6823f7a12d8e", field="sources_add_url.id"
        )
        assert_decoded_equals(
            source.title,
            "Artificial intelligence - Wikipedia",
            field="sources_add_url.title",
        )
        assert_decoded_equals(
            source.url,
            "https://en.wikipedia.org/wiki/Artificial_intelligence",
            field="sources_add_url.url",
        )
        assert_decoded_equals(source.status, SourceStatus.READY, field="sources_add_url.status")

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("sources_add_file.yaml")
    async def test_add_file_decoded_golden(self, tmp_path):
        """``sources.add_file`` decodes the registered source id (o4cbdc).

        The id is the ONLY recording-derived field on this path: in the
        default no-wait flow the returned ``Source``'s title/status are
        client-synthesized placeholders (``_source/upload.py``), so pinning
        them would assert the synthesizer, not the decoder.
        """
        test_file = tmp_path / "vcr_test_document.txt"
        with test_file.open("w", encoding="utf-8", newline="\n") as f:
            f.write("This is a test document for VCR cassette recording.")

        async with vcr_client() as client:
            source = await client.sources.add_file(MUTABLE_NOTEBOOK_ID, str(test_file))

        assert_decoded_equals(
            source.id, "dc84ca28-2629-49ac-aec3-de45f0ec93e4", field="sources_add_file.id"
        )

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("sources_rename.yaml")
    async def test_rename_decoded_golden(self):
        """``sources.rename`` decodes the renamed ``Source`` row (b7Wfje)."""
        async with vcr_client() as client:
            sources = await client.sources.list(MUTABLE_NOTEBOOK_ID)
            assert sources, "expected the recorded notebook to have sources"
            original = sources[0]
            renamed = await client.sources.rename(
                MUTABLE_NOTEBOOK_ID, original.id, "VCR Test Renamed Source"
            )
            # Restore (replays the second recorded UPDATE_SOURCE interaction).
            await client.sources.rename(MUTABLE_NOTEBOOK_ID, original.id, original.title)

        assert_decoded_equals(
            renamed.id, "b1b9efdd-b2af-4974-ad97-16025c05f1d7", field="sources_rename.id"
        )
        assert_decoded_equals(
            renamed.title, "VCR Test Renamed Source", field="sources_rename.title"
        )
        assert_decoded_equals(renamed.status, SourceStatus.READY, field="sources_rename.status")


# =============================================================================
# Notes (cFji9 list, CYK0Xb create)
# =============================================================================


class TestNotesGoldenDecoded:
    """Pin decoded ``Note`` fields for the notes cassettes."""

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("notes_list.yaml")
    async def test_list_decoded_golden(self):
        """``notes.list`` decodes the recorded (empty) note list without drift.

        The recorded notebook has one mind map and zero notes, so the decoded
        list MUST be empty — a filter mis-decode that lets the mind-map row
        leak into the notes list would make this non-empty while the cassette
        still replays. (The mind-map side of the same ``cFji9`` payload is
        pinned via ``mind_maps.list`` below.)
        """
        async with vcr_client() as client:
            notes = await client.notes.list(READONLY_NOTEBOOK_ID)

        assert_decoded_equals(len(notes), 0, field="notes_list length")

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("notes_create.yaml")
    async def test_create_decoded_golden(self):
        """``notes.create`` decodes the server-assigned note id (CYK0Xb)."""
        async with vcr_client() as client:
            note = await client.notes.create(
                MUTABLE_NOTEBOOK_ID,
                title="VCR Test Note",
                content="This is a test note created by VCR recording.",
            )

        assert_decoded_equals(
            note.id, "3ba71644-5e30-4330-96d8-d29f5f1ecef4", field="notes_create.id"
        )
        assert_decoded_equals(note.title, "VCR Test Note", field="notes_create.title")
        assert_decoded_equals(
            note.content,
            "This is a test note created by VCR recording.",
            field="notes_create.content",
        )
        # The CREATE_NOTE response carries the creation timestamp in the note
        # metadata envelope (``row[1][2][2][0]``; issue #1529). Pin the decoded
        # EPOCH INT — not the wall-time string — so the assertion is identical
        # on every CI timezone (#1511/#1519 lesson). The round-tripped
        # ``created_at.timestamp()`` is the same TZ-invariant int.
        assert note.created_at is not None
        assert_decoded_equals(
            int(note.created_at.timestamp()),
            1768312234,
            field="notes_create.created_at (epoch seconds)",
        )


# =============================================================================
# Chat history (khqZz conversation turns via get_history)
# =============================================================================


class TestChatHistoryGoldenDecoded:
    """Pin the decoded Q&A pairs from the conversation-turns cassette."""

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("chat_get_history.yaml")
    async def test_get_history_decoded_golden(self):
        """``chat.get_history`` decodes the recorded (question, answer) pair.

        Exercises the ``ConversationTurnRow`` adapter end-to-end: a role-column
        or text-column slip would swap/garble the Q&A pairing while the
        cassette still replays.
        """
        async with vcr_client() as client:
            pairs = await client.chat.get_history(MUTABLE_NOTEBOOK_ID)

        assert_decoded_equals(len(pairs), 1, field="chat_get_history length")
        question, answer = pairs[0]
        assert_decoded_equals(
            question, "What question should I ask?", field="chat_get_history[0].question"
        )
        assert_decoded_equals(len(answer), 233, field="chat_get_history[0].answer length")
        assert answer.startswith(
            "Based on the sources provided, you can ask about the main topics"
        ), f"Unexpected answer head: {answer[:120]!r}"


# =============================================================================
# Labels (I3xc3c list, agX4Bc create)
# =============================================================================


class TestLabelsGoldenDecoded:
    """Pin decoded ``Label`` fields for the label cassettes."""

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("label_list.yaml")
    async def test_list_decoded_golden(self):
        """``labels.list`` decodes the recorded label row (id/name/emoji)."""
        async with vcr_client() as client:
            labels = await client.labels.list(MUTABLE_NOTEBOOK_ID)

        assert_decoded_equals(len(labels), 1, field="label_list length")
        label = labels[0]
        assert_decoded_equals(
            label.id, "fdfc8ac4-3237-4f2a-8a79-3e24297a7040", field="label_list[0].id"
        )
        assert_decoded_equals(label.name, "VCR Test Label", field="label_list[0].name")
        assert_decoded_equals(label.emoji, "\U0001f4c4", field="label_list[0].emoji")
        assert_decoded_equals(label.source_ids, [], field="label_list[0].source_ids")

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("label_create.yaml")
    async def test_create_decoded_golden(self):
        """``labels.create`` ID-diffs the create echo to the new ``Label`` (agX4Bc)."""
        async with vcr_client() as client:
            label = await client.labels.create(MUTABLE_NOTEBOOK_ID, "VCR Label")

        assert_decoded_equals(
            label.id, "fdfc8ac4-3237-4f2a-8a79-3e24297a7040", field="label_create.id"
        )
        # The recorded server echo carries the label as actually created
        # (name + emoji come from the response, not the test's input).
        assert_decoded_equals(label.name, "VCR Test Label", field="label_create.name")
        assert_decoded_equals(label.emoji, "\U0001f4c4", field="label_create.emoji")


# =============================================================================
# Sharing (JFMDGd get_status)
# =============================================================================


class TestSharingGoldenDecoded:
    """Pin the decoded ``ShareStatus`` fields."""

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("sharing_get_status.yaml")
    async def test_get_status_decoded_golden(self):
        """``sharing.get_status`` decodes access/view-level/user-permission slots."""
        async with vcr_client() as client:
            status = await client.sharing.get_status(READONLY_NOTEBOOK_ID)

        assert_decoded_equals(status.is_public, False, field="sharing_get_status.is_public")
        assert_decoded_equals(status.access.name, "RESTRICTED", field="sharing_get_status.access")
        assert_decoded_equals(
            status.view_level.name, "FULL_NOTEBOOK", field="sharing_get_status.view_level"
        )
        assert_decoded_equals(status.share_url, None, field="sharing_get_status.share_url")
        # One shared user (the owner) is recorded; the email/name are scrubbed
        # by the sanitizer so only the decoded permission enum is pinned.
        assert_decoded_equals(
            len(status.shared_users), 1, field="sharing_get_status.shared_users length"
        )
        assert_decoded_equals(
            status.shared_users[0].permission,
            SharePermission.OWNER,
            field="sharing_get_status.shared_users[0].permission",
        )


# =============================================================================
# Research (Ljjv0c fast start, QA9ei deep start, e3bVqc poll)
# =============================================================================


class TestResearchGoldenDecoded:
    """Pin decoded research start/poll fields."""

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("research_start_fast.yaml")
    async def test_start_fast_decoded_golden(self):
        """``research.start(mode="fast")`` decodes the server task id (Ljjv0c)."""
        async with vcr_client() as client:
            result = await client.research.start(
                MUTABLE_NOTEBOOK_ID,
                query="Python programming best practices",
                source="web",
                mode="fast",
            )

        assert_decoded_equals(
            result.task_id,
            "ac0bc757-fa42-4a0d-8c22-755a9ff075a3",
            field="research_start_fast.task_id",
        )
        assert_decoded_equals(result.report_id, None, field="research_start_fast.report_id")
        assert_decoded_equals(result.mode, "fast", field="research_start_fast.mode")

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("research_start_deep.yaml")
    async def test_start_deep_decoded_golden(self):
        """``research.start(mode="deep")`` decodes BOTH the task and report ids (QA9ei).

        Deep research is the one start variant that returns a second id — a
        column slip between the two would satisfy any is-not-None check.
        """
        async with vcr_client() as client:
            result = await client.research.start(
                MUTABLE_NOTEBOOK_ID,
                query="Artificial intelligence history",
                source="web",
                mode="deep",
            )

        assert_decoded_equals(
            result.task_id,
            "e9b7cb1c-268a-4678-8a50-686722f65e27",
            field="research_start_deep.task_id",
        )
        assert_decoded_equals(
            result.report_id,
            "24f83c74-9f7a-4137-aaf3-68e54840dca5",
            field="research_start_deep.report_id",
        )
        assert_decoded_equals(result.mode, "deep", field="research_start_deep.mode")

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("research_poll.yaml")
    async def test_poll_decoded_golden(self):
        """``research.poll`` decodes the recorded poll outcome (e3bVqc).

        The recorded POLL_RESEARCH response carries no entry for the started
        task, so the decoder must return the NOT_FOUND sentinel — a poll-row
        mis-decode that fabricated a task entry would flip the status.
        """
        async with vcr_client() as client:
            start = await client.research.start(
                MUTABLE_NOTEBOOK_ID,
                query="Machine learning fundamentals",
                source="web",
                mode="fast",
            )
            result = await client.research.poll(MUTABLE_NOTEBOOK_ID, task_id=start.task_id)

        # The start task id is decoded from the Ljjv0c response recorded in
        # this cassette (distinct from research_start_fast.yaml's).
        assert_decoded_equals(
            start.task_id,
            "32b1e6c3-863f-4502-8509-fe9d5801db14",
            field="research_poll.start.task_id",
        )
        assert_decoded_equals(result.status, ResearchStatus.NOT_FOUND, field="research_poll.status")
        assert_decoded_equals(result.sources, (), field="research_poll.sources")


# =============================================================================
# Settings (ZwVcOc get, hT54vc set)
# =============================================================================


class TestSettingsGoldenDecoded:
    """Pin decoded settings values (language codes)."""

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("settings_get_output_language.yaml")
    async def test_get_output_language_decoded_golden(self):
        """``settings.get_output_language`` decodes the recorded language code."""
        async with vcr_client() as client:
            language = await client.settings.get_output_language()

        assert_decoded_equals(language, "fr", field="settings_get_output_language")

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("settings_set_output_language.yaml")
    async def test_set_output_language_decoded_golden(self):
        """``settings.set_output_language`` decodes the SET response echo (hT54vc).

        ``set_output_language`` parses the language back out of the
        SET_USER_SETTINGS response (it is NOT an input echo) — the recorded
        flow reads ``fr``, sets ``en``, restores ``fr``.
        """
        async with vcr_client() as client:
            original = await client.settings.get_output_language()
            result = await client.settings.set_output_language("en")
            restored = await client.settings.set_output_language(original)

        assert_decoded_equals(original, "fr", field="settings_set_output_language.original")
        assert_decoded_equals(result, "en", field="settings_set_output_language.set result")
        assert_decoded_equals(restored, "fr", field="settings_set_output_language.restore result")


# =============================================================================
# Artifact generation / export / revision (R7cb6c, ciyUvf, Krh3pd, KmcKPe)
# =============================================================================


class TestArtifactsWriteGoldenDecoded:
    """Pin decoded ``GenerationStatus`` / suggestion / export values."""

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("artifacts_generate_report.yaml")
    async def test_generate_report_decoded_golden(self):
        """``artifacts.generate_report`` decodes the server task id (R7cb6c)."""
        async with vcr_client() as client:
            status = await client.artifacts.generate_report(
                MUTABLE_NOTEBOOK_ID,
                report_format=ReportFormat.BRIEFING_DOC,
            )

        assert_decoded_equals(
            status.task_id,
            "31dc7d61-2b07-444e-8be2-5da70154ac5a",
            field="artifacts_generate_report.task_id",
        )
        assert_decoded_equals(
            status.status, "in_progress", field="artifacts_generate_report.status"
        )
        assert_decoded_equals(status.error, None, field="artifacts_generate_report.error")

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("artifacts_suggest_reports.yaml")
    async def test_suggest_reports_decoded_golden(self):
        """``artifacts.suggest_reports`` decodes per-suggestion semantic fields (ciyUvf).

        Titles are scrubbed in the cassette, so the pins are the decoded
        audience-level column (the positional canary) and the first
        suggestion's description text.
        """
        async with vcr_client() as client:
            suggestions = await client.artifacts.suggest_reports(READONLY_NOTEBOOK_ID)

        assert_decoded_equals(len(suggestions), 4, field="artifacts_suggest_reports length")
        assert_decoded_equals(
            [s.audience_level for s in suggestions],
            [2, 2, 1, 1],
            field="artifacts_suggest_reports audience_levels",
        )
        assert_decoded_equals(
            suggestions[0].description,
            "A long-form plan for applying the source's core methodologies within a "
            "professional organization.",
            field="artifacts_suggest_reports[0].description",
        )

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("artifacts_export_report.yaml")
    async def test_export_report_decoded_golden(self):
        """``artifacts.export_report`` decodes the exported Google Doc URL (Krh3pd)."""
        async with vcr_client() as client:
            reports = await client.artifacts.list_reports(MUTABLE_NOTEBOOK_ID)
            completed = [r for r in reports if r.is_completed]
            assert completed, "expected a completed report in the recorded list"
            result = await client.artifacts.export_report(
                MUTABLE_NOTEBOOK_ID, completed[0].id, title="VCR Export Test"
            )

        assert_decoded_equals(
            completed[0].id,
            "07733c15-fa16-4c17-8a6f-2e8b7a9da2dd",
            field="artifacts_export_report.report id",
        )
        # The export response is a single-element envelope holding the doc URL.
        assert_decoded_equals(
            result,
            ["https://docs.google.com/document/d/1bAgBGlybk82LZfbz6IPCwpQ12E4hlDQsuWTVWJVEHfM"],
            field="artifacts_export_report.result",
        )

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("artifacts_revise_slide.yaml")
    async def test_revise_slide_decoded_golden(self):
        """``artifacts.revise_slide`` decodes the revision task id (KmcKPe)."""
        async with vcr_client() as client:
            status = await client.artifacts.revise_slide(
                MUTABLE_NOTEBOOK_ID,
                "848df2ec-4916-4dea-aa20-3dc02954cfd0",
                0,
                "Make it shorter",
            )

        assert_decoded_equals(
            status.task_id,
            "b84c4e66-ce7b-43b8-ac86-80c8add3fa23",
            field="artifacts_revise_slide.task_id",
        )
        assert_decoded_equals(status.status, "in_progress", field="artifacts_revise_slide.status")


# =============================================================================
# Mind maps (v9rmvd interactive tree, yyryJe generate chain)
# =============================================================================


class TestMindMapsGoldenDecoded:
    """Pin decoded ``MindMap`` rows and tree node values."""

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("mind_maps_interactive.yaml")
    async def test_interactive_list_and_tree_decoded_golden(self):
        """``mind_maps.list`` + ``get_tree`` decode the interactive mind map (v9rmvd).

        The tree lives at a deep positional slot of the GET_INTERACTIVE_HTML
        response — pinning the root/child node names catches a slot slip that
        would otherwise surface as a structurally-valid-but-wrong tree.
        """
        async with vcr_client() as client:
            maps = await client.mind_maps.list(MINDMAP_NOTEBOOK_ID)
            interactive = [m for m in maps if m.kind == MindMapKind.INTERACTIVE]
            assert interactive, "expected an interactive mind map in the recording"
            mind_map = interactive[0]
            tree = await client.mind_maps.get_tree(
                MINDMAP_NOTEBOOK_ID, mind_map.id, kind=MindMapKind.INTERACTIVE
            )

        assert_decoded_equals(
            mind_map.id,
            "47523923-9839-48fd-ae10-25bb685a0644",
            field="mind_maps_interactive.list[0].id",
        )
        assert_decoded_equals(
            mind_map.title, "Learning Mindmap AAA", field="mind_maps_interactive.list[0].title"
        )
        assert_decoded_equals(
            tree["name"], "Machine Learning Fundamentals", field="mind_maps_interactive.tree.name"
        )
        children = tree["children"]
        assert_decoded_equals(len(children), 4, field="mind_maps_interactive.tree children count")
        assert_decoded_equals(
            children[0]["name"],
            "Key Concepts",
            field="mind_maps_interactive.tree.children[0].name",
        )

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("notes_list.yaml")
    async def test_note_backed_list_created_at_decoded_golden(self):
        """``mind_maps.list_note_backed`` decodes the note-backed creation time.

        The ``GET_NOTES_AND_MIND_MAPS`` (cFji9) payload carries the timestamp in
        the note metadata envelope at ``row[1][2][2][0]`` — the SAME slot the
        artifact path decodes. Pin the decoded EPOCH INT (not the wall-time
        string) so the assertion is timezone-invariant (#1511/#1519; #1529).
        """
        async with vcr_client() as client:
            maps = await client.mind_maps.list_note_backed(READONLY_NOTEBOOK_ID)

        assert_decoded_equals(len(maps), 1, field="note_backed list length")
        mind_map = maps[0]
        assert_decoded_equals(
            mind_map.kind, MindMapKind.NOTE_BACKED, field="note_backed list[0].kind"
        )
        assert mind_map.created_at is not None
        assert_decoded_equals(
            int(mind_map.created_at.timestamp()),
            1768311078,
            field="note_backed list[0].created_at (epoch seconds)",
        )

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("generate_mind_map_chain.yaml")
    async def test_generate_mind_map_decoded_golden(self):
        """``artifacts.generate_mind_map`` decodes the generated tree + note id (yyryJe).

        The mind-map JSON comes out of the GENERATE_MIND_MAP response and the
        note id out of the chained CREATE_NOTE response — pinning both proves
        the chain plumbed the decoded values, not fabricated defaults.
        """
        async with vcr_client() as client:
            result = await client.artifacts.generate_mind_map(
                MUTABLE_NOTEBOOK_ID,
                source_ids=[_WIKIPEDIA_SOURCE_ID],
            )

        assert_decoded_equals(
            result.note_id,
            "208ac8c0-5206-4e93-ae24-4b83ce14084b",
            field="generate_mind_map_chain.note_id",
        )
        assert result.mind_map is not None
        assert_decoded_equals(
            result.mind_map["name"], "NotebookLM", field="generate_mind_map_chain.mind_map.name"
        )
        assert_decoded_equals(
            len(result.mind_map["children"]),
            6,
            field="generate_mind_map_chain.mind_map children count",
        )
        # The chained CREATE_NOTE response carries the persisted note's creation
        # time, threaded onto MindMapResult.created_at (issue #1529). Pin the
        # decoded EPOCH INT — TZ-invariant — not the wall-time string.
        assert result.created_at is not None
        assert_decoded_equals(
            int(result.created_at.timestamp()),
            1778851315,
            field="generate_mind_map_chain.created_at (epoch seconds)",
        )


# =============================================================================
# Cassette-side sanity: golden values really are in the recordings
# =============================================================================


def test_golden_values_visible_in_cassette_bytes() -> None:
    """Spot-check that representative golden values exist in the cassette bytes.

    Belt-and-braces against a hypothetical future where a decode path starts
    fabricating plausible defaults: each sampled golden value must literally
    appear in its cassette's recorded response, so the pins above are provably
    recording-derived (not synthesized by the client).
    """
    cassette_dir = Path(__file__).resolve().parent.parent / "cassettes"
    # ``(cassette, value)`` pairs — a list (not a dict) so a cassette can be
    # sampled more than once (e.g. an id AND a created_at epoch).
    samples = [
        ("notebooks_create.yaml", "afefc562-f8d1-41ec-a5d5-c197efdf52e1"),
        ("notes_create.yaml", "3ba71644-5e30-4330-96d8-d29f5f1ecef4"),
        # created_at epochs pinned above (issue #1529) — provably recording-derived.
        ("notes_create.yaml", "1768312234"),
        ("notes_list.yaml", "1768311078"),
        ("generate_mind_map_chain.yaml", "1778851315"),
        # Notebook created_at/modified_at epochs (swapped-slot fix): created_at
        # comes from data[5][8][0], modified_at from data[5][5][0].
        ("notebooks_list.yaml", "1768174413"),  # list[0].created_at
        ("notebooks_list.yaml", "1768311605"),  # list[0].modified_at
        ("notebooks_get.yaml", "1767921609"),  # get.created_at
        ("notebooks_get.yaml", "1768963937"),  # get.modified_at
        ("artifacts_export_report.yaml", "1bAgBGlybk82LZfbz6IPCwpQ12E4hlDQsuWTVWJVEHfM"),
        ("research_poll.yaml", "32b1e6c3-863f-4502-8509-fe9d5801db14"),
        ("generate_mind_map_chain.yaml", "208ac8c0-5206-4e93-ae24-4b83ce14084b"),
    ]
    missing = {
        f"{name}:{value}": value
        for name, value in samples
        if value not in (cassette_dir / name).read_text(encoding="utf-8")
    }
    assert missing == {}, (
        "Golden value(s) not found in their cassette bytes — either the cassette "
        f"was re-recorded (refresh the goldens) or the pin is wrong: {missing}"
    )
