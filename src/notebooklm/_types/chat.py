"""Private chat type implementations."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from ..rpc.types import ChatGoal, ChatResponseLength


class ChatMode(Enum):
    """Predefined chat modes for common use cases."""

    DEFAULT = "default"  # General purpose
    LEARNING_GUIDE = "learning_guide"  # Educational focus
    CONCISE = "concise"  # Brief responses
    DETAILED = "detailed"  # Verbose responses


@dataclass
class ConversationTurn:
    """Represents a single turn in a conversation."""

    query: str
    answer: str
    turn_number: int


@dataclass(frozen=True)
class ChatSettings:
    """A notebook's current chat configuration, read from the server (#1751).

    Returned by :meth:`ChatAPI.get_settings`. Lets a partial ``configure`` merge
    (read-modify-write) instead of clobbering the fields it doesn't touch — the
    server stores the whole chat-settings block with no merge, so preserving an
    omitted field requires reading the current value first.

    Attributes:
        goal: The active chat goal/persona (``ChatGoal``). ``DEFAULT`` when the
            notebook has never been configured.
        response_length: The active response verbosity (``ChatResponseLength``).
            ``DEFAULT`` when never configured.
        custom_prompt: The active custom-persona text — populated only when
            ``goal`` is ``CUSTOM``, else ``None``. (The server may retain an
            inactive prompt draft under a non-CUSTOM goal; it is not surfaced
            here because it is not the active persona.)
    """

    goal: ChatGoal
    response_length: ChatResponseLength
    custom_prompt: str | None = None


@dataclass
class ChatReference:
    """A reference/citation in a chat response.

    References link parts of the answer to specific sources.
    When you click a reference in the NotebookLM UI, it shows
    the relevant passage from the source.

    Attributes:
        source_id: The source UUID this reference points to.
        citation_number: The citation number shown in the answer (e.g., [1], [2]).
            Assigned client-side in answer-array order; this is the marker that
            appears inline in the answer text.
        cited_text: The actual text passage from the source being cited.
            Reliably populated for content-bearing citations (empirically ~95%
            of refs have ``len(cited_text) ≈ end_char - start_char``). May be
            ``None`` for structural-anchor citations (single-char source ranges
            at page/section boundaries, image/infobox refs) — the server has no
            plaintext to deliver for those.
        start_char: Start character position in the source's chunked index
            (if available). NOT a position in ``SourceFulltext.content``.
        end_char: End character position in the source's chunked index.
        chunk_id: Internal chunk ID (for debugging, not user-facing).
        passage_id: Forward-compatibility slot for the per-passage UUID
            that NotebookLM's web UI sends in its saved-from-chat
            CREATE_NOTE payload (issue #660). The streaming chat response
            does NOT currently expose this UUID, so it stays ``None`` in
            production. ``build_save_chat_as_note_params`` falls back to
            ``chunk_id`` when it's unset.
        answer_start_char: Start position in the *answer text* of the span that
            this citation supports. Distinct from ``start_char`` (which is
            source-side). Useful for highlighting the supported span in a UI.
            ``None`` if the server omitted it.
        answer_end_char: End position in the answer text (exclusive).
        score: Server-side relevance score for this citation, 0.0-1.0.
            Typically observed in the 0.6-0.7 range. ``None`` if the server
            omitted it.
    """

    source_id: str
    citation_number: int | None = None
    cited_text: str | None = None
    start_char: int | None = None
    end_char: int | None = None
    chunk_id: str | None = None
    passage_id: str | None = None
    answer_start_char: int | None = None
    answer_end_char: int | None = None
    score: float | None = None

    def __post_init__(self) -> None:
        """Validate paired-offset invariants at construction time.

        ``start_char``/``end_char`` and ``answer_start_char``/``answer_end_char``
        are semantically paired ranges: each pair is either fully populated or
        fully ``None``, and start must not exceed end. The streamed-chat parser
        already produces values that satisfy this contract; this check catches
        bad hand-constructed instances at the dataclass boundary instead of
        leaking the half-populated state into downstream consumers.

        Raises:
            ValueError: when either pair is half-populated, or when start
                exceeds end on either pair.
        """
        if (self.start_char is None) != (self.end_char is None):
            raise ValueError(
                "ChatReference start_char/end_char must both be set or both None "
                f"(got start_char={self.start_char!r}, end_char={self.end_char!r})"
            )
        if (self.answer_start_char is None) != (self.answer_end_char is None):
            raise ValueError(
                "ChatReference answer_start_char/answer_end_char must both be set or both None "
                f"(got answer_start_char={self.answer_start_char!r}, "
                f"answer_end_char={self.answer_end_char!r})"
            )
        if (
            self.start_char is not None
            and self.end_char is not None
            and self.start_char > self.end_char
        ):
            raise ValueError(
                f"ChatReference start_char ({self.start_char}) > end_char ({self.end_char})"
            )
        if (
            self.answer_start_char is not None
            and self.answer_end_char is not None
            and self.answer_start_char > self.answer_end_char
        ):
            raise ValueError(
                f"ChatReference answer_start_char ({self.answer_start_char}) "
                f"> answer_end_char ({self.answer_end_char})"
            )


@dataclass
class AskResult:
    """Result of asking the notebook a question.

    Attributes:
        answer: The AI-generated answer text.
        conversation_id: UUID for this conversation (used for follow-ups).
        turn_number: The turn number in the conversation.
        is_follow_up: Whether this was a follow-up question.
        references: List of source references cited in the answer.
        raw_response: First 1000 chars of raw API response (for debugging).
    """

    answer: str
    conversation_id: str
    turn_number: int
    is_follow_up: bool
    references: list[ChatReference] = field(default_factory=list)
    raw_response: str = ""
