"""Transport-neutral semantic service for prompt and report suggestions."""

from __future__ import annotations

from ._backend import BackendAdapter
from ._deadline import RuntimeDeadline
from ._records import (
    ARTIFACT_SUGGEST_REPORTS_DEF,
    NOTEBOOK_SUGGEST_PROMPTS_DEF,
    ArtifactSuggestReportsInput,
    NotebookSuggestPromptsInput,
    PromptSuggestionRecord,
    ReportSuggestionRecord,
)
from .exceptions import ValidationError

# Required backend mode/surface discriminator. Live captures identify modes
# 1/2/5/6 as Audio Deep Dive/Brief/Critique/Debate, 3/10 as Video
# Explainer/Short, 4 as Chat, 8 as Quiz, and 9 as Flashcards; 7 remains
# unidentified. Keep it as an int because Google's enum member names are not
# exposed. Zero and values above ten are rejected by the server.
PROMPT_SUGGESTIONS_DEFAULT_MODE = 4
PROMPT_SUGGESTIONS_MODE_MIN = 1
PROMPT_SUGGESTIONS_MODE_MAX = 10


def validate_prompt_suggestion_mode(mode: int) -> None:
    """Reject unsupported modes before a source-resolution RPC can occur."""
    if not PROMPT_SUGGESTIONS_MODE_MIN <= mode <= PROMPT_SUGGESTIONS_MODE_MAX:
        error = ValueError(
            f"mode must be in the inclusive range "
            f"{PROMPT_SUGGESTIONS_MODE_MIN}..{PROMPT_SUGGESTIONS_MODE_MAX}, got {mode!r}"
        )
        raise ValidationError(str(error)) from error


class SuggestionService:
    """Invoke typed suggestion operations and return their neutral records."""

    __slots__ = ("_backend",)

    def __init__(self, backend: BackendAdapter) -> None:
        self._backend = backend

    async def suggest_prompts(
        self,
        notebook_id: str,
        *,
        source_ids: list[str] | None = None,
        mode: int = PROMPT_SUGGESTIONS_DEFAULT_MODE,
        query: str | None = None,
        deadline: RuntimeDeadline | None = None,
    ) -> list[PromptSuggestionRecord]:
        validate_prompt_suggestion_mode(mode)
        result = await self._backend.invoke(
            NOTEBOOK_SUGGEST_PROMPTS_DEF,
            NotebookSuggestPromptsInput(
                notebook_id=notebook_id,
                source_ids=None if source_ids is None else tuple(source_ids),
                mode=mode,
                query=query,
            ),
            deadline=deadline,
        )
        return list(result.suggestions)

    async def suggest_reports(
        self,
        notebook_id: str,
        *,
        deadline: RuntimeDeadline | None = None,
    ) -> list[ReportSuggestionRecord]:
        result = await self._backend.invoke(
            ARTIFACT_SUGGEST_REPORTS_DEF,
            ArtifactSuggestReportsInput(notebook_id),
            deadline=deadline,
        )
        return list(result.suggestions)


__all__ = [
    "PROMPT_SUGGESTIONS_DEFAULT_MODE",
    "PROMPT_SUGGESTIONS_MODE_MAX",
    "PROMPT_SUGGESTIONS_MODE_MIN",
    "SuggestionService",
    "validate_prompt_suggestion_mode",
]
