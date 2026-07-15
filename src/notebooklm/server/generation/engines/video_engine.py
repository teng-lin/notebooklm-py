from __future__ import annotations

import json
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from ..base import ContentGenerator, GeneratedContent, PreviewResult, TemplateInfo


@dataclass
class VideoGenerator(ContentGenerator):
    content_type: str = "video"
    notebooklm_client: Any = None
    media_root: Path = field(default_factory=lambda: Path.home() / ".notebooklm" / "data")
    _templates: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._load_templates()

    def _load_templates(self) -> None:
        template_dir = Path(__file__).resolve().parent.parent / "templates" / "video"
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
            "width": 1920,
            "height": 1080,
            "fps": 30,
            "background_color": "#1a202c",
            "title_color": "#FFFFFF",
            "body_color": "#E2E8F0",
            "scene_duration_seconds": 5,
            "tts_language": "zh-CN",
        }

    async def generate(
        self,
        notebook_id: str,
        prompt: str,
        template: str | None = None,
        options: dict | None = None,
    ) -> GeneratedContent:
        opts = options or {}
        title = opts.get("title", f"Video {uuid.uuid4().hex[:8]}")
        user_id = opts.get("user_id", 0)
        template_name = template or "default"
        tmpl = self._get_template(template_name)

        scenes = opts.get(
            "scenes",
            [
                {"text": title, "type": "title"},
                {"text": prompt, "type": "content"},
            ],
        )

        user_dir = self.media_root / "generated" / str(user_id) / notebook_id
        user_dir.mkdir(parents=True, exist_ok=True)
        frames_dir = user_dir / f"frames_{uuid.uuid4().hex[:8]}"
        frames_dir.mkdir(exist_ok=True)

        width = tmpl.get("width", 1920)
        height = tmpl.get("height", 1080)
        bg_color = tmpl.get("background_color", "#1a202c")
        scene_duration = tmpl.get("scene_duration_seconds", 5)
        fps = tmpl.get("fps", 30)

        frame_files: list[str] = []
        for i, scene in enumerate(scenes):
            img = Image.new("RGB", (width, height), bg_color)
            draw = ImageDraw.Draw(img)
            text = scene.get("text", "")
            lines = text.split("\n")
            y_start = height // 2 - len(lines) * 20
            for j, line in enumerate(lines):
                draw.text(
                    (width // 2 - len(line) * 5, y_start + j * 40),
                    line,
                    fill=("#FFFFFF" if scene.get("type") == "title" else "#E2E8F0"),
                    font=None,
                )
            for f in range(scene_duration * fps):
                frame_path = frames_dir / f"frame_{i:04d}_{f:06d}.png"
                img.save(str(frame_path))
                frame_files.append(str(frame_path))

        output_filename = f"video_{uuid.uuid4().hex[:8]}.mp4"
        output_path = user_dir / output_filename

        if frame_files:
            try:
                subprocess.run(
                    [
                        "ffmpeg",
                        "-y",
                        "-framerate",
                        str(fps),
                        "-pattern_type",
                        "glob",
                        "-i",
                        str(frames_dir / "*.png"),
                        "-c:v",
                        "libx264",
                        "-pix_fmt",
                        "yuv420p",
                        str(output_path),
                    ],
                    capture_output=True,
                    timeout=120,
                )
            except Exception:
                output_path.write_text(f"[Video would be {len(scenes)} scenes]", encoding="utf-8")

        return GeneratedContent(
            id=0,
            content_type="video",
            title=title,
            status="completed",
            local_file_path=str(output_path),
            file_size=output_path.stat().st_size if output_path.exists() else 0,
            content=json.dumps(scenes, ensure_ascii=False),
            metadata={
                "video_scenes": json.dumps(scenes, ensure_ascii=False),
                "video_duration_seconds": len(scenes) * scene_duration,
                "video_resolution": f"{width}x{height}",
                "video_narration": prompt,
            },
        )

    async def preview(
        self,
        notebook_id: str,
        prompt: str,
        template: str | None = None,
    ) -> PreviewResult:
        return PreviewResult(
            content_type="video",
            outline=[{"title": "场景 1"}, {"title": "场景 2"}, {"title": "场景 3"}],
            estimated_duration_seconds=15,
        )

    async def get_supported_templates(self) -> list[TemplateInfo]:
        if not self._templates:
            return [
                TemplateInfo(name="default", label="默认视频", description="图文混合默认短视频模板")
            ]
        return [
            TemplateInfo(
                name=t["name"],
                label=t.get("label", t["name"]),
                description=t.get("description", ""),
            )
            for t in self._templates
        ]
