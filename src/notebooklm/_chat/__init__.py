"""Backend-neutral private chat package."""

from __future__ import annotations

from typing import Any

from . import api
from .api import ChatAPI


def __getattr__(name: str) -> Any:
    """Lazily preserve the historically importable private turn helper."""
    if name == "_extract_next_turn_content":
        from .._web.rows.chat_stream import _extract_next_turn_content

        return _extract_next_turn_content
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["api", "ChatAPI", "_extract_next_turn_content"]
