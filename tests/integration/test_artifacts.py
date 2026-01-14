"""Integration tests for ArtifactsAPI."""

import csv
import json

import pytest
from pytest_httpx import HTTPXMock

from notebooklm import NotebookLMClient
from notebooklm.rpc import AudioFormat, AudioLength, RPCError, RPCMethod, VideoFormat, VideoStyle


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
            RPCMethod.CREATE_VIDEO, [["artifact_123", "Audio Overview", "2024-01-05", None, 1]]
        )
        httpx_mock.add_response(content=audio_response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.artifacts.generate_audio(notebook_id="nb_123")

        assert result is not None
        assert result.task_id == "artifact_123"
        assert result.status in ("pending", "in_progress")

        request = httpx_mock.get_requests()[-1]
        assert RPCMethod.CREATE_VIDEO.value in str(request.url)

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
            RPCMethod.CREATE_VIDEO, [["artifact_123", "Audio Overview", "2024-01-05", None, 1]]
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
            RPCMethod.CREATE_VIDEO, [["artifact_456", "Video Overview", "2024-01-05", None, 1]]
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
            RPCMethod.CREATE_VIDEO, [["artifact_456", "Slide Deck", "2024-01-05", None, 1]]
        )
        httpx_mock.add_response(content=notebook_response.encode())
        httpx_mock.add_response(content=slide_deck_response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.artifacts.generate_slide_deck(notebook_id="nb_123")

        assert result is not None
        assert result.task_id == "artifact_456"

    @pytest.mark.asyncio
    async def test_poll_studio_status(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        response = build_rpc_response(
            RPCMethod.LIST_ARTIFACTS, ["task_id_123", "completed", "https://audio.url"]
        )
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.artifacts.poll_status(
                notebook_id="nb_123",
                task_id="task_id_123",
            )

        assert result is not None
        assert result.status == "completed"
        assert result.url == "https://audio.url"


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
            RPCMethod.CREATE_VIDEO, [["quiz_123", "Quiz", "2024-01-05", None, 1]]
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
        response = build_rpc_response(RPCMethod.DELETE_STUDIO, [True])
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
        mindmap_response = build_rpc_response(RPCMethod.ACT_ON_SOURCES, None)
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
            RPCMethod.CREATE_VIDEO, [["fc_123", "Flashcards", "2024-01-05", None, 1]]
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
            RPCMethod.CREATE_VIDEO, [["sg_123", "Study Guide", "2024-01-05", None, 1]]
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
            RPCMethod.CREATE_VIDEO, [["ig_123", "Infographic", "2024-01-05", None, 1]]
        )
        httpx_mock.add_response(content=notebook_response.encode())
        httpx_mock.add_response(content=infographic_response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.artifacts.generate_infographic("nb_123")

        assert result is not None
        assert result.task_id == "ig_123"

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
            RPCMethod.CREATE_VIDEO, [["dt_123", "Data Table", "2024-01-05", None, 1]]
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
        response = build_rpc_response(RPCMethod.DELETE_STUDIO, None)
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.artifacts.delete("nb_123", "art_001")

        assert result is True
        request = httpx_mock.get_request()
        assert RPCMethod.DELETE_STUDIO in str(request.url)

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
            with pytest.raises(ValueError, match="(not found|[Nn]o completed)"):
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
            with pytest.raises(ValueError, match="not found"):
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
            with pytest.raises(ValueError, match="(not found|[Nn]o completed)"):
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
            with pytest.raises(ValueError, match="(not found|[Nn]o completed)"):
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
            with pytest.raises(ValueError, match="(not found|[Nn]o completed)"):
                await client.artifacts.download_slide_deck("nb_123", "/tmp/slides")

    @pytest.mark.asyncio
    async def test_poll_status_with_url(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test poll_status returns url when available."""
        response = build_rpc_response(
            RPCMethod.LIST_ARTIFACTS, ["task_id_123", "completed", "https://audio.url", None]
        )
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.artifacts.poll_status(
                notebook_id="nb_123",
                task_id="task_id_123",
            )

        assert result is not None
        assert result.url == "https://audio.url"

    @pytest.mark.asyncio
    async def test_poll_status_with_error(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test poll_status returns error message when available."""
        response = build_rpc_response(
            RPCMethod.LIST_ARTIFACTS, ["task_id_123", "failed", None, "Generation failed"]
        )
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.artifacts.poll_status(
                notebook_id="nb_123",
                task_id="task_id_123",
            )

        assert result is not None
        assert result.error == "Generation failed"

    @pytest.mark.asyncio
    async def test_rpc_error_http_500(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
    ):
        """Test RPC error handling for HTTP 500."""
        httpx_mock.add_response(status_code=500)

        async with NotebookLMClient(auth_tokens) as client:
            with pytest.raises(RPCError, match="HTTP 500"):
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
            with pytest.raises(ValueError, match="No completed report"):
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
            with pytest.raises(ValueError, match="No mind maps found"):
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
            with pytest.raises(ValueError, match="No completed data table"):
                await client.artifacts.download_data_table("nb_123", "/tmp/data.csv")
