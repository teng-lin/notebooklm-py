from __future__ import annotations

import pytest

from notebooklm.server.generation.engines.mindmap_engine import MindmapGenerator


class TestMindmapGenerator:
    @pytest.fixture
    def gen(self) -> MindmapGenerator:
        return MindmapGenerator()

    @pytest.mark.asyncio
    async def test_content_type(self, gen: MindmapGenerator) -> None:
        assert gen.content_type == "mindmap"

    @pytest.mark.asyncio
    async def test_generate_returns_json(self, gen: MindmapGenerator) -> None:
        result = await gen.generate(
            "nb-1",
            "Create mindmap about AI",
            options={"title": "AI Map", "source_text": "# AI\n## ML\n## DL"},
        )
        assert result.status == "completed"
        assert result.local_file_path.endswith(".json")

    @pytest.mark.asyncio
    async def test_generate_builds_hierarchy(self, gen: MindmapGenerator) -> None:
        result = await gen.generate(
            "nb-1",
            "Mindmap",
            options={"source_text": "# Root\n## Child 1\n## Child 2"},
        )
        import json

        data = json.loads(result.content)
        assert data["name"] == "Root"
        assert len(data["children"]) >= 2

    @pytest.mark.asyncio
    async def test_preview_returns_outline(self, gen: MindmapGenerator) -> None:
        result = await gen.preview("nb-1", "Preview")
        assert result.content_type == "mindmap"

    @pytest.mark.asyncio
    async def test_get_supported_templates(self, gen: MindmapGenerator) -> None:
        templates = await gen.get_supported_templates()
        assert len(templates) >= 1
