"""Web request shapes for chat-session state and generation control."""

from __future__ import annotations

from typing import Any


def build_chat_session_status_params(conversation_id: str) -> list[Any]:
    """Build ``GetChatSessionStatus`` params: ``[context, session_id]``."""
    return [None, conversation_id]


def build_cancel_generation_params(conversation_id: str) -> list[Any]:
    """Build the Web app's ``CancelGeneration`` shape."""
    return [None, conversation_id]


__all__ = ["build_cancel_generation_params", "build_chat_session_status_params"]
