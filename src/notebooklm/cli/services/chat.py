"""CLI adapter for the chat commands — thin re-export over ``_app``.

The ``ask`` / ``configure`` / ``history`` business logic (the conversation-id
selection ladder, the ``configure`` mode/goal/length mapping, the ``history``
fetch + note-content formatting, and the ``ask`` save-as-note workflow) now
lives in the transport-neutral :mod:`notebooklm._app.chat`. This module
re-exports the neutral result/helper names so the command layer in
``cli/chat_cmd.py`` keeps resolving them by local name (preserving the
``patch("...chat_cmd.NotebookLMClient")`` seams). The mutual-exclusion validator
raises the public :class:`~notebooklm.exceptions.ValidationError`, which the
command maps to its own ``--json`` envelope / Click ``UsageError``.

The Rich-coupled :class:`ProgressSink` implementations that route the neutral
status events through ``cli_print`` / ``emit_status`` live in the command module
(``cli/chat_cmd.py``), NOT here: this service module must stay free of any
``..rendering`` reach-in to satisfy the ADR-0008 service boundary
(``tests/unit/cli/test_services_boundary.py``). Command-layer rendering (the
streamed answer display, the ``--json`` envelope) and exit codes stay in
``cli/chat_cmd.py``.
"""

from __future__ import annotations

from ..._app.chat import (
    ChatModeChoice,
    ClearCacheResult,
    ConfigureResult,
    HistoryFetch,
    ResponseLengthChoice,
    SaveNoteOutcome,
    determine_conversation_id,
    execute_clear_cache,
    execute_configure,
    fetch_history,
    format_history,
    format_single_qa,
    get_latest_conversation_from_server,
    save_answer_as_note,
    validate_ask_flags,
)

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
