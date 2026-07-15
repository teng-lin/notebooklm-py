from __future__ import annotations

import pytest

from notebooklm.server.generation.engines.document_engine import DocumentGenerator
from notebooklm.server.generation.engines.podcast_engine import PodcastGenerator


class TestDocumentGenerator:

    @pytest.fixture
    def gen(self) -> DocumentGenerator:
        return DocumentGenerator()

    @pytest.mark.asyncio
    async def test_content_type(self, gen: DocumentGenerator) -> None:
        assert gen.content_type == "document"

    @pytest.mark.asyncio
    async def test_generate_returns_content(self, gen: DocumentGenerator) -> None:
        result = await gen.generate("nb-1", "Write a summary", template="summary")
        assert result.status == "completed"
        assert result.content_type == "document"
        assert "Summary" in result.content or "Mock" in result.content

    @pytest.mark.asyncio
    async def test_generate_without_client_returns_mock(self, gen: DocumentGenerator) -> None:
        result = await gen.generate("nb-1", "test prompt")
        assert "[Mock]" in result.content

    @pytest.mark.asyncio
    async def test_preview_returns_outline(self, gen: DocumentGenerator) -> None:
        result = await gen.preview("nb-1", "Preview")
        assert result.content_type == "document"

    @pytest.mark.asyncio
    async def test_get_supported_templates(self, gen: DocumentGenerator) -> None:
        templates = await gen.get_supported_templates()
        assert len(templates) >= 3
        names = [t.name for t in templates]
        assert "note" in names
        assert "summary" in names


class TestPodcastGenerator:

    @pytest.fixture
    def gen(self) -> PodcastGenerator:
        return PodcastGenerator()

    @pytest.mark.asyncio
    async def test_content_type(self, gen: PodcastGenerator) -> None:
        assert gen.content_type == "podcast"

    @pytest.mark.asyncio
    async def test_generate_returns_audio(self, gen: PodcastGenerator) -> None:
        result = await gen.generate("nb-1", "Discuss AI", template="deep_dive")
        assert result.status == "completed"
        assert result.content_type == "podcast"
        assert "[Mock transcript" in result.content

    @pytest.mark.asyncio
    async def test_preview_returns_outline(self, gen: PodcastGenerator) -> None:
        result = await gen.preview("nb-1", "Preview")
        assert result.estimated_duration_seconds > 0

    @pytest.mark.asyncio
    async def test_get_supported_templates(self, gen: PodcastGenerator) -> None:
        templates = await gen.get_supported_templates()
        assert len(templates) >= 2
