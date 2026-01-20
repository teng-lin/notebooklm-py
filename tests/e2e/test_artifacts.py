"""Artifact CRUD and retrieval tests.

Generation tests are in test_generation.py. This file contains:
- Artifact listing and retrieval
- Artifact mutations (delete, rename)
- Report suggestions
- Status polling
"""

import asyncio

import pytest

from notebooklm import Artifact, ArtifactType, ReportSuggestion

from .conftest import assert_generation_started, requires_auth


@requires_auth
class TestArtifactRetrieval:
    """Tests for artifact retrieval and listing operations."""

    @pytest.mark.asyncio
    @pytest.mark.readonly
    async def test_list_artifacts(self, client, read_only_notebook_id):
        """Read-only test - lists existing artifacts."""
        artifacts = await client.artifacts.list(read_only_notebook_id)
        assert isinstance(artifacts, list)
        assert all(isinstance(art, Artifact) for art in artifacts)

    @pytest.mark.asyncio
    @pytest.mark.readonly
    async def test_get_artifact(self, client, read_only_notebook_id):
        """Test getting a specific artifact by ID."""
        artifacts = await client.artifacts.list(read_only_notebook_id)
        if not artifacts:
            pytest.skip("No artifacts available to get")

        artifact = await client.artifacts.get(read_only_notebook_id, artifacts[0].id)
        assert artifact is not None
        assert isinstance(artifact, Artifact)
        assert artifact.id == artifacts[0].id

    @pytest.mark.asyncio
    @pytest.mark.readonly
    async def test_get_artifact_not_found(self, client, read_only_notebook_id):
        """Test getting a non-existent artifact returns None."""
        artifact = await client.artifacts.get(read_only_notebook_id, "nonexistent_artifact_id")
        assert artifact is None


@requires_auth
class TestArtifactTypeSpecificLists:
    """Tests for type-specific artifact list methods."""

    @pytest.mark.asyncio
    @pytest.mark.readonly
    async def test_list_audio(self, client, read_only_notebook_id):
        """Test listing audio artifacts."""
        artifacts = await client.artifacts.list_audio(read_only_notebook_id)
        assert isinstance(artifacts, list)
        # All returned should be audio type
        for art in artifacts:
            assert art.kind == ArtifactType.AUDIO

    @pytest.mark.asyncio
    @pytest.mark.readonly
    async def test_list_video(self, client, read_only_notebook_id):
        """Test listing video artifacts."""
        artifacts = await client.artifacts.list_video(read_only_notebook_id)
        assert isinstance(artifacts, list)
        # All returned should be video type
        for art in artifacts:
            assert art.kind == ArtifactType.VIDEO

    @pytest.mark.asyncio
    @pytest.mark.readonly
    async def test_list_reports(self, client, read_only_notebook_id):
        """Test listing report artifacts."""
        artifacts = await client.artifacts.list_reports(read_only_notebook_id)
        assert isinstance(artifacts, list)
        # All returned should be report type
        for art in artifacts:
            assert art.kind == ArtifactType.REPORT

    @pytest.mark.asyncio
    @pytest.mark.readonly
    async def test_list_quizzes(self, client, read_only_notebook_id):
        """Test listing quiz artifacts."""
        artifacts = await client.artifacts.list_quizzes(read_only_notebook_id)
        assert isinstance(artifacts, list)
        # All returned should be quizzes
        for art in artifacts:
            assert art.kind == ArtifactType.QUIZ
            assert art.is_quiz is True

    @pytest.mark.asyncio
    @pytest.mark.readonly
    async def test_list_flashcards(self, client, read_only_notebook_id):
        """Test listing flashcard artifacts."""
        artifacts = await client.artifacts.list_flashcards(read_only_notebook_id)
        assert isinstance(artifacts, list)
        # All returned should be flashcards
        for art in artifacts:
            assert art.kind == ArtifactType.FLASHCARDS
            assert art.is_flashcards is True

    @pytest.mark.asyncio
    @pytest.mark.readonly
    async def test_list_infographics(self, client, read_only_notebook_id):
        """Test listing infographic artifacts."""
        artifacts = await client.artifacts.list_infographics(read_only_notebook_id)
        assert isinstance(artifacts, list)
        # All returned should be infographic type
        for art in artifacts:
            assert art.kind == ArtifactType.INFOGRAPHIC

    @pytest.mark.asyncio
    @pytest.mark.readonly
    async def test_list_slide_decks(self, client, read_only_notebook_id):
        """Test listing slide deck artifacts."""
        artifacts = await client.artifacts.list_slide_decks(read_only_notebook_id)
        assert isinstance(artifacts, list)
        # All returned should be slide deck type
        for art in artifacts:
            assert art.kind == ArtifactType.SLIDES

    @pytest.mark.asyncio
    @pytest.mark.readonly
    async def test_list_data_tables(self, client, read_only_notebook_id):
        """Test listing data table artifacts."""
        artifacts = await client.artifacts.list_data_tables(read_only_notebook_id)
        assert isinstance(artifacts, list)
        # All returned should be data table type
        for art in artifacts:
            assert art.kind == ArtifactType.DATA_TABLE


@requires_auth
class TestReportSuggestions:
    """Report suggestion tests."""

    @pytest.mark.asyncio
    @pytest.mark.readonly
    async def test_suggest_reports(self, client, read_only_notebook_id):
        """Read-only test - gets suggestions without generating."""
        suggestions = await client.artifacts.suggest_reports(read_only_notebook_id)

        assert isinstance(suggestions, list)
        if suggestions:
            assert all(isinstance(s, ReportSuggestion) for s in suggestions)
            for s in suggestions:
                assert s.title
                assert s.description
                assert s.prompt


@requires_auth
class TestArtifactMutations:
    """Tests that modify/delete artifacts.

    Combines poll/rename/wait into one test to reuse a single flashcard artifact.
    Delete test uses a separate quiz artifact to spread rate limits.
    """

    @pytest.mark.asyncio
    async def test_poll_rename_wait(self, client, temp_notebook):
        """Test poll_status, rename, and wait_for_completion on ONE artifact.

        Combines three operations into one test to minimize API calls:
        1. Generate one flashcard artifact
        2. Poll its status
        3. Wait for completion
        4. Rename it and rename back

        Uses flashcards (more reliable than quiz for generation).
        """
        # Generate ONE artifact for all operations
        result = await client.artifacts.generate_flashcards(temp_notebook.id)
        assert_generation_started(result, "Flashcard")
        notebook_id = temp_notebook.id
        artifact_id = result.task_id

        # 1. Test poll_status
        await asyncio.sleep(2)
        status = await client.artifacts.poll_status(notebook_id, artifact_id)
        assert status is not None
        assert hasattr(status, "status")

        # 2. Test wait_for_completion
        final_status = await client.artifacts.wait_for_completion(
            notebook_id,
            artifact_id,
            initial_interval=1.0,
            max_interval=5.0,
            timeout=60.0,
        )
        assert final_status is not None
        assert final_status.is_complete or final_status.is_failed

        # 3. Test rename (only if artifact completed successfully)
        if final_status.is_complete:
            artifact = await client.artifacts.get(notebook_id, artifact_id)
            if artifact:
                original_title = artifact.title

                # Rename to new title
                new_title = "Renamed E2E Test"
                await client.artifacts.rename(notebook_id, artifact_id, new_title)

                # Verify rename
                await asyncio.sleep(1)
                renamed = await client.artifacts.get(notebook_id, artifact_id)
                assert renamed is not None
                assert renamed.title == new_title

                # Restore original title
                await client.artifacts.rename(notebook_id, artifact_id, original_title)

    @pytest.mark.asyncio
    async def test_delete_artifact(self, client, temp_notebook):
        """Test deleting an artifact.

        Uses quiz instead of flashcards to spread rate limits across different
        artifact type quotas.
        """
        # Create a quiz artifact for deletion (different type than flashcards)
        result = await client.artifacts.generate_quiz(temp_notebook.id)
        assert_generation_started(result, "Quiz")
        artifact_id = result.task_id

        await asyncio.sleep(2)

        # Delete it
        deleted = await client.artifacts.delete(temp_notebook.id, artifact_id)
        assert deleted is True

        # Verify it's gone
        artifacts = await client.artifacts.list(temp_notebook.id)
        artifact_ids = [a.id for a in artifacts]
        assert artifact_id not in artifact_ids
