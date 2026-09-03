"""Shared helpers for live generation tests and their unit coverage."""

from __future__ import annotations

from typing import Any

_TYPED_RATE_LIMIT_ATTR = "_notebooklm_typed_rate_limit"


async def generate_note_mind_map(client: Any, notebook_id: str, operation: Any) -> Any:
    """Close the manual note-map journal operation when typed quota rejects creation."""
    try:
        return await client.artifacts.generate_mind_map(notebook_id)
    except BaseException as exc:
        if bool(getattr(exc, _TYPED_RATE_LIMIT_ATTR, False)):
            operation.rate_limited_rejected()
        raise
