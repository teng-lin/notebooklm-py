from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..base import ContentGenerator, GeneratedContent, PreviewResult, TemplateInfo
from ..extractors.source_extractor import SourceExtractor


@dataclass
class MindmapGenerator(ContentGenerator):
    content_type: str = "mindmap"
    notebooklm_client: Any = None
    media_root: Path = field(default_factory=lambda: Path.home() / ".notebooklm" / "data")
    _templates: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._load_templates()

    def _load_templates(self) -> None:
        template_dir = Path(__file__).resolve().parent.parent / "templates" / "mindmap"
        if template_dir.is_dir():
            for f in sorted(template_dir.glob("*.json")):
                try:
                    self._templates.append(json.loads(f.read_text(encoding="utf-8")))
                except Exception:
                    pass

    def _get_template(self, name: str) -> dict:
        for t in self._templates:
            if t["name"] == name:
                return dict(t)
        return {
            "name": "default",
            "layout": "tree",
            "node_color": "#4A90D9",
            "export_width": 1920,
            "export_height": 1080,
        }

    async def generate(
        self,
        notebook_id: str,
        prompt: str,
        template: str | None = None,
        options: dict | None = None,
    ) -> GeneratedContent:
        opts = options or {}
        title = opts.get("title", f"Mindmap {uuid.uuid4().hex[:8]}")
        user_id = opts.get("user_id", 0)
        template_name = template or "default"
        tmpl = self._get_template(template_name)

        source_text = opts.get("source_text", prompt)
        hierarchy = await SourceExtractor.build_hierarchy(source_text)

        mindmap_data = {
            "meta": {"name": title, "author": "", "version": "1.0"},
            "format": "node_tree",
            "data": hierarchy,
            "template": tmpl,
        }

        user_dir = self.media_root / "generated" / str(user_id) / notebook_id
        user_dir.mkdir(parents=True, exist_ok=True)

        json_filename = f"mindmap_{uuid.uuid4().hex[:8]}.json"
        json_path = user_dir / json_filename
        json_path.write_text(json.dumps(mindmap_data, ensure_ascii=False), encoding="utf-8")

        return GeneratedContent(
            id=0,
            content_type="mindmap",
            title=title,
            status="completed",
            local_file_path=str(json_path),
            file_size=json_path.stat().st_size,
            content=json.dumps(hierarchy, ensure_ascii=False),
            metadata={
                "mindmap_data": json.dumps(mindmap_data, ensure_ascii=False),
                "mindmap_layout": tmpl.get("layout", "tree"),
            },
        )

    async def preview(
        self,
        notebook_id: str,
        prompt: str,
        template: str | None = None,
    ) -> PreviewResult:
        return PreviewResult(
            content_type="mindmap",
            outline=[{"title": "根节点"}, {"title": "子节点 1"}, {"title": "子节点 2"}],
            estimated_pages=1,
        )

    async def get_supported_templates(self) -> list[TemplateInfo]:
        if not self._templates:
            return [
                TemplateInfo(name="default", label="默认", description="树形布局思维导图"),
                TemplateInfo(name="radial", label="辐射", description="辐射状布局思维导图"),
            ]
        return [
            TemplateInfo(
                name=t["name"],
                label=t.get("label", t["name"]),
                description=t.get("description", ""),
            )
            for t in self._templates
        ]
