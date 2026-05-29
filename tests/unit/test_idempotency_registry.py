"""Tests for the RPC idempotency registry.

The registry is a 6-policy classification layer with operation variants
that the ``RpcExecutor`` consults to compute
``effective_disable_internal_retries`` and optional client-token injection.
The production registry must explicitly classify every ``RPCMethod``; the
``UNCLASSIFIED`` policy is retained only as a placeholder for hand-built
test registries and future-drift detection.
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
        (RPCMethod.START_FAST_RESEARCH, None): IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY,
        (RPCMethod.START_DEEP_RESEARCH, None): IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY,
        (RPCMethod.IMPORT_RESEARCH, None): IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY,
        (RPCMethod.GENERATE_MIND_MAP, None): IdempotencyPolicy.PROBE_THEN_CREATE,
        (RPCMethod.CREATE_NOTE, None): IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY,
        (RPCMethod.CREATE_NOTE, "plain"): IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY,
        (RPCMethod.CREATE_NOTE, "saved_from_chat"): IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY,
        (RPCMethod.SHARE_NOTEBOOK, None): IdempotencyPolicy.PROBE_THEN_CREATE,
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


def test_non_idempotent_no_retry_entries_document_dedupe_gap() -> None:
    """Hard no-retry methods must explain why blind retry cannot be safe."""
    expected_terms = {
        (RPCMethod.ADD_SOURCE, None): ("operation_variant", "blind retry"),
        (RPCMethod.ADD_SOURCE, "text"): ("no reliable dedupe key",),
        (RPCMethod.EXPORT_ARTIFACT, None): ("external docs/sheets", "no client-token"),
        (RPCMethod.REVISE_SLIDE, None): ("no client-token", "blind retry"),
        (RPCMethod.START_FAST_RESEARCH, None): ("ambiguous", "same query"),
        (RPCMethod.START_DEEP_RESEARCH, None): ("ambiguous", "same query"),
        (RPCMethod.IMPORT_RESEARCH, None): ("cannot bind", "same urls"),
        (RPCMethod.CREATE_NOTE, None): ("no client-token", "no client-visible note_id"),
        (RPCMethod.CREATE_NOTE, "plain"): ("no client-token", "no client-visible note_id"),
        (RPCMethod.CREATE_NOTE, "saved_from_chat"): (
            "no client-token",
            "no client-visible note_id",
        ),
    }

    for (method, variant), terms in expected_terms.items():
        entry = IDEMPOTENCY_REGISTRY.get_entry(method, operation_variant=variant)

        assert entry.policy is IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY
        lower_notes = entry.notes.lower()
        for term in terms:
            assert term in lower_notes


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
# RpcExecutor consultation: behavioral equivalence with active registry
# ---------------------------------------------------------------------------


@pytest.fixture
def _build_rpc_executor() -> Any:
    """Build a minimally-wired RpcExecutor for behavioral-equivalence tests.

    The executor is driven via its ``_execute_once()`` method (the lowest of the
    five consultation sites). The fixture stubs the transport so we can
    assert on the ``disable_internal_retries`` value that the executor
    actually hands to ``_perform_authed_post``.
    """
    from notebooklm._rpc_executor import RpcExecutor

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

    # ADR-014 Rule 5 (Wave 4 of session-decoupling): RpcExecutor takes
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
    # shape. After Wave 4 of session-decoupling (ADR-014 Rule 5), the executor
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
