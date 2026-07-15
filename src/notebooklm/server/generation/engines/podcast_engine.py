from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..base import ContentGenerator, GeneratedContent, PreviewResult, TemplateInfo


@dataclass
class PodcastGenerator(ContentGenerator):
    content_type: str = "podcast"
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
        title = opts.get("title", f"Podcast {uuid.uuid4().hex[:8]}")
        user_id = opts.get("user_id", 0)

        if self.notebooklm_client is not None:
            audio_result = await self.notebooklm_client.audio.generate(
                notebook_id=notebook_id,
                prompt=prompt,
            )
            audio_data = getattr(audio_result, "audio_data", b"")
            transcript = getattr(audio_result, "transcript", "")
            duration = getattr(audio_result, "duration_seconds", 0)
        else:
            audio_data = b"mock-audio-data"
            transcript = f"[Mock transcript for: {prompt}]"
            duration = 120

        user_dir = self.media_root / "generated" / str(user_id) / notebook_id
        user_dir.mkdir(parents=True, exist_ok=True)

        audio_filename = f"podcast_{uuid.uuid4().hex[:8]}.mp3"
        audio_path = user_dir / audio_filename
        audio_path.write_bytes(audio_data)

        metadata_filename = f"podcast_{uuid.uuid4().hex[:8]}.json"
        metadata_path = user_dir / metadata_filename
        metadata_path.write_text(
            json.dumps({"transcript": transcript, "duration": duration, "title": title}, ensure_ascii=False),
            encoding="utf-8",
        )

        return GeneratedContent(
            id=0,
            content_type="podcast",
            title=title,
            status="completed",
            local_file_path=str(audio_path),
            file_size=len(audio_data),
            content=transcript,
            metadata={
                "audio_file_path": str(audio_path),
                "duration_seconds": duration,
                "audio_transcript": transcript,
                "audio_speakers": json.dumps([
                    {"name": "Host", "voice": "en-US-Standard-A"},
                    {"name": "Guest", "voice": "en-US-Standard-B"},
                ]),
            },
        )

    async def preview(
        self,
        notebook_id: str,
        prompt: str,
        template: str | None = None,
    ) -> PreviewResult:
        return PreviewResult(
            content_type="podcast",
            outline=[{"title": "Host introduction"}, {"title": "Discussion"}, {"title": "Conclusion"}],
            estimated_duration_seconds=180,
        )

    async def get_supported_templates(self) -> list[TemplateInfo]:
        return [
            TemplateInfo(name="deep_dive", label="深度讨论", description="双人深度对话式播客"),
            TemplateInfo(name="interview", label="采访", description="主持人采访嘉宾形式"),
            TemplateInfo(name="summary", label="摘要播客", description="快速总结式播客"),
        ]
