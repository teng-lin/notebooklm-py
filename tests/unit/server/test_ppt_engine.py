from __future__ import annotations

import pytest

from notebooklm.server.generation.engines.ppt_engine import PptGenerator


class TestPptGenerator:

    @pytest.fixture
    def gen(self) -> PptGenerator:
        return PptGenerator()

    @pytest.mark.asyncio
    async def test_content_type(self, gen: PptGenerator) -> None:
        assert gen.content_type == "ppt"

    @pytest.mark.asyncio
    async def test_generate_creates_pptx(self, gen: PptGenerator) -> None:
        result = await gen.generate(
            "nb-1",
            "AI \u53d1\u5c55\u53f2",
            template="classic",
            options={
                "title": "Test PPT",
                "slides": [
                    {"title": "\u5c01\u9762", "items": ["AI \u53d1\u5c55\u53f2"], "layout": "title_slide"},
                    {"title": "\u65e9\u671f", "items": ["1950s: \u56fe\u7075\u6d4b\u8bd5"], "layout": "content"},
                ],
            },
        )
        assert result.status == "completed"
        assert result.local_file_path.endswith(".pptx")
        assert result.metadata["ppt_page_count"] == 2

    @pytest.mark.asyncio
    async def test_generate_without_slides_creates_default(self, gen: PptGenerator) -> None:
        result = await gen.generate("nb-1", "Default prompt")
        assert result.status == "completed"
        assert result.metadata["ppt_page_count"] >= 1

    @pytest.mark.asyncio
    async def test_preview_returns_outline(self, gen: PptGenerator) -> None:
        result = await gen.preview("nb-1", "Preview PPT")
        assert result.estimated_pages == 4

    @pytest.mark.asyncio
    async def test_get_supported_templates(self, gen: PptGenerator) -> None:
        templates = await gen.get_supported_templates()
        assert len(templates) >= 1
        names = [t.name for t in templates]
        assert "classic" in names
