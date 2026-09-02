"""Private chat type implementations."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .documents import StructuredDocument
from .enums import ChatGoal, ChatResponseLength, MagicArtifactType


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
class ChatSessionStatus:
    """Whether a server-side chat generation is active for one session.

    Returned by :meth:`ChatAPI.session_status`. ``token`` is Google's opaque
    generation identifier and is populated only while :attr:`generating` is
    true; callers should use it for correlation only, not as a cancellation
    key. Cancellation is addressed by conversation ID.

    Attributes:
        generating: ``True`` while the session is producing an answer.
        token: Opaque generation token while active, otherwise ``None``.
    """

    generating: bool
    token: str | None = None


@dataclass(frozen=True)
class ConversationTurnKey:
    """The backend's three-part identifier for one chat turn (#2122).

    Decoded from ``AnswerResponse.conversationTurnKey`` (proto tag 3 →
    ``answer_row[2]``), which the streamed-chat endpoint sends on **every**
    chunk of every answer. ``SubmitFeedbackRequest.conversationTurnKey``
    (tag 1) is the one consumer of this message in the recovered schema, so a
    caller wanting to build that call no longer has to re-plumb the stream
    parser to find the key.

    The three parts always travel together — a half-populated key addresses
    nothing — so they are modelled as one value object rather than three loose
    optionals on :class:`AskResult`, and :attr:`session_id` is required.

    .. warning::
       :attr:`session_id` is **not** a conversation id, despite carrying one in
       some captures. It is the same wire slot
       ``AnswerRow.server_conversation_id`` reads, which issue #659 established
       is a per-stream identifier: querying ``khqZz`` with it returns 0 turns,
       and passing it back as a follow-up ``conversation_id`` produces a ghost
       turn the server does not record.

       The evidence is genuinely mixed, so this class does not pick a side. In
       a live two-turn probe (2026-08-13) it held the value ``hPTbtc`` returned,
       identical on both turns of one conversation. In this repo's own recorded
       cassettes it differs from the recorded ``hPTbtc`` id in 4/4 chat
       captures. Because it cannot be relied on to be a conversation id, it is
       exposed under its **proto** name and nothing is claimed for it. Use
       :attr:`AskResult.conversation_id` for follow-ups.

    Attributes:
        session_id: Wire slot 0 (proto ``sessionId``). Required — the key is
            addressed by it. Plausibly the ``chatSessionId`` that
            ``GenerateFreeFormStreamedRequest`` (tag 5) and
            ``DeleteChatTurnsRequest`` (tag 2) carry, though nothing here
            confirms that. See the warning above before treating it as a
            conversation id.
        turn_id: Wire slot 1 (proto ``conversationId``). Held a *different*
            UUID on each turn of one conversation in every observation, so it
            identifies the turn rather than the conversation — which is why it
            is NOT named for its proto field.
        turn_code: Wire slot 2 (proto ``fieldType``). An integer, constant
            across every chunk of one turn and different on the next. The
            proto's ``fieldType`` label is the schema extractor's placeholder
            for a name it could not recover, and the observed values are not
            type tags, so this is carried verbatim and NOT interpreted.
    """

    session_id: str
    turn_id: str | None = None
    turn_code: int | None = None

    def __post_init__(self) -> None:
        """Reject a key that addresses nothing.

        The class docstring's reason for existing is that the parts travel
        together; an empty :attr:`session_id` is the one shape that makes the
        whole key unusable, so it is refused at construction rather than left
        for a caller to discover when the RPC it built is rejected. The two
        trailing parts stay optional: they were populated in every observation,
        but a short block is a decode-time absence, not a broken key.
        """
        if not self.session_id:
            raise ValueError("ConversationTurnKey requires a non-empty session_id")


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


#: Deprecated-name -> canonical-name pairs mirrored by
#: :meth:`ChatReference.__setattr__`, in both directions (#2120).
_CHAT_REFERENCE_ALIASES: dict[str, str] = {
    "answer_start_char": "fragment_start_char",
    "answer_end_char": "fragment_end_char",
    "fragment_start_char": "answer_start_char",
    "fragment_end_char": "answer_end_char",
}


def _validate_paired_range(
    start_name: str,
    start: int | None,
    end_name: str,
    end: int | None,
    owner: str,
) -> None:
    """Raise ``ValueError`` unless ``start``/``end`` form a valid optional range.

    A pair is valid when both are ``None``, or both are set, non-negative, and
    ordered. Half-populated, negative and inverted ranges are all rejected:
    none can describe a span, and letting one through pushes the failure into
    whichever consumer resolved it — where it does not look like a failure. A
    negative anchor resolved through ``answer_document.slice()`` is silently
    clamped to the start of the answer, so it would present as a citation
    highlighting the wrong text rather than as an error.

    Matches the range validation on ``DocumentAnnotation`` and friends, which
    these offsets are ultimately resolved against.
    """
    if (start is None) != (end is None):
        raise ValueError(
            f"{owner} {start_name}/{end_name} must both be set or both None "
            f"(got {start_name}={start!r}, {end_name}={end!r})"
        )
    if start is None or end is None:
        return
    if start < 0 or end < 0:
        raise ValueError(
            f"{owner} {start_name}/{end_name} must be non-negative "
            f"(got {start_name}={start!r}, {end_name}={end!r})"
        )
    if start > end:
        raise ValueError(f"{owner} {start_name} ({start}) > {end_name} ({end})")


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
        cited_text: The cited source passage, verbatim — every block of the
            fragment concatenated, with no separators, no stripping and no
            filler. Before #2120 it was truncated to the fragment's first
            block: 37 of 556 characters in the live capture that motivated the
            fix. ``None`` for a structural-anchor citation that decoded no
            blocks at all.

            **Do not derive offsets from its length.** ``len(cited_text)`` is
            *usually* ``end_char - start_char`` and is not guaranteed to be:
            it counts Python characters while the range counts UTF-16 code
            units (so an emoji costs one), and the fragment may span positions
            this client does not render as text (an image, a rule, and for now
            a code block — see ``BlockKind``), which are omitted here rather
            than filled. If you need the span's width, take it from the range
            itself (``end_char - start_char``) rather than from any string —
            ``SourceFulltext.document.slice(start_char, end_char)`` returns
            exactly that range but counts Python characters too, so it is
            ``utf16_len()`` of the slice, not ``len()``, that equals the range.
            This field is the reading optimised for being read.
        start_char: Start of the cited fragment in the **source document's**
            coordinate space — the lowest ``start_index`` among its blocks,
            counted in **UTF-16 code units** (see ``utf16_len``). Resolve it
            with ``SourceFulltext.document.slice(start_char, end_char)``.

            It is **not** interchangeable with the ``offset`` parameter of
            ``source read`` / ``sources.read()``, in two independent ways:
            that offset windows ``SourceFulltext.content``, a different string
            (the legacy flat rendering, whose ``"\n"`` joins the backend never
            counted), and it counts Python characters rather than UTF-16 units.
            Passing one where the other is expected yields a window that looks
            plausible and is wrong. See
            `#2211 <https://github.com/teng-lin/notebooklm-py/issues/2211>`_.
        end_char: End of that range (exclusive) — the highest ``end_index``
            among the fragment's blocks.
        chunk_id: The citation's ``DocumentObject.objectId``. Also the key the
            answer document's annotation map anchors on, which is what joins
            this reference to ``answer_anchor_start`` / ``answer_anchor_end``
            (#2120). Not user-facing.
        passage_id: Forward-compatibility slot for the per-passage UUID
            that NotebookLM's web UI sends in its saved-from-chat
            CREATE_NOTE payload (issue #660). The streaming chat response
            does NOT currently expose this UUID, so it stays ``None`` in
            production. ``build_save_chat_as_note_params`` falls back to
            ``chunk_id`` when it's unset.
        answer_start_char: **Deprecated alias for** ``fragment_start_char``
            (#2120) — despite the name it was never an answer-text position.
        answer_end_char: **Deprecated alias for** ``fragment_end_char``.
        score: Server-side relevance score for this citation, 0.0-1.0.
            Typically observed in the 0.6-0.7 range. ``None`` if the server
            omitted it.
        fragment_start_char: Start of the cited fragment's **source-side**
            character range, as declared by the server. Same coordinate space
            as ``start_char`` / ``end_char``, which this client derives
            independently from the fragment's blocks; the two have agreed on
            every capture so far, and a disagreement means the server and this
            client no longer read the fragment the same way. ``None`` if the
            server omitted it.

            (The slot is ``Citation``'s protobuf tag 4, which the recovered
            schema in ``docs/android/schema.proto`` does not name — the meaning
            is established by live capture, not by that file.)
        fragment_end_char: End of that source-side range (exclusive).
        answer_anchor_start: Start of the range **of the answer** this
            citation supports, read from the answer document's annotation map
            (#2120). Resolve it with
            ``AskResult.answer_document.slice(answer_anchor_start,
            answer_anchor_end)`` — the offsets index that document, *not*
            ``AskResult.answer``, which additionally carries markdown emphasis
            and the inline ``[N]`` markers the document does not. ``None`` when
            the answer carried no annotation for this citation.

            Named ``anchor`` rather than ``range`` because it frequently has
            zero width: the backend commonly anchors a citation at the
            insertion point of its ``[N]`` marker rather than over a span. The
            name is also deliberately unlike the deprecated
            ``answer_start_char`` — which, despite the shared prefix, holds a
            *source*-side value and is not this field's predecessor.
        answer_anchor_end: End of that anchor (exclusive), equal to
            ``answer_anchor_start`` for a zero-width anchor.
    """

    source_id: str
    citation_number: int | None = None
    cited_text: str | None = None
    start_char: int | None = None
    end_char: int | None = None
    chunk_id: str | None = None
    passage_id: str | None = None
    #: **Deprecated alias for :attr:`fragment_start_char`** (#2120). Kept in
    #: lock-step by :meth:`__post_init__` / :meth:`__setattr__` and scheduled
    #: for removal in v1.0; see ``docs/deprecations.md``. Docs-only, like
    #: ``Notebook.modified_at``: warning on a dataclass **field** read would
    #: also fire from ``repr()`` / ``__eq__`` / ``dataclasses.replace()`` and
    #: the MCP/REST serializer, flooding callers who never typed the old name.
    #:
    #: Declared here, ahead of the canonical field, so positional construction
    #: keeps placing this value at the same index it always has.
    answer_start_char: int | None = None
    #: **Deprecated alias for :attr:`fragment_end_char`** (#2120).
    answer_end_char: int | None = None
    score: float | None = None
    #: Source-side union range of the cited fragment.
    fragment_start_char: int | None = None
    fragment_end_char: int | None = None
    #: Answer-side anchor from the answer document's annotation map.
    answer_anchor_start: int | None = None
    answer_anchor_end: int | None = None

    def __setattr__(self, name: str, value: object) -> None:
        r"""Keep the deprecated ``answer_*`` alias in lock-step with the canonical pair.

        ``answer_start_char`` / ``answer_end_char`` are the pre-#2120 names for
        ``fragment_start_char`` / ``fragment_end_char``. They must stay
        *fields* rather than become properties: the MCP/REST serializer emits
        ``dataclasses.fields`` only, so a property would silently vanish from
        every adapter's response. The invariant is therefore maintained on
        assignment, mirroring ``Notebook.__setattr__`` (#2126).

        Both directions mirror, so a legacy caller that writes
        ``ref.answer_start_char = X`` after construction still round-trips.
        Each is guarded on ``value is not None`` for a mechanical reason: the
        generated ``__init__`` assigns in declaration order and the deprecated
        names come first, so an unguarded mirror would let the canonical
        field's later ``None`` default wipe out a legacy
        ``ChatReference(..., answer_start_char=X)`` argument.
        :meth:`__post_init__` restores the canonical field from that legacy
        argument once, at birth.

        That guard leaves one residual gap, the same one
        ``Notebook.__setattr__`` documents: **only non-**``None``** assignments
        mirror**, so ``ref.fragment_start_char = None`` after construction
        leaves ``answer_start_char`` stale, and
        ``dataclasses.replace(ref, answer_start_char=X)`` is a no-op on a ref
        that already carries a ``fragment_start_char``. Neither is something
        the parser or any plausible caller does — the parser builds these once
        and only re-``replace``\s the ``citation_number`` — and closing them
        would need an "``__init__`` finished" flag whose cost outlives the
        alias it protects. ``docs/deprecations.md`` records both.
        """
        super().__setattr__(name, value)
        if value is None:
            return
        mirror = _CHAT_REFERENCE_ALIASES.get(name)
        if mirror is not None:
            super().__setattr__(mirror, value)

    def __post_init__(self) -> None:
        """Reconcile the deprecated alias, then validate paired-offset invariants.

        ``start_char``/``end_char``, ``fragment_start_char``/``fragment_end_char``
        and ``answer_anchor_start``/``answer_anchor_end`` are semantically
        paired ranges: each pair is either fully populated or fully ``None``,
        and start must not exceed end. The streamed-chat parser already
        produces values that satisfy this contract; this check catches bad
        hand-constructed instances at the dataclass boundary instead of leaking
        the half-populated state into downstream consumers.

        Reconciliation runs first so a legacy ``answer_start_char=`` keyword is
        validated as the fragment range it actually is. The canonical name
        wins when both are supplied and disagree.

        Raises:
            ValueError: when any pair is half-populated, or when start exceeds
                end on any pair.
        """
        if self.fragment_start_char is None:
            self.fragment_start_char = self.answer_start_char
        if self.fragment_end_char is None:
            self.fragment_end_char = self.answer_end_char
        # Re-assign so the alias follows the canonical field even when the
        # canonical field won a disagreement (``__setattr__`` mirrors it back).
        self.answer_start_char = self.fragment_start_char
        self.answer_end_char = self.fragment_end_char

        _validate_paired_range(
            "start_char", self.start_char, "end_char", self.end_char, "ChatReference"
        )
        _validate_paired_range(
            "fragment_start_char",
            self.fragment_start_char,
            "fragment_end_char",
            self.fragment_end_char,
            "ChatReference",
        )
        _validate_paired_range(
            "answer_anchor_start",
            self.answer_anchor_start,
            "answer_anchor_end",
            self.answer_anchor_end,
            "ChatReference",
        )

    def __setstate__(self, state: dict[str, Any]) -> None:
        """Restore from a pickle, re-establishing the alias invariant.

        Unpickling bypasses ``__init__``, ``__post_init__`` *and*
        ``__setattr__``: the default protocol writes ``__dict__`` directly. A
        pickle written before #2120 therefore restores with
        ``answer_start_char`` populated and no ``fragment_start_char`` key —
        which does not raise, because the canonical field has a class-level
        ``None`` default, so the lookup falls through and quietly yields
        ``None``. That silent "the two names disagree" state is exactly what
        the alias runway promises cannot happen, so seed the canonical fields
        from the legacy ones here too. Mirrors ``Notebook.__setstate__``
        (#2126).
        """
        self.__dict__.update(state)
        if state.get("fragment_start_char") is None:
            self.__dict__["fragment_start_char"] = state.get("answer_start_char")
        if state.get("fragment_end_char") is None:
            self.__dict__["fragment_end_char"] = state.get("answer_end_char")
        self.__dict__["answer_start_char"] = self.__dict__["fragment_start_char"]
        self.__dict__["answer_end_char"] = self.__dict__["fragment_end_char"]


@dataclass(frozen=True)
class NextStepSuggestion:
    """A backend-suggested follow-up action shown beneath a chat answer.

    ``type_code`` is preserved verbatim so a newly introduced server value is
    never dropped. :attr:`kind` provides the typed enum when the installed
    client recognizes that value.
    """

    question: str
    type_code: int

    @property
    def kind(self) -> MagicArtifactType | None:
        """Known ``MagicArtifactType`` member, or ``None`` for a new code."""
        try:
            return MagicArtifactType(self.type_code)
        except ValueError:
            return None


@dataclass
class AskResult:
    """Result of asking the notebook a question.

    Attributes:
        answer: The AI-generated answer text.
        conversation_id: UUID for this conversation (used for follow-ups).
        turn_number: The server-derived turn number in the conversation. A
            non-empty answer is numbered after the prior user-question turns;
            an empty answer reports the number of prior turns unchanged.
        is_follow_up: Whether this was a follow-up request. A caller-supplied
            conversation ID is explicit follow-up intent even if that server
            conversation currently has no prior question rows; an implicit
            ask is a follow-up only when prior server turns exist.
        references: List of source references cited in the answer.
        raw_response: First 1000 chars of raw API response (for debugging).
        answer_document: The answer's own structured document — its paragraphs
            plus the annotation map that anchors each citation to a range of
            the answer (#2120). Empty (not ``None``) when the response carried
            no decodable document, so consumers never have to branch.

            ``answer_document.text`` is **not** ``answer``: the answer string
            carries markdown emphasis and inline ``[N]`` citation markers that
            the document does not, so it is longer and its offsets differ.
            ``ChatReference.answer_anchor_start`` /
            ``answer_anchor_end`` index ``answer_document.text``, and only
            that.

            Omitted from the MCP / REST / CLI ``--json`` envelopes (see
            ``_app.views.ask_result_view``): it restates the answer in a form
            those agent-facing surfaces do not consume, and would roughly
            double every payload.
        turn_key: The backend's :class:`ConversationTurnKey` for this turn,
            decoded from the streamed answer (#2122) — the key
            ``SubmitFeedback`` is addressed by. ``None`` when the stream
            carried no usable key, which includes every ``AskResult`` built by
            hand or by an older code path, so callers must treat it as
            optional. ``turn_key.session_id`` is a raw wire value and is **not**
            a substitute for :attr:`conversation_id`; see
            :class:`ConversationTurnKey`.
        next_steps: Suggested follow-up questions/actions volunteered with the
            winning answer row. Empty when the stream omitted the optional
            ``NextStepSuggestions`` block.
    """

    answer: str
    conversation_id: str
    turn_number: int
    is_follow_up: bool
    references: list[ChatReference] = field(default_factory=list)
    raw_response: str = ""
    answer_document: StructuredDocument = field(default_factory=StructuredDocument, repr=False)
    turn_key: ConversationTurnKey | None = None
    next_steps: list[NextStepSuggestion] = field(default_factory=list)
