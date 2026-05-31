"""Idempotency layer for mutating-RPC patterns.

This module hosts two cooperating pieces:

1. :func:`idempotent_create` — the existing per-API probe-then-retry
   wrapper for create-RPC patterns. A create RPC like
   ``NotebooksAPI.create`` or ``SourcesAPI.add_url`` is a mutating POST:
   the *server may have committed the write* even if the client sees a
   5xx or network error. Naive retries duplicate the resource; the
   wrapper inverts the direction: run with internal-retries disabled,
   then probe for a server-side commit before re-issuing.

2. :class:`IdempotencyRegistry` — the 5-policy classification layer that
   :class:`~notebooklm._rpc_executor.RpcExecutor` consults to compute the
   *effective* ``disable_internal_retries`` value. The registry is a
   single source of truth for every ``RPCMethod`` without touching the
   executor.

   The production registry is complete: every active ``RPCMethod`` has
   an explicit default classification, with variant rows for wire shapes
   like ``ADD_SOURCE`` and ``CREATE_NOTE`` where retry safety differs by
   call site. ``UNCLASSIFIED`` remains available only as a hand-built
   registry placeholder for tests and future development.

Per-API probes used by :func:`idempotent_create` are caller-supplied
because there is no universal probe key (notebooks: title +
baseline-diff; sources: url-match; ``add_text``: no probe possible — see
:class:`~notebooklm.exceptions.NonIdempotentRetryError`).

This module is private (``_idempotency.py``); call sites live in the
domain APIs (``_notebooks.py``, ``_sources.py``) and the RPC executor
(``_rpc_executor.py``). The canonical home for the taxonomy itself and
the per-RPC classification rationale is ADR-005
(``docs/adr/0005-idempotency-taxonomy.md``).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable, Iterator
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
# RPC idempotency registry
# ============================================================================
#
# The registry is the single source of truth for "how should this RPC behave
# under retry?" It is consulted by ``RpcExecutor`` to compute the *effective*
# ``disable_internal_retries`` value before request encoding.
#
# IMPORTANT — complete production registry:
#   The module-level registry seeds missing methods with UNCLASSIFIED only as a
#   future-drift sentinel, then overwrites every current ``RPCMethod`` with an
#   explicit policy below. Unit tests fail if a new enum member keeps the
#   placeholder.


class IdempotencyPolicy(str, Enum):
    """Classification axis for mutating-RPC retry safety.

    Five policies — no more, no fewer. The axis was sized to cover all
    realistic NotebookLM RPC shapes without inventing per-method special
    cases. See ADR-005 (``docs/adr/0005-idempotency-taxonomy.md``) for
    the derivation and the per-policy rationale.

    Policies fall into three retry-safety bands:

    * **Safe to retry inside the transport**:
      :attr:`UNCLASSIFIED` (placeholder — preserves today's retries),
      :attr:`IDEMPOTENT_SET_OP` (read-only, rename / delete / set-state
      operations where replay leaves the same server state),
      :attr:`AT_LEAST_ONCE_ACCEPTED` (caller has accepted at-least-once
      semantics; WARN logged).

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
# write I issued?" Currently informational; future probe-loop work may plumb it
# into create-probe state machines. ``None`` is the no-probe sentinel.
ProbeKeyFn = Callable[[list[Any]], Any]


@dataclass(frozen=True)
class IdempotencyEntry:
    """One row in :class:`IdempotencyRegistry`.

    Attributes:
        policy: Classification for the ``(RPCMethod, operation_variant)``
            row this entry describes.
        probe_key_fn: Optional probe-key extractor for PROBE_THEN_CREATE
            entries. ``None`` for policies that don't probe. Future work may
            wire this into the per-API probe loops.
        notes: Free-form human-readable note. UNCLASSIFIED entries
            registered without an explicit ``notes`` value receive the
            placeholder marker that flags them for explicit classification;
            all other policies default to an empty string.
    """

    policy: IdempotencyPolicy
    probe_key_fn: ProbeKeyFn | None = None
    notes: str = ""


_UNCLASSIFIED_PLACEHOLDER_NOTE = "placeholder — must classify"


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
      explicit variant table signals "this method is classified by variant" —
      an unknown variant is almost certainly a caller typo or API drift, not
      safe to mask via silent fallback.

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
        notes: str | None = None,
    ) -> None:
        """Register (or overwrite) the entry for ``(method, variant)``.

        Production code calls this once per method/variant at module import.
        Tests may call it ad-hoc on a fresh :class:`IdempotencyRegistry`
        instance to exercise specific policies.

        Effective notes default: when ``policy == UNCLASSIFIED`` and the
        caller did not pass ``notes=...``, the placeholder marker
        ``"placeholder — must classify"`` is used. Any other
        policy defaults to ``""``.
        """
        if notes is None:
            notes = (
                _UNCLASSIFIED_PLACEHOLDER_NOTE if policy is IdempotencyPolicy.UNCLASSIFIED else ""
            )
        entry = IdempotencyEntry(
            policy=policy,
            probe_key_fn=probe_key_fn,
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

    def iter_entries(self) -> Iterator[tuple[RPCMethod, str | None, IdempotencyEntry]]:
        """Return an iterator over a snapshot of ``(method, variant, entry)`` rows."""
        snapshot: list[tuple[RPCMethod, str | None, IdempotencyEntry]] = []
        for method, method_entries in self._entries.items():
            for variant, entry in method_entries.items():
                snapshot.append((method, variant, entry))
        return iter(snapshot)

    def _seed_defaults(self) -> None:
        """Populate missing :class:`~notebooklm.rpc.RPCMethod` defaults with
        the UNCLASSIFIED placeholder.

        Called once at module import to guarantee the registry is a total
        function over ``RPCMethod``. The production registrations below
        replace every current placeholder; guard tests fail if future enum
        members are added without an explicit classification.
        """
        for method in RPCMethod:
            # ``setdefault`` would lose the placeholder note if a future caller
            # pre-registers a non-default entry. Use explicit absence check so
            # we never overwrite a real classification.
            if method not in self._entries or None not in self._entries[method]:
                self.register(method, IdempotencyPolicy.UNCLASSIFIED)


# Module-level production registry. Classifications are registered in two
# passes:
#
#   * Some entries are registered *before* the default-fill seeding pass so
#     ``_seed_defaults`` skips them. Variant entries (``variant != None``) sit
#     alongside the ``None`` default; the seeder leaves them alone.
#   * The remaining entries are registered *after* the seeding pass and
#     overwrite any UNCLASSIFIED placeholders that the seeder populated.
#
# Both orderings yield the same final registry shape; the difference is
# stylistic. Future classifications may use either approach.
IDEMPOTENCY_REGISTRY = IdempotencyRegistry()


# ----------------------------------------------------------------------------
# Active classifications — research and notes
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
#     ``ChatAPI.save_answer_as_note``.
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

# Default-fill every remaining method with an UNCLASSIFIED placeholder. The
# explicit registrations below must replace every placeholder before tests pass.
# Methods classified above are skipped by the absence check inside
# ``_seed_defaults``.
IDEMPOTENCY_REGISTRY._seed_defaults()


# ---------------------------------------------------------------------------
# Active classifications — artifact and generation create patterns
# ---------------------------------------------------------------------------
#
# CREATE_ARTIFACT (P0-3) — mutating create. Params are nested positional
# lists shaped like ``[[2], notebook_id, [None, None, type_code,
# source_ids_triple, ..., config]]`` for every artifact variant (audio,
# video, report, quiz, etc.; see the ``generate_*`` methods and the
# ``_artifact_payloads.build_*`` helpers in ``_artifacts.py``). Every
# position is structural — there is no caller-supplied client-token slot.
# The server allocates the artifact_id in the response
# (``ArtifactsAPI._parse_generation_result`` reads ``result[0][0]`` — see
# ``_artifacts.py``), so a token-dedupe strategy is impossible.
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
# [2, None, [1]]]`` (see ``ArtifactsAPI.generate_mind_map`` in
# ``_artifacts.py`` and ``_artifact_payloads.build_mind_map_params``).
# Every slot is structural (sources, content config, language, mode
# triple). The response carries the mind-map JSON directly
# (``generate_mind_map`` reads ``result[0][0]``) — there is no task_id to
# probe with after the fact, so token-dedupe is impossible here too.
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
# task spec, which restricted edits to the artifact-generation path —
# now folded into ``_artifacts.py`` — and ``_idempotency.py``).
IDEMPOTENCY_REGISTRY.register(
    RPCMethod.GENERATE_MIND_MAP,
    IdempotencyPolicy.PROBE_THEN_CREATE,
    notes=(
        "P0-3: generation RPC with no caller-supplied client-token slot. "
        "Response carries the mind-map JSON directly. PROBE_THEN_CREATE "
        "forces the inner retry loop off so a 5xx after server-side "
        "generation does not trigger a fresh LLM inference whose result "
        "may diverge from the first (lost) response. The persisted-note "
        "side of the mind-map chain is classified separately: CREATE_NOTE "
        "is NON_IDEMPOTENT_NO_RETRY and UPDATE_NOTE is an idempotent set op."
    ),
)


# ----------------------------------------------------------------------------
# Active classifications — side effects and notebooks
# ----------------------------------------------------------------------------
#
# These entries replace the UNCLASSIFIED placeholders for mutating RPCs whose
# side-effect semantics are well-understood and stable. The full
# audit decision matrix lives in ADR-005
# (``docs/adr/0005-idempotency-taxonomy.md``); the short version follows.
#
# CREATE_NOTEBOOK
#   Mutating create with an executable wrapper in ``NotebooksAPI.create``:
#   the caller captures a title/baseline probe before issuing the RPC and
#   retries only after probing for a committed notebook. Classification:
#   ``PROBE_THEN_CREATE`` so raw ``rpc_call(CREATE_NOTEBOOK, ...)`` disables
#   blind transport retries too.
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
    RPCMethod.CREATE_NOTEBOOK,
    IdempotencyPolicy.PROBE_THEN_CREATE,
    notes=(
        "notebook create has an executable title/baseline probe wrapper in "
        "NotebooksAPI.create; raw rpc_call paths must also suppress blind "
        "transport retries to avoid duplicate notebooks on commit-lost errors"
    ),
)
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
# Active classifications — ADD_SOURCE + ADD_SOURCE_FILE
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
# These entries force-disable blind transport retries via
# ``resolve_effective_disable_internal_retries``. The per-API call sites in
# ``_source_add.py`` / ``_source_upload.py`` own the executable probe loop for
# the URL, Drive, and file variants.

_RAW_ADD_SOURCE_NOT_IDEMPOTENT_NOTE = (
    "raw ADD_SOURCE without an operation_variant has no proven dedupe/probe "
    "key. Public call sites must pass 'url', 'drive', or 'text'; direct "
    "rpc_call users get first-failure surfacing rather than blind retry"
)

IDEMPOTENCY_REGISTRY.register(
    RPCMethod.ADD_SOURCE,
    IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY,
    notes=_RAW_ADD_SOURCE_NOT_IDEMPOTENT_NOTE,
)
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
# Complete coverage — read-only / idempotent set-state RPCs
# ----------------------------------------------------------------------------
#
# ``IDEMPOTENT_SET_OP`` is the retry-safe bucket for operations where replay
# cannot create an additional server resource. This includes side-effect-free
# reads and "set this state to X" mutations; both preserve the public retry
# default because transport retries remain enabled.

_IDEMPOTENT_READ_OR_SET_OP_NOTES: dict[RPCMethod, str] = {
    RPCMethod.LIST_NOTEBOOKS: "read-only list; replay does not mutate notebook state",
    RPCMethod.GET_NOTEBOOK: "read-only notebook fetch; replay does not mutate notebook state",
    RPCMethod.RENAME_NOTEBOOK: (
        "set notebook title/settings to caller-supplied values; replay leaves the same state"
    ),
    RPCMethod.GET_SOURCE: "read-only source content fetch; replay does not mutate source state",
    RPCMethod.CHECK_SOURCE_FRESHNESS: (
        "read-only freshness check; replay does not start a refresh job"
    ),
    RPCMethod.UPDATE_SOURCE: (
        "set source metadata/title to caller-supplied values; replay leaves the same state"
    ),
    RPCMethod.SUMMARIZE: (
        "response-only notebook summary generation; no persisted resource is created by replay"
    ),
    RPCMethod.GET_SOURCE_GUIDE: (
        "response-only source guide fetch/generation; no persisted resource is created by replay"
    ),
    RPCMethod.GET_SUGGESTED_REPORTS: (
        "response-only report suggestion generation; no persisted resource is created by replay"
    ),
    RPCMethod.LIST_ARTIFACTS: "read-only artifact list; replay does not mutate artifact state",
    RPCMethod.RENAME_ARTIFACT: (
        "set artifact title to a caller-supplied value; replay leaves the same state"
    ),
    RPCMethod.SHARE_ARTIFACT: (
        "legacy public share-link state update; replay leaves the same share state"
    ),
    RPCMethod.GET_INTERACTIVE_HTML: (
        "read-only artifact HTML fetch; replay does not mutate artifact state"
    ),
    RPCMethod.POLL_RESEARCH: "read-only research task poll; replay does not start a task",
    RPCMethod.GET_NOTES_AND_MIND_MAPS: (
        "read-only notes/mind-maps list; replay does not mutate note state"
    ),
    RPCMethod.UPDATE_NOTE: (
        "set note content/title to caller-supplied values; replay leaves the same state"
    ),
    RPCMethod.DELETE_NOTE: "server-side note delete is idempotent (set-op semantics)",
    RPCMethod.GET_LAST_CONVERSATION_ID: (
        "read-only conversation id fetch; replay does not mutate chat state"
    ),
    RPCMethod.GET_CONVERSATION_TURNS: (
        "read-only conversation history fetch; replay does not mutate chat state"
    ),
    RPCMethod.DELETE_CONVERSATION: (
        "server-side conversation delete is idempotent (set-op semantics)"
    ),
    RPCMethod.GET_SHARE_STATUS: "read-only share status fetch; replay does not mutate ACL state",
    RPCMethod.REMOVE_RECENTLY_VIEWED: (
        "remove notebook from recents is idempotent; replay leaves it absent"
    ),
    RPCMethod.GET_USER_SETTINGS: "read-only settings fetch; replay does not mutate settings",
    RPCMethod.SET_USER_SETTINGS: (
        "set user settings to caller-supplied values; replay leaves the same state"
    ),
    RPCMethod.GET_USER_TIER: "read-only account tier fetch; replay does not mutate account state",
}

for _method, _notes in _IDEMPOTENT_READ_OR_SET_OP_NOTES.items():
    IDEMPOTENCY_REGISTRY.register(
        _method,
        IdempotencyPolicy.IDEMPOTENT_SET_OP,
        notes=_notes,
    )


# ----------------------------------------------------------------------------
# Complete coverage — non-idempotent methods with no reliable probe/token
# ----------------------------------------------------------------------------

IDEMPOTENCY_REGISTRY.register(
    RPCMethod.EXPORT_ARTIFACT,
    IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY,
    notes=(
        "exports create an external Docs/Sheets artifact and return its URL; "
        "there is no client-token slot or reliable post-failure probe to bind "
        "a commit-lost export to this call"
    ),
)
IDEMPOTENCY_REGISTRY.register(
    RPCMethod.REVISE_SLIDE,
    IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY,
    notes=(
        "slide revision starts a prompt-driven generation/update with no "
        "client-token slot or probe; a blind retry may create a second, "
        "divergent revision"
    ),
)


# ----------------------------------------------------------------------------
# AT_LEAST_ONCE_ACCEPTED rate-limited WARN logger
# ----------------------------------------------------------------------------
#
# Per-method timestamp ledger so the WARN log fires at most once per
# ``_AT_LEAST_ONCE_LOG_INTERVAL`` seconds per ``(method, variant)``. This
# keeps the registry behavior manageable under load: even if several hot-path
# RPCs are AT_LEAST_ONCE_ACCEPTED, callers won't drown in WARN spam. The choice
# of 30s mirrors the cadence of similar advisory-log throttles elsewhere in the
# codebase.
_AT_LEAST_ONCE_LOG_INTERVAL: float = 30.0
# Audit CC6: single-loop-per-client invariant per ADR-004; not safe for multi-loop fan-out.
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
    4. All other policies (UNCLASSIFIED, IDEMPOTENT_SET_OP) → returns
       ``caller_disable_internal_retries`` unchanged. UNCLASSIFIED is
       silent (no log emission) and should appear only in hand-built
       test registries, not in the production registry.

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

    # UNCLASSIFIED / IDEMPOTENT_SET_OP: silent, caller value passes
    # through unchanged.
    return caller_disable_internal_retries


__all__ = [
    "idempotent_create",
    "IdempotencyPolicy",
    "IdempotencyEntry",
    "IdempotencyRegistry",
    "IDEMPOTENCY_REGISTRY",
    "ProbeKeyFn",
    "resolve_effective_disable_internal_retries",
]
