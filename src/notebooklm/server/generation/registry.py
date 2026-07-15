from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .base import ContentGenerator


_GENERATORS: dict[str, type[ContentGenerator]] = {}


class GeneratorRegistry:

    @staticmethod
    def register(content_type: str, generator_class: type[ContentGenerator]) -> None:
        _GENERATORS[content_type] = generator_class

    @staticmethod
    def create(content_type: str, notebooklm_client: object | None = None) -> ContentGenerator:
        cls = _GENERATORS.get(content_type)
        if cls is None:
            msg = f"Unknown content type: {content_type!r}. Available: {list(_GENERATORS)}"
            raise ValueError(msg)
        return cls(notebooklm_client=notebooklm_client) if notebooklm_client is not None else cls()

    @staticmethod
    def list_types() -> list[str]:
        return list(_GENERATORS)
