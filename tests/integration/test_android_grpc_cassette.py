"""Public-client replay coverage for the Android gRPC cassette seam.

Each test binds one recorded family (see ``tests/integration/README.md``) and
drives the public ``NotebookLMClient`` API. Assertions are mode-neutral: they
hold against the live scratch notebook while recording and against the
sanitized placeholders while replaying. Re-record with::

    NOTEBOOKLM_ANDROID_GRPC_RECORD=1 NOTEBOOKLM_PROFILE=<profile> \\
        uv run pytest tests/integration/test_android_grpc_cassette.py -p no:randomly
"""

from __future__ import annotations

import asyncio
from typing import Any, Protocol

import grpc
import httpx
import pytest

from notebooklm.types import (
    ChatSessionStatus,
    Notebook,
    PlayBook,
    RelevantChunk,
    ShareStatus,
    Source,
    SourceType,
    UserSettings,
)
from tests._helpers.android_grpc_harness import SCRATCH_NOTE_TITLE, is_record_mode

pytestmark = pytest.mark.grpc_cassette


class CassetteBinder(Protocol):
    def __call__(self, name: str, *, phenotype_http: bool = False) -> Any: ...


replay_only = pytest.mark.skipif(
    is_record_mode(), reason="the unbound-channel guards are lifted while recording"
)


@replay_only
def test_grpc_cassette_replay_marker_blocks_unbound_aio_channels() -> None:
    with pytest.raises(RuntimeError, match="refusing an unbound grpc.aio channel"):
        grpc.aio.secure_channel("localhost:443", grpc.ssl_channel_credentials())
    with pytest.raises(RuntimeError, match="refusing an unbound grpc.aio channel"):
        grpc.aio.insecure_channel("localhost:443")


@replay_only
@pytest.mark.asyncio
async def test_grpc_cassette_replay_marker_blocks_unbound_http_fallbacks() -> None:
    async with httpx.AsyncClient() as http_client:
        with pytest.raises(RuntimeError, match="refusing unbound httpx request"):
            await http_client.get(
                "https://notebooklm.google.com/_/LabsTailwindUi/data/batchexecute"
            )
        with pytest.raises(RuntimeError, match="refusing unbound httpx stream"):
            http_client.stream("POST", "https://notebooklm.google.com/_/LabsTailwindUi/data/stream")


# --- 1. settings via unary GetOrCreateAccount ---------------------------------


@pytest.mark.asyncio
async def test_settings_get_user_settings_over_get_or_create_account(
    android_grpc_cassette: CassetteBinder,
) -> None:
    async with android_grpc_cassette("get_or_create_account") as (client, _values):
        settings = await client.settings.get_user_settings()
    assert isinstance(settings, UserSettings)
    # The recording account has an output language; its value is scrubbed on
    # replay but its presence must survive decoding.
    assert settings.output_language
    assert settings.limits.notebook_limit is not None and settings.limits.notebook_limit >= 1


# --- 2. rich GetProject project/source response ------------------------------


@pytest.mark.asyncio
async def test_notebook_get_and_source_list_over_rich_get_project(
    android_grpc_cassette: CassetteBinder,
) -> None:
    async with android_grpc_cassette("get_project_rich") as (client, values):
        notebook = await client.notebooks.get(values.notebook_id)
        sources = await client.sources.list(values.notebook_id)
    assert isinstance(notebook, Notebook)
    assert notebook.id == values.notebook_id
    assert notebook.title
    assert notebook.sources_count == 1
    assert len(sources) == 1
    assert isinstance(sources[0], Source)
    assert sources[0].id and sources[0].title


# --- 3. LoadSource structured content ----------------------------------------


@pytest.mark.asyncio
async def test_source_fulltext_over_load_source(android_grpc_cassette: CassetteBinder) -> None:
    async with android_grpc_cassette("load_source") as (client, values):
        (source,) = await client.sources.list(values.notebook_id)
        fulltext = await client.sources.get_fulltext(values.notebook_id, source.id)
    assert fulltext.source_id == source.id
    assert fulltext.content
    assert fulltext.title


@pytest.mark.asyncio
async def test_source_search_over_retrieve_relevant_chunks(
    android_grpc_cassette: CassetteBinder,
) -> None:
    async with android_grpc_cassette("retrieve_relevant_chunks") as (client, values):
        (source,) = await client.sources.list(values.notebook_id)
        chunks = await client.sources.search(values.notebook_id, values.question, limit=1)
        filtered = await client.sources.search(
            values.notebook_id,
            values.question,
            source_ids=[source.id],
        )
    assert len(chunks) == 1
    assert isinstance(chunks[0], RelevantChunk)
    assert chunks[0].source_id == source.id
    assert chunks[0].text
    assert filtered and all(chunk.source_id == source.id for chunk in filtered)


@pytest.mark.asyncio
async def test_play_books_list_and_add_with_phenotype_metadata(
    android_grpc_cassette: CassetteBinder,
) -> None:
    async with android_grpc_cassette("play_books", phenotype_http=True) as (client, values):
        books = await client.sources.list_play_books()
        assert books and all(isinstance(book, PlayBook) for book in books)
        exportable = next((book for book in books if not book.export_disabled), None)
        assert exportable is not None, "recording account needs one exportable Play Book"

        added = await client.sources.add_play_book(
            values.notebook_id,
            exportable.content_id,
            wait=False,
        )
        try:
            assert added.kind is SourceType.EXPERT_INTELLIGENCE
            assert added.id
        finally:
            await client.sources.delete(values.notebook_id, added.id)


# --- 4. ListArtifacts plus GetNotes ------------------------------------------


@pytest.mark.asyncio
async def test_artifact_and_note_listing_over_list_artifacts_and_get_notes(
    android_grpc_cassette: CassetteBinder,
) -> None:
    async with android_grpc_cassette("list_artifacts_get_notes") as (client, values):
        artifacts = await client.artifacts.list(values.notebook_id)
        notes = await client.notes.list(values.notebook_id)
    assert artifacts == []
    assert len(notes) == 1
    assert notes[0].id
    # Titles are real while recording and scrubbed on replay; both are non-empty.
    assert notes[0].title == SCRATCH_NOTE_TITLE or notes[0].title.startswith("SCRUBBED_")


# --- 5. both GetLabels arms: notebook labels and account collections ---------


@pytest.mark.asyncio
async def test_label_and_collection_listing_over_get_labels(
    android_grpc_cassette: CassetteBinder,
) -> None:
    async with android_grpc_cassette("get_labels") as (client, values):
        labels = await client.labels.list(values.notebook_id)
        # Establish one known collection so the account arm is non-empty on
        # any recording account, then remove it again.
        created = await client.collections.create(values.texts[0])
        try:
            collections = await client.collections.list()
        finally:
            await client.collections.delete(created.id)
    assert labels == []
    # A regression returning [] from the second GetLabels arm would fail here.
    assert any(item.id == created.id for item in collections)
    assert all(item.id and item.name for item in collections)


# --- 6. ListDiscoverSourcesJob research state --------------------------------


@pytest.mark.asyncio
async def test_research_poll_over_list_discover_sources_job(
    android_grpc_cassette: CassetteBinder,
) -> None:
    from notebooklm.types import ResearchTask

    async with android_grpc_cassette("list_discover_sources_job") as (client, values):
        # A scratch notebook has no research runs: the decoder must surface the
        # empty job list as the public "empty" task, not a decode crash.
        task = await client.research.poll(values.notebook_id)
    assert task == ResearchTask.empty()


# --- 7. sharing GetProjectDetails --------------------------------------------


@pytest.mark.asyncio
async def test_sharing_status_over_get_project_details(
    android_grpc_cassette: CassetteBinder,
) -> None:
    async with android_grpc_cassette("get_project_details") as (client, values):
        status = await client.sharing.get_status(values.notebook_id)
    assert isinstance(status, ShareStatus)
    assert status.is_public is False


# --- 9 (recorded before 8 so the history cassette is non-empty) --------------
# server-streaming GenerateFreeFormStreamed


@pytest.mark.asyncio
async def test_chat_ask_over_generate_free_form_streamed(
    android_grpc_cassette: CassetteBinder,
) -> None:
    async with android_grpc_cassette("generate_free_form_streamed") as (client, values):
        answer = await client.chat.ask(values.notebook_id, values.question)
    assert answer.answer
    assert answer.conversation_id


# --- 8. chat ListChatSessions plus ListChatTurns -----------------------------


@pytest.mark.asyncio
async def test_chat_history_over_list_chat_sessions_and_turns(
    android_grpc_cassette: CassetteBinder,
) -> None:
    async with android_grpc_cassette("list_chat_sessions_turns") as (client, values):
        # Create the conversation inside the family so it re-records on its own.
        answer = await client.chat.ask(values.notebook_id, values.question)
        conversation_id = await client.chat.get_conversation_id(values.notebook_id)
        history = await client.chat.get_history(values.notebook_id)
    assert conversation_id == answer.conversation_id
    # The reserved question placeholder round-trips through ListChatTurns, which
    # pins turn pairing and ordering. Answer *rendering* is not replayable: it is
    # sliced out of ``response_doc`` by index ranges that numeric redaction
    # collapses, so only its type is asserted here (see tests/integration/README.md).
    assert [question for question, _answer in history] == [values.question]
    assert all(isinstance(answer, str) for _question, answer in history)


@pytest.mark.asyncio
async def test_chat_session_status_and_cancel(
    android_grpc_cassette: CassetteBinder,
) -> None:
    async with android_grpc_cassette("chat_session_control") as (client, values):
        answer = await client.chat.ask(values.notebook_id, values.question)
        status = await client.chat.session_status(
            values.notebook_id,
            answer.conversation_id,
        )
        await client.chat.cancel(values.notebook_id, answer.conversation_id)
    assert isinstance(status, ChatSessionStatus)
    assert status.generating is False
    assert status.token is None


# =============================================================================
# Mutation families — each records and cleans up its own state on the scratch
# notebook (or on notebooks it creates), so replay is self-contained.
# =============================================================================


async def _settle(seconds: float) -> None:
    """Wait for the live backend while recording; a no-op on replay.

    Polling helpers in the public API sleep between polls in *both* modes, so
    families poll once per settled wait instead of spinning inside the client.
    """

    if is_record_mode():
        await asyncio.sleep(seconds)


# --- notebooks: CreateProject, MutateProject, ListRecentlyViewedProjects, CopyProject, DeleteProjects


@pytest.mark.asyncio
async def test_notebook_lifecycle_over_project_mutations(
    android_grpc_cassette: CassetteBinder,
) -> None:
    async with android_grpc_cassette("notebook_lifecycle") as (client, values):
        created = await client.notebooks.create(values.texts[0])
        copied = None
        try:
            renamed = await client.notebooks.rename(created.id, values.texts[1])
            with_emoji = await client.notebooks.set_emoji(created.id, values.texts[2])
            listed = await client.notebooks.list()
            copied = await client.notebooks.copy(created.id, values.texts[3])
        finally:
            # Account-scoped resources: never leave them behind on a failure.
            if copied is not None:
                await client.notebooks.delete(copied.id)
            await client.notebooks.delete(created.id)
    assert created.id and renamed.id == created.id and with_emoji.id == created.id
    assert renamed.title == values.texts[1]
    assert any(notebook.id == created.id for notebook in listed)
    assert copied.id and copied.id != created.id


# --- notebooks: GenerateNotebookGuide ------------------------------------------


@pytest.mark.asyncio
async def test_notebook_guide_over_generate_notebook_guide(
    android_grpc_cassette: CassetteBinder,
) -> None:
    async with android_grpc_cassette("generate_notebook_guide") as (client, values):
        description = await client.notebooks.get_description(values.notebook_id)
        summary = await client.notebooks.get_summary(values.notebook_id)
    assert description is not None
    assert summary


# --- sources: AddTentativeSources, AddSources, GetProject, MutateSource,
#     GenerateDocumentGuides, CheckSourceFreshness, RefreshSource, DeleteSources


@pytest.mark.asyncio
async def test_source_lifecycle_over_source_mutations(
    android_grpc_cassette: CassetteBinder,
) -> None:
    async with android_grpc_cassette("source_lifecycle") as (client, values):
        text_source = await client.sources.add_text(
            values.notebook_id,
            values.texts[0],
            values.texts[1],
            wait=False,
        )
        url_source = await client.sources.add_url(values.notebook_id, values.url, wait=False)
        await _settle(20)
        text_ready = await client.sources.wait_until_ready(
            values.notebook_id, text_source.id, timeout=60, initial_interval=0.1
        )
        url_ready = await client.sources.wait_until_ready(
            values.notebook_id, url_source.id, timeout=120, initial_interval=0.1
        )
        renamed = await client.sources.rename(values.notebook_id, text_source.id, values.texts[2])
        # Both echo shapes from issue #2276, in order. The backend labels a
        # guide with the requested source id on the FIRST response for that
        # source and omits the label on every repeat call, so calling twice is
        # what pins the relaxed echo rule against recorded bytes -- the second
        # call is the one that used to raise ``DecodingError``.
        guide = await client.sources.get_guide(values.notebook_id, text_source.id)
        guide_again = await client.sources.get_guide(values.notebook_id, text_source.id)
        fresh = await client.sources.check_freshness(values.notebook_id, url_source.id)
        await client.sources.refresh(values.notebook_id, url_source.id)
        await client.sources.delete(values.notebook_id, url_source.id)
        await client.sources.delete(values.notebook_id, text_source.id)
    assert text_source.id and url_source.id and text_source.id != url_source.id
    assert text_ready.id == text_source.id and url_ready.id == url_source.id
    assert renamed is not None and renamed.id == text_source.id
    assert isinstance(guide.summary, str)
    # Same guide either way: only the source-id echo differs between the two
    # responses, which is exactly what the relaxed rule has to tolerate.
    assert guide_again.summary == guide.summary
    assert guide_again.keywords == guide.keywords
    assert fresh is True  # an empty CheckSourceFreshness response means "fresh"


# --- notes: CreateNote, MutateNote, GetNotes, DeleteNotes ----------------------


@pytest.mark.asyncio
async def test_note_lifecycle_over_note_mutations(android_grpc_cassette: CassetteBinder) -> None:
    async with android_grpc_cassette("note_lifecycle") as (client, values):
        note = await client.notes.create(
            values.notebook_id, title=values.texts[0], content=values.texts[1]
        )
        await client.notes.update(
            values.notebook_id, note.id, title=values.texts[2], content=values.texts[3]
        )
        fetched = await client.notes.get(values.notebook_id, note.id)
        await client.notes.delete(values.notebook_id, note.id)
    assert note.id and fetched.id == note.id
    assert (fetched.title, fetched.content) == (values.texts[2], values.texts[3])


# --- labels: CreateLabel, MutateLabel, GetLabels, DeleteLabels -----------------


@pytest.mark.asyncio
async def test_label_lifecycle_over_label_mutations(
    android_grpc_cassette: CassetteBinder,
) -> None:
    async with android_grpc_cassette("label_lifecycle") as (client, values):
        (source,) = await client.sources.list(values.notebook_id)
        label = await client.labels.create(
            values.notebook_id, values.texts[0], emoji=values.texts[1]
        )
        renamed = await client.labels.rename(values.notebook_id, label.id, values.texts[2])
        with_emoji = await client.labels.set_emoji(values.notebook_id, label.id, values.texts[3])
        await client.labels.add_sources(values.notebook_id, label.id, [source.id])
        members = await client.labels.sources(values.notebook_id, label.id)
        await client.labels.remove_sources(values.notebook_id, label.id, [source.id])
        # ``scope="all"`` regenerates the notebook's labels (destructive for
        # existing ones); safe here because only the scratch notebook is touched.
        generated = await client.labels.generate(values.notebook_id, scope="all")
        await client.labels.delete(values.notebook_id, [label.id, *[item.id for item in generated]])
        remaining = await client.labels.list(values.notebook_id)
    assert label.id and renamed is not None and renamed.id == label.id
    assert with_emoji is not None and with_emoji.id == label.id
    assert [member.id for member in members] == [source.id]
    assert len(generated) >= 1
    assert remaining == []


# --- collections: CreateLabel, MutateLabel, GetLabels, DeleteLabels (account arm)


@pytest.mark.asyncio
async def test_collection_lifecycle_over_collection_mutations(
    android_grpc_cassette: CassetteBinder,
) -> None:
    async with android_grpc_cassette("collection_lifecycle") as (client, values):
        collection = await client.collections.create(values.texts[0])
        try:
            renamed = await client.collections.rename(collection.id, values.texts[1])
            await client.collections.add_notebooks(collection.id, [values.notebook_id])
            members = await client.collections.notebooks(collection.id)
            await client.collections.remove_notebooks(collection.id, [values.notebook_id])
        finally:
            # Collections are account-scoped and outlive the scratch notebook.
            await client.collections.delete(collection.id)
        remaining = await client.collections.get_or_none(collection.id)
    assert collection.id and renamed is not None and renamed.id == collection.id
    assert [member.id for member in members] == [values.notebook_id]
    assert remaining is None


# --- sharing: ShareProject, GetProjectDetails ----------------------------------


@pytest.mark.asyncio
async def test_sharing_toggle_over_share_project(android_grpc_cassette: CassetteBinder) -> None:
    async with android_grpc_cassette("share_project") as (client, values):
        opened = await client.sharing.set_public(values.notebook_id, True)
        status = await client.sharing.get_status(values.notebook_id)
        closed = await client.sharing.set_public(values.notebook_id, False)
    assert opened.is_public is True
    assert status.is_public is True
    assert closed.is_public is False


# --- chat: DeleteChatTurns -----------------------------------------------------


@pytest.mark.asyncio
async def test_chat_clear_over_delete_chat_turns(android_grpc_cassette: CassetteBinder) -> None:
    async with android_grpc_cassette("delete_chat_turns") as (client, values):
        answer = await client.chat.ask(values.notebook_id, values.question)
        await client.chat.delete_conversation(values.notebook_id, answer.conversation_id)
        after = await client.chat.get_history(values.notebook_id)
    assert answer.conversation_id
    assert after == []


# --- settings: MutateAccount ---------------------------------------------------


@pytest.mark.asyncio
async def test_settings_language_over_mutate_account(
    android_grpc_cassette: CassetteBinder,
) -> None:
    async with android_grpc_cassette("mutate_account") as (client, _values):
        current = await client.settings.get_output_language()
        # Re-assert the account's own language: a real MutateAccount round-trip
        # that leaves the recording account exactly as it was.
        assert current, "recording account must have an output language set"
        result = await client.settings.set_output_language(current)
    assert result == current


# --- artifacts: ActOnSources (mind map) + GetNotes/DeleteNotes ------------------


@pytest.mark.asyncio
async def test_mind_map_over_act_on_sources(android_grpc_cassette: CassetteBinder) -> None:
    async with android_grpc_cassette("act_on_sources_mind_map") as (client, values):
        result = await client.artifacts.generate_mind_map(values.notebook_id)
        # Mind-map rows are recognised by their JSON note content, which the
        # redactor scrubs, so replay cannot rediscover them through
        # ``list_mind_maps`` / ``delete_mind_map``; the persisted note id is
        # deterministic and ``notes.delete`` matches by id.
        assert result.note_id, "mind map was not persisted to a note"
        await client.notes.delete(values.notebook_id, result.note_id)
    assert result.mind_map is not None


# --- artifacts: CreateArtifact (quiz), ListArtifacts/GetArtifact, UpdateArtifact, DeleteArtifact


@pytest.mark.asyncio
async def test_quiz_lifecycle_over_artifact_mutations(
    android_grpc_cassette: CassetteBinder,
) -> None:
    async with android_grpc_cassette("quiz_lifecycle") as (client, values):
        started = await client.artifacts.generate_quiz(values.notebook_id)
        polls = 0
        while True:
            status = await client.artifacts.poll_status(values.notebook_id, started.task_id)
            polls += 1
            if status.is_complete or polls >= 12:
                break
            await _settle(15)
        quiz = await client.artifacts.get(values.notebook_id, started.task_id)
        renamed = await client.artifacts.rename(
            values.notebook_id, started.task_id, values.texts[0]
        )
        await client.artifacts.delete(values.notebook_id, started.task_id)
        after = await client.artifacts.get_or_none(values.notebook_id, started.task_id)
    assert started.task_id
    assert status.is_complete, f"quiz never completed after {polls} polls"
    assert quiz.id == started.task_id
    assert renamed is not None and renamed.id == started.task_id
    assert after is None


# --- artifacts: GenerateReportSuggestions ---------------------------------------


@pytest.mark.asyncio
async def test_report_suggestions_over_generate_report_suggestions(
    android_grpc_cassette: CassetteBinder,
) -> None:
    async with android_grpc_cassette("generate_report_suggestions") as (client, values):
        suggestions = await client.artifacts.suggest_reports(values.notebook_id)
    assert len(suggestions) >= 1
    assert all(item.title and item.prompt for item in suggestions)


# --- research: DiscoverSourcesManifold, ListDiscoverSourcesJob, CancelDiscoverSourcesJob


@pytest.mark.asyncio
async def test_research_fast_cancel_over_discover_sources_manifold(
    android_grpc_cassette: CassetteBinder,
) -> None:
    from notebooklm.types import ResearchStatus

    async with android_grpc_cassette("research_fast_cancel") as (client, values):
        started = await client.research.start(values.notebook_id, values.texts[0], mode="fast")
        # Let the run become listable before cancelling. Fast runs finish in a
        # few seconds, so this family pins the Start -> Cancel -> List wire
        # sequence and the post-cancel terminal status, not a mid-flight abort.
        await _settle(8)
        before = await client.research.poll(values.notebook_id, started.task_id)
        await client.research.cancel(values.notebook_id, started.task_id)
        await _settle(3)
        after = await client.research.poll(values.notebook_id, started.task_id)
    assert started.task_id and started.mode == "fast"
    assert before.task_id == started.task_id
    assert after.task_id == started.task_id
    assert after.status != ResearchStatus.IN_PROGRESS
    assert after.status in {
        ResearchStatus.COMPLETED,
        ResearchStatus.NOT_FOUND,
        ResearchStatus.FAILED,
    }


# --- research: DiscoverSources (synchronous; #2283) --------------------------


@pytest.mark.asyncio
async def test_research_discover_over_discover_sources(
    android_grpc_cassette: CassetteBinder,
) -> None:
    from notebooklm.types import DiscoveryMode, ResearchStatus

    async with android_grpc_cassette("research_discover") as (client, values):
        task = await client.research.discover(values.notebook_id, values.research_query)
    # One unary round trip answers with the ranked sources, the overview and
    # the id of the completed job the backend also recorded.
    assert task.status is ResearchStatus.COMPLETED and task.status_code == 2
    assert task.task_id
    assert task.query == values.research_query
    assert task.discovery_mode is DiscoveryMode.DEFAULT_LLM_SEARCH
    assert task.summary
    assert task.sources
    assert all(src.url and src.title for src in task.sources)
    assert all(src.research_task_id == task.task_id for src in task.sources)
    assert task.tasks == () and task.report == ""


# --- research: DiscoverSourcesManifold, ListDiscoverSourcesJob, FinishDiscoverSourcesRun
#     (recorded last: importing adds sources to the scratch notebook)


@pytest.mark.asyncio
async def test_research_fast_import_over_finish_discover_sources_run(
    android_grpc_cassette: CassetteBinder,
) -> None:
    from notebooklm.types import ResearchStatus

    async with android_grpc_cassette("research_fast_import") as (client, values):
        started = await client.research.start(
            values.notebook_id, values.research_query, mode="fast"
        )
        polls = 0
        while True:
            # A just-started run is not listed yet (``not_found``) and then
            # ``in_progress``; each poll is one ListDiscoverSourcesJob interaction.
            await _settle(15)
            task = await client.research.poll(values.notebook_id, started.task_id)
            polls += 1
            if task.status not in {ResearchStatus.IN_PROGRESS, ResearchStatus.NOT_FOUND}:
                break
            if polls >= 12:
                break
        imported = await client.research.import_sources(
            values.notebook_id, started.task_id, list(task.sources[:1])
        )
    assert task.status == ResearchStatus.COMPLETED, f"research not complete after {polls} polls"
    assert len(task.sources) >= 1
    assert len(imported) == 1


# =============================================================================
# Artifact generation families — CreateArtifact, ListArtifacts/GetArtifact polls,
# DeleteArtifact. Generation runs live for minutes; replay is instant because
# ``_settle`` is a no-op and each poll is one recorded interaction.
# =============================================================================


async def _generate_poll_and_delete(
    client: Any,
    notebook_id: str,
    started: Any,
    *,
    settle_seconds: float,
    max_polls: int,
) -> tuple[Any, Any, Any]:
    """Poll ``started`` to completion, fetch it, delete it, and return the trail."""

    polls = 0
    try:
        while True:
            status = await client.artifacts.poll_status(notebook_id, started.task_id)
            polls += 1
            if status.is_complete or status.is_failed or polls >= max_polls:
                break
            await _settle(settle_seconds)
        artifact = await client.artifacts.get(notebook_id, started.task_id)
    finally:
        # Delete even when polling or retrieval failed so a live recording never
        # leaves a generated artifact behind; a cleanup error must not mask the
        # original failure.
        try:
            await client.artifacts.delete(notebook_id, started.task_id)
        except Exception as cleanup_error:  # noqa: BLE001 - reported, not hidden
            print(
                f"WARNING: artifact {started.task_id} was not deleted: "
                f"{type(cleanup_error).__name__}"
            )
    after = await client.artifacts.get_or_none(notebook_id, started.task_id)
    return status, artifact, after


@pytest.mark.timeout(400)
@pytest.mark.asyncio
async def test_report_generation_over_create_artifact(
    android_grpc_cassette: CassetteBinder,
) -> None:
    async with android_grpc_cassette("generate_report") as (client, values):
        started = await client.artifacts.generate_report(values.notebook_id)
        status, artifact, after = await _generate_poll_and_delete(
            client, values.notebook_id, started, settle_seconds=15, max_polls=16
        )
    assert started.task_id
    assert status.is_complete, f"report generation ended as {status.status}"
    assert artifact.id == started.task_id
    assert after is None


@pytest.mark.timeout(400)
@pytest.mark.asyncio
async def test_flashcard_generation_over_create_artifact(
    android_grpc_cassette: CassetteBinder,
) -> None:
    async with android_grpc_cassette("generate_flashcards") as (client, values):
        started = await client.artifacts.generate_flashcards(values.notebook_id)
        status, artifact, after = await _generate_poll_and_delete(
            client, values.notebook_id, started, settle_seconds=15, max_polls=16
        )
    assert started.task_id
    assert status.is_complete, f"flashcard generation ended as {status.status}"
    assert artifact.id == started.task_id
    assert after is None


@pytest.mark.timeout(900)
@pytest.mark.asyncio
async def test_audio_overview_generation_over_create_artifact(
    android_grpc_cassette: CassetteBinder,
) -> None:
    async with android_grpc_cassette("generate_audio") as (client, values):
        started = await client.artifacts.generate_audio(values.notebook_id)
        status, artifact, after = await _generate_poll_and_delete(
            client, values.notebook_id, started, settle_seconds=20, max_polls=30
        )
    assert started.task_id
    assert status.is_complete, f"audio generation ended as {status.status}"
    assert artifact.id == started.task_id
    assert after is None


# --- #2283 transfer / suggestion family ---------------------------------------


@pytest.mark.asyncio
async def test_next_step_suggestions_and_customization_choices(
    android_grpc_cassette: CassetteBinder,
) -> None:
    async with android_grpc_cassette("next_step_suggestions") as (client, values):
        (source,) = await client.sources.list(values.notebook_id)
        suggestions = await client.notebooks.suggest_next_steps(values.notebook_id)
        scoped = await client.notebooks.suggest_next_steps(
            values.notebook_id, source_ids=[source.id]
        )
        choices = await client.artifacts.get_customization_choices(values.notebook_id)
    for rows in (suggestions, scoped):
        assert rows
        assert all(step.question and type(step.type_code) is int for step in rows)
    for family in (choices.audio, choices.video, choices.slide_deck):
        assert family
        # Replay redaction collapses ints and rewrites text, so pin pairing/shape only.
        assert all(type(item.code) is int and item.code > 0 and item.title for item in family)
    assert choices.reports
    assert all(preset.report_type and preset.directive for preset in choices.reports)


@pytest.mark.timeout(300)
@pytest.mark.asyncio
async def test_source_transfers_over_add_async_append_and_copy(
    android_grpc_cassette: CassetteBinder,
) -> None:
    async with android_grpc_cassette("source_transfers") as (client, values):
        target = await client.notebooks.create(values.texts[0])
        try:
            queued = await client.sources.add_urls_async(values.notebook_id, [values.url])
            await _settle(10)
            ready = await client.sources.wait_until_ready(
                values.notebook_id, queued[0].id, timeout=120, initial_interval=0.1
            )
            (seed,) = [
                source
                for source in await client.sources.list(values.notebook_id)
                if source.id != queued[0].id
            ]
            before = await client.sources.get_fulltext(values.notebook_id, seed.id)
            await client.sources.append_text(
                values.notebook_id, seed.id, values.texts[1], header=values.texts[2]
            )
            await _settle(3)
            after = await client.sources.get_fulltext(values.notebook_id, seed.id)
            copied = await client.sources.copy(values.notebook_id, [seed.id], target.id)
            await client.sources.delete(values.notebook_id, queued[0].id)
        finally:
            await client.notebooks.delete(target.id)
    assert len(queued) == 1 and queued[0].id
    assert ready.id == queued[0].id
    assert len(after.content) > len(before.content)
    assert len(copied) == 1
    assert copied[0].original_id == seed.id
    assert copied[0].source.id and copied[0].source.id != seed.id


@pytest.mark.timeout(600)
@pytest.mark.asyncio
async def test_artifact_copy_over_copy_artifacts_async(
    android_grpc_cassette: CassetteBinder,
) -> None:
    async with android_grpc_cassette("artifact_copy") as (client, values):
        started = await client.artifacts.generate_flashcards(values.notebook_id)
        polls = 0
        while True:
            status = await client.artifacts.poll_status(values.notebook_id, started.task_id)
            polls += 1
            if status.is_complete or status.is_failed or polls >= 16:
                break
            await _settle(15)
        target = await client.notebooks.create(values.texts[0])
        try:
            copied = await client.artifacts.copy(values.notebook_id, [started.task_id], target.id)
        finally:
            await client.notebooks.delete(target.id)
            await client.artifacts.delete(values.notebook_id, started.task_id)
    assert status.is_complete, f"flashcard generation ended as {status.status}"
    assert len(copied) == 1
    assert copied[0].original_id == started.task_id
    assert copied[0].artifact.id and copied[0].artifact.id != started.task_id
