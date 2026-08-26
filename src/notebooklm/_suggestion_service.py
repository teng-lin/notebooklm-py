"""Transport-neutral semantic service for prompt and report suggestions."""

from __future__ import annotations

from ._backend import BackendAdapter
from ._deadline import RuntimeDeadline, RuntimeDeadlineFactory
from ._projectors import project_prompt_suggestions, project_report_suggestions
from ._read_services import NotebookReadService
from ._records import (
    ARTIFACT_SUGGEST_REPORTS_DEF,
    NOTEBOOK_SUGGEST_PROMPTS_DEF,
    ArtifactSuggestReportsInput,
    NotebookSuggestPromptsInput,
)
from .exceptions import ValidationError
from .types import PromptSuggestion, ReportSuggestion

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
    """Invoke typed suggestion operations and project existing public models."""

    __slots__ = ("_backend", "_deadline_factory", "_notebooks")

    def __init__(
        self,
        backend: BackendAdapter,
        *,
        deadline_factory: RuntimeDeadlineFactory | None = None,
    ) -> None:
        self._backend = backend
        self._deadline_factory = deadline_factory
        # The default source scope is resolved here, above the port: the
        # suggestion operation itself takes an already-resolved input record.
        self._notebooks = NotebookReadService(backend)

    async def suggest_prompts(
        self,
        notebook_id: str,
        *,
        source_ids: list[str] | None = None,
        mode: int = PROMPT_SUGGESTIONS_DEFAULT_MODE,
        query: str | None = None,
        deadline: RuntimeDeadline | None = None,
    ) -> list[PromptSuggestion]:
        """Suggest prompts for a source scope, defaulting to the whole notebook.

        ``source_ids=None`` is this service's documented default for "every
        source in the notebook": it costs one extra ``NOTEBOOK_GET`` read, which
        shares the suggestion call's budget so the pair spends one client
        timeout. An explicit list — the empty one included — is used verbatim.
        """
        validate_prompt_suggestion_mode(mode)
        if deadline is None and self._deadline_factory is not None:
            # Captured once, before the read: both natives spend one budget.
            deadline = self._deadline_factory.start()
        resolved = (
            tuple(await self._notebooks.get_source_ids(notebook_id, deadline=deadline))
            if source_ids is None
            else tuple(source_ids)
        )
        result = await self._backend.invoke(
            NOTEBOOK_SUGGEST_PROMPTS_DEF,
            NotebookSuggestPromptsInput(
                notebook_id=notebook_id,
                source_ids=resolved,
                mode=mode,
                query=query,
            ),
            deadline=deadline,
        )
        return project_prompt_suggestions(result.suggestions)

    async def suggest_reports(
        self,
        notebook_id: str,
        *,
        deadline: RuntimeDeadline | None = None,
    ) -> list[ReportSuggestion]:
        result = await self._backend.invoke(
            ARTIFACT_SUGGEST_REPORTS_DEF,
            ArtifactSuggestReportsInput(notebook_id),
            deadline=deadline,
        )
        return project_report_suggestions(result.suggestions)


__all__ = [
    "PROMPT_SUGGESTIONS_DEFAULT_MODE",
    "PROMPT_SUGGESTIONS_MODE_MAX",
    "PROMPT_SUGGESTIONS_MODE_MIN",
    "SuggestionService",
    "validate_prompt_suggestion_mode",
]
