"""Tests for client-owned composition primitives.

Covers the helpers introduced by Stage B1 PR 1 and made live by Stage B1
PR 2 of the post-refactoring plan
(``docs/post-refactoring-plan-2026-05-27.md``):

- :class:`notebooklm._runtime.init.ClientInternals` dataclass
- :func:`notebooklm._runtime.init.compose_client_internals`
- ``ClientComposed.bind_*`` write-once setters
- ``ClientComposed`` required-property guards

The redundant ``resolve_seam_defaults`` resolver was removed in issue
#1327 — ``compose_client_internals`` resolves seams directly via
``resolve_client_seams`` + ``_resolve_async_client_factory``, so the
parallel dict-shaped resolver had no production caller.

Session-elimination Phase 3 leaves ``NotebookLMClient`` as both composition
root and public surface; all composition runtime state belongs to
``ClientComposed`` or the client-owned collaborator bundle.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from notebooklm._client_composed import ClientComposed
from notebooklm._client_seams import ClientSeams
from notebooklm._runtime.init import (
    ClientInternals,
    compose_client_internals,
)
from notebooklm.auth import AuthTokens
from notebooklm.client import NotebookLMClient
from tests._helpers.client_factory import build_client_shell_for_tests


def _make_auth() -> AuthTokens:
    """Build a minimal :class:`AuthTokens` for composition tests.

    Cookies / CSRF / session id are sentinel values — these tests never
    hit the network; they only need a token shape that passes
    :func:`_validate_required_cookies`.
    """
    return AuthTokens(
        cookies={"SID": "x", "__Secure-1PSIDTS": "y"},
        csrf_token="csrf",
        session_id="sid",
    )


# ---------------------------------------------------------------------------
# compose_client_internals — client-owned composition root
# ---------------------------------------------------------------------------


def test_compose_client_internals_returns_client_internals() -> None:
    """The helper returns collaborators + executor while binding ``ClientComposed``."""
    holder = ClientComposed()
    internals = compose_client_internals(auth=_make_auth(), composed=holder)

    assert isinstance(internals, ClientInternals)
    assert holder.executor is internals.executor
    assert holder.runtime_collaborators is internals.collaborators
    assert holder.transport is internals.executor._transport
    assert holder.chain_host._transport is holder.transport
    assert holder.chain_builder is not None
    assert len(holder.middlewares) == 7


def test_shell_helpers_carry_client_holders() -> None:
    """Client shell helpers mirror production holder attributes."""
    client = build_client_shell_for_tests(auth=_make_auth(), max_concurrent_rpcs=3)

    assert isinstance(client._seams, ClientSeams)
    assert isinstance(client._composed, ClientComposed)
    assert client._composed.max_concurrent_rpcs == 3
    assert client._composed.runtime_collaborators is client._collaborators
    assert client._composed.executor is client._rpc_executor


def test_notebooklm_client_initializes_client_holders() -> None:
    """Production clients own the same holder shape returned by composition."""
    client = NotebookLMClient(_make_auth(), max_concurrent_rpcs=2)

    assert isinstance(client._seams, ClientSeams)
    assert isinstance(client._composed, ClientComposed)
    assert client._composed.runtime_collaborators is client._collaborators
    assert client._composed.max_concurrent_rpcs == 2
    assert client._composed.executor is client._rpc_executor
    assert client._composed.transport is client._rpc_executor._transport


def test_invalid_max_concurrent_rpcs_rejected_before_zero_cap_semaphore() -> None:
    """Production and test construction reject invalid caps before composition use."""
    auth = _make_auth()

    with pytest.raises(ValueError, match="max_concurrent_rpcs must be >= 1, got 0"):
        NotebookLMClient(auth, max_concurrent_rpcs=0)

    with pytest.raises(ValueError, match="max_concurrent_rpcs must be >= 1, got 0"):
        build_client_shell_for_tests(auth, max_concurrent_rpcs=0)


def test_prebuilt_client_composed_cap_must_match_constructor_cap() -> None:
    """A supplied holder cannot silently diverge from validated constructor args."""
    holder = ClientComposed(max_concurrent_rpcs=5)

    with pytest.raises(
        ValueError,
        match=(
            r"composed\.max_concurrent_rpcs must match max_concurrent_rpcs "
            r"\(got composed\.max_concurrent_rpcs=5, max_concurrent_rpcs=10\)"
        ),
    ):
        compose_client_internals(
            auth=_make_auth(),
            max_concurrent_rpcs=10,
            composed=holder,
        )


def test_compose_client_internals_refuses_synthetic_error_first(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``_refuse_synthetic_error_outside_test_context`` MUST run before any
    other work in :func:`compose_client_internals`.

    Pins the same contract as
    :mod:`tests.unit.concurrency.test_synthetic_error_transport_guard` —
    the guard fires at the *earliest* opportunity. Setting the env var
    without ``PYTEST_CURRENT_TEST`` must raise from the helper before the
    seam resolution, validation, or collaborator construction can run.
    """
    monkeypatch.setenv("NOTEBOOKLM_VCR_RECORD_ERRORS", "5xx")
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    with (
        caplog.at_level(logging.WARNING, logger="notebooklm._core"),
        pytest.raises(RuntimeError, match="NOTEBOOKLM_VCR_RECORD_ERRORS"),
    ):
        compose_client_internals(auth=_make_auth())


def test_compose_client_internals_preserves_late_binding_for_decode_response() -> None:
    """Post-construction ``seams.decode_response = rebound`` MUST still
    steer the executor's decode path.

    Pins the lambda-closure contract documented in the plan: the executor
    is wired with ``decode_response=lambda *a, **kw: seams.decode_response(*a, **kw)``
    so that test reassignments after construction continue to take effect.
    """
    seams = ClientSeams(
        decode_response=lambda *_a, **_kw: None,
        sleep=asyncio.sleep,
        is_auth_error=lambda _exc: False,
    )
    internals = compose_client_internals(auth=_make_auth(), seams=seams)

    sentinel: list[Any] = []

    def rebound(*args: Any, **kwargs: Any) -> str:
        """Recording stand-in for ``seams.decode_response``."""
        sentinel.append(("decoded", args, kwargs))
        return "rebound-result"

    seams.decode_response = rebound

    # The executor closure should dispatch through the live attribute,
    # not the value frozen at construction time.
    result = internals.executor._decode_response("payload", "method-id", allow_null=False)
    assert result == "rebound-result"
    assert sentinel and sentinel[-1][0] == "decoded"


def test_compose_client_internals_preserves_late_binding_for_is_auth_error() -> None:
    """Post-construction ``seams.is_auth_error = rebound`` MUST still
    steer the executor's classifier.

    Mirror of the ``decode_response`` test for the auth-error seam.
    """
    seams = ClientSeams(
        decode_response=lambda *_a, **_kw: None,
        sleep=asyncio.sleep,
        is_auth_error=lambda _exc: False,
    )
    internals = compose_client_internals(auth=_make_auth(), seams=seams)

    def rebound(exc: Exception) -> bool:
        """Stand-in classifier — treats KeyError as auth-related."""
        return isinstance(exc, KeyError)

    seams.is_auth_error = rebound

    assert internals.executor._is_auth_error(KeyError("auth")) is True
    assert internals.executor._is_auth_error(RuntimeError("nope")) is False


def test_compose_client_internals_preserves_late_binding_for_sleep() -> None:
    """Post-construction ``seams.sleep = rebound`` MUST still steer the
    executor's backoff path.
    """
    seams = ClientSeams(
        decode_response=lambda *_a, **_kw: None,
        sleep=asyncio.sleep,
        is_auth_error=lambda _exc: False,
    )
    internals = compose_client_internals(auth=_make_auth(), seams=seams)

    calls: list[float] = []

    async def rebound(delay: float) -> None:
        """Recording stand-in for ``seams.sleep`` (captures delays)."""
        calls.append(delay)

    seams.sleep = rebound

    asyncio.run(internals.executor._sleep(0.25))
    assert calls == [0.25]


def test_compose_client_internals_preserves_late_binding_for_refresh_retry_delay() -> None:
    """Post-construction ``chain_host._refresh_retry_delay = X`` MUST be seen
    by the executor's ``refresh_retry_delay_provider`` lambda on the next
    call.

    The integration-test contract is that
    ``client._composed.chain_host._refresh_retry_delay = 0`` continues
    to steer the live chain after construction. The lambda
    ``refresh_retry_delay_provider=lambda: chain_host._refresh_retry_delay``
    re-reads the attribute on every invocation, so this is a live binding,
    not a frozen snapshot.
    """
    holder = ClientComposed()
    internals = compose_client_internals(auth=_make_auth(), composed=holder)

    chain_host = holder.chain_host
    # The provider lambda must dereference the *current* attribute on
    # each call — not the value captured at construction time.
    initial = chain_host._refresh_retry_delay
    assert internals.executor._refresh_retry_delay_provider() == initial

    chain_host._refresh_retry_delay = 0.99
    assert internals.executor._refresh_retry_delay_provider() == 0.99


def test_compose_client_internals_executor_timeout_provider_reads_lifecycle() -> None:
    """The executor's ``timeout_provider`` reads from the live
    ``ClientLifecycle._timeout`` collaborator attribute.

    Pins the documented closure shape
    ``timeout_provider=lambda: collaborators.lifecycle._timeout`` (plan
    line 253). A lifecycle-side mutation must surface on the next executor
    call without re-binding.
    """
    internals = compose_client_internals(auth=_make_auth())

    initial = internals.collaborators.lifecycle._timeout
    assert internals.executor._timeout_provider() == initial

    internals.collaborators.lifecycle._timeout = 99.0
    assert internals.executor._timeout_provider() == 99.0


# ---------------------------------------------------------------------------
# ClientComposed write-once binders
# ---------------------------------------------------------------------------


def test_client_composed_executor_binder_raises_on_double_bind() -> None:
    holder = ClientComposed()
    compose_client_internals(auth=_make_auth(), composed=holder)

    with pytest.raises(RuntimeError, match="_executor already bound"):
        holder.bind_executor(holder.executor)


def test_client_composed_transport_binder_raises_on_double_bind() -> None:
    holder = ClientComposed()
    compose_client_internals(auth=_make_auth(), composed=holder)

    with pytest.raises(RuntimeError, match="_transport already bound"):
        holder.bind_transport(holder.transport)


def test_client_composed_chain_metadata_binder_raises_on_double_bind() -> None:
    holder = ClientComposed()
    compose_client_internals(auth=_make_auth(), composed=holder)

    # Build a sentinel ``WiredMiddleware`` carrying the existing values so
    # the rejection comes from the write-once guard, not a missing field.
    from notebooklm._runtime.init import WiredMiddleware

    wired = WiredMiddleware(
        chain_builder=holder.chain_builder,
        middlewares=holder.middlewares,
        authed_post_chain=holder.chain_host._authed_post_chain,
    )
    with pytest.raises(RuntimeError, match="_chain_builder already bound"):
        holder.bind_chain_metadata(wired)


def test_client_composed_chain_host_binder_raises_on_double_bind() -> None:
    holder = ClientComposed()
    compose_client_internals(auth=_make_auth(), composed=holder)

    with pytest.raises(RuntimeError, match="_chain_host already bound"):
        holder.bind_chain_host(holder.chain_host)


def test_client_composed_runtime_collaborators_binder_raises_on_double_bind() -> None:
    holder = ClientComposed()
    internals = compose_client_internals(auth=_make_auth(), composed=holder)

    with pytest.raises(RuntimeError, match="_runtime_collaborators already bound"):
        holder.bind_runtime_collaborators(internals.collaborators)


# ---------------------------------------------------------------------------
# ClientComposed required-property guards
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("attr_name", "message"),
    [
        ("transport", "_transport"),
        ("executor", "_executor"),
        ("chain_host", "_chain_host"),
        ("chain_builder", "_chain_builder"),
        ("middlewares", "_middlewares"),
        ("runtime_collaborators", "_runtime_collaborators"),
    ],
)
def test_client_composed_properties_raise_before_binding(attr_name: str, message: str) -> None:
    holder = ClientComposed()

    with pytest.raises(
        RuntimeError,
        match=rf"ClientComposed not fully constructed: {message} is None",
    ):
        getattr(holder, attr_name)


# ---------------------------------------------------------------------------
# ClientComposed RPC-semaphore loop-affinity guard / reset (issue #1169)
# ---------------------------------------------------------------------------


def test_get_rpc_semaphore_returns_nullcontext_when_cap_is_none() -> None:
    """``max_concurrent_rpcs=None`` short-circuits to a no-op context.

    The affinity guard MUST NOT run on the unbounded opt-out path — there is
    no loop-bound primitive to protect, so even an unset binding is fine.
    """
    from contextlib import nullcontext

    holder = ClientComposed(max_concurrent_rpcs=None)
    # No ``set_bound_loop`` call — the None-cap path never touches the guard.
    ctx = holder.get_rpc_semaphore()
    assert isinstance(ctx, type(nullcontext()))


def test_get_rpc_semaphore_no_binding_is_silent_noop() -> None:
    """An unbound holder builds the semaphore without raising.

    Mirrors the sibling primitives: ``assert_bound_loop(None)`` is a silent
    no-op so standalone holders (composition / unit fixtures) that never ran
    ``open()`` keep working.
    """

    async def _exercise() -> None:
        holder = ClientComposed(max_concurrent_rpcs=2)
        # ``_bound_loop`` is None — the guard is a no-op and the semaphore is
        # built lazily on this running loop.
        async with holder.get_rpc_semaphore():
            pass
        assert holder._rpc_semaphore is not None

    asyncio.run(_exercise())


def test_get_rpc_semaphore_same_loop_use_unaffected() -> None:
    """A holder bound to the running loop acquires its semaphore normally."""

    async def _exercise() -> None:
        holder = ClientComposed(max_concurrent_rpcs=2)
        holder.set_bound_loop(asyncio.get_running_loop())
        async with holder.get_rpc_semaphore():
            pass
        # Same instance is reused across calls on the same loop.
        first = holder._rpc_semaphore
        async with holder.get_rpc_semaphore():
            pass
        assert holder._rpc_semaphore is first

    asyncio.run(_exercise())


def test_get_rpc_semaphore_cross_loop_raises_actionable_runtime_error() -> None:
    """A holder bound to loop A raises if its semaphore is acquired on loop B.

    Two independent ``asyncio.run`` calls give two genuinely distinct loops.
    The guard must fire with the shared loop-affinity diagnostic before the
    stale ``asyncio.Semaphore`` (bound to the dead loop A) is reused.
    """
    holder = ClientComposed(max_concurrent_rpcs=2)

    async def _bind_under_loop_a() -> None:
        holder.set_bound_loop(asyncio.get_running_loop())
        # Construct the semaphore on loop A so loop B would reuse a stale one.
        async with holder.get_rpc_semaphore():
            pass

    asyncio.run(_bind_under_loop_a())
    assert holder._rpc_semaphore is not None

    async def _acquire_under_loop_b() -> None:
        with pytest.raises(RuntimeError, match="bound to a different event loop"):
            async with holder.get_rpc_semaphore():
                pass
        # The actionable second sentence tells users how to fix it.
        try:
            async with holder.get_rpc_semaphore():
                pass
        except RuntimeError as exc:
            assert "create a new client in the target loop" in str(exc)
        else:  # pragma: no cover - defensive
            raise AssertionError("expected RuntimeError on cross-loop reuse")

    asyncio.run(_acquire_under_loop_b())


def test_reset_after_open_discards_lazy_semaphore() -> None:
    """``reset_after_open`` drops the cached semaphore so the next call rebuilds.

    This is what lets a client closed on loop A and reopened on loop B build a
    fresh semaphore bound to loop B rather than reusing the stale one. The cap
    itself is left untouched.
    """

    async def _exercise() -> None:
        holder = ClientComposed(max_concurrent_rpcs=3)
        holder.set_bound_loop(asyncio.get_running_loop())
        async with holder.get_rpc_semaphore():
            pass
        first = holder._rpc_semaphore
        assert first is not None

        holder.reset_after_open()
        assert holder._rpc_semaphore is None
        assert holder.max_concurrent_rpcs == 3

        async with holder.get_rpc_semaphore():
            pass
        assert holder._rpc_semaphore is not None
        assert holder._rpc_semaphore is not first

    asyncio.run(_exercise())


def test_set_bound_loop_none_clears_binding_and_discards_semaphore() -> None:
    """``set_bound_loop(None)`` re-arms the no-op path and drops the stale semaphore."""

    async def _exercise() -> None:
        holder = ClientComposed(max_concurrent_rpcs=2)
        holder.set_bound_loop(asyncio.get_running_loop())
        async with holder.get_rpc_semaphore():
            pass
        assert holder._rpc_semaphore is not None
        # Clearing the binding is a loop change (loop -> None), so the cached
        # semaphore bound to the old loop is discarded for self-consistency.
        holder.set_bound_loop(None)
        assert holder._bound_loop is None
        assert holder._rpc_semaphore is None
        # With the binding cleared the guard is a no-op again.
        async with holder.get_rpc_semaphore():
            pass

    asyncio.run(_exercise())


def test_set_bound_loop_same_loop_keeps_cached_semaphore() -> None:
    """Re-binding to the *same* loop must NOT discard the live semaphore.

    Idempotent ``set_bound_loop`` calls with the unchanged loop are a no-op on
    the cache — only a genuine loop change invalidates it.
    """

    async def _exercise() -> None:
        holder = ClientComposed(max_concurrent_rpcs=2)
        loop = asyncio.get_running_loop()
        holder.set_bound_loop(loop)
        async with holder.get_rpc_semaphore():
            pass
        first = holder._rpc_semaphore
        assert first is not None
        # Same loop again — the cached semaphore survives.
        holder.set_bound_loop(loop)
        assert holder._rpc_semaphore is first

    asyncio.run(_exercise())


def test_set_bound_loop_different_loop_discards_stale_semaphore() -> None:
    """A loop change via ``set_bound_loop`` alone discards the stale semaphore.

    This pins the gemini-flagged self-consistency contract: even without a
    matching ``reset_after_open`` call, rebinding to a different loop must
    invalidate the semaphore bound to the previous loop so it is never reused.
    """
    holder = ClientComposed(max_concurrent_rpcs=2)

    async def _bind_and_build_under_loop_a() -> None:
        holder.set_bound_loop(asyncio.get_running_loop())
        async with holder.get_rpc_semaphore():
            pass

    asyncio.run(_bind_and_build_under_loop_a())
    assert holder._rpc_semaphore is not None

    async def _rebind_under_loop_b() -> None:
        # set_bound_loop to a genuinely different loop must drop the stale
        # semaphore so the next get_rpc_semaphore() rebuilds on loop B.
        holder.set_bound_loop(asyncio.get_running_loop())
        assert holder._rpc_semaphore is None
        async with holder.get_rpc_semaphore():
            pass
        assert holder._rpc_semaphore is not None

    asyncio.run(_rebind_under_loop_b())


def test_client_shell_reads_composition_from_client_composed() -> None:
    client = build_client_shell_for_tests(_make_auth())

    assert client._rpc_executor is client._composed.executor
    assert client._rpc_executor._transport is client._composed.transport
    assert client._composed.chain_host._transport is client._composed.transport
    assert client._composed.chain_builder._drain_tracker is client._collaborators.drain_tracker
    assert client._composed.middlewares[0]._drain_tracker is client._collaborators.drain_tracker
    assert client._composed.middlewares[1]._metrics is client._collaborators.metrics
