"""Chat row adapters for the streamed-chat (``GenerateFreeFormStreamed``)
payload and the conversation-history (``GET_CONVERSATION_TURNS`` / ``khqZz``)
payload.

The streamed-chat endpoint is **not** a ``batchexecute`` RPC, so there is no
obfuscated method ID to thread through ``safe_index`` — descents pass
``method_id=None`` and rely on named ``source`` labels to localise schema drift
in raised :class:`UnknownRPCMethodError` diagnostics (ADR-0011). The
conversation-history rows (:class:`ConversationTurnRow` /
:func:`unwrap_conversation_turns`) come from a regular ``batchexecute`` RPC and
carry ``RPCMethod.GET_CONVERSATION_TURNS`` as their drift-diagnostic method id.

These adapters centralise the positional knowledge that ``_web/rows/chat_stream.py``
previously open-coded as scattered single-level subscripts (``first[4]``,
``cite[1]``, ``cite_inner[5]``, ``passage_data[0]`` …). Consumer sites should
wrap the raw lists in the typed views below and read named properties so a
future Google reshape of the chat wire format is a one-place fix here, and so
genuine drift RAISES ``UnknownRPCMethodError`` via ``safe_index`` instead of
silently degrading to an empty/wrong answer.

Position contracts (pinned by ``tests/unit/test_chat_row_adapter.py``):

* :class:`StreamEnvelopeRow` — one decoded ``wrb.fr`` inner payload
  (``inner_data``, a ``GenerateFreeFormStreamedResponse``):

  =====  ============================================================
  Index  Meaning
  =====  ============================================================
  0      the answer record consumed by :class:`AnswerRow`
  4      ``isFinalResponse`` (bool) — ``True`` on exactly the last chunk
  5      ``NextStepSuggestions``; ``[5][0]`` is its suggestion-row list
  =====  ============================================================

* :class:`AnswerRow` — one populated answer record (``inner_data[0]``):

  =====  ============================================================
  Index  Meaning
  =====  ============================================================
  0      answer text (str)
  2      ``ConversationTurnKey``; ``[2][0]`` is the session id, ``[2][1]``
         the per-turn id and ``[2][2]`` the per-turn code
  4      ``responseDoc`` (a ``TailwindDoc``); ``[4][0]`` is its document
         body, ``[4][3]`` the citation list (``TailwindDoc.objects``) and
         ``[4][4] == 1`` the answer marker (``TailwindDoc.type``)
  =====  ============================================================

* :class:`CitationRow` — one citation entry (``type_info[3][i]``):

  =====  ============================================================
  Index  Meaning
  =====  ============================================================
  0      chunk-id block; ``[0][0]`` is the chunk id
  1      citation detail block (:class:`CitationDetail`)
  =====  ============================================================

* :class:`CitationDetail` — ``cite[1]`` (a ``Citation``):

  =====  ============================================================
  Index  Meaning
  =====  ============================================================
  2      relevance score (float 0.0-1.0)
  3      fragment range ``[[None, start, end]]`` — **source-side**, the
         union of the fragment's element ranges (#2120)
  4      ``fragment`` message; its ``elements`` list is one level below
         at ``[4][0]``
  5      nested source-id data
  =====  ============================================================

  ``Citation.objectId`` sits at ``cite_inner[6]`` and is not read: it repeats
  the id the enclosing ``DocumentObject`` already carries at ``cite[0][0]``,
  which :attr:`CitationRow.chunk_id` surfaces and the answer-document
  annotation map joins on.

  The fragment's elements are ``StructuralElement`` rows shared with the
  source-content path; they are decoded by
  :mod:`notebooklm._web.rows.documents`, not by a chat-local adapter.

* :class:`ConversationTurnRow` — one ``GET_CONVERSATION_TURNS`` turn row:

  =====  ============================================================
  Index  Meaning
  =====  ============================================================
  2      role code (``1`` = user question, ``2`` = AI answer)
  3      question text (str) — only on role-1 rows
  4      nested answer-content payload — only on role-2 rows
         (the ``[4][0][0]`` leaf descent lives in
         ``_chat._extract_next_turn_content``)
  =====  ============================================================
"""

from __future__ import annotations

import reprlib
from dataclasses import dataclass, field
from typing import Any, ClassVar

from ..._types.chat import ConversationTurnKey
from ..._types.documents import StructuredDocument
from ...exceptions import UnknownRPCMethodError
from ...rpc import RPCMethod, safe_index
from ..wire.decoder import sanitize_status_message
from .documents import build_document

__all__ = [
    "AnswerRow",
    "CitationRow",
    "CitationDetail",
    "ChatSessionStatusRow",
    "ChatSettingsRow",
    "ConversationTurnRow",
    "count_question_turn_rows",
    "ErrorPayloadRow",
    "NextStepSuggestionRow",
    "StreamEnvelopeRow",
    "StreamFrameRow",
    "SavedChatNoteRow",
    "unwrap_chat_settings",
    "unwrap_chat_session_status",
    "unwrap_conversation_turns",
    "unwrap_last_conversation_id",
]

# ``GET_NOTEBOOK`` (``rLM1Ne``) method id, threaded into ``safe_index`` /
# ``UnknownRPCMethodError`` so chat-settings drift points at the right RPC.
_GET_NOTEBOOK_METHOD_ID = RPCMethod.GET_NOTEBOOK.value

# Position of the chat-settings block inside a GET_NOTEBOOK ``nb_info`` row.
# Live-verified (#1751): mirrors the write slot ``ChatAPI.configure`` targets at
# ``params[1][0][7]``.
_CHAT_SETTINGS_POS = 7

# Enum codes a never-configured notebook decodes to (GET_NOTEBOOK returns
# ``null`` at the chat-settings slot). ``1`` == ``ChatGoal.DEFAULT`` ==
# ``ChatResponseLength.DEFAULT``; kept as bare ints so this positional layer
# stays enum-free — the code→enum mapping lives in ``ChatAPI.get_settings``.
_DEFAULT_GOAL_CODE = 1
_DEFAULT_LENGTH_CODE = 1

# ``ChatGoal.CUSTOM`` — the only goal whose persona prompt is *active*. Bare int
# to keep this layer enum-free.
_CUSTOM_GOAL_CODE = 2

# ``GET_CONVERSATION_TURNS`` method id, threaded into ``safe_index`` /
# ``UnknownRPCMethodError`` so drift diagnostics point at the right RPC.
_TURNS_METHOD_ID = RPCMethod.GET_CONVERSATION_TURNS.value

# Envelope-unwrap position: ``GET_CONVERSATION_TURNS`` wraps the turn list as
# the first element of a single-element envelope (``[[turn, ...], ...]``).
_TURNS_CONTAINER_POS = 0

# Position of the conversation id inside one innermost ``[conv_id]`` row of the
# ``GET_LAST_CONVERSATION_ID`` (``hPTbtc``) ``[[[conv_id]]]`` payload.
_LAST_CONVERSATION_ID_POS = 0

_SESSION_STATUS_METHOD_ID = RPCMethod.GET_CHAT_SESSION_STATUS.value


@dataclass(frozen=True)
class ChatSessionStatusRow:
    """Decoded semantic fields from ``GetChatSessionStatus``."""

    generating: bool
    token: str | None


def unwrap_chat_session_status(raw: Any) -> ChatSessionStatusRow:
    """Decode ``[token-or-null, status]`` into a strict semantic row.

    Status ``1`` is idle and must not carry a token; status ``2`` is active and
    must carry a non-empty token. Any other present shape is protocol drift.
    """
    source = "WebChatAPI.session_status"
    if not isinstance(raw, list) or len(raw) != 2:
        raise UnknownRPCMethodError(
            "chat session status response is not a two-slot list",
            method_id=_SESSION_STATUS_METHOD_ID,
            path=(),
            source=source,
            data_at_failure=reprlib.repr(raw),
        )
    token = safe_index(
        raw,
        0,
        method_id=_SESSION_STATUS_METHOD_ID,
        source=source,
    )
    status = safe_index(
        raw,
        1,
        method_id=_SESSION_STATUS_METHOD_ID,
        source=source,
    )
    if status == 1 and token is None:
        return ChatSessionStatusRow(generating=False, token=None)
    if status == 2 and isinstance(token, str) and token:
        return ChatSessionStatusRow(generating=True, token=token)
    raise UnknownRPCMethodError(
        "chat session status response has an unknown status/token combination",
        method_id=_SESSION_STATUS_METHOD_ID,
        path=(1,),
        source=source,
        data_at_failure=reprlib.repr(raw),
    )


def unwrap_last_conversation_id(raw: Any) -> str | None:
    """Return the most-recent conversation id from a ``GET_LAST_CONVERSATION_ID`` payload.

    The wire shape is the nested ``[[[conv_id]]]`` envelope: an outer list of
    groups, each a list of rows, each row an innermost ``[conv_id]`` list whose
    first element is the id string. This centralises the ``conv[0]`` descent
    ``_chat.py`` previously open-coded, and is **deliberately SOFT** —
    mirroring the historical ``get_conversation_id`` contract:

    * a non-list / falsy payload, or a payload that yields no innermost
      ``[str]`` row, returns ``None`` (no conversation exists yet);
    * the first innermost ``[str]`` row found wins, and its id is returned.

    Unlike :func:`unwrap_conversation_turns`, this read does NOT raise on a
    truthy non-list payload: the caller (``ChatAPI.get_conversation_id``)
    retains its own WARNING-then-``None`` diagnostics for unexpected shapes, so
    moving the position knowledge here must not change that return contract. The
    inner ``conv[_LAST_CONVERSATION_ID_POS]`` read is a single-level index taken
    only AFTER the ``conv and isinstance(conv[0], str)`` guard proves the slot
    present, so it can never raise.
    """
    if not isinstance(raw, list):
        return None
    for group in raw:
        if not isinstance(group, list):
            continue
        for conv in group:
            if isinstance(conv, list) and conv and isinstance(conv[_LAST_CONVERSATION_ID_POS], str):
                return conv[_LAST_CONVERSATION_ID_POS]
    return None


def unwrap_conversation_turns(turns_data: Any, *, source: str) -> list[Any]:
    """Return the flat turn list from a raw ``GET_CONVERSATION_TURNS`` result.

    The wire shape is a single-element envelope whose first element is the
    turn list (``[[turn, ...], ...]``). This centralises the ``turns_data[0]``
    container probe so ``_chat.py`` stops open-coding it, with the
    absence-vs-malformed split of the #1485 policy:

    * **Absence stays soft** — a falsy payload (no history yet) and a falsy
      first slot (``[[]]`` / ``[None]``, a legitimately-empty conversation)
      return ``[]`` without logging, preserving the historical "no history"
      contract.
    * **Present-but-malformed RAISES** — a *truthy non-list* payload, or a
      *truthy non-list* where the turn list belongs, is genuine schema drift,
      not an empty conversation, and raises :class:`UnknownRPCMethodError`
      (consistent with the strict ``safe_index`` leaf behavior in
      ``_extract_next_turn_content``) instead of silently yielding an empty
      chat history.

    Args:
        turns_data: Raw decoded ``GET_CONVERSATION_TURNS`` payload.
        source: Caller label for drift diagnostics
            (e.g. ``"_chat.get_history"``).
    """
    if not turns_data:
        return []
    if not isinstance(turns_data, list):
        # Top-level drift: the RPC returned something other than the envelope
        # list. ``path=()`` marks top-level per the UnknownRPCMethodError
        # contract; the preview is reprlib-bounded.
        raise UnknownRPCMethodError(
            f"conversation turns payload holds {type(turns_data).__name__} "
            "(expected the envelope list)",
            method_id=_TURNS_METHOD_ID,
            path=(),
            source=source,
            data_at_failure=reprlib.repr(turns_data),
        )
    # ``turns_data`` is a non-empty list here, so the descent is a no-op on
    # the happy path; routed through ``safe_index`` for the shared telemetry
    # seam (mirrors ``StreamFrameRow.tag``).
    turns = safe_index(
        turns_data,
        _TURNS_CONTAINER_POS,
        method_id=_TURNS_METHOD_ID,
        source=source,
    )
    if not turns:
        return []
    if not isinstance(turns, list):
        raise UnknownRPCMethodError(
            f"conversation turns container holds {type(turns).__name__} (expected the turn list)",
            method_id=_TURNS_METHOD_ID,
            path=(_TURNS_CONTAINER_POS,),
            source=source,
            data_at_failure=reprlib.repr(turns_data),
        )
    return turns


@dataclass(frozen=True)
class ChatSettingsRow:
    """Decoded chat-settings block from a ``GET_NOTEBOOK`` ``nb_info`` row (#1751).

    Holds RAW enum codes (not ``ChatGoal`` / ``ChatResponseLength``) so this
    positional layer stays enum-free; ``ChatAPI.get_settings`` maps the codes to
    enums and builds the public ``ChatSettings``. Built by
    :func:`unwrap_chat_settings`.
    """

    goal_code: int
    response_length_code: int
    custom_prompt: str | None = None


def unwrap_chat_settings(nb_info: Any, *, source: str) -> ChatSettingsRow:
    """Decode the chat-settings block at ``nb_info[7]`` of a GET_NOTEBOOK row.

    Wire shape (live-verified #1751): ``nb_info[7] = [[goal_code, custom_prompt?],
    [length_code]]`` — e.g. ``[[2, "persona"], [4]]`` = CUSTOM persona + LONGER,
    ``[[1], [1]]`` = DEFAULT/DEFAULT. Mirrors the write shape ``ChatAPI.configure``
    sends at ``params[1][0][7]``.

    Absence-vs-malformed policy (ADR-0011 / #1485):

    * **Never-configured stays soft** — an explicit ``null`` at the slot maps to
      the DEFAULT/DEFAULT codes with no prompt (there is genuinely nothing to
      preserve).
    * **Present-but-malformed RAISES** — a too-short ``nb_info``, a truthy
      non-list block, or a block whose goal/length arrays are missing/empty/
      non-int is genuine drift and raises :class:`UnknownRPCMethodError`, NOT a
      silent default. On the read-modify-write ``configure`` path a silent
      default would clobber the field the caller meant to preserve — the exact
      #1751 footgun — so the read fails loud instead.

    Args:
        nb_info: The ``result[0]`` notebook-info list from GET_NOTEBOOK.
        source: Caller label for drift diagnostics (e.g. ``"ChatAPI.get_settings"``).
    """
    if not isinstance(nb_info, list) or len(nb_info) <= _CHAT_SETTINGS_POS:
        raise UnknownRPCMethodError(
            f"GET_NOTEBOOK nb_info holds {type(nb_info).__name__} with no chat-settings "
            f"slot at [{_CHAT_SETTINGS_POS}]",
            method_id=_GET_NOTEBOOK_METHOD_ID,
            path=(),
            source=source,
            data_at_failure=reprlib.repr(nb_info),
        )
    block = nb_info[_CHAT_SETTINGS_POS]
    if block is None:
        # Never configured — nothing to preserve; DEFAULT/DEFAULT, no persona.
        return ChatSettingsRow(_DEFAULT_GOAL_CODE, _DEFAULT_LENGTH_CODE, None)
    if not isinstance(block, list):
        raise UnknownRPCMethodError(
            f"chat-settings block holds {type(block).__name__} (expected [[goal],[length]])",
            method_id=_GET_NOTEBOOK_METHOD_ID,
            path=(_CHAT_SETTINGS_POS,),
            source=source,
            data_at_failure=reprlib.repr(block),
        )
    goal_array = safe_index(block, 0, method_id=_GET_NOTEBOOK_METHOD_ID, source=source)
    length_array = safe_index(block, 1, method_id=_GET_NOTEBOOK_METHOD_ID, source=source)
    if not isinstance(goal_array, list) or not goal_array:
        raise UnknownRPCMethodError(
            f"chat-settings goal array holds {type(goal_array).__name__} "
            "(expected [goal_code, custom_prompt?])",
            method_id=_GET_NOTEBOOK_METHOD_ID,
            path=(_CHAT_SETTINGS_POS, 0),
            source=source,
            data_at_failure=reprlib.repr(block),
        )
    if not isinstance(length_array, list) or not length_array:
        raise UnknownRPCMethodError(
            f"chat-settings length array holds {type(length_array).__name__} "
            "(expected [length_code])",
            method_id=_GET_NOTEBOOK_METHOD_ID,
            path=(_CHAT_SETTINGS_POS, 1),
            source=source,
            data_at_failure=reprlib.repr(block),
        )
    goal_code = safe_index(goal_array, 0, method_id=_GET_NOTEBOOK_METHOD_ID, source=source)
    length_code = safe_index(length_array, 0, method_id=_GET_NOTEBOOK_METHOD_ID, source=source)
    # ``type(...) is int`` (not ``isinstance``) so a drifted bool code — ``bool``
    # is an ``int`` subclass — is rejected as malformed, not silently decoded to
    # DEFAULT/DEFAULT.
    if type(goal_code) is not int or type(length_code) is not int:
        raise UnknownRPCMethodError(
            f"chat-settings codes are not ints (goal={goal_code!r}, length={length_code!r})",
            method_id=_GET_NOTEBOOK_METHOD_ID,
            path=(_CHAT_SETTINGS_POS,),
            source=source,
            data_at_failure=reprlib.repr(block),
        )
    # The custom prompt is the *active* persona, meaningful only for the CUSTOM
    # goal. The server retains a prompt string under a non-CUSTOM goal as an
    # inactive DRAFT — live-verified: applying a preset over a persona leaves
    # ``[[1, "…"], …]`` — but that draft is not an active persona and
    # ``configure()`` cannot round-trip it (it serializes the prompt only for
    # CUSTOM), so it is not surfaced. Conversely a CUSTOM goal REQUIRES a
    # non-empty prompt (``configure()`` enforces this on write), so a CUSTOM goal
    # without one is decode drift — raise here rather than let the length-only
    # merge fail later in ``configure()`` with a ValidationError.
    custom_prompt: str | None = None
    if goal_code == _CUSTOM_GOAL_CODE:
        prompt_value = goal_array[1] if len(goal_array) > 1 else None
        if not isinstance(prompt_value, str) or not prompt_value:
            raise UnknownRPCMethodError(
                "chat-settings CUSTOM goal is missing its persona prompt",
                method_id=_GET_NOTEBOOK_METHOD_ID,
                path=(_CHAT_SETTINGS_POS, 0),
                source=source,
                data_at_failure=reprlib.repr(block),
            )
        custom_prompt = prompt_value
    return ChatSettingsRow(goal_code, length_code, custom_prompt)


@dataclass(frozen=True)
class SavedChatNoteRow:
    """Typed view of the ``CREATE_NOTE`` (saved-from-chat) response envelope.

    The captured server response wraps the note in an outer list
    (``[[note_id, ..., title, rich_content]]``), but some response paths return
    the note flat (``[note_id, ...]``). This adapter centralises the unwrap +
    the ``note_data[0]`` id / ``note_data[4]`` server-title position knowledge
    that the saved-chat note adapter previously open-coded, so a future reshape is a
    one-place fix here.

    Every read is **SOFT** — it preserves the historical
    ``save_chat_answer_as_note`` degrade exactly: an unrecognised / short /
    wrong-typed shape leaves :attr:`note_id` ``None`` (the caller raises a
    ``RuntimeError`` from that), :attr:`server_title` falls back to ``None``
    (the caller keeps the requested title), and :attr:`note_data` is ``None``.
    Nothing here raises ``UnknownRPCMethodError``: the saved-chat create path
    has always treated these as best-effort reads, not strict drift points.
    """

    _raw: Any = field(repr=False)

    # ---- Position constants (the canary contract) ------------------------
    # If any of these change,
    # ``tests/unit/test_chat_row_adapter.py::TestSavedChatNoteRowPositionContract``
    # MUST be updated in the same commit — that failure is the wire-shape
    # change signal.
    _OUTER_NOTE_POS: ClassVar[int] = 0
    _ID_POS: ClassVar[int] = 0
    _SERVER_TITLE_POS: ClassVar[int] = 4

    @property
    def note_data(self) -> list[Any] | None:
        """The inner note envelope (``[id, content, metadata, ...]``) or ``None``.

        Unwraps the two captured shapes: the outer-wrapped
        ``[[note_id, ...]]`` (the inner list at position 0) and the flat
        ``[note_id, ...]`` (a leading ``str`` id). Any other shape — empty
        result, non-list/non-str leading slot — is an unusable response and
        degrades to ``None``.
        """
        if not isinstance(self._raw, list) or len(self._raw) <= self._OUTER_NOTE_POS:
            return None
        first = self._raw[self._OUTER_NOTE_POS]
        if isinstance(first, list):
            return first
        if isinstance(first, str):
            return self._raw
        return None

    @property
    def note_id(self) -> str | None:
        """Note id at ``note_data[0]`` — ``None`` when absent or not a string."""
        data = self.note_data
        if data is None or len(data) <= self._ID_POS:
            return None
        value = data[self._ID_POS]
        return value if isinstance(value, str) else None

    @property
    def server_title(self) -> str | None:
        """Server-stored title at ``note_data[4]`` — ``None`` when absent.

        Slot 4 of the note carries the server-stored title, which may differ
        from the requested title (smart-title generation). ``None`` (the caller
        keeps the requested title) when the row is too short or the slot is not
        a string.
        """
        data = self.note_data
        if data is None or len(data) <= self._SERVER_TITLE_POS:
            return None
        value = data[self._SERVER_TITLE_POS]
        return value if isinstance(value, str) else None


@dataclass(frozen=True)
class ConversationTurnRow:
    """Typed view of one ``GET_CONVERSATION_TURNS`` (``khqZz``) turn row.

    Each turn is ``[id?, ?, role, ...]`` where ``role`` at position 2 is
    ``1`` for a user question (text at position 3) or ``2`` for an AI answer
    (nested content at position 4, whose ``[4][0][0]`` leaf descent lives in
    ``_chat._extract_next_turn_content``). Position knowledge is
    centralised here; ``_chat.py`` should NEVER open-code ``turn[2]`` /
    ``turn[3]``.

    Per the #1485 absence-vs-malformed policy, every read here is a soft
    length-guarded degrade: a short or non-list row is a *skip-this-turn*
    signal (the QA-pair walk preserves its load-bearing skip-row contract and
    logs a diagnostic), never a raise. The strict raise for genuine drift
    lives in the answer-content leaf descent, which the consumer routes
    through ``safe_index``.
    """

    # Wrapped row; ``repr=False`` so logs don't explode with the entire
    # conversation payload when a row appears in a stack trace.
    _raw: Any = field(repr=False)

    # ---- Position constants (the canary contract) ------------------------
    # ClassVar so the frozen dataclass treats these as class-level constants.
    # If any of these change,
    # ``tests/unit/test_chat_row_adapter.py::TestConversationTurnPositionContract``
    # MUST be updated in the same commit — that failure is the wire-shape
    # change signal.
    _ROLE_POS: ClassVar[int] = 2
    _QUESTION_TEXT_POS: ClassVar[int] = 3
    _ANSWER_CONTENT_POS: ClassVar[int] = 4
    # A turn must carry at least its role slot to be classifiable — mirrors
    # the historical ``len(turn) < 3`` skip in ``_parse_turns_to_qa_pairs``.
    _MIN_LEN: ClassVar[int] = 3

    #: Role code marking a user-question turn.
    ROLE_QUESTION: ClassVar[int] = 1
    #: Role code marking an AI-answer turn.
    ROLE_ANSWER: ClassVar[int] = 2

    @property
    def raw(self) -> Any:
        """The wrapped raw turn row (for the answer-content leaf descent)."""
        return self._raw

    @property
    def is_well_formed(self) -> bool:
        """Whether the row is a list long enough to carry its role slot."""
        return isinstance(self._raw, list) and len(self._raw) >= self._MIN_LEN

    @property
    def role(self) -> Any:
        """Role code at ``turn[2]`` — ``None`` when the row is malformed."""
        if not self.is_well_formed:
            return None
        return self._raw[self._ROLE_POS]

    @property
    def has_unrecognized_role(self) -> bool:
        """Whether a *well-formed* row carries a role outside the known set.

        ``False`` for malformed rows (those are skipped as malformed, not as
        role drift) and for the known :data:`ROLE_QUESTION` /
        :data:`ROLE_ANSWER` codes — an ordinary unpaired answer row must NOT
        trip this. A well-formed row whose role slot holds anything else
        signals role-slot drift: without a diagnostic, real history would
        silently parse to ``[]`` (the fabrication class #1485 targets), so
        the QA-pair walk logs a DEBUG record before skipping it.
        """
        return self.is_well_formed and self.role not in (self.ROLE_QUESTION, self.ROLE_ANSWER)

    @property
    def is_question(self) -> bool:
        """Whether this is a user-question turn carrying its text slot.

        Mirrors the historical ``turn[2] == 1 and len(turn) > 3`` guard: a
        role-1 row too short to carry its question text is not usable as a
        question (soft skip, not drift).
        """
        return self.role == self.ROLE_QUESTION and len(self._raw) > self._QUESTION_TEXT_POS

    @property
    def is_answer(self) -> bool:
        """Whether this is an AI-answer turn carrying its content slot.

        Mirrors the historical ``len(next_turn) > 4 and next_turn[2] == 2``
        guard: a role-2 row too short to carry its content payload is not
        usable as an answer (the preceding question keeps its empty-answer
        fallback).
        """
        return self.role == self.ROLE_ANSWER and len(self._raw) > self._ANSWER_CONTENT_POS

    @property
    def question_text(self) -> str:
        """Question text at ``turn[3]`` — ``""`` for a null/absent slot.

        Preserves the historical ``str(turn[3] or "")`` coercion (a ``None``
        text slot legitimately yields the empty question).
        """
        if not self.is_question:
            return ""
        return str(self._raw[self._QUESTION_TEXT_POS] or "")


def count_question_turn_rows(turns_data: Any, *, source: str) -> tuple[int, int]:
    """Return ``(row_count, question_count)`` for a conversation-turn envelope."""
    turns = unwrap_conversation_turns(turns_data, source=source)
    return len(turns), sum(
        ConversationTurnRow(turn).role == ConversationTurnRow.ROLE_QUESTION for turn in turns
    )


@dataclass(frozen=True)
class StreamFrameRow:
    """Typed view of one streamed-chat envelope frame.

    Frames arrive as ``["wrb.fr", None, inner_json, ...]`` (a successful RPC
    result) or ``["er", rpc_id, code, ...]`` (a server-side error). This adapter
    centralises the ``item[0]`` tag, ``item[2]`` inner-JSON / error-code, and
    ``item[5]`` error-payload reads so the stream parser stops open-coding them
    (issue #1491).

    The ``tag`` read goes through ``safe_index`` because the caller already
    guarantees ``len(item) >= 2`` — the frame tag slot is the one position that
    must always be present, so its absence is genuine drift. Every other slot is
    optional and short-circuits to ``None`` on a short frame.
    """

    _raw: list[Any] = field(repr=False)

    _TAG_POS: ClassVar[int] = 0
    _TERMINAL_SEQUENCE_POS: ClassVar[int] = 1
    _INNER_JSON_POS: ClassVar[int] = 2
    _ERROR_CODE_POS: ClassVar[int] = 2
    _ERROR_PAYLOAD_POS: ClassVar[int] = 5

    #: Source label for ``safe_index`` drift diagnostics on the tag descent.
    _SOURCE: ClassVar[str] = "ChatStreamFrameRow.tag"

    @property
    def tag(self) -> Any:
        """Frame tag at ``item[0]`` (``"wrb.fr"`` / ``"er"``).

        The caller guarantees ``len(item) >= 2`` so this is a no-op on the happy
        path; ``safe_index`` only fires if the tag slot itself drifted out.
        """
        return safe_index(self._raw, self._TAG_POS, method_id=None, source=self._SOURCE)

    @property
    def inner_json(self) -> Any:
        """Inner-JSON payload at ``item[2]`` (a ``str`` for ``wrb.fr`` frames)."""
        if len(self._raw) <= self._INNER_JSON_POS:
            return None
        return self._raw[self._INNER_JSON_POS]

    @property
    def terminal_sequence(self) -> int | None:
        """Bookkeeping sequence at ``item[1]`` of an ``"e"`` frame.

        This value is not an RPC or gRPC status code: successful streams carry
        values such as 15 here. It is useful only to identify the bounded
        error-only terminator shape when no ``wrb.fr`` payload arrived.
        """
        if len(self._raw) <= self._TERMINAL_SEQUENCE_POS:
            return None
        value = self._raw[self._TERMINAL_SEQUENCE_POS]
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    @property
    def error_code(self) -> Any:
        """Optional error code at ``item[2]`` of an ``"er"`` frame.

        Read with a length guard (not ``safe_index``): an absent code is normal
        for a short ``"er"`` frame and must NOT be treated as schema drift — the
        frame itself is the error signal.
        """
        if len(self._raw) <= self._ERROR_CODE_POS:
            return None
        return self._raw[self._ERROR_CODE_POS]

    @property
    def error_payload(self) -> list[Any] | None:
        """Optional server-side error payload at ``item[5]`` (a list) or ``None``."""
        if len(self._raw) <= self._ERROR_PAYLOAD_POS:
            return None
        value = self._raw[self._ERROR_PAYLOAD_POS]
        return value if isinstance(value, list) else None


@dataclass(frozen=True)
class StreamEnvelopeRow:
    """Typed view of one decoded streamed-chat inner payload (``inner_data``).

    The ``wrb.fr`` frame's inner JSON decodes to a
    ``GenerateFreeFormStreamedResponse``: the answer record at index 0 (wrapped
    by :class:`AnswerRow`), ``isFinalResponse`` at index 4, and optional
    ``NextStepSuggestions`` at index 5. Heartbeat frames decode to ``[]`` and
    answer the optional-field questions with their empty defaults.

    This adapter owns only the ``isFinalResponse`` read; the ``inner_data[0]``
    answer-row descent stays in ``_web/rows/chat_stream.py``, which raises its own typed
    drift error for a non-list answer row (a contract this positional view has
    no business restating).
    """

    _raw: Any = field(repr=False)

    # ---- Position constants (the canary contract) ------------------------
    # If this changes,
    # ``tests/unit/test_chat_row_adapter.py::TestStreamEnvelopeRowPositionContract``
    # MUST be updated in the same commit — that failure is the wire-shape
    # change signal.
    #
    # ``GenerateFreeFormStreamedResponse.isFinalResponse`` (tag 5). Live
    # (#2122, two probes): ``False`` on every chunk but the last and ``True``
    # on exactly the last, across a 5-chunk and a 6-chunk stream.
    _IS_FINAL_RESPONSE_POS: ClassVar[int] = 4
    _NEXT_STEPS_POS: ClassVar[int] = 5
    _NEXT_STEPS_ROWS_POS: ClassVar[int] = 0

    @property
    def is_final_response(self) -> bool:
        """Whether the backend marked this chunk as the final one.

        Deliberately narrow: ``True`` only for a literal wire ``true``. A short
        payload (heartbeat ``[]``), a ``null`` slot, and a truthy non-bool all
        answer ``False`` — this predicate is used to *select* the answer, so
        anything other than the server explicitly saying "final" must leave the
        existing longest-wins fallback in charge rather than promote an
        arbitrary chunk.
        """
        if not isinstance(self._raw, list) or len(self._raw) <= self._IS_FINAL_RESPONSE_POS:
            return False
        return self._raw[self._IS_FINAL_RESPONSE_POS] is True

    @property
    def next_step_rows(self) -> list[NextStepSuggestionRow]:
        """Typed follow-up suggestion rows from ``inner_data[5][0]``.

        Suggestions are optional UI sugar and older cassette frames omit the
        block entirely, so every absent/malformed container degrades to ``[]``
        without affecting the answer itself. Individual malformed rows are
        retained as adapters and filtered by the consumer.
        """
        if not isinstance(self._raw, list) or len(self._raw) <= self._NEXT_STEPS_POS:
            return []
        block = self._raw[self._NEXT_STEPS_POS]
        if not isinstance(block, list) or len(block) <= self._NEXT_STEPS_ROWS_POS:
            return []
        rows = block[self._NEXT_STEPS_ROWS_POS]
        if not isinstance(rows, list):
            return []
        return [NextStepSuggestionRow(row) for row in rows]


@dataclass(frozen=True)
class ErrorPayloadRow:
    """Typed view of a streamed-chat error payload (``item[5]``).

    Structure: ``[8, None, [["type.googleapis.com/.../UserDisplayableError", …]]]``
    for the rich rate-limit shape, or a bare ``[3]`` status for a request-level
    rejection (issue #1472). Both are the same envelope — a JSON-array-encoded
    ``google.rpc.Status`` — at different arities, which is why index 0 is the
    code, index 1 the message, and index 2 the details. Centralises the
    ``error_payload[0]`` status, the ``error_payload[1]`` message, the
    ``error_payload[2]`` entries, and the inner ``entry[0]`` reads so
    ``raise_if_rate_limited`` / ``_raise_chat_rejection`` stop open-coding them
    (issues #1491, #2188).
    """

    _raw: list[Any] = field(repr=False)

    _STATUS_POS: ClassVar[int] = 0
    _MESSAGE_POS: ClassVar[int] = 1
    _ENTRIES_POS: ClassVar[int] = 2

    @property
    def status_code(self) -> Any:
        """Leading status/code at ``error_payload[0]`` (e.g. ``3`` for a bare
        ``[3]`` rejection, ``8`` for the rate-limit shape) — ``None`` if absent."""
        if not self._raw:
            return None
        return self._raw[self._STATUS_POS]

    @property
    def message(self) -> str | None:
        """Server-authored ``google.rpc.Status.message`` at ``error_payload[1]``.

        The envelope is a ``google.rpc.Status`` (``code`` tag 1 → index 0,
        ``message`` tag 2 → index 1, ``details`` tag 3 → index 2), so this slot
        is the one place the *server* can say why it rejected the request —
        everything else this client shows for a rejection is client-authored
        wording (#2188).

        **No captured response has ever carried a populated message**: the one
        recorded rich sample holds ``None`` here and the bare rejections are
        length-1 arrays. The normalising rule (non-empty string only, whitespace
        collapsed, length capped, non-string warned about) is shared with the
        ``batchexecute`` decoder via :func:`sanitize_status_message` so the two
        layers cannot drift in what users see; only the position is declared
        twice, because ``rpc`` cannot import ``_web.rows``.
        """
        if len(self._raw) <= self._MESSAGE_POS:
            return None
        return sanitize_status_message(
            self._raw[self._MESSAGE_POS], source="ErrorPayloadRow.message"
        )

    @property
    def entries(self) -> list[Any]:
        """Error entries at ``error_payload[2]`` — ``[]`` when absent/non-list."""
        if len(self._raw) <= self._ENTRIES_POS:
            return []
        value = self._raw[self._ENTRIES_POS]
        return value if isinstance(value, list) else []

    @staticmethod
    def entry_type(entry: Any) -> str | None:
        """The leading type string at ``entry[0]`` of one error entry, or ``None``."""
        if not isinstance(entry, list) or not entry:
            return None
        value = entry[0]
        return value if isinstance(value, str) else None


@dataclass(frozen=True)
class NextStepSuggestionRow:
    """One ``NextStep`` row: ``[suggestion, MagicArtifactType]``."""

    _raw: Any = field(repr=False)

    _QUESTION_POS: ClassVar[int] = 0
    _TYPE_CODE_POS: ClassVar[int] = 1
    _MIN_LEN: ClassVar[int] = 2

    @property
    def question(self) -> str | None:
        if not isinstance(self._raw, list) or len(self._raw) <= self._QUESTION_POS:
            return None
        value = self._raw[self._QUESTION_POS]
        return value if isinstance(value, str) and value else None

    @property
    def type_code(self) -> int | None:
        if not isinstance(self._raw, list) or len(self._raw) <= self._TYPE_CODE_POS:
            return None
        value = self._raw[self._TYPE_CODE_POS]
        return value if type(value) is int else None

    @property
    def is_well_formed(self) -> bool:
        return (
            isinstance(self._raw, list)
            and len(self._raw) >= self._MIN_LEN
            and self.question is not None
            and self.type_code is not None
        )


@dataclass(frozen=True)
class AnswerRow:
    """Typed view of one populated streamed-chat answer record.

    The wrapped row is ``inner_data[0]`` of a decoded ``wrb.fr`` envelope
    whose ``inner_data`` is a populated list (heartbeats decode to ``[]``
    and never reach this adapter). Position knowledge is centralised here;
    consumer sites should NEVER open-code ``first[0]`` / ``first[2][0]`` /
    ``first[4][4]`` / ``first[4][3]``.

    The dataclass is frozen so the wrapped row can't be mutated through the
    adapter; the adapter never copies the raw row, so it is cheap to build.
    """

    # Wrapped row; ``repr=False`` so logs don't explode with the entire
    # streamed-chat payload when an AnswerRow appears in a stack trace.
    _raw: list[Any] = field(repr=False)

    # ---- Position constants (the canary contract) ------------------------
    # ClassVar so the frozen dataclass treats these as class-level constants
    # rather than instance fields. If any of these change,
    # ``tests/unit/test_chat_row_adapter.py::TestAnswerRowPositionContract``
    # MUST be updated in the same commit — that failure is the wire-shape
    # change signal.
    _TEXT_POS: ClassVar[int] = 0
    _CONV_BLOCK_POS: ClassVar[int] = 2
    _EMPTY_ANSWER_REASON_POS: ClassVar[int] = 3
    _TYPE_BLOCK_POS: ClassVar[int] = 4
    _ANSWER_MARKER_POS: ClassVar[int] = 4
    _CITATIONS_POS: ClassVar[int] = 3
    _ANSWER_MARKER_VALUE: ClassVar[int] = 1
    #: ``TailwindDoc.body`` inside the ``responseDoc`` at ``first[4]``.
    _DOC_BODY_POS: ClassVar[int] = 0
    # Layout INSIDE the ``ConversationTurnKey`` block at ``first[2]`` (#2122).
    # Slot 0 keeps its proto name (``sessionId``) because this client cannot
    # substantiate a stronger one; slot 1 does not, because its proto name
    # (``conversationId``) contradicts every observation. See
    # ``ConversationTurnKey`` in ``_types/chat.py``.
    _TURN_KEY_SESSION_ID_POS: ClassVar[int] = 0
    _TURN_KEY_TURN_ID_POS: ClassVar[int] = 1
    _TURN_KEY_TURN_CODE_POS: ClassVar[int] = 2

    @property
    def raw(self) -> list[Any]:
        """The wrapped raw answer row."""
        return self._raw

    @property
    def text(self) -> str | None:
        """Answer text at ``first[0]`` — ``None`` when absent or not a string.

        The caller guarantees ``len(self._raw) > 0`` before constructing the
        row, so the ``safe_index`` descent is a no-op on the happy path; the
        ``ChatAnswerRow.text`` label localises any top-level reshape in
        diagnostics.
        """
        if len(self._raw) <= self._TEXT_POS:
            return None
        value = safe_index(
            self._raw,
            self._TEXT_POS,
            method_id=None,
            source="ChatAnswerRow.text",
        )
        return value if isinstance(value, str) and value else None

    @property
    def _turn_key_block(self) -> list[Any] | None:
        """The ``ConversationTurnKey`` block at ``first[2]`` (a list) or ``None``.

        An absent / empty / non-list block legitimately means "no turn key on
        this record" (not drift), so both readers below short-circuit on it
        without invoking ``safe_index``.
        """
        if len(self._raw) <= self._CONV_BLOCK_POS:
            return None
        block = self._raw[self._CONV_BLOCK_POS]
        if not isinstance(block, list) or not block:
            return None
        return block

    @property
    def server_conversation_id(self) -> str | None:
        """Server conversation id at ``first[2][0]``.

        An absent / empty / non-list block legitimately means "no server
        conversation id present" (not drift) so it short-circuits to ``None``
        before invoking ``safe_index``.
        """
        block = self._turn_key_block
        if block is None:
            return None
        value = block[self._TURN_KEY_SESSION_ID_POS]
        return value if isinstance(value, str) else None

    @property
    def turn_key(self) -> ConversationTurnKey | None:
        """The whole ``ConversationTurnKey`` at ``first[2]`` — ``None`` when absent.

        Live-populated on every chunk of every ask (9/9 asks in the 2026-08-07
        audit, plus both probes for #2122). ``None`` when the block is absent,
        empty, not a list, or carries no usable id at slot 0 — the key is
        addressed *by* that id, so a key without one identifies nothing and is
        reported as absent rather than as a half-populated object.

        The two trailing slots are optional in the type (a short block yields
        ``None`` for them) but were populated in every observation; each is
        type-checked independently so one drifted slot does not discard the
        rest of the key. ``turn_code`` uses ``type(...) is int`` because
        ``bool`` is an ``int`` subclass and a wire ``true`` must not decode to
        the code ``1``.
        """
        block = self._turn_key_block
        if block is None:
            return None
        session_id = block[self._TURN_KEY_SESSION_ID_POS]
        if not isinstance(session_id, str) or not session_id:
            return None
        turn_id = (
            block[self._TURN_KEY_TURN_ID_POS] if len(block) > self._TURN_KEY_TURN_ID_POS else None
        )
        turn_code = (
            block[self._TURN_KEY_TURN_CODE_POS]
            if len(block) > self._TURN_KEY_TURN_CODE_POS
            else None
        )
        return ConversationTurnKey(
            session_id=session_id,
            turn_id=turn_id if isinstance(turn_id, str) and turn_id else None,
            turn_code=turn_code if type(turn_code) is int else None,
        )

    @property
    def _type_block(self) -> list[Any] | None:
        """The optional type/flags block at ``first[4]`` (a list) or ``None``.

        An absent block legitimately means "not an answer record" (non-answer
        records carry no type block), so a short row or a non-list slot
        short-circuits to ``None`` rather than tripping ``safe_index``.
        """
        if len(self._raw) <= self._TYPE_BLOCK_POS:
            return None
        block = self._raw[self._TYPE_BLOCK_POS]
        return block if isinstance(block, list) else None

    @property
    def has_response_doc(self) -> bool:
        """Whether the optional ``TailwindDoc`` slot is present."""
        return len(self._raw) > self._TYPE_BLOCK_POS and self._raw[self._TYPE_BLOCK_POS] is not None

    @property
    def suggests_wire_drift(self) -> bool:
        """Whether an *unmarked* row looks like drift rather than an empty answer.

        Drift when ``responseDoc`` is present but unmarked (the marker moved), or
        when the row is too short to reach ``first[3]`` — a row that could not even
        carry ``emptyAnswerReason`` is malformed, so "no doc" proves nothing about
        it. A row long enough to have spoken, with no doc, is the observed
        empty-answer shape and stays quiet.
        """
        return self.has_response_doc or len(self._raw) <= self._EMPTY_ANSWER_REASON_POS

    @property
    def is_answer(self) -> bool:
        """Whether the type block marks this record as an answer (``[4][4] == 1``).

        An absent / empty type block legitimately means "not an answer", so the
        flag read is a single-level ``type_block[4]`` index on a bound local
        rather than a chained ``first[4][4]`` descent.
        """
        type_block = self._type_block
        return (
            type_block is not None
            and len(type_block) > self._ANSWER_MARKER_POS
            and type_block[self._ANSWER_MARKER_POS] == self._ANSWER_MARKER_VALUE
        )

    @property
    def citations(self) -> list[Any]:
        """Raw citation entries at ``first[4][3]`` — empty list when absent.

        Absence-vs-malformed split (#1485 policy, the #1505 follow-up for
        the citation path):

        * **Absence stays soft** — a short row, a non-list type block, a
          type block too short to carry the slot, or a *falsy* slot all
          degrade to ``[]``: real wire traffic routinely sends ``None`` here
          for "answer without citations". The non-list *type block*
          (``first[4]``) is deliberately kept soft even though it could be
          drift: it doubles as the legitimate "not an answer record" shape
          consumed by :attr:`is_answer`. Visibility is narrow by design —
          it surfaces only on the stream path, and only when no other
          marked chunk wins (the parser's "No marked answer found"
          WARNING); on a losing chunk, or in a direct ``parse_citations``
          call, it stays silent.
        * **Truthy non-list RAISES** — a truthy non-list where the citation
          container belongs is structural wire drift, not a citation-less
          answer, and raises :class:`UnknownRPCMethodError`. Precedent: the
          :func:`unwrap_conversation_turns` container raise above and the
          ``inner_data[0]`` non-list raise in ``_web/rows/chat_stream.py`` — this
          parser family treats reshaped containers as a raise, never a
          silent ``[]``.
        """
        type_block = self._type_block
        if type_block is None or len(type_block) <= self._CITATIONS_POS:
            return []
        citations = type_block[self._CITATIONS_POS]
        if not citations:
            return []
        if not isinstance(citations, list):
            raise UnknownRPCMethodError(
                f"chat citation container holds {type(citations).__name__} "
                "(expected the citation list)",
                method_id=None,
                path=(self._TYPE_BLOCK_POS, self._CITATIONS_POS),
                source="ChatAnswerRow.citations",
                data_at_failure=reprlib.repr(citations),
            )
        return citations

    def citation_rows(self) -> list[CitationRow]:
        """Wrap each raw citation entry as a :class:`CitationRow`."""
        return [CitationRow(cite) for cite in self.citations]

    @property
    def document(self) -> StructuredDocument:
        """The answer's own ``responseDoc`` body as a :class:`StructuredDocument`.

        ``first[4]`` is the ``TailwindDoc`` this adapter otherwise only reads
        the answer marker and citation list out of; its ``body`` at ``[0]``
        carries the answer's paragraphs *and* the annotation map that anchors
        each citation to a range of the answer (#2120). An absent or malformed
        block yields an empty document — reshaped containers warn but never
        raise, deliberately, because this runs on the streamed-answer hot path
        and an answer without a decodable document is still an answer.

        The document's text is **not** :attr:`text`: the answer string carries
        markdown emphasis and inline ``[N]`` citation markers that the document
        does not, so it is longer and its offsets differ. Annotation ranges
        index ``document.text`` only.
        """
        type_block = self._type_block
        if type_block is None or len(type_block) <= self._DOC_BODY_POS:
            return StructuredDocument()
        return build_document(type_block[self._DOC_BODY_POS])


@dataclass(frozen=True)
class CitationRow:
    """Typed view of one streamed-chat citation entry (``type_info[3][i]``).

    Centralises the ``cite[0][0]`` chunk-id and ``cite[1]`` detail-block
    position knowledge. Consumer sites should NEVER open-code ``cite[0]`` /
    ``cite[1]``.
    """

    _raw: Any = field(repr=False)

    _CHUNK_BLOCK_POS: ClassVar[int] = 0
    _DETAIL_POS: ClassVar[int] = 1
    _MIN_LEN: ClassVar[int] = 2

    @property
    def is_well_formed(self) -> bool:
        """Whether the entry is a list long enough to carry chunk + detail."""
        return isinstance(self._raw, list) and len(self._raw) >= self._MIN_LEN

    @property
    def chunk_id(self) -> str | None:
        """Chunk id at ``cite[0][0]`` — the entry's ``DocumentObject.objectId``.

        The answer document's annotation map anchors on this same id, which is
        what lets an answer-side range be joined onto the citation (#2120). The
        ``Citation`` at ``cite[1]`` repeats it at ``[6]``; this is the one read.

        An absent / empty / non-list chunk block legitimately means "no chunk
        id" (the citation is still kept), so it short-circuits to ``None``.
        """
        if not self.is_well_formed:
            return None
        chunk_block = self._raw[self._CHUNK_BLOCK_POS]
        if not isinstance(chunk_block, list) or not chunk_block:
            return None
        value = chunk_block[0]
        return value if isinstance(value, str) else None

    @property
    def detail(self) -> CitationDetail | None:
        """The citation detail block at ``cite[1]`` as a :class:`CitationDetail`.

        Returns ``None`` when the entry is too short or ``cite[1]`` is not a
        list — both legitimately mean "unusable citation, skip it" rather than
        wire drift.
        """
        if not self.is_well_formed:
            return None
        inner = self._raw[self._DETAIL_POS]
        if not isinstance(inner, list):
            return None
        return CitationDetail(inner)


@dataclass(frozen=True)
class CitationDetail:
    """Typed view of a citation detail block (``cite[1]``) — a ``Citation``.

    Centralises the score / fragment-range / fragment-elements / source-id
    position knowledge. Consumer sites should NEVER open-code ``cite_inner[2]``
    / ``cite_inner[3]`` / ``cite_inner[4]`` / ``cite_inner[5]`` /
    ``cite_inner[6]``.

    ``cite_inner[4]`` is ``Citation.fragment``, a ``TailwindDocFragment``
    *message* whose only field is ``elements`` at ``[0]``. Reading
    ``cite_inner[4]`` itself therefore yields a one-element list — the fragment
    wrapper, not the fragment's blocks. Before #2120 that one wrapper was
    mistaken for the passage list, so the passage loop ran exactly once and
    ``cited_text`` was truncated to the fragment's first block (37 of 556
    characters in the issue's live capture). :attr:`fragment_elements` performs
    the full two-level descent.
    """

    _raw: list[Any] = field(repr=False)

    _SCORE_POS: ClassVar[int] = 2
    _FRAGMENT_RANGE_POS: ClassVar[int] = 3
    _FRAGMENT_POS: ClassVar[int] = 4
    _SOURCE_ID_POS: ClassVar[int] = 5

    # ``TailwindDocFragment.elements`` — the second level of the descent.
    _FRAGMENT_ELEMENTS_POS: ClassVar[int] = 0

    # Inner fragment-range layout: ``cite_inner[3] = [[None, start, end]]``.
    _FRAGMENT_RANGE_START_POS: ClassVar[int] = 1
    _FRAGMENT_RANGE_END_POS: ClassVar[int] = 2

    @property
    def raw_list(self) -> list[Any]:
        """The wrapped ``cite[1]`` detail list (for legacy raw-list consumers)."""
        return self._raw

    @property
    def raw_score(self) -> Any:
        """Raw value at ``cite_inner[2]`` (validation lives in the caller)."""
        if len(self._raw) <= self._SCORE_POS:
            return None
        return self._raw[self._SCORE_POS]

    @property
    def source_id_data(self) -> Any:
        """Nested source-id data at ``cite_inner[5]`` — ``None`` when absent."""
        if len(self._raw) <= self._SOURCE_ID_POS:
            return None
        return self._raw[self._SOURCE_ID_POS]

    @property
    def fragment_elements(self) -> list[Any]:
        """``Citation.fragment.elements`` at ``cite_inner[4][0]`` — ``[]`` when absent.

        The full two-level descent: ``cite_inner[4]`` is the
        ``TailwindDocFragment`` *message* and its ``elements`` field sits one
        level below at ``[0]``. Each element is a ``StructuralElement``
        (``[startIndex, endIndex, paragraph]``) sliced out of the source
        document — decode them with
        :func:`notebooklm._web.rows.documents.build_blocks`.

        Stopping at ``cite_inner[4]`` (the pre-#2120 behavior) yields the
        one-element wrapper instead, which is why ``cited_text`` covered only
        the fragment's first block.
        """
        if len(self._raw) <= self._FRAGMENT_POS:
            return []
        fragment = self._raw[self._FRAGMENT_POS]
        if not isinstance(fragment, list) or len(fragment) <= self._FRAGMENT_ELEMENTS_POS:
            return []
        elements = fragment[self._FRAGMENT_ELEMENTS_POS]
        return elements if isinstance(elements, list) else []

    def fragment_range(self) -> tuple[Any, Any]:
        """Raw ``(start, end)`` from ``cite_inner[3][0]`` (``[None, start, end]``).

        The server's own statement of the cited fragment's **source-side**
        character range — the union of every element range in
        :attr:`fragment_elements`, in the same coordinate space as this
        client's ``start_char`` / ``end_char``. It is *not* an answer-text
        range, despite the ``answer_start_char`` / ``answer_end_char`` names
        this client exposed it under before #2120; a live capture showed
        ``[1130, 1695]`` on a 536-character answer.

        Returns ``(None, None)`` when the range block is absent, not a list,
        empty, its first element is not a list, or that inner list is too
        short — all legitimate "no range" shapes, not drift. The numeric /
        ordering validation lives in the caller.
        """
        if len(self._raw) <= self._FRAGMENT_RANGE_POS:
            return None, None
        outer = self._raw[self._FRAGMENT_RANGE_POS]
        if not isinstance(outer, list) or not outer:
            return None, None
        inner = outer[0]
        if not isinstance(inner, list) or len(inner) <= self._FRAGMENT_RANGE_END_POS:
            return None, None
        return inner[self._FRAGMENT_RANGE_START_POS], inner[self._FRAGMENT_RANGE_END_POS]
