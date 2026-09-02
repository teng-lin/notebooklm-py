"""Notebook row adapters for ``batchexecute`` notebook-scoped RPCs.

Centralises the positional knowledge of the ``Project`` message returned by
notebook reads/creates and of the ``GeneratePromptSuggestions``
(``otmP3b`` / ``SUGGEST_PROMPTS``) reply.

Position contract (pinned by ``tests/unit/test_notebooks_row_adapter.py``):

* :class:`ProjectRow` — one decoded ``Project`` message:

  =====  ============================================================
  Index  Meaning
  =====  ============================================================
  3      notebook emoji
  9      ``PremiumFeatureInfo``
  11     ``ChatSession`` rows (CREATE response only)
  =====  ============================================================

* :class:`PromptSuggestionRow` — one suggestion row:

  =====  ============================================================
  Index  Meaning
  =====  ============================================================
  0      title (str)
  1      prompt (str) — a ready-to-send multi-line instruction
  =====  ============================================================
"""

from __future__ import annotations

import logging
import re
import reprlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar

from ..._types.common import _datetime_from_timestamp
from ..._types.enums import ChatGoal, ChatResponseLength, SharePermission
from ...exceptions import UnknownRPCMethodError
from ...rpc import RPCMethod, safe_index

if TYPE_CHECKING:
    from ..._types.notebooks import Notebook

logger = logging.getLogger("notebooklm._types.notebooks")

__all__ = [
    "PromptSuggestionRow",
    "ProjectRow",
    "decode_notebook",
    "unwrap_next_step_suggestions",
    "unwrap_prompt_suggestions",
]

_NOTEBOOK_METHOD_ID = RPCMethod.LIST_NOTEBOOKS.value
_NOTEBOOK_ROLES = frozenset(
    {SharePermission.OWNER.value, SharePermission.EDITOR.value, SharePermission.VIEWER.value}
)


def _extract_notebook_sources_count(data: list[Any]) -> int:
    """Extract the embedded source count from a notebook API payload."""
    sources = (
        safe_index(data, 1, method_id=_NOTEBOOK_METHOD_ID, source="Notebook.sources_count")
        if len(data) > 1
        else None
    )
    return len(sources) if isinstance(sources, list) else 0


def _role_from_wire(raw_role: Any, row: list[Any]) -> SharePermission | None:
    """Map a raw ``userRole`` slot to :class:`SharePermission`, or ``None``."""
    if raw_role is None:
        return None
    if isinstance(raw_role, int) and not isinstance(raw_role, bool):
        if raw_role in _NOTEBOOK_ROLES:
            return SharePermission(raw_role)
    logger.warning(
        "Notebook row userRole slot unmapped — reporting unknown role "
        "(expected 1/2/3 at data[5][0], got %r; row=%s)",
        raw_role,
        reprlib.repr(row),
    )
    return None


@dataclass(frozen=True)
class ProjectRow:
    """Best-effort typed view over one decoded ``Project`` message.

    ``Notebook.from_api_response`` deliberately tolerates short rows because it
    also maps whole notebook listings: one partial row must not discard its
    siblings. These newly surfaced fields follow that contract. Missing or
    malformed optional leaves degrade to ``None``/``[]``; the load-bearing
    title/id/metadata handling remains owned by ``Notebook``.
    """

    _raw: Any = field(repr=False)

    _EMOJI_POS: ClassVar[int] = 3
    _PREMIUM_FEATURE_INFO_POS: ClassVar[int] = 9
    _CHAT_SESSIONS_POS: ClassVar[int] = 11

    @property
    def emoji(self) -> str | None:
        """Notebook emoji (``Project.emoji``), or ``None`` when unstated."""
        if not isinstance(self._raw, list) or len(self._raw) <= self._EMOJI_POS:
            return None
        value = self._raw[self._EMOJI_POS]
        return value if isinstance(value, str) else None

    @property
    def premium_feature_flags(self) -> tuple[bool | None, bool | None, bool | None] | None:
        """The three ``PremiumFeatureInfo`` flags, preserving unknown leaves.

        The mobile schema names the slots, but this positional adapter stays
        domain-type free and returns them in wire order. A present short block
        remains distinguishable from an absent block via ``None`` leaves.
        """
        if not isinstance(self._raw, list) or len(self._raw) <= self._PREMIUM_FEATURE_INFO_POS:
            return None
        block = self._raw[self._PREMIUM_FEATURE_INFO_POS]
        if not isinstance(block, list):
            return None

        def _flag(position: int) -> bool | None:
            if len(block) <= position:
                return None
            value = block[position]
            return value if isinstance(value, bool) else None

        return (_flag(0), _flag(1), _flag(2))

    @property
    def chat_session_ids(self) -> list[str]:
        """IDs from ``Project.chatSessions`` (populated on CREATE only).

        Each session is the single-field row ``[chatSessionId]``. Invalid rows
        are skipped independently so one malformed optional session does not
        make the created notebook unusable.
        """
        if not isinstance(self._raw, list) or len(self._raw) <= self._CHAT_SESSIONS_POS:
            return []
        rows = self._raw[self._CHAT_SESSIONS_POS]
        if not isinstance(rows, list):
            return []
        return [
            row[0]
            for row in rows
            if isinstance(row, list) and row and isinstance(row[0], str) and row[0]
        ]


def decode_notebook(
    cls: type[Notebook],
    data: list[Any],
    *,
    include_chat_settings: bool = False,
) -> Notebook:
    """Decode a web ``Project`` row into the requested public notebook class."""
    from ..._types.chat import ChatSettings
    from ..._types.notebooks import ChatSession, PremiumFeatureInfo
    from .chat import unwrap_chat_settings

    project = ProjectRow(data)
    title_slot = (
        safe_index(data, 0, method_id=_NOTEBOOK_METHOD_ID, source="Notebook.title")
        if len(data) > 0
        else None
    )
    raw_title = title_slot if isinstance(title_slot, str) else ""
    title = raw_title.replace("thought\n", "").strip()
    sources_count = _extract_notebook_sources_count(data)

    notebook_id = ""
    if len(data) > 2:
        raw_id = safe_index(data, 2, method_id=_NOTEBOOK_METHOD_ID, source="Notebook.id")
        if isinstance(raw_id, str):
            notebook_id = raw_id
        elif raw_id is not None:
            logger.warning(
                "Notebook row id slot malformed — fabricating empty id "
                "(expected str at data[2], got %s; row=%s)",
                type(raw_id).__name__,
                reprlib.repr(data),
            )

    meta_slot = (
        safe_index(data, 5, method_id=_NOTEBOOK_METHOD_ID, source="Notebook.metadata")
        if len(data) > 5
        else None
    )
    meta = meta_slot if isinstance(meta_slot, list) else None

    created_at = None
    if meta is not None and len(meta) > 8:
        created_ts = safe_index(
            meta, 8, method_id=_NOTEBOOK_METHOD_ID, source="Notebook.created_at"
        )
        if isinstance(created_ts, list) and created_ts:
            created_at = _datetime_from_timestamp(
                safe_index(
                    created_ts, 0, method_id=_NOTEBOOK_METHOD_ID, source="Notebook.created_at"
                )
            )

    last_viewed_at = None
    if meta is not None and len(meta) > 5:
        viewed_ts = safe_index(
            meta, 5, method_id=_NOTEBOOK_METHOD_ID, source="Notebook.last_viewed_at"
        )
        if isinstance(viewed_ts, list) and viewed_ts:
            last_viewed_at = _datetime_from_timestamp(
                safe_index(
                    viewed_ts,
                    0,
                    method_id=_NOTEBOOK_METHOD_ID,
                    source="Notebook.last_viewed_at",
                )
            )

    role = None
    if meta:
        raw_role = safe_index(meta, 0, method_id=_NOTEBOOK_METHOD_ID, source="Notebook.role")
        role = _role_from_wire(raw_role, data)

    premium_features = None
    premium_flags = project.premium_feature_flags
    if premium_flags is not None:
        premium_features = PremiumFeatureInfo(*premium_flags)

    chat_settings = None
    if include_chat_settings:
        try:
            settings_row = unwrap_chat_settings(data, source="Notebook.chat_settings")
            chat_settings = ChatSettings(
                goal=ChatGoal(settings_row.goal_code),
                response_length=ChatResponseLength(settings_row.response_length_code),
                custom_prompt=settings_row.custom_prompt,
            )
        except (UnknownRPCMethodError, ValueError):
            logger.warning(
                "Notebook row chat-settings slot could not be decoded — reporting unknown "
                "settings (row=%s)",
                reprlib.repr(data),
            )

    return cls(
        id=notebook_id,
        title=title,
        created_at=created_at,
        sources_count=sources_count,
        role=role,
        last_viewed_at=last_viewed_at,
        emoji=project.emoji,
        premium_features=premium_features,
        chat_sessions=[ChatSession(id=session_id) for session_id in project.chat_session_ids],
        chat_settings=chat_settings,
    )


# A single leading markdown *bullet* marker (``-``/``*``/``+``) plus its trailing
# space. The backend sometimes frames a suggestion as a markdown list item, so a
# ``prompt`` / ``title`` leaf can arrive as ``"\n- Ask X"``; an agent piping that
# straight into ``chat_ask`` would send a bullet-dash as the question. We strip
# one leading bullet so both the CLI and the MCP surface get a clean, ready-to-send
# string. Ordered-list counters (``1.`` / ``2026.``) are deliberately NOT matched:
# the only observed framing is the bullet, and a numeric prefix is frequently
# legitimate content (a year, a count) that must be preserved (#1912 review).
_LEADING_LIST_MARKER = re.compile(r"[-*+]\s+")


def _strip_leading_list_marker(text: str) -> str:
    """Return ``text`` with surrounding whitespace and one leading bullet marker removed.

    Tight normalization for suggestion leaves (issue #1909): only leading
    whitespace + a single leading bullet marker + trailing whitespace are removed.
    Interior newlines and content are left untouched, so a genuinely multi-line
    prompt keeps its body — only the leading list-item framing is stripped.

    ``lstrip`` runs before the match (not a full ``strip``) so a marker-only leaf
    like ``"\\n-   "`` collapses cleanly to ``""`` rather than a bare ``"-"``
    (#1912 review): the leading whitespace is removed first, the whole marker then
    matches, and the empty remainder is returned.
    """
    lstripped = text.lstrip()
    marker = _LEADING_LIST_MARKER.match(lstripped)
    if marker:
        return lstripped[marker.end() :].strip()
    return lstripped.rstrip()


# ``GeneratePromptSuggestions`` (``otmP3b``) method id, threaded into
# ``safe_index`` / drift diagnostics for the suggestion-list unwrap.
_SUGGEST_PROMPTS_METHOD_ID = RPCMethod.SUGGEST_PROMPTS.value

# Envelope-unwrap position: ``GeneratePromptSuggestions`` wraps the suggestion
# list as the first element of a single-element envelope
# (``[[[title, prompt], ...]]``).
_SUGGEST_PROMPTS_CONTAINER_POS = 0

# ``NextStepSuggestions`` (``OcvKNc``) method id for the follow-up unwrap.
_SUGGEST_NEXT_STEPS_METHOD_ID = RPCMethod.SUGGEST_NEXT_STEPS.value

# Envelope-unwrap position: the reply IS one ``NextStepSuggestions`` message
# (``{ repeated NextStep next_steps = 1 }``), so its rows are the first element
# (``[[[question, type_code], ...]]``) — the same layout the streamed chat
# answer carries at ``inner[5]`` (``StreamEnvelopeRow.next_step_rows``).
_SUGGEST_NEXT_STEPS_ROWS_POS = 0


def unwrap_next_step_suggestions(result: Any, *, source: str) -> list[Any]:
    """Return the ``NextStep`` row list from a ``NextStepSuggestions`` reply.

    A falsy / non-list payload yields ``[]``; a present-but-non-list inner
    container also yields ``[]``. Same permissive contract as
    :func:`unwrap_prompt_suggestions` — follow-up chips are best-effort UI sugar.
    """
    if not isinstance(result, list) or not result:
        return []
    inner = safe_index(
        result,
        _SUGGEST_NEXT_STEPS_ROWS_POS,
        method_id=_SUGGEST_NEXT_STEPS_METHOD_ID,
        source=source,
    )
    return inner if isinstance(inner, list) else []


def unwrap_prompt_suggestions(result: Any, *, source: str) -> list[Any]:
    """Return the suggestion-row list from a ``GeneratePromptSuggestions`` reply.

    The ``otmP3b`` reply wraps the rows as a single-element envelope
    (``[[ [title, prompt], [title, prompt], ... ]]``): the rows live at
    ``result[0]``. A falsy / non-list payload (no suggestions) yields ``[]``;
    a present-but-non-list inner container also yields ``[]``. This mirrors the
    permissive contract of the report-suggestion unwrap — a suggestion list is
    best-effort UI sugar, not a load-bearing decode, so an absent / degenerate
    payload degrades to an empty list rather than raising.
    """
    if not isinstance(result, list) or not result:
        return []
    inner = safe_index(
        result,
        _SUGGEST_PROMPTS_CONTAINER_POS,
        method_id=_SUGGEST_PROMPTS_METHOD_ID,
        source=source,
    )
    return inner if isinstance(inner, list) else []


@dataclass(frozen=True)
class PromptSuggestionRow:
    """Typed view of one raw ``GeneratePromptSuggestions`` suggestion row.

    The wrapped row is a single AI-suggested prompt entry from the ``otmP3b``
    (``SUGGEST_PROMPTS``) RPC. Position layout:

    =====  ============================================================
    Index  Meaning
    =====  ============================================================
    0      title (str)
    1      prompt (str) — a ready-to-send multi-line instruction
    =====  ============================================================

    Short / malformed rows degrade to empty strings rather than raising — a
    suggestion list is best-effort UI sugar (the same permissive contract as
    :class:`~notebooklm._web.rows.artifacts.ReportSuggestionRow`). Positions
    are pinned by ``tests/unit/test_notebooks_row_adapter.py``.
    """

    _raw: Any = field(repr=False)

    _TITLE_POS: ClassVar[int] = 0
    _PROMPT_POS: ClassVar[int] = 1
    # A row must carry at least the prompt slot (index 1) to be usable.
    _MIN_LEN: ClassVar[int] = 2

    @property
    def is_well_formed(self) -> bool:
        """Whether the row is a list long enough to carry title + prompt."""
        return isinstance(self._raw, list) and len(self._raw) >= self._MIN_LEN

    def _str_at(self, position: int) -> str:
        """Return ``self._raw[position]`` when it is a str, else ``""``.

        Bounds-guarded so a short / malformed row degrades to ``""`` (the
        documented contract) instead of raising when a property is read without
        first checking :attr:`is_well_formed`.
        """
        if not isinstance(self._raw, list) or len(self._raw) <= position:
            return ""
        value = self._raw[position]
        return value if isinstance(value, str) else ""

    @property
    def title(self) -> str:
        """Suggestion title — empty string when absent / non-string.

        A leading markdown list marker (e.g. ``"\\n- "``) is stripped so the
        title is clean ready-to-display text (issue #1909).
        """
        return _strip_leading_list_marker(self._str_at(self._TITLE_POS))

    @property
    def prompt(self) -> str:
        """Suggestion prompt — empty string when absent / non-string.

        A leading markdown list marker (e.g. ``"\\n- "``) is stripped so the
        prompt is a clean ready-to-send string — an agent can pipe it straight
        into ``chat_ask`` without leaking a bullet-dash as the question (#1909).
        """
        return _strip_leading_list_marker(self._str_at(self._PROMPT_POS))
