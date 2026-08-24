"""Reviewed semantic/native policy bindings for active web operations.

``CallPolicy`` describes the whole semantic workflow.  It is deliberately not
the retry authority: individual web calls continue to resolve retry behavior
from :mod:`notebooklm._idempotency`.  This ledger makes the relationship exact
and fail-closed without feeding semantic policy back into the executor.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final

from .._idempotency import (
    IDEMPOTENCY_REGISTRY,
    IdempotencyPolicy,
    IdempotencyRegistry,
)
from .._operations import CallPolicy, Operation, OperationDef
from ..rpc import RPCMethod


@dataclass(frozen=True, slots=True)
class NativePolicyBinding:
    """One exact native method/variant and its reviewed retry classification."""

    method: RPCMethod
    variant: str | None
    expected_policy: IdempotencyPolicy
    role: str


@dataclass(frozen=True, slots=True)
class WebCallPolicyBinding:
    """Whole-workflow policy plus every native call reachable in its web handler."""

    policy: CallPolicy
    native_bindings: tuple[NativePolicyBinding, ...]
    known_divergence: str | None = None


def _native(
    method: RPCMethod,
    expected_policy: IdempotencyPolicy,
    role: str,
    *,
    variant: str | None = None,
) -> NativePolicyBinding:
    return NativePolicyBinding(method, variant, expected_policy, role)


_IDEMPOTENT = IdempotencyPolicy.IDEMPOTENT_SET_OP
_PROBE_CREATE = IdempotencyPolicy.PROBE_THEN_CREATE
_NO_RETRY = IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY
_AT_LEAST_ONCE = IdempotencyPolicy.AT_LEAST_ONCE_ACCEPTED
_APP_DOWNLOAD_DIVERGENCE = (
    "_app/download.py owns selection/conflict/filesystem choreography while the facade owns "
    "network reads. P4.2 starts a separate budget at each facade list/download operation; "
    "P5 keeps one backend execution path."
)


WEB_CALL_POLICY_BINDINGS: Final[Mapping[Operation, WebCallPolicyBinding]] = MappingProxyType(
    {
        Operation.NOTEBOOK_LIST: WebCallPolicyBinding(
            CallPolicy.READ,
            (_native(RPCMethod.LIST_NOTEBOOKS, _IDEMPOTENT, "ordered collection read"),),
        ),
        Operation.NOTEBOOK_GET: WebCallPolicyBinding(
            CallPolicy.MUTATION,
            (_native(RPCMethod.GET_NOTEBOOK, _IDEMPOTENT, "read with recency side effect"),),
        ),
        Operation.NOTEBOOK_CREATE: WebCallPolicyBinding(
            CallPolicy.MUTATION,
            (
                _native(RPCMethod.LIST_NOTEBOOKS, _IDEMPOTENT, "baseline/probe/quota read"),
                _native(RPCMethod.CREATE_NOTEBOOK, _PROBE_CREATE, "guarded create"),
                _native(RPCMethod.GET_USER_SETTINGS, _IDEMPOTENT, "quota diagnosis"),
            ),
        ),
        Operation.NOTEBOOK_UPDATE: WebCallPolicyBinding(
            CallPolicy.MUTATION,
            (
                _native(RPCMethod.RENAME_NOTEBOOK, _IDEMPOTENT, "property mutation"),
                _native(RPCMethod.GET_NOTEBOOK, _IDEMPOTENT, "post-mutation readback"),
            ),
        ),
        Operation.NOTEBOOK_DELETE: WebCallPolicyBinding(
            CallPolicy.MUTATION,
            (_native(RPCMethod.DELETE_NOTEBOOK, _IDEMPOTENT, "idempotent delete"),),
        ),
        Operation.NOTEBOOK_REMOVE_RECENT: WebCallPolicyBinding(
            CallPolicy.MUTATION,
            (
                _native(
                    RPCMethod.REMOVE_RECENTLY_VIEWED,
                    _IDEMPOTENT,
                    "idempotent recent-list removal",
                ),
            ),
        ),
        Operation.NOTEBOOK_SUMMARIZE: WebCallPolicyBinding(
            CallPolicy.STATEFUL_START,
            (_native(RPCMethod.SUMMARIZE, _IDEMPOTENT, "response-only guide generation"),),
        ),
        Operation.NOTEBOOK_DESCRIBE: WebCallPolicyBinding(
            CallPolicy.STATEFUL_START,
            (_native(RPCMethod.SUMMARIZE, _IDEMPOTENT, "response-only guide generation"),),
        ),
        Operation.SOURCE_LIST: WebCallPolicyBinding(
            CallPolicy.MUTATION,
            (_native(RPCMethod.GET_NOTEBOOK, _IDEMPOTENT, "read with recency side effect"),),
        ),
        Operation.SOURCE_GET: WebCallPolicyBinding(
            CallPolicy.MUTATION,
            (_native(RPCMethod.GET_NOTEBOOK, _IDEMPOTENT, "read with recency side effect"),),
        ),
        Operation.SOURCE_ADD_URL: WebCallPolicyBinding(
            CallPolicy.MUTATION,
            (
                _native(
                    RPCMethod.GET_NOTEBOOK,
                    _IDEMPOTENT,
                    "baseline/probe or title null-echo readback",
                ),
                _native(
                    RPCMethod.ADD_SOURCE,
                    _PROBE_CREATE,
                    "guarded generic/YouTube create",
                    variant="url",
                ),
                _native(RPCMethod.UPDATE_SOURCE, _IDEMPOTENT, "optional title readback"),
            ),
        ),
        Operation.SOURCE_ADD_URL_BATCH: WebCallPolicyBinding(
            CallPolicy.MUTATION,
            (
                _native(
                    RPCMethod.ADD_SOURCE,
                    _PROBE_CREATE,
                    "single non-replayed URL/YouTube batch write",
                    variant="url",
                ),
                _native(RPCMethod.GET_NOTEBOOK, _IDEMPOTENT, "conditional reconciliation read"),
            ),
        ),
        Operation.SOURCE_ADD_TEXT: WebCallPolicyBinding(
            CallPolicy.MUTATION,
            (
                _native(
                    RPCMethod.ADD_SOURCE,
                    _NO_RETRY,
                    "non-idempotent pasted-text allocation",
                    variant="text",
                ),
            ),
        ),
        Operation.SOURCE_ADD_DRIVE: WebCallPolicyBinding(
            CallPolicy.MUTATION,
            (
                _native(
                    RPCMethod.ADD_SOURCE,
                    _PROBE_CREATE,
                    "Drive-document allocation with baseline probe",
                    variant="drive",
                ),
                _native(RPCMethod.GET_NOTEBOOK, _IDEMPOTENT, "baseline/probe read"),
                _native(RPCMethod.UPDATE_SOURCE, _IDEMPOTENT, "optional title set-op"),
            ),
        ),
        Operation.SOURCE_ADD_FILE: WebCallPolicyBinding(
            CallPolicy.MUTATION,
            (
                _native(RPCMethod.ADD_SOURCE_FILE, _PROBE_CREATE, "file-source registration"),
                _native(
                    RPCMethod.GET_NOTEBOOK,
                    _IDEMPOTENT,
                    "baseline/probe or title registration tick",
                ),
                _native(RPCMethod.GET_USER_SETTINGS, _IDEMPOTENT, "source-limit diagnosis"),
                _native(RPCMethod.UPDATE_SOURCE, _IDEMPOTENT, "optional title set-op"),
            ),
        ),
        Operation.SOURCE_DELETE: WebCallPolicyBinding(
            CallPolicy.MUTATION,
            (_native(RPCMethod.DELETE_SOURCE, _IDEMPOTENT, "idempotent source delete"),),
        ),
        Operation.SOURCE_UPDATE: WebCallPolicyBinding(
            CallPolicy.MUTATION,
            (
                _native(RPCMethod.UPDATE_SOURCE, _IDEMPOTENT, "source title set-op"),
                _native(RPCMethod.GET_NOTEBOOK, _IDEMPOTENT, "conditional null-echo readback"),
            ),
        ),
        Operation.SOURCE_REFRESH: WebCallPolicyBinding(
            CallPolicy.MUTATION,
            (
                _native(
                    RPCMethod.REFRESH_SOURCE,
                    _AT_LEAST_ONCE,
                    "accepted duplicate refresh kickoff",
                ),
            ),
            known_divergence=(
                "Semantic mutation is backed by AT_LEAST_ONCE_ACCEPTED native retry; P4 records "
                "parity but must not change behavior."
            ),
        ),
        Operation.SOURCE_CHECK_FRESHNESS: WebCallPolicyBinding(
            CallPolicy.READ,
            (
                _native(
                    RPCMethod.CHECK_SOURCE_FRESHNESS,
                    _IDEMPOTENT,
                    "source freshness read",
                ),
            ),
        ),
        Operation.SOURCE_GET_GUIDE: WebCallPolicyBinding(
            CallPolicy.STATEFUL_START,
            (
                _native(
                    RPCMethod.GET_SOURCE_GUIDE,
                    _IDEMPOTENT,
                    "response-only source-guide generation",
                ),
            ),
        ),
        Operation.SOURCE_GET_FULLTEXT: WebCallPolicyBinding(
            CallPolicy.READ,
            (_native(RPCMethod.GET_SOURCE, _IDEMPOTENT, "source content read"),),
        ),
        Operation.CHAT_ASK: WebCallPolicyBinding(
            CallPolicy.STREAM,
            (
                _native(RPCMethod.GET_NOTEBOOK, _IDEMPOTENT, "default source-set read"),
                _native(
                    RPCMethod.GET_LAST_CONVERSATION_ID,
                    _IDEMPOTENT,
                    "conversation resolution before or after the streamed request",
                ),
            ),
        ),
        Operation.CHAT_GET_CONVERSATION: WebCallPolicyBinding(
            CallPolicy.READ,
            (
                _native(
                    RPCMethod.GET_LAST_CONVERSATION_ID,
                    _IDEMPOTENT,
                    "most-recent conversation read",
                ),
            ),
        ),
        Operation.CHAT_GET_HISTORY: WebCallPolicyBinding(
            CallPolicy.READ,
            (
                _native(
                    RPCMethod.GET_LAST_CONVERSATION_ID,
                    _IDEMPOTENT,
                    "optional most-recent conversation resolution",
                ),
                _native(
                    RPCMethod.GET_CONVERSATION_TURNS,
                    _IDEMPOTENT,
                    "conversation-turn collection read",
                ),
            ),
        ),
        Operation.CHAT_DELETE_HISTORY: WebCallPolicyBinding(
            CallPolicy.MUTATION,
            (
                _native(
                    RPCMethod.DELETE_CONVERSATION,
                    _IDEMPOTENT,
                    "idempotent conversation-turn delete",
                ),
            ),
        ),
        Operation.CHAT_CONFIGURE: WebCallPolicyBinding(
            CallPolicy.MUTATION,
            (
                _native(RPCMethod.GET_NOTEBOOK, _IDEMPOTENT, "chat-settings read"),
                _native(RPCMethod.RENAME_NOTEBOOK, _IDEMPOTENT, "chat-settings set-op"),
            ),
        ),
        Operation.CHAT_SAVE_NOTE: WebCallPolicyBinding(
            CallPolicy.MUTATION,
            (
                _native(
                    RPCMethod.CREATE_NOTE,
                    _NO_RETRY,
                    "non-idempotent citation-rich note allocation",
                    variant="saved_from_chat",
                ),
            ),
        ),
        Operation.SOURCE_WAIT: WebCallPolicyBinding(
            CallPolicy.MUTATION,
            (
                _native(
                    RPCMethod.GET_NOTEBOOK,
                    _IDEMPOTENT,
                    "source readiness snapshot; READY_ALL shares it across inputs per tick",
                ),
            ),
        ),
        Operation.LABEL_LIST: WebCallPolicyBinding(
            CallPolicy.READ,
            (_native(RPCMethod.LIST_LABELS, _IDEMPOTENT, "source-label collection read"),),
        ),
        Operation.LABEL_GET: WebCallPolicyBinding(
            CallPolicy.READ,
            (_native(RPCMethod.LIST_LABELS, _IDEMPOTENT, "source-label identity scan"),),
        ),
        Operation.LABEL_GENERATE: WebCallPolicyBinding(
            CallPolicy.STATEFUL_START,
            (_native(RPCMethod.CREATE_LABEL, _NO_RETRY, "automatic source-label grouping"),),
        ),
        Operation.LABEL_CREATE: WebCallPolicyBinding(
            CallPolicy.MUTATION,
            (
                _native(RPCMethod.LIST_LABELS, _IDEMPOTENT, "pre-create identity baseline"),
                _native(RPCMethod.CREATE_LABEL, _NO_RETRY, "manual source-label allocation"),
            ),
        ),
        Operation.LABEL_UPDATE: WebCallPolicyBinding(
            CallPolicy.MUTATION,
            (
                _native(RPCMethod.LIST_LABELS, _IDEMPOTENT, "preflight and readback"),
                _native(RPCMethod.UPDATE_LABEL, _IDEMPOTENT, "name/emoji set-op"),
                _native(
                    RPCMethod.UPDATE_LABEL,
                    _NO_RETRY,
                    "source membership append",
                    variant="add_sources",
                ),
                _native(
                    RPCMethod.UPDATE_LABEL,
                    _IDEMPOTENT,
                    "source membership removal",
                    variant="remove_sources",
                ),
            ),
        ),
        Operation.LABEL_DELETE: WebCallPolicyBinding(
            CallPolicy.MUTATION,
            (_native(RPCMethod.DELETE_LABEL, _NO_RETRY, "batch source-label delete"),),
        ),
        Operation.COLLECTION_LIST: WebCallPolicyBinding(
            CallPolicy.READ,
            (_native(RPCMethod.LIST_LABELS, _IDEMPOTENT, "account collection read"),),
        ),
        Operation.COLLECTION_GET: WebCallPolicyBinding(
            CallPolicy.READ,
            (_native(RPCMethod.LIST_LABELS, _IDEMPOTENT, "collection identity scan"),),
        ),
        Operation.COLLECTION_CREATE: WebCallPolicyBinding(
            CallPolicy.MUTATION,
            (
                _native(RPCMethod.LIST_LABELS, _IDEMPOTENT, "baseline and create readback"),
                _native(RPCMethod.CREATE_LABEL, _NO_RETRY, "account collection allocation"),
            ),
        ),
        Operation.COLLECTION_UPDATE: WebCallPolicyBinding(
            CallPolicy.MUTATION,
            (
                _native(RPCMethod.LIST_LABELS, _IDEMPOTENT, "preflight and readback"),
                _native(RPCMethod.UPDATE_LABEL, _IDEMPOTENT, "collection field set-op"),
                _native(
                    RPCMethod.UPDATE_LABEL,
                    _NO_RETRY,
                    "notebook membership append",
                    variant="add_notebooks",
                ),
                _native(
                    RPCMethod.UPDATE_LABEL,
                    _IDEMPOTENT,
                    "notebook membership removal",
                    variant="remove_notebooks",
                ),
            ),
        ),
        Operation.COLLECTION_DELETE: WebCallPolicyBinding(
            CallPolicy.MUTATION,
            (_native(RPCMethod.DELETE_LABEL, _NO_RETRY, "batch collection delete"),),
        ),
        Operation.SETTINGS_GET: WebCallPolicyBinding(
            CallPolicy.READ,
            (_native(RPCMethod.GET_USER_SETTINGS, _IDEMPOTENT, "account settings read"),),
        ),
        Operation.SETTINGS_GET_LIMITS: WebCallPolicyBinding(
            CallPolicy.READ,
            (_native(RPCMethod.GET_USER_SETTINGS, _IDEMPOTENT, "account limits read"),),
        ),
        Operation.SETTINGS_SET_LANGUAGE: WebCallPolicyBinding(
            CallPolicy.MUTATION,
            (_native(RPCMethod.SET_USER_SETTINGS, _IDEMPOTENT, "output-language mutation"),),
        ),
        Operation.NOTEBOOK_SUGGEST_PROMPTS: WebCallPolicyBinding(
            CallPolicy.STATEFUL_START,
            (
                _native(RPCMethod.GET_NOTEBOOK, _IDEMPOTENT, "optional source resolution"),
                _native(RPCMethod.SUGGEST_PROMPTS, _IDEMPOTENT, "prompt suggestion read"),
            ),
        ),
        Operation.ARTIFACT_SUGGEST_REPORTS: WebCallPolicyBinding(
            CallPolicy.STATEFUL_START,
            (
                _native(
                    RPCMethod.GET_SUGGESTED_REPORTS,
                    _IDEMPOTENT,
                    "report-format suggestion read",
                ),
            ),
        ),
        Operation.ARTIFACT_LIST: WebCallPolicyBinding(
            CallPolicy.READ,
            (
                _native(RPCMethod.LIST_ARTIFACTS, _IDEMPOTENT, "studio catalog read"),
                _native(
                    RPCMethod.GET_NOTES_AND_MIND_MAPS,
                    _IDEMPOTENT,
                    "conditional note-backed mind-map merge",
                ),
            ),
        ),
        Operation.ARTIFACT_GET: WebCallPolicyBinding(
            CallPolicy.READ,
            (
                _native(RPCMethod.LIST_ARTIFACTS, _IDEMPOTENT, "catalog identity scan"),
                _native(
                    RPCMethod.GET_NOTES_AND_MIND_MAPS,
                    _IDEMPOTENT,
                    "note-backed mind-map identity scan",
                ),
            ),
        ),
        Operation.ARTIFACT_GENERATE_AUDIO: WebCallPolicyBinding(
            CallPolicy.STATEFUL_START,
            (
                _native(
                    RPCMethod.GET_NOTEBOOK,
                    _IDEMPOTENT,
                    "conditional default-source resolution",
                ),
                _native(
                    RPCMethod.CREATE_ARTIFACT,
                    _PROBE_CREATE,
                    "audio artifact allocation",
                ),
            ),
        ),
        Operation.ARTIFACT_GENERATE_QUIZ: WebCallPolicyBinding(
            CallPolicy.STATEFUL_START,
            (
                _native(
                    RPCMethod.GET_NOTEBOOK,
                    _IDEMPOTENT,
                    "conditional default-source resolution",
                ),
                _native(
                    RPCMethod.CREATE_ARTIFACT,
                    _PROBE_CREATE,
                    "quiz artifact allocation",
                ),
            ),
        ),
        Operation.ARTIFACT_GENERATE_FLASHCARDS: WebCallPolicyBinding(
            CallPolicy.STATEFUL_START,
            (
                _native(
                    RPCMethod.GET_NOTEBOOK,
                    _IDEMPOTENT,
                    "conditional default-source resolution",
                ),
                _native(
                    RPCMethod.CREATE_ARTIFACT,
                    _PROBE_CREATE,
                    "flashcards artifact allocation",
                ),
            ),
        ),
        Operation.ARTIFACT_GENERATE_VIDEO: WebCallPolicyBinding(
            CallPolicy.STATEFUL_START,
            (
                _native(RPCMethod.GET_NOTEBOOK, _IDEMPOTENT, "optional all-source resolution"),
                _native(RPCMethod.CREATE_ARTIFACT, _PROBE_CREATE, "guarded video kickoff"),
            ),
        ),
        Operation.ARTIFACT_GENERATE_REPORT: WebCallPolicyBinding(
            CallPolicy.STATEFUL_START,
            (
                _native(RPCMethod.GET_NOTEBOOK, _IDEMPOTENT, "optional all-source resolution"),
                _native(RPCMethod.CREATE_ARTIFACT, _PROBE_CREATE, "guarded report kickoff"),
            ),
        ),
        Operation.ARTIFACT_GENERATE_INFOGRAPHIC: WebCallPolicyBinding(
            CallPolicy.STATEFUL_START,
            (
                _native(RPCMethod.GET_NOTEBOOK, _IDEMPOTENT, "optional all-source resolution"),
                _native(RPCMethod.CREATE_ARTIFACT, _PROBE_CREATE, "guarded infographic kickoff"),
            ),
        ),
        Operation.ARTIFACT_GENERATE_SLIDE_DECK: WebCallPolicyBinding(
            CallPolicy.STATEFUL_START,
            (
                _native(RPCMethod.GET_NOTEBOOK, _IDEMPOTENT, "optional all-source resolution"),
                _native(RPCMethod.CREATE_ARTIFACT, _PROBE_CREATE, "guarded slide-deck kickoff"),
            ),
        ),
        Operation.ARTIFACT_GENERATE_DATA_TABLE: WebCallPolicyBinding(
            CallPolicy.STATEFUL_START,
            (
                _native(RPCMethod.GET_NOTEBOOK, _IDEMPOTENT, "optional source-set read"),
                _native(RPCMethod.CREATE_ARTIFACT, _PROBE_CREATE, "data-table kickoff"),
            ),
        ),
        Operation.ARTIFACT_GENERATE_MIND_MAP: WebCallPolicyBinding(
            CallPolicy.STATEFUL_START,
            (
                _native(RPCMethod.GET_NOTEBOOK, _IDEMPOTENT, "optional source-set read"),
                _native(RPCMethod.GENERATE_MIND_MAP, _PROBE_CREATE, "mind-map tree generation"),
                _native(
                    RPCMethod.CREATE_NOTE,
                    _NO_RETRY,
                    "non-idempotent note allocation",
                    variant="plain",
                ),
                _native(RPCMethod.UPDATE_NOTE, _IDEMPOTENT, "persist tree and title"),
                _native(RPCMethod.DELETE_NOTE, _IDEMPOTENT, "cancelled create cleanup"),
            ),
        ),
        Operation.ARTIFACT_EXPORT: WebCallPolicyBinding(
            CallPolicy.MUTATION,
            (
                _native(
                    RPCMethod.EXPORT_ARTIFACT,
                    _NO_RETRY,
                    "explicit Google Drive companion export",
                ),
            ),
        ),
        Operation.ARTIFACT_REVISE_SLIDE: WebCallPolicyBinding(
            CallPolicy.MUTATION,
            (_native(RPCMethod.REVISE_SLIDE, _NO_RETRY, "unrepeatable slide revision"),),
        ),
        Operation.ARTIFACT_RETRY: WebCallPolicyBinding(
            CallPolicy.STATEFUL_START,
            (_native(RPCMethod.RETRY_ARTIFACT, _NO_RETRY, "unrepeatable retry kickoff"),),
        ),
        Operation.ARTIFACT_DELETE: WebCallPolicyBinding(
            CallPolicy.MUTATION,
            (_native(RPCMethod.DELETE_ARTIFACT, _IDEMPOTENT, "idempotent deletion"),),
        ),
        Operation.ARTIFACT_RENAME: WebCallPolicyBinding(
            CallPolicy.MUTATION,
            (
                _native(RPCMethod.RENAME_ARTIFACT, _IDEMPOTENT, "title set operation"),
                _native(RPCMethod.LIST_ARTIFACTS, _IDEMPOTENT, "post-mutation readback"),
            ),
        ),
        Operation.ARTIFACT_DOWNLOAD: WebCallPolicyBinding(
            CallPolicy.READ,
            (
                _native(RPCMethod.LIST_ARTIFACTS, _IDEMPOTENT, "representation catalog read"),
                _native(
                    RPCMethod.GET_NOTES_AND_MIND_MAPS,
                    _IDEMPOTENT,
                    "note-backed mind-map representation read",
                ),
                _native(
                    RPCMethod.GET_INTERACTIVE_HTML,
                    _IDEMPOTENT,
                    "interactive representation read",
                ),
            ),
            known_divergence=_APP_DOWNLOAD_DIVERGENCE,
        ),
        Operation.ARTIFACT_WAIT: WebCallPolicyBinding(
            CallPolicy.READ,
            (_native(RPCMethod.LIST_ARTIFACTS, _IDEMPOTENT, "lifecycle status poll"),),
        ),
        Operation.NOTE_LIST: WebCallPolicyBinding(
            CallPolicy.READ,
            (
                _native(
                    RPCMethod.GET_NOTES_AND_MIND_MAPS,
                    _IDEMPOTENT,
                    "plain-note collection read",
                ),
            ),
        ),
        Operation.NOTE_GET: WebCallPolicyBinding(
            CallPolicy.READ,
            (
                _native(
                    RPCMethod.GET_NOTES_AND_MIND_MAPS,
                    _IDEMPOTENT,
                    "plain-note identity scan",
                ),
            ),
        ),
        Operation.NOTE_CREATE: WebCallPolicyBinding(
            CallPolicy.MUTATION,
            (
                _native(
                    RPCMethod.CREATE_NOTE,
                    _NO_RETRY,
                    "non-idempotent plain-note allocation",
                    variant="plain",
                ),
            ),
        ),
        Operation.NOTE_UPDATE: WebCallPolicyBinding(
            CallPolicy.MUTATION,
            (_native(RPCMethod.UPDATE_NOTE, _IDEMPOTENT, "note content/title set-op"),),
        ),
        Operation.NOTE_DELETE: WebCallPolicyBinding(
            CallPolicy.MUTATION,
            (_native(RPCMethod.DELETE_NOTE, _IDEMPOTENT, "idempotent note delete"),),
        ),
        Operation.MIND_MAP_LIST: WebCallPolicyBinding(
            CallPolicy.READ,
            (
                _native(
                    RPCMethod.GET_NOTES_AND_MIND_MAPS,
                    _IDEMPOTENT,
                    "note-backed mind-map collection read",
                ),
            ),
        ),
        Operation.MIND_MAP_GET: WebCallPolicyBinding(
            CallPolicy.READ,
            (
                _native(
                    RPCMethod.GET_INTERACTIVE_HTML,
                    _IDEMPOTENT,
                    "interactive tree read",
                ),
            ),
        ),
        Operation.MIND_MAP_GENERATE_NOTE: WebCallPolicyBinding(
            CallPolicy.STATEFUL_START,
            (
                _native(
                    RPCMethod.GET_NOTEBOOK,
                    _IDEMPOTENT,
                    "conditional default-source resolution",
                ),
                _native(
                    RPCMethod.GENERATE_MIND_MAP,
                    _PROBE_CREATE,
                    "note-backed tree generation",
                ),
            ),
        ),
        Operation.MIND_MAP_GENERATE_INTERACTIVE: WebCallPolicyBinding(
            CallPolicy.STATEFUL_START,
            (
                _native(
                    RPCMethod.GET_NOTEBOOK,
                    _IDEMPOTENT,
                    "conditional default-source resolution",
                ),
                _native(
                    RPCMethod.CREATE_ARTIFACT,
                    _PROBE_CREATE,
                    "interactive mind-map allocation",
                ),
            ),
        ),
        Operation.MIND_MAP_UPDATE: WebCallPolicyBinding(
            CallPolicy.MUTATION,
            (_native(RPCMethod.RENAME_ARTIFACT, _IDEMPOTENT, "interactive title set-op"),),
        ),
        Operation.MIND_MAP_DELETE: WebCallPolicyBinding(
            CallPolicy.MUTATION,
            (_native(RPCMethod.DELETE_ARTIFACT, _IDEMPOTENT, "idempotent interactive delete"),),
        ),
        Operation.SHARING_GET: WebCallPolicyBinding(
            CallPolicy.READ,
            (_native(RPCMethod.GET_SHARE_STATUS, _IDEMPOTENT, "sharing status read"),),
        ),
        Operation.SHARING_SET_PUBLIC: WebCallPolicyBinding(
            CallPolicy.MUTATION,
            (
                _native(RPCMethod.SHARE_NOTEBOOK, _PROBE_CREATE, "guarded link mutation"),
                _native(RPCMethod.GET_SHARE_STATUS, _IDEMPOTENT, "post-mutation read"),
            ),
        ),
        Operation.SHARING_SET_VIEW_LEVEL: WebCallPolicyBinding(
            CallPolicy.MUTATION,
            (
                _native(RPCMethod.RENAME_NOTEBOOK, _IDEMPOTENT, "viewer-scope set-op"),
                _native(RPCMethod.GET_SHARE_STATUS, _IDEMPOTENT, "post-mutation read"),
            ),
        ),
        Operation.SHARING_UPDATE_USERS: WebCallPolicyBinding(
            CallPolicy.MUTATION,
            (
                _native(RPCMethod.SHARE_NOTEBOOK, _PROBE_CREATE, "guarded ACL mutation"),
                _native(RPCMethod.GET_SHARE_STATUS, _IDEMPOTENT, "post-mutation read"),
            ),
        ),
        Operation.LEGACY_SHARE_ARTIFACT: WebCallPolicyBinding(
            CallPolicy.MUTATION,
            (
                _native(
                    RPCMethod.SHARE_ARTIFACT,
                    _IDEMPOTENT,
                    "legacy public share-link set-op",
                ),
            ),
        ),
        Operation.RESEARCH_START: WebCallPolicyBinding(
            CallPolicy.STATEFUL_START,
            (
                _native(
                    RPCMethod.START_FAST_RESEARCH,
                    _NO_RETRY,
                    "non-idempotent fast research start",
                ),
                _native(
                    RPCMethod.START_DEEP_RESEARCH,
                    _NO_RETRY,
                    "non-idempotent deep research start",
                ),
            ),
        ),
        Operation.RESEARCH_POLL: WebCallPolicyBinding(
            CallPolicy.READ,
            (_native(RPCMethod.POLL_RESEARCH, _IDEMPOTENT, "research task poll"),),
        ),
        Operation.RESEARCH_CANCEL: WebCallPolicyBinding(
            CallPolicy.MUTATION,
            (_native(RPCMethod.CANCEL_RESEARCH, _IDEMPOTENT, "terminal-state set-op"),),
        ),
        Operation.RESEARCH_IMPORT: WebCallPolicyBinding(
            CallPolicy.MUTATION,
            (_native(RPCMethod.IMPORT_RESEARCH, _NO_RETRY, "non-idempotent source import"),),
        ),
    }
)


def audit_web_call_policy_bindings(
    definitions: Mapping[Operation, OperationDef[Any, Any]],
    *,
    bindings: Mapping[Operation, WebCallPolicyBinding] = WEB_CALL_POLICY_BINDINGS,
    registry: IdempotencyRegistry = IDEMPOTENCY_REGISTRY,
) -> tuple[str, ...]:
    """Return deterministic active-binding drift errors without changing retry behavior."""
    errors: list[str] = []
    missing = set(definitions) - set(bindings)
    stale = set(bindings) - set(definitions)
    if missing:
        errors.append(
            "active web operations lack policy bindings: "
            + ", ".join(sorted(operation.value for operation in missing))
        )
    if stale:
        errors.append(
            "policy bindings name inactive web operations: "
            + ", ".join(sorted(operation.value for operation in stale))
        )

    for operation in sorted(set(definitions) & set(bindings), key=lambda item: item.value):
        definition = definitions[operation]
        binding = bindings[operation]
        if definition.key is not operation:
            errors.append(f"{operation.value}: definition key is {definition.key.value}")
        if definition.policy is not binding.policy:
            errors.append(
                f"{operation.value}: semantic policy is {definition.policy.value}, "
                f"reviewed binding expects {binding.policy.value}"
            )
        native_keys = [(item.method, item.variant) for item in binding.native_bindings]
        if len(native_keys) != len(set(native_keys)):
            errors.append(f"{operation.value}: duplicate native policy bindings")
        if not binding.native_bindings:
            errors.append(f"{operation.value}: active web operation has no native policy binding")
        for native in binding.native_bindings:
            try:
                actual = registry.get_entry(native.method, operation_variant=native.variant).policy
            except Exception as exc:  # pragma: no cover - registry owns exact exception types
                errors.append(
                    f"{operation.value}: cannot resolve {native.method.name}:"
                    f"{native.variant or '<default>'}: {type(exc).__name__}"
                )
                continue
            if actual is not native.expected_policy:
                errors.append(
                    f"{operation.value}: {native.method.name}:"
                    f"{native.variant or '<default>'} idempotency is {actual.value}, "
                    f"reviewed binding expects {native.expected_policy.value}"
                )
    return tuple(errors)


def web_call_policy_report() -> dict[str, object]:
    """Return the stable catalog projection of active semantic/native parity."""

    def operation_key(item: tuple[Operation, WebCallPolicyBinding]) -> str:
        operation, _binding = item
        return operation.value

    return {
        operation.value: {
            "call_policy": binding.policy.value,
            "known_divergence": binding.known_divergence,
            "native_bindings": [
                {
                    "rpc_method": native.method.name,
                    "variant": native.variant,
                    "idempotency_policy": native.expected_policy.value,
                    "role": native.role,
                }
                for native in binding.native_bindings
            ],
        }
        for operation, binding in sorted(WEB_CALL_POLICY_BINDINGS.items(), key=operation_key)
    }


__all__ = [
    "NativePolicyBinding",
    "WEB_CALL_POLICY_BINDINGS",
    "WebCallPolicyBinding",
    "audit_web_call_policy_bindings",
    "web_call_policy_report",
]
