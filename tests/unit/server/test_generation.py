from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from notebooklm.server.generation.base import (
    ContentGenerator,
    GeneratedContent,
    PreviewResult,
    TemplateInfo,
)
from notebooklm.server.generation.extractors.source_extractor import SourceExtractor
from notebooklm.server.generation.registry import GeneratorRegistry


@dataclass
class FakeGenerator(ContentGenerator):
    content_type: str = "fake"
    notebooklm_client: object = None
    config: dict = field(default_factory=dict)

    async def generate(
        self, notebook_id: str, prompt: str,
        template: str | None = None,
        options: dict | None = None,
    ) -> GeneratedContent:
        return GeneratedContent(
            id=1, content_type="fake", title="Fake Output",
            status="completed", content=f"Generated for: {prompt}",
        )

    async def preview(
        self, notebook_id: str, prompt: str, template: str | None = None,
    ) -> PreviewResult:
        return PreviewResult(content_type="fake", estimated_pages=3)

    async def get_supported_templates(self) -> list[TemplateInfo]:
        return [TemplateInfo(name="default", label="Default Template")]


class TestContentGeneratorBase:

    @pytest.mark.asyncio
    async def test_generate_returns_content(self) -> None:
        g = FakeGenerator()
        result = await g.generate("nb-1", "Make a summary")
        assert result.status == "completed"
        assert "Make a summary" in result.content

    @pytest.mark.asyncio
    async def test_preview_returns_outline(self) -> None:
        g = FakeGenerator()
        result = await g.preview("nb-1", "Preview this")
        assert result.estimated_pages == 3

    @pytest.mark.asyncio
    async def test_get_supported_templates(self) -> None:
        g = FakeGenerator()
        templates = await g.get_supported_templates()
        assert len(templates) == 1
        assert templates[0].name == "default"


class TestGeneratorRegistry:

    def test_register_and_create(self) -> None:
        GeneratorRegistry.register("fake", FakeGenerator)
        g = GeneratorRegistry.create("fake")
        assert isinstance(g, FakeGenerator)

    def test_create_unknown_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown content type"):
            GeneratorRegistry.create("nonexistent")

    def test_list_types(self) -> None:
        types = GeneratorRegistry.list_types()
        assert "fake" in types


SAMPLE_TEXT = """# Introduction
This is the first paragraph of a document.
It contains important information.

## Key Features
Feature one is about speed.
Feature two is about reliability.
Feature three is about scalability.

## Conclusion
The conclusion summarizes everything."""


class TestSourceExtractor:

    @pytest.mark.asyncio
    async def test_extract_from_text_returns_headings(self) -> None:
        result = await SourceExtractor.extract_from_text(SAMPLE_TEXT)
        assert "Introduction" in result.headings[0]
        assert "Key Features" in result.headings[1]
        assert "Conclusion" in result.headings[2]

    @pytest.mark.asyncio
    async def test_extract_from_text_returns_paragraphs(self) -> None:
        result = await SourceExtractor.extract_from_text(SAMPLE_TEXT)
        assert len(result.paragraphs) >= 1

    @pytest.mark.asyncio
    async def test_extract_from_text_returns_key_points(self) -> None:
        result = await SourceExtractor.extract_from_text(SAMPLE_TEXT)
        assert len(result.key_points) > 0

    @pytest.mark.asyncio
    async def test_build_hierarchy_returns_tree(self) -> None:
        tree = await SourceExtractor.build_hierarchy(SAMPLE_TEXT)
        assert tree["name"] == "Introduction"
        assert len(tree["children"]) >= 3
