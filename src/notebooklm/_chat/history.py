"""Server-backed conversation history helpers."""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Any, Protocol

from .._row_adapters.chat import ConversationTurnRow, unwrap_conversation_turns
from ..exceptions import ChatError

_TURN_COUNT_INITIAL_LIMIT = 100
_TURN_COUNT_MAX_LIMIT = 12_800


class _TurnFetcher(Protocol):
    def __call__(
        self,
        notebook_id: str,
        conversation_id: str,
        limit: int = 2,
    ) -> Awaitable[Any]: ...


async def count_prior_server_turns(
    fetch_turns: _TurnFetcher,
    notebook_id: str,
    conversation_id: str,
) -> int:
    """Count user-question turns from a complete newest-first snapshot.

    ``khqZz`` returns individual role rows. A raw row count cannot be halved
    safely because a bounded newest-first window can start with an unpaired
    answer. Grow the requested window until the response is complete, then
    count only rows classified as user questions.

    Fetch failures and malformed truthy containers propagate; treating either
    as zero would fabricate a fresh conversation and an incorrect ordinal.
    """
    limit = _TURN_COUNT_INITIAL_LIMIT
    while True:
        turns_data = await fetch_turns(notebook_id, conversation_id, limit=limit)
        turns = unwrap_conversation_turns(turns_data, source="_chat.ask.turn_count")
        if len(turns) < limit:
            return sum(
                ConversationTurnRow(turn).role == ConversationTurnRow.ROLE_QUESTION
                for turn in turns
            )
        if limit >= _TURN_COUNT_MAX_LIMIT:
            raise ChatError(
                f"Conversation history filled the maximum {_TURN_COUNT_MAX_LIMIT:,}-row snapshot; "
                "cannot derive an authoritative turn number."
            )
        limit *= 2
