"""Transport-neutral records and operation definitions for notebooks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum, unique

from ._operations import CallPolicy, Operation, OperationDef, OperationTier


@unique
class SourceIdDiagnostics(str, Enum):
    """How one caller reports a notebook snapshot it cannot read source ids from.

    The notebook read decodes its embedded source ids for callers that differ
    only in what they say about a malformed payload, so the mode travels on
    :class:`NotebookGetInput` and the decoder applies it.
    """

    #: The Audio family: every shape mismatch yields no ids and says nothing.
    SILENT = "silent"
    #: The generation families: each shape mismatch logs the schema-drift warning.
    WARN = "warn"
    #: Prompt suggestions and the notebook read itself: warn, and additionally
    #: swallow an ``IndexError``/``TypeError`` raised despite the guards.
    GUARDED = "guarded"


@dataclass(frozen=True, slots=True)
class NotebookPremiumFeaturesRecord:
    """Tier-dependent notebook feature verdicts, independent of public models."""

    can_edit_advanced_settings: bool | None = None
    can_edit_guidebook_config: bool | None = None
    can_view_analytics: bool | None = None


@dataclass(frozen=True, slots=True)
class NotebookChatSessionRecord:
    """One chat-session identity volunteered by a notebook read."""

    id: str


@dataclass(frozen=True, slots=True)
class NotebookChatSettingsRecord:
    """Semantic notebook chat configuration without RPC enum types."""

    goal: str
    response_length: str
    custom_prompt: str | None = None


@dataclass(frozen=True, slots=True)
class NotebookRecord:
    """Neutral notebook value returned by list/get backends."""

    id: str
    title: str
    created_at: datetime | None = None
    sources_count: int = 0
    is_owner: bool = True
    role: str | None = None
    last_viewed_at: datetime | None = None
    emoji: str | None = None
    premium_features: NotebookPremiumFeaturesRecord | None = None
    chat_sessions: tuple[NotebookChatSessionRecord, ...] = ()
    chat_settings: NotebookChatSettingsRecord | None = None


@dataclass(frozen=True, slots=True)
class SuggestedTopicRecord:
    """One transport-neutral notebook guide topic."""

    question: str
    prompt: str


@dataclass(frozen=True, slots=True)
class NotebookDescriptionRecord:
    """Decoded notebook guide without exported model dependencies."""

    summary: str
    suggested_topics: tuple[SuggestedTopicRecord, ...] = ()


@dataclass(frozen=True, slots=True)
class NotebookListInput:
    """Input for the parameter-free notebook listing operation."""


@dataclass(frozen=True, slots=True)
class NotebookListResult:
    """Notebook listing in backend order."""

    notebooks: tuple[NotebookRecord, ...]


@dataclass(frozen=True, slots=True)
class NotebookGetInput:
    """Identity requested by the notebook get operation.

    ``source_diagnostics`` selects how the decoder reports a snapshot whose
    source slot it cannot read; the callers that resolve a default source set
    differ only in that report.
    """

    notebook_id: str
    include_notebook: bool = True
    require_notebook: bool = False
    source_diagnostics: SourceIdDiagnostics = SourceIdDiagnostics.GUARDED


@dataclass(frozen=True, slots=True)
class NotebookGetResult:
    """Notebook get result; ``None`` is the semantic not-found state."""

    notebook: NotebookRecord | None
    source_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class NotebookGuideInput:
    """Notebook identity whose generated guide is requested."""

    notebook_id: str


@dataclass(frozen=True, slots=True)
class NotebookGuideResult:
    """Generated notebook guide before public projection."""

    description: NotebookDescriptionRecord


@dataclass(frozen=True, slots=True)
class NotebookRemoveRecentInput:
    """Notebook identity to remove from the account's recent list."""

    notebook_id: str


@dataclass(frozen=True, slots=True)
class NotebookRemoveRecentResult:
    """Successful idempotent removal from the recent list."""


@dataclass(frozen=True, slots=True)
class NotebookCreateInput:
    """Requested notebook title."""

    title: str


@dataclass(frozen=True, slots=True)
class NotebookCreateResult:
    """Created or uniquely reconciled notebook."""

    notebook: NotebookRecord


@dataclass(frozen=True, slots=True)
class NotebookAllocateInput:
    """One native notebook allocation attempt."""

    title: str


@dataclass(frozen=True, slots=True)
class NotebookAllocateResult:
    """Notebook returned by one successful allocation attempt."""

    notebook: NotebookRecord


@dataclass(frozen=True, slots=True)
class NotebookUpdateInput:
    """Notebook identity and optional title/emoji replacements."""

    notebook_id: str
    title: str | None = None
    emoji: str | None = None


@dataclass(frozen=True, slots=True)
class NotebookUpdateResult:
    """Notebook read back after its property mutation."""

    notebook: NotebookRecord


@dataclass(frozen=True, slots=True)
class NotebookPatchInput:
    """One notebook property-mask mutation without a readback."""

    notebook_id: str
    title: str | None = None
    emoji: str | None = None


@dataclass(frozen=True, slots=True)
class NotebookPatchResult:
    """Successful notebook property mutation."""


@dataclass(frozen=True, slots=True)
class NotebookDeleteInput:
    """Single notebook identity to delete idempotently."""

    notebook_id: str


@dataclass(frozen=True, slots=True)
class NotebookDeleteResult:
    """Successful idempotent notebook deletion."""


@dataclass(frozen=True, slots=True)
class PromptSuggestionRecord:
    """One best-effort notebook prompt suggestion."""

    title: str
    prompt: str


@dataclass(frozen=True, slots=True)
class NotebookSuggestPromptsInput:
    """Notebook prompt-suggestion request whose source scope is already resolved.

    ``source_ids`` is required: "no scope given means every source" is a
    service-level default (:class:`~notebooklm._suggestion_service.SuggestionService`
    resolves the notebook's full source set through ``NOTEBOOK_GET`` before
    invoking), not something the backend re-derives below the port.
    """

    notebook_id: str
    source_ids: tuple[str, ...]
    mode: int = 4
    query: str | None = None


@dataclass(frozen=True, slots=True)
class NotebookSuggestPromptsResult:
    """Prompt suggestions in backend order."""

    suggestions: tuple[PromptSuggestionRecord, ...]


NOTEBOOK_LIST_DEF: OperationDef[NotebookListInput, NotebookListResult] = OperationDef(
    Operation.NOTEBOOK_LIST,
    CallPolicy.READ,
    NotebookListInput,
    NotebookListResult,
)
NOTEBOOK_GET_DEF: OperationDef[NotebookGetInput, NotebookGetResult] = OperationDef(
    Operation.NOTEBOOK_GET,
    # GET_NOTEBOOK updates lastViewedTime even though its result is read-shaped.
    CallPolicy.MUTATION,
    NotebookGetInput,
    NotebookGetResult,
)
NOTEBOOK_CREATE_DEF: OperationDef[NotebookCreateInput, NotebookCreateResult] = OperationDef(
    Operation.NOTEBOOK_CREATE,
    CallPolicy.MUTATION,
    NotebookCreateInput,
    NotebookCreateResult,
)
NOTEBOOK_ALLOCATE_DEF: OperationDef[NotebookAllocateInput, NotebookAllocateResult] = OperationDef(
    Operation.NOTEBOOK_ALLOCATE,
    CallPolicy.MUTATION,
    NotebookAllocateInput,
    NotebookAllocateResult,
    tier=OperationTier.PRIMITIVE,
)
NOTEBOOK_UPDATE_DEF: OperationDef[NotebookUpdateInput, NotebookUpdateResult] = OperationDef(
    Operation.NOTEBOOK_UPDATE,
    CallPolicy.MUTATION,
    NotebookUpdateInput,
    NotebookUpdateResult,
)
NOTEBOOK_PATCH_DEF: OperationDef[NotebookPatchInput, NotebookPatchResult] = OperationDef(
    Operation.NOTEBOOK_PATCH,
    CallPolicy.MUTATION,
    NotebookPatchInput,
    NotebookPatchResult,
    tier=OperationTier.PRIMITIVE,
)
NOTEBOOK_DELETE_DEF: OperationDef[NotebookDeleteInput, NotebookDeleteResult] = OperationDef(
    Operation.NOTEBOOK_DELETE,
    CallPolicy.MUTATION,
    NotebookDeleteInput,
    NotebookDeleteResult,
)
NOTEBOOK_REMOVE_RECENT_DEF: OperationDef[NotebookRemoveRecentInput, NotebookRemoveRecentResult] = (
    OperationDef(
        Operation.NOTEBOOK_REMOVE_RECENT,
        CallPolicy.MUTATION,
        NotebookRemoveRecentInput,
        NotebookRemoveRecentResult,
    )
)
NOTEBOOK_SUMMARIZE_DEF: OperationDef[NotebookGuideInput, NotebookGuideResult] = OperationDef(
    Operation.NOTEBOOK_SUMMARIZE,
    CallPolicy.STATEFUL_START,
    NotebookGuideInput,
    NotebookGuideResult,
)
NOTEBOOK_DESCRIBE_DEF: OperationDef[NotebookGuideInput, NotebookGuideResult] = OperationDef(
    Operation.NOTEBOOK_DESCRIBE,
    CallPolicy.STATEFUL_START,
    NotebookGuideInput,
    NotebookGuideResult,
)
NOTEBOOK_SUGGEST_PROMPTS_DEF: OperationDef[
    NotebookSuggestPromptsInput, NotebookSuggestPromptsResult
] = OperationDef(
    Operation.NOTEBOOK_SUGGEST_PROMPTS,
    CallPolicy.STATEFUL_START,
    NotebookSuggestPromptsInput,
    NotebookSuggestPromptsResult,
)


__all__ = [
    "NOTEBOOK_ALLOCATE_DEF",
    "NOTEBOOK_CREATE_DEF",
    "NOTEBOOK_DELETE_DEF",
    "NOTEBOOK_DESCRIBE_DEF",
    "NOTEBOOK_GET_DEF",
    "NOTEBOOK_LIST_DEF",
    "NOTEBOOK_PATCH_DEF",
    "NOTEBOOK_REMOVE_RECENT_DEF",
    "NOTEBOOK_SUGGEST_PROMPTS_DEF",
    "NOTEBOOK_SUMMARIZE_DEF",
    "NOTEBOOK_UPDATE_DEF",
    "NotebookChatSessionRecord",
    "NotebookChatSettingsRecord",
    "NotebookAllocateInput",
    "NotebookAllocateResult",
    "NotebookCreateInput",
    "NotebookCreateResult",
    "NotebookDeleteInput",
    "NotebookDeleteResult",
    "NotebookDescriptionRecord",
    "NotebookGetInput",
    "NotebookGetResult",
    "NotebookGuideInput",
    "NotebookGuideResult",
    "NotebookListInput",
    "NotebookListResult",
    "NotebookPatchInput",
    "NotebookPatchResult",
    "NotebookPremiumFeaturesRecord",
    "NotebookRecord",
    "NotebookRemoveRecentInput",
    "NotebookRemoveRecentResult",
    "NotebookSuggestPromptsInput",
    "NotebookSuggestPromptsResult",
    "NotebookUpdateInput",
    "NotebookUpdateResult",
    "PromptSuggestionRecord",
    "SourceIdDiagnostics",
    "SuggestedTopicRecord",
]
