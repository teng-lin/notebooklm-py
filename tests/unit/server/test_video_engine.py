from __future__ import annotations

import pytest

from notebooklm.server.generation.engines.video_engine import VideoGenerator


class TestVideoGenerator:
    @pytest.fixture
    def gen(self) -> VideoGenerator:
        return VideoGenerator()

    @pytest.mark.asyncio
    async def test_content_type(self, gen: VideoGenerator) -> None:
        assert gen.content_type == "video"

    @pytest.mark.asyncio
    async def test_generate_returns_video(self, gen: VideoGenerator) -> None:
        result = await gen.generate(
            "nb-1",
            "Create video about AI",
            options={
                "title": "AI Video",
                "scenes": [
                    {"text": "Introduction to AI", "type": "title"},
                    {"text": "Deep Learning", "type": "content"},
                ],
            },
        )
        assert result.status == "completed"
        assert result.metadata["video_duration_seconds"] > 0

    @pytest.mark.asyncio
    async def test_preview_returns_outline(self, gen: VideoGenerator) -> None:
        result = await gen.preview("nb-1", "Preview")
        assert result.content_type == "video"

    @pytest.mark.asyncio
    async def test_get_supported_templates(self, gen: VideoGenerator) -> None:
        templates = await gen.get_supported_templates()
        assert len(templates) >= 1
