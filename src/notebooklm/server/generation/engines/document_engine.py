from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..base import ContentGenerator, GeneratedContent, PreviewResult, TemplateInfo


@dataclass
class DocumentGenerator(ContentGenerator):
    content_type: str = "document"
    notebooklm_client: Any = None
    media_root: Path = field(default_factory=lambda: Path.home() / ".notebooklm" / "data")

    async def generate(
        self,
        notebook_id: str,
        prompt: str,
        template: str | None = None,
        options: dict | None = None,
    ) -> GeneratedContent:
        opts = options or {}
        doc_format = opts.get("format", "markdown")
        template_name = template or "note"
        title = opts.get("title", f"Document {uuid.uuid4().hex[:8]}")

        artifact_type = {
            "note": "NOTE",
            "summary": "SUMMARY",
            "faq": "FAQ",
            "study_guide": "STUDY_GUIDE",
            "briefing_doc": "BRIEFING_DOC",
            "outline": "OUTLINE",
            "timeline": "TIMELINE",
        }.get(template_name, "NOTE")

        if self.notebooklm_client is not None:
            artifact = await self.notebooklm_client.artifacts.generate(
                notebook_id=notebook_id,
                prompt=prompt,
                artifact_type=artifact_type,
            )
            content = getattr(artifact, "content", str(artifact))
        else:
            content = f"[Mock] {artifact_type} generated for: {prompt}"

        user_dir = self.media_root / "generated" / str(opts.get("user_id", 0)) / notebook_id
        user_dir.mkdir(parents=True, exist_ok=True)

        file_ext = ".md" if doc_format == "markdown" else ".pdf"
        filename = f"doc_{uuid.uuid4().hex[:8]}{file_ext}"
        file_path = user_dir / filename
        file_path.write_text(content, encoding="utf-8")

        return GeneratedContent(
            id=0,
            content_type="document",
            title=title,
            status="completed",
            local_file_path=str(file_path),
            file_size=len(content.encode("utf-8")),
            content=content,
            metadata={
                "format": doc_format,
                "template": template_name,
                "artifact_type": artifact_type,
                "doc_page_count": len(content.splitlines()),
                "doc_sections": json.dumps([{"title": title}]),
            },
        )

    async def preview(
        self,
        notebook_id: str,
        prompt: str,
        template: str | None = None,
    ) -> PreviewResult:
        return PreviewResult(
            content_type="document",
            outline=[{"title": prompt, "type": "section"}],
            estimated_pages=1,
        )

    async def get_supported_templates(self) -> list[TemplateInfo]:
        return [
            TemplateInfo(name="note", label="笔记", description="自由格式笔记"),
            TemplateInfo(name="summary", label="摘要", description="文档摘要"),
            TemplateInfo(name="faq", label="常见问题", description="FAQ 生成"),
            TemplateInfo(name="study_guide", label="学习指南", description="考试复习材料"),
            TemplateInfo(name="briefing_doc", label="简报", description="简报文档"),
            TemplateInfo(name="outline", label="大纲", description="文档大纲"),
            TemplateInfo(name="timeline", label="时间线", description="事件时间线"),
        ]
