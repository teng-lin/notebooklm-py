"""Transport-neutral chat business logic.

This is the Click-free core of the ``ask`` / ``configure`` / ``history`` CLI
commands: it owns the conversation-id selection ladder, the ``configure``
mode/goal/length mapping + dispatch, the ``history`` fetch + note-content
formatting, and the ``ask`` save-as-note workflow. Every transport adapter (the
Click CLI today, the FastMCP server / future HTTP later) drives this core and
renders the typed result / status events into its own surface + exit-code
policy.

``_app`` returns **typed results only** — no ``--json`` envelope dicts. Each
adapter builds its own envelope from the typed fields (e.g. the CLI's
``history``/``configure``/``history --clear`` ``--json`` payloads). The
``format_*`` helpers here produce *note content* (a saved-note markdown body, a
domain artifact), not a presentation envelope, so they stay neutral.

Boundary-imposed seams worth calling out:

* **Status prose is emitted into an injected :class:`ProgressSink`, never
  printed.** The conversation-selection status lines (``"Continuing
  conversation ..."`` etc.) and the save-as-note status lines carry Rich markup
  but are emitted as :class:`ProgressEvent` messages so the CLI adapter routes
  them through its markup-aware ``cli_print`` / ``emit_status`` (honoring root
  ``--quiet`` / JSON-stdout purity). This module never touches a console.

* **The cached-context reads are injected.** :func:`determine_conversation_id`
  takes the cached notebook/conversation values as plain arguments so the CLI
  adapter supplies them from its ``get_current_notebook`` /
  ``get_current_conversation`` context helpers (preserving the
  ``patch("...chat_cmd.get_current_*")`` test seams in the adapter layer).

Conversation IDs pass straight through (no validation / resolution here) — the
server owns their shape and a partial/explicit id is forwarded verbatim to
``ChatAPI.ask``.

This module is transport-neutral — no ``click`` / ``rich`` / ``cli`` /
``fastmcp`` imports (enforced by ``tests/_guardrails/test_app_boundary.py``).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from ..exceptions import ValidationError
from ..rpc.types import ChatGoal, ChatResponseLength
from ..types import ChatMode
from .events import ProgressEvent, ProgressSink

if TYPE_CHECKING:
    from ..types import AskResult

logger = logging.getLogger(__name__)


def _emit(progress: ProgressSink | None, message: str, *, kind: str = "status") -> None:
    """Best-effort status emission into the optional sink."""
    if progress is not None:
        progress.emit(ProgressEvent(message=message, kind=kind))


# ---------------------------------------------------------------------------
# ask: mutual-exclusion validation + conversation-id selection ladder
# ---------------------------------------------------------------------------


def validate_ask_flags(*, new_conversation: bool, conversation_id: str | None) -> None:
    """Reject the ``--new`` / ``--conversation-id`` mutual-exclusion conflict.

    Raises the public :class:`~notebooklm.exceptions.ValidationError` (which
    ``_app.errors.classify`` maps to ``ErrorCategory.VALIDATION`` → the CLI's
    ``VALIDATION_ERROR`` code) so every adapter projects it onto its own
    code/exit vocabulary uniformly.

    Raises:
        ValidationError: when both ``--new`` and ``--conversation-id`` are
            supplied (the command layer maps this to its own envelope / exit).
    """
    if new_conversation and conversation_id:
        raise ValidationError(
            "--new and --conversation-id are mutually exclusive: "
            "--new starts a fresh conversation while --conversation-id resumes a specific one."
        )


def determine_conversation_id(
    *,
    explicit_conversation_id: str | None,
    explicit_notebook_id: str | None,
    resolved_notebook_id: str,
    cached_notebook_id: Callable[[], str | None],
    cached_conversation_id: Callable[[], str | None],
    progress: ProgressSink | None = None,
) -> str | None:
    """Select the conversation ID to continue for the ``ask`` command.

    The selection ladder (decision 6: an explicit id passes straight through):

    1. An explicit ``--conversation-id`` wins outright.
    2. A ``--notebook`` switch (explicit notebook differs from the cached one)
       starts a fresh conversation (emits a status line, returns ``None``).
    3. Otherwise fall back to the cached conversation id (may be ``None``).

    The cached notebook/conversation values are read through **lazy** callables
    (the CLI adapter supplies its ``get_current_notebook`` /
    ``get_current_conversation`` context helpers) so this core stays free of the
    CLI context layer AND preserves the historical short-circuit: an explicit
    ``--conversation-id`` reads neither helper, and the notebook-switch branch
    reads only the notebook helper — exactly as the pre-relocation command did
    (no extra context-file touches on those paths). Status prose is emitted into
    the optional :class:`ProgressSink`.
    """
    if explicit_conversation_id:
        return explicit_conversation_id

    # Check if the user switched notebooks via the --notebook flag. Only read
    # the cached notebook when an explicit one was supplied (matches the
    # historical lazy read; ``and`` short-circuits before the callable fires).
    if explicit_notebook_id and (cached := cached_notebook_id()) and resolved_notebook_id != cached:
        _emit(progress, "[dim]Different notebook specified, starting new conversation...[/dim]")
        return None

    return cached_conversation_id()


async def get_latest_conversation_from_server(
    client: Any,
    notebook_id: str,
    *,
    progress: ProgressSink | None = None,
) -> str | None:
    """Fetch the most recent conversation ID from the server.

    Returns ``None`` if unavailable or empty. A server-fetch failure is logged
    at DEBUG and folded into a neutral "starting new conversation" status line —
    never re-raised — so the caller can proceed to create a fresh conversation.
    """
    history_unavailable = False
    try:
        conv_id = await client.chat.get_conversation_id(notebook_id)
        if conv_id:
            _emit(progress, f"[dim]Continuing conversation {conv_id[:8]}...[/dim]")
            return conv_id
    except Exception as e:
        logger.debug("Failed to fetch last conversation (%s): %s", type(e).__name__, e)
        history_unavailable = True
    # Emit the fallback status *outside* the ``except`` handler: it is a status
    # line (best-effort, honoring root --quiet via the sink), not an error
    # diagnostic, and emitting it inside the handler would trip the error-path
    # heuristic in the CLI quiet-enforcement test.
    if history_unavailable:
        _emit(progress, "[dim]Starting new conversation (history unavailable)[/dim]")
    return None


# ---------------------------------------------------------------------------
# ask: save-as-note workflow
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SaveNoteOutcome:
    """Outcome of the ``ask --save-as-note`` secondary action.

    Exactly one of ``note`` / ``error`` is populated. ``status_message`` carries
    the (Rich-markup) status line the adapter emits; it is set on the success,
    plain-text-fallback, and no-answer paths. The two booleans let the adapter
    reproduce the historical ordering of its status emissions.
    """

    note: dict[str, str] | None = None
    error: str | None = None
    status_message: str | None = None
    plain_text_fallback: bool = False


async def save_answer_as_note(
    client: Any,
    notebook_id: str,
    result: AskResult,
    *,
    note_title: str | None,
    question: str,
    progress: ProgressSink | None = None,
) -> SaveNoteOutcome:
    """Save an ``ask`` answer as a note, preserving citations when present.

    Mirrors the historical CLI flow:

    * No answer to save → a typed no-answer outcome (the adapter renders the
      warning + records ``note_save_error``).
    * Citation-rich answer → ``client.chat.save_answer_as_note`` (server stores
      ``[N]`` markers as hover-anchored references, issue #660).
    * No citations → a plain-text ``client.notes.create`` fallback (emits a
      status line so the adapter can surface the downgrade).
    * Any exception → folded into the outcome's ``error`` so the chat response
      payload still prints (save-as-note is a non-fatal secondary action).

    Status prose is emitted into the optional sink; the adapter also reads
    :attr:`SaveNoteOutcome.status_message` for the JSON-envelope merge.
    """
    if not result.answer:
        _emit(progress, "[yellow]Warning: No answer to save as note[/yellow]")
        return SaveNoteOutcome(error="No answer to save as note")

    try:
        title = note_title or f"Chat: {question[:50].strip().replace(chr(10), ' ')}"
        plain_text_fallback = False
        if result.references:
            # Citation-rich path: server stores [N] markers as hover-anchored
            # references (issue #660).
            note = await client.chat.save_answer_as_note(notebook_id, result, title=title)
        else:
            # No citations to preserve -- fall back to the plain-text path so the
            # save still succeeds.
            _emit(progress, "[dim]No citations in answer; saving as plain-text note.[/dim]")
            note = await client.notes.create(notebook_id, title, result.answer)
            plain_text_fallback = True
        saved_status = f"\n[dim]Saved as note: {note.title} ({note.id[:8]}...)[/dim]"
        _emit(progress, saved_status)
        return SaveNoteOutcome(
            note={"id": note.id, "title": note.title},
            status_message=saved_status,
            plain_text_fallback=plain_text_fallback,
        )
    except Exception as e:
        # Non-fatal: the chat response payload must still print, so the error is
        # returned (not raised) for the adapter to render as a warning.
        _emit(progress, f"[yellow]Warning: Failed to save note: {e}[/yellow]")
        return SaveNoteOutcome(error=str(e))


# ---------------------------------------------------------------------------
# configure
# ---------------------------------------------------------------------------

ChatModeChoice = Literal["default", "learning-guide", "concise", "detailed"]
ResponseLengthChoice = Literal["default", "longer", "shorter"]

_MODE_MAP: dict[str, ChatMode] = {
    "default": ChatMode.DEFAULT,
    "learning-guide": ChatMode.LEARNING_GUIDE,
    "concise": ChatMode.CONCISE,
    "detailed": ChatMode.DETAILED,
}

_RESPONSE_LENGTH_MAP: dict[str, ChatResponseLength] = {
    "default": ChatResponseLength.DEFAULT,
    "longer": ChatResponseLength.LONGER,
    "shorter": ChatResponseLength.SHORTER,
}


@dataclass(frozen=True)
class ConfigureResult:
    """Typed outcome of ``configure`` — neutral fields only.

    Discriminated by :attr:`mode`: a non-``None`` :attr:`mode` means a
    predefined ``--mode`` was applied; ``None`` means the persona /
    response-length branch ran. The CLI adapter builds its stable ``--json``
    envelope from these fields (``_app`` returns typed results only;
    envelope-building stays in the adapter).

    :attr:`goal_name` is the lowercase enum name (e.g. ``"custom"``) when a
    persona selected the ``CUSTOM`` goal, else ``None``.
    """

    notebook_id: str
    mode: str | None
    goal_name: str | None
    persona: str | None
    response_length: str | None


async def execute_configure(
    client: Any,
    notebook_id: str,
    *,
    chat_mode: ChatModeChoice | None,
    persona: str | None,
    response_length: ResponseLengthChoice | None,
) -> ConfigureResult:
    """Apply chat configuration and return a typed :class:`ConfigureResult`.

    A predefined ``--mode`` short-circuits to ``client.chat.set_mode`` (and the
    result carries the mode for the adapter's envelope/prose). Otherwise the
    persona / response-length settings are applied via ``client.chat.configure``;
    a non-empty ``persona`` selects the ``CUSTOM`` goal. The ``goal_name`` is the
    lowercase enum name (e.g. ``"custom"``) for a stable, human-readable JSON
    contract.
    """
    if chat_mode:
        try:
            mode = _MODE_MAP[chat_mode]
        except KeyError as exc:
            raise ValidationError(
                f"Unknown chat mode {chat_mode!r}; expected one of {sorted(_MODE_MAP)}"
            ) from exc
        await client.chat.set_mode(notebook_id, mode)
        return ConfigureResult(
            notebook_id=notebook_id,
            mode=chat_mode,
            goal_name=None,
            persona=None,
            response_length=None,
        )

    goal = ChatGoal.CUSTOM if persona else None
    if response_length:
        try:
            length = _RESPONSE_LENGTH_MAP[response_length]
        except KeyError as exc:
            raise ValidationError(
                f"Unknown response length {response_length!r}; "
                f"expected one of {sorted(_RESPONSE_LENGTH_MAP)}"
            ) from exc
    else:
        length = None

    await client.chat.configure(
        notebook_id, goal=goal, response_length=length, custom_prompt=persona
    )
    return ConfigureResult(
        notebook_id=notebook_id,
        mode=None,
        goal_name=goal.name.lower() if goal else None,
        persona=persona,
        response_length=response_length,
    )


# ---------------------------------------------------------------------------
# history
# ---------------------------------------------------------------------------


def format_single_qa(question: str, answer: str) -> str:
    """Format one Q&A pair as note content."""
    parts = []
    if question:
        parts.append(f"**Q:** {question}")
    if answer:
        parts.append(f"**A:** {answer}")
    return "\n\n".join(parts)


def format_history(qa_pairs: list[tuple[str, str]]) -> str:
    """Format Q&A history as note content."""
    turns = []
    for i, (question, answer) in enumerate(qa_pairs, 1):
        turns.append(f"### Turn {i}\n\n{format_single_qa(question, answer)}")
    return "\n\n---\n\n".join(turns)


@dataclass(frozen=True)
class HistoryFetch:
    """The conversation-id + Q&A turns fetched for the ``history`` command."""

    conversation_id: str | None
    qa_pairs: list[tuple[str, str]]


async def fetch_history(client: Any, notebook_id: str, *, limit: int) -> HistoryFetch:
    """Fetch the last conversation's id and its Q&A turns.

    Preserves the historical RPC order: ``get_conversation_id``
    (GET_LAST_CONVERSATION_ID) then ``get_history`` (GET_CONVERSATION_TURNS).
    """
    conversation_id = await client.chat.get_conversation_id(notebook_id)
    qa_pairs = await client.chat.get_history(
        notebook_id, limit=limit, conversation_id=conversation_id
    )
    return HistoryFetch(conversation_id=conversation_id, qa_pairs=qa_pairs)


@dataclass(frozen=True)
class ClearCacheResult:
    """Typed outcome of ``history --clear`` — neutral fields only.

    The CLI adapter builds its ``--json`` envelope (``{"cleared", "count"}``)
    from these fields.
    """

    cleared: bool
    count: int


def execute_clear_cache(client: Any) -> ClearCacheResult:
    """Clear the local conversation cache, capturing the pre-clear count.

    The pre-clear size is captured BEFORE the clear because ``clear_cache``
    returns only a bool; the count reports what was dropped (0 when nothing was
    cleared).
    """
    pre_clear_count = client.chat.cache_size()
    cleared = bool(client.chat.clear_cache())
    return ClearCacheResult(cleared=cleared, count=pre_clear_count if cleared else 0)


__all__ = [
    "ChatModeChoice",
    "ClearCacheResult",
    "ConfigureResult",
    "HistoryFetch",
    "ResponseLengthChoice",
    "SaveNoteOutcome",
    "determine_conversation_id",
    "execute_clear_cache",
    "execute_configure",
    "fetch_history",
    "format_history",
    "format_single_qa",
    "get_latest_conversation_from_server",
    "save_answer_as_note",
    "validate_ask_flags",
]
