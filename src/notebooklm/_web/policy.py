"""Web RPC idempotency registry, resolution policy, and classifications."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from enum import Enum
from typing import Any

from ..exceptions import IdempotencyVariantError
from ..rpc.types import RPCMethod

logger = logging.getLogger("notebooklm._idempotency")


class IdempotencyPolicy(str, Enum):
    """Classification axis for mutating-RPC retry safety.

    Five policies — no more, no fewer. The axis was sized to cover all
    realistic NotebookLM RPC shapes without inventing per-method special
    cases. See ADR-0005 (``docs/adr/0005-idempotency-taxonomy.md``) for
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
# Single-loop-per-client invariant per ADR-0004; not safe for multi-loop fan-out.
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


def register_default_policies(registry: IdempotencyRegistry) -> None:
    """Register every production idempotency classification on ``registry``.

    This is the declarative classification data extracted from the former
    root policy modules (issue #1331). It is applied to the module-level
    ``IDEMPOTENCY_REGISTRY`` singleton once at this module's import time. The
    two-pass shape is load-bearing: some
    ``register`` calls run *before* :meth:`IdempotencyRegistry._seed_defaults`
    (so the seeder skips them), the seeder then fills ``UNCLASSIFIED`` for
    every remaining method, and the rest register *after* the seed (replacing
    the placeholders). See ADR-0005 for the taxonomy rationale.
    """
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

    registry.register(
        RPCMethod.DISCOVER_SOURCES,
        IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY,
        notes=(
            "synchronous discovery also records a completed job server-side "
            "(#2283); a blind retry after a lost commit would run a second, "
            "quota-bearing search and leave two jobs. Caller decides"
        ),
    )
    registry.register(
        RPCMethod.START_FAST_RESEARCH,
        IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY,
        notes=_START_RESEARCH_NOT_IDEMPOTENT_NOTE,
    )
    registry.register(
        RPCMethod.START_DEEP_RESEARCH,
        IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY,
        notes=_START_RESEARCH_NOT_IDEMPOTENT_NOTE,
    )
    registry.register(
        RPCMethod.IMPORT_RESEARCH,
        IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY,
        notes=_IMPORT_RESEARCH_NOT_IDEMPOTENT_NOTE,
    )
    # CANCEL_RESEARCH drives an in-flight run to its terminal (FAILED) state. The
    # server returns [] unconditionally and never validates the run id, so a
    # blind transport retry is safe: re-cancelling a now-terminal (or unknown)
    # run is a no-op with the same observable result. Retry-safe set-op
    # semantics, like DELETE_SOURCE / DELETE_ARTIFACT.
    registry.register(
        RPCMethod.CANCEL_RESEARCH,
        IdempotencyPolicy.IDEMPOTENT_SET_OP,
        notes=(
            "research run cancel is idempotent — final-state set-op; server "
            "returns [] unconditionally (no id validation), so a retry is a no-op"
        ),
    )

    # CREATE_NOTE has two operation variants on the wire:
    #   * ``"plain"`` — 5-element params from ``NoteService.create_note``
    #     (default for ``notes.create()`` and mind-map row creation). The
    #     ``(CREATE_NOTE, None)`` default mirrors the same policy so callers
    #     that omit ``operation_variant`` still get NON_IDEMPOTENT_NO_RETRY.
    #   * ``"saved_from_chat"`` — 7-element params from
    #     ``_web.chat.save_chat_answer_as_note`` (issue #660). Used by
    #     ``ChatAPI.save_answer_as_note``.
    # Both variants share the policy; explicit registration documents the
    # two distinct param shapes for future-classification work.
    registry.register(
        RPCMethod.CREATE_NOTE,
        IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY,
        notes=_CREATE_NOTE_NOT_IDEMPOTENT_NOTE,
    )
    registry.register(
        RPCMethod.CREATE_NOTE,
        IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY,
        variant="plain",
        notes=_CREATE_NOTE_NOT_IDEMPOTENT_NOTE,
    )
    registry.register(
        RPCMethod.CREATE_NOTE,
        IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY,
        variant="saved_from_chat",
        notes=_CREATE_NOTE_NOT_IDEMPOTENT_NOTE,
    )
    registry.register(
        RPCMethod.COPY_NOTEBOOK,
        IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY,
        notes=(
            "CopyProject has no caller-provided idempotency token and creates a "
            "new notebook with duplicated children; a blind retry after a lost "
            "response can create a second full copy"
        ),
    )
    # The #2283 transfer family: every write below creates or extends rows with
    # no client-token slot and no post-failure probe that can bind a lost
    # response to this call, so a blind transport retry duplicates the work.
    registry.register(
        RPCMethod.ADD_SOURCES_ASYNC,
        IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY,
        notes=(
            "AddSourcesAsync queues new source rows for every URL in the batch; "
            "the reply is the only proof of which rows were created, so a retry "
            "after a lost response would enqueue a duplicate set"
        ),
    )
    registry.register(
        RPCMethod.ADD_SOURCES_ASYNC,
        IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY,
        variant="play_book",
        notes=(
            "the Play Books add (sources.add_play_book) enqueues a new "
            "ExpertIntelligenceContent source row with no client-token slot and "
            "no post-failure probe, so a blind retry after a lost response would "
            "add the book a second time (#2292)"
        ),
    )
    registry.register(
        RPCMethod.APPEND_SOURCE,
        IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY,
        notes=(
            "AppendSource appends a text block to the source fulltext in place "
            "(live-verified: the block lands at the very end); a replay appends "
            "the same block a second time"
        ),
    )
    registry.register(
        RPCMethod.COPY_SOURCES,
        IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY,
        notes=(
            "CopySourcesAsync creates new source rows in the target notebook and "
            "maps each original to its copy; a retry after a lost response "
            "creates a second set of copies"
        ),
    )
    registry.register(
        RPCMethod.COPY_ARTIFACTS,
        IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY,
        notes=(
            "CopyArtifactsAsync creates new artifact rows in the target notebook "
            "(live-verified by re-listing the target); a retry after a lost "
            "response creates a second set of copies"
        ),
    )

    # Default-fill every remaining method with an UNCLASSIFIED placeholder. The
    # explicit registrations below must replace every placeholder before tests pass.
    # Methods classified above are skipped by the absence check inside
    # ``_seed_defaults``.
    registry._seed_defaults()

    # ---------------------------------------------------------------------------
    # Active classifications — artifact and generation create patterns
    # ---------------------------------------------------------------------------
    #
    # CREATE_ARTIFACT — mutating create. Params are nested positional lists
    # shaped like ``[client_options, notebook_id, [None, None, type_code,
    # source_ids_triple, ..., config]]`` for every artifact variant (audio,
    # video, report, quiz, etc.; see the ``generate_*`` methods and the
    # ``_web.params.artifacts.build_*`` helpers). Every position is structural —
    # there is no caller-supplied client-token slot. The server allocates the
    # artifact_id in the response (``ArtifactGenerationService`` decodes the
    # result in ``_web/artifact/generation.py``), so a token-dedupe strategy is
    # impossible.
    #
    # PROBE_THEN_CREATE forces ``effective_disable_internal_retries=True``,
    # which suppresses the retry middleware inside
    # ``RuntimeTransport.perform_authed_post``. Without
    # this, a 5xx between server-side commit and client-side response would
    # trigger a naive re-POST and duplicate the artifact (the original
    # audit finding). Callers can layer a list-based probe + retry on top of
    # this foundation via ``idempotent_create`` in a follow-up; for B-generation
    # the classification alone removes the duplicate-write risk.
    registry.register(
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

    # GENERATE_MIND_MAP (live method ``ActOnSources``, a generic source-action
    # op we drive to generate a mind map) — generation RPC with no client-token
    # slot.
    # Params are ``[source_ids_nested, None, None, None, None,
    # ["interactive_mindmap", [["[CONTEXT]", instructions]], language], None,
    # [2, None, [1]]]`` (see ``ArtifactGenerationService.generate_mind_map`` in
    # ``_web/artifact/generation.py`` and
    # ``_web.params.artifacts.build_mind_map_params``).
    # Every slot is structural (sources, content config, language, mode
    # triple). The response carries the mind-map JSON directly
    # (``generate_mind_map`` reads ``result[0][0]``) — there is no task_id to
    # probe with after the fact, so token-dedupe is impossible here too.
    #
    # Note: ``GENERATE_MIND_MAP`` itself does NOT persist the note server-side
    # (see ``tests/integration/test_mind_map_chain_vcr.py`` header). The actual
    # persistence is the subsequent ``CREATE_NOTE`` + ``UPDATE_NOTE`` chain in
    # ``NoteService.create_note``. PROBE_THEN_CREATE here suppresses the inner retry loop on
    # the *generation* RPC for two reasons: (a) a blind re-POST wastes the
    # expensive LLM inference, and (b) LLM nondeterminism means a retried
    # generation may return a *different* mind-map JSON, which would
    # silently mismatch what the client saw on the first commit before the
    # response was lost. The persisted-write side is classified too:
    # ``CREATE_NOTE`` is ``NON_IDEMPOTENT_NO_RETRY`` for both note-create
    # variants, while ``UPDATE_NOTE`` is an idempotent set op.
    registry.register(
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
    # audit decision matrix lives in ADR-0005
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
    #   re-issuing. Today only the classification is in place (which suppresses
    #   the blind retry); the caller-side probe-then-create wrapper is a
    #   follow-up.
    registry.register(
        RPCMethod.CREATE_NOTEBOOK,
        IdempotencyPolicy.PROBE_THEN_CREATE,
        notes=(
            "notebook create has an executable title/baseline probe wrapper in "
            "NotebooksAPI.create; raw rpc_call paths must also suppress blind "
            "transport retries to avoid duplicate notebooks on commit-lost errors"
        ),
    )
    registry.register(
        RPCMethod.DELETE_NOTEBOOK,
        IdempotencyPolicy.IDEMPOTENT_SET_OP,
        notes="server-side delete is idempotent (set-op semantics)",
    )
    registry.register(
        RPCMethod.DELETE_SOURCE,
        IdempotencyPolicy.IDEMPOTENT_SET_OP,
        notes=(
            "server-side delete is idempotent (set-op semantics); "
            "batch multi-id uses the same method"
        ),
    )
    registry.register(
        RPCMethod.DELETE_ARTIFACT,
        IdempotencyPolicy.IDEMPOTENT_SET_OP,
        notes="server-side delete is idempotent (set-op semantics)",
    )
    registry.register(
        RPCMethod.REFRESH_SOURCE,
        IdempotencyPolicy.AT_LEAST_ONCE_ACCEPTED,
        notes="duplicate refresh jobs are acceptable cost (extra fetch, same content)",
    )
    registry.register(
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
    # * ``"url"`` — probe by ``source.url == url`` on a notebook list, filtered
    #   against a baseline of source ids captured before the create: a URL is
    #   NOT unique within a notebook, so an unfiltered match could hand back a
    #   pre-existing source and report a create that never landed (#2204). The
    #   probe is a single GET_NOTEBOOK; the wrapper retries the create once if
    #   the probe finds nothing. PROBE_THEN_CREATE.
    # * ``"drive"`` — probe by ``source.drive_document_id == file_id``, the
    #   Drive ``documentId`` echoed back in the source metadata, filtered
    #   against the same kind of pre-create baseline (a ``documentId`` is not
    #   unique within a notebook either; #2113). Drive rows carry no URL at
    #   all, so the ``/d/<file_id>``-in-``source.url`` probe this replaced
    #   could never match. Same wrapper as ``"url"``. PROBE_THEN_CREATE.
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
    # ``_web/sources/add.py`` / ``_web/sources/upload.py`` own the executable probe loop for
    # the URL, Drive, and file variants.

    _RAW_ADD_SOURCE_NOT_IDEMPOTENT_NOTE = (
        "raw ADD_SOURCE without an operation_variant has no proven dedupe/probe "
        "key. Public call sites must pass 'url', 'drive', or 'text'; direct "
        "rpc_call users get first-failure surfacing rather than blind retry"
    )

    registry.register(
        RPCMethod.ADD_SOURCE,
        IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY,
        notes=_RAW_ADD_SOURCE_NOT_IDEMPOTENT_NOTE,
    )
    registry.register(
        RPCMethod.ADD_SOURCE,
        IdempotencyPolicy.PROBE_THEN_CREATE,
        variant="url",
        notes=(
            "probe by source.url == url on notebook list (web + YouTube), "
            "filtered against a pre-create source-id baseline"
        ),
    )
    registry.register(
        RPCMethod.ADD_SOURCE,
        IdempotencyPolicy.PROBE_THEN_CREATE,
        variant="drive",
        notes=(
            "probe by source.drive_document_id == file_id on notebook list, "
            "filtered against a pre-create source-id baseline"
        ),
    )
    registry.register(
        RPCMethod.ADD_SOURCE,
        IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY,
        variant="text",
        notes="no reliable dedupe key — titles non-unique, body not exposed",
    )
    registry.register(
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
        # Live method ListRecentlyViewedProjects: a read-only recents list.
        # Probed for #2126: repeated LIST_NOTEBOOKS leaves lastViewedTime pinned,
        # so unlike GET_NOTEBOOK it writes nothing at all.
        RPCMethod.LIST_NOTEBOOKS: "read-only recents list; replay does not mutate notebook state",
        # NOT free of server-side effect, despite the bucket: GET_NOTEBOOK writes
        # ProjectMetadata.lastViewedTime and so reorders the account's recency
        # list (#2126). It stays idempotent because that write is
        # last-write-wins on a timestamp — a replay lands the same notebook at
        # the top of the same list, creating no resource and losing no data.
        RPCMethod.GET_NOTEBOOK: (
            "notebook fetch; replay does not mutate notebook content (it does refresh the "
            "server-side lastViewedTime recency stamp, which is last-write-wins under replay)"
        ),
        # Live method MutateProject (generic notebook mutator; covers title plus
        # chat-config and view-level via different params).
        RPCMethod.RENAME_NOTEBOOK: (
            "set notebook title/settings to caller-supplied values; replay leaves the same state"
        ),
        RPCMethod.GET_SOURCE: "read-only source content fetch; replay does not mutate source state",
        RPCMethod.RETRIEVE_RELEVANT_CHUNKS: (
            "read-only ranked source-passage retrieval; replay does not mutate source state"
        ),
        RPCMethod.CHECK_SOURCE_FRESHNESS: (
            "read-only freshness check; replay does not start a refresh job"
        ),
        RPCMethod.UPDATE_SOURCE: (
            "set source metadata/title to caller-supplied values; replay leaves the same state"
        ),
        # Live method GenerateNotebookGuide: generates the notebook guide
        # (summary + suggested questions); response-only, persists nothing.
        RPCMethod.SUMMARIZE: (
            "response-only notebook guide generation; no persisted resource is created by replay"
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
        # Live method GetArtifact: a generic single-artifact getter (we use it
        # to fetch interactive-artifact HTML / mind-map tree). Read-only.
        RPCMethod.GET_INTERACTIVE_HTML: (
            "read-only artifact fetch; replay does not mutate artifact state"
        ),
        # Live method ListDiscoverSourcesJob (the research family is the
        # DiscoverSources pipeline).
        RPCMethod.POLL_RESEARCH: "read-only research task poll; replay does not start a task",
        # Live method GetNotes: mind maps come back as JSON-bodied notes, so a
        # single GetNotes read returns both.
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
        RPCMethod.GET_CHAT_SESSION_STATUS: (
            "read-only chat generation-state fetch; replay does not mutate chat state"
        ),
        RPCMethod.CANCEL_GENERATION: (
            "chat generation cancel is idempotent; replay leaves the session stopped"
        ),
        # Live method DeleteChatTurns: deletes the conversation's chat turns
        # (the web UI "Delete history" action), idempotent set-op.
        RPCMethod.DELETE_CONVERSATION: (
            "server-side chat-turn delete is idempotent (set-op semantics)"
        ),
        # Live method GeneratePromptSuggestions: response-only prompt-suggestion
        # generation (same family as GET_SUGGESTED_REPORTS); persists nothing.
        RPCMethod.SUGGEST_PROMPTS: (
            "response-only prompt suggestion generation; no persisted resource is created by replay"
        ),
        # Live method NextStepSuggestions: the standalone follow-up-question
        # generator (the block chat answers carry at index 5); persists nothing.
        RPCMethod.SUGGEST_NEXT_STEPS: (
            "response-only follow-up question generation; no persisted resource is created by replay"
        ),
        # Live method GetArtifactCustomizationChoices: account-level read of the
        # Studio option tables; the server ignores the notebook id entirely.
        RPCMethod.GET_CUSTOMIZATION_CHOICES: (
            "read-only studio customization table; replay does not mutate any state"
        ),
        RPCMethod.GET_SHARE_STATUS: "read-only share status fetch; replay does not mutate ACL state",
        RPCMethod.REMOVE_RECENTLY_VIEWED: (
            "remove notebook from recents is idempotent; replay leaves it absent"
        ),
        # Live method GetOrCreateAccount: the first call may create the account
        # server-side, but every call (including replay) converges to the same
        # account state, so replay creates no *additional* resource.
        RPCMethod.GET_USER_SETTINGS: (
            "GetOrCreateAccount settings fetch; replay converges to the same account state"
        ),
        # Live method MutateAccount (generic account mutator; we use it only for
        # the output-language setting).
        RPCMethod.SET_USER_SETTINGS: (
            "set user settings to caller-supplied values; replay leaves the same state"
        ),
        RPCMethod.LIST_LABELS: "read-only label list; replay does not mutate label state",
        RPCMethod.LIST_EXPERT_INTELLIGENCE_CONTENT: (
            "read-only Play Books library list; replay does not mutate any state (#2292)"
        ),
        RPCMethod.UPDATE_LABEL: (
            "default (rename / set-emoji) sets label fields to caller-supplied values; "
            "replay leaves the same state. The add_sources variant is classified "
            "separately as NON_IDEMPOTENT_NO_RETRY"
        ),
    }

    for _method, _notes in _IDEMPOTENT_READ_OR_SET_OP_NOTES.items():
        registry.register(
            _method,
            IdempotencyPolicy.IDEMPOTENT_SET_OP,
            notes=_notes,
        )

    # ----------------------------------------------------------------------------
    # Complete coverage — non-idempotent methods with no reliable probe/token
    # ----------------------------------------------------------------------------

    registry.register(
        RPCMethod.EXPORT_ARTIFACT,
        IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY,
        notes=(
            "exports create an external Docs/Sheets artifact and return its URL; "
            "there is no client-token slot or reliable post-failure probe to bind "
            "a commit-lost export to this call"
        ),
    )
    registry.register(
        RPCMethod.REVISE_SLIDE,
        IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY,
        notes=(
            "slide revision starts a prompt-driven generation/update with no "
            "client-token slot or probe; a blind retry may create a second, "
            "divergent revision"
        ),
    )
    registry.register(
        RPCMethod.RETRY_ARTIFACT,
        IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY,
        notes=(
            "in-place retry kicks off a fresh generation for an already-failed "
            "artifact; the artifact_id is fixed and re-used, but the RPC has no "
            "client-token slot and the response carries the same id whether or "
            "not the kickoff committed, so a blind transport retry could re-launch "
            "generation twice. Surface the first failure and let the caller decide "
            "whether to re-invoke (issue #1319)"
        ),
    )

    # ----------------------------------------------------------------------------
    # Source labels (multi-mode CREATE_LABEL / batch DELETE_LABEL / fieldmask
    # UPDATE_LABEL add_sources). LIST_LABELS and the default rename/emoji
    # UPDATE_LABEL are idempotent set-ops registered above; the writes below have
    # no caller-supplied client-token slot.
    # ----------------------------------------------------------------------------
    registry.register(
        RPCMethod.CREATE_LABEL,
        IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY,
        notes=(
            "multi-mode label create/auto-group (agX4Bc) with no client-token slot; "
            "the server allocates label ids and echoes the full set, so a blind retry "
            "on commit-lost could create a duplicate manual label or regenerate every "
            "label with fresh ids — surface the first failure"
        ),
    )
    registry.register(
        RPCMethod.DELETE_LABEL,
        IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY,
        notes=(
            "batch label delete with no client-token slot; conservative until the "
            "already-absent wire behavior is captured (rpc.md open item) — no blind "
            "retry until a committed-then-retried delete is proven to no-op, then "
            "downgrade to IDEMPOTENT_SET_OP like DELETE_SOURCE/DELETE_ARTIFACT"
        ),
    )
    registry.register(
        RPCMethod.UPDATE_LABEL,
        IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY,
        variant="add_sources",
        notes=(
            "add_sources APPENDS source ids via the fieldmask, with no client-token "
            "slot; whether a blind retry that lands twice dedupes server-side is "
            "unverified (rpc.md), so surface the first failure rather than risk a "
            "double-append"
        ),
    )
    registry.register(
        RPCMethod.UPDATE_LABEL,
        IdempotencyPolicy.IDEMPOTENT_SET_OP,
        variant="remove_sources",
        notes=(
            "remove_sources UN-ASSIGNS a source via the sources_remove fieldmask slot; "
            "removing an already-absent member is a confirmed silent no-op (rpc.md "
            "2026-06-07), so a blind transport retry that lands twice leaves the same "
            "final state — retry-safe set-op semantics like DELETE_SOURCE"
        ),
    )
    # Collections reuse UPDATE_LABEL (le8sX) for account-level notebook membership.
    # Distinct variants (not the source-scoped keys above) so the registry notes stay
    # honest about what is being mutated.
    registry.register(
        RPCMethod.UPDATE_LABEL,
        IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY,
        variant="add_notebooks",
        notes=(
            "add_notebooks APPENDS a notebook id to a collection via the membership "
            "fieldmask, with no client-token slot; like add_sources, whether a blind "
            "retry that lands twice dedupes server-side is unverified, so surface the "
            "first failure rather than risk a double-append"
        ),
    )
    registry.register(
        RPCMethod.UPDATE_LABEL,
        IdempotencyPolicy.IDEMPOTENT_SET_OP,
        variant="remove_notebooks",
        notes=(
            "remove_notebooks UN-ASSIGNS a notebook from a collection via the "
            "wire-captured remove fieldmask group (PR #2009, live-confirmed on "
            "four independent accounts, thanks to tomihe0720 and "
            "erricklong85-tech); removing an already-absent member is a "
            "confirmed silent no-op (live-verified), so a blind transport retry "
            "that lands twice leaves the same final state — retry-safe set-op "
            "semantics like remove_sources"
        ),
    )


# The web policy owns the one production registry and its one import-time seed.
# Keep this after ``register_default_policies`` so importing the module always
# exposes a fully classified singleton without a neutral-to-web import cycle.
IDEMPOTENCY_REGISTRY = IdempotencyRegistry()
register_default_policies(IDEMPOTENCY_REGISTRY)


__all__ = [
    "IDEMPOTENCY_REGISTRY",
    "IdempotencyEntry",
    "IdempotencyPolicy",
    "IdempotencyRegistry",
    "ProbeKeyFn",
    "register_default_policies",
    "resolve_effective_disable_internal_retries",
]
