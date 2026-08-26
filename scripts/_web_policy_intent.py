"""Hand-reviewed native/policy intent for every active web operation.

This is the *expected* half of the P4 parity audit (P10 decision D3): the exact
natives each active operation is reviewed to reach, their roles, their expected
:class:`~notebooklm._idempotency.IdempotencyPolicy`, the leaf edges each P9.2
service-owned workflow sequences, and the reviewed divergences.

It lives beside :mod:`scripts._operation_catalog_specs` — the other hand-authored
catalog metadata — and deliberately **not** in production, and deliberately not
derived from the binding rows or the web registry: the *actual* native set is
derived from each row's ``NativeCallSpec.choices`` (invariant I7), and an audit
whose two sides are both derived from the rows would compare the rows with
themselves. The comparison itself lives in :mod:`scripts.audit_operation_catalog`
and runs as a test, not at registry construction.

``CallPolicy`` describes the whole semantic workflow. It is deliberately not the
retry authority: individual web calls continue to resolve retry behavior from
:mod:`notebooklm._idempotency`. This ledger makes the relationship exact and
fail-closed without feeding semantic policy back into the executor.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from notebooklm._idempotency import IdempotencyPolicy
from notebooklm._operations import CallPolicy, Operation
from notebooklm.rpc import RPCMethod


@dataclass(frozen=True, slots=True)
class NativePolicyBinding:
    """One exact native method/variant and its reviewed retry classification."""

    method: RPCMethod
    variant: str | None
    expected_policy: IdempotencyPolicy
    role: str


@dataclass(frozen=True, slots=True)
class StreamedPolicyBinding:
    """One streamed verb an active web operation dispatches.

    A streamed request carries no ``RPCMethod`` and therefore no idempotency
    registry entry: it is not a batchexecute call and the retry middleware
    never classifies it.  Recording it here is what lets the audit tell a
    reviewed stream-only row apart from a row that simply forgot its natives.
    """

    label: str
    role: str


@dataclass(frozen=True, slots=True)
class WorkflowLeaf:
    """One leaf operation a service-owned workflow invokes, with its allowed variants."""

    operation: Operation
    allowed_variants: frozenset[str | None]


@dataclass(frozen=True, slots=True)
class WorkflowPolicyBinding:
    """A service-owned workflow: its policy, hand-reviewed natives, and leaf edges.

    ``native_bindings`` stays hand-reviewed so the P4 parity audit remains an
    independent check: the audit derives the workflow's native set transitively
    from ``leaf_operations`` (each leaf's ledger row filtered by the allowed
    variants) and compares it with these expected natives exactly as it did
    when the workflow was one web handler.
    """

    policy: CallPolicy
    native_bindings: tuple[NativePolicyBinding, ...]
    leaf_operations: tuple[WorkflowLeaf, ...]
    known_divergence: str | None = None


@dataclass(frozen=True, slots=True)
class WebCallPolicyBinding:
    """Whole-workflow policy plus every native call reachable in its web handler."""

    policy: CallPolicy
    native_bindings: tuple[NativePolicyBinding, ...]
    known_divergence: str | None = None
    #: Streamed verbs the row dispatches. An active operation must reach the
    #: wire somehow: the audit requires at least one native *or* one stream.
    streamed_bindings: tuple[StreamedPolicyBinding, ...] = ()


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
        Operation.NOTEBOOK_ALLOCATE: WebCallPolicyBinding(
            CallPolicy.MUTATION,
            (_native(RPCMethod.CREATE_NOTEBOOK, _PROBE_CREATE, "guarded create"),),
        ),
        Operation.NOTEBOOK_PATCH: WebCallPolicyBinding(
            CallPolicy.MUTATION,
            (_native(RPCMethod.RENAME_NOTEBOOK, _IDEMPOTENT, "property mutation"),),
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
        Operation.SOURCE_PATCH_TITLE: WebCallPolicyBinding(
            CallPolicy.MUTATION,
            (_native(RPCMethod.UPDATE_SOURCE, _IDEMPOTENT, "source title set-op"),),
        ),
        # P10 primitive: one ADD_SOURCE allocation, the variant chosen from the
        # request's registration kind. The three choices deliberately carry two
        # different reviewed retry classifications — the registry keys on
        # (method, variant), so collapsing the source-add family onto one leaf
        # cannot flatten text's NON_IDEMPOTENT_NO_RETRY into the url/drive
        # PROBE_THEN_CREATE the way a method-keyed ledger would.
        Operation.SOURCE_REGISTER: WebCallPolicyBinding(
            CallPolicy.MUTATION,
            (
                _native(
                    RPCMethod.ADD_SOURCE,
                    _PROBE_CREATE,
                    "guarded generic/YouTube create (single or true batch)",
                    variant="url",
                ),
                _native(
                    RPCMethod.ADD_SOURCE,
                    _NO_RETRY,
                    "non-idempotent pasted-text allocation",
                    variant="text",
                ),
                _native(
                    RPCMethod.ADD_SOURCE,
                    _PROBE_CREATE,
                    "guarded Drive-document allocation",
                    variant="drive",
                ),
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
        Operation.CHAT_STREAM_ANSWER: WebCallPolicyBinding(
            CallPolicy.STREAM,
            (),
            streamed_bindings=(
                StreamedPolicyBinding(
                    "chat.ask",
                    "streamed free-form answer generation",
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
            # Reviewed ledger correction (P9.3 chat; gate table §9): the handler
            # never issued GET_LAST_CONVERSATION_ID — the facade resolves the
            # conversation through CHAT_GET_CONVERSATION above the port.
            (
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
        # P9.2 primitives: one native set-op each, sequenced by the hoisted
        # label/collection workflows above the port.
        Operation.LABEL_MUTATE: WebCallPolicyBinding(
            CallPolicy.MUTATION,
            (
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
        Operation.LABEL_ALLOCATE: WebCallPolicyBinding(
            CallPolicy.MUTATION,
            (_native(RPCMethod.CREATE_LABEL, _NO_RETRY, "manual group allocation"),),
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
            (_native(RPCMethod.SUGGEST_PROMPTS, _IDEMPOTENT, "prompt suggestion read"),),
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
        Operation.ARTIFACT_GENERATE_AUDIO: WebCallPolicyBinding(
            CallPolicy.STATEFUL_START,
            (_native(RPCMethod.CREATE_ARTIFACT, _PROBE_CREATE, "audio artifact allocation"),),
        ),
        Operation.ARTIFACT_GENERATE_QUIZ: WebCallPolicyBinding(
            CallPolicy.STATEFUL_START,
            (_native(RPCMethod.CREATE_ARTIFACT, _PROBE_CREATE, "quiz artifact allocation"),),
        ),
        Operation.ARTIFACT_GENERATE_FLASHCARDS: WebCallPolicyBinding(
            CallPolicy.STATEFUL_START,
            (_native(RPCMethod.CREATE_ARTIFACT, _PROBE_CREATE, "flashcards artifact allocation"),),
        ),
        Operation.ARTIFACT_GENERATE_VIDEO: WebCallPolicyBinding(
            CallPolicy.STATEFUL_START,
            (_native(RPCMethod.CREATE_ARTIFACT, _PROBE_CREATE, "guarded video kickoff"),),
        ),
        Operation.ARTIFACT_GENERATE_REPORT: WebCallPolicyBinding(
            CallPolicy.STATEFUL_START,
            (_native(RPCMethod.CREATE_ARTIFACT, _PROBE_CREATE, "guarded report kickoff"),),
        ),
        Operation.ARTIFACT_GENERATE_INFOGRAPHIC: WebCallPolicyBinding(
            CallPolicy.STATEFUL_START,
            (_native(RPCMethod.CREATE_ARTIFACT, _PROBE_CREATE, "guarded infographic kickoff"),),
        ),
        Operation.ARTIFACT_GENERATE_SLIDE_DECK: WebCallPolicyBinding(
            CallPolicy.STATEFUL_START,
            (_native(RPCMethod.CREATE_ARTIFACT, _PROBE_CREATE, "guarded slide-deck kickoff"),),
        ),
        Operation.ARTIFACT_GENERATE_DATA_TABLE: WebCallPolicyBinding(
            CallPolicy.STATEFUL_START,
            (_native(RPCMethod.CREATE_ARTIFACT, _PROBE_CREATE, "data-table kickoff"),),
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
        Operation.ARTIFACT_PATCH_TITLE: WebCallPolicyBinding(
            CallPolicy.MUTATION,
            (_native(RPCMethod.RENAME_ARTIFACT, _IDEMPOTENT, "title set operation"),),
        ),
        Operation.ARTIFACT_CATALOG: WebCallPolicyBinding(
            CallPolicy.READ,
            (_native(RPCMethod.LIST_ARTIFACTS, _IDEMPOTENT, "plain Studio catalog read"),),
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
        Operation.MIND_MAP_GENERATE: WebCallPolicyBinding(
            CallPolicy.STATEFUL_START,
            (
                _native(
                    RPCMethod.GENERATE_MIND_MAP,
                    _PROBE_CREATE,
                    "resolved-source tree generation",
                ),
            ),
        ),
        Operation.SHARING_GET: WebCallPolicyBinding(
            CallPolicy.READ,
            (_native(RPCMethod.GET_SHARE_STATUS, _IDEMPOTENT, "sharing status read"),),
        ),
        Operation.SHARING_MUTATE: WebCallPolicyBinding(
            CallPolicy.MUTATION,
            (_native(RPCMethod.SHARE_NOTEBOOK, _PROBE_CREATE, "guarded link/ACL mutation"),),
        ),
        Operation.SHARING_PATCH_VIEW_LEVEL: WebCallPolicyBinding(
            CallPolicy.MUTATION,
            (_native(RPCMethod.RENAME_NOTEBOOK, _IDEMPOTENT, "viewer-scope set-op"),),
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


def _leaf(operation: Operation, *variants: str | None) -> WorkflowLeaf:
    return WorkflowLeaf(operation, frozenset(variants))


def _stream_leaf(operation: Operation) -> WorkflowLeaf:
    """A leaf whose row streams: it declares no native variant to allow."""
    return WorkflowLeaf(operation, frozenset())


# P9.2 service-owned workflows. Each row keeps the natives the P6 handler
# executed (reviewed by hand) plus the leaf edges the semantic service now
# sequences; the audit checks the two agree.
SERVICE_OWNED_WORKFLOW_BINDINGS: Final[Mapping[Operation, WorkflowPolicyBinding]] = (
    MappingProxyType(
        {
            Operation.NOTEBOOK_CREATE: WorkflowPolicyBinding(
                CallPolicy.MUTATION,
                (
                    _native(RPCMethod.LIST_NOTEBOOKS, _IDEMPOTENT, "baseline/probe/quota read"),
                    _native(RPCMethod.CREATE_NOTEBOOK, _PROBE_CREATE, "guarded create"),
                    _native(RPCMethod.GET_USER_SETTINGS, _IDEMPOTENT, "quota diagnosis"),
                ),
                (
                    _leaf(Operation.NOTEBOOK_LIST, None),
                    _leaf(Operation.NOTEBOOK_ALLOCATE, None),
                    _leaf(Operation.SETTINGS_GET_LIMITS, None),
                ),
            ),
            Operation.LABEL_CREATE: WorkflowPolicyBinding(
                CallPolicy.MUTATION,
                (
                    _native(RPCMethod.LIST_LABELS, _IDEMPOTENT, "pre-create identity baseline"),
                    _native(RPCMethod.CREATE_LABEL, _NO_RETRY, "manual source-label allocation"),
                ),
                (
                    _leaf(Operation.LABEL_LIST, None),
                    _leaf(Operation.LABEL_ALLOCATE, None),
                ),
            ),
            Operation.LABEL_UPDATE: WorkflowPolicyBinding(
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
                (
                    _leaf(Operation.LABEL_GET, None),
                    _leaf(Operation.LABEL_MUTATE, None, "add_sources", "remove_sources"),
                ),
            ),
            Operation.COLLECTION_CREATE: WorkflowPolicyBinding(
                CallPolicy.MUTATION,
                (
                    _native(RPCMethod.LIST_LABELS, _IDEMPOTENT, "baseline and create readback"),
                    _native(RPCMethod.CREATE_LABEL, _NO_RETRY, "account collection allocation"),
                ),
                (
                    _leaf(Operation.COLLECTION_LIST, None),
                    _leaf(Operation.LABEL_ALLOCATE, None),
                ),
            ),
            Operation.COLLECTION_UPDATE: WorkflowPolicyBinding(
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
                (
                    _leaf(Operation.COLLECTION_GET, None),
                    _leaf(Operation.LABEL_MUTATE, None, "add_notebooks", "remove_notebooks"),
                ),
            ),
            Operation.SOURCE_ADD_URL: WorkflowPolicyBinding(
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
                (
                    _leaf(Operation.SOURCE_LIST, None),
                    _leaf(Operation.SOURCE_REGISTER, "url"),
                    _leaf(Operation.SOURCE_PATCH_TITLE, None),
                    _leaf(Operation.SOURCE_GET, None),
                ),
            ),
            Operation.SOURCE_ADD_DRIVE: WorkflowPolicyBinding(
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
                (
                    _leaf(Operation.SOURCE_LIST, None),
                    _leaf(Operation.SOURCE_REGISTER, "drive"),
                    _leaf(Operation.SOURCE_PATCH_TITLE, None),
                ),
            ),
            Operation.SOURCE_ADD_URL_BATCH: WorkflowPolicyBinding(
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
                (
                    _leaf(Operation.SOURCE_REGISTER, "url"),
                    _leaf(Operation.SOURCE_LIST, None),
                ),
            ),
            Operation.SOURCE_ADD_TEXT: WorkflowPolicyBinding(
                CallPolicy.MUTATION,
                (
                    _native(
                        RPCMethod.ADD_SOURCE,
                        _NO_RETRY,
                        "non-idempotent pasted-text allocation",
                        variant="text",
                    ),
                ),
                (_leaf(Operation.SOURCE_REGISTER, "text"),),
            ),
            Operation.SOURCE_UPDATE: WorkflowPolicyBinding(
                CallPolicy.MUTATION,
                (
                    _native(RPCMethod.UPDATE_SOURCE, _IDEMPOTENT, "source title set-op"),
                    _native(
                        RPCMethod.GET_NOTEBOOK,
                        _IDEMPOTENT,
                        "conditional null-echo readback",
                    ),
                ),
                (
                    _leaf(Operation.SOURCE_PATCH_TITLE, None),
                    _leaf(Operation.SOURCE_GET, None),
                ),
            ),
            Operation.SHARING_SET_PUBLIC: WorkflowPolicyBinding(
                CallPolicy.MUTATION,
                (
                    _native(RPCMethod.SHARE_NOTEBOOK, _PROBE_CREATE, "guarded link mutation"),
                    _native(RPCMethod.GET_SHARE_STATUS, _IDEMPOTENT, "post-mutation read"),
                ),
                (
                    _leaf(Operation.SHARING_MUTATE, None),
                    _leaf(Operation.SHARING_GET, None),
                ),
            ),
            Operation.SHARING_UPDATE_USERS: WorkflowPolicyBinding(
                CallPolicy.MUTATION,
                (
                    _native(RPCMethod.SHARE_NOTEBOOK, _PROBE_CREATE, "guarded ACL mutation"),
                    _native(RPCMethod.GET_SHARE_STATUS, _IDEMPOTENT, "post-mutation read"),
                ),
                (
                    _leaf(Operation.SHARING_MUTATE, None),
                    _leaf(Operation.SHARING_GET, None),
                ),
            ),
            Operation.SHARING_SET_VIEW_LEVEL: WorkflowPolicyBinding(
                CallPolicy.MUTATION,
                (
                    _native(RPCMethod.RENAME_NOTEBOOK, _IDEMPOTENT, "viewer-scope set-op"),
                    _native(RPCMethod.GET_SHARE_STATUS, _IDEMPOTENT, "post-mutation read"),
                ),
                (
                    _leaf(Operation.SHARING_PATCH_VIEW_LEVEL, None),
                    _leaf(Operation.SHARING_GET, None),
                ),
            ),
            Operation.ARTIFACT_GENERATE_MIND_MAP: WorkflowPolicyBinding(
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
                (
                    _leaf(Operation.MIND_MAP_GENERATE_NOTE, None),
                    _leaf(Operation.NOTE_CREATE, "plain"),
                    _leaf(Operation.NOTE_UPDATE, None),
                    _leaf(Operation.NOTE_DELETE, None),
                ),
            ),
            Operation.ARTIFACT_LIST: WorkflowPolicyBinding(
                CallPolicy.READ,
                (
                    _native(RPCMethod.LIST_ARTIFACTS, _IDEMPOTENT, "studio catalog read"),
                    _native(
                        RPCMethod.GET_NOTES_AND_MIND_MAPS,
                        _IDEMPOTENT,
                        "conditional note-backed mind-map merge",
                    ),
                ),
                (
                    _leaf(Operation.ARTIFACT_CATALOG, None),
                    _leaf(Operation.MIND_MAP_LIST, None),
                ),
            ),
            Operation.ARTIFACT_GET: WorkflowPolicyBinding(
                CallPolicy.READ,
                (
                    _native(RPCMethod.LIST_ARTIFACTS, _IDEMPOTENT, "catalog identity scan"),
                    _native(
                        RPCMethod.GET_NOTES_AND_MIND_MAPS,
                        _IDEMPOTENT,
                        "note-backed mind-map identity scan",
                    ),
                ),
                (
                    _leaf(Operation.ARTIFACT_CATALOG, None),
                    _leaf(Operation.MIND_MAP_LIST, None),
                ),
            ),
            Operation.ARTIFACT_RENAME: WorkflowPolicyBinding(
                CallPolicy.MUTATION,
                (
                    _native(RPCMethod.RENAME_ARTIFACT, _IDEMPOTENT, "title set operation"),
                    _native(RPCMethod.LIST_ARTIFACTS, _IDEMPOTENT, "post-mutation readback"),
                ),
                (
                    _leaf(Operation.ARTIFACT_PATCH_TITLE, None),
                    _leaf(Operation.ARTIFACT_CATALOG, None),
                ),
            ),
            Operation.CHAT_ASK: WorkflowPolicyBinding(
                CallPolicy.STREAM,
                (
                    _native(RPCMethod.GET_NOTEBOOK, _IDEMPOTENT, "default source-set read"),
                    _native(
                        RPCMethod.GET_LAST_CONVERSATION_ID,
                        _IDEMPOTENT,
                        "conversation resolution before or after the streamed request",
                    ),
                ),
                (
                    _stream_leaf(Operation.CHAT_STREAM_ANSWER),
                    _leaf(Operation.CHAT_GET_CONVERSATION, None),
                ),
                # P9.4 (gate table §9): the workflow reaches only
                # ``GET_LAST_CONVERSATION_ID`` plus the streamed query; the default
                # source-set ``GET_NOTEBOOK`` is issued above the port by the facade
                # through ``NOTEBOOK_GET`` and stays here only because the catalog's
                # recency contract for ``chat.ask`` is keyed on this ledger row.
                known_divergence=(
                    "GET_NOTEBOOK is the facade's NOTEBOOK_GET recency read, not a native the "
                    "CHAT_ASK row dispatches"
                ),
            ),
            Operation.NOTEBOOK_UPDATE: WorkflowPolicyBinding(
                CallPolicy.MUTATION,
                (
                    _native(RPCMethod.RENAME_NOTEBOOK, _IDEMPOTENT, "property mutation"),
                    _native(RPCMethod.GET_NOTEBOOK, _IDEMPOTENT, "post-mutation readback"),
                ),
                (
                    _leaf(Operation.NOTEBOOK_PATCH, None),
                    _leaf(Operation.NOTEBOOK_GET, None),
                ),
            ),
            # P10 R6.4. Both were UNSUPPORTED until they gained typed
            # definitions: the wait loop and the import reconciliation have
            # always been sequenced by ``ResearchService``, so the flip records
            # what already ran rather than moving any execution.
            Operation.RESEARCH_WAIT: WorkflowPolicyBinding(
                CallPolicy.READ,
                (_native(RPCMethod.POLL_RESEARCH, _IDEMPOTENT, "research task poll"),),
                (_leaf(Operation.RESEARCH_POLL, None),),
            ),
            Operation.RESEARCH_IMPORT_VERIFY: WorkflowPolicyBinding(
                CallPolicy.MUTATION,
                (
                    _native(RPCMethod.IMPORT_RESEARCH, _NO_RETRY, "non-idempotent source import"),
                    _native(
                        RPCMethod.GET_NOTEBOOK,
                        _IDEMPOTENT,
                        "pre-import baseline and post-failure verification probe",
                    ),
                ),
                (
                    _leaf(Operation.RESEARCH_IMPORT, None),
                    _leaf(Operation.SOURCE_LIST, None),
                ),
            ),
        }
    )
)

__all__ = [
    "NativePolicyBinding",
    "SERVICE_OWNED_WORKFLOW_BINDINGS",
    "StreamedPolicyBinding",
    "WEB_CALL_POLICY_BINDINGS",
    "WebCallPolicyBinding",
    "WorkflowLeaf",
    "WorkflowPolicyBinding",
]
