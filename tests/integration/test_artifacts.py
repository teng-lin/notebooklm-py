"""Integration tests for ArtifactsAPI."""

import csv
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_httpx import HTTPXMock

from notebooklm import NotebookLMClient
from notebooklm.exceptions import ValidationError
from notebooklm.rpc import (
    AudioFormat,
    AudioLength,
    InfographicStyle,
    RPCError,
    RPCMethod,
    VideoFormat,
    VideoStyle,
)
from notebooklm.types import (
    ArtifactNotReadyError,
    ArtifactParseError,
)


class TestStudioContent:
    @pytest.mark.asyncio
    async def test_generate_audio(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        notebook_response = build_rpc_response(
            RPCMethod.GET_NOTEBOOK,
            [
                [
                    "Test Notebook",
                    [[["src_001"], "Source 1", [None, 0], [None, 2]]],
                    "nb_123",
                    "📘",
                    None,
                    [None, None, None, None, None, [1704067200, 0]],
                ]
            ],
        )
        httpx_mock.add_response(content=notebook_response.encode())

        audio_response = build_rpc_response(
            RPCMethod.CREATE_ARTIFACT, [["artifact_123", "Audio Overview", "2024-01-05", None, 1]]
        )
        httpx_mock.add_response(content=audio_response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.artifacts.generate_audio(notebook_id="nb_123")

        assert result is not None
        assert result.task_id == "artifact_123"
        assert result.status in ("pending", "in_progress")

        request = httpx_mock.get_requests()[-1]
        assert RPCMethod.CREATE_ARTIFACT.value in str(request.url)

    @pytest.mark.asyncio
    async def test_generate_audio_with_format_and_length(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        notebook_response = build_rpc_response(
            RPCMethod.GET_NOTEBOOK,
            [
                [
                    "Test Notebook",
                    [[["src_001"], "Source 1", [None, 0], [None, 2]]],
                    "nb_123",
                    "📘",
                    None,
                    [None, None, None, None, None, [1704067200, 0]],
                ]
            ],
        )
        httpx_mock.add_response(content=notebook_response.encode())

        response = build_rpc_response(
            RPCMethod.CREATE_ARTIFACT, [["artifact_123", "Audio Overview", "2024-01-05", None, 1]]
        )
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.artifacts.generate_audio(
                notebook_id="nb_123",
                audio_format=AudioFormat.DEBATE,
                audio_length=AudioLength.LONG,
            )

        assert result is not None
        assert result.task_id == "artifact_123"

    @pytest.mark.asyncio
    async def test_generate_video_with_format_and_style(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        notebook_response = build_rpc_response(
            RPCMethod.GET_NOTEBOOK,
            [
                [
                    "Test Notebook",
                    [[["source_123"], "Source", [None, 0], [None, 2]]],
                    "nb_123",
                    "📘",
                    None,
                    [None, None, None, None, None, [1704067200, 0]],
                ]
            ],
        )
        video_response = build_rpc_response(
            RPCMethod.CREATE_ARTIFACT, [["artifact_456", "Video Overview", "2024-01-05", None, 1]]
        )
        httpx_mock.add_response(content=notebook_response.encode())
        httpx_mock.add_response(content=video_response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.artifacts.generate_video(
                notebook_id="nb_123",
                video_format=VideoFormat.BRIEF,
                video_style=VideoStyle.ANIME,
            )

        assert result is not None
        assert result.task_id == "artifact_456"

    @pytest.mark.asyncio
    async def test_generate_cinematic_video(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        notebook_response = build_rpc_response(
            RPCMethod.GET_NOTEBOOK,
            [
                [
                    "Test Notebook",
                    [[["source_123"], "Source", [None, 0], [None, 2]]],
                    "nb_123",
                    "📘",
                    None,
                    [None, None, None, None, None, [1704067200, 0]],
                ]
            ],
        )
        cinematic_response = build_rpc_response(
            RPCMethod.CREATE_ARTIFACT,
            [["artifact_789", "Cinematic Video Overview", "2026-03-09", None, 1]],
        )
        httpx_mock.add_response(content=notebook_response.encode())
        httpx_mock.add_response(content=cinematic_response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.artifacts.generate_cinematic_video(
                notebook_id="nb_123",
                instructions="documentary about quantum physics",
            )

        assert result is not None
        assert result.task_id == "artifact_789"

    @pytest.mark.asyncio
    async def test_generate_slide_deck(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        notebook_response = build_rpc_response(
            RPCMethod.GET_NOTEBOOK,
            [
                [
                    "Test Notebook",
                    [[["source_123"], "Source", [None, 0], [None, 2]]],
                    "nb_123",
                    "📘",
                    None,
                    [None, None, None, None, None, [1704067200, 0]],
                ]
            ],
        )
        slide_deck_response = build_rpc_response(
            RPCMethod.CREATE_ARTIFACT, [["artifact_456", "Slide Deck", "2024-01-05", None, 1]]
        )
        httpx_mock.add_response(content=notebook_response.encode())
        httpx_mock.add_response(content=slide_deck_response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.artifacts.generate_slide_deck(notebook_id="nb_123")

        assert result is not None
        assert result.task_id == "artifact_456"

    @pytest.mark.asyncio
    async def test_revise_slide(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test revise_slide calls REVISE_SLIDE RPC with correct params."""
        revise_response = build_rpc_response(
            RPCMethod.REVISE_SLIDE,
            [["artifact_456", "Slide Deck", "2024-01-05", None, 1]],
        )
        httpx_mock.add_response(content=revise_response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.artifacts.revise_slide(
                notebook_id="nb_123",
                artifact_id="artifact_456",
                slide_index=0,
                prompt="Move the title up a bit",
            )

        assert result is not None
        assert result.task_id == "artifact_456"

    @pytest.mark.asyncio
    async def test_poll_studio_status(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        # LIST_ARTIFACTS format: [[artifact1, artifact2, ...]]
        # Use REPORT type (no URL check needed) for simpler test
        artifact = [
            "task_id_123",
            "Briefing Doc",
            2,  # REPORT type (no URL check)
            None,
            3,  # COMPLETED status
        ]
        response = build_rpc_response(RPCMethod.LIST_ARTIFACTS, [[artifact]])
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.artifacts.poll_status(
                notebook_id="nb_123",
                task_id="task_id_123",
            )

        assert result is not None
        assert result.status == "completed"


class TestGenerateQuiz:
    @pytest.mark.asyncio
    async def test_generate_quiz(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        notebook_response = build_rpc_response(
            RPCMethod.GET_NOTEBOOK,
            [
                [
                    "Test Notebook",
                    [[["source_123"], "Source", [None, 0], [None, 2]]],
                    "nb_123",
                    "📘",
                    None,
                    [None, None, None, None, None, [1704067200, 0]],
                ]
            ],
        )
        quiz_response = build_rpc_response(
            RPCMethod.CREATE_ARTIFACT, [["quiz_123", "Quiz", "2024-01-05", None, 1]]
        )
        httpx_mock.add_response(content=notebook_response.encode())
        httpx_mock.add_response(content=quiz_response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.artifacts.generate_quiz("nb_123")

        assert result is not None
        assert result.task_id == "quiz_123"


class TestDeleteStudioContent:
    @pytest.mark.asyncio
    async def test_delete_studio_content(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        response = build_rpc_response(RPCMethod.DELETE_ARTIFACT, [True])
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.artifacts.delete("nb_123", "task_id_123")

        assert result is True


class TestMindMap:
    @pytest.mark.asyncio
    async def test_generate_mind_map(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        notebook_response = build_rpc_response(
            RPCMethod.GET_NOTEBOOK,
            [
                [
                    "Test Notebook",
                    [[["source_123"], "Source", [None, 0], [None, 2]]],
                    "nb_123",
                    "📘",
                    None,
                    [None, None, None, None, None, [1704067200, 0]],
                ]
            ],
        )
        mindmap_response = build_rpc_response(RPCMethod.GENERATE_MIND_MAP, None)
        httpx_mock.add_response(content=notebook_response.encode())
        httpx_mock.add_response(content=mindmap_response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.artifacts.generate_mind_map("nb_123")

        # Mind map returns dict or None
        assert result is None or isinstance(result, dict)


class TestArtifactsAPI:
    """Integration tests for ArtifactsAPI methods."""

    @pytest.mark.asyncio
    async def test_list_artifacts(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test listing all artifacts."""
        # Response for LIST_ARTIFACTS (gArtLc)
        response1 = build_rpc_response(
            RPCMethod.LIST_ARTIFACTS,
            [
                ["art_001", "Audio Overview", 1, None, "completed"],
                ["art_002", "Quiz", 4, None, "completed"],
                ["art_003", "Study Guide", 2, None, "completed"],
            ],
        )
        # Response for GET_NOTES_AND_MIND_MAPS (cFji9) - empty (no mind maps)
        response2 = build_rpc_response(RPCMethod.GET_NOTES_AND_MIND_MAPS, [[]])
        httpx_mock.add_response(content=response1.encode())
        httpx_mock.add_response(content=response2.encode())

        async with NotebookLMClient(auth_tokens) as client:
            artifacts = await client.artifacts.list("nb_123")

        assert isinstance(artifacts, list)

    @pytest.mark.asyncio
    async def test_rename_artifact(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test renaming an artifact."""
        response = build_rpc_response(RPCMethod.RENAME_ARTIFACT, None)
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            await client.artifacts.rename("nb_123", "art_001", "New Title")

        request = httpx_mock.get_request()
        assert RPCMethod.RENAME_ARTIFACT.value in str(request.url)

    @pytest.mark.asyncio
    async def test_export_artifact(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test exporting an artifact."""
        response = build_rpc_response(RPCMethod.EXPORT_ARTIFACT, ["export_content_here"])
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.artifacts.export("nb_123", "art_001")

        assert result is not None
        request = httpx_mock.get_request()
        assert RPCMethod.EXPORT_ARTIFACT.value in str(request.url)

    @pytest.mark.asyncio
    async def test_generate_flashcards(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test generating flashcards."""
        notebook_response = build_rpc_response(
            RPCMethod.GET_NOTEBOOK,
            [
                [
                    "Test Notebook",
                    [[["source_123"], "Source", [None, 0], [None, 2]]],
                    "nb_123",
                    "📘",
                    None,
                    [None, None, None, None, None, [1704067200, 0]],
                ]
            ],
        )
        flashcards_response = build_rpc_response(
            RPCMethod.CREATE_ARTIFACT, [["fc_123", "Flashcards", "2024-01-05", None, 1]]
        )
        httpx_mock.add_response(content=notebook_response.encode())
        httpx_mock.add_response(content=flashcards_response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.artifacts.generate_flashcards("nb_123")

        assert result is not None
        assert result.task_id == "fc_123"

    @pytest.mark.asyncio
    async def test_generate_study_guide(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test generating study guide."""
        notebook_response = build_rpc_response(
            RPCMethod.GET_NOTEBOOK,
            [
                [
                    "Test Notebook",
                    [[["source_123"], "Source", [None, 0], [None, 2]]],
                    "nb_123",
                    "📘",
                    None,
                    [None, None, None, None, None, [1704067200, 0]],
                ]
            ],
        )
        guide_response = build_rpc_response(
            RPCMethod.CREATE_ARTIFACT, [["sg_123", "Study Guide", "2024-01-05", None, 1]]
        )
        httpx_mock.add_response(content=notebook_response.encode())
        httpx_mock.add_response(content=guide_response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.artifacts.generate_study_guide("nb_123")

        assert result is not None
        assert result.task_id == "sg_123"

    @pytest.mark.asyncio
    async def test_generate_infographic(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test generating infographic."""
        notebook_response = build_rpc_response(
            RPCMethod.GET_NOTEBOOK,
            [
                [
                    "Test Notebook",
                    [[["source_123"], "Source", [None, 0], [None, 2]]],
                    "nb_123",
                    "📘",
                    None,
                    [None, None, None, None, None, [1704067200, 0]],
                ]
            ],
        )
        infographic_response = build_rpc_response(
            RPCMethod.CREATE_ARTIFACT, [["ig_123", "Infographic", "2024-01-05", None, 1]]
        )
        httpx_mock.add_response(content=notebook_response.encode())
        httpx_mock.add_response(content=infographic_response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.artifacts.generate_infographic("nb_123")

        assert result is not None
        assert result.task_id == "ig_123"

    @pytest.mark.asyncio
    async def test_generate_infographic_with_style(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test generating infographic with a specific visual style."""
        notebook_response = build_rpc_response(
            RPCMethod.GET_NOTEBOOK,
            [
                [
                    "Test Notebook",
                    [[["source_123"], "Source", [None, 0], [None, 2]]],
                    "nb_123",
                    "📘",
                    None,
                    [None, None, None, None, None, [1704067200, 0]],
                ]
            ],
        )
        infographic_response = build_rpc_response(
            RPCMethod.CREATE_ARTIFACT, [["ig_456", "Infographic", "2024-01-05", None, 1]]
        )
        httpx_mock.add_response(content=notebook_response.encode())
        httpx_mock.add_response(content=infographic_response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.artifacts.generate_infographic(
                "nb_123",
                style=InfographicStyle.PROFESSIONAL,
            )

        assert result is not None
        assert result.task_id == "ig_456"

    @pytest.mark.asyncio
    async def test_generate_data_table(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test generating data table."""
        notebook_response = build_rpc_response(
            RPCMethod.GET_NOTEBOOK,
            [
                [
                    "Test Notebook",
                    [[["source_123"], "Source", [None, 0], [None, 2]]],
                    "nb_123",
                    "📘",
                    None,
                    [None, None, None, None, None, [1704067200, 0]],
                ]
            ],
        )
        table_response = build_rpc_response(
            RPCMethod.CREATE_ARTIFACT, [["dt_123", "Data Table", "2024-01-05", None, 1]]
        )
        httpx_mock.add_response(content=notebook_response.encode())
        httpx_mock.add_response(content=table_response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.artifacts.generate_data_table("nb_123")

        assert result is not None
        assert result.task_id == "dt_123"

    @pytest.mark.asyncio
    async def test_get_artifact_not_found(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test getting a non-existent artifact returns None."""
        # Response for LIST_ARTIFACTS (gArtLc) - empty
        response1 = build_rpc_response(RPCMethod.LIST_ARTIFACTS, [])
        # Response for GET_NOTES_AND_MIND_MAPS (cFji9) - empty
        response2 = build_rpc_response(RPCMethod.GET_NOTES_AND_MIND_MAPS, [[]])
        httpx_mock.add_response(content=response1.encode())
        httpx_mock.add_response(content=response2.encode())

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.artifacts.get("nb_123", "nonexistent")

        assert result is None

    @pytest.mark.asyncio
    async def test_list_audio_artifacts(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test listing audio artifacts."""
        response = build_rpc_response(
            RPCMethod.LIST_ARTIFACTS,
            [
                ["art_001", "Audio Overview", 1, None, 3],
                ["art_002", "Quiz", 4, None, 3],
            ],
        )
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            artifacts = await client.artifacts.list_audio("nb_123")

        assert isinstance(artifacts, list)

    @pytest.mark.asyncio
    async def test_list_video_artifacts(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test listing video artifacts."""
        response = build_rpc_response(
            RPCMethod.LIST_ARTIFACTS,
            [
                ["art_001", "Video Overview", 3, None, 3],
                ["art_002", "Audio Overview", 1, None, 3],
            ],
        )
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            artifacts = await client.artifacts.list_video("nb_123")

        assert isinstance(artifacts, list)

    @pytest.mark.asyncio
    async def test_list_quiz_artifacts(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test listing quiz artifacts (list_quizzes)."""
        response = build_rpc_response(
            RPCMethod.LIST_ARTIFACTS,
            [
                ["art_001", "Quiz", 4, None, 3, None, [None, None, None, None, None, None, 2]],
                [
                    "art_002",
                    "Flashcards",
                    4,
                    None,
                    3,
                    None,
                    [None, None, None, None, None, None, 1],
                ],
            ],
        )
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            artifacts = await client.artifacts.list_quizzes("nb_123")

        assert isinstance(artifacts, list)

    @pytest.mark.asyncio
    async def test_delete_artifact(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test deleting an artifact."""
        response = build_rpc_response(RPCMethod.DELETE_ARTIFACT, None)
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.artifacts.delete("nb_123", "art_001")

        assert result is True
        request = httpx_mock.get_request()
        assert RPCMethod.DELETE_ARTIFACT in str(request.url)

    @pytest.mark.asyncio
    async def test_list_flashcards(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test listing flashcard artifacts."""
        response = build_rpc_response(
            RPCMethod.LIST_ARTIFACTS,
            [
                ["art_001", "Quiz", 4, None, 3, None, [None, None, None, None, None, None, 2]],
                [
                    "art_002",
                    "Flashcards",
                    4,
                    None,
                    3,
                    None,
                    [None, None, None, None, None, None, 1],
                ],
            ],
        )
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            artifacts = await client.artifacts.list_flashcards("nb_123")

        assert isinstance(artifacts, list)

    @pytest.mark.asyncio
    async def test_list_infographics(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test listing infographic artifacts."""
        response = build_rpc_response(
            RPCMethod.LIST_ARTIFACTS,
            [
                ["art_001", "Infographic", 7, None, 3],
                ["art_002", "Audio", 1, None, 3],
            ],
        )
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            artifacts = await client.artifacts.list_infographics("nb_123")

        assert isinstance(artifacts, list)

    @pytest.mark.asyncio
    async def test_list_slide_decks(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test listing slide deck artifacts."""
        response = build_rpc_response(
            RPCMethod.LIST_ARTIFACTS,
            [
                ["art_001", "Slide Deck", 8, None, 3],
                ["art_002", "Video", 3, None, 3],
            ],
        )
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            artifacts = await client.artifacts.list_slide_decks("nb_123")

        assert isinstance(artifacts, list)


class TestArtifactErrorPaths:
    """Test error handling paths in ArtifactsAPI."""

    @pytest.mark.asyncio
    async def test_download_audio_no_completed_audio(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test download_audio raises error when no completed audio exists."""
        # LIST_ARTIFACTS returns empty (no audio artifacts)
        response = build_rpc_response(RPCMethod.LIST_ARTIFACTS, [[]])
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            with pytest.raises(ArtifactNotReadyError):
                await client.artifacts.download_audio("nb_123", "/tmp/audio.mp4")

    @pytest.mark.asyncio
    async def test_download_audio_artifact_id_not_found(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test download_audio raises error when specific artifact_id not found."""
        # Return an audio artifact but not the one requested
        response = build_rpc_response(
            RPCMethod.LIST_ARTIFACTS,
            [
                [
                    ["other_audio_id", "Audio", 1, None, 3, None, []],
                ]
            ],
        )
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            with pytest.raises(ArtifactNotReadyError):
                await client.artifacts.download_audio(
                    "nb_123", "/tmp/audio.mp4", artifact_id="nonexistent_id"
                )

    @pytest.mark.asyncio
    async def test_download_video_no_completed_video(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test download_video raises error when no completed video exists."""
        response = build_rpc_response(RPCMethod.LIST_ARTIFACTS, [[]])
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            with pytest.raises(ArtifactNotReadyError):
                await client.artifacts.download_video("nb_123", "/tmp/video.mp4")

    @pytest.mark.asyncio
    async def test_download_infographic_no_completed(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test download_infographic raises error when none completed."""
        response = build_rpc_response(RPCMethod.LIST_ARTIFACTS, [[]])
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            with pytest.raises(ArtifactNotReadyError):
                await client.artifacts.download_infographic("nb_123", "/tmp/infographic.png")

    @pytest.mark.asyncio
    async def test_download_slide_deck_no_completed(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test download_slide_deck raises error when none completed."""
        response = build_rpc_response(RPCMethod.LIST_ARTIFACTS, [[]])
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            with pytest.raises(ArtifactNotReadyError):
                await client.artifacts.download_slide_deck("nb_123", "/tmp/slides")

    @pytest.mark.asyncio
    async def test_download_slide_deck_pptx(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
        tmp_path,
    ):
        """Test download_slide_deck with format='pptx' downloads PPTX URL."""
        pdf_url = "https://docs.googleusercontent.com/slides.pdf"
        pptx_url = "https://docs.googleusercontent.com/slides.pptx"
        slide_art = [
            "artifact_456",
            "Slide Deck",
            8,
            None,
            3,  # COMPLETED
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            [None, "Title", [], pdf_url, pptx_url],  # art[16] with PPTX at [4]
        ]
        response = build_rpc_response(RPCMethod.LIST_ARTIFACTS, [[slide_art]])
        httpx_mock.add_response(content=response.encode())
        httpx_mock.add_response(content=b"pptx-content")

        output = str(tmp_path / "slides.pptx")
        with patch("notebooklm._artifacts.load_httpx_cookies", return_value=MagicMock()):
            async with NotebookLMClient(auth_tokens) as client:
                result = await client.artifacts.download_slide_deck(
                    "nb_123", output, output_format="pptx"
                )
        assert result == output

    @pytest.mark.asyncio
    async def test_poll_status_in_progress(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test poll_status returns in_progress for processing artifacts."""
        # LIST_ARTIFACTS format: [[artifact1, artifact2, ...]]
        artifact = [
            "task_id_123",
            "Report",
            2,  # REPORT type
            None,
            1,  # PROCESSING status
        ]
        response = build_rpc_response(RPCMethod.LIST_ARTIFACTS, [[artifact]])
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.artifacts.poll_status(
                notebook_id="nb_123",
                task_id="task_id_123",
            )

        assert result is not None
        assert result.status == "in_progress"

    @pytest.mark.asyncio
    async def test_poll_status_failed(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test poll_status returns failed status."""
        # LIST_ARTIFACTS format: [[artifact1, artifact2, ...]]
        artifact = [
            "task_id_123",
            "Report",
            2,  # REPORT type
            None,
            4,  # FAILED status
        ]
        response = build_rpc_response(RPCMethod.LIST_ARTIFACTS, [[artifact]])
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.artifacts.poll_status(
                notebook_id="nb_123",
                task_id="task_id_123",
            )

        assert result is not None
        assert result.status == "failed"

    @pytest.mark.asyncio
    async def test_rpc_error_http_500(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
    ):
        """Test RPC error handling for HTTP 500.

        Uses ``server_error_max_retries=0`` to exercise the immediate-raise
        path; T3.A adds bounded exponential-backoff retries for 5xx by
        default, but the error-shape contract on exhaustion is what this test
        pins down.
        """
        httpx_mock.add_response(status_code=500)

        async with NotebookLMClient(auth_tokens, server_error_max_retries=0) as client:
            with pytest.raises(RPCError, match="Server error 500"):
                await client.artifacts.list("nb_123")

    @pytest.mark.asyncio
    async def test_list_empty_result(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test listing artifacts when notebook has none."""
        # Response for LIST_ARTIFACTS (gArtLc) - empty
        response1 = build_rpc_response(RPCMethod.LIST_ARTIFACTS, [[]])
        # Response for GET_NOTES_AND_MIND_MAPS (cFji9) - empty
        response2 = build_rpc_response(RPCMethod.GET_NOTES_AND_MIND_MAPS, [[]])
        httpx_mock.add_response(content=response1.encode())
        httpx_mock.add_response(content=response2.encode())

        async with NotebookLMClient(auth_tokens) as client:
            artifacts = await client.artifacts.list("nb_123")

        assert artifacts == []


class TestDownloadReport:
    """Integration tests for download_report method."""

    @pytest.mark.asyncio
    async def test_download_report_success(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
        tmp_path,
    ):
        """Test successful report download."""
        # Mock _list_raw response - type 2 (report), status 3 (completed)
        # Data needs to be [[artifact1], [artifact2], ...] because _list_raw does result[0]
        response = build_rpc_response(
            RPCMethod.LIST_ARTIFACTS,
            [
                [
                    [
                        "report_001",
                        "Study Guide",
                        2,  # type (report)
                        None,
                        3,  # status (completed)
                        None,
                        None,
                        ["# Test Report\n\nThis is markdown content."],  # content at index 7
                    ]
                ]
            ],
        )
        httpx_mock.add_response(content=response.encode())

        output_path = tmp_path / "report.md"
        async with NotebookLMClient(auth_tokens) as client:
            result = await client.artifacts.download_report("nb_123", str(output_path))

        assert result == str(output_path)
        assert output_path.exists()
        content = output_path.read_text()
        assert "# Test Report" in content

    @pytest.mark.asyncio
    async def test_download_report_not_found(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test error when no report exists."""
        response = build_rpc_response(RPCMethod.LIST_ARTIFACTS, [])
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            with pytest.raises(ArtifactNotReadyError):
                await client.artifacts.download_report("nb_123", "/tmp/report.md")


class TestDownloadMindMap:
    """Integration tests for download_mind_map method."""

    @pytest.mark.asyncio
    async def test_download_mind_map_success(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
        tmp_path,
    ):
        """Test successful mind map download."""
        # Mock notes API response for mind maps
        response = build_rpc_response(
            RPCMethod.GET_NOTES_AND_MIND_MAPS,
            [
                [
                    [
                        "mindmap_001",
                        [None, '{"name": "Root", "children": []}'],
                        None,
                        None,
                        "Mind Map Title",
                    ]
                ]
            ],
        )
        httpx_mock.add_response(content=response.encode())

        output_path = tmp_path / "mindmap.json"
        async with NotebookLMClient(auth_tokens) as client:
            result = await client.artifacts.download_mind_map("nb_123", str(output_path))

        assert result == str(output_path)
        assert output_path.exists()
        data = json.loads(output_path.read_text())
        assert data["name"] == "Root"

    @pytest.mark.asyncio
    async def test_download_mind_map_not_found(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test error when no mind map exists."""
        response = build_rpc_response(RPCMethod.GET_NOTES_AND_MIND_MAPS, [[]])
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            with pytest.raises(ArtifactNotReadyError):
                await client.artifacts.download_mind_map("nb_123", "/tmp/mindmap.json")


class TestDownloadDataTable:
    """Integration tests for download_data_table method."""

    @pytest.mark.asyncio
    async def test_download_data_table_success(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
        tmp_path,
    ):
        """Test successful data table download."""
        # Build complex nested structure for data table
        rows_data = [
            [
                0,
                20,
                [
                    [0, 5, [[0, 5, [[0, 5, [["Col1"]]]]]]],
                    [5, 10, [[5, 10, [[5, 10, [["Col2"]]]]]]],
                ],
            ],
            [
                20,
                40,
                [
                    [20, 25, [[20, 25, [[20, 25, [["A"]]]]]]],
                    [25, 30, [[25, 30, [[25, 30, [["B"]]]]]]],
                ],
            ],
        ]
        data_table_structure = [[[[[0, 100, None, None, [6, 7, rows_data]]]]]]

        artifact = ["table_001", "Data Table", 9, None, 3]
        artifact.extend([None] * 13)  # Pad to index 18
        artifact.append(data_table_structure)

        # Data needs to be [[artifact1]] because _list_raw does result[0]
        response = build_rpc_response(RPCMethod.LIST_ARTIFACTS, [[artifact]])
        httpx_mock.add_response(content=response.encode())

        output_path = tmp_path / "data.csv"
        async with NotebookLMClient(auth_tokens) as client:
            result = await client.artifacts.download_data_table("nb_123", str(output_path))

        assert result == str(output_path)
        assert output_path.exists()
        with open(output_path, encoding="utf-8-sig") as f:
            rows = list(csv.reader(f))
        assert rows[0] == ["Col1", "Col2"]
        assert rows[1] == ["A", "B"]

    @pytest.mark.asyncio
    async def test_download_data_table_not_found(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test error when no data table exists."""
        response = build_rpc_response(RPCMethod.LIST_ARTIFACTS, [])
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            with pytest.raises(ArtifactNotReadyError):
                await client.artifacts.download_data_table("nb_123", "/tmp/data.csv")


# =============================================================================
# New tests for uncovered lines
# =============================================================================


class TestExtractAppData:
    """Tests for _extract_app_data error path (line 78)."""

    @pytest.mark.asyncio
    async def test_download_quiz_html_without_app_data_attribute(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
        tmp_path,
    ):
        """download_quiz raises ArtifactParseError when HTML lacks data-app-data attribute."""
        # Build a completed quiz artifact - variant at data[9][1][0] = 2 for quiz
        # (variant=1 is flashcards, variant=2 is quiz)
        artifact_data = [
            "quiz_001",  # [0] id
            "My Quiz",  # [1] title
            4,  # [2] QUIZ type
            None,  # [3]
            3,  # [4] COMPLETED
            None,  # [5]
            None,  # [6]
            None,  # [7]
            None,  # [8]
            [None, [2]],  # [9] options: [9][1][0] = 2 => quiz variant
            None,  # [10]
            None,  # [11]
            None,  # [12]
            None,  # [13]
            None,  # [14]
            [[1704067200]],  # [15] created_at
        ]
        list_response = build_rpc_response(RPCMethod.LIST_ARTIFACTS, [[artifact_data]])
        httpx_mock.add_response(content=list_response.encode())

        # HTML that has NO data-app-data attribute
        html_without_data = "<html><body><div>No app data here</div></body></html>"

        output_path = str(tmp_path / "quiz.json")
        async with NotebookLMClient(auth_tokens) as client:
            with patch.object(
                client.artifacts,
                "_get_artifact_content",
                AsyncMock(return_value=html_without_data),
            ):
                with pytest.raises(ArtifactParseError, match="data-app-data"):
                    await client.artifacts.download_quiz("nb_123", output_path)


class TestListMindMapErrorHandling:
    """Tests for list() mind map error handling (lines 288-295)."""

    @pytest.mark.asyncio
    async def test_list_continues_when_mind_map_rpc_error(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """list() returns studio artifacts when mind map fetch raises RPCError."""
        # Return a report artifact from studio
        artifact_data = ["art_001", "My Report", 2, None, 3]
        list_response = build_rpc_response(RPCMethod.LIST_ARTIFACTS, [[artifact_data]])
        httpx_mock.add_response(content=list_response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            with patch.object(
                client.artifacts._notes,
                "list_mind_maps",
                AsyncMock(side_effect=RPCError("mind map fetch failed")),
            ):
                result = await client.artifacts.list("nb_123")

        # Should still return the studio artifact despite mind map failure
        assert isinstance(result, list)
        assert len(result) >= 1
        assert any(a.id == "art_001" for a in result)

    @pytest.mark.asyncio
    async def test_list_continues_when_mind_map_http_error(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """list() returns studio artifacts when mind map fetch raises HTTPError."""
        import httpx

        artifact_data = ["art_002", "Audio Overview", 1, None, 3]
        list_response = build_rpc_response(RPCMethod.LIST_ARTIFACTS, [[artifact_data]])
        httpx_mock.add_response(content=list_response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            with patch.object(
                client.artifacts._notes,
                "list_mind_maps",
                AsyncMock(side_effect=httpx.HTTPError("connection failed")),
            ):
                result = await client.artifacts.list("nb_123")

        assert isinstance(result, list)
        assert len(result) >= 1
        assert any(a.id == "art_002" for a in result)


class TestGetArtifactReturnsNone:
    """Tests for get() returning None (lines 312-313)."""

    @pytest.mark.asyncio
    async def test_get_returns_none_when_not_found(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """get() returns None when the artifact_id is not in the list."""
        # Return one artifact with a different ID
        artifact_data = ["art_exists", "My Report", 2, None, 3]
        list_response = build_rpc_response(RPCMethod.LIST_ARTIFACTS, [[artifact_data]])
        notes_response = build_rpc_response(RPCMethod.GET_NOTES_AND_MIND_MAPS, [[]])
        httpx_mock.add_response(content=list_response.encode())
        httpx_mock.add_response(content=notes_response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.artifacts.get("nb_123", "art_does_not_exist")

        assert result is None

    @pytest.mark.asyncio
    async def test_get_returns_artifact_when_found(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """get() returns the artifact when found by ID."""
        artifact_data = ["art_found", "My Report", 2, None, 3]
        list_response = build_rpc_response(RPCMethod.LIST_ARTIFACTS, [[artifact_data]])
        notes_response = build_rpc_response(RPCMethod.GET_NOTES_AND_MIND_MAPS, [[]])
        httpx_mock.add_response(content=list_response.encode())
        httpx_mock.add_response(content=notes_response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.artifacts.get("nb_123", "art_found")

        assert result is not None
        assert result.id == "art_found"


class TestReviseSlide:
    """Tests for revise_slide() paths (lines 836, 851, 853-861)."""

    @pytest.mark.asyncio
    async def test_revise_slide_negative_index_raises_validation_error(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """revise_slide raises ValidationError for slide_index < 0."""
        async with NotebookLMClient(auth_tokens) as client:
            with pytest.raises(ValidationError, match="slide_index must be >= 0"):
                await client.artifacts.revise_slide(
                    notebook_id="nb_123",
                    artifact_id="artifact_456",
                    slide_index=-1,
                    prompt="Move title up",
                )

    @pytest.mark.asyncio
    async def test_revise_slide_null_result_returns_generation_status(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """revise_slide logs warning and returns GenerationStatus when RPC returns null."""
        # Build a null response (allow_null=True path)
        null_response = build_rpc_response(RPCMethod.REVISE_SLIDE, None)
        httpx_mock.add_response(content=null_response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.artifacts.revise_slide(
                notebook_id="nb_123",
                artifact_id="artifact_456",
                slide_index=0,
                prompt="Remove taxonomy section",
            )

        assert result is not None
        assert result.status == "failed"

    @pytest.mark.asyncio
    async def test_revise_slide_user_displayable_error_returns_failed_status(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """revise_slide returns failed GenerationStatus on USER_DISPLAYABLE_ERROR."""
        async with NotebookLMClient(auth_tokens) as client:
            err = RPCError("Rate limit exceeded")
            err.rpc_code = "USER_DISPLAYABLE_ERROR"
            with patch.object(
                client.artifacts._core,
                "rpc_call",
                AsyncMock(side_effect=err),
            ):
                result = await client.artifacts.revise_slide(
                    notebook_id="nb_123",
                    artifact_id="artifact_456",
                    slide_index=2,
                    prompt="Make it simpler",
                )

        assert result is not None
        assert result.status == "failed"
        assert result.error_code == "USER_DISPLAYABLE_ERROR"

    @pytest.mark.asyncio
    async def test_revise_slide_other_rpc_error_reraises(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """revise_slide re-raises non-USER_DISPLAYABLE_ERROR RPCErrors."""
        async with NotebookLMClient(auth_tokens) as client:
            err = RPCError("Internal error")
            err.rpc_code = "INTERNAL_ERROR"
            with (
                patch.object(
                    client.artifacts._core,
                    "rpc_call",
                    AsyncMock(side_effect=err),
                ),
                pytest.raises(RPCError),
            ):
                await client.artifacts.revise_slide(
                    notebook_id="nb_123",
                    artifact_id="artifact_456",
                    slide_index=0,
                    prompt="Fix this",
                )


class TestGenerateMindMapParsing:
    """Tests for generate_mind_map() result parsing (lines 957-986)."""

    @pytest.mark.asyncio
    async def test_generate_mind_map_returns_none_on_empty_result(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """generate_mind_map returns {'mind_map': None, 'note_id': None} when RPC returns None."""
        notebook_response = build_rpc_response(
            RPCMethod.GET_NOTEBOOK,
            [
                [
                    "Test Notebook",
                    [[["src_001"], "Source 1", [None, 0], [None, 2]]],
                    "nb_123",
                    None,
                    None,
                    [None, None, None, None, None, [1704067200, 0]],
                ]
            ],
        )
        mindmap_response = build_rpc_response(RPCMethod.GENERATE_MIND_MAP, None)
        httpx_mock.add_response(content=notebook_response.encode())
        httpx_mock.add_response(content=mindmap_response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.artifacts.generate_mind_map("nb_123")

        assert result == {"mind_map": None, "note_id": None}

    @pytest.mark.asyncio
    async def test_generate_mind_map_parses_json_string(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """generate_mind_map parses JSON string in RPC result and creates a note."""
        mind_map_dict = {"name": "Root Topic", "children": [{"name": "Child 1"}]}
        mind_map_json_str = json.dumps(mind_map_dict)

        notebook_response = build_rpc_response(
            RPCMethod.GET_NOTEBOOK,
            [
                [
                    "Test Notebook",
                    [[["src_001"], "Source 1", [None, 0], [None, 2]]],
                    "nb_123",
                    None,
                    None,
                    [None, None, None, None, None, [1704067200, 0]],
                ]
            ],
        )
        # RPC response: [[json_string]]
        mindmap_response = build_rpc_response(RPCMethod.GENERATE_MIND_MAP, [[mind_map_json_str]])
        httpx_mock.add_response(content=notebook_response.encode())
        httpx_mock.add_response(content=mindmap_response.encode())

        mock_note = MagicMock()
        mock_note.id = "note_created_001"

        async with NotebookLMClient(auth_tokens) as client:
            with patch.object(
                client.artifacts._notes,
                "create",
                AsyncMock(return_value=mock_note),
            ):
                result = await client.artifacts.generate_mind_map("nb_123")

        assert result["mind_map"] == mind_map_dict
        assert result["note_id"] == "note_created_001"

    @pytest.mark.asyncio
    async def test_generate_mind_map_handles_dict_not_string(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """generate_mind_map handles case where mind_map_json is already a dict."""
        mind_map_dict = {"name": "Topic", "children": []}

        notebook_response = build_rpc_response(
            RPCMethod.GET_NOTEBOOK,
            [
                [
                    "Test Notebook",
                    [[["src_001"], "Source 1", [None, 0], [None, 2]]],
                    "nb_123",
                    None,
                    None,
                    [None, None, None, None, None, [1704067200, 0]],
                ]
            ],
        )
        # RPC response: [[dict_object]] — not a string, already parsed
        mindmap_response = build_rpc_response(RPCMethod.GENERATE_MIND_MAP, [[mind_map_dict]])
        httpx_mock.add_response(content=notebook_response.encode())
        httpx_mock.add_response(content=mindmap_response.encode())

        mock_note = MagicMock()
        mock_note.id = "note_dict_001"

        async with NotebookLMClient(auth_tokens) as client:
            with patch.object(
                client.artifacts._notes,
                "create",
                AsyncMock(return_value=mock_note),
            ):
                result = await client.artifacts.generate_mind_map("nb_123")

        assert result["mind_map"] == mind_map_dict
        assert result["note_id"] == "note_dict_001"

    @pytest.mark.asyncio
    async def test_generate_mind_map_with_source_ids_none_fetches_sources(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """generate_mind_map with source_ids=None fetches source IDs from GET_NOTEBOOK."""

        notebook_response = build_rpc_response(
            RPCMethod.GET_NOTEBOOK,
            [
                [
                    "Test Notebook",
                    [[["src_001"], "Source 1", [None, 0], [None, 2]]],
                    "nb_123",
                    None,
                    None,
                    [None, None, None, None, None, [1704067200, 0]],
                ]
            ],
        )
        mindmap_response = build_rpc_response(RPCMethod.GENERATE_MIND_MAP, None)
        httpx_mock.add_response(content=notebook_response.encode())
        httpx_mock.add_response(content=mindmap_response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.artifacts.generate_mind_map("nb_123", source_ids=None)

        assert result == {"mind_map": None, "note_id": None}


class TestDownloadAudioErrorPaths:
    """Tests for download_audio error paths (lines 1098-1147, 1177-1179)."""

    @pytest.mark.asyncio
    async def test_download_audio_empty_list_raises_not_ready(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """download_audio raises ArtifactNotReadyError when no audio artifacts found."""
        response = build_rpc_response(RPCMethod.LIST_ARTIFACTS, [[]])
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            with pytest.raises(ArtifactNotReadyError):
                await client.artifacts.download_audio("nb_123", "/tmp/audio.mp4")

    @pytest.mark.asyncio
    async def test_download_audio_artifact_id_not_found_raises_not_ready(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """download_audio raises ArtifactNotReadyError when specified artifact_id not found."""
        # Completed audio artifact but with a different ID
        audio_art = [
            "other_audio_001",
            "Audio Overview",
            1,  # AUDIO type
            None,
            3,  # COMPLETED
            None,
            [None, None, None, None, None, [[]]],  # metadata at [6]
        ]
        response = build_rpc_response(RPCMethod.LIST_ARTIFACTS, [[audio_art]])
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            with pytest.raises(ArtifactNotReadyError):
                await client.artifacts.download_audio(
                    "nb_123", "/tmp/audio.mp4", artifact_id="requested_but_missing"
                )

    @pytest.mark.asyncio
    async def test_download_audio_invalid_metadata_structure_raises_parse_error(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """download_audio raises ArtifactParseError when metadata[6] has wrong structure."""
        # Audio artifact with art[6] being a short list (len <= 5)
        audio_art = [
            "audio_001",
            "Audio Overview",
            1,  # AUDIO type
            None,
            3,  # COMPLETED
            None,
            [None, None],  # art[6] — too short, len <= 5
        ]
        response = build_rpc_response(RPCMethod.LIST_ARTIFACTS, [[audio_art]])
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            with pytest.raises(ArtifactParseError, match="Could not extract download URL"):
                await client.artifacts.download_audio("nb_123", "/tmp/audio.mp4")

    @pytest.mark.asyncio
    async def test_download_audio_no_media_urls_raises_parse_error(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """download_audio raises ArtifactParseError when media_list is empty."""
        # Audio artifact with art[6][5] being an empty list
        audio_art = [
            "audio_001",
            "Audio Overview",
            1,  # AUDIO type
            None,
            3,  # COMPLETED
            None,
            [None, None, None, None, None, []],  # art[6], art[6][5] = []
        ]
        response = build_rpc_response(RPCMethod.LIST_ARTIFACTS, [[audio_art]])
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            with pytest.raises(ArtifactParseError, match="Could not extract download URL"):
                await client.artifacts.download_audio("nb_123", "/tmp/audio.mp4")

    @pytest.mark.asyncio
    async def test_download_audio_index_error_raises_parse_error(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """download_audio wraps IndexError/TypeError in ArtifactParseError."""
        # art[6] is a string, not a list — will cause TypeError
        audio_art = [
            "audio_001",
            "Audio Overview",
            1,  # AUDIO type
            None,
            3,  # COMPLETED
            None,
            "not_a_list",  # art[6] is wrong type, causes TypeError
        ]
        response = build_rpc_response(RPCMethod.LIST_ARTIFACTS, [[audio_art]])
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            with pytest.raises(ArtifactParseError, match="Failed to parse audio artifact"):
                await client.artifacts.download_audio("nb_123", "/tmp/audio.mp4")


class TestGenerateMindMapSourceIdsNone:
    """Tests for generate methods with source_ids=None (lines 1189-1211)."""

    @pytest.mark.asyncio
    async def test_generate_audio_source_ids_none_fetches_sources(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """generate_audio with source_ids=None calls GET_NOTEBOOK to fetch source IDs."""
        notebook_response = build_rpc_response(
            RPCMethod.GET_NOTEBOOK,
            [
                [
                    "Test Notebook",
                    [[["src_abc"], "Source A", [None, 0], [None, 2]]],
                    "nb_123",
                    None,
                    None,
                    [None, None, None, None, None, [1704067200, 0]],
                ]
            ],
        )
        audio_response = build_rpc_response(
            RPCMethod.CREATE_ARTIFACT, [["audio_new", "Audio Overview", None, None, 1]]
        )
        httpx_mock.add_response(content=notebook_response.encode())
        httpx_mock.add_response(content=audio_response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.artifacts.generate_audio("nb_123", source_ids=None)

        assert result.task_id == "audio_new"

    @pytest.mark.asyncio
    async def test_generate_data_table_source_ids_none_fetches_sources(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """generate_data_table with source_ids=None calls GET_NOTEBOOK to fetch source IDs."""
        notebook_response = build_rpc_response(
            RPCMethod.GET_NOTEBOOK,
            [
                [
                    "Test Notebook",
                    [[["src_xyz"], "Source X", [None, 0], [None, 2]]],
                    "nb_123",
                    None,
                    None,
                    [None, None, None, None, None, [1704067200, 0]],
                ]
            ],
        )
        table_response = build_rpc_response(
            RPCMethod.CREATE_ARTIFACT, [["dt_new", "Data Table", None, None, 1]]
        )
        httpx_mock.add_response(content=notebook_response.encode())
        httpx_mock.add_response(content=table_response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.artifacts.generate_data_table("nb_123", source_ids=None)

        assert result.task_id == "dt_new"


class TestPollStatusVariousPaths:
    """Tests for poll_status() various status paths (lines 1412-1518)."""

    @pytest.mark.asyncio
    async def test_poll_status_not_found_artifact_not_in_list(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """poll_status returns 'not_found' when task_id not found in artifact list."""
        # Return artifacts that don't include the task_id we're polling
        artifact = ["some_other_artifact", "Report", 2, None, 3]
        response = build_rpc_response(RPCMethod.LIST_ARTIFACTS, [[artifact]])
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.artifacts.poll_status("nb_123", "unknown_task_id")

        assert result.status == "not_found"
        assert result.task_id == "unknown_task_id"

    @pytest.mark.asyncio
    async def test_poll_status_completed_audio_without_url_downgrades_to_processing(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """poll_status downgrades audio from COMPLETED to in_progress when URLs missing."""
        # Audio artifact with COMPLETED status but no media URLs (art[6] missing)
        artifact = [
            "audio_task",
            "Audio Overview",
            1,  # AUDIO type
            None,
            3,  # COMPLETED status
            # No art[6] — missing media URL data
        ]
        response = build_rpc_response(RPCMethod.LIST_ARTIFACTS, [[artifact]])
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.artifacts.poll_status("nb_123", "audio_task")

        # Should be downgraded to in_progress since URL is not ready
        assert result.status == "in_progress"

    @pytest.mark.asyncio
    async def test_poll_status_completed_report_no_url_check(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """poll_status returns 'completed' for non-media types without URL check."""
        # Report artifact (non-media) — status=COMPLETED should pass through directly
        artifact = [
            "report_task",
            "Briefing Doc",
            2,  # REPORT type — non-media
            None,
            3,  # COMPLETED
        ]
        response = build_rpc_response(RPCMethod.LIST_ARTIFACTS, [[artifact]])
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.artifacts.poll_status("nb_123", "report_task")

        assert result.status == "completed"

    @pytest.mark.asyncio
    async def test_poll_status_completed_audio_with_valid_url(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """poll_status returns 'completed' for audio when URLs are ready."""
        # Audio artifact with proper media URL structure
        audio_artifact = [
            "audio_ready",
            "Audio Overview",
            1,  # AUDIO type
            None,
            3,  # COMPLETED
            None,
            [
                None,
                None,
                None,
                None,
                None,
                [["https://storage.googleapis.com/audio.mp4", None, "audio/mp4"]],
            ],
        ]
        response = build_rpc_response(RPCMethod.LIST_ARTIFACTS, [[audio_artifact]])
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.artifacts.poll_status("nb_123", "audio_ready")

        assert result.status == "completed"

    @pytest.mark.asyncio
    async def test_poll_status_empty_list_returns_not_found(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """poll_status returns 'not_found' when artifact list is empty."""
        response = build_rpc_response(RPCMethod.LIST_ARTIFACTS, [[]])
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.artifacts.poll_status("nb_123", "some_task_id")

        assert result.status == "not_found"


class TestCallGenerateErrorHandling:
    """Tests for _call_generate() error handling (USER_DISPLAYABLE_ERROR path)."""

    @pytest.mark.asyncio
    async def test_generate_audio_user_displayable_error_returns_failed(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """_call_generate returns failed GenerationStatus on USER_DISPLAYABLE_ERROR."""
        async with NotebookLMClient(auth_tokens) as client:
            err = RPCError("You have exceeded your quota")
            err.rpc_code = "USER_DISPLAYABLE_ERROR"
            # Pass source_ids explicitly so get_source_ids (GET_NOTEBOOK) is NOT called.
            # Then patch _core.rpc_call so the CREATE_ARTIFACT call raises the error.
            with patch.object(
                client.artifacts._core,
                "rpc_call",
                AsyncMock(side_effect=err),
            ):
                result = await client.artifacts.generate_audio("nb_123", source_ids=["src_001"])

        assert result.status == "failed"
        assert result.error_code == "USER_DISPLAYABLE_ERROR"

    @pytest.mark.asyncio
    async def test_generate_audio_other_rpc_error_reraises(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """_call_generate re-raises non-USER_DISPLAYABLE_ERROR RPCErrors."""
        async with NotebookLMClient(auth_tokens) as client:
            err = RPCError("Server error")
            err.rpc_code = "INTERNAL_ERROR"
            # Pass source_ids explicitly so get_source_ids (GET_NOTEBOOK) is NOT called.
            # Then patch _core.rpc_call so the CREATE_ARTIFACT call raises the error.
            with (
                patch.object(
                    client.artifacts._core,
                    "rpc_call",
                    AsyncMock(side_effect=err),
                ),
                pytest.raises(RPCError),
            ):
                await client.artifacts.generate_audio("nb_123", source_ids=["src_001"])


class TestDownloadUrlValidation:
    """Tests for _download_url security validation (lines 2041-2044)."""

    @pytest.mark.asyncio
    async def test_download_url_non_https_raises_error(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """_download_url raises ArtifactDownloadError for non-HTTPS URLs."""
        from notebooklm.types import ArtifactDownloadError

        # Build completed audio artifact with HTTP (not HTTPS) URL
        audio_artifact = [
            "audio_insecure",
            "Audio Overview",
            1,  # AUDIO type
            None,
            3,  # COMPLETED
            None,
            [
                None,
                None,
                None,
                None,
                None,
                [["http://storage.googleapis.com/audio.mp4", None, "audio/mp4"]],
            ],
        ]
        response = build_rpc_response(RPCMethod.LIST_ARTIFACTS, [[audio_artifact]])
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            with pytest.raises(ArtifactDownloadError, match="must use HTTPS"):
                await client.artifacts.download_audio("nb_123", "/tmp/audio.mp4")

    @pytest.mark.asyncio
    async def test_download_url_untrusted_domain_raises_error(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """_download_url raises ArtifactDownloadError for untrusted domains."""
        from notebooklm.types import ArtifactDownloadError

        audio_artifact = [
            "audio_untrusted",
            "Audio Overview",
            1,  # AUDIO type
            None,
            3,  # COMPLETED
            None,
            [
                None,
                None,
                None,
                None,
                None,
                [["https://evil.example.com/audio.mp4", None, "audio/mp4"]],
            ],
        ]
        response = build_rpc_response(RPCMethod.LIST_ARTIFACTS, [[audio_artifact]])
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            with pytest.raises(ArtifactDownloadError, match="Untrusted download domain"):
                await client.artifacts.download_audio("nb_123", "/tmp/audio.mp4")


class TestParseGenerationResult:
    """Tests for _parse_generation_result (lines 2092-2095, 2117-2123)."""

    @pytest.mark.asyncio
    async def test_generate_returns_failed_status_when_no_artifact_id(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
        monkeypatch,
    ):
        """_parse_generation_result returns failed status when result has no artifact_id."""
        # These tests pin down soft-strict behavior; guard against CI flipping
        # NOTEBOOKLM_STRICT_DECODE on in the future. Strict-mode coverage of
        # the same inputs lives in test_artifacts_drift.py.
        monkeypatch.delenv("NOTEBOOKLM_STRICT_DECODE", raising=False)
        notebook_response = build_rpc_response(
            RPCMethod.GET_NOTEBOOK,
            [
                [
                    "Test Notebook",
                    [[["src_001"], "Source 1", [None, 0], [None, 2]]],
                    "nb_123",
                    None,
                    None,
                    [None, None, None, None, None, [1704067200, 0]],
                ]
            ],
        )
        # CREATE_ARTIFACT returns an empty list — no artifact_id
        empty_response = build_rpc_response(RPCMethod.CREATE_ARTIFACT, [])
        httpx_mock.add_response(content=notebook_response.encode())
        httpx_mock.add_response(content=empty_response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.artifacts.generate_audio("nb_123")

        assert result.status == "failed"
        assert result.task_id == ""

    @pytest.mark.asyncio
    async def test_generate_returns_failed_status_when_result_is_none(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
        monkeypatch,
    ):
        """_parse_generation_result returns failed status when result is None."""
        monkeypatch.delenv("NOTEBOOKLM_STRICT_DECODE", raising=False)
        notebook_response = build_rpc_response(
            RPCMethod.GET_NOTEBOOK,
            [
                [
                    "Test Notebook",
                    [[["src_001"], "Source 1", [None, 0], [None, 2]]],
                    "nb_123",
                    None,
                    None,
                    [None, None, None, None, None, [1704067200, 0]],
                ]
            ],
        )
        # CREATE_ARTIFACT returns null result
        null_response = build_rpc_response(RPCMethod.CREATE_ARTIFACT, None)
        httpx_mock.add_response(content=notebook_response.encode())
        httpx_mock.add_response(content=null_response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.artifacts.generate_audio("nb_123")

        assert result.status == "failed"

    @pytest.mark.asyncio
    async def test_generate_returns_status_from_artifact_data(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
        monkeypatch,
    ):
        """_parse_generation_result reads status_code from artifact_data[4]."""
        monkeypatch.delenv("NOTEBOOKLM_STRICT_DECODE", raising=False)
        notebook_response = build_rpc_response(
            RPCMethod.GET_NOTEBOOK,
            [
                [
                    "Test Notebook",
                    [[["src_001"], "Source 1", [None, 0], [None, 2]]],
                    "nb_123",
                    None,
                    None,
                    [None, None, None, None, None, [1704067200, 0]],
                ]
            ],
        )
        # CREATE_ARTIFACT returns artifact with status_code=3 (COMPLETED)
        completed_response = build_rpc_response(
            RPCMethod.CREATE_ARTIFACT, [["art_done", "Audio", 1, None, 3]]
        )
        httpx_mock.add_response(content=notebook_response.encode())
        httpx_mock.add_response(content=completed_response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.artifacts.generate_audio("nb_123")

        assert result.task_id == "art_done"
        assert result.status == "completed"


class TestGetArtifactTypeNameAndIsMediaReady:
    """Tests for _get_artifact_type_name and _is_media_ready helper methods."""

    @pytest.mark.asyncio
    async def test_poll_status_quiz_completed_no_url_check(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """poll_status returns 'completed' for quiz without URL check (non-media type)."""
        artifact = [
            "quiz_task",
            "Quiz",
            4,  # QUIZ type
            None,
            3,  # COMPLETED
        ]
        response = build_rpc_response(RPCMethod.LIST_ARTIFACTS, [[artifact]])
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.artifacts.poll_status("nb_123", "quiz_task")

        assert result.status == "completed"

    @pytest.mark.asyncio
    async def test_poll_status_data_table_completed_no_url_check(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """poll_status returns 'completed' for data table without URL check."""
        artifact = [
            "table_task",
            "Data Table",
            9,  # DATA_TABLE type
            None,
            3,  # COMPLETED
        ]
        response = build_rpc_response(RPCMethod.LIST_ARTIFACTS, [[artifact]])
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.artifacts.poll_status("nb_123", "table_task")

        assert result.status == "completed"

    @pytest.mark.asyncio
    async def test_poll_status_video_completed_with_valid_url(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """poll_status returns 'completed' for video when art[8] has valid URL."""
        # Video artifact with art[8] containing a URL.
        # Real-API shape: art[8][i][0][0] holds the URL string (matches the
        # structure parsed by download_video).
        video_artifact = [
            "video_task",
            "Video Overview",
            3,  # VIDEO type
            None,
            3,  # COMPLETED
            None,
            None,
            None,
            [[["https://storage.googleapis.com/video.mp4"]]],  # art[8]
        ]
        response = build_rpc_response(RPCMethod.LIST_ARTIFACTS, [[video_artifact]])
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.artifacts.poll_status("nb_123", "video_task")

        assert result.status == "completed"

    @pytest.mark.asyncio
    async def test_poll_status_video_completed_without_url_downgrades(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """poll_status downgrades video from COMPLETED to in_progress when URL missing."""
        video_artifact = [
            "video_task",
            "Video Overview",
            3,  # VIDEO type
            None,
            3,  # COMPLETED — but no art[8]
        ]
        response = build_rpc_response(RPCMethod.LIST_ARTIFACTS, [[video_artifact]])
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.artifacts.poll_status("nb_123", "video_task")

        assert result.status == "in_progress"

    @pytest.mark.asyncio
    async def test_poll_status_infographic_completed_without_url_downgrades(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """poll_status downgrades infographic from COMPLETED to in_progress when URL missing."""
        infographic_artifact = [
            "ig_task",
            "Infographic",
            7,  # INFOGRAPHIC type
            None,
            3,  # COMPLETED — but no nested URL structure
        ]
        response = build_rpc_response(RPCMethod.LIST_ARTIFACTS, [[infographic_artifact]])
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.artifacts.poll_status("nb_123", "ig_task")

        assert result.status == "in_progress"

    @pytest.mark.asyncio
    async def test_poll_status_slide_deck_completed_with_valid_url(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """poll_status returns 'completed' for slide deck when art[16][3] has valid URL."""
        slide_artifact = [
            "slide_task",
            "Slide Deck",
            8,  # SLIDE_DECK type
            None,
            3,  # COMPLETED
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            [None, "Title", None, "https://docs.googleusercontent.com/slides.pdf"],  # art[16]
        ]
        response = build_rpc_response(RPCMethod.LIST_ARTIFACTS, [[slide_artifact]])
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.artifacts.poll_status("nb_123", "slide_task")

        assert result.status == "completed"

    @pytest.mark.asyncio
    async def test_poll_status_slide_deck_completed_without_url_downgrades(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """poll_status downgrades slide deck from COMPLETED to in_progress when URL missing."""
        slide_artifact = [
            "slide_task",
            "Slide Deck",
            8,  # SLIDE_DECK type
            None,
            3,  # COMPLETED — but no art[16]
        ]
        response = build_rpc_response(RPCMethod.LIST_ARTIFACTS, [[slide_artifact]])
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.artifacts.poll_status("nb_123", "slide_task")

        assert result.status == "in_progress"


class TestDownloadQuizFlashcardParsing:
    """Tests for download_quiz/flashcard parsing error paths."""

    @pytest.mark.asyncio
    async def test_download_flashcards_html_without_app_data(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """download_flashcards raises ArtifactParseError when HTML lacks data-app-data."""
        artifact_data = [
            "fc_001",  # [0] id
            "My Flashcards",  # [1] title
            4,  # [2] QUIZ type (also used for flashcards)
            None,  # [3]
            3,  # [4] COMPLETED
            None,  # [5]
            None,  # [6]
            None,  # [7]
            None,  # [8]
            [None, [1]],  # [9] options: [9][1][0] = 1 => flashcards variant
            None,  # [10]
            None,  # [11]
            None,  # [12]
            None,  # [13]
            None,  # [14]
            [[1704067200]],  # [15] created_at
        ]
        list_response = build_rpc_response(RPCMethod.LIST_ARTIFACTS, [[artifact_data]])
        httpx_mock.add_response(content=list_response.encode())

        html_without_data = "<html><body><p>No app data</p></body></html>"

        async with NotebookLMClient(auth_tokens) as client:
            with patch.object(
                client.artifacts,
                "_get_artifact_content",
                AsyncMock(return_value=html_without_data),
            ):
                with pytest.raises(ArtifactParseError, match="data-app-data"):
                    await client.artifacts.download_flashcards("nb_123", "/tmp/flashcards.json")

    @pytest.mark.asyncio
    async def test_download_quiz_invalid_output_format_raises_validation_error(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """download_quiz raises ValidationError for invalid output_format."""
        async with NotebookLMClient(auth_tokens) as client:
            with pytest.raises(ValidationError, match="Invalid output_format"):
                await client.artifacts.download_quiz("nb_123", "/tmp/quiz.xyz", output_format="xyz")

    @pytest.mark.asyncio
    async def test_download_flashcards_invalid_output_format_raises_validation_error(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """download_flashcards raises ValidationError for invalid output_format."""
        async with NotebookLMClient(auth_tokens) as client:
            with pytest.raises(ValidationError, match="Invalid output_format"):
                await client.artifacts.download_flashcards(
                    "nb_123", "/tmp/flashcards.bad", output_format="csv"
                )

    @pytest.mark.asyncio
    async def test_download_quiz_no_completed_raises_not_ready(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """download_quiz raises ArtifactNotReadyError when no completed quiz exists."""
        # Return empty list — no quiz artifacts
        response = build_rpc_response(RPCMethod.LIST_ARTIFACTS, [[]])
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            with pytest.raises(ArtifactNotReadyError):
                await client.artifacts.download_quiz("nb_123", "/tmp/quiz.json")

    @pytest.mark.asyncio
    async def test_download_quiz_html_format_returns_raw_html(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
        tmp_path,
    ):
        """download_quiz with output_format='html' writes raw HTML content."""
        artifact_data = [
            "quiz_html_001",  # [0] id
            "HTML Quiz",  # [1] title
            4,  # [2] QUIZ type
            None,  # [3]
            3,  # [4] COMPLETED
            None,  # [5]
            None,  # [6]
            None,  # [7]
            None,  # [8]
            [None, [2]],  # [9] options: [9][1][0] = 2 => quiz variant
            None,  # [10]
            None,  # [11]
            None,  # [12]
            None,  # [13]
            None,  # [14]
            [[1704067200]],  # [15] created_at
        ]
        list_response = build_rpc_response(RPCMethod.LIST_ARTIFACTS, [[artifact_data]])
        httpx_mock.add_response(content=list_response.encode())

        raw_html = '<html><body data-app-data="{&quot;quiz&quot;:[]}">content</body></html>'

        output_path = str(tmp_path / "quiz.html")
        async with NotebookLMClient(auth_tokens) as client:
            with patch.object(
                client.artifacts,
                "_get_artifact_content",
                AsyncMock(return_value=raw_html),
            ):
                result = await client.artifacts.download_quiz(
                    "nb_123", output_path, output_format="html"
                )

        assert result == output_path
        from pathlib import Path

        assert Path(output_path).read_text() == raw_html


class TestWaitForCompletionDeprecated:
    """Tests for wait_for_completion deprecated poll_interval parameter."""

    @pytest.mark.asyncio
    async def test_wait_for_completion_deprecated_poll_interval_warning(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """wait_for_completion issues DeprecationWarning for poll_interval parameter."""
        import warnings

        # Return a completed artifact immediately so it doesn't loop
        artifact = ["task_dep", "Report", 2, None, 3]
        response = build_rpc_response(RPCMethod.LIST_ARTIFACTS, [[artifact]])
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            with warnings.catch_warnings(record=True) as w:
                warnings.simplefilter("always")
                result = await client.artifacts.wait_for_completion(
                    "nb_123", "task_dep", poll_interval=1.0
                )
                assert any(issubclass(warning.category, DeprecationWarning) for warning in w)

        assert result.status == "completed"


def _decoded_request_body(request) -> str:
    """Return the readable URL-decoded body of the latest CREATE_ARTIFACT POST.

    The batchexecute payload double-encodes the inner params (the f.req
    array is JSON, and one of its slots is itself a JSON-encoded string),
    so we further normalise the embedded backslash-escaped quotes back to
    plain ``"`` to make substring assertions stable.
    """
    from urllib.parse import unquote_plus

    raw = unquote_plus(request.content.decode("utf-8"))
    # Collapse the JSON-in-JSON escaping so that '"ja"' literally appears.
    return raw.replace('\\"', '"')


_LANGUAGE_AWARE_GENERATORS = [
    "generate_audio",
    "generate_video",
    "generate_cinematic_video",
    "generate_report",
    "generate_study_guide",
    "generate_infographic",
    "generate_slide_deck",
    "generate_data_table",
    "generate_mind_map",
]


def _register_generate_responses(method_name: str, httpx_mock, build_rpc_response) -> int:
    """Register the HTTP responses required for one ``generate_*`` call and
    return the index of the request that carries the language code.

    Most language-aware generators issue a single CREATE_ARTIFACT call.
    ``generate_mind_map`` is the outlier: it calls GENERATE_MIND_MAP first,
    then CREATE_NOTE + UPDATE_NOTE to persist the result. The language code
    is embedded only in the first request, so the assertion target differs.
    """
    if method_name == "generate_mind_map":
        # GENERATE_MIND_MAP returns mind-map JSON; subsequent note RPCs persist it.
        httpx_mock.add_response(
            content=build_rpc_response(
                RPCMethod.GENERATE_MIND_MAP,
                [['{"name": "Root", "children": []}', None, ["note_xyz"]]],
            ).encode()
        )
        httpx_mock.add_response(
            content=build_rpc_response(
                RPCMethod.CREATE_NOTE,
                [["note_xyz"]],
            ).encode()
        )
        httpx_mock.add_response(
            content=build_rpc_response(
                RPCMethod.UPDATE_NOTE,
                [["note_xyz"]],
            ).encode()
        )
        return 0  # language travels with the first (GENERATE_MIND_MAP) request

    httpx_mock.add_response(
        content=build_rpc_response(
            RPCMethod.CREATE_ARTIFACT,
            [["artifact_123", "Title", "2024-01-05", None, 1]],
        ).encode()
    )
    return -1  # last (and only) request carries the language


class TestGenerateUsesNotebookLMHL:
    """The 9 language-aware generate_* methods must honor NOTEBOOKLM_HL when
    the caller does not pass an explicit language argument, and the explicit
    argument must still win when both are present.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("method_name", _LANGUAGE_AWARE_GENERATORS)
    async def test_generate_uses_env_language_when_none(
        self,
        method_name,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
        monkeypatch,
    ):
        """NOTEBOOKLM_HL=ja, no language arg -> outgoing RPC carries 'ja'."""
        monkeypatch.setenv("NOTEBOOKLM_HL", "ja")

        request_index = _register_generate_responses(method_name, httpx_mock, build_rpc_response)

        async with NotebookLMClient(auth_tokens) as client:
            method = getattr(client.artifacts, method_name)
            await method(notebook_id="nb_123", source_ids=["src_001"])

        body = _decoded_request_body(httpx_mock.get_requests()[request_index])
        # The language code is embedded as a quoted JSON string inside the
        # nested params list. Assert presence/absence.
        assert '"ja"' in body
        assert '"en"' not in body

    @pytest.mark.asyncio
    @pytest.mark.parametrize("method_name", _LANGUAGE_AWARE_GENERATORS)
    async def test_generate_explicit_language_overrides_env(
        self,
        method_name,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
        monkeypatch,
    ):
        """NOTEBOOKLM_HL=ja but explicit language='ko' -> outgoing carries 'ko'."""
        monkeypatch.setenv("NOTEBOOKLM_HL", "ja")

        request_index = _register_generate_responses(method_name, httpx_mock, build_rpc_response)

        async with NotebookLMClient(auth_tokens) as client:
            method = getattr(client.artifacts, method_name)
            await method(
                notebook_id="nb_123",
                source_ids=["src_001"],
                language="ko",
            )

        body = _decoded_request_body(httpx_mock.get_requests()[request_index])
        assert '"ko"' in body
        assert '"ja"' not in body
