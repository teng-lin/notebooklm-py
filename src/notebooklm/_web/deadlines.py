"""Closed deadline-authority ledger for multi-native semantic operations."""

from __future__ import annotations

from enum import Enum, unique
from types import MappingProxyType
from typing import Final

from .._operations import Operation


@unique
class SemanticDeadlineAuthority(str, Enum):
    """Reviewed source of the aggregate budget for a semantic operation."""

    CLIENT_TIMEOUT = "client_timeout"
    WORKFLOW_OWNED = "workflow_owned"
    BRANCH_EXCLUSIVE = "branch_exclusive"


# This is deliberately a closed ledger, not a broad "seed every invoke" rule.
# CLIENT_TIMEOUT entries can issue more than one native RPC during one typed
# backend operation, so production captures the configured client timeout once
# and hands the same RuntimeDeadline to every phase. WORKFLOW_OWNED entries
# retain their documented upload/poll/chat budget. BRANCH_EXCLUSIVE handlers
# contain more than one syntactic RPC site, but each input selects exactly one.
SEMANTIC_DEADLINE_AUTHORITIES: Final[MappingProxyType[Operation, SemanticDeadlineAuthority]] = (
    MappingProxyType(
        {
            Operation.NOTEBOOK_SUGGEST_PROMPTS: SemanticDeadlineAuthority.CLIENT_TIMEOUT,
            Operation.ARTIFACT_LIST: SemanticDeadlineAuthority.CLIENT_TIMEOUT,
            Operation.ARTIFACT_GET: SemanticDeadlineAuthority.CLIENT_TIMEOUT,
            Operation.ARTIFACT_GENERATE_AUDIO: SemanticDeadlineAuthority.CLIENT_TIMEOUT,
            Operation.ARTIFACT_GENERATE_VIDEO: SemanticDeadlineAuthority.CLIENT_TIMEOUT,
            Operation.ARTIFACT_GENERATE_REPORT: SemanticDeadlineAuthority.CLIENT_TIMEOUT,
            Operation.ARTIFACT_GENERATE_QUIZ: SemanticDeadlineAuthority.CLIENT_TIMEOUT,
            Operation.ARTIFACT_GENERATE_FLASHCARDS: SemanticDeadlineAuthority.CLIENT_TIMEOUT,
            Operation.ARTIFACT_GENERATE_INFOGRAPHIC: SemanticDeadlineAuthority.CLIENT_TIMEOUT,
            Operation.ARTIFACT_GENERATE_SLIDE_DECK: SemanticDeadlineAuthority.CLIENT_TIMEOUT,
            Operation.ARTIFACT_GENERATE_DATA_TABLE: SemanticDeadlineAuthority.CLIENT_TIMEOUT,
            Operation.ARTIFACT_GENERATE_MIND_MAP: SemanticDeadlineAuthority.CLIENT_TIMEOUT,
            Operation.MIND_MAP_GENERATE_NOTE: SemanticDeadlineAuthority.CLIENT_TIMEOUT,
            Operation.MIND_MAP_GENERATE_INTERACTIVE: SemanticDeadlineAuthority.CLIENT_TIMEOUT,
            # Source file registration/upload, source polling, artifact shared-leader
            # polling, chat streaming, and research reconciliation all have explicit
            # existing budgets whose observable semantics P4.2 preserves.
            Operation.SOURCE_ADD_FILE: SemanticDeadlineAuthority.WORKFLOW_OWNED,
            Operation.SOURCE_WAIT: SemanticDeadlineAuthority.WORKFLOW_OWNED,
            Operation.ARTIFACT_WAIT: SemanticDeadlineAuthority.WORKFLOW_OWNED,
            Operation.CHAT_STREAM_ANSWER: SemanticDeadlineAuthority.WORKFLOW_OWNED,
            Operation.RESEARCH_IMPORT: SemanticDeadlineAuthority.WORKFLOW_OWNED,
            # These handlers have mutually exclusive action branches. No input can
            # execute both native sites, so aggregating them would invent a budget.
            Operation.ARTIFACT_DOWNLOAD: SemanticDeadlineAuthority.BRANCH_EXCLUSIVE,
            Operation.CHAT_CONFIGURE: SemanticDeadlineAuthority.BRANCH_EXCLUSIVE,
            # P9.2 primitive: one UPDATE_LABEL call per input, variant chosen from it.
            Operation.LABEL_MUTATE: SemanticDeadlineAuthority.BRANCH_EXCLUSIVE,
            # P10 primitive: one ADD_SOURCE call per input, variant chosen from it.
            Operation.SOURCE_REGISTER: SemanticDeadlineAuthority.BRANCH_EXCLUSIVE,
        }
    )
)


CLIENT_TIMEOUT_DEADLINE_OPERATIONS: Final[frozenset[Operation]] = frozenset(
    operation
    for operation, authority in SEMANTIC_DEADLINE_AUTHORITIES.items()
    if authority is SemanticDeadlineAuthority.CLIENT_TIMEOUT
)


__all__ = [
    "CLIENT_TIMEOUT_DEADLINE_OPERATIONS",
    "SEMANTIC_DEADLINE_AUTHORITIES",
    "SemanticDeadlineAuthority",
]
