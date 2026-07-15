from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class TemplateInfo:
    name: str
    label: str
    description: str = ""
    preview_image: str = ""


@dataclass
class PreviewResult:
    content_type: str
    outline: list[dict[str, Any]] = field(default_factory=list)
    estimated_pages: int = 0
    estimated_duration_seconds: int = 0
    warning: str = ""


@dataclass
class GeneratedContent:
    id: int = 0
    content_type: str = ""
    title: str = ""
    status: str = "processing"
    local_file_path: str = ""
    file_size: int = 0
    thumbnail_path: str = ""
    content: str = ""
    metadata: dict = field(default_factory=dict)
    error_message: str = ""


class ContentGenerator(ABC):
    @property
    @abstractmethod
    def content_type(self) -> str: ...

    @abstractmethod
    async def generate(
        self,
        notebook_id: str,
        prompt: str,
        template: str | None = None,
        options: dict | None = None,
    ) -> GeneratedContent: ...

    @abstractmethod
    async def preview(
        self,
        notebook_id: str,
        prompt: str,
        template: str | None = None,
    ) -> PreviewResult: ...

    @abstractmethod
    async def get_supported_templates(self) -> list[TemplateInfo]: ...
