from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from ..base import ContentGenerator, GeneratedContent, PreviewResult, TemplateInfo


@dataclass
class InfographicGenerator(ContentGenerator):
    content_type: str = "infographic"
    notebooklm_client: Any = None
    media_root: Path = field(default_factory=lambda: Path.home() / ".notebooklm" / "data")
    _templates: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._load_templates()

    def _load_templates(self) -> None:
        template_dir = Path(__file__).resolve().parent.parent / "templates" / "infographic"
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
            "width": 800,
            "height": 2000,
            "background_color": "#FFFFFF",
            "title_color": "#1a365d",
            "body_color": "#2d3748",
            "font_family": "Microsoft YaHei",
        }

    def _render_block(
        self,
        draw: ImageDraw.Draw,
        block: dict,
        x: int,
        y: int,
        w: int,
        h: int,
        colors: dict[str, str],
    ) -> None:
        btype = block.get("type", "text")
        content = block.get("content", "")
        if btype == "header":
            draw.rectangle([x, y, x + w, y + h], fill="#1a365d")
            draw.text((x + 20, y + 20), content or "标题", fill="#FFFFFF", font=None)
        elif btype == "text":
            draw.text(
                (x + 20, y + 10), content or "正文内容", fill=colors.get("body", "#333"), font=None
            )
        elif btype == "stats":
            draw.rectangle([x + 20, y + 10, x + w - 20, y + h - 10], outline="#3182CE", width=2)
            draw.text((x + 40, y + 20), content or "统计数据", fill="#3182CE", font=None)
        elif btype == "divider":
            draw.line([(x + 40, y + h // 2), (x + w - 40, y + h // 2)], fill="#E2E8F0", width=2)
        elif btype == "footer":
            draw.rectangle([x, y, x + w, y + h], fill="#EDF2F7")
            draw.text((x + 20, y + 10), content or "页脚", fill="#333", font=None)

    async def generate(
        self,
        notebook_id: str,
        prompt: str,
        template: str | None = None,
        options: dict | None = None,
    ) -> GeneratedContent:
        opts = options or {}
        title = opts.get("title", f"Infographic {uuid.uuid4().hex[:8]}")
        user_id = opts.get("user_id", 0)
        template_name = template or "default"
        tmpl = self._get_template(template_name)

        user_dir = self.media_root / "generated" / str(user_id) / notebook_id
        user_dir.mkdir(parents=True, exist_ok=True)

        width = tmpl.get("width", 800)
        height = tmpl.get("height", 2000)
        bg_color_str = tmpl.get("background_color", "#FFFFFF")

        img = Image.new("RGB", (width, height), bg_color_str)
        draw = ImageDraw.Draw(img)

        blocks = tmpl.get("blocks", [])
        block_height_total = sum(b.get("height_ratio", 0.1) for b in blocks)
        y_offset = 0
        for block in blocks:
            hr = block.get("height_ratio", 0.1)
            bh = (
                int(height * hr / block_height_total)
                if block_height_total > 0
                else int(height * hr)
            )
            block["content"] = ""
            self._render_block(
                draw,
                block,
                0,
                y_offset,
                width,
                bh,
                {
                    "title": tmpl.get("title_color", "#1a365d"),
                    "body": tmpl.get("body_color", "#2d3748"),
                    "accent": tmpl.get("accent_color", "#3182CE"),
                },
            )
            y_offset += bh

        png_filename = f"infographic_{uuid.uuid4().hex[:8]}.png"
        png_path = user_dir / png_filename
        img.save(str(png_path), "PNG")

        return GeneratedContent(
            id=0,
            content_type="infographic",
            title=title,
            status="completed",
            local_file_path=str(png_path),
            file_size=png_path.stat().st_size,
            content=json.dumps(
                {"template": template_name, "blocks": len(blocks)}, ensure_ascii=False
            ),
            metadata={
                "infographic_template": template_name,
                "infographic_blocks": json.dumps(blocks, ensure_ascii=False),
            },
        )

    async def preview(
        self,
        notebook_id: str,
        prompt: str,
        template: str | None = None,
    ) -> PreviewResult:
        return PreviewResult(content_type="infographic", estimated_pages=1)

    async def get_supported_templates(self) -> list[TemplateInfo]:
        if not self._templates:
            return [
                TemplateInfo(name="default", label="默认信息图", description="简洁三栏信息图模板")
            ]
        return [
            TemplateInfo(
                name=t["name"],
                label=t.get("label", t["name"]),
                description=t.get("description", ""),
            )
            for t in self._templates
        ]
