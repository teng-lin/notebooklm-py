from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ExtractedContent:
    title: str = ""
    headings: list[str] = field(default_factory=list)
    paragraphs: list[str] = field(default_factory=list)
    key_points: list[str] = field(default_factory=list)
    raw_text: str = ""


class SourceExtractor:
    @staticmethod
    async def extract_from_text(text: str, title: str = "") -> ExtractedContent:
        paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
        lines = text.splitlines()
        headings = [l.strip().strip("#").strip() for l in lines if l.strip().startswith("#")]
        sentences = []
        for p in paragraphs:
            for s in p.replace("! ", ". ").replace("? ", ". ").split(". "):
                s = s.strip()
                if len(s) > 20:
                    sentences.append(s)
        key_points = sentences[:5]
        return ExtractedContent(
            title=title or (headings[0] if headings else ""),
            headings=headings,
            paragraphs=paragraphs,
            key_points=key_points,
            raw_text=text,
        )

    @staticmethod
    async def extract_from_sources(source_texts: list[str]) -> ExtractedContent:
        combined = "\n\n".join(source_texts)
        return await SourceExtractor.extract_from_text(combined)

    @staticmethod
    async def build_hierarchy(text: str) -> dict[str, Any]:
        extracted = await SourceExtractor.extract_from_text(text)
        root: dict[str, Any] = {"name": extracted.title or "Root", "children": []}
        for heading in extracted.headings:
            root["children"].append({"name": heading, "children": []})
        if not root["children"]:
            for point in extracted.key_points:
                root["children"].append({"name": point, "children": []})
        return root
