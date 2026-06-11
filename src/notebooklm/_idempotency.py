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
the per-RPC classification rationale is ADR-0005
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
# server. With ``disable_internal_retries=True``, the middleware retry loop
# inside ``RuntimeTransport.perform_authed_post`` does not replay these;
# instead ``rpc_call`` translates the underlying ``TransportServerError`` /
# network failure into ``ServerError`` / ``NetworkError`` / ``RateLimitError``
# and surfaces it here. ``idempotent_create`` catches exactly these; anything else (auth,
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
            failure surfaces to this wrapper instead of being replayed
            blindly by the retry middleware inside
            ``RuntimeTransport.perform_authed_post``.
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


# Module-level production registry. The declarative per-method classification
# data lives in ``_idempotency_policy.py`` and is applied to this singleton by
# ``register_default_policies`` at the bottom of this module (issue #1331).
#
# The classification pass is two-stage and the ordering is load-bearing: some
# entries register *before* ``_seed_defaults`` (so the seeder skips them), the
# seeder then fills the ``UNCLASSIFIED`` placeholder for every remaining method,
# and the rest register *after* the seed (overwriting placeholders). See
# ``register_default_policies`` and ADR-0005 for the full rationale.
IDEMPOTENCY_REGISTRY = IdempotencyRegistry()


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


__all__ = [
    "idempotent_create",
    "IdempotencyPolicy",
    "IdempotencyEntry",
    "IdempotencyRegistry",
    "IDEMPOTENCY_REGISTRY",
    "ProbeKeyFn",
    "resolve_effective_disable_internal_retries",
]


# Seed the production singleton with its declarative classification data. This
# import is intentionally at the bottom of the module: by now every class the
# policy data depends on (``IdempotencyPolicy``/``IdempotencyRegistry``) is
# defined, which breaks the import cycle with ``_idempotency_policy`` (it imports
# those names from here). Seeding runs once at ``_idempotency`` import time, so
# every importer of ``IDEMPOTENCY_REGISTRY`` gets a fully-seeded singleton.
from ._idempotency_policy import register_default_policies  # noqa: E402

register_default_policies(IDEMPOTENCY_REGISTRY)
