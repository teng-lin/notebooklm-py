"""Notebook row adapters for ``batchexecute`` notebook-scoped RPCs.

Currently centralises the positional knowledge of the
``GeneratePromptSuggestions`` (``otmP3b`` / ``SUGGEST_PROMPTS``) reply: the
envelope unwrap (:func:`unwrap_prompt_suggestions`) and the per-row title /
prompt reads (:class:`PromptSuggestionRow`). These back
:meth:`NotebooksAPI.suggest_prompts`.

Position contract (pinned by ``tests/unit/test_notebooks_row_adapter.py``):

* :class:`PromptSuggestionRow` — one suggestion row:

  =====  ============================================================
  Index  Meaning
  =====  ============================================================
  0      title (str)
  1      prompt (str) — a ready-to-send multi-line instruction
  =====  ============================================================
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar

from ..rpc import RPCMethod, safe_index

__all__ = [
    "PromptSuggestionRow",
    "unwrap_prompt_suggestions",
]

# ``GeneratePromptSuggestions`` (``otmP3b``) method id, threaded into
# ``safe_index`` / drift diagnostics for the suggestion-list unwrap.
_SUGGEST_PROMPTS_METHOD_ID = RPCMethod.SUGGEST_PROMPTS.value

# Envelope-unwrap position: ``GeneratePromptSuggestions`` wraps the suggestion
# list as the first element of a single-element envelope
# (``[[[title, prompt], ...]]``).
_SUGGEST_PROMPTS_CONTAINER_POS = 0


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
    :class:`~notebooklm._row_adapters.artifacts.ReportSuggestionRow`). Positions
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
        """Suggestion title — empty string when absent / non-string."""
        return self._str_at(self._TITLE_POS)

    @property
    def prompt(self) -> str:
        """Suggestion prompt — empty string when absent / non-string."""
        return self._str_at(self._PROMPT_POS)
