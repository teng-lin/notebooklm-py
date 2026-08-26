"""Raw-payload conversation-history helpers retained for compatibility callers.

Split out of :mod:`.history` so that module holds records only. Both helpers
here take an undecoded ``khqZz`` (``GET_CONVERSATION_TURNS``) payload and decode
it through ``_row_adapters`` plus the codec's answer-content descent, so they
carry the decode-layer dependency the record half must not have.

Their only production reader is the compatibility static method
``ChatAPI._parse_turns_to_qa_pairs``; the live ``ask`` path uses the record
helpers instead.
"""

from __future__ import annotations

import logging
import reprlib
from collections.abc import Awaitable
from typing import Any, Protocol

from .._row_adapters.chat import ConversationTurnRow, unwrap_conversation_turns
from .._web.codec.chat_stream import _extract_next_turn_content
from ..exceptions import ChatError

_TURN_COUNT_INITIAL_LIMIT = 100
_TURN_COUNT_MAX_LIMIT = 12_800

logger = logging.getLogger("notebooklm._chat.api")


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


def parse_legacy_turns_to_qa_pairs(turns_data: Any) -> list[tuple[str, str]]:
    """Compatibility parser for callers of the historical private static helper."""
    turns = unwrap_conversation_turns(turns_data, source="_chat._parse_turns_to_qa_pairs")
    pairs: list[tuple[str, str]] = []
    index = 0
    while index < len(turns):
        turn = ConversationTurnRow(turns[index])
        if not turn.is_well_formed:
            logger.debug(
                "_parse_turns_to_qa_pairs: skipping malformed turn at index %d: %s",
                index,
                reprlib.repr(turns[index]),
            )
            index += 1
            continue
        if turn.has_unrecognized_role:
            logger.debug(
                "_parse_turns_to_qa_pairs: unrecognized role code %r at turn %d — skipping; "
                "possible role-slot drift: %s",
                turn.role,
                index,
                reprlib.repr(turns[index]),
            )
            index += 1
            continue
        if turn.is_question:
            answer = ""
            if index + 1 < len(turns):
                next_turn = ConversationTurnRow(turns[index + 1])
                if next_turn.is_answer:
                    answer = str(_extract_next_turn_content(next_turn.raw) or "")
                    index += 1
            pairs.append((turn.question_text, answer))
        index += 1
    return pairs


__all__ = [
    "count_prior_server_turns",
    "parse_legacy_turns_to_qa_pairs",
]
