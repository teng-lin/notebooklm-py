"""Tests for the mutating-RPC idempotency registry foundation.

This is the B1 foundation (P0-3 + P1-2): a 6-policy classification layer
with operation variants that the ``RpcExecutor`` consults to compute
``effective_disable_internal_retries`` and optional client-token injection.

Behavioral classifications for individual RPCs are intentionally deferred
to Wave 2 — every method default-populates to
:attr:`~notebooklm._idempotency.IdempotencyPolicy.UNCLASSIFIED` and the
executor MUST stay silent + reproduce today's retry behavior for those
entries (regression-protect the "no behavioral drift" acceptance).
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

# ---------------------------------------------------------------------------
# Coverage: every RPCMethod has a (method, None) entry
# ---------------------------------------------------------------------------


def test_registry_covers_every_rpc_method_at_variant_none() -> None:
    """Every RPCMethod MUST have a (method, None) entry — the default fallback.

    Wave 2 will classify each method individually. Until then, every method
    resolves to UNCLASSIFIED with the placeholder note so the registry is
    a total function over ``RPCMethod``.
    """
    for method in RPCMethod:
        entry = IDEMPOTENCY_REGISTRY.get_entry(method)
        assert entry is not None, f"{method.name} has no (method, None) registry entry"
        assert isinstance(entry, IdempotencyEntry)
        assert isinstance(entry.policy, IdempotencyPolicy)


def test_registry_defaults_to_unclassified_placeholder() -> None:
    """Default placeholder MUST be UNCLASSIFIED with the Wave 2 marker note."""
    # Pick a stable method that Wave 2 hasn't classified yet.
    entry = IDEMPOTENCY_REGISTRY.get_entry(RPCMethod.LIST_NOTEBOOKS)
    assert entry.policy is IdempotencyPolicy.UNCLASSIFIED
    assert "placeholder" in entry.notes.lower()
    assert "wave 2" in entry.notes.lower()


# ---------------------------------------------------------------------------
# 6-policy enum
# ---------------------------------------------------------------------------


def test_idempotency_policy_has_all_six_values() -> None:
    """The classification axis is 6-way; nothing else."""
    expected = {
        "UNCLASSIFIED",
        "PROBE_THEN_CREATE",
        "IDEMPOTENT_SET_OP",
        "CLIENT_TOKEN_DEDUPE",
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
    variant name (silent fallback). This keeps the foundation behavior-neutral
    until Wave 2 adds variant tables."""
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
        IdempotencyPolicy.CLIENT_TOKEN_DEDUPE,
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
    """UNCLASSIFIED MUST be 100% silent — the foundation cannot spam logs
    while Wave 2 hasn't classified the bulk of the registry. 1000 calls
    is enough to catch any per-call WARN/INFO leak."""
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
# CLIENT_TOKEN_DEDUPE: token injection
# ---------------------------------------------------------------------------


def test_client_token_dedupe_injects_uuid_when_field_missing() -> None:
    """CLIENT_TOKEN_DEDUPE policy MUST inject a fresh ``uuid4().hex`` token
    into the field named by ``IdempotencyEntry.client_token_field`` when the
    caller did NOT pre-populate it."""
    from notebooklm._idempotency import maybe_inject_client_token

    registry = IdempotencyRegistry()
    registry.register(
        RPCMethod.LIST_NOTEBOOKS,
        IdempotencyPolicy.CLIENT_TOKEN_DEDUPE,
        client_token_field="client_token",
    )

    params: dict[str, Any] = {"foo": "bar"}
    maybe_inject_client_token(
        registry,
        RPCMethod.LIST_NOTEBOOKS,
        params,
        operation_variant=None,
    )

    assert "client_token" in params
    token = params["client_token"]
    assert isinstance(token, str)
    assert len(token) == 32  # uuid4().hex is 32 hex chars
    assert int(token, 16) >= 0  # parseable as hex


def test_client_token_dedupe_respects_caller_provided_token() -> None:
    """If the caller already pre-populated the client-token field, the
    registry MUST NOT overwrite it. Caller intent wins."""
    from notebooklm._idempotency import maybe_inject_client_token

    registry = IdempotencyRegistry()
    registry.register(
        RPCMethod.LIST_NOTEBOOKS,
        IdempotencyPolicy.CLIENT_TOKEN_DEDUPE,
        client_token_field="client_token",
    )

    params: dict[str, Any] = {"client_token": "caller-provided-token"}
    maybe_inject_client_token(
        registry,
        RPCMethod.LIST_NOTEBOOKS,
        params,
        operation_variant=None,
    )

    assert params["client_token"] == "caller-provided-token"


def test_client_token_dedupe_positional_injection_into_list_params() -> None:
    """When ``client_token_field`` is an int, the registry MUST inject into
    the list-shaped params at that index (batchexecute typical shape)."""
    from notebooklm._idempotency import maybe_inject_client_token

    registry = IdempotencyRegistry()
    registry.register(
        RPCMethod.LIST_NOTEBOOKS,
        IdempotencyPolicy.CLIENT_TOKEN_DEDUPE,
        client_token_field=2,  # positional slot
    )

    params: list[Any] = ["notebook_id", "title", None, "extra"]
    maybe_inject_client_token(
        registry,
        RPCMethod.LIST_NOTEBOOKS,
        params,
        operation_variant=None,
    )

    token = params[2]
    assert isinstance(token, str)
    assert len(token) == 32
    # Surrounding slots untouched
    assert params[0] == "notebook_id"
    assert params[1] == "title"
    assert params[3] == "extra"


def test_client_token_dedupe_positional_respects_caller_value() -> None:
    """Caller-populated positional client-token MUST NOT be overwritten."""
    from notebooklm._idempotency import maybe_inject_client_token

    registry = IdempotencyRegistry()
    registry.register(
        RPCMethod.LIST_NOTEBOOKS,
        IdempotencyPolicy.CLIENT_TOKEN_DEDUPE,
        client_token_field=2,
    )

    params: list[Any] = ["nb", "t", "caller-token", "extra"]
    maybe_inject_client_token(
        registry,
        RPCMethod.LIST_NOTEBOOKS,
        params,
        operation_variant=None,
    )

    assert params[2] == "caller-token"


def test_client_token_dedupe_positional_out_of_range_warns_and_noops(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Out-of-range positional index MUST log a warning and no-op
    (foundation safety guard — don't crash a live RPC over registry drift)."""
    from notebooklm._idempotency import maybe_inject_client_token

    registry = IdempotencyRegistry()
    registry.register(
        RPCMethod.LIST_NOTEBOOKS,
        IdempotencyPolicy.CLIENT_TOKEN_DEDUPE,
        client_token_field=99,  # out of range
    )

    params: list[Any] = ["only", "two"]
    with caplog.at_level(logging.WARNING, logger="notebooklm._idempotency"):
        maybe_inject_client_token(
            registry,
            RPCMethod.LIST_NOTEBOOKS,
            params,
            operation_variant=None,
        )

    # Params unchanged
    assert params == ["only", "two"]
    # Warning emitted
    warn_records = [
        r
        for r in caplog.records
        if r.name.startswith("notebooklm._idempotency") and r.levelno >= logging.WARNING
    ]
    assert len(warn_records) == 1
    assert "out-of-range" in warn_records[0].message


def test_client_token_dedupe_field_shape_mismatch_noops() -> None:
    """A ``str`` ``client_token_field`` with list-shaped params (or an
    ``int`` field with dict-shaped params) MUST no-op rather than crash."""
    from notebooklm._idempotency import maybe_inject_client_token

    # str field, list params → no-op
    registry = IdempotencyRegistry()
    registry.register(
        RPCMethod.LIST_NOTEBOOKS,
        IdempotencyPolicy.CLIENT_TOKEN_DEDUPE,
        client_token_field="client_token",
    )
    list_params: list[Any] = ["a", "b"]
    maybe_inject_client_token(
        registry, RPCMethod.LIST_NOTEBOOKS, list_params, operation_variant=None
    )
    assert list_params == ["a", "b"]

    # int field, dict params → no-op
    registry2 = IdempotencyRegistry()
    registry2.register(
        RPCMethod.LIST_NOTEBOOKS,
        IdempotencyPolicy.CLIENT_TOKEN_DEDUPE,
        client_token_field=0,
    )
    dict_params: dict[str, Any] = {"foo": "bar"}
    maybe_inject_client_token(
        registry2, RPCMethod.LIST_NOTEBOOKS, dict_params, operation_variant=None
    )
    assert dict_params == {"foo": "bar"}


def test_client_token_dedupe_is_noop_for_other_policies() -> None:
    """Token injection MUST be skipped for non-CLIENT_TOKEN_DEDUPE policies."""
    from notebooklm._idempotency import maybe_inject_client_token

    registry = IdempotencyRegistry()
    registry.register(RPCMethod.LIST_NOTEBOOKS, IdempotencyPolicy.UNCLASSIFIED)

    params: dict[str, Any] = {"foo": "bar"}
    maybe_inject_client_token(
        registry,
        RPCMethod.LIST_NOTEBOOKS,
        params,
        operation_variant=None,
    )

    assert "client_token" not in params


# ---------------------------------------------------------------------------
# RpcExecutor consultation: behavioral equivalence with default registry
# ---------------------------------------------------------------------------


@pytest.fixture
def _build_rpc_executor() -> Any:
    """Build a minimally-wired RpcExecutor for behavioral-equivalence tests.

    The executor is driven via its ``execute()`` method (the lowest of the
    five consultation sites). The fixture stubs the transport so we can
    assert on the ``disable_internal_retries`` value that the executor
    actually hands to ``_perform_authed_post``.
    """
    from notebooklm._core_rpc import RpcExecutor

    captured: dict[str, Any] = {}

    async def _fake_perform_authed_post(
        *,
        build_request: Any,
        log_label: str,
        disable_internal_retries: bool = False,
        rpc_method: str | None = None,
    ) -> httpx.Response:
        captured["disable_internal_retries"] = disable_internal_retries
        captured["log_label"] = log_label
        captured["rpc_method"] = rpc_method
        return httpx.Response(200, text=")]}'\n[]")

    owner = MagicMock()
    owner._timeout = 30.0
    owner._refresh_callback = None
    owner._refresh_retry_delay = 0.0
    owner._http_client = MagicMock()
    owner._perform_authed_post = AsyncMock(side_effect=_fake_perform_authed_post)
    owner._await_refresh = AsyncMock()
    owner._begin_transport_post = AsyncMock(return_value=object())
    owner._finish_transport_post = AsyncMock()
    owner._increment_metrics = MagicMock()
    owner._emit_rpc_event = AsyncMock()
    owner.rpc_call = AsyncMock()
    owner._rpc_call_impl = AsyncMock()

    def _decode(raw: str, rpc_id: str, *, allow_null: bool = False) -> Any:
        return []

    async def _sleep(_: float) -> None:
        return None

    def _is_auth_error(_: Exception) -> bool:
        return False

    executor = RpcExecutor(
        owner,
        decode_response_late_bound=_decode,
        is_auth_error=_is_auth_error,
        sleep=_sleep,
    )
    return executor, owner, captured


@pytest.mark.asyncio
async def test_default_registry_preserves_today_behavior(
    _build_rpc_executor: Any,
) -> None:
    """Behavioral equivalence: with the production registry's default
    UNCLASSIFIED policy for every method, an unspecified
    ``disable_internal_retries`` MUST resolve to False — exactly today's
    behavior. No drift."""
    executor, owner, captured = _build_rpc_executor

    await executor.execute(
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
    executor, owner, captured = _build_rpc_executor

    await executor.execute(
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
    ``RpcExecutor.execute()`` without breaking the call. Wave 2 will wire
    behavioral effects; this PR only adds the seam."""
    executor, owner, captured = _build_rpc_executor

    # Should not raise — kwarg is accepted everywhere.
    await executor.execute(
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
