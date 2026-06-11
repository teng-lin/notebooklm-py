"""Tests for the RPC idempotency registry.

The registry is a 5-policy classification layer with operation variants
that the ``RpcExecutor`` consults to compute
``effective_disable_internal_retries``. The production registry must
explicitly classify every ``RPCMethod``; the ``UNCLASSIFIED`` policy is
retained only as a placeholder for hand-built test registries and
future-drift detection.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from notebooklm._idempotency import (
    IDEMPOTENCY_REGISTRY,
    IdempotencyEntry,
    IdempotencyPolicy,
    IdempotencyRegistry,
    resolve_effective_disable_internal_retries,
)
from notebooklm.exceptions import IdempotencyVariantError
from notebooklm.rpc import RPCMethod


class SeedSpyRegistry(IdempotencyRegistry):
    """An :class:`IdempotencyRegistry` that counts ``_seed_defaults`` calls.

    Used to assert that ``register_default_policies`` runs the totality seed
    pass exactly once — the one regression the per-method lookups cannot catch
    while every current ``RPCMethod`` is explicitly classified.
    """

    def __init__(self) -> None:
        super().__init__()
        self.seed_calls = 0

    def _seed_defaults(self) -> None:
        self.seed_calls += 1
        super()._seed_defaults()


# ---------------------------------------------------------------------------
# Coverage: every RPCMethod has an explicit production classification
# ---------------------------------------------------------------------------


def test_registry_classifies_every_rpc_method_at_variant_none() -> None:
    """Every RPCMethod MUST have an explicit ``(method, None)`` entry."""
    for method in RPCMethod:
        entry = IDEMPOTENCY_REGISTRY.get_entry(method)
        assert entry is not None, f"{method.name} has no (method, None) registry entry"
        assert isinstance(entry, IdempotencyEntry)
        assert isinstance(entry.policy, IdempotencyPolicy)
        assert entry.policy is not IdempotencyPolicy.UNCLASSIFIED, (
            f"{method.name} kept the UNCLASSIFIED placeholder; add an explicit "
            "idempotency classification"
        )
        assert entry.notes.strip(), f"{method.name} classification must document its rationale"


def test_seed_defaults_makes_unregistered_methods_resolve_unclassified() -> None:
    """A method with NO explicit ``.register()`` MUST resolve to UNCLASSIFIED.

    This pins the seed *semantics* directly: a fresh registry seeded only by
    ``_seed_defaults`` (no explicit registrations) classifies every method as
    UNCLASSIFIED. The seeding pass is what guarantees the registry is a *total*
    function over ``RPCMethod`` by filling the UNCLASSIFIED placeholder for
    every method the explicit registrations leave untouched.
    """
    seeded = IdempotencyRegistry()
    seeded._seed_defaults()

    for method in RPCMethod:
        entry = seeded.get_entry(method)
        assert entry.policy is IdempotencyPolicy.UNCLASSIFIED, (
            f"{method.name} did not resolve to UNCLASSIFIED after _seed_defaults; "
            "the totality seed pass is broken"
        )


def test_register_default_policies_runs_the_totality_seed_pass() -> None:
    """``register_default_policies`` MUST run the ``_seed_defaults`` totality pass.

    The seed pass is what guarantees the registry is a *total* function over
    ``RPCMethod`` for any method the explicit registrations leave untouched
    (today every method is classified, so a dropped seed would NOT regress any
    currently-classified method — only a future, unregistered one). That makes
    the seed invisible to every per-method ``lookup()`` against today's enum, so
    the only way to catch a dropped/misordered seed is to assert it actually
    fires. A spy registry counts the ``_seed_defaults`` calls, and a separate
    fresh registry confirms the applied result is total over ``RPCMethod``.
    """
    from notebooklm._idempotency_policy import register_default_policies

    # (a) The seed pass fires exactly once during policy application.
    spy = SeedSpyRegistry()
    register_default_policies(spy)
    assert spy.seed_calls == 1, (
        "register_default_policies did not invoke _seed_defaults exactly once; "
        "the totality seed pass was dropped or duplicated"
    )

    # (b) The applied registry is total: every method resolves (a dropped seed
    # would KeyError for any method left unregistered).
    registry = IdempotencyRegistry()
    register_default_policies(registry)
    for method in RPCMethod:
        entry = registry.get_entry(method)
        assert isinstance(entry, IdempotencyEntry)
        assert entry.policy is not IdempotencyPolicy.UNCLASSIFIED, (
            f"{method.name} kept UNCLASSIFIED after register_default_policies; "
            "add an explicit classification"
        )


def test_registry_has_no_unclassified_production_entries() -> None:
    """Guard against future ``RPCMethod`` additions without classification."""
    unclassified = [
        f"{method.name}:{variant or '<default>'}"
        for method, variant, entry in IDEMPOTENCY_REGISTRY.iter_entries()
        if entry.policy is IdempotencyPolicy.UNCLASSIFIED
    ]

    assert unclassified == []


def test_retry_disabled_entries_are_intentional_and_documented() -> None:
    """Non-retryable methods are pinned so cleanup cannot make them retryable."""
    expected = {
        (RPCMethod.CREATE_NOTEBOOK, None): IdempotencyPolicy.PROBE_THEN_CREATE,
        (RPCMethod.ADD_SOURCE, None): IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY,
        (RPCMethod.ADD_SOURCE, "url"): IdempotencyPolicy.PROBE_THEN_CREATE,
        (RPCMethod.ADD_SOURCE, "drive"): IdempotencyPolicy.PROBE_THEN_CREATE,
        (RPCMethod.ADD_SOURCE, "text"): IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY,
        (RPCMethod.ADD_SOURCE_FILE, None): IdempotencyPolicy.PROBE_THEN_CREATE,
        (RPCMethod.CREATE_ARTIFACT, None): IdempotencyPolicy.PROBE_THEN_CREATE,
        (RPCMethod.EXPORT_ARTIFACT, None): IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY,
        (RPCMethod.REVISE_SLIDE, None): IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY,
        (RPCMethod.RETRY_ARTIFACT, None): IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY,
        (RPCMethod.START_FAST_RESEARCH, None): IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY,
        (RPCMethod.START_DEEP_RESEARCH, None): IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY,
        (RPCMethod.IMPORT_RESEARCH, None): IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY,
        (RPCMethod.GENERATE_MIND_MAP, None): IdempotencyPolicy.PROBE_THEN_CREATE,
        (RPCMethod.CREATE_NOTE, None): IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY,
        (RPCMethod.CREATE_NOTE, "plain"): IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY,
        (RPCMethod.CREATE_NOTE, "saved_from_chat"): IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY,
        (RPCMethod.SHARE_NOTEBOOK, None): IdempotencyPolicy.PROBE_THEN_CREATE,
        (RPCMethod.CREATE_LABEL, None): IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY,
        (RPCMethod.DELETE_LABEL, None): IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY,
        (RPCMethod.UPDATE_LABEL, "add_sources"): IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY,
    }
    actual = {
        (method, variant): entry.policy
        for method, variant, entry in IDEMPOTENCY_REGISTRY.iter_entries()
        if entry.policy
        in {
            IdempotencyPolicy.PROBE_THEN_CREATE,
            IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY,
        }
    }

    assert actual == expected
    for method, variant in expected:
        entry = IDEMPOTENCY_REGISTRY.get_entry(method, operation_variant=variant)
        assert entry.notes.strip()
        assert (
            resolve_effective_disable_internal_retries(
                IDEMPOTENCY_REGISTRY,
                method,
                caller_disable_internal_retries=False,
                operation_variant=variant,
            )
            is True
        )


def test_update_label_remove_sources_variant_is_idempotent_set_op() -> None:
    """The ``remove_sources`` UPDATE_LABEL variant is a retry-safe set-op.

    Removing an already-absent member is a confirmed silent no-op (rpc.md
    2026-06-07), so a blind transport retry that lands twice leaves the same
    final state — it MUST classify as ``IDEMPOTENT_SET_OP`` (not the NO_RETRY
    bucket that ``add_sources`` lives in). This is asserted as a positive case
    rather than via the NO_RETRY ``expected`` table, which filters set-ops out.
    """
    entry = IDEMPOTENCY_REGISTRY.get_entry(
        RPCMethod.UPDATE_LABEL, operation_variant="remove_sources"
    )
    assert entry.policy is IdempotencyPolicy.IDEMPOTENT_SET_OP
    assert entry.notes.strip()
    # Set-op semantics keep the transport retry loop enabled (caller-False stays False).
    assert (
        resolve_effective_disable_internal_retries(
            IDEMPOTENCY_REGISTRY,
            RPCMethod.UPDATE_LABEL,
            caller_disable_internal_retries=False,
            operation_variant="remove_sources",
        )
        is False
    )


def test_non_idempotent_no_retry_entries_document_dedupe_gap() -> None:
    """Hard no-retry methods must explain why blind retry cannot be safe."""
    expected_terms = {
        (RPCMethod.ADD_SOURCE, None): ("operation_variant", "blind retry"),
        (RPCMethod.ADD_SOURCE, "text"): ("no reliable dedupe key",),
        (RPCMethod.EXPORT_ARTIFACT, None): ("external docs/sheets", "no client-token"),
        (RPCMethod.REVISE_SLIDE, None): ("no client-token", "blind retry"),
        (RPCMethod.RETRY_ARTIFACT, None): ("no client-token", "blind transport retry"),
        (RPCMethod.START_FAST_RESEARCH, None): ("ambiguous", "same query"),
        (RPCMethod.START_DEEP_RESEARCH, None): ("ambiguous", "same query"),
        (RPCMethod.IMPORT_RESEARCH, None): ("cannot bind", "same urls"),
        (RPCMethod.CREATE_NOTE, None): ("no client-token", "no client-visible note_id"),
        (RPCMethod.CREATE_NOTE, "plain"): ("no client-token", "no client-visible note_id"),
        (RPCMethod.CREATE_NOTE, "saved_from_chat"): (
            "no client-token",
            "no client-visible note_id",
        ),
        (RPCMethod.CREATE_LABEL, None): ("no client-token", "blind retry"),
        (RPCMethod.DELETE_LABEL, None): ("no client-token", "blind retry"),
        (RPCMethod.UPDATE_LABEL, "add_sources"): ("no client-token", "blind retry"),
    }

    for (method, variant), terms in expected_terms.items():
        entry = IDEMPOTENCY_REGISTRY.get_entry(method, operation_variant=variant)

        assert entry.policy is IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY
        lower_notes = entry.notes.lower()
        for term in terms:
            assert term in lower_notes


# ---------------------------------------------------------------------------
# 5-policy enum
# ---------------------------------------------------------------------------


def test_idempotency_policy_has_all_five_values() -> None:
    """The classification axis is 5-way; nothing else."""
    expected = {
        "UNCLASSIFIED",
        "PROBE_THEN_CREATE",
        "IDEMPOTENT_SET_OP",
        "AT_LEAST_ONCE_ACCEPTED",
        "NON_IDEMPOTENT_NO_RETRY",
    }
    actual = {p.name for p in IdempotencyPolicy}
    assert actual == expected


# ---------------------------------------------------------------------------
# Variant lookup + fallback semantics
# ---------------------------------------------------------------------------


def test_variant_lookup_returns_variant_specific_entry() -> None:
    """Looking up ``(method, variant)`` MUST hit the variant-specific entry."""
    registry = IdempotencyRegistry()
    registry.register(RPCMethod.LIST_NOTEBOOKS, IdempotencyPolicy.UNCLASSIFIED)
    registry.register(
        RPCMethod.LIST_NOTEBOOKS,
        IdempotencyPolicy.IDEMPOTENT_SET_OP,
        variant="upsert",
    )

    entry = registry.get_entry(RPCMethod.LIST_NOTEBOOKS, operation_variant="upsert")
    assert entry.policy is IdempotencyPolicy.IDEMPOTENT_SET_OP


def test_variant_lookup_falls_back_to_method_none_when_no_variant_table() -> None:
    """A method with NO variant entries falls back to ``(method, None)``."""
    registry = IdempotencyRegistry()
    registry.register(RPCMethod.LIST_NOTEBOOKS, IdempotencyPolicy.UNCLASSIFIED)

    # No variant table for LIST_NOTEBOOKS exists at all — fall back silently.
    entry = registry.get_entry(RPCMethod.LIST_NOTEBOOKS, operation_variant="anything")
    assert entry.policy is IdempotencyPolicy.UNCLASSIFIED


def test_unknown_variant_with_explicit_variant_entries_raises() -> None:
    """If a method HAS variant entries but the caller supplies an unknown one,
    the registry MUST raise :class:`IdempotencyVariantError` rather than
    silently fall back to ``(method, None)``. Silent fallback would hide
    typos / API drift in caller code."""
    registry = IdempotencyRegistry()
    registry.register(RPCMethod.LIST_NOTEBOOKS, IdempotencyPolicy.UNCLASSIFIED)
    registry.register(
        RPCMethod.LIST_NOTEBOOKS,
        IdempotencyPolicy.IDEMPOTENT_SET_OP,
        variant="upsert",
    )
    registry.register(
        RPCMethod.LIST_NOTEBOOKS,
        IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY,
        variant="overwrite",
    )

    with pytest.raises(IdempotencyVariantError) as exc_info:
        registry.get_entry(RPCMethod.LIST_NOTEBOOKS, operation_variant="frobnicate")

    assert "frobnicate" in str(exc_info.value)
    assert "LIST_NOTEBOOKS" in str(exc_info.value)


def test_unknown_variant_with_no_variant_entries_falls_back_quietly() -> None:
    """A method with ONLY the ``(method, None)`` entry MUST tolerate any
    variant name (silent fallback). This keeps methods without variant tables
    backward-compatible."""
    registry = IdempotencyRegistry()
    registry.register(RPCMethod.LIST_NOTEBOOKS, IdempotencyPolicy.UNCLASSIFIED)

    entry = registry.get_entry(RPCMethod.LIST_NOTEBOOKS, operation_variant="anything-goes")
    assert entry.policy is IdempotencyPolicy.UNCLASSIFIED


def test_none_variant_returns_method_default() -> None:
    """``operation_variant=None`` MUST return the ``(method, None)`` entry."""
    registry = IdempotencyRegistry()
    registry.register(RPCMethod.LIST_NOTEBOOKS, IdempotencyPolicy.IDEMPOTENT_SET_OP)

    entry = registry.get_entry(RPCMethod.LIST_NOTEBOOKS, operation_variant=None)
    assert entry.policy is IdempotencyPolicy.IDEMPOTENT_SET_OP


# ---------------------------------------------------------------------------
# Effective disable_internal_retries precedence
# ---------------------------------------------------------------------------


def test_caller_disable_true_always_wins() -> None:
    """Caller-passed ``disable_internal_retries=True`` MUST always win,
    regardless of policy. Explicit caller intent dominates policy."""
    registry = IdempotencyRegistry()
    # Even an IDEMPOTENT_SET_OP policy (which would leave retries enabled)
    # must not flip the caller's True back to False.
    registry.register(RPCMethod.LIST_NOTEBOOKS, IdempotencyPolicy.IDEMPOTENT_SET_OP)

    effective = resolve_effective_disable_internal_retries(
        registry,
        RPCMethod.LIST_NOTEBOOKS,
        caller_disable_internal_retries=True,
        operation_variant=None,
    )
    assert effective is True


def test_probe_then_create_disables_internal_retries() -> None:
    """PROBE_THEN_CREATE methods are NOT safe to retry inside the transport —
    the executor must surface failures so the caller's probe-then-create
    state machine handles them."""
    registry = IdempotencyRegistry()
    registry.register(RPCMethod.LIST_NOTEBOOKS, IdempotencyPolicy.PROBE_THEN_CREATE)

    effective = resolve_effective_disable_internal_retries(
        registry,
        RPCMethod.LIST_NOTEBOOKS,
        caller_disable_internal_retries=False,
        operation_variant=None,
    )
    assert effective is True


def test_non_idempotent_no_retry_disables_internal_retries() -> None:
    """NON_IDEMPOTENT_NO_RETRY is a hard "never retry" — disables the
    transport retry loop unconditionally (caller-False is overridden upward)."""
    registry = IdempotencyRegistry()
    registry.register(RPCMethod.LIST_NOTEBOOKS, IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY)

    effective = resolve_effective_disable_internal_retries(
        registry,
        RPCMethod.LIST_NOTEBOOKS,
        caller_disable_internal_retries=False,
        operation_variant=None,
    )
    assert effective is True


@pytest.mark.parametrize(
    "policy",
    [
        IdempotencyPolicy.UNCLASSIFIED,
        IdempotencyPolicy.IDEMPOTENT_SET_OP,
        IdempotencyPolicy.AT_LEAST_ONCE_ACCEPTED,
    ],
)
def test_safe_policies_leave_caller_false_untouched(
    policy: IdempotencyPolicy,
) -> None:
    """Policies that are safe to retry (or that handle retries via other
    mechanisms) MUST NOT flip caller-False to True."""
    registry = IdempotencyRegistry()
    registry.register(RPCMethod.LIST_NOTEBOOKS, policy)

    effective = resolve_effective_disable_internal_retries(
        registry,
        RPCMethod.LIST_NOTEBOOKS,
        caller_disable_internal_retries=False,
        operation_variant=None,
    )
    assert effective is False


# ---------------------------------------------------------------------------
# Silent placeholder: UNCLASSIFIED emits zero log lines
# ---------------------------------------------------------------------------


def test_unclassified_emits_no_log_lines_across_1000_calls(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """UNCLASSIFIED MUST be 100% silent in hand-built placeholder registries.

    1000 calls is enough to catch any per-call WARN/INFO leak.
    """
    registry = IdempotencyRegistry()
    registry.register(RPCMethod.LIST_NOTEBOOKS, IdempotencyPolicy.UNCLASSIFIED)

    with caplog.at_level(logging.DEBUG, logger="notebooklm._idempotency"):
        for _ in range(1000):
            resolve_effective_disable_internal_retries(
                registry,
                RPCMethod.LIST_NOTEBOOKS,
                caller_disable_internal_retries=False,
                operation_variant=None,
            )

    idempotency_records = [
        r for r in caplog.records if r.name.startswith("notebooklm._idempotency")
    ]
    assert idempotency_records == []


# ---------------------------------------------------------------------------
# AT_LEAST_ONCE_ACCEPTED: rate-limited WARN
# ---------------------------------------------------------------------------


def test_at_least_once_accepted_rate_limits_warn_log(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AT_LEAST_ONCE_ACCEPTED methods MUST emit a WARN to flag that the
    caller is accepting at-least-once semantics, but the log MUST be
    rate-limited to avoid spamming under load (100 calls → ≤2 log lines)."""
    # Clear the module-level rate-limit ledger so a previously-tripped
    # window from another test doesn't suppress the first WARN here.
    import notebooklm._idempotency as idemp_mod

    monkeypatch.setattr(idemp_mod, "_at_least_once_last_logged", {})

    registry = IdempotencyRegistry()
    registry.register(RPCMethod.LIST_NOTEBOOKS, IdempotencyPolicy.AT_LEAST_ONCE_ACCEPTED)

    with caplog.at_level(logging.WARNING, logger="notebooklm._idempotency"):
        for _ in range(100):
            resolve_effective_disable_internal_retries(
                registry,
                RPCMethod.LIST_NOTEBOOKS,
                caller_disable_internal_retries=False,
                operation_variant=None,
            )

    warn_records = [
        r
        for r in caplog.records
        if r.name.startswith("notebooklm._idempotency") and r.levelno >= logging.WARNING
    ]
    assert len(warn_records) <= 2, (
        f"AT_LEAST_ONCE_ACCEPTED emitted {len(warn_records)} WARN lines for 100 "
        "calls; expected ≤2 (rate-limited)"
    )
    assert len(warn_records) >= 1, "AT_LEAST_ONCE_ACCEPTED emitted 0 WARN lines; expected ≥1"


# ---------------------------------------------------------------------------
# RpcExecutor consultation: behavioral equivalence with active registry
# ---------------------------------------------------------------------------


@pytest.fixture
def _build_rpc_executor() -> Any:
    """Build a minimally-wired RpcExecutor for behavioral-equivalence tests.

    The executor is driven via its ``_execute_once()`` method (the lowest of the
    five consultation sites). The fixture stubs the transport so we can
    assert on the ``disable_internal_retries`` value that the executor
    actually hands to ``RuntimeTransport.perform_authed_post``.
    """
    from notebooklm._rpc_executor import RpcExecutor

    captured: dict[str, Any] = {}

    async def _fake_perform_authed_post(
        *,
        build_request: Any,
        log_label: str,
        disable_internal_retries: bool = False,
        rpc_method: str | None = None,
        refresh_budget: Any = None,
    ) -> httpx.Response:
        captured["disable_internal_retries"] = disable_internal_retries
        captured["log_label"] = log_label
        captured["rpc_method"] = rpc_method
        return httpx.Response(200, text=")]}'\n[]")

    # ADR-0014 Rule 5 (Wave 4 of session-decoupling): RpcExecutor takes
    # its four collaborators (kernel/transport/auth_refresh/metrics) as
    # keyword-only args. Use four MagicMock collaborators so each role
    # can be inspected independently.
    kernel = MagicMock()
    transport = MagicMock()
    transport.perform_authed_post = AsyncMock(side_effect=_fake_perform_authed_post)
    auth_refresh = MagicMock()
    auth_refresh.await_refresh = AsyncMock()
    auth_refresh.has_refresh_callback = False
    metrics = MagicMock()

    # Surviving legacy ivars used by the providers below.
    timeout = 30.0
    refresh_retry_delay = 0.0

    def _decode(raw: str, rpc_id: str, *, allow_null: bool = False) -> Any:
        return []

    async def _sleep(_: float) -> None:
        return None

    def _is_auth_error(_: Exception) -> bool:
        return False

    executor = RpcExecutor(
        kernel=kernel,
        transport=transport,
        auth_refresh=auth_refresh,
        metrics=metrics,
        decode_response=_decode,
        is_auth_error=_is_auth_error,
        sleep=_sleep,
        timeout_provider=lambda: timeout,
        refresh_callback_enabled_provider=lambda: auth_refresh.has_refresh_callback,
        refresh_retry_delay_provider=lambda: refresh_retry_delay,
    )
    # ``_unused`` slot preserved for backward-compatible 3-tuple unpacking
    # at call sites that have not been migrated to the keyword-collaborators
    # shape. After Wave 4 of session-decoupling (ADR-0014 Rule 5), the executor
    # holds its collaborators directly so the middle slot is just None.
    return executor, None, captured


@pytest.mark.asyncio
async def test_default_registry_preserves_today_behavior(
    _build_rpc_executor: Any,
) -> None:
    """Behavioral equivalence: retry-safe classifications keep retries enabled.

    ``LIST_NOTEBOOKS`` is explicitly classified as a retry-safe read. An
    unspecified ``disable_internal_retries`` MUST still resolve to False —
    exactly the public default.
    """
    executor, _unused, captured = _build_rpc_executor

    await executor._execute_once(
        RPCMethod.LIST_NOTEBOOKS,
        params=[],
        source_path="/",
        allow_null=False,
        _is_retry=False,
    )

    assert captured["disable_internal_retries"] is False
    # Pin ``rpc_method`` propagation through the executor → transport seam
    # so a regression in the kwarg threading can't slip past the suite.
    assert captured["rpc_method"] == RPCMethod.LIST_NOTEBOOKS.name


@pytest.mark.asyncio
async def test_caller_disable_true_propagates_through_executor(
    _build_rpc_executor: Any,
) -> None:
    """Explicit caller ``disable_internal_retries=True`` MUST reach the
    transport regardless of policy (caller wins)."""
    executor, _unused, captured = _build_rpc_executor

    await executor._execute_once(
        RPCMethod.LIST_NOTEBOOKS,
        params=[],
        source_path="/",
        allow_null=False,
        _is_retry=False,
        disable_internal_retries=True,
    )

    assert captured["disable_internal_retries"] is True
    # PR 12.9 audit fix: pin ``rpc_method`` threading too — coderabbit
    # flagged that the fake captures it but no test asserts on it.
    assert captured["rpc_method"] == RPCMethod.LIST_NOTEBOOKS.name


@pytest.mark.asyncio
async def test_operation_variant_kwarg_threads_through_executor(
    _build_rpc_executor: Any,
) -> None:
    """``operation_variant`` MUST be accepted as a kwarg on
    ``RpcExecutor._execute_once()`` without breaking fallback for methods
    without explicit variant tables."""
    executor, _unused, captured = _build_rpc_executor

    # Should not raise — kwarg is accepted everywhere.
    await executor._execute_once(
        RPCMethod.LIST_NOTEBOOKS,
        params=[],
        source_path="/",
        allow_null=False,
        _is_retry=False,
        operation_variant="some-variant",
    )

    assert captured["disable_internal_retries"] is False
    # PR 12.9 audit fix: pin ``rpc_method`` threading on this path too.
    assert captured["rpc_method"] == RPCMethod.LIST_NOTEBOOKS.name
