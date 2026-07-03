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

These adapters centralise the positional knowledge that ``_chat/wire.py``
previously open-coded as scattered single-level subscripts (``first[4]``,
``cite[1]``, ``cite_inner[5]``, ``passage_data[0]`` …). Consumer sites should
wrap the raw lists in the typed views below and read named properties so a
future Google reshape of the chat wire format is a one-place fix here, and so
genuine drift RAISES ``UnknownRPCMethodError`` via ``safe_index`` instead of
silently degrading to an empty/wrong answer.

Position contracts (pinned by ``tests/unit/test_chat_row_adapter.py``):

* :class:`AnswerRow` — one populated answer record (``inner_data[0]``):

  =====  ============================================================
  Index  Meaning
  =====  ============================================================
  0      answer text (str)
  2      conversation-id block; ``[2][0]`` is the server conversation id
  4      type/flags block; ``[4][-1] == 1`` marks an answer record and
         ``[4][3]`` is the citation list
  =====  ============================================================

* :class:`CitationRow` — one citation entry (``type_info[3][i]``):

  =====  ============================================================
  Index  Meaning
  =====  ============================================================
  0      chunk-id block; ``[0][0]`` is the chunk id
  1      citation detail block (:class:`CitationDetail`)
  =====  ============================================================

* :class:`CitationDetail` — ``cite[1]``:

  =====  ============================================================
  Index  Meaning
  =====  ============================================================
  2      relevance score (float 0.0-1.0)
  3      answer-range block ``[[None, answer_start, answer_end]]``
  4      source-side passages list
  5      nested source-id data
  =====  ============================================================

* :class:`PassageRow` — one passage record (``passage_wrapper[0]``):

  =====  ============================================================
  Index  Meaning
  =====  ============================================================
  0      source-side start char (int)
  1      source-side end char (int)
  2      nested text payload
  =====  ============================================================

* :class:`ConversationTurnRow` — one ``GET_CONVERSATION_TURNS`` turn row:

  =====  ============================================================
  Index  Meaning
  =====  ============================================================
  2      role code (``1`` = user question, ``2`` = AI answer)
  3      question text (str) — only on role-1 rows
  4      nested answer-content payload — only on role-2 rows
         (the ``[4][0][0]`` leaf descent lives in
         ``_chat.api._extract_next_turn_content``)
  =====  ============================================================
"""

from __future__ import annotations

import reprlib
from dataclasses import dataclass, field
from typing import Any, ClassVar

from ..exceptions import UnknownRPCMethodError
from ..rpc import RPCMethod, safe_index

__all__ = [
    "AnswerRow",
    "CitationRow",
    "CitationDetail",
    "ChatSettingsRow",
    "ConversationTurnRow",
    "ErrorPayloadRow",
    "PassageRow",
    "StreamFrameRow",
    "TextLeafRow",
    "SavedChatNoteRow",
    "unwrap_chat_settings",
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


def unwrap_last_conversation_id(raw: Any) -> str | None:
    """Return the most-recent conversation id from a ``GET_LAST_CONVERSATION_ID`` payload.

    The wire shape is the nested ``[[[conv_id]]]`` envelope: an outer list of
    groups, each a list of rows, each row an innermost ``[conv_id]`` list whose
    first element is the id string. This centralises the ``conv[0]`` descent
    ``_chat/api.py`` previously open-coded, and is **deliberately SOFT** —
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
    container probe so ``_chat/api.py`` stops open-coding it, with the
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
    that ``_chat/notes.py`` previously open-coded, so a future reshape is a
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
    ``_chat.api._extract_next_turn_content``). Position knowledge is
    centralised here; ``_chat/api.py`` should NEVER open-code ``turn[2]`` /
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


@dataclass(frozen=True)
class StreamFrameRow:
    """Typed view of one streamed-chat envelope frame.

    Frames arrive as ``["wrb.fr", None, inner_json, ...]`` (a successful RPC
    result) or ``["er", rpc_id, code, ...]`` (a server-side error). This adapter
    centralises the ``item[0]`` tag, ``item[2]`` inner-JSON / error-code, and
    ``item[5]`` error-payload reads so ``_chat/wire.py`` stops open-coding them
    (issue #1491).

    The ``tag`` read goes through ``safe_index`` because the caller already
    guarantees ``len(item) >= 2`` — the frame tag slot is the one position that
    must always be present, so its absence is genuine drift. Every other slot is
    optional and short-circuits to ``None`` on a short frame.
    """

    _raw: list[Any] = field(repr=False)

    _TAG_POS: ClassVar[int] = 0
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
class ErrorPayloadRow:
    """Typed view of a streamed-chat error payload (``item[5]``).

    Structure: ``[8, None, [["type.googleapis.com/.../UserDisplayableError", …]]]``
    for the rich rate-limit shape, or a bare ``[3]`` status for a request-level
    rejection (issue #1472). Centralises the ``error_payload[0]`` status, the
    ``error_payload[2]`` entries, and the inner ``entry[0]`` reads so
    ``raise_if_rate_limited`` / ``_raise_chat_rejection`` stop open-coding them
    (issue #1491).
    """

    _raw: list[Any] = field(repr=False)

    _STATUS_POS: ClassVar[int] = 0
    _ENTRIES_POS: ClassVar[int] = 2

    @property
    def status_code(self) -> Any:
        """Leading status/code at ``error_payload[0]`` (e.g. ``3`` for a bare
        ``[3]`` rejection, ``8`` for the rate-limit shape) — ``None`` if absent."""
        if not self._raw:
            return None
        return self._raw[self._STATUS_POS]

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
class TextLeafRow:
    """Typed view of one deeply-nested passage text leaf (``inner`` triple).

    Centralises the ``inner[2]`` text-payload read in ``collect_texts_from_nested``
    so the nested-walk decoder stops open-coding the position (issue #1491).
    """

    _raw: Any = field(repr=False)

    _TEXT_POS: ClassVar[int] = 2
    _MIN_LEN: ClassVar[int] = 3

    @property
    def is_well_formed(self) -> bool:
        """Whether the leaf is a list long enough to carry the text payload."""
        return isinstance(self._raw, list) and len(self._raw) >= self._MIN_LEN

    @property
    def text_value(self) -> Any:
        """Raw text payload at ``inner[2]`` (str / list validated upstream)."""
        if not self.is_well_formed:
            return None
        return self._raw[self._TEXT_POS]


@dataclass(frozen=True)
class AnswerRow:
    """Typed view of one populated streamed-chat answer record.

    The wrapped row is ``inner_data[0]`` of a decoded ``wrb.fr`` envelope
    whose ``inner_data`` is a populated list (heartbeats decode to ``[]``
    and never reach this adapter). Position knowledge is centralised here;
    consumer sites should NEVER open-code ``first[0]`` / ``first[2][0]`` /
    ``first[4][-1]`` / ``first[4][3]``.

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
    _TYPE_BLOCK_POS: ClassVar[int] = 4
    _ANSWER_MARKER_POS: ClassVar[int] = -1
    _CITATIONS_POS: ClassVar[int] = 3
    _ANSWER_MARKER_VALUE: ClassVar[int] = 1

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
    def server_conversation_id(self) -> str | None:
        """Server conversation id at ``first[2][0]``.

        An absent / empty / non-list block legitimately means "no server
        conversation id present" (not drift) so it short-circuits to ``None``
        before invoking ``safe_index``.
        """
        if len(self._raw) <= self._CONV_BLOCK_POS:
            return None
        conv_block = self._raw[self._CONV_BLOCK_POS]
        if not isinstance(conv_block, list) or not conv_block:
            return None
        value = conv_block[0]
        return value if isinstance(value, str) else None

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
    def is_answer(self) -> bool:
        """Whether the type block marks this record as an answer (``[4][-1] == 1``).

        An absent / empty type block legitimately means "not an answer", so the
        flag read is a single-level ``type_block[-1]`` index on a bound local
        rather than a chained ``first[4][-1]`` descent.
        """
        type_block = self._type_block
        return (
            type_block is not None
            and len(type_block) > 0
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
          ``inner_data[0]`` non-list raise in ``_chat/wire.py`` — this
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
        """Chunk id at ``cite[0][0]``.

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
    """Typed view of a citation detail block (``cite[1]``).

    Centralises the score / answer-range / passages / source-id position
    knowledge. Consumer sites should NEVER open-code ``cite_inner[2]`` /
    ``cite_inner[3]`` / ``cite_inner[4]`` / ``cite_inner[5]``.
    """

    _raw: list[Any] = field(repr=False)

    _SCORE_POS: ClassVar[int] = 2
    _ANSWER_RANGE_POS: ClassVar[int] = 3
    _PASSAGES_POS: ClassVar[int] = 4
    _SOURCE_ID_POS: ClassVar[int] = 5

    # Inner answer-range layout: ``cite_inner[3] = [[None, start, end]]``.
    _ANSWER_RANGE_START_POS: ClassVar[int] = 1
    _ANSWER_RANGE_END_POS: ClassVar[int] = 2

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
    def passages(self) -> list[Any]:
        """Source-side passages list at ``cite_inner[4]`` — ``[]`` when absent."""
        if len(self._raw) <= self._PASSAGES_POS:
            return []
        value = self._raw[self._PASSAGES_POS]
        return value if isinstance(value, list) else []

    def answer_range(self) -> tuple[Any, Any]:
        """Raw ``(start, end)`` from ``cite_inner[3][0]`` (``[None, start, end]``).

        Returns ``(None, None)`` when the answer-range block is absent,
        not a list, empty, its first element is not a list, or that inner
        list is too short — all legitimate "no answer range" shapes, not drift.
        The numeric / ordering validation lives in the caller.
        """
        if len(self._raw) <= self._ANSWER_RANGE_POS:
            return None, None
        outer = self._raw[self._ANSWER_RANGE_POS]
        if not isinstance(outer, list) or not outer:
            return None, None
        inner = outer[0]
        if not isinstance(inner, list) or len(inner) <= self._ANSWER_RANGE_END_POS:
            return None, None
        return inner[self._ANSWER_RANGE_START_POS], inner[self._ANSWER_RANGE_END_POS]


@dataclass(frozen=True)
class PassageRow:
    """Typed view of one source-side passage *wrapper* (``cite_inner[4][i]``).

    The wrapped value is the outer ``passage_wrapper`` (``[passage_data, …]``);
    the adapter unwraps the inner ``passage_data`` at ``[0]`` and centralises its
    ``[0]`` / ``[1]`` / ``[2]`` start / end / text-payload reads. Consumer sites
    should NEVER open-code ``passage_wrapper[0]`` or ``passage_data[0..2]``
    (issue #1491).
    """

    _raw: Any = field(repr=False)

    _PASSAGE_DATA_POS: ClassVar[int] = 0
    _START_POS: ClassVar[int] = 0
    _END_POS: ClassVar[int] = 1
    _TEXT_PAYLOAD_POS: ClassVar[int] = 2
    _MIN_LEN: ClassVar[int] = 3

    @property
    def _passage_data(self) -> list[Any] | None:
        """Inner ``passage_data`` at ``passage_wrapper[0]`` when well-formed.

        Returns ``None`` (rather than raising) for an empty wrapper or an inner
        record too short to carry start/end/text — both legitimate "skip this
        passage" shapes, not wire drift.
        """
        if not isinstance(self._raw, list) or len(self._raw) <= self._PASSAGE_DATA_POS:
            return None
        data = self._raw[self._PASSAGE_DATA_POS]
        if not isinstance(data, list) or len(data) < self._MIN_LEN:
            return None
        return data

    @property
    def is_well_formed(self) -> bool:
        """Whether the wrapper holds an inner record long enough for start/end/text."""
        return self._passage_data is not None

    @property
    def start_char(self) -> Any:
        """Raw source-side start char at ``passage_data[0]`` (int validated upstream)."""
        data = self._passage_data
        return None if data is None else data[self._START_POS]

    @property
    def end_char(self) -> Any:
        """Raw source-side end char at ``passage_data[1]`` (int validated upstream)."""
        data = self._passage_data
        return None if data is None else data[self._END_POS]

    @property
    def text_payload(self) -> Any:
        """Nested text payload at ``passage_data[2]`` — ``None`` when malformed."""
        data = self._passage_data
        return None if data is None else data[self._TEXT_PAYLOAD_POS]
