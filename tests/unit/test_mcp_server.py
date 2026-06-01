"""Unit tests for the NotebookLM MCP server.

All tests are fully offline – the NotebookLMClient is mocked so no
authentication or network access is needed.
"""

import json
from datetime import datetime
from urllib.parse import urlparse
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers to build fake domain objects
# ---------------------------------------------------------------------------


def _fake_notebook(nb_id="nb_001", title="Test Notebook"):
    nb = MagicMock()
    nb.id = nb_id
    nb.title = title
    nb.created_at = datetime(2024, 1, 1)
    nb.updated_at = datetime(2024, 6, 1)
    return nb


def _fake_source(src_id="src_001", title="Source 1", kind="web_page", status="ready", url=None):
    src = MagicMock()
    src.id = src_id
    src.title = title
    src.kind = kind
    src.status = status
    src.url = url
    return src


def _fake_generation_status(
    task_id="task_001", is_complete=True, is_failed=False, status="complete"
):
    gs = MagicMock()
    gs.task_id = task_id
    gs.is_complete = is_complete
    gs.is_failed = is_failed
    gs.status = status
    return gs


def _fake_ask_result(answer="This is the answer"):
    ar = MagicMock()
    ar.answer = answer
    return ar


def _fake_fulltext(content="Full text content here"):
    ft = MagicMock()
    ft.content = content
    return ft


# ---------------------------------------------------------------------------
# Fixture: reset the module-level singleton between tests
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_client_singleton():
    """Ensure the client singleton is clear before and after every test."""
    from notebooklm_mcp.server import _reset_client_for_testing

    _reset_client_for_testing()
    yield
    _reset_client_for_testing()


# ---------------------------------------------------------------------------
# Fixture: a fully mocked NotebookLMClient
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_client():
    client = MagicMock()

    # Notebooks API
    client.notebooks = MagicMock()
    client.notebooks.list = AsyncMock(return_value=[_fake_notebook()])
    client.notebooks.create = AsyncMock(return_value=_fake_notebook("nb_new", "New NB"))
    client.notebooks.delete = AsyncMock(return_value=True)
    client.notebooks.get = AsyncMock(return_value=_fake_notebook())

    # Sources API
    client.sources = MagicMock()
    client.sources.list = AsyncMock(return_value=[_fake_source()])
    client.sources.add_url = AsyncMock(return_value=_fake_source(src_id="src_url"))
    client.sources.add_text = AsyncMock(return_value=_fake_source(src_id="src_txt"))
    client.sources.get_fulltext = AsyncMock(return_value=_fake_fulltext())

    # Chat API
    client.chat = MagicMock()
    client.chat.ask = AsyncMock(return_value=_fake_ask_result())

    # Artifacts API
    gen_status = _fake_generation_status()
    client.artifacts = MagicMock()
    client.artifacts.generate_audio = AsyncMock(return_value=gen_status)
    client.artifacts.generate_quiz = AsyncMock(return_value=gen_status)
    client.artifacts.generate_mind_map = AsyncMock(
        return_value={"mind_map": {"name": "Root", "children": []}, "note_id": "note_001"}
    )
    client.artifacts.wait_for_completion = AsyncMock(
        return_value=_fake_generation_status(is_complete=True)
    )
    client.artifacts.download_audio = AsyncMock(return_value="/tmp/audio.mp4")
    client.artifacts.download_quiz = AsyncMock(return_value="/tmp/quiz.json")

    # Context manager support
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)

    return client


@pytest.fixture
def patched_get_client(mock_client):
    """Patch get_client() to return mock_client without network calls."""
    with patch("notebooklm_mcp.server.get_client", new=AsyncMock(return_value=mock_client)):
        yield mock_client


# ---------------------------------------------------------------------------
# Tests: notebooklm_list_notebooks
# ---------------------------------------------------------------------------


class TestListNotebooks:
    async def test_returns_json_array(self, patched_get_client):
        from notebooklm_mcp.server import notebooklm_list_notebooks

        result = await notebooklm_list_notebooks()
        data = json.loads(result)
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["id"] == "nb_001"
        assert data[0]["title"] == "Test Notebook"

    async def test_empty_list(self, patched_get_client):
        patched_get_client.notebooks.list = AsyncMock(return_value=[])
        from notebooklm_mcp.server import notebooklm_list_notebooks

        result = await notebooklm_list_notebooks()
        assert json.loads(result) == []

    async def test_runtime_error_returns_error_string(self):
        from notebooklm_mcp.server import notebooklm_list_notebooks

        with patch(
            "notebooklm_mcp.server.get_client",
            new=AsyncMock(side_effect=RuntimeError("auth missing")),
        ):
            result = await notebooklm_list_notebooks()
        assert result.startswith("ERROR:")
        assert "auth missing" in result

    async def test_api_exception_returns_error_string(self, patched_get_client):
        patched_get_client.notebooks.list = AsyncMock(side_effect=Exception("network error"))
        from notebooklm_mcp.server import notebooklm_list_notebooks

        result = await notebooklm_list_notebooks()
        assert result.startswith("ERROR:")


# ---------------------------------------------------------------------------
# Tests: notebooklm_create_notebook
# ---------------------------------------------------------------------------


class TestCreateNotebook:
    async def test_creates_and_returns_notebook(self, patched_get_client):
        from notebooklm_mcp.server import notebooklm_create_notebook

        result = await notebooklm_create_notebook(title="New NB")
        data = json.loads(result)
        assert data["id"] == "nb_new"
        assert data["title"] == "New NB"
        patched_get_client.notebooks.create.assert_awaited_once_with("New NB")

    async def test_empty_title_returns_error(self, patched_get_client):
        from notebooklm_mcp.server import notebooklm_create_notebook

        result = await notebooklm_create_notebook(title="   ")
        assert result.startswith("ERROR:")

    async def test_strips_whitespace_from_title(self, patched_get_client):
        from notebooklm_mcp.server import notebooklm_create_notebook

        await notebooklm_create_notebook(title="  My NB  ")
        patched_get_client.notebooks.create.assert_awaited_once_with("My NB")

    async def test_api_exception_returns_error_string(self, patched_get_client):
        patched_get_client.notebooks.create = AsyncMock(side_effect=Exception("boom"))
        from notebooklm_mcp.server import notebooklm_create_notebook

        result = await notebooklm_create_notebook(title="Test")
        assert result.startswith("ERROR:")


# ---------------------------------------------------------------------------
# Tests: notebooklm_delete_notebook
# ---------------------------------------------------------------------------


class TestDeleteNotebook:
    async def test_deletes_and_returns_confirmation(self, patched_get_client):
        from notebooklm_mcp.server import notebooklm_delete_notebook

        result = await notebooklm_delete_notebook(notebook_id="nb_001")
        data = json.loads(result)
        assert data["deleted"] is True
        assert data["notebook_id"] == "nb_001"

    async def test_empty_id_returns_error(self, patched_get_client):
        from notebooklm_mcp.server import notebooklm_delete_notebook

        result = await notebooklm_delete_notebook(notebook_id="")
        assert result.startswith("ERROR:")

    async def test_api_exception_returns_error_string(self, patched_get_client):
        patched_get_client.notebooks.delete = AsyncMock(side_effect=Exception("not found"))
        from notebooklm_mcp.server import notebooklm_delete_notebook

        result = await notebooklm_delete_notebook(notebook_id="nb_001")
        assert result.startswith("ERROR:")


# ---------------------------------------------------------------------------
# Tests: notebooklm_add_source_url
# ---------------------------------------------------------------------------


class TestAddSourceUrl:
    async def test_adds_url_source(self, patched_get_client):
        from notebooklm_mcp.server import notebooklm_add_source_url

        result = await notebooklm_add_source_url(notebook_id="nb_001", url="https://example.com")
        data = json.loads(result)
        assert data["id"] == "src_url"
        patched_get_client.sources.add_url.assert_awaited_once_with(
            "nb_001", "https://example.com", wait=True
        )

    async def test_empty_notebook_id_returns_error(self, patched_get_client):
        from notebooklm_mcp.server import notebooklm_add_source_url

        result = await notebooklm_add_source_url(notebook_id="", url="https://example.com")
        assert result.startswith("ERROR:")

    async def test_empty_url_returns_error(self, patched_get_client):
        from notebooklm_mcp.server import notebooklm_add_source_url

        result = await notebooklm_add_source_url(notebook_id="nb_001", url="")
        assert result.startswith("ERROR:")

    async def test_api_exception_returns_error_string(self, patched_get_client):
        patched_get_client.sources.add_url = AsyncMock(side_effect=Exception("timeout"))
        from notebooklm_mcp.server import notebooklm_add_source_url

        result = await notebooklm_add_source_url(notebook_id="nb_001", url="https://x.com")
        assert result.startswith("ERROR:")


# ---------------------------------------------------------------------------
# Tests: notebooklm_add_source_text
# ---------------------------------------------------------------------------


class TestAddSourceText:
    async def test_adds_text_source(self, patched_get_client):
        from notebooklm_mcp.server import notebooklm_add_source_text

        result = await notebooklm_add_source_text(
            notebook_id="nb_001", title="My Doc", text="Hello world"
        )
        data = json.loads(result)
        assert data["id"] == "src_txt"
        patched_get_client.sources.add_text.assert_awaited_once_with(
            "nb_001", "My Doc", "Hello world", wait=True
        )

    async def test_empty_title_returns_error(self, patched_get_client):
        from notebooklm_mcp.server import notebooklm_add_source_text

        result = await notebooklm_add_source_text(notebook_id="nb_001", title="", text="content")
        assert result.startswith("ERROR:")

    async def test_empty_text_returns_error(self, patched_get_client):
        from notebooklm_mcp.server import notebooklm_add_source_text

        result = await notebooklm_add_source_text(notebook_id="nb_001", title="Title", text="   ")
        assert result.startswith("ERROR:")

    async def test_api_exception_returns_error_string(self, patched_get_client):
        patched_get_client.sources.add_text = AsyncMock(side_effect=Exception("rpc error"))
        from notebooklm_mcp.server import notebooklm_add_source_text

        result = await notebooklm_add_source_text(notebook_id="nb_001", title="T", text="body")
        assert result.startswith("ERROR:")


# ---------------------------------------------------------------------------
# Tests: notebooklm_ask_chat
# ---------------------------------------------------------------------------


class TestAskChat:
    async def test_returns_answer_text(self, patched_get_client):
        from notebooklm_mcp.server import notebooklm_ask_chat

        result = await notebooklm_ask_chat(notebook_id="nb_001", query="What is this?")
        assert result == "This is the answer"

    async def test_empty_query_returns_error(self, patched_get_client):
        from notebooklm_mcp.server import notebooklm_ask_chat

        result = await notebooklm_ask_chat(notebook_id="nb_001", query="")
        assert result.startswith("ERROR:")

    async def test_empty_notebook_id_returns_error(self, patched_get_client):
        from notebooklm_mcp.server import notebooklm_ask_chat

        result = await notebooklm_ask_chat(notebook_id="", query="question")
        assert result.startswith("ERROR:")

    async def test_api_exception_returns_error_string(self, patched_get_client):
        patched_get_client.chat.ask = AsyncMock(side_effect=Exception("chat failed"))
        from notebooklm_mcp.server import notebooklm_ask_chat

        result = await notebooklm_ask_chat(notebook_id="nb_001", query="Q")
        assert result.startswith("ERROR:")


# ---------------------------------------------------------------------------
# Tests: notebooklm_generate_audio_podcast
# ---------------------------------------------------------------------------


class TestGenerateAudioPodcast:
    async def test_generates_and_downloads(self, patched_get_client, tmp_path):
        out = str(tmp_path / "podcast.mp4")
        patched_get_client.artifacts.download_audio = AsyncMock(return_value=out)
        from notebooklm_mcp.server import notebooklm_generate_audio_podcast

        result = await notebooklm_generate_audio_podcast(notebook_id="nb_001", download_path=out)
        data = json.loads(result)
        assert data["status"] == "complete"
        assert "file_path" in data

    async def test_empty_notebook_id_returns_error(self, patched_get_client):
        from notebooklm_mcp.server import notebooklm_generate_audio_podcast

        result = await notebooklm_generate_audio_podcast(notebook_id="")
        assert result.startswith("ERROR:")

    async def test_timeout_returns_timeout_json(self, patched_get_client):
        from notebooklm.exceptions import ArtifactTimeoutError
        patched_get_client.artifacts.wait_for_completion = AsyncMock(
            side_effect=ArtifactTimeoutError("nb_001", "task_001", 1.0, last_status="pending")
        )
        from notebooklm_mcp.server import notebooklm_generate_audio_podcast

        result = await notebooklm_generate_audio_podcast(notebook_id="nb_001", timeout=1.0)
        data = json.loads(result)
        assert data["status"] == "timeout"
        assert data["task_id"] == "task_001"

    async def test_generate_audio_exception_returns_error(self, patched_get_client):
        patched_get_client.artifacts.generate_audio = AsyncMock(
            side_effect=Exception("quota exceeded")
        )
        from notebooklm_mcp.server import notebooklm_generate_audio_podcast

        result = await notebooklm_generate_audio_podcast(notebook_id="nb_001")
        assert result.startswith("ERROR:")

    async def test_incomplete_status_returns_status_json(self, patched_get_client):
        patched_get_client.artifacts.wait_for_completion = AsyncMock(
            return_value=_fake_generation_status(is_complete=False, status="failed")
        )
        from notebooklm_mcp.server import notebooklm_generate_audio_podcast

        result = await notebooklm_generate_audio_podcast(notebook_id="nb_001")
        data = json.loads(result)
        assert data["status"] == "failed"

    async def test_download_failure_returns_graceful_json(self, patched_get_client):
        patched_get_client.artifacts.download_audio = AsyncMock(
            side_effect=Exception("download error")
        )
        from notebooklm_mcp.server import notebooklm_generate_audio_podcast

        result = await notebooklm_generate_audio_podcast(notebook_id="nb_001")
        data = json.loads(result)
        assert data["status"] == "complete_but_download_failed"

    async def test_language_passed_to_api(self, patched_get_client, tmp_path):
        out = str(tmp_path / "podcast.mp4")
        patched_get_client.artifacts.download_audio = AsyncMock(return_value=out)
        from notebooklm_mcp.server import notebooklm_generate_audio_podcast

        await notebooklm_generate_audio_podcast(
            notebook_id="nb_001", language="es", download_path=out
        )
        patched_get_client.artifacts.generate_audio.assert_awaited_once_with(
            "nb_001", language="es", instructions=None
        )

    async def test_instructions_passed_to_api(self, patched_get_client, tmp_path):
        out = str(tmp_path / "podcast.mp4")
        patched_get_client.artifacts.download_audio = AsyncMock(return_value=out)
        from notebooklm_mcp.server import notebooklm_generate_audio_podcast

        await notebooklm_generate_audio_podcast(
            notebook_id="nb_001", instructions="Be concise", download_path=out
        )
        patched_get_client.artifacts.generate_audio.assert_awaited_once_with(
            "nb_001", language="en", instructions="Be concise"
        )

    async def test_temp_file_cleaned_up_when_no_download_path(self, patched_get_client):
        """Test that temporary file is cleaned up when no download_path is provided."""
        from notebooklm_mcp.server import notebooklm_generate_audio_podcast
        from pathlib import Path

        # Mock that download succeeds
        saved_path = "/tmp/generated_podcast.mp4"
        patched_get_client.artifacts.download_audio = AsyncMock(return_value=saved_path)

        # Mock tempfile.mkstemp to track the temp file path
        temp_file_path = "/tmp/notebooklm_podcast_temp.mp4"
        with patch("tempfile.mkstemp", return_value=(10, temp_file_path)) as mock_mkstemp, \
                patch("pathlib.Path.unlink") as mock_unlink:
            result = await notebooklm_generate_audio_podcast(notebook_id="nb_001")

            # Verify mkstemp was called
            mock_mkstemp.assert_called_once_with(prefix="notebooklm_podcast_", suffix=".mp3")

            # Verify the function succeeded
            data = json.loads(result)
            assert data["status"] == "complete"
            assert data["file_path"] == saved_path

            # Verify the temp file was cleaned up (unlink called)
            mock_unlink.assert_called_once_with(missing_ok=True)


# ---------------------------------------------------------------------------
# Tests: notebooklm_generate_quiz
# ---------------------------------------------------------------------------


class TestGenerateQuiz:
    async def test_generates_quiz(self, patched_get_client, tmp_path):
        quiz_data = {"questions": [{"q": "Q1", "answer": "A"}]}
        quiz_file = tmp_path / "quiz.json"
        quiz_file.write_text(json.dumps(quiz_data))
        patched_get_client.artifacts.download_quiz = AsyncMock(return_value=str(quiz_file))

        from notebooklm_mcp.server import notebooklm_generate_quiz

        result = await notebooklm_generate_quiz(notebook_id="nb_001")
        data = json.loads(result)
        assert data["status"] == "complete"
        assert "questions" in data["quiz"]

    async def test_empty_notebook_id_returns_error(self, patched_get_client):
        from notebooklm_mcp.server import notebooklm_generate_quiz

        result = await notebooklm_generate_quiz(notebook_id="")
        assert result.startswith("ERROR:")

    async def test_timeout_returns_timeout_json(self, patched_get_client):
        from notebooklm.exceptions import ArtifactTimeoutError
        patched_get_client.artifacts.wait_for_completion = AsyncMock(
            side_effect=ArtifactTimeoutError("nb_001", "task_001", 1.0, last_status="pending")
        )
        from notebooklm_mcp.server import notebooklm_generate_quiz

        result = await notebooklm_generate_quiz(notebook_id="nb_001", timeout=1.0)
        data = json.loads(result)
        assert data["status"] == "timeout"
        assert data["task_id"] == "task_001"

    async def test_generate_exception_returns_error(self, patched_get_client):
        patched_get_client.artifacts.generate_quiz = AsyncMock(side_effect=Exception("rpc error"))
        from notebooklm_mcp.server import notebooklm_generate_quiz

        result = await notebooklm_generate_quiz(notebook_id="nb_001")
        assert result.startswith("ERROR:")

    async def test_download_failure_returns_graceful_json(self, patched_get_client):
        patched_get_client.artifacts.download_quiz = AsyncMock(
            side_effect=Exception("download error")
        )
        from notebooklm_mcp.server import notebooklm_generate_quiz

        result = await notebooklm_generate_quiz(notebook_id="nb_001")
        data = json.loads(result)
        assert data["status"] == "complete_but_download_failed"


# ---------------------------------------------------------------------------
# Tests: notebooklm_generate_mind_map
# ---------------------------------------------------------------------------


class TestGenerateMindMap:
    async def test_generates_mind_map(self, patched_get_client):
        from notebooklm_mcp.server import notebooklm_generate_mind_map

        result = await notebooklm_generate_mind_map(notebook_id="nb_001")
        data = json.loads(result)
        assert data["status"] == "complete"
        assert data["mind_map"]["name"] == "Root"
        assert data["note_id"] == "note_001"

    async def test_empty_notebook_id_returns_error(self, patched_get_client):
        from notebooklm_mcp.server import notebooklm_generate_mind_map

        result = await notebooklm_generate_mind_map(notebook_id="")
        assert result.startswith("ERROR:")

    async def test_language_and_instructions_passed(self, patched_get_client):
        from notebooklm_mcp.server import notebooklm_generate_mind_map

        await notebooklm_generate_mind_map(
            notebook_id="nb_001", language="fr", instructions="Focus on risks"
        )
        patched_get_client.artifacts.generate_mind_map.assert_awaited_once_with(
            "nb_001", language="fr", instructions="Focus on risks"
        )

    async def test_api_exception_returns_error_string(self, patched_get_client):
        patched_get_client.artifacts.generate_mind_map = AsyncMock(
            side_effect=Exception("rpc error")
        )
        from notebooklm_mcp.server import notebooklm_generate_mind_map

        result = await notebooklm_generate_mind_map(notebook_id="nb_001")
        assert result.startswith("ERROR:")


# ---------------------------------------------------------------------------
# Tests: resources
# ---------------------------------------------------------------------------


class TestResources:
    async def test_notebook_metadata_resource(self, patched_get_client):
        from notebooklm_mcp.server import notebook_metadata

        result = await notebook_metadata(notebook_id="nb_001")
        data = json.loads(result)
        assert data["id"] == "nb_001"
        assert "sources" in data
        assert data["sources"][0]["id"] == "src_001"

    async def test_notebook_metadata_runtime_error(self):
        from notebooklm_mcp.server import notebook_metadata

        with patch(
            "notebooklm_mcp.server.get_client",
            new=AsyncMock(side_effect=RuntimeError("no auth")),
        ):
            result = await notebook_metadata(notebook_id="nb_001")
        assert result.startswith("ERROR: no auth")

    async def test_source_fulltext_resource(self, patched_get_client):
        from notebooklm_mcp.server import notebook_source_fulltext

        result = await notebook_source_fulltext(notebook_id="nb_001", source_id="src_001")
        assert result == "Full text content here"

    async def test_source_fulltext_runtime_error(self):
        from notebooklm_mcp.server import notebook_source_fulltext

        with patch(
            "notebooklm_mcp.server.get_client",
            new=AsyncMock(side_effect=RuntimeError("no auth")),
        ):
            result = await notebook_source_fulltext(notebook_id="nb_001", source_id="src_001")
        assert result.startswith("ERROR:")

    async def test_source_fulltext_empty_content(self, patched_get_client):
        patched_get_client.sources.get_fulltext = AsyncMock(return_value=_fake_fulltext(""))
        from notebooklm_mcp.server import notebook_source_fulltext

        result = await notebook_source_fulltext(notebook_id="nb_001", source_id="src_001")
        assert result == ""


# ---------------------------------------------------------------------------
# Tests: deep research prompt
# ---------------------------------------------------------------------------


class TestDeepResearchPrompt:
    def test_prompt_contains_topic(self):
        from notebooklm_mcp.server import notebooklm_deep_research

        result = notebooklm_deep_research(
            topic="quantum computing", urls="https://a.com,https://b.com"
        )
        assert "quantum computing" in result

    def test_prompt_contains_urls(self):
        from notebooklm_mcp.server import notebooklm_deep_research

        result = notebooklm_deep_research(topic="AI safety", urls="https://a.com, https://b.com")
        # Check URLs appear as individual bullet entries, validated by parsed components
        bullet_urls = [
            ln.strip().lstrip("- ").strip()
            for ln in result.splitlines()
            if ln.strip().startswith("- http")
        ]
        parsed_urls = [urlparse(u) for u in bullet_urls]
        assert any(p.scheme == "https" and p.hostname == "a.com" for p in parsed_urls)
        assert any(p.scheme == "https" and p.hostname == "b.com" for p in parsed_urls)

    def test_prompt_has_all_steps(self):
        from notebooklm_mcp.server import notebooklm_deep_research

        result = notebooklm_deep_research(topic="test", urls="https://x.com")
        for step in ("Step 1", "Step 2", "Step 3", "Step 4", "Step 5"):
            assert step in result

    def test_prompt_single_url(self):
        from notebooklm_mcp.server import notebooklm_deep_research

        result = notebooklm_deep_research(topic="topic", urls="https://only.com")
        # Check the URL appears as a bullet entry
        bullet_urls = [
            ln.strip().lstrip("- ").strip()
            for ln in result.splitlines()
            if ln.strip().startswith("- http")
        ]
        # The string https://only.com may be at an arbitrary position in the sanitized URL.
        parsed_urls = [urlparse(u) for u in bullet_urls]
        assert any(p.scheme == "https" and p.hostname == "only.com" for p in parsed_urls)
    def test_prompt_returns_string(self):
        from notebooklm_mcp.server import notebooklm_deep_research

        result = notebooklm_deep_research(topic="t", urls="https://u.com")
        assert isinstance(result, str)
        assert len(result) > 0


# ---------------------------------------------------------------------------
# Tests: get_client singleton
# ---------------------------------------------------------------------------


class TestGetClientSingleton:
    async def test_returns_same_instance_on_repeated_calls(self):
        """get_client() must return the same singleton on repeated calls."""
        import notebooklm_mcp.server as srv

        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)

        call_count = 0

        async def _fake_from_storage():
            nonlocal call_count
            call_count += 1
            return mock_client

        with patch("notebooklm.NotebookLMClient.from_storage", new=_fake_from_storage):
            c1 = await srv.get_client()
            c2 = await srv.get_client()

        assert c1 is c2
        assert call_count == 1

    async def test_runtime_error_when_notebooklm_not_installed(self):
        from notebooklm_mcp.server import _reset_client_for_testing

        _reset_client_for_testing()
        with patch.dict("sys.modules", {"notebooklm": None}):
            import notebooklm_mcp.server as srv

            with pytest.raises(RuntimeError, match="notebooklm-py is not installed"):
                await srv.get_client()

    async def test_shutdown_client_clears_singleton(self):
        import notebooklm_mcp.server as srv

        mock_client = MagicMock()
        mock_client.__aexit__ = AsyncMock(return_value=None)
        # Set via the internal attribute (unavoidable for this specific test)
        srv._client_instance = mock_client

        await srv._shutdown_client()
        assert srv._client_instance is None

    async def test_shutdown_client_noop_when_none(self):
        from notebooklm_mcp.server import _reset_client_for_testing, _shutdown_client

        _reset_client_for_testing()
        # Must not raise
        await _shutdown_client()
