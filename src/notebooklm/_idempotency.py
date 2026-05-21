"""Idempotency layer for mutating-RPC patterns.

This module hosts two cooperating pieces:

1. :func:`idempotent_create` — the existing per-API probe-then-retry
   wrapper for create-RPC patterns. A create RPC like
   ``NotebooksAPI.create`` or ``SourcesAPI.add_url`` is a mutating POST:
   the *server may have committed the write* even if the client sees a
   5xx or network error. Naive retries duplicate the resource; the
   wrapper inverts the direction: run with internal-retries disabled,
   then probe for a server-side commit before re-issuing.

2. :class:`IdempotencyRegistry` — the 6-policy classification layer that
   :class:`~notebooklm._rpc_executor.RpcExecutor` consults to compute the
   *effective* ``disable_internal_retries`` value (and, for
   ``CLIENT_TOKEN_DEDUPE`` policies, inject a fresh client-token into
   request params before encoding). The registry is a single source of
   truth that future RPCs can be classified against without touching the
   executor.

   This foundation is intentionally **behavior-neutral**: every method
   defaults to :attr:`IdempotencyPolicy.UNCLASSIFIED`, which is silent
   and reproduces today's retry behavior. The Wave-2 classification
   arc (see ADR-005, ``docs/adr/0005-idempotency-taxonomy.md``)
   replaces these placeholders RPC-by-RPC.

Per-API probes used by :func:`idempotent_create` are caller-supplied
because there is no universal probe key (notebooks: title +
baseline-diff; sources: url-match; ``add_text``: no probe possible — see
:class:`~notebooklm.exceptions.NonIdempotentRetryError`).

This module is private (``_idempotency.py``); call sites live in the
domain APIs (``_notebooks.py``, ``_sources.py``) and the RPC executor
(``_rpc_executor.py``). The canonical home for the taxonomy itself, the
six-policy axis, and the per-RPC classification rationale is
ADR-005 (``docs/adr/0005-idempotency-taxonomy.md``).
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any, TypeVar

from .exceptions import (
    IdempotencyVariantError,
    NetworkError,
    RateLimitError,
    ServerError,
)
from .rpc.types import RPCMethod

logger = logging.getLogger(__name__)

T = TypeVar("T")

# The translated exception types that ``rpc_call`` raises when the
# request fails in a way that *might* have committed the write on the
# server. With ``disable_internal_retries=True``, ``_perform_authed_post``
# does not retry these on its own; instead it lets ``rpc_call`` translate
# the underlying ``TransportServerError``/network failure into
# ``ServerError`` / ``NetworkError`` / ``RateLimitError`` and surface it
# here. ``idempotent_create`` catches exactly these; anything else (auth,
# validation, decoding) propagates unchanged because it indicates the
# request never reached a state where the write could land.
#
# Note: ``RPCTimeoutError`` inherits from ``NetworkError`` so it is
# already covered by the ``NetworkError`` catch.
_RETRYABLE_TRANSPORT_ERRORS: tuple[type[BaseException], ...] = (
    RateLimitError,
    ServerError,
    NetworkError,
)


async def idempotent_create(
    create: Callable[[], Awaitable[T]],
    probe: Callable[[], Awaitable[T | None]],
    *,
    max_attempts: int = 2,
    label: str = "create",
) -> T:
    """Probe-then-retry wrapper for mutating create RPCs.

    Args:
        create: Coroutine factory that issues the create RPC. The
            underlying ``rpc_call`` MUST be invoked with
            ``disable_internal_retries=True`` so the first transport
            failure surfaces to this wrapper instead of being retried
            blindly inside ``_perform_authed_post``.
        probe: Coroutine factory that returns the resource if it
            already exists server-side, or ``None`` if not. Probes are
            API-specific (notebooks: list-then-baseline-diff by title;
            sources: list-then-url-match).
        max_attempts: Maximum total ``create()`` invocations (default
            2 — one initial + one retry). Each attempt is followed by
            a probe; the probe runs only after a transport failure.
        label: Diagnostic label embedded in log messages.

    Returns:
        The result of a successful ``create()`` call, or the value
        returned by ``probe()`` after a transient transport failure.

    Raises:
        Whatever ``create()`` raises on the final attempt if the probe
        consistently returns ``None`` and retries are exhausted. Non-
        transport exceptions (auth, validation, decoding) propagate
        from the first ``create()`` call without invoking the probe.

    Cancellation:
        Pure ``await`` — no ``asyncio.shield``. A ``CancelledError``
        propagates immediately at the next yield point so the caller
        keeps full structured-concurrency semantics.
    """
    if max_attempts < 1:
        raise ValueError(f"max_attempts must be >= 1, got {max_attempts}")

    last_error: BaseException | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            return await create()
        except _RETRYABLE_TRANSPORT_ERRORS as exc:
            last_error = exc
            logger.warning(
                "%s attempt %d/%d failed with transport error (%s); "
                "probing for server-side commit before retry",
                label,
                attempt,
                max_attempts,
                type(exc).__name__,
            )
            existing = await probe()
            if existing is not None:
                logger.info(
                    "%s probe found existing resource after transport "
                    "failure on attempt %d; returning it without retry",
                    label,
                    attempt,
                )
                return existing
            # Probe returned None: the create did not land. Loop and
            # retry as long as we have attempts remaining.
            logger.debug(
                "%s probe returned no match on attempt %d; will retry create",
                label,
                attempt,
            )

    # Exhausted attempts. Re-raise the last transport error so callers
    # see the original failure, not a synthetic wrapper.
    assert last_error is not None  # loop body always sets this on failure
    logger.error(
        "%s failed after %d attempts with no probe match; re-raising last error",
        label,
        max_attempts,
    )
    raise last_error


# ============================================================================
# Mutating-RPC idempotency registry (B1 foundation — P0-3 + P1-2)
# ============================================================================
#
# The registry is the single source of truth for "how should this RPC behave
# under retry?" It is consulted by ``RpcExecutor`` at five sites to compute
# the *effective* ``disable_internal_retries`` value, and (for
# ``CLIENT_TOKEN_DEDUPE`` policies) to inject a fresh client-token into
# request params before encoding.
#
# IMPORTANT — behavior-neutral foundation:
#   This module is a foundation only. Every ``RPCMethod`` default-populates
#   to ``IdempotencyPolicy.UNCLASSIFIED`` with the Wave-2 placeholder note.
#   UNCLASSIFIED is silent (no log emission, no behavior change) and
#   resolves to ``effective_disable_internal_retries=False`` so today's
#   retries continue to fire identically. Wave 2 will classify individual
#   RPCs without changing the executor.


class IdempotencyPolicy(str, Enum):
    """Classification axis for mutating-RPC retry safety.

    Six policies — no more, no fewer. The axis was sized to cover all
    realistic NotebookLM RPC shapes without inventing per-method special
    cases. See ADR-005 (``docs/adr/0005-idempotency-taxonomy.md``) for
    the derivation and the per-policy rationale.

    Policies fall into three retry-safety bands:

    * **Safe to retry inside the transport**:
      :attr:`UNCLASSIFIED` (placeholder — preserves today's retries),
      :attr:`IDEMPOTENT_SET_OP` (rename / delete — server is the
      idempotence anchor), :attr:`CLIENT_TOKEN_DEDUPE` (server
      deduplicates by injected token), :attr:`AT_LEAST_ONCE_ACCEPTED`
      (caller has accepted at-least-once semantics; WARN logged).

    * **NOT safe to retry inside the transport**:
      :attr:`PROBE_THEN_CREATE` (callers own the probe loop; transport
      retry would race the probe), :attr:`NON_IDEMPOTENT_NO_RETRY`
      (e.g. ``add_text`` — no probe key, must surface the first
      failure).

    The ``str`` mixin keeps the enum JSON-serializable and consistent
    with :class:`~notebooklm.rpc.RPCMethod` (which also uses ``str,
    Enum`` rather than ``StrEnum`` for 3.10 compatibility).
    """

    UNCLASSIFIED = "unclassified"
    PROBE_THEN_CREATE = "probe_then_create"
    IDEMPOTENT_SET_OP = "idempotent_set_op"
    CLIENT_TOKEN_DEDUPE = "client_token_dedupe"
    AT_LEAST_ONCE_ACCEPTED = "at_least_once_accepted"
    NON_IDEMPOTENT_NO_RETRY = "non_idempotent_no_retry"


# Policies that force ``effective_disable_internal_retries`` to True even
# when the caller passed False. These RPCs cannot tolerate the transport's
# inner retry loop because either (a) the caller owns a probe state
# machine that races a blind retry (PROBE_THEN_CREATE), or (b) the write
# has no server-side dedupe key and a retry would create a duplicate
# (NON_IDEMPOTENT_NO_RETRY).
_POLICIES_THAT_FORCE_DISABLE: frozenset[IdempotencyPolicy] = frozenset(
    {
        IdempotencyPolicy.PROBE_THEN_CREATE,
        IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY,
    }
)


# ProbeKeyFn signature: takes the encoded ``params`` list and returns an
# opaque, hashable probe key the caller can use to identify "is this the
# write I issued?" Currently informational — Wave 2 plumbs it into the
# create-probe state machines. ``None`` is the no-probe sentinel.
ProbeKeyFn = Callable[[list[Any]], Any]


@dataclass(frozen=True)
class IdempotencyEntry:
    """One row in :class:`IdempotencyRegistry`.

    Attributes:
        policy: Classification for the ``(RPCMethod, operation_variant)``
            row this entry describes.
        probe_key_fn: Optional probe-key extractor for PROBE_THEN_CREATE
            entries. ``None`` for policies that don't probe. Wave 2 wires
            this into the per-API probe loops.
        client_token_field: For CLIENT_TOKEN_DEDUPE entries, the slot
            in the params payload that receives the auto-injected
            ``uuid4().hex`` token. ``str`` keys are used when the RPC
            params are a dict; ``int`` keys are used to inject into a
            positional slot inside the list-shaped params that the
            batchexecute encoder consumes. ``None`` for other policies.
        notes: Free-form human-readable note. UNCLASSIFIED entries
            registered without an explicit ``notes`` value receive the
            placeholder marker that flags them for Wave 2 classification;
            all other policies default to an empty string.
    """

    policy: IdempotencyPolicy
    probe_key_fn: ProbeKeyFn | None = None
    client_token_field: str | int | None = None
    notes: str = ""


_UNCLASSIFIED_PLACEHOLDER_NOTE = "placeholder — Wave 2 must classify"


class IdempotencyRegistry:
    """Registry of :class:`IdempotencyEntry` keyed by
    ``(RPCMethod, operation_variant | None)``.

    Look-up semantics:

    * ``get_entry(method)`` → returns the ``(method, None)`` entry.
    * ``get_entry(method, operation_variant=v)`` with a variant entry
      present → returns that variant entry.
    * ``get_entry(method, operation_variant=v)`` when ``method`` has
      ONLY a ``(method, None)`` entry (no variant table at all) →
      silently falls back to ``(method, None)``.
    * ``get_entry(method, operation_variant=v)`` when ``method`` has
      explicit variant entries but ``v`` is not among them → raises
      :class:`~notebooklm.exceptions.IdempotencyVariantError`. The
      explicit variant table signals "Wave 2 has classified this method
      by variant" — an unknown variant is almost certainly a caller typo
      or API drift, not safe to mask via silent fallback.

    Thread/loop-safety: the registry is populated at import time and is
    intended to be effectively immutable in production. Tests may
    construct fresh instances. There is no internal lock — concurrent
    writes during a process's lifetime are not supported.
    """

    def __init__(self) -> None:
        # Two-level shape: ``method`` → ``operation_variant | None`` →
        # entry. The inner dict ALWAYS contains a ``None`` key (the
        # default), populated by either :meth:`register` or
        # :meth:`_seed_defaults`.
        self._entries: dict[RPCMethod, dict[str | None, IdempotencyEntry]] = {}

    def register(
        self,
        method: RPCMethod,
        policy: IdempotencyPolicy,
        *,
        variant: str | None = None,
        probe_key_fn: ProbeKeyFn | None = None,
        client_token_field: str | int | None = None,
        notes: str | None = None,
    ) -> None:
        """Register (or overwrite) the entry for ``(method, variant)``.

        Wave 2 will call this once per method/variant at module import.
        Tests may call it ad-hoc on a fresh :class:`IdempotencyRegistry`
        instance to exercise specific policies.

        Effective notes default: when ``policy == UNCLASSIFIED`` and the
        caller did not pass ``notes=...``, the placeholder marker
        ``"placeholder — Wave 2 must classify"`` is used. Any other
        policy defaults to ``""``.
        """
        if notes is None:
            notes = (
                _UNCLASSIFIED_PLACEHOLDER_NOTE if policy is IdempotencyPolicy.UNCLASSIFIED else ""
            )
        entry = IdempotencyEntry(
            policy=policy,
            probe_key_fn=probe_key_fn,
            client_token_field=client_token_field,
            notes=notes,
        )
        self._entries.setdefault(method, {})[variant] = entry

    def get_entry(
        self,
        method: RPCMethod,
        operation_variant: str | None = None,
    ) -> IdempotencyEntry:
        """Return the entry for ``(method, operation_variant)``.

        See class docstring for fallback semantics. Raises
        :class:`~notebooklm.exceptions.IdempotencyVariantError` when an
        unknown non-None variant is requested on a method that has
        explicit variant entries.
        """
        method_entries = self._entries.get(method)
        if method_entries is None:
            # Shouldn't happen with the seeded production registry, but
            # makes the contract explicit for hand-built instances.
            raise KeyError(
                f"IdempotencyRegistry has no entry for {method.name!r}; "
                "missing default (method, None) registration"
            )

        # Variant-specific lookup wins when present.
        if operation_variant is not None:
            variant_entry = method_entries.get(operation_variant)
            if variant_entry is not None:
                return variant_entry
            # Unknown variant on a method that has an explicit variant
            # table is treated as a caller typo / API drift; raise rather
            # than silently fall back to (method, None). Methods that
            # ONLY have a (method, None) entry tolerate any variant
            # name (no typo to catch).
            known = sorted(k for k in method_entries if k is not None)
            if known:
                raise IdempotencyVariantError(
                    f"Unknown operation_variant {operation_variant!r} for "
                    f"{method.name}; known variants: {known}"
                )

        # Fall back to the (method, None) default. Seeding guarantees it
        # exists; raise loudly if a hand-built instance is missing it.
        default = method_entries.get(None)
        if default is None:
            raise KeyError(f"IdempotencyRegistry has no (method, None) default for {method.name!r}")
        return default

    def _seed_defaults(self) -> None:
        """Populate every :class:`~notebooklm.rpc.RPCMethod` with the
        UNCLASSIFIED placeholder at ``variant=None``.

        Called once at module import to guarantee the registry is a
        total function over ``RPCMethod``. Wave 2 will replace these
        placeholders one at a time as it classifies each RPC.
        """
        for method in RPCMethod:
            # ``setdefault`` would lose the placeholder note if a future
            # caller pre-registers a non-default entry. Use explicit
            # absence check so we never overwrite a real Wave 2 entry.
            if method not in self._entries or None not in self._entries[method]:
                self.register(method, IdempotencyPolicy.UNCLASSIFIED)


# Module-level production registry. Wave 2 classifies individual RPCs in
# two passes:
#
#   * Some entries (research/notes from b-research-notes, sources/
#     ADD_SOURCE variants from b-sources) are registered *before* the
#     default-fill seeding pass so ``_seed_defaults`` skips them (it only
#     populates ``(method, None)`` entries that are absent). Variant
#     entries (``variant != None``) sit alongside the ``None`` default;
#     the seeder leaves them alone.
#   * Other entries (delete/refresh/share from b-generation) are
#     registered *after* the seeding pass and overwrite the
#     UNCLASSIFIED placeholders that the seeder populated.
#
# Both orderings yield the same final registry shape; the difference is
# stylistic. Future Wave-2 classifications may use either approach.
IDEMPOTENCY_REGISTRY = IdempotencyRegistry()


# ----------------------------------------------------------------------------
# Wave 2 classifications — P0-3 (b-research-notes)
# ----------------------------------------------------------------------------
#
# Three RPCs in the research + notes family are ``NON_IDEMPOTENT_NO_RETRY``.
# None of them accept a caller-supplied client-token slot, and the
# probe surfaces available to the client (``ResearchAPI.poll`` /
# ``SourcesAPI.list`` / ``GET_NOTES_AND_MIND_MAPS``) cannot reliably
# disambiguate a commit-lost retry from a pre-existing peer resource:
#
# * START_FAST_RESEARCH / START_DEEP_RESEARCH — multiple in-flight
#   research tasks for the same ``(notebook_id, query)`` are valid, so a
#   query-based probe is ambiguous when the user has previously started
#   the same query on the same notebook.
# * IMPORT_RESEARCH — source URLs may already exist in the notebook
#   from prior workflows, so a URL-based probe cannot bind to "the row
#   this specific import committed".
# * CREATE_NOTE — both variants. The plain 5-element variant gets no
#   client-visible ``note_id`` on commit-lost (CREATE_NOTE failed before
#   returning), so a probe against ``GET_NOTES_AND_MIND_MAPS`` cannot
#   bind to the row. The 7-element saved-from-chat variant has a title
#   but the server may apply smart-title generation, breaking
#   title-based probes; chat-answer fingerprints are not unique enough
#   to safely dedupe either.
#
# Caller recourse on failure: poll/list and decide manually (e.g.
# ``client.research.poll(notebook_id)`` after a START_RESEARCH failure
# returns the freshly committed task if the write landed). This mirrors
# the ``sources.add_text(idempotent=True)`` precedent (also
# NON_IDEMPOTENT_NO_RETRY for the same "no reliable dedupe key" reason).

_START_RESEARCH_NOT_IDEMPOTENT_NOTE = (
    "research start: no client-token slot in params and ResearchAPI.poll "
    "keyed by (notebook_id, query) is ambiguous when peer tasks exist with "
    "the same query — surface the first failure and let the caller poll to "
    "decide whether the write landed"
)
_IMPORT_RESEARCH_NOT_IDEMPOTENT_NOTE = (
    "research import: no client-token slot in params; source rows are not "
    "granular per-task on the wire so a post-commit-lost SourcesAPI.list "
    "probe cannot bind URL-matched rows to this specific import batch "
    "(collides with prior workflows that imported the same URLs) — surface "
    "the failure and let the caller list-and-disambiguate"
)
_CREATE_NOTE_NOT_IDEMPOTENT_NOTE = (
    "CREATE_NOTE has no client-token slot and no client-visible note_id on "
    "commit-lost; title-based probes break under server-side smart-title "
    "generation (saved_from_chat variant). Caller must list notes and "
    "disambiguate on failure"
)

IDEMPOTENCY_REGISTRY.register(
    RPCMethod.START_FAST_RESEARCH,
    IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY,
    notes=_START_RESEARCH_NOT_IDEMPOTENT_NOTE,
)
IDEMPOTENCY_REGISTRY.register(
    RPCMethod.START_DEEP_RESEARCH,
    IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY,
    notes=_START_RESEARCH_NOT_IDEMPOTENT_NOTE,
)
IDEMPOTENCY_REGISTRY.register(
    RPCMethod.IMPORT_RESEARCH,
    IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY,
    notes=_IMPORT_RESEARCH_NOT_IDEMPOTENT_NOTE,
)

# CREATE_NOTE has two operation variants on the wire:
#   * ``"plain"`` — 5-element params from ``NoteService.create_note``
#     (default for ``notes.create()`` and mind-map row creation). The
#     ``(CREATE_NOTE, None)`` default mirrors the same policy so callers
#     that omit ``operation_variant`` still get NON_IDEMPOTENT_NO_RETRY.
#   * ``"saved_from_chat"`` — 7-element params from
#     ``_chat_notes.save_chat_answer_as_note`` (issue #660). Used by
#     ``ChatAPI.save_answer_as_note`` (and the deprecated
#     ``NotesAPI.create_from_chat`` forwarder).
# Both variants share the policy; explicit registration documents the
# two distinct param shapes for future-classification work.
IDEMPOTENCY_REGISTRY.register(
    RPCMethod.CREATE_NOTE,
    IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY,
    notes=_CREATE_NOTE_NOT_IDEMPOTENT_NOTE,
)
IDEMPOTENCY_REGISTRY.register(
    RPCMethod.CREATE_NOTE,
    IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY,
    variant="plain",
    notes=_CREATE_NOTE_NOT_IDEMPOTENT_NOTE,
)
IDEMPOTENCY_REGISTRY.register(
    RPCMethod.CREATE_NOTE,
    IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY,
    variant="saved_from_chat",
    notes=_CREATE_NOTE_NOT_IDEMPOTENT_NOTE,
)

# Default-fill every remaining method with the UNCLASSIFIED placeholder.
# Methods classified above are skipped by the absence check inside
# ``_seed_defaults``.
IDEMPOTENCY_REGISTRY._seed_defaults()


# ---------------------------------------------------------------------------
# Wave 2 classifications
# ---------------------------------------------------------------------------
#
# CREATE_ARTIFACT (P0-3) — mutating create. Params are nested positional
# lists shaped like ``[[2], notebook_id, [None, None, type_code,
# source_ids_triple, ..., config]]`` for every artifact variant (audio,
# video, report, quiz, etc.; see ``_artifact_generation.py`` lines 75-99,
# 143-161, 266-291, ...). Every position is structural — there is no
# caller-supplied client-token slot. The server allocates the artifact_id
# in the response (``ArtifactGenerationService.parse_generation_result``
# reads ``result[0][0]`` — see ``_artifact_generation.py``), so a
# CLIENT_TOKEN_DEDUPE classification is impossible.
#
# PROBE_THEN_CREATE forces ``effective_disable_internal_retries=True``,
# which suppresses ``_perform_authed_post``'s inner retry loop. Without
# this, a 5xx between server-side commit and client-side response would
# trigger a naive re-POST and duplicate the artifact (the original P0-3
# audit finding). Callers can layer a list-based probe + retry on top of
# this foundation via ``idempotent_create`` in a follow-up; for B-generation
# the classification alone removes the duplicate-write risk.
IDEMPOTENCY_REGISTRY.register(
    RPCMethod.CREATE_ARTIFACT,
    IdempotencyPolicy.PROBE_THEN_CREATE,
    notes=(
        "P0-3: mutating create with no caller-supplied client-token slot. "
        "Server allocates artifact_id in the response. PROBE_THEN_CREATE "
        "forces the inner retry loop off to prevent duplicate-write on 5xx; "
        "a list-based probe wrapper can be layered via idempotent_create "
        "in a follow-up."
    ),
)

# GENERATE_MIND_MAP (P0-3) — generation RPC with no client-token slot.
# Params are ``[source_ids_nested, None, None, None, None,
# ["interactive_mindmap", [["[CONTEXT]", instructions]], language], None,
# [2, None, [1]]]`` (see ``_artifact_generation.py`` line 595-604). Every
# slot is structural (sources, content config, language, mode triple). The
# response carries the mind-map JSON directly (line 614-622 reads
# ``result[0][0]``) — there is no task_id to probe with after the fact, so
# CLIENT_TOKEN_DEDUPE is impossible here too.
#
# Note: ``GENERATE_MIND_MAP`` itself does NOT persist the note server-side
# (see ``tests/integration/test_mind_map_chain_vcr.py`` header). The actual
# persistence is the subsequent ``CREATE_NOTE`` + ``UPDATE_NOTE`` chain in
# ``NoteService.create_note`` (formerly ``_mind_map.create_note``, removed
# in Phase 6). PROBE_THEN_CREATE here suppresses the inner retry loop on
# the *generation* RPC for two reasons: (a) a blind re-POST wastes the
# expensive LLM inference, and (b) LLM nondeterminism means a retried
# generation may return a *different* mind-map JSON, which would
# silently mismatch what the client saw on the first commit before the
# response was lost. Classifying CREATE_NOTE for the persisted-write side
# of the chain is a separate follow-up (out of scope per the b-generation
# task spec, which restricts edits to ``_artifact_generation.py`` and
# ``_idempotency.py``).
IDEMPOTENCY_REGISTRY.register(
    RPCMethod.GENERATE_MIND_MAP,
    IdempotencyPolicy.PROBE_THEN_CREATE,
    notes=(
        "P0-3: generation RPC with no caller-supplied client-token slot. "
        "Response carries the mind-map JSON directly. PROBE_THEN_CREATE "
        "forces the inner retry loop off so a 5xx after server-side "
        "generation does not trigger a fresh LLM inference whose result "
        "may diverge from the first (lost) response. The persisted-note "
        "side of the mind-map chain (CREATE_NOTE / UPDATE_NOTE in "
        "NoteService.create_note) remains UNCLASSIFIED and is the subject "
        "of a follow-up classification task."
    ),
)


# ----------------------------------------------------------------------------
# Wave 2 classifications (P0-3 side-effects + P1-2 notebooks)
# ----------------------------------------------------------------------------
#
# These entries replace the UNCLASSIFIED placeholders for the five mutating
# RPCs whose side-effect semantics are well-understood and stable. The full
# audit decision matrix lives in ADR-005
# (``docs/adr/0005-idempotency-taxonomy.md``); the short version follows.
#
# DELETE_NOTEBOOK / DELETE_SOURCE / DELETE_ARTIFACT
#   Server-side delete is idempotent: replaying the request after a 5xx /
#   network failure yields the same final state (the resource is gone).
#   Classification: ``IDEMPOTENT_SET_OP``. The transport retry loop keeps
#   running unchanged — today's behavior is preserved, the registry simply
#   documents *why* it is safe.
#
# REFRESH_SOURCE
#   Refresh kicks off a server-side fetch job. A duplicate refresh job is
#   harmless (extra bandwidth, same eventual content) but observable, so
#   the caller has accepted at-least-once semantics. Classification:
#   ``AT_LEAST_ONCE_ACCEPTED``. The transport may retry; the registry
#   emits a rate-limited WARN so operators can see the trade-off when it
#   actually fires.
#
# SHARE_NOTEBOOK
#   Mutates the shared-users / public-access ACL. A blind retry after a
#   network blip can re-send invitation emails (with ``notify=True``) or
#   flip access between RESTRICTED / ANYONE-WITH-LINK twice. The codebase
#   does expose a server-side probe RPC (``GET_SHARE_STATUS``) that can
#   list the current ACL, so the *correct* policy is ``PROBE_THEN_CREATE``
#   — the transport must NOT retry blindly, and a future wrapper can
#   ``get_status()`` to decide whether the prior call landed before
#   re-issuing. Wave-2 scope is the classification (which suppresses the
#   blind retry today); the caller-side probe-then-create wrapper is a
#   follow-up.
IDEMPOTENCY_REGISTRY.register(
    RPCMethod.DELETE_NOTEBOOK,
    IdempotencyPolicy.IDEMPOTENT_SET_OP,
    notes="server-side delete is idempotent (set-op semantics)",
)
IDEMPOTENCY_REGISTRY.register(
    RPCMethod.DELETE_SOURCE,
    IdempotencyPolicy.IDEMPOTENT_SET_OP,
    notes="server-side delete is idempotent (set-op semantics)",
)
IDEMPOTENCY_REGISTRY.register(
    RPCMethod.DELETE_ARTIFACT,
    IdempotencyPolicy.IDEMPOTENT_SET_OP,
    notes="server-side delete is idempotent (set-op semantics)",
)
IDEMPOTENCY_REGISTRY.register(
    RPCMethod.REFRESH_SOURCE,
    IdempotencyPolicy.AT_LEAST_ONCE_ACCEPTED,
    notes="duplicate refresh jobs are acceptable cost (extra fetch, same content)",
)
IDEMPOTENCY_REGISTRY.register(
    RPCMethod.SHARE_NOTEBOOK,
    IdempotencyPolicy.PROBE_THEN_CREATE,
    notes=(
        "mutates ACL; blind retry can re-send invite emails or double-flip access. "
        "GET_SHARE_STATUS exposes the server-side ACL for a future probe-then-create "
        "wrapper; today's classification suppresses the inner retry loop."
    ),
)


# ----------------------------------------------------------------------------
# Wave 2 classifications — ADD_SOURCE + ADD_SOURCE_FILE (P0-3, P1-2)
# ----------------------------------------------------------------------------
#
# ADD_SOURCE is variant-shaped: the call site distinguishes ``"url"`` (web /
# YouTube), ``"drive"`` (Google Drive document), and ``"text"`` (pasted
# content). Each variant has a different retry-safety profile because the
# server-side dedupe key differs:
#
# * ``"url"`` — probe by ``source.url == url`` on a notebook list. The probe
#   is a single GET_NOTEBOOK; the wrapper retries the create once if the
#   probe finds nothing. PROBE_THEN_CREATE.
# * ``"drive"`` — probe by ``file_id in source.url`` (Drive URLs embed the
#   file_id). Same wrapper as ``"url"``. PROBE_THEN_CREATE.
# * ``"text"`` — no reliable dedupe key (titles non-unique, body not
#   exposed in the source list). NON_IDEMPOTENT_NO_RETRY: force-disable the
#   inner transport retries and let the first failure surface so the caller
#   can decide. See the ``add_text`` rationale in
#   ``tests/integration/concurrency/test_idempotency_create.py:17-19``.
#
# ADD_SOURCE_FILE is single-shape: it registers a file source by name.
# Filenames are NOT identity-bearing (two uploads of ``report.pdf`` are
# legitimately two distinct sources), so the per-API wrapper captures a
# baseline of source IDs *before* the create attempt and filters probe
# matches to "new since the create started" sources only. Ambiguous
# matches (>1 new source with the same filename) raise rather than guess.
# PROBE_THEN_CREATE.
#
# These four entries flip the executor onto the probe-then-create path
# via ``resolve_effective_disable_internal_retries`` — the per-API call
# sites in ``_source_add.py`` / ``_source_upload.py`` own the probe loop.

IDEMPOTENCY_REGISTRY.register(
    RPCMethod.ADD_SOURCE,
    IdempotencyPolicy.PROBE_THEN_CREATE,
    variant="url",
    notes="probe by source.url == url on notebook list (web + YouTube)",
)
IDEMPOTENCY_REGISTRY.register(
    RPCMethod.ADD_SOURCE,
    IdempotencyPolicy.PROBE_THEN_CREATE,
    variant="drive",
    notes="probe by /d/<file_id> URL segment marker on notebook list",
)
IDEMPOTENCY_REGISTRY.register(
    RPCMethod.ADD_SOURCE,
    IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY,
    variant="text",
    notes="no reliable dedupe key — titles non-unique, body not exposed",
)
IDEMPOTENCY_REGISTRY.register(
    RPCMethod.ADD_SOURCE_FILE,
    IdempotencyPolicy.PROBE_THEN_CREATE,
    notes=(
        "baseline-diff probe by source.title == filename — filenames are not "
        "identity-bearing, so the wrapper captures source-id baseline before "
        "the create and filters probe matches to new sources only"
    ),
)


# ----------------------------------------------------------------------------
# AT_LEAST_ONCE_ACCEPTED rate-limited WARN logger
# ----------------------------------------------------------------------------
#
# Per-method timestamp ledger so the WARN log fires at most once per
# ``_AT_LEAST_ONCE_LOG_INTERVAL`` seconds per ``(method, variant)``. This
# keeps the foundation behavior-neutral under load: even if Wave 2
# classifies several hot-path RPCs as AT_LEAST_ONCE_ACCEPTED, callers
# won't drown in WARN spam. The choice of 30s mirrors the cadence of
# similar advisory-log throttles elsewhere in the codebase.
_AT_LEAST_ONCE_LOG_INTERVAL: float = 30.0
_at_least_once_last_logged: dict[tuple[RPCMethod, str | None], float] = {}


def _maybe_log_at_least_once(method: RPCMethod, variant: str | None) -> None:
    """Emit a rate-limited WARN that this RPC is AT_LEAST_ONCE_ACCEPTED.

    Per-key throttle: at most one WARN per
    ``_AT_LEAST_ONCE_LOG_INTERVAL`` seconds per ``(method, variant)``.
    The first call always emits; subsequent calls inside the window are
    silent. Tests rely on this to assert that 100 calls produce ≤2 lines.
    """
    key = (method, variant)
    now = time.monotonic()
    last = _at_least_once_last_logged.get(key)
    if last is not None and (now - last) < _AT_LEAST_ONCE_LOG_INTERVAL:
        return
    _at_least_once_last_logged[key] = now
    logger.warning(
        "RPC %s%s classified AT_LEAST_ONCE_ACCEPTED — transport retries "
        "may cause duplicate server-side commits; caller has opted in",
        method.name,
        f" (variant={variant!r})" if variant is not None else "",
    )


def resolve_effective_disable_internal_retries(
    registry: IdempotencyRegistry,
    method: RPCMethod,
    *,
    caller_disable_internal_retries: bool,
    operation_variant: str | None,
) -> bool:
    """Resolve the effective ``disable_internal_retries`` flag for an RPC.

    Precedence (caller wins):

    1. ``caller_disable_internal_retries=True`` → returns True
       regardless of policy. Explicit caller intent dominates registry
       classification.
    2. Policy is :attr:`IdempotencyPolicy.PROBE_THEN_CREATE` or
       :attr:`IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY` → returns True.
       These RPCs cannot tolerate the inner retry loop.
    3. Policy is :attr:`IdempotencyPolicy.AT_LEAST_ONCE_ACCEPTED` →
       emits a rate-limited WARN and returns ``caller_disable_internal_retries``
       unchanged. Caller has accepted at-least-once semantics; retries
       remain enabled.
    4. All other policies (UNCLASSIFIED, IDEMPOTENT_SET_OP,
       CLIENT_TOKEN_DEDUPE) → returns ``caller_disable_internal_retries``
       unchanged. UNCLASSIFIED is silent (no log emission).

    Raises :class:`~notebooklm.exceptions.IdempotencyVariantError` for
    unknown variants on methods with explicit variant tables.
    """
    if caller_disable_internal_retries:
        return True

    entry = registry.get_entry(method, operation_variant=operation_variant)
    policy = entry.policy

    if policy in _POLICIES_THAT_FORCE_DISABLE:
        return True

    if policy is IdempotencyPolicy.AT_LEAST_ONCE_ACCEPTED:
        _maybe_log_at_least_once(method, operation_variant)
        return caller_disable_internal_retries

    # UNCLASSIFIED / IDEMPOTENT_SET_OP / CLIENT_TOKEN_DEDUPE: silent,
    # caller value passes through unchanged.
    return caller_disable_internal_retries


def maybe_inject_client_token(
    registry: IdempotencyRegistry,
    method: RPCMethod,
    params: Any,
    *,
    operation_variant: str | None,
) -> None:
    """Inject a fresh ``uuid4().hex`` client-token for CLIENT_TOKEN_DEDUPE
    methods, when (and only when) the caller did not already populate the
    token slot.

    ``params`` is mutated in place. Two shapes are supported, matching
    the two shapes that ``RpcExecutor.execute`` is asked to encode:

    * ``dict``-shaped params with a ``str`` ``client_token_field`` key:
      ``params[field_name] = uuid4().hex`` if the key is absent or maps
      to a falsy value.
    * ``list``-shaped params (the batchexecute-typical shape) with an
      ``int`` ``client_token_field`` index: ``params[index] = uuid4().hex``
      when ``0 <= index < len(params)`` and the existing slot is falsy
      (``None``, empty string). If the index is out of range the
      function logs a warning and returns without raising — this is a
      foundation safety guard so a mis-registered entry doesn't crash a
      live RPC; Wave 2 owns the per-method registration audit.

    No-op for policies other than ``CLIENT_TOKEN_DEDUPE``, for entries
    without a ``client_token_field``, for entries where the slot already
    holds a non-falsy value (caller-provided token wins), and for params
    whose shape doesn't match the field type (``int`` field on a non-list
    or ``str`` field on a non-dict). Raises
    :class:`~notebooklm.exceptions.IdempotencyVariantError` for unknown
    variants on methods with explicit variant tables.
    """
    entry = registry.get_entry(method, operation_variant=operation_variant)
    if entry.policy is not IdempotencyPolicy.CLIENT_TOKEN_DEDUPE:
        return
    field_key = entry.client_token_field
    if field_key is None:
        return

    if isinstance(field_key, str):
        if not isinstance(params, dict):
            # Shape mismatch — registry registered a dict-style field
            # but caller passed a list. No-op rather than crash.
            return
        if params.get(field_key):
            # Caller-provided token wins.
            return
        params[field_key] = uuid.uuid4().hex
        return

    # Positional injection into list params (batchexecute typical shape).
    if not isinstance(params, list):
        # Shape mismatch — registry registered a positional slot but
        # caller passed a dict / scalar. No-op.
        return
    if not (0 <= field_key < len(params)):
        # Out-of-range index — likely a Wave 2 mis-registration. Don't
        # crash a live RPC; log once and let the caller surface it via
        # logs rather than via exception.
        logger.warning(
            "CLIENT_TOKEN_DEDUPE for RPC %s has out-of-range "
            "client_token_field=%d for params of length %d; skipping injection",
            method.name,
            field_key,
            len(params),
        )
        return
    if params[field_key]:
        # Caller-provided token (or other truthy value) wins.
        return
    params[field_key] = uuid.uuid4().hex


__all__ = [
    "idempotent_create",
    "IdempotencyPolicy",
    "IdempotencyEntry",
    "IdempotencyRegistry",
    "IDEMPOTENCY_REGISTRY",
    "ProbeKeyFn",
    "resolve_effective_disable_internal_retries",
    "maybe_inject_client_token",
]
