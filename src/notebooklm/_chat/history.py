"""Record-based conversation-history helpers.

Everything here consumes :mod:`notebooklm._records` values and nothing else:
no row adapters, no wire vocabulary, no web package. That is deliberate — the
live ``ask`` path (turn counting and question/answer pairing) reads only this
module, so the chat workflow service can depend on it without acquiring a
transitive dependency on the decode layer.

The legacy half — the two helpers that still take raw ``khqZz`` payloads and
decode them through ``_row_adapters`` — lives in :mod:`.history_legacy`.
"""

from __future__ import annotations

import logging
import reprlib
from collections.abc import Awaitable
from typing import Protocol

from .._records import ChatGetHistoryResult, ChatTurnDecodeErrorRecord
from ..exceptions import ChatError, UnknownRPCMethodError

_TURN_COUNT_INITIAL_LIMIT = 100
_TURN_COUNT_MAX_LIMIT = 12_800

logger = logging.getLogger("notebooklm._chat.api")


class _RecordTurnFetcher(Protocol):
    def __call__(
        self,
        notebook_id: str,
        conversation_id: str,
        *,
        limit: int = 2,
    ) -> Awaitable[ChatGetHistoryResult]: ...


async def count_prior_recorded_turns(
    fetch_turns: _RecordTurnFetcher,
    notebook_id: str,
    conversation_id: str,
) -> int:
    """Count question roles from complete typed newest-first snapshots."""
    limit = _TURN_COUNT_INITIAL_LIMIT
    while True:
        result = await fetch_turns(notebook_id, conversation_id, limit=limit)
        if len(result.turns) < limit:
            return sum(turn.is_question_role for turn in result.turns)
        if limit >= _TURN_COUNT_MAX_LIMIT:
            raise ChatError(
                f"Conversation history filled the maximum {_TURN_COUNT_MAX_LIMIT:,}-row snapshot; "
                "cannot derive an authoritative turn number."
            )
        limit *= 2


def _raise_turn_error(error: ChatTurnDecodeErrorRecord) -> None:
    raise UnknownRPCMethodError(
        error.message,
        method_id=error.method_id,
        path=error.path,
        source=error.source,
        found_ids=list(error.found_ids),
        raw_response=error.raw_response,
        data_at_failure=error.data_at_failure,
        rpc_code=error.rpc_code,
    )


def parse_recorded_turns_to_qa_pairs(
    result: ChatGetHistoryResult,
    *,
    oldest_first: bool = False,
) -> list[tuple[str, str]]:
    """Pair typed turn records without re-decoding their compatibility rows."""
    turns = list(reversed(result.turns)) if oldest_first else list(result.turns)
    pairs: list[tuple[str, str]] = []
    index = 0
    while index < len(turns):
        turn = turns[index]
        if not turn.is_well_formed:
            logger.debug(
                "_parse_turns_to_qa_pairs: skipping malformed turn at index %d: %s",
                index,
                reprlib.repr(turn.legacy_row),
            )
            index += 1
            continue
        if turn.has_unrecognized_role:
            logger.debug(
                "_parse_turns_to_qa_pairs: unrecognized role code %r at turn %d — skipping; "
                "possible role-slot drift: %s",
                turn.role,
                index,
                reprlib.repr(turn.legacy_row),
            )
            index += 1
            continue
        if turn.is_question:
            answer = ""
            if index + 1 < len(turns):
                next_turn = turns[index + 1]
                if next_turn.is_answer:
                    if next_turn.answer_error is not None:
                        _raise_turn_error(next_turn.answer_error)
                    answer = next_turn.answer_text or ""
                    index += 1
            pairs.append((turn.question_text, answer))
        index += 1
    return pairs


__all__ = [
    "count_prior_recorded_turns",
    "parse_recorded_turns_to_qa_pairs",
]
