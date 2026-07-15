from __future__ import annotations

import pytest

from notebooklm.server.generation.engines.infographic_engine import InfographicGenerator


class TestInfographicGenerator:
    @pytest.fixture
    def gen(self) -> InfographicGenerator:
        return InfographicGenerator()

    @pytest.mark.asyncio
    async def test_content_type(self, gen: InfographicGenerator) -> None:
        assert gen.content_type == "infographic"

    @pytest.mark.asyncio
    async def test_generate_returns_png(self, gen: InfographicGenerator) -> None:
        result = await gen.generate("nb-1", "Create infographic", options={"title": "Test Info"})
        assert result.status == "completed"
        assert result.local_file_path.endswith(".png")
        assert result.file_size > 0

    @pytest.mark.asyncio
    async def test_preview_returns_outline(self, gen: InfographicGenerator) -> None:
        result = await gen.preview("nb-1", "Preview")
        assert result.content_type == "infographic"

    @pytest.mark.asyncio
    async def test_get_supported_templates(self, gen: InfographicGenerator) -> None:
        templates = await gen.get_supported_templates()
        assert len(templates) >= 1
